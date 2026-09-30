"""Per-machine settings: who the owner is, where mail goes, which secrets manager holds the keys.

They live outside the repo, in `~/.config/agenda/env` (or the file `$AGENDA_ENV` names), as KEY=VALUE
lines, and anything already in the environment wins. No secret belongs in that file: API keys come from the
environment or from Doppler at the moment they're needed.

    AGENDA_REQUIRE_USER="you@example.com"        # tailnet login the page accepts (tailscale serve)
    AGENDA_PAGE_URL="https://host.tailnet.ts.net:10480"
    AGENDA_MAIL_TO="you@example.com"             # the only address screenshots are emailed to
    AGENDA_MAIL_FROM="Agenda <agenda@example.com>"  # a sender on a domain verified in Resend
    AGENDA_RESEND_DOPPLER="project/config"       # where RESEND_API_KEY lives, if it isn't in the environment
    AGENDA_DOPPLER="project/config"              # run the page under this config for ELEVENLABS_API_KEY
    AGENDA_VOICE_ID="..."                        # ElevenLabs voice for the call
"""

from __future__ import annotations

import os
from pathlib import Path


def env_file() -> Path:
    if explicit := os.environ.get("AGENDA_ENV"):
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "agenda" / "env"


def load() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = env_file().read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        raw = raw.strip()
        if raw[:1] in ("'", '"'):
            end = raw.find(raw[0], 1)
            value = raw[1:end] if end > 0 else raw[1:]
        else:
            value = raw.split(" #", 1)[0].strip()
        values[key.strip()] = value
    return values


def get(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key) or load().get(key) or default
