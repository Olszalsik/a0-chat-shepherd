"""chat_shepherd — the supervisor's health check on itself.

Ported from the chat_shepherd2 donor module (pure evaluation kept verbatim;
the clock/config helpers this plugin does not carry are local).

A monitor that cannot report its own failure is a monitor that fails silently.
That is the most common way one of these dies: the tick raises, the extension
logs one line, the dashboard keeps showing the last good snapshot, and nobody
learns anything until a chat is visibly dead days later.

Two properties make this useful rather than decorative:

* **It is pure.** ``evaluate(snapshot)`` takes a dict of already-collected
  facts and returns findings. It cannot itself hang on the thing it is
  checking — the classic self-monitoring trap, where the watchdog shares the
  failure mode of the thing it watches.

* **A failing check is itself durable.** The caller stamps the failure
  counters durably (monitor._record_tick_failure) *before* the next
  evaluation, so even a check that dies leaves the evidence behind. A
  self-check that vanishes is worse than one that reports a problem.

Findings carry a severity. ``info`` findings are surfaced and never paged;
only ``warning`` and ``error`` can page a human, and paging is persistence
gated with a re-arm window so a persistent fault does not become a page every
five minutes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

# --- finding kinds (donor names, v1 constants vocabulary) ---
HC_FIRST_RUN = 'first_run'
HC_TICK_LATE = 'tick_late'
HC_TICK_FAILED = 'tick_failed'
HC_INFLIGHT_STUCK = 'inflight_stuck'
HC_CHANNEL_DEGRADED = 'channel_degraded'
HC_CAPABILITY_DEGRADED = 'capability_degraded'
HC_TRACKED_GAP = 'tracked_gap'
HC_SAVE_FAILING = 'save_failing'
HC_EXTERNAL_BLOCKS = 'external_blocks'
HC_WAKE_CAP = 'wake_cap_reached'
HC_ACTION_CAP = 'action_cap_reached'
HC_LOOP_SUSPENDED = 'loop_suspended'
# v1.23.0: alerts fired recently but every page went to the in-memory toast
# only — the operator is not told out-of-band. Info severity by design: the
# dashboard IS the channel, this only makes its limits visible.
HC_NO_ALERT_CHANNEL = 'no_alert_channel'

FINDING_SEVERITIES = {
    HC_FIRST_RUN: 'info',
    HC_TICK_LATE: 'warning',
    HC_TICK_FAILED: 'error',
    HC_INFLIGHT_STUCK: 'warning',
    HC_CHANNEL_DEGRADED: 'warning',
    HC_CAPABILITY_DEGRADED: 'warning',
    HC_TRACKED_GAP: 'warning',
    HC_SAVE_FAILING: 'warning',
    HC_EXTERNAL_BLOCKS: 'info',
    HC_WAKE_CAP: 'info',
    HC_ACTION_CAP: 'info',
    HC_LOOP_SUSPENDED: 'warning',
    HC_NO_ALERT_CHANNEL: 'info',
}

HEALTH_OK = 'ok'
HEALTH_DEGRADED = 'degraded'
HEALTH_BROKEN = 'broken'
HEALTH_UNKNOWN = 'unknown'
SEVERITY_INFO = 'info'
SEVERITY_WARN = 'warning'
SEVERITY_ERROR = 'error'


# --- tolerant, never-raising evidence readers (donor as_int/as_float) -------

def as_int(value: Any, default: int = 0) -> int:
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


def minutes_since(value: Any, now: Any = None) -> float:
    """Minutes elapsed since an ISO stamp (datetime objects accepted).

    Unparseable evidence reads 9999 minutes — caller sees "long ago", which
    is the fail-loud direction (a real timestamp always beats 9999).
    """
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value or ''))
        except Exception:
            return 9999.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ref = now if isinstance(now, datetime) else None
    if ref is None:
        ref = datetime.now(timezone.utc)
    return max(0.0, (ref - dt).total_seconds() / 60.0)


def _finding(kind: str, detail: str, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        'kind': kind,
        'severity': FINDING_SEVERITIES.get(kind, SEVERITY_WARN),
        'detail': str(detail)[:300],
    }
    row.update(extra)
    return row


def evaluate(
    snapshot: dict[str, Any], cfg: dict[str, Any], *, now: Any = None
) -> list[dict[str, Any]]:
    """Every invariant that can be judged from the snapshot.

    Never raises and never returns an exception: a check that dies is worse
    than a check that says nothing, because it also destroys the evidence
    that the check ran.
    """
    try:
        return _evaluate(snapshot or {}, cfg or {}, now)
    except Exception as exc:  # pragma: no cover - defensive
        return [_finding(HC_TICK_FAILED, f'self-check raised: {exc!r}')]


def _evaluate(snapshot: dict[str, Any], cfg: dict[str, Any], now: Any) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    first_run = not bool(snapshot.get('last_tick'))
    if first_run:
        findings.append(_finding(HC_FIRST_RUN, 'no previous tick recorded'))
    elif bool(cfg.get('selfcheck_enabled', True)):
        stale = as_float(cfg.get('stale_tick_minutes'), 10.0)
        gap = minutes_since(snapshot.get('last_tick'), now)
        # `gap` is only meaningful while the loop is awake. A suspended job
        # loop (a second development instance) is not a fault of this plugin.
        if not bool(snapshot.get('loop_suspended')) and gap > stale:
            findings.append(
                _finding(HC_TICK_LATE, f'previous tick was {gap:.0f}m ago (limit {stale:.0f}m)')
            )

    consecutive = as_int(snapshot.get('consecutive_tick_failures'), 0)
    threshold = as_int(cfg.get('notify_after_consecutive_failures'), 3)
    if consecutive >= max(1, threshold):
        findings.append(
            _finding(
                HC_TICK_FAILED,
                f'{consecutive} consecutive tick failures',
                consecutive=consecutive,
            )
        )

    inflight = snapshot.get('inflight') or []
    if isinstance(inflight, list) and inflight:
        findings.append(
            _finding(HC_INFLIGHT_STUCK, f'{len(inflight)} action(s) still in flight')
        )

    # Mirrors the alerting threshold on purpose. Judging a channel "degraded"
    # on its first failure would make a single blip indistinguishable from a
    # broken endpoint, which is the opposite of the signal's purpose.
    channel_threshold = as_int(cfg.get('notify_degraded_after_failures'), 3)
    degraded = [
        name
        for name, health in _channel_map(snapshot).items()
        if _is_degraded(health, channel_threshold)
    ]
    if degraded:
        findings.append(
            _finding(HC_CHANNEL_DEGRADED, 'unreachable: ' + ', '.join(sorted(degraded)))
        )

    live = as_int(snapshot.get('live_contexts'), 0)
    checked = as_int(snapshot.get('checked'), 0)
    if live > 0 and checked == 0:
        # The tracking predicate and the live set disagree: something that
        # exists is invisible. That is a silent blind spot, not a quiet day.
        findings.append(
            _finding(HC_TRACKED_GAP, f'{live} live contexts but 0 were supervised')
        )

    if as_int(snapshot.get('save_failures'), 0) >= 2:
        findings.append(_finding(HC_SAVE_FAILING, 'state writes are failing repeatedly'))

    if as_int(snapshot.get('blocked_external'), 0) > 0:
        findings.append(
            _finding(
                HC_EXTERNAL_BLOCKS,
                f'{as_int(snapshot.get("blocked_external"), 0)} chat(s) blocked elsewhere',
            )
        )

    if bool(snapshot.get('wake_cap_reached')):
        ceiling = as_int(snapshot.get('wake_ceiling'), 0)
        findings.append(_finding(HC_WAKE_CAP, f'daily wake ceiling reached ({ceiling})'))

    if bool(snapshot.get('action_cap_reached')):
        findings.append(_finding(HC_ACTION_CAP, 'per-tick action budget saturated'))

    if bool(snapshot.get('loop_suspended')):
        findings.append(
            _finding(HC_LOOP_SUSPENDED, 'the tick wrapper is raising; the loop is suspended')
        )

    # v1.23.0: alerts fired recently while every external channel is unset.
    # The pages still happened (durable dashboard rows now exist), but nobody
    # was told out-of-band — make that a visible finding, not a silent gap.
    alerts_recent = as_int(snapshot.get('alerts_recent'), 0)
    if alerts_recent > 0 and not bool(snapshot.get('channels_configured')):
        findings.append(
            _finding(
                HC_NO_ALERT_CHANNEL,
                f'{alerts_recent} alert(s) fired recently but no external '
                'alert channel (webhook/Telegram) is configured; they are '
                'dashboard-only',
                alerts_recent=alerts_recent,
            )
        )

    return findings


def _channel_map(snapshot: dict[str, Any]) -> dict[str, Any]:
    channels = snapshot.get('channels')
    return channels if isinstance(channels, dict) else {}


def _is_degraded(health: Any, threshold: Any = 3) -> bool:
    if not isinstance(health, dict):
        return False
    return as_int(health.get('consecutive_failures'), 0) >= max(1, as_int(threshold, 3))


# --- verdict ---------------------------------------------------------------

def health_verdict(findings: list[dict[str, Any]]) -> str:
    """Roll findings up into one word for the dashboard badge.

    The first tick has no history to judge, so ``unknown`` is a real answer
    and must not be rendered as "ok": an unproven supervisor is not a
    healthy one.
    """
    if not findings:
        return HEALTH_OK
    if any(str(row.get('kind')) == HC_FIRST_RUN for row in findings):
        return HEALTH_UNKNOWN
    severities = {str(row.get('severity') or '') for row in findings}
    if SEVERITY_ERROR in severities:
        return HEALTH_BROKEN
    if SEVERITY_WARN in severities:
        return HEALTH_DEGRADED
    if severities <= {SEVERITY_INFO}:
        return HEALTH_OK
    return HEALTH_DEGRADED


def health_score(findings: list[dict[str, Any]]) -> int:
    """0-100. Penalties stack so several warnings read as worse than one."""
    penalty = 0
    for row in findings:
        severity = str(row.get('severity') or '')
        if severity == SEVERITY_ERROR:
            penalty += 40
        elif severity == SEVERITY_WARN:
            penalty += 12
    return max(0, 100 - penalty)


def pageable(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Findings worth waking a human for. Info never pages."""
    return [
        row for row in findings
        if str(row.get('severity')) in (SEVERITY_WARN, SEVERITY_ERROR)
    ]


