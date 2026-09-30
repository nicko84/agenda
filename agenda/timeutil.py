"""Parsing "later" and showing times the way a person says them."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta

from .store import AgendaError

_DURATION = re.compile(r"^(\d+)\s*(m|min|mins|h|hr|hrs|d|day|days)$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}
MORNING = time(8, 0)


def parse_until(text: str, now: datetime) -> datetime:
    """'30m', '2h', '1d', 'tomorrow' (8am local) or an ISO time (local if it has no zone)."""
    value = text.strip().lower()
    if match := _DURATION.match(value):
        return now + timedelta(**{_UNITS[match[2][0]]: int(match[1])})
    local_now = now.astimezone()
    if value == "tomorrow":
        return datetime.combine(local_now.date() + timedelta(days=1), MORNING, tzinfo=local_now.tzinfo)
    try:
        when = datetime.fromisoformat(text.strip())
    except ValueError:
        raise AgendaError(f"can't read {text!r} as a time; use 30m, 2h, 1d, tomorrow or an ISO time") from None
    return when if when.tzinfo else when.replace(tzinfo=local_now.tzinfo)


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def ago(value: str, now: datetime) -> str:
    """Compact age: '<1m', '12m', '3h', '2d'."""
    minutes = int((now - parse_iso(value)).total_seconds() // 60)
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 48 * 60:
        return f"{minutes // 60}h"
    return f"{minutes // (24 * 60)}d"


def when(value: str, now: datetime) -> str:
    """Local clock time, with the weekday or date once it isn't today."""
    moment = parse_iso(value).astimezone()
    today = now.astimezone().date()
    if moment.date() == today:
        return moment.strftime("%H:%M")
    if abs((moment.date() - today).days) < 7:
        return moment.strftime("%a %H:%M")
    return moment.strftime("%-d %b %H:%M")
