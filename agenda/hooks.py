"""Claude Code hooks that hand a session its agenda news at natural moments.

- Stop: when a turn ends and there is news the session hasn't been told about, keep it going with a
  short summary (once per piece of news; `stop_hook_active` prevents a loop).
- SessionStart (startup, resume, compact, clear): a session that owns or watches open items is told
  what they are, so a compaction or restart never loses them, and its pane is kept current.

These run in every Claude Code session on this machine, so they read only, stay silent for sessions
that have never used the agenda, and never fail loudly: any error exits 0 with no output.
"""

from __future__ import annotations

import json
import os
import sys

from . import render
from .identity import Actor
from .store import OPEN, Agenda


def stop(payload: dict, agenda: Agenda) -> dict | None:
    session = payload.get("session_id")
    if not session or payload.get("stop_hook_active") or not agenda.knows(session):
        return None
    agenda.relocate(_actor(session))
    items, upto = agenda.unread(session, since_nudge=True)
    if not items:
        return None
    agenda.record_nudge(session, upto)
    return {
        "decision": "block",
        "reason": (
            f"Agenda news for you on {len(items)} item(s):\n{render.news_brief(items, session)}\n\n"
            "Run `agenda inbox` (that reads it in full and marks it read) and act on it as the agenda skill says: "
            "reply to Nick's questions, carry out answers, close what is settled. If none of it needs doing now, "
            "say so briefly and stop."
        ),
    }


def session_start(payload: dict, agenda: Agenda) -> dict | None:
    session = payload.get("session_id")
    if not session or not agenda.knows(session):
        return None
    actor = _actor(session)
    agenda.relocate(actor)
    items = [i for i in agenda.mine(actor) if i.status == OPEN]
    unread, _ = agenda.unread(session)
    if not items and not unread:
        return None
    listed = "\n".join(
        f"#{i.id} {i.kind} \"{i.title}\" — waiting on {'Nick' if i.waiting_on == 'nick' else i.name_of(i.owner)}"
        + ("" if i.owner == session else " (you watch it)")
        for i in items[:12]
    )
    more = f"\n…and {len(items) - 12} more (`agenda mine`)" if len(items) > 12 else ""
    news = f"\n\nNews you haven't read on {len(unread)} item(s); run `agenda inbox`." if unread else ""
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                "The agenda (Nick's durable queue; see the agenda skill) holds these open items of yours. "
                f"Trust it over any summary:\n{listed}{more}{news}"
            ),
        }
    }


def _actor(session: str) -> Actor:
    return Actor(session, "claude", None, os.environ.get("HERDR_PANE_ID"), os.environ.get("HERDR_TAB_ID"))


HOOKS = {"stop": stop, "session-start": session_start}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        hook = HOOKS[argv[0]]
        payload = json.loads(sys.stdin.read() or "{}")
        with Agenda() as agenda:
            result = hook(payload, agenda)
        if result:
            print(json.dumps(result))
    except Exception:  # noqa: BLE001, S110 — a hook must never break the session it runs in
        pass
    return 0
