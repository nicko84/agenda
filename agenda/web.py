"""The agenda in a browser: Nick's queue as cards, with his verbs as buttons.

Loopback only. Expose it with `tailscale serve`, which stamps the caller's tailnet login on each proxied
request; every request must carry the login in AGENDA_REQUIRE_USER (or --require-user). Anything on this
machine could still forge the header, as it could write the database directly, so this guards against a
session acting as the owner by accident, not against one determined to.

    python3 -m agenda.web --port 6110          # AGENDA_REQUIRE_USER from ~/.config/agenda/env
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import mail, plan, settings, voice
from .identity import NICK_ACTOR
from .store import ORCHESTRATOR, Agenda, AgendaError, Item, iso
from .timeutil import parse_until

PAGE = Path(__file__).with_name("static") / "index.html"
VERBS = ("answer", "question", "accept", "ack", "done", "defer", "drop")


def state(agenda: Agenda) -> dict:
    """Everything the page shows, in one read."""
    queue = agenda.queue(include_deferred=True)
    now = agenda.now()
    waiting = [i for i in queue if not _deferred(i, now)]
    return {
        "now": now.isoformat(),
        "queue": _dicts(waiting),
        "plan": [
            {
                "name": project.name,
                "brief": project.brief,
                "features": [
                    {"name": f.name, "buckets": [{"name": b, "title": plan.BUCKET_TITLES[b], "ids": [i.id for i in items]}
                                                 for b, items in f.buckets()]}
                    for f in project.features
                ],
            }
            for project in plan.plan(agenda, waiting)
        ],
        "deferred": _dicts([i for i in queue if _deferred(i, now)]),
        "owed": _dicts(agenda.owed()),
        "history": _dicts(agenda.history(30)),
        "orchestrators": [s.name for s in agenda.sessions() if s.role == ORCHESTRATOR],
    }


def act(agenda: Agenda, item_id: int, verb: str, body: dict) -> Item:
    text = (body.get("text") or "").strip()
    match verb:
        case "answer":
            return agenda.answer(NICK_ACTOR, item_id, text)
        case "question":
            return agenda.question(NICK_ACTOR, item_id, text)
        case "accept":
            return agenda.accept(NICK_ACTOR, item_id)
        case "ack":
            return agenda.ack(NICK_ACTOR, item_id)
        case "done":
            return agenda.done(NICK_ACTOR, item_id, text)
        case "defer":
            return agenda.defer(NICK_ACTOR, item_id, parse_until(body.get("until") or "1h", agenda.now()))
        case "drop":
            return agenda.drop(NICK_ACTOR, item_id, text)
    raise AgendaError(f"unknown action {verb!r}")


def _deferred(item: Item, now) -> bool:
    return bool(item.deferred_until and item.deferred_until > iso(now))


def _dicts(items: list[Item]) -> list[dict]:
    return [item.to_dict() for item in items]


class Handler(BaseHTTPRequestHandler):
    server: Server
    server_version = "agenda"

    def log_message(self, fmt, *args):
        if self.server.verbose:
            super().log_message(fmt, *args)

    def do_GET(self) -> None:
        if not self._authorised():
            return
        if self.path in ("/", "/index.html"):
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            return self._with_agenda(state)
        if match := re.fullmatch(r"/api/items/(\d+)", self.path):
            return self._with_agenda(lambda a: a.get(int(match[1])).to_dict())
        if match := re.fullmatch(r"/api/files/(\d+)", self.path):
            return self._file(int(match[1]))
        if self.path == "/api/voice/start":
            return self._voice_start()
        if self.path == "/api/voice/overview":
            return self._with_agenda(lambda a: {"text": voice.overview(a)})
        if match := re.fullmatch(r"/api/voice/items/(\d+)", self.path):
            return self._with_agenda(lambda a: {"text": voice.item_text(a.get(int(match[1])), a.now())})
        self._json({"error": "not found"}, 404)

    def _voice_start(self) -> None:
        """Everything the page needs to start a call: a one-call token and what the agent reads first."""
        try:
            token = voice.conversation_token()
        except voice.VoiceError as error:
            return self._json({"error": str(error)}, 503)

        def start(agenda: Agenda) -> dict:
            now = agenda.now().astimezone()
            return {
                "token": token,
                "watermark": agenda.latest_message_id(),
                "dynamic_variables": {
                    "now": now.strftime("%A %-d %B, %-I:%M %p"),
                    "agenda_overview": voice.overview(agenda),
                    "opening": voice.opening(agenda),
                },
            }

        self._with_agenda(start)

    def do_POST(self) -> None:
        if not self._authorised():
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            return self._json({"error": "body must be JSON"}, 400)
        if match := re.fullmatch(r"/api/items/(\d+)/(\w+)", self.path):
            item_id, verb = int(match[1]), match[2]
            if verb not in VERBS:
                return self._json({"error": f"unknown action {verb!r}"}, 404)
            return self._with_agenda(lambda a: {"item": act(a, item_id, verb, body).to_dict(), **state(a)})
        if self.path == "/api/screenshots":
            ids = [int(i) for i in body.get("item_ids") or []]
            return self._with_agenda(lambda a: {"message": mail.send_screenshots(a, ids)})
        if self.path == "/api/ask":
            kind = "instruction" if body.get("instruction") else "ask"
            return self._with_agenda(
                lambda a: {
                    "item": a.ask(NICK_ACTOR, body.get("text") or "", kind=kind, to=body.get("to") or None).to_dict(),
                    **state(a),
                }
            )
        self._json({"error": "not found"}, 404)

    def _file(self, attachment_id: int) -> None:
        """An attachment the agenda keeps, for thumbnails on the page. Only files in the agenda's own folder."""
        try:
            with Agenda(self.server.db) as agenda:
                attachment = agenda.attachment(attachment_id)
                root = agenda.files_dir.resolve()
        except AgendaError as error:
            return self._json({"error": str(error)}, 404)
        path = Path(attachment.path or "").resolve()
        if not attachment.path or not path.is_relative_to(root) or not path.is_file():
            return self._json({"error": "not a stored file"}, 404)
        self._send(200, path.read_bytes(), attachment.mime or "application/octet-stream")

    def _authorised(self) -> bool:
        required = self.server.require_user
        if required and self.headers.get("Tailscale-User-Login", "") != required:
            self._json({"error": "this page is only for Nick, through tailscale serve"}, 403)
            return False
        return True

    def _with_agenda(self, fn) -> None:
        try:
            with Agenda(self.server.db) as agenda:
                payload = fn(agenda)
        except AgendaError as error:
            return self._json({"error": str(error)}, 400)
        self._json(payload)

    def _json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, *, db: str | None, require_user: str | None, verbose: bool = False):
        super().__init__(addr, Handler)
        self.db, self.require_user, self.verbose = db, require_user, verbose


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agenda-web", description="Serve the agenda to Nick's browser (loopback).")
    parser.add_argument("--port", type=int, default=6110)
    parser.add_argument("--db", help="database (default $AGENDA_DB or ~/.local/share/agenda/agenda.db)")
    parser.add_argument("--require-user", help="tailnet login every request must carry (default AGENDA_REQUIRE_USER)")
    parser.add_argument("--allow-anyone", action="store_true", help="no login check; only for a scratch copy")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    required = args.require_user or settings.get("AGENDA_REQUIRE_USER")
    if not required and not args.allow_anyone:
        parser.error("set AGENDA_REQUIRE_USER (the tailnet login allowed in) or pass --allow-anyone for a scratch copy")
    server = Server(("127.0.0.1", args.port), db=args.db, require_user=required, verbose=args.verbose)
    print(f"agenda-web on http://127.0.0.1:{args.port} (require user: {required or 'nobody'})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
