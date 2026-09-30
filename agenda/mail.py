"""Email the owner the screenshots for items they are about to sign off, so a design can be seen mid-call.

Sent through Resend's API, with screenshots as attachments (inline images get stripped by Gmail). The
recipient is the one address in AGENDA_MAIL_TO: this module can't email anyone else. RESEND_API_KEY comes
from the environment, or from Doppler at send time when AGENDA_RESEND_DOPPLER names "project/config".
"""

from __future__ import annotations

import base64
import html
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from . import settings
from .store import Agenda, AgendaError, Item


def recipient() -> str:
    to = settings.get("AGENDA_MAIL_TO")
    if not to:
        raise AgendaError("AGENDA_MAIL_TO isn't set, so there's nobody to email screenshots to")
    return to


def _resend_key() -> str:
    if key := os.environ.get("RESEND_API_KEY"):
        return key
    where = settings.get("AGENDA_RESEND_DOPPLER")
    doppler = shutil.which("doppler") or str(Path.home() / ".local/bin/doppler")
    if where and "/" in where:
        project, config = where.split("/", 1)
        result = subprocess.run(
            [doppler, "secrets", "get", "RESEND_API_KEY", "--plain", "--project", project, "--config", config],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    raise AgendaError("no RESEND_API_KEY: set it in the environment, or AGENDA_RESEND_DOPPLER to project/config")


def resend(subject: str, body_html: str, files: list[str]) -> None:
    sender = settings.get("AGENDA_MAIL_FROM")
    if not sender:
        raise AgendaError("AGENDA_MAIL_FROM isn't set (a sender on a domain verified in Resend)")
    payload = {
        "from": sender,
        "to": [recipient()],
        "subject": subject,
        "html": body_html,
        "attachments": [
            {"filename": Path(f).name, "content": base64.b64encode(Path(f).read_bytes()).decode()} for f in files
        ],
    }
    request = urllib.request.Request(
        "https://api.resend.com/emails", data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {_resend_key()}", "Content-Type": "application/json", "User-Agent": "agenda/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60):
            pass
    except urllib.error.HTTPError as error:
        with error:
            raise AgendaError(f"sending the email failed: HTTP {error.code} {error.read()[:300]!r}") from None


def dry_run(subject: str, body_html: str, files: list[str]) -> None:
    """For tests against a scratch agenda: write the email to $AGENDA_MAIL_DRYRUN instead of sending it."""
    folder = Path(os.environ["AGENDA_MAIL_DRYRUN"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{int(time.time() * 1000)}.json").write_text(json.dumps({"subject": subject, "html": body_html, "files": files}))


def default_sender() -> Callable:
    return dry_run if os.environ.get("AGENDA_MAIL_DRYRUN") else resend


def screenshots_email(items: list[Item]) -> tuple[str, str, list[str]]:
    """(subject, html, attachment paths) for these items."""
    files: list[str] = []
    blocks = []
    for item in items:
        where = " › ".join(x for x in (item.project, item.feature) if x)
        shots = [a for a in item.attachments if a.path]
        links = [a for a in item.attachments if a.url]
        files += [a.path for a in shots]
        parts = [f"<h3 style='margin:18px 0 4px'>#{item.id} · {html.escape(item.title)}</h3>"]
        if where:
            parts.append(f"<div style='color:#666;font-size:13px'>{html.escape(where)} · from {html.escape(item.name_of(item.owner))}</div>")
        if item.context:
            parts.append(f"<p><b>Context:</b> {html.escape(item.context)}</p>")
        parts.append(f"<p>{html.escape(item.summary)}</p>")
        if item.options:
            parts.append("<ol>" + "".join(f"<li>{html.escape(o)}</li>" for o in item.options) + "</ol>")
        if item.recommendation:
            parts.append(f"<p><b>Recommends:</b> {html.escape(item.recommendation)}</p>")
        if shots:
            parts.append("<p>Attached: " + ", ".join(html.escape(a.name) for a in shots) + "</p>")
        if links:
            parts.append("<p>" + "<br>".join(f"<a href='{html.escape(a.url)}'>{html.escape(a.url)}</a>" for a in links) + "</p>")
        blocks.append("\n".join(parts))
    ids = ", ".join(f"#{i.id}" for i in items)
    subject = f"Agenda screenshots: {ids}" + (f" ({items[0].project})" if len({i.project for i in items}) == 1 and items[0].project else "")
    page = settings.get("AGENDA_PAGE_URL")
    where = f" or at {html.escape(page)}" if page else ""
    body = f"<p>Screenshots for the design sign-offs on your agenda call. Answer on the call{where}.</p>\n" + "\n".join(blocks)
    return subject, body, files


def send_screenshots(agenda: Agenda, item_ids: list[int], send: Callable | None = None) -> str:
    send = send or default_sender()
    items = [agenda.get(i) for i in item_ids]
    with_visuals = [i for i in items if i.attachments]
    without = [i.id for i in items if not i.attachments]
    if not with_visuals:
        raise AgendaError(
            f"no screenshots or links on {', '.join(f'#{i}' for i in item_ids)}; ask the owner to attach them"
        )
    subject, body, files = screenshots_email(with_visuals)
    send(subject, body, files)
    for item in with_visuals:
        agenda.record_note(item.id, f"Screenshots emailed ({len([a for a in item.attachments if a.path])} files).")
    told = f"Emailed {len(files)} screenshot(s) and links for {', '.join(f'#{i.id}' for i in with_visuals)}."
    if without:
        told += f" Nothing attached on {', '.join(f'#{i}' for i in without)}."
    return told
