"""Who is running this command: Nick, or an agent session.

Nick's verbs (answer, accept, ack, ...) must really come from Nick, so an agent can
never approve its own recommendation. Anything running inside a Claude Code session,
or in a herdr pane where herdr sees an agent (which also catches Codex), is a session.
Everything else, like a plain shell or the voice bridge, is Nick.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

NICK = "nick"


@dataclass(frozen=True)
class Actor:
    id: str  # "nick", or the agent's own session id
    agent: str | None = None  # claude, codex, ...
    label: str | None = None  # herdr tab label, e.g. "merge-queue"
    pane_id: str | None = None  # where to deliver news right now
    tab_id: str | None = None

    @property
    def is_nick(self) -> bool:
        return self.id == NICK


NICK_ACTOR = Actor(NICK)


def detect(env: Mapping[str, str] = os.environ) -> Actor:
    pane_id, tab_id = env.get("HERDR_PANE_ID"), env.get("HERDR_TAB_ID")
    label = env.get("AGENDA_LABEL")
    if explicit := env.get("AGENDA_SESSION"):
        if explicit == NICK:
            return NICK_ACTOR
        return Actor(explicit, env.get("AGENDA_AGENT", "agent"), label, pane_id, tab_id)

    pane = (_herdr("pane", "get", pane_id).get("pane") or {}) if pane_id else {}
    tab_id = pane.get("tab_id") or tab_id
    if session := env.get("CLAUDE_CODE_SESSION_ID"):
        agent = "claude"
    elif pane.get("agent"):
        agent = pane["agent"]
        session = (pane.get("agent_session") or {}).get("value") or f"herdr:{pane_id}"
    else:
        return NICK_ACTOR
    if not label and tab_id:
        label = (_herdr("tab", "get", tab_id).get("tab") or {}).get("label")
    return Actor(session, agent, label, pane_id, tab_id)


def _herdr(*args: str) -> dict:
    """One herdr socket query; empty on any failure, so identity never blocks a command."""
    try:
        out = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=2, check=False)
        return json.loads(out.stdout).get("result") or {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}
