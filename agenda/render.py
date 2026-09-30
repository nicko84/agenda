"""Plain-text views of the agenda for a terminal or a session's context."""

from __future__ import annotations

from datetime import datetime

from .identity import NICK, Actor
from .store import AGENDA, OPEN, SYSTEM, Item, Message, Session, iso
from .timeutil import ago, when

INDENT = " " * 6


def label(item: Item) -> str:
    if item.from_nick:
        return f"Nick's {item.kind}"
    return f"BLOCKING {item.kind}" if item.blocking else item.kind


def state(item: Item, now: datetime, *, compact: bool = False) -> str:
    if item.status != OPEN:
        ended = item.outcome or item.status
        return f"{ended} {when(item.closed_at, now)}" if item.closed_at else ended
    whom = "Nick" if item.waiting_on == NICK else item.name_of(item.owner)
    text = f"waiting {ago(item.waiting_since, now)}" if compact else f"waiting on {whom} {ago(item.waiting_since, now)}"
    if item.deferred_until and item.deferred_until > iso(now):
        text += f", deferred until {when(item.deferred_until, now)}"
    return text


def heading(item: Item, now: datetime, *, compact: bool = False) -> str:
    where = " › ".join(x for x in (item.project, item.feature) if x)
    parts = [label(item) + (" (quick)" if item.quick else ""), item.name_of(item.owner), *([where] if where else []),
             *([item.lane] if item.lane else []), state(item, now, compact=compact)]
    return f"#{item.id:<5}" + " · ".join(parts)


def queue(items: list[Item], deferred: int, now: datetime) -> str:
    head = f"{len(items)} waiting on Nick:" if items else "Nothing waiting on Nick."
    if deferred:
        head += f" ({deferred} deferred; agenda list --deferred)"
    return "\n\n".join([head, *(_entry(item, now) for item in items)])


def owed(items: list[Item], now: datetime, whose: str) -> str:
    if not items:
        return f"Nothing waiting on {whose}."
    return "\n\n".join([f"{len(items)} waiting on {whose}:", *(_entry(item, now) for item in items)])


def mine(items: list[Item], now: datetime) -> str:
    if not items:
        return "You own or watch no open items."
    return "\n\n".join([f"{len(items)} open items you own or watch:", *(_entry(item, now) for item in items)])


def inbox(items: list[Item], actor: Actor, now: datetime) -> str:
    if not items:
        return "Nothing new."
    noun = "item" if len(items) == 1 else "items"
    blocks = [f"News on {len(items)} {noun}:"]
    for item in items:
        news = item.news_for(actor.id)
        unread = {m.id for m in news}
        brand_new = item.from_nick and all(m.id in unread for m in item.messages if m.author == NICK)
        title = f"NEW {item.kind} from Nick" if brand_new else f'{item.kind} "{item.title}"'
        if item.status != OPEN:
            status = state(item, now)
        elif item.owner == actor.id:
            status = "your turn" if item.waiting_on != NICK else "waiting on Nick"
        else:
            status = f"owned by {item.name_of(item.owner)}; you're watching"
        lines = [f"#{item.id:<5}{title} · {status}"]
        lines += [_said(item, m, now) for m in news]
        blocks.append("\n".join(lines))
    if any(i.status == OPEN and i.owner == actor.id and i.waiting_on != NICK for i in items):
        blocks.append("Reply to Nick with `agenda reply ID TEXT`; once you have acted, `agenda close ID [NOTE]`.")
    return "\n\n".join(blocks)


def show(item: Item, now: datetime) -> str:
    out = [heading(item, now), item.title]
    if item.context:
        out += ["", f"Context: {item.context}"]
    if item.summary != item.title:
        out += ["", item.summary]
    if item.options:
        out += ["", "Options:", *(f"  {n}. {option}" for n, option in enumerate(item.options, 1))]
    if item.recommendation:
        out += ["", f"Recommends: {item.recommendation}"]
    if item.default_option:
        out += [f"Default if no answer by {when(item.default_at, now)}: {item.default_option}"]
    if item.detail:
        out += ["", "Detail:", _indent(item.detail, "  ")]
    if item.briefing:
        out += ["", "Briefing:", _indent(item.briefing, "  ")]
    facts = [f"Raised by {item.name_of(item.raised_by)}, owned by {item.name_of(item.owner)}"]
    others = [item.name_of(s) for s in item.watchers if s != item.owner]
    if others:
        facts.append(f"Watching: {', '.join(others)}")
    if item.topics:
        facts.append(f"Topics: {', '.join(item.topics)}")
    if item.attachments:
        facts.append("Attachments: " + ", ".join(a.url or a.name for a in item.attachments))
    if item.outcome:
        facts.append(f"Outcome: {item.outcome}" + (f" — {item.resolution}" if item.resolution else ""))
    out += ["", *facts]
    if item.messages:
        width = max(len(item.name_of(m.author)) for m in item.messages)
        out += ["", "Thread:"]
        for m in item.messages:
            who = f"{item.name_of(m.author)}{' asks' if m.is_question else ''}"
            lead = f"  {when(m.created_at, now):>9}  {who:<{width + 5}}  "
            out.append(_hang(m.body, lead, " " * len(lead)))
    return "\n".join(out)


