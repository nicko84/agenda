"""Nudge idle sessions when the agenda has news for them.

A session mid-turn hears its news from the Stop hook when the turn ends. A session sitting idle at its
prompt never ends a turn, so this loop types a short nudge into its herdr pane, but only when all of these
hold:
- herdr says the pane still runs that same session (panes get reused) and it is idle, not working or
  waiting on a dialog;
- the input box holds no text of Nick's. `herdr agent prompt` replaces whatever is there, and it once
  destroyed a draft of his. Claude Code's suggestions are drawn dim and may be replaced; plain text is a
  draft; if the box can't be found, don't risk it;
- there is news newer than the last nudge, so each piece of news is announced once.

Claude Code panes only for now; Codex lanes hear their news when they next run `agenda`.

    python3 -m agenda.deliver [--interval 15] [--once] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time

from . import render
from .store import Agenda, Session

SEPARATOR = re.compile(r"^\s*─{8,}")
SGR = re.compile(r"\x1b\[([0-9;]*)m")
IDLE = ("idle", "done")


def herdr(*args: str, timeout: float = 5) -> dict | str | None:
    try:
        out = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout).get("result")
    except ValueError:
        return out.stdout


def typed_text(screen: str) -> str | None:
    """What Nick has typed in a Claude Code input box, ignoring dim suggestion text.

    Returns None when the box can't be found (then nobody should type into the pane).
    """
    lines = screen.splitlines()
    seps = [i for i, line in enumerate(lines) if SEPARATOR.match(SGR.sub("", line))]
    if len(seps) < 2:
        return None
    typed, dim = [], False
    for line in lines[seps[-2] + 1 : seps[-1]]:
        pos = 0
        for match in SGR.finditer(line):
            if not dim:
                typed.append(line[pos : match.start()])
            dim = _dim_after(match[1], dim)
            pos = match.end()
        if not dim:
            typed.append(line[pos:])
    return "".join(typed).replace("❯", "").replace(" ", " ").strip()


def _dim_after(params: str, dim: bool) -> bool:
    """Apply one SGR sequence to the dim flag. 38/48 colour parameters are skipped, so the 2 in
    `38;2;r;g;b` (truecolor) isn't mistaken for dim."""
    codes = [c for c in params.split(";")] if params else ["0"]
    i = 0
    while i < len(codes):
        code = codes[i] or "0"
        if code in ("38", "48", "58"):
            i += 5 if i + 1 < len(codes) and codes[i + 1] == "2" else 3
            continue
        if code == "2":
            dim = True
        elif code in ("0", "22"):
            dim = False
        i += 1
    return dim


def can_nudge(session: Session) -> tuple[bool, str]:
    if not session.pane_id or session.agent != "claude":
        return False, "no Claude pane on record"
    pane = herdr("pane", "get", session.pane_id)
    pane = pane.get("pane") if isinstance(pane, dict) else None
    if not pane:
        return False, "pane gone"
    if (pane.get("agent_session") or {}).get("value") != session.id:
        return False, "pane now runs another session"
    if pane.get("agent_status") not in IDLE:
        return False, f"pane is {pane.get('agent_status')}"
    screen = herdr("pane", "read", session.pane_id, "--source", "visible", "--format", "ansi")
    if not isinstance(screen, str):
        return False, "couldn't read the pane"
    typed = typed_text(screen)
    if typed is None:
        return False, "couldn't find the input box"
    if typed:
        return False, "Nick has text in the input box"
    return True, "idle"


def nudge_text(agenda: Agenda, session: Session) -> str | None:
    items, _ = agenda.unread(session.id, since_nudge=True)
    if not items:
        return None
    return (
        f"Agenda news for you on {len(items)} item(s):\n{render.news_brief(items, session.id)}\n"
        "Run `agenda inbox` and act on it as the agenda skill says."
    )


def deliver_once(agenda: Agenda, *, dry_run: bool = False, log=print, skipped: dict | None = None) -> int:
    """One pass. `skipped` carries each session's last skip reason between passes, so a busy session is
    logged once rather than every 15 seconds."""
    skipped = {} if skipped is None else skipped
    sent = 0
    for session, upto in agenda.unnudged():
        ok, why = can_nudge(session)
        if not ok:
            if skipped.get(session.id) != why:
                log(f"waiting on {session.name}: {why}")
            skipped[session.id] = why
            continue
        skipped.pop(session.id, None)
        text = nudge_text(agenda, session)
        if text is None:
            continue
        if dry_run:
            log(f"would nudge {session.name} ({session.pane_id}):\n{text}")
            continue
        if herdr("agent", "prompt", session.pane_id, text, timeout=15) is None:
            log(f"nudge to {session.name} failed; will retry")
            continue
        agenda.record_nudge(session.id, upto)
        log(f"nudged {session.name} ({session.pane_id}) up to message {upto}")
        sent += 1
    return sent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agenda-deliver", description="Nudge idle sessions that have agenda news.")
    parser.add_argument("--interval", type=float, default=15, help="seconds between passes")
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    parser.add_argument("--dry-run", action="store_true", help="say what would be sent; send nothing")
    parser.add_argument("--db", help="database (default $AGENDA_DB or ~/.local/share/agenda/agenda.db)")
    args = parser.parse_args(argv)

    def log(message: str) -> None:
        print(time.strftime("%H:%M:%S"), message, flush=True)

    skipped: dict = {}
    while True:
        try:
            with Agenda(args.db) as agenda:
                deliver_once(agenda, dry_run=args.dry_run, log=log, skipped=skipped)
        except Exception as error:  # noqa: BLE001 — one bad pass must not stop delivery
            log(f"pass failed: {error!r}")
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
