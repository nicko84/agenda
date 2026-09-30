"""The voice agent: an ElevenLabs agent that walks Nick through his agenda on a call.

The agent runs in ElevenLabs' cloud: speech-to-text, then an LLM (Claude Sonnet 5, Haiku 4.5 as backup), then
Eleven v4 Turbo. It is a host, not a brain. It reads the sessions' own words, answers follow-ups from an
item's briefing, and records Nick's decisions through client tools. Those tools run in his browser on the
agenda page and reach this server over the tailnet, so nothing here is exposed to the internet.

This module holds the agent's configuration (so it lives in git), syncs it to ElevenLabs, and renders the
text the agent reads: a compact overview at the start of a call, and one item in full on request.

    ELEVENLABS_API_KEY=... python3 -m agenda.voice sync      # or under `doppler run`
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from . import plan, settings
from .identity import NICK
from .store import OPEN, SYSTEM, Agenda, Item
from .timeutil import ago, when

API = "https://api.elevenlabs.io"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "agenda" / "voice.json"

LLM = "claude-sonnet-5"
BACKUP_LLMS = ["claude-haiku-4-5"]
TTS_MODEL = "eleven_v4_turbo"
VOICE = settings.get("AGENDA_VOICE_ID", "cjVigY5qzO86Huf0OWal")  # any voice in the ElevenLabs account works

PROMPT = """\
You are the host of Nick's agenda call. Nick runs many Claude Code sessions (orchestrators and lanes) that
put decisions, questions, hands-on tasks and updates for him on a shared agenda. Run the call like a good
meeting: say what you'll cover, take one project at a time and one feature at a time, and give just enough
context that every item makes sense to someone who hasn't followed the work. You record his decisions
accurately and get his questions to the session that owns each item. You are not doing the work, and you
know nothing beyond what the agenda says.

It is {{now}}. The call plan, in the order to take it:
{{agenda_overview}}

How to run the call
- Open with the shape of the call in two or three sentences: how many items, which projects in what order
  (with counts), how many are blocking, and that quick sign-offs will be batched. Ask whether to go in that
  order. He can reorder, skip a project, or ask for "just the blocking ones".
- Start each project with its brief: the workstreams in flight, in two or three sentences, then what you'll
  cover in it ("three features: Campaigns, SEO and Support"). With no brief, sum up the project from its items'
  context. End each project with a one-line wrap-up and the transition to the next.
- Start each feature with its name and one line on what's in it.
- For every item, open with its context (what it is and why it has come up), then the ask, then the
  recommendation and any default deadline. Never open with a detail that only makes sense to the session
  that raised it. Summaries were written to be heard; keep close to their words. Don't read markdown, long IDs
  or URLs aloud. Call show_item as you start each item so his screen follows along.
- Quick sign-offs: take a feature's quick sign-offs together. One sentence each (context plus the
  recommendation), then ask "Happy to approve those N?" He can pull any out ("all except 21"). After he
  confirms, record them in one go with accept_recommendations, naming the numbers you're approving.
- To discuss: one at a time, with more context. Call get_item for the detail and briefing before answering a
  follow-up, and answer only from what it says. If it doesn't cover the question, say you'll ask the owner,
  call ask_owner with his question, and offer to move on while you wait. Never guess or fill gaps.
- Designs: never ask for a design sign-off until he has seen the screenshots. When a project has items with
  screenshots, call send_screenshots for all of them as you start that project and tell him they're in his
  inbox; then check he has them open before asking him to decide. If an item needs a design sign-off but has
  no screenshots, don't ask him to sign off: call ask_owner asking them to attach screenshots, and move on.
- Tasks that need him at a computer: list them briefly at the end of their feature and move on unless he
  wants to go through them. Updates: one line each, then offer to mark them heard together.
- Keep turns short. One item or one batch, one question, then let him talk.

Recording what Nick says
- accept_recommendation (one) or accept_recommendations (several): he goes with the recommendation.
- record_answer: anything else he decides. Pass his words, trimmed of filler. Keep his reasons, add nothing.
- ask_owner: he wants to know something before deciding. This does not decide the item.
- mark_heard: an update or a reply he has heard and needs nothing more on.
- mark_done: he did a task himself. defer_item: later ("1h", "3h", "tomorrow"). drop_item: he doesn't
  want it done at all (the owner is told).
