"""UTC storage, local display. Timestamps are stored as ISO-8601 UTC strings."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ISO = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(ISO)


def from_iso(s: str) -> datetime:
    return datetime.strptime(s, ISO).replace(tzinfo=timezone.utc)


def tzinfo_for(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return timezone.utc


def fmt_local(iso: str | None, tz, now: datetime | None = None) -> str:
    """'14:05' if today in the local zone, otherwise 'Oct 27 14:05'."""
    if not iso:
        return ""
    now = now or utcnow()
    local = from_iso(iso).astimezone(tz)
    if local.date() == now.astimezone(tz).date():
        return local.strftime("%H:%M")
    return local.strftime("%b %d %H:%M")


def fmt_clock(iso: str | None, tz) -> str:
    """Just the local time of day, for slots that must stay short: '22:04'."""
    if not iso:
        return ""
    return from_iso(iso).astimezone(tz).strftime("%H:%M")


def fmt_until(iso: str | None, now: datetime | None = None) -> str:
    """Minutes until an ETA: 'in 11m', 'now', or 'late 5m' once passed."""
    if not iso:
        return ""
    now = now or utcnow()
    secs = int((from_iso(iso) - now).total_seconds())
    mins = abs(secs) // 60
    if mins == 0:
        return "now"
    body = f"{mins // 60}h {mins % 60:02d}m" if mins >= 60 else f"{mins}m"
    return f"in {body}" if secs > 0 else f"late {body}"


def fmt_local_full(iso: str | None, tz) -> str:
    if not iso:
        return ""
    return from_iso(iso).astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def fmt_remaining(expires_iso: str, now: datetime | None = None) -> str:
    now = now or utcnow()
    delta = from_iso(expires_iso) - now
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "expired"
    hours, rem = divmod(secs, 3600)
    mins = rem // 60
    if hours >= 48:
        return f"{hours // 24}d {hours % 24}h"
    return f"{hours}h {mins:02d}m"


def plus_hours(iso: str, hours: int) -> str:
    return to_iso(from_iso(iso) + timedelta(hours=hours))
