"""`agenda` command line: one tool for Nick's side and the sessions' side.

Who is calling is detected, not claimed: inside a Claude Code session, or a herdr pane
running an agent, you are that session; anywhere else you are Nick.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from . import identity, render
from .identity import Actor
from .store import LANE, ORCHESTRATOR, SESSION_KINDS, Agenda, AgendaError, Item
from .timeutil import parse_until, when

DESCRIPTION = """\
A durable queue of topics between Nick and his agent sessions. Every exchange is an
item with a thread; while it is open it waits on Nick or on the session that owns it.

  Nick                list  show  answer  question  accept  ack  done  defer  drop  ask
  Sessions            inbox  mine  owed  raise  update  reply  note  close  withdraw
                      handoff  watch  register  project
  Anyone              show  history  sessions  projects  whoami

Inside a Claude Code session (or a herdr pane running an agent) you are that session;
anywhere else you are Nick. Items are numbered (#14 or 14). Add --json for machine output.
"""


def item_id(value: str) -> int:
    try:
        number = int(value.removeprefix("#"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an item number: {value!r}") from None
    if number < 1:
        raise argparse.ArgumentTypeError(f"not an item number: {value!r}")
    return number


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print JSON instead of text")
    common.add_argument("--db", metavar="PATH", help="database (default $AGENDA_DB or ~/.local/share/agenda/agenda.db)")

    parser = argparse.ArgumentParser(
        prog="agenda", description=DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def command(name: str, help: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help, description=help, parents=[common])

    # Nick
    p = command("list", "what is waiting on Nick, in the order to raise it")
    p.add_argument("--deferred", action="store_true", help="include deferred items")

    p = command("show", "one item in full, with its thread")
    p.add_argument("id", type=item_id)

    p = command("answer", "Nick: reply to an item; hands it to its owner")
    p.add_argument("id", type=item_id)
    p.add_argument("text", nargs="+")

    p = command("question", "Nick: ask the owner something about an item without deciding it")
    p.add_argument("id", type=item_id)
    p.add_argument("text", nargs="+")

    p = command("accept", "Nick: go with the recommendation (several ids at once is fine)")
    p.add_argument("ids", nargs="+", type=item_id, metavar="ID")

    p = command("ack", "Nick: heard it, nothing to add; closes it (several ids at once is fine)")
    p.add_argument("ids", nargs="+", type=item_id, metavar="ID")

    p = command("done", "Nick: I did it myself (a task, or anything); the owner checks and carries on")
    p.add_argument("id", type=item_id)
    p.add_argument("note", nargs="*")

    p = command("defer", "Nick: push an item down the queue for a while")
    p.add_argument("id", type=item_id)
    p.add_argument("until", nargs="?", default="1h", help="30m, 2h, 1d, tomorrow (8am) or an ISO time; default 1h")

    p = command("drop", "Nick: close an item outright; its sessions are told")
    p.add_argument("id", type=item_id)
    p.add_argument("reason", nargs="*")

    p = command("ask", "Nick: file a question (or --instruction) for a session")
    p.add_argument("text", nargs="+")
    p.add_argument("--to", metavar="SESSION", help="tab label or session id; default: the one orchestrator")
    p.add_argument("--instruction", action="store_true", help="something to do, not a question")
    p.add_argument("--title", help="short heading; defaults to the start of the text")
    p.add_argument("--topic", action="append", dest="topics", default=[], metavar="KEY")

    # Sessions
    p = command("register", "session: mark yourself an orchestrator (or --lane) and set your name")
    p.add_argument("--lane", action="store_true", help="register as a lane instead")
    p.add_argument("--label", help="name shown to Nick; defaults to your herdr tab label")

    p = command("inbox", "session: news on your items you haven't read; marks it read")
    p.add_argument("--peek", action="store_true", help="show without marking read")
    p.add_argument("--count", action="store_true", help="print only how many items have news")

    command("mine", "session: every open item you own or watch; read this after a compaction")

    p = command("owed", "session: items waiting on you (--all: on every session)")
    p.add_argument("--all", action="store_true")

    p = command("raise", "session: put a topic on Nick's agenda")
    p.add_argument("kind", choices=SESSION_KINDS)
    p.add_argument("title", help="short heading for a screen; name the repo, file or board")
    p.add_argument("--summary", required=True, help="a sentence or two, written to be heard")
    p.add_argument("--project", help="which project, e.g. 'Member Hub' (see `agenda projects`); required for a new item")
    p.add_argument("--feature", help="which feature or workstream within it; required for a new item")
    p.add_argument("--context", help="what this is and why it has come up, for someone who hasn't followed you")
    p.add_argument("--quick", action="store_true", help="high-confidence recommendation: a quick sign-off will do")
    p.add_argument("--attach", action="append", default=[], metavar="FILE|URL",
                   help="screenshot, render or board link; needed for a design sign-off; repeat for each")
    p.add_argument("--topic", action="append", dest="topics", default=[], metavar="KEY",
                   help="dedupe key, e.g. 'pr webapp#402'; repeat for aliases")
    p.add_argument("--into", type=item_id, metavar="ID", help="join this open item instead of raising a new one")
    p.add_argument("--distinct", action="store_true", help="it only looks like an open item; raise it anyway")
    p.add_argument("--reask", action="store_true", help="the topic was settled but something has changed")
    p.add_argument("--default", help="what you will do if Nick hasn't answered by --by")
    p.add_argument("--by", metavar="WHEN", help="deadline for the default: 30m, 2h, 1d, tomorrow or ISO")
    p.add_argument("--cc", action="append", default=[], metavar="SESSION", help="another session to tell the outcome")
    _add_item_fields(p)
    p.add_argument("--blocking", action="store_true", help="work is stopped until Nick answers")

    p = command("update", "session: revise an item instead of raising a duplicate")
    p.add_argument("id", type=item_id)
    p.add_argument("--title")
    p.add_argument("--summary")
    p.add_argument("--project")
    p.add_argument("--feature")
    p.add_argument("--context")
    p.add_argument("--quick", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--attach", action="append", default=[], metavar="FILE|URL", help="add a screenshot or link")
    p.add_argument("--topic", action="append", dest="topics", default=[], metavar="KEY", help="add a topic alias")
    _add_item_fields(p)
    p.add_argument("--blocking", action=argparse.BooleanOptionalAction, default=None)

    p = command("reply", "session: say something back to Nick; hands the item to him")
    p.add_argument("id", type=item_id)
    p.add_argument("text", nargs="+")

    p = command("note", "session: add context to an item without changing whose turn it is")
    p.add_argument("id", type=item_id)
    p.add_argument("text", nargs="+")

    p = command("close", "session: settled; after acting, or with a note saying how it was settled elsewhere")
    p.add_argument("id", type=item_id)
    p.add_argument("note", nargs="*", help="what you did, or Nick's words if he answered somewhere else")

    p = command("withdraw", "session: retract a topic that no longer matters")
    p.add_argument("id", type=item_id)
    p.add_argument("reason", nargs="+")

    p = command("handoff", "session: make another session the owner")
    p.add_argument("id", type=item_id)
    p.add_argument("to", metavar="SESSION")

    p = command("watch", "session: hear how an item ends without owning it")
    p.add_argument("id", type=item_id)

    # Anyone
    p = command("history", "recently closed and withdrawn items, with how each ended")
    p.add_argument("--limit", type=int, default=20)

    command("sessions", "sessions that have used the agenda, their roles and unread news")
    command("projects", "projects in use, their briefs, and how many open items each has")

    p = command("project", "session: set a project's brief, the state of play Nick hears before its items")
    p.add_argument("name")
    p.add_argument("--brief", required=True, help="three to five sentences: workstreams in flight, what shipped, what's open")
    command("whoami", "who the agenda thinks you are")

    return parser


def _add_item_fields(p: argparse.ArgumentParser) -> None:
    detail = p.add_mutually_exclusive_group()
    detail.add_argument("--detail", help="markdown for a screen")
    detail.add_argument("--detail-file", metavar="PATH", help="read the detail from a file, or - for stdin")
    briefing = p.add_mutually_exclusive_group()
    briefing.add_argument("--briefing", help="what a stand-in needs to answer Nick's follow-ups without you")
    briefing.add_argument("--briefing-file", metavar="PATH", help="read the briefing from a file, or - for stdin")
    p.add_argument("--option", action="append", dest="options", metavar="TEXT", help="a choice; repeat for each")
    p.add_argument("--recommend", dest="recommendation", metavar="TEXT", help="the option you would pick, and why")
    p.add_argument("--lane", help="which workstream this is about, if not obvious from the owner")


def _detail(args: argparse.Namespace) -> str | None:
    return _text_or_file(args.detail, args.detail_file)


def _briefing(args: argparse.Namespace) -> str | None:
    return _text_or_file(args.briefing, args.briefing_file)


def _text_or_file(text: str | None, path: str | None) -> str | None:
    if path is None:
        return text
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as f:
        return f.read()


def run(agenda: Agenda, actor: Actor, args: argparse.Namespace) -> tuple[object, str]:
    """Carry out the command; returns (JSON payload, text)."""
    now = agenda.now
    match args.command:
        case "list":
            items = agenda.queue(include_deferred=args.deferred)
            deferred = 0 if args.deferred else agenda.deferred_count()
            return _dicts(items), render.queue(items, deferred, now())
        case "show":
            item = agenda.read(actor, args.id)
            text = render.show(item, now())
            if not actor.is_nick and actor.id not in item.watchers:
                text += f"\n\nYou're now watching #{item.id}; you'll hear how it ends."
            return item.to_dict(), text
        case "answer":
            item = agenda.answer(actor, args.id, " ".join(args.text))
            return item.to_dict(), f"Answered #{item.id}; it's with {item.name_of(item.owner)} now."
        case "question":
            item = agenda.question(actor, args.id, " ".join(args.text))
            return item.to_dict(), f"Asked {item.name_of(item.owner)} about #{item.id}; the reply comes back to you."
        case "accept":
            return _each(args.ids, lambda i: agenda.accept(actor, i), "Accepted the recommendation on")
        case "ack":
            return _each(args.ids, lambda i: agenda.ack(actor, i), "Closed")
        case "done":
            item = agenda.done(actor, args.id, " ".join(args.note))
            return item.to_dict(), f"Told {item.name_of(item.owner)} you did #{item.id}."
        case "defer":
            item = agenda.defer(actor, args.id, parse_until(args.until, now()))
            return item.to_dict(), f"Deferred #{item.id}: {render.state(item, now())}."
        case "drop":
            item = agenda.drop(actor, args.id, " ".join(args.reason))
            return item.to_dict(), f"Dropped #{item.id}."
        case "ask":
            kind = "instruction" if args.instruction else "ask"
            item = agenda.ask(actor, " ".join(args.text), kind=kind, title=args.title, to=args.to, topics=args.topics)
            return item.to_dict(), f"Filed #{item.id} for {item.name_of(item.owner)}."
        case "register":
            session = agenda.register(actor, role=LANE if args.lane else ORCHESTRATOR, label=args.label)
            return _session_dict(session), f"Registered {session.name} as {session.role}."
        case "inbox":
            if args.count:
                count = agenda.inbox_count(actor)
                return {"count": count}, str(count)
            items = agenda.inbox(actor, mark=not args.peek)
            return _dicts(items), render.inbox(items, actor, now())
        case "mine":
            agenda.touch(actor)
            items = agenda.mine(actor)
            return _dicts(items), render.mine(items, now())
        case "owed":
            items = agenda.owed(None if args.all else actor)
            return _dicts(items), render.owed(items, now(), "any session" if args.all else "you")
        case "raise":
            default_at = parse_until(args.by, now()) if args.by else None
            item, joined = agenda.raise_item(
                actor, args.kind, args.title, args.summary, topics=args.topics, into=args.into,
                distinct=args.distinct, reask=args.reask, detail=_detail(args), briefing=_briefing(args),
                options=args.options or (),
                recommendation=args.recommendation, default=args.default, default_at=default_at,
                lane=args.lane, project=args.project, feature=args.feature, context=args.context, quick=args.quick,
                attach=args.attach, blocking=args.blocking, cc=args.cc,
            )
            if not joined:
                text = f"Raised #{item.id} ({render.label(item)})."
                if item.default_at:
                    text += f" If Nick hasn't answered by {when(item.default_at, now())}, the default applies."
                return item.to_dict(), text
            if item.owner == actor.id:
                return item.to_dict(), f"#{item.id} is already open and yours; posted this as an update."
            return item.to_dict(), (
                f"Joined #{item.id}, owned by {item.name_of(item.owner)}. You're watching it and will hear how it ends."
            )
        case "update":
            item = agenda.update(
                actor, args.id, title=args.title, summary=args.summary, detail=_detail(args), briefing=_briefing(args),
                options=args.options,
                recommendation=args.recommendation, lane=args.lane, topics=args.topics, blocking=args.blocking,
                project=args.project, feature=args.feature, context=args.context, quick=args.quick, attach=args.attach,
            )
            return item.to_dict(), f"Updated #{item.id}."
        case "reply":
            item = agenda.reply(actor, args.id, " ".join(args.text))
            return item.to_dict(), f"Replied on #{item.id}; it's with Nick now."
        case "note":
            item = agenda.note(actor, args.id, " ".join(args.text))
            return item.to_dict(), f"Added a note to #{item.id}."
        case "close":
            item = agenda.close_item(actor, args.id, " ".join(args.note))
            return item.to_dict(), f"Closed #{item.id} ({item.outcome}).{_told(item, actor)}"
        case "withdraw":
            item = agenda.withdraw(actor, args.id, " ".join(args.reason))
            return item.to_dict(), f"Withdrew #{item.id}.{_told(item, actor)}"
        case "handoff":
            item = agenda.handoff(actor, args.id, args.to)
            return item.to_dict(), f"#{item.id} now belongs to {item.name_of(item.owner)}."
        case "watch":
            item = agenda.watch(actor, args.id)
            return item.to_dict(), f"Watching #{item.id}."
        case "history":
            items = agenda.history(args.limit)
            return _dicts(items), render.history(items, now())
        case "sessions":
            rows = _session_rows(agenda)
            return [
                {**_session_dict(s), "owns_open": owned, "news_on": news} for s, owned, news in rows
            ], render.sessions(rows, now())
        case "projects":
            rows = agenda.projects()
            payload = [{**project.__dict__, "open": count} for project, count in rows]
            text = "\n".join(
                f"{project.name} ({count} open)" + (f"\n  {project.brief}" if project.brief else "\n  (no brief yet)")
                for project, count in rows
            ) or "No projects yet."
            return payload, text
        case "project":
            project = agenda.set_brief(actor, args.name, args.brief)
            return project.__dict__, f"Brief for {project.name} saved."
        case "whoami":
            if actor.is_nick:
                return {"id": "nick"}, "Nick."
            agenda.touch(actor)
            session = agenda.session(actor.id)
            return _session_dict(session), f"{session.name} ({session.role}, {session.agent}, {session.id})"
    raise AssertionError(f"unhandled command {args.command!r}")


class PartialFailure(AgendaError):
    """Some of several items went through: `done` for stdout, the refusals for stderr."""

    def __init__(self, payload: list[dict], done: str, refused: str):
        super().__init__(refused)
        self.payload, self.done = payload, done


def _each(ids: list[int], act, verb: str) -> tuple[object, str]:
    """Apply a Nick verb to several items; one refusal doesn't stop the rest."""
    if len(ids) == 1:
        item = act(ids[0])
        return [item.to_dict()], f"{verb} #{item.id}."
    done: list[Item] = []
    refused: list[str] = []
    for number in ids:
        try:
            done.append(act(number))
        except AgendaError as error:
            refused.append(f"agenda: #{number}: {error}")
    text = "\n".join(f"{verb} #{i.id}." for i in done)
    if refused:
        raise PartialFailure(_dicts(done), text, "\n".join(refused))
    return _dicts(done), text


def _session_rows(agenda: Agenda):
    news = {s.id: n for s, n in agenda.news_counts()}
    owned: dict[str, int] = {}
    for item in agenda.owed() + agenda.queue(include_deferred=True):
        owned[item.owner] = owned.get(item.owner, 0) + 1
    return [(s, owned.get(s.id, 0), news.get(s.id, 0)) for s in agenda.sessions()]


def _session_dict(session) -> dict:
    return {**session.__dict__, "name": session.name}


def _told(item: Item, actor: Actor) -> str:
    others = [item.name_of(s) for s in item.watchers if s != actor.id]
    return f" {', '.join(others)} will see it in their inbox." if others else ""


def _dicts(items: list[Item]) -> list[dict]:
    return [item.to_dict() for item in items]


def main(argv: Sequence[str] | None = None, actor: Actor | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    actor = actor or identity.detect()
    try:
        with Agenda(args.db) as agenda:
            payload, text = run(agenda, actor, args)
    except PartialFailure as failure:
        if failure.done or args.json:
            print(json.dumps(failure.payload, indent=2) if args.json else failure.done)
        print(str(failure), file=sys.stderr)
        return 1
    except AgendaError as error:
        print(f"agenda: {error}", file=sys.stderr)
        return 1
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else text)
    return 0