def history(items: list[Item], now: datetime) -> str:
    if not items:
        return "Nothing closed yet."
    lines = []
    for i in items:
        lines.append(f"#{i.id:<5}{state(i, now):<20} {label(i):<18} {i.name_of(i.owner):<16} {i.title}")
        if i.resolution:
            lines.append(f"{INDENT}→ {i.resolution}")
    return "\n".join(lines)


def sessions(rows: list[tuple[Session, int, int]], now: datetime) -> str:
    """(session, open items it owns, items with unread news)."""
    if not rows:
        return "No sessions have used the agenda yet."
    lines = []
    for s, owned, unread in rows:
        where = f"pane {s.pane_id}" if s.pane_id else "no pane"
        lines.append(
            f"{s.name:<22} {s.role:<12} {s.agent or '?':<7} {where:<14} seen {ago(s.last_seen, now):>4} ago · "
            f"owns {owned} open · news on {unread}"
        )
    return "\n".join(lines)


def _entry(item: Item, now: datetime) -> str:
    lines = [heading(item, now, compact=True)]
    if item.from_nick:
        lines.append(_hang(item.summary, INDENT, INDENT))
    else:
        lines += [INDENT + item.title, _hang(item.summary, INDENT, INDENT)]
        if item.recommendation and not item.nick_has_answered:
            lines.append(f"{INDENT}Recommends: {item.recommendation}")
        if item.default_option:
            lines.append(f"{INDENT}If no answer by {when(item.default_at, now)}: {item.default_option}")
    # The latest word that decides things (Nick's, the owner's, or a default applying), not a note from a
    # bystander; the ask itself is already shown above. Anything after it is counted, not shown.
    spoken = [m for m in item.messages if m.author != SYSTEM]
    deciding = [m for m in spoken if m.author in (NICK, AGENDA, item.owner)]
    main = deciding[-1] if deciding else None
    if main is not None and not (item.from_nick and main is spoken[0]):
        lines.append(_said(item, main, now))
    later = [m for m in spoken if main is None or m.id > main.id]
    if later:
        lines.append(f"{INDENT}(+{len(later)} more on the thread: agenda show {item.id})")
    return "\n".join(lines)


def _said(item: Item, message: Message, now: datetime) -> str:
    who = "Nick" if message.author == NICK else item.name_of(message.author)
    if message.is_question:
        who += " asks"
    lead = f"{INDENT}{who}, {ago(message.created_at, now)} ago: "
    return _hang(message.body, lead, INDENT + "  ")


def _hang(text: str, first: str, rest: str) -> str:
    lines = text.splitlines() or [""]
    return "\n".join([first + lines[0], *(rest + line if line else line for line in lines[1:])])


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line if line else line for line in text.splitlines())


def news_brief(items: list[Item], session: str, *, limit: int = 6) -> str:
    """A few lines a session can act on without opening anything: what changed, on which item."""
    lines = []
    for item in items[:limit]:
        news = item.news_for(session)
        last = news[-1] if news else None
        who = "Nick" if last and last.author == NICK else (item.name_of(last.author) if last else "")
        verb = " asks" if last and last.is_question else ""
        said = f"{who}{verb}: {_clip(last.body, 140)}" if last else ""
        lines.append(f"#{item.id} {item.kind} \"{_clip(item.title, 70)}\" — {said}")
    if len(items) > limit:
        lines.append(f"…and {len(items) - limit} more")
    return "\n".join(lines)


def _clip(text: str, width: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1].rstrip() + "…"
