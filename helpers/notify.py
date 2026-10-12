"""chat_shepherd — alert channel health.

The health half of the donor chat_shepherd2 notify module, carried over with
its contracts; the delivery itself stays in monitor.py (v1 already has the
SSRF guard, redirect refusal and token redaction in `_external_jobs` /
`_post_json` — this module folds the per-channel RESULTS into durable state).

Why channel health exists: a dead webhook/telegram pair that keeps being
pumped silently eats every alert for days. The state copy is what makes a
dead channel visible on a dashboard nobody is watching at 3am, and what lets
the next alert be routed through a channel that is actually up.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

CHANNEL_WEBHOOK = 'webhook'
CHANNEL_TELEGRAM = 'telegram'
ERROR_MAX = 200


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def record_channels(
    state: dict[str, Any],
    results: dict[str, str | None],
    *,
    threshold: Any = 3,
) -> list[str]:
    """Fold a delivery round into state; return the newly degraded channels.

    results: channel -> None when delivered, or a (redacted) error string.
    """
    limit = max(1, as_int(threshold, 3))
    channels = state.get('channels')
    if not isinstance(channels, dict):
        channels = {}
        state['channels'] = channels
    degraded: list[str] = []
    for channel, error in (results or {}).items():
        record = channels.get(channel)
        if not isinstance(record, dict):
            record = {'consecutive_failures': 0, 'last_ok_at': '', 'last_attempt_at': ''}
            channels[channel] = record
        record['last_attempt_at'] = _iso()
        if error is None:
            record['consecutive_failures'] = 0
            record['last_ok_at'] = record['last_attempt_at']
            record['last_error'] = ''
            continue
        record['consecutive_failures'] = as_int(record.get('consecutive_failures'), 0) + 1
        record['last_error'] = str(error)[:ERROR_MAX]
        if record['consecutive_failures'] >= limit:
            degraded.append(channel)
    return sorted(set(degraded))


def degraded_channels(state: dict[str, Any], threshold: Any = 3) -> list[str]:
    """Channels currently at or past the failure threshold."""
    limit = max(1, as_int(threshold, 3))
    channels = state.get('channels')
    if not isinstance(channels, dict):
        return []
    return sorted(
        name
        for name, record in channels.items()
        if isinstance(record, dict)
        and as_int(record.get('consecutive_failures'), 0) >= limit
    )


def configured_channels(cfg: dict[str, Any], skip: Any = ()) -> list[str]:
    """Channel names that look configured, WITHOUT touching the network.

    Deliberately cheap: it does the same string checks `_external_jobs` uses
    to decide what to build, but stops before the SSRF guard. That guard
    resolves DNS, and resolving it here would put a DNS timeout on whatever
    thread asked which channels exist.
    """
    if not isinstance(cfg, dict):
        return []
    skip = set(skip or ())
    names: list[str] = []
    if CHANNEL_WEBHOOK not in skip and str(cfg.get('webhook_url') or '').strip():
        names.append(CHANNEL_WEBHOOK)
    if CHANNEL_TELEGRAM not in skip and (
        str(cfg.get('telegram_bot_token') or '').strip()
        and str(cfg.get('telegram_chat_id') or '').strip()
    ):
        names.append(CHANNEL_TELEGRAM)
    return names


def channel_view(state: dict[str, Any], threshold: Any = 3) -> dict[str, Any]:
    """Dashboard shape: configured/degraded status per channel."""
    limit = max(1, as_int(threshold, 3))
    channels = state.get('channels')
    out: dict[str, Any] = {'degraded': [], 'threshold': limit, 'channels': {}}
    if not isinstance(channels, dict):
        return out
    for name, record in channels.items():
        if not isinstance(record, dict):
            continue
        failures = as_int(record.get('consecutive_failures'), 0)
        degraded = failures >= limit
        status = (
            'degraded' if degraded
            else ('ok' if record.get('last_ok_at') else 'pending')
        )
        out['channels'][name] = {
            'consecutive_failures': failures,
            'last_ok_at': str(record.get('last_ok_at') or ''),
            'last_attempt_at': str(record.get('last_attempt_at') or ''),
            'last_error': str(record.get('last_error') or ''),
            'status': status,
        }
        if degraded:
            out['degraded'].append(name)
    out['degraded'] = sorted(out['degraded'])
    return out