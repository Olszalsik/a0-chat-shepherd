"""chat_shepherd — time policy. Pure, framework-free, and deliberately small.

Everything here is a function of its arguments plus an optional `now`, so the
whole module is exercised offline. Two rules drive the design:

* Absolute time only. "Five minutes after the last action" is a comparison
  against a stored ISO stamp, never an accumulated counter, so a missed tick
  or a paused loop cannot drift the schedule.
* A misconfigured window must fail *open or shut, never sideways*. An
  unparseable duration raises at the boundary so the caller can journal it
  and skip; an unparseable time-of-day falls back to the shipped default
  rather than silently meaning "always".

`start == end` means a full 24-hour window, not "never". Treating an
equal start/end as disabled is a classic off-by-semantics bug: an operator who
wants "always" types 00:00-00:00 and gets a supervisor that never acts.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

_DURATION_RE = re.compile(r'(\d+)\s*([smhd])', re.IGNORECASE)
_UNIT_SECONDS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}

DEFAULT_START = '00:00'
DEFAULT_END = '24:00'


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat()


def parse_iso(value: Any) -> datetime | None:
    """ISO string -> aware UTC datetime, or None. Never raises.

    Accepts an already-parsed `datetime` too: the framework hands out
    `context.last_message` as a datetime, and feeding one to `fromisoformat`
    raises — which is how a whole tick can start reporting a 9999-minute
    stall for every chat.
    """
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def as_int(value: Any, default: int = 0) -> int:
    """Tolerant, never-clamping read of evidence (donor config_defaults.as_int)."""
    try:
        if isinstance(value, bool):
            return int(value)
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        value = int(value)
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if out != out or out in (float('inf'), float('-inf')):
        return default
    return out


def minutes_since(value: Any, now: datetime | None = None) -> float:
    dt = parse_iso(value)
    if dt is None:
        return 9999.0
    return ((now or utcnow()) - dt).total_seconds() / 60.0


def parse_duration(value: str | None) -> timedelta | None:
    """'90s' / '5m' / '2h' / '1d' -> timedelta.

    Raises ValueError on anything malformed: a silently-ignored bad schedule
    looks exactly like a supervisor that stopped acting, and that is the
    failure mode this plugin exists to prevent. Returns None for a parsed
    zero duration ("0 disables"), so callers can tell "off" from "broken".
    """
    text = (value or '').strip().lower()
    if not text:
        raise ValueError('duration is required')
    matches = list(_DURATION_RE.finditer(text))
    if not matches:
        raise ValueError(f'invalid duration: {value!r}')
    if ''.join(m.group(0).replace(' ', '') for m in matches) != re.sub(r'\s+', '', text):
        raise ValueError(f'invalid duration: {value!r}')
    total = sum(int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()] for m in matches)
    if total <= 0:
        return None
    return timedelta(seconds=total)


def compute_next_due(last_run_at: Any, every: str | None) -> str:
    """When the next fire is due. Empty string means "off"."""
    try:
        interval = parse_duration(every)
    except ValueError:
        return ''
    if interval is None:
        return ''
    last = parse_iso(last_run_at)
    return (last + interval if last else utcnow()).isoformat()


def is_due(last_run_at: Any, every: str | None, now: datetime | None = None) -> bool:
    """True when `every` has elapsed since `last_run_at`.

    A malformed `every` is False (do not fire) rather than True: a typo in
    the interval must not turn into a hot loop.
    """
    try:
        interval = parse_duration(every)
    except ValueError:
        return False
    if interval is None:
        return False
    last = parse_iso(last_run_at)
    if last is None:
        return True
    return (now or utcnow()) >= (last + interval)


def day_key(value: Any = None) -> str:
    """UTC calendar day, used to bucket per-day action ceilings."""
    return (parse_iso(value) or utcnow()).strftime('%Y-%m-%d')


def _parse_hhmm(value: Any, fallback: str) -> int:
    text = str(value if value is not None else '').strip() or fallback
    if text == '24:00':
        return 24 * 60
    parts = text.split(':')
    if len(parts) != 2:
        return _parse_hhmm(fallback, DEFAULT_START)
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return _parse_hhmm(fallback, DEFAULT_START)
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return _parse_hhmm(fallback, DEFAULT_START)
    return hour * 60 + minute


def resolve_timezone(cfg: dict[str, Any]) -> str:
    name = str((cfg or {}).get('active_hours_timezone') or '').strip()
    if name:
        return name
    try:
        from helpers.localization import Localization

        return Localization.get().get_timezone() or 'UTC'
    except Exception:
        return 'UTC'


def in_active_hours(cfg: dict[str, Any], now: datetime | None = None) -> bool:
    """True when the current local time falls inside the configured window.

    Handles the window crossing midnight (22:00-06:00) and treats
    start == end as a full 24-hour window. An unknown timezone falls back to
    UTC rather than raising.
    """
    cfg = cfg or {}
    if not bool(cfg.get('active_hours_enabled')):
        return True
    start = _parse_hhmm(cfg.get('active_hours_start'), DEFAULT_START)
    end = _parse_hhmm(cfg.get('active_hours_end'), DEFAULT_END)
    if start == end:
        return True
    current = now or utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        local = current.astimezone(ZoneInfo(resolve_timezone(cfg)))
    except Exception:
        local = current
    minutes = local.hour * 60 + local.minute
    if start < end:
        return start <= minutes < end
    # wraps past midnight
    return minutes >= start or minutes < end