def should_page(
    findings: list[dict[str, Any]],
    selfcheck_state: dict[str, Any],
    cfg: dict[str, Any],
    *,
    now: Any = None,
) -> tuple[bool, str]:
    """Persistence-gated page with a re-arm window.

    A fault that persists should re-page, but not on every tick. The clock
    starts when the current set of pageable findings is first seen, so a
    warning that never clears pages once, waits, and pages again.
    """
    candidates = pageable(findings)
    if not candidates:
        return False, ''

    if not bool(cfg.get('notify_on_selfcheck', True)):
        return False, ''

    stamp = selfcheck_state if isinstance(selfcheck_state, dict) else {}
    after = as_float(cfg.get('notify_after_minutes'), 30.0)
    rearm = as_float(cfg.get('notify_rearm_minutes'), 60.0)
    kinds = ', '.join(sorted(str(row.get('kind')) for row in candidates))

    # Re-arm is checked FIRST. It gates every repeat page; if the
    # "has persisted long enough" branch ran first, a fault that had already
    # paged would re-page on every tick forever and the channel would be
    # buried the moment it was needed.
    if stamp.get('paged_at'):
        if minutes_since(stamp.get('paged_at'), now) < rearm:
            return False, ''

    # A hard error pages immediately; it is not a condition to sit on.
    if any(str(row.get('severity')) == SEVERITY_ERROR for row in candidates):
        return True, kinds

    if minutes_since(stamp.get('since'), now) >= after:
        return True, kinds

    return False, ''


def health_summary(
    selfcheck_state: dict[str, Any],
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    stamp = selfcheck_state if isinstance(selfcheck_state, dict) else {}
    counts: dict[str, int] = {}
    for row in findings:
        key = str(row.get('kind') or 'unknown')
        counts[key] = counts.get(key, 0) + 1
    return {
        'verdict': health_verdict(findings),
        'score': health_score(findings),
        'last_at': str(stamp.get('last_at') or ''),
        'last_ok': bool(stamp.get('last_ok')),
        'consecutive_failures': as_int(stamp.get('consecutive_failures'), 0),
        'findings': findings,
        'counts': counts,
    }