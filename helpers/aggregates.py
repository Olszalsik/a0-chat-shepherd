'''Dashboard aggregates from the journal history (read-side only).

v1.15.0: pure-compute module - no config keys, no state writes, no I/O.
Callers pass an event list (the journal mirror in state['history'],
newest-first or any order); compute() sorts chronologically and never
raises: malformed entries are skipped.

Metrics over a fixed WINDOW_DAYS window:
- stalls: auto_nudge events clustered per chat into episodes (ladder
  retries within EPISODE_GAP_MINUTES belong to the same stall).
- resume: time-to-resume from the recorded minutes_after_nudge deltas
  (nudge_effective = stall resume, wedge_nudge_effective = recovery).
- wedge: remediation success rate - each wedge_nudge_effective credits
  the latest unconsumed attempt (wedge_auto_continue /
  wedge_soft_restart) of the same chat within WEDGE_RESOLVE_MINUTES.
'''

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import mean, median
from typing import Any

WINDOW_DAYS = 7

# Ladder retries inside this gap continue the same stall episode.
EPISODE_GAP_MINUTES = 90
# A wedge recovery credits the attempt it follows within this window.
WEDGE_RESOLVE_MINUTES = 120

_ATTEMPT_ACTIONS = ('wedge_auto_continue', 'wedge_soft_restart')
_STALL_ACTION = 'auto_nudge'
_STALL_EFFECTIVE = 'nudge_effective'
_WEDGE_EFFECTIVE = 'wedge_nudge_effective'


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        try:
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _num(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float('inf'), float('-inf')) or f < 0:
        return None
    return round(f, 1)


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {'mean_minutes': None, 'median_minutes': None, 'samples': 0}
    return {
        'mean_minutes': round(mean(values), 1),
        'median_minutes': round(median(values), 1),
        'samples': len(values),
    }


def _empty(window_days: int) -> dict[str, Any]:
    return {
        'window_days': window_days,
        'events_in_window': 0,
        'generated_at': '',
        'stalls': {'episodes': 0, 'per_day': 0.0, 'nudges_sent': 0, 'recovered': 0},
        'resume': {
            'stall': _stats([]),
            'wedge': _stats([]),
        },
        'wedge': {
            'attempts': 0,
            'resolved': 0,
            'success_rate_pct': None,
            'continues': 0,
            'soft_restarts': 0,
        },
    }


def compute(
    events: Any, now: datetime | None = None, window_days: int = WINDOW_DAYS
) -> dict[str, Any]:
    '''Aggregate journal events into dashboard metrics (never raises).'''
    try:
        if not isinstance(window_days, int) or window_days < 1:
            window_days = WINDOW_DAYS
        if now is None:
            now = datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        window_start = now - timedelta(days=window_days)

        parsed: list[tuple[datetime, str, str, dict]] = []
        for ev in events or []:
            if not isinstance(ev, dict):
                continue
            dt = _parse_ts(ev.get('timestamp'))
            action = ev.get('action')
            if dt is None or not isinstance(action, str):
                continue
            if dt < window_start or dt > now:
                continue
            chat_id = ev.get('chat_id')
            parsed.append((dt, action, chat_id if isinstance(chat_id, str) else '', ev))
        parsed.sort(key=lambda item: item[0])

        if not parsed:
            base = _empty(window_days)
            base['generated_at'] = now.isoformat()
            return base

        episodes = 0
        nudges_sent = 0
        recovered = 0
        last_nudge: dict[str, datetime] = {}
        stall_deltas: list[float] = []
        wedge_deltas: list[float] = []
        attempts: dict[str, list[datetime]] = {}
        resolutions: dict[str, list[datetime]] = {}
        continues = 0
        soft_restarts = 0
        first_ts = parsed[0][0]
        last_ts = parsed[-1][0]

        for dt, action, chat_id, ev in parsed:
            if action == _STALL_ACTION:
                nudges_sent += 1
                prev = last_nudge.get(chat_id)
                if prev is None or (dt - prev).total_seconds() > EPISODE_GAP_MINUTES * 60:
                    episodes += 1
                last_nudge[chat_id] = dt
            elif action == _STALL_EFFECTIVE:
                recovered += 1
                m = _num(ev.get('minutes_after_nudge'))
                if m is not None:
                    stall_deltas.append(m)
            elif action == _WEDGE_EFFECTIVE:
                m = _num(ev.get('minutes_after_nudge'))
                if m is not None:
                    wedge_deltas.append(m)
                resolutions.setdefault(chat_id, []).append(dt)
            elif action in _ATTEMPT_ACTIONS:
                if action == 'wedge_auto_continue':
                    continues += 1
                else:
                    soft_restarts += 1
                attempts.setdefault(chat_id, []).append(dt)

        # Wedge pairing: each recovery credits the latest unconsumed
        # attempt of the same chat within the resolve window; attempts
        # with no following recovery stay unresolved.
        resolved = 0
        total_attempts = 0
        for chat_id, ats in attempts.items():
            ats.sort()
            total_attempts += len(ats)
            used: set[int] = set()
            for rdt in sorted(resolutions.get(chat_id, [])):
                best = -1
                for i, at in enumerate(ats):
                    if at <= rdt and i not in used:
                        best = i
                if best >= 0 and (rdt - ats[best]).total_seconds() <= WEDGE_RESOLVE_MINUTES * 60:
                    used.add(best)
            resolved += len(used)

        raw_span = (last_ts - first_ts).total_seconds() / 86400.0
        span_days = min(float(window_days), max(1.0, raw_span))
        per_day = round(episodes / span_days, 1) if episodes else 0.0

        return {
            'window_days': window_days,
            'events_in_window': len(parsed),
            'generated_at': now.isoformat(),
            'stalls': {
                'episodes': episodes,
                'per_day': per_day,
                'nudges_sent': nudges_sent,
                'recovered': recovered,
            },
            'resume': {
                'stall': _stats(stall_deltas),
                'wedge': _stats(wedge_deltas),
            },
            'wedge': {
                'attempts': total_attempts,
                'resolved': resolved,
                'success_rate_pct': (
                    round(100.0 * resolved / total_attempts) if total_attempts else None
                ),
                'continues': continues,
                'soft_restarts': soft_restarts,
            },
        }
    except Exception:
        # Defensive: /status must never fail because of a malformed entry.
        return _empty(WINDOW_DAYS)