- ask_session: a new question or instruction for a session that isn't about an existing item.
- Before accept, answer or drop, read it back in one short sentence ("Accepting the recommendation on 19,
  wait for Sam. Yes?") and only record it after he clearly confirms. If his reply is unclear or could be
  noise, ask again rather than record. Mark heard, defer and ask_owner need no read-back.
- If a tool returns an error, say so plainly and don't pretend it was recorded.

While you talk
- Messages starting with "AGENDA UPDATE" arrive while the call is running. They are usually a session
  answering a question Nick asked earlier. At the next natural pause, tell him in a sentence and offer to go
  back to that item.
- At the end, or when he says that's all, recap in two or three sentences: what was decided, what was asked
  and is still waiting for an answer, and which projects you didn't get to.
"""

FIRST_MESSAGE = "{{opening}}"


def agent_config() -> dict:
    def tool(name: str, description: str, props: dict | None = None, required: tuple = (), responds: bool = True) -> dict:
        return {
            "type": "client",
            "name": name,
            "description": description,
            "expects_response": responds,
            "response_timeout_secs": 20,
            "parameters": {"type": "object", "properties": props or {}, "required": list(required)},
        }

    item = {"item_id": {"type": "integer", "description": "The agenda item number, e.g. 19"}}
    items = {"item_ids": {"type": "array", "items": {"type": "integer", "description": "An agenda item number"},
                          "description": "Agenda item numbers"}}
    tools = [
        tool("get_item", "Full detail, briefing, options and thread for one item. Call before answering a follow-up.",
             item, ("item_id",)),
        tool("show_item", "Scroll Nick's screen to the item being discussed.", item, ("item_id",), responds=False),
        tool("refresh_agenda", "The current overview of the agenda, after changes."),
        tool("accept_recommendation", "Record that Nick goes with the item's recommendation.", item, ("item_id",)),
        tool("accept_recommendations", "Record that Nick goes with the recommendation on several items (quick sign-offs).",
             items, ("item_ids",)),
        tool("send_screenshots", "Email Nick the screenshots and links for these items, before a design sign-off.",
             items, ("item_ids",)),
        tool("record_answer", "Record Nick's decision or answer on an item, in his words.",
             {**item, "answer": {"type": "string", "description": "Nick's answer, in his words"}}, ("item_id", "answer")),
        tool("ask_owner", "Send Nick's question about an item to the session that owns it; the reply comes back.",
             {**item, "question": {"type": "string", "description": "Nick's question, in his words"}},
             ("item_id", "question")),
        tool("mark_heard", "Nick has heard an update or a reply and needs nothing more; closes it.", item, ("item_id",)),
        tool("mark_done", "Nick did a hands-on task himself; tells the owner to check and carry on.",
             {**item, "note": {"type": "string", "description": "Anything he said about it"}}, ("item_id",)),
        tool("defer_item", "Push an item down the queue for a while.",
             {**item, "until": {"type": "string", "description": "1h, 3h, tomorrow, or an ISO time"}},
             ("item_id", "until")),
        tool("drop_item", "Nick doesn't want this done at all; closes it and tells the owner.",
             {**item, "reason": {"type": "string", "description": "His reason, if he gave one"}}, ("item_id",)),
        tool("ask_session", "A new question or instruction from Nick for a session, not about an existing item.",
             {"text": {"type": "string", "description": "What Nick wants to ask or have done, in his words"},
              "to": {"type": "string", "description": "Which session or orchestrator, if he said"},
              "instruction": {"type": "boolean", "description": "True if it's something to do rather than a question"}},
             ("text",)),
    ]
    return {
        "name": "Agenda (Nick)",
        "conversation_config": {
            "agent": {
                "first_message": FIRST_MESSAGE,
                "language": "en",
                "dynamic_variables": {
                    "dynamic_variable_placeholders": {
                        "now": "Tuesday 9:00",
                        "agenda_overview": "Nothing waiting.",
                        "opening": "Hi Nick, your agenda is empty right now.",
                    }
                },
                "prompt": {
                    "prompt": PROMPT,
                    "llm": LLM,
                    "reasoning_effort": "low",  # extra thinking delays turn-taking
                    "temperature": None,  # Sonnet 5 rejects sampling parameters; None omits it from the request
                    "backup_llm_config": {"preference": "override", "order": BACKUP_LLMS},
                    "tools": tools,
                },
            },
            "tts": {"model_id": TTS_MODEL, "voice_id": VOICE},
            "conversation": {
                "max_duration_seconds": 1800,
                "client_events": [
                    "conversation_initiation_metadata", "audio", "interruption", "user_transcript",
                    "tentative_user_transcript", "agent_response", "agent_response_correction", "client_tool_call",
                    "agent_response_complete", "ping",
                ],
            },
        },
        "platform_settings": {
            "auth": {"enable_auth": True},  # a call needs a token only this server can mint
            "privacy": {"record_voice": False, "delete_audio": True, "retention_days": 30},
        },
    }


# --- What the agent reads -------------------------------------------------


def overview(agenda: Agenda) -> str:
    """The call plan: projects in order with their briefs, features, and each item with its context."""
    now = agenda.now()
    projects = plan.plan(agenda)
    items = [i for p in projects for i in p.items]
    if not items:
        return "Nothing is waiting on Nick."
    quick = sum(plan.bucket(i) == "quick" for i in items)
    shots = sum(bool(i.attachments) for i in items)
    lines = [
        f"{len(items)} items in {len(projects)} projects ({sum(i.blocking for i in items)} blocking, {quick} quick "
        f"sign-offs, {shots} with screenshots). Projects in order: "
        + "; ".join(f"{p.name} ({len(p.items)})" for p in projects) + "."
    ]
    for n, project in enumerate(projects, 1):
        lines.append(f"\nPROJECT {n}: {project.name} ({len(project.items)} items)")
        lines.append(f"  Brief: {project.brief}" if project.brief else "  Brief: none written; sum it up from the items' context.")
        for feature in project.features:
            lines.append(f"  FEATURE: {feature.name} ({len(feature.items)})")
            for name, members in feature.buckets():
                lines.append(f"    {plan.BUCKET_TITLES[name]}:")
                lines += [f"      {_item_line(i, now)}" for i in members]
    lines.append(
        f"\nAlso: {agenda.deferred_count()} deferred, {len(agenda.owed())} answered and with sessions "
        "(not for this call unless he asks)."
    )
    return "\n".join(lines)


def _item_line(i: Item, now: datetime) -> str:
    kind = ("Nick's own " if i.from_nick else "") + i.kind
    line = f"#{i.id} {kind} from {i.name_of(i.owner)}, waiting {ago(i.waiting_since, now)}."
    line += f" Context: {_clip(i.context, 240)}" if i.context else " Context: none given."
    line += f" Ask: {i.title} — {_clip(i.summary, 240)}"
    if i.recommendation and not i.nick_has_answered:
        line += f" Recommends: {_clip(i.recommendation, 140).rstrip('.')}."
    if i.default_option:
        line += f" Default if no answer by {when(i.default_at, now)}: {_clip(i.default_option, 100).rstrip('.')}."
    if i.attachments:
        files = sum(bool(a.path) for a in i.attachments)
        line += f" Screenshots: {files} file(s), {len(i.attachments) - files} link(s)."
    if reply := _latest_reply(i):
        line += f" Latest from {i.name_of(reply.author)}: {_clip(reply.body, 160)}"
    return line


def item_text(item: Item, now: datetime) -> str:
    """Everything the agent may use to answer a follow-up about one item."""
    parts = [
        f"#{item.id} {item.kind} from {item.name_of(item.owner)}, {item.status}"
        + (f", waiting on {'Nick' if item.waiting_on == NICK else item.name_of(item.owner)}" if item.status == OPEN else ""),
        f"Project: {item.project or 'not filed'}" + (f" › {item.feature}" if item.feature else ""),
        f"Context: {item.context or 'none given'}",
        f"Title: {item.title}",
        f"Summary: {item.summary}",
    ]
    if item.attachments:
        parts.append("Screenshots and links: " + ", ".join(a.url or a.name for a in item.attachments)
                     + " (send_screenshots emails them to Nick)")
    if item.options:
        parts.append("Options: " + "; ".join(f"{n}. {o}" for n, o in enumerate(item.options, 1)))
    if item.recommendation:
        parts.append(f"Recommendation: {item.recommendation}")
    if item.default_option:
        parts.append(f"Default if no answer by {when(item.default_at, now)}: {item.default_option}")
    if item.briefing:
        parts.append(f"Briefing:\n{_clip(item.briefing, 6000, flat=False)}")
    if item.detail:
        parts.append(f"Detail:\n{_clip(item.detail, 4000, flat=False)}")
    spoken = [m for m in item.messages if m.author != SYSTEM][-8:]
    if spoken:
        parts.append("Thread (latest last):\n" + "\n".join(
            f"- {'Nick' if m.author == NICK else item.name_of(m.author)}{' asked' if m.is_question else ''}: "
            f"{_clip(m.body, 400)}" for m in spoken
        ))
    if not item.briefing and not item.detail:
        parts.append("There is no briefing or detail; anything beyond the summary needs ask_owner.")
    return "\n".join(parts)


def opening(agenda: Agenda) -> str:
    projects = plan.plan(agenda)
    items = [i for p in projects for i in p.items]
    if not items:
        return "Hi Nick. Nothing's waiting on you right now. Anything you want to ask a session?"
    blocking = sum(i.blocking for i in items)
    filed = [p for p in projects if p.name != plan.UNFILED]
    unfiled = sum(len(p.items) for p in projects if p.name == plan.UNFILED)
    named = [f"{p.name} ({len(p.items)})" for p in filed]
    listed = ", ".join(named[:-1]) + f" and {named[-1]}" if len(named) > 1 else "".join(named)
    shape = f": {listed}" if named else ""
    extra = f", plus {unfiled} not filed yet" if unfiled and named else ""
    lead = f". {blocking} {'is' if blocking == 1 else 'are'} blocking" if blocking else ""
    start = filed[0].name if filed else "the top"
    count = f"{len(items)} item{'' if len(items) == 1 else 's'}"
    return f"Hi Nick. {count} today{shape}{extra}{lead}. Shall I go project by project, starting with {start}?"


def _latest_reply(item: Item):
    """The owner's latest word when it answers something of Nick's; worth hearing before the item."""
    spoken = [m for m in item.messages if m.author != SYSTEM]
    if spoken and spoken[-1].author == item.owner and (item.from_nick or item.nick_has_answered or item.answers_nicks_question):
        return spoken[-1]
    return None


def _clip(text: str, width: int, *, flat: bool = True) -> str:
    text = " ".join(text.split()) if flat else text.strip()
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


# --- ElevenLabs ------------------------------------------------------------


class VoiceError(Exception):
    pass


def _key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise VoiceError("ELEVENLABS_API_KEY isn't set (put it in the environment, e.g. with `doppler run`)")
    return key


def _call(method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        API + path, method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"xi-api-key": _key(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        with error:
            raise VoiceError(f"ElevenLabs {method} {path}: HTTP {error.code} {error.read()[:600]!r}") from None


def agent_id() -> str | None:
    try:
        return json.loads(CONFIG.read_text()).get("agent_id")
    except (OSError, ValueError):
        return None


def sync() -> str:
    """Create the agent the first time, then keep it matching agent_config()."""
    config = agent_config()
    existing = agent_id()
    if existing:
        _call("PATCH", f"/v1/convai/agents/{existing}", config)
        return existing
    created = _call("POST", "/v1/convai/agents/create", config)["agent_id"]
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps({"agent_id": created}, indent=2) + "\n")
    return created


def conversation_token() -> str:
    """A short-lived token that lets the page start one call with the private agent."""
    existing = agent_id()
    if not existing:
        raise VoiceError("no voice agent yet; run `python3 -m agenda.voice sync`")
    return _call("GET", f"/v1/convai/conversation/token?agent_id={existing}")["token"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agenda-voice", description="Manage the agenda's ElevenLabs voice agent.")
    parser.add_argument("command", choices=("sync", "show", "overview"))
    args = parser.parse_args(argv)
    try:
        if args.command == "sync":
            print(f"agent {sync()} is in sync")
        elif args.command == "show":
            print(json.dumps(_call("GET", f"/v1/convai/agents/{agent_id()}"), indent=2)[:4000])
        else:
            with Agenda() as agenda:
                print(overview(agenda))
    except VoiceError as error:
        print(f"agenda-voice: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
