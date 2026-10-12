"""chat_shepherd — the pure decision layer: may the ladder act, and must the
chat be blocked?

Ported from the chat_shepherd2 donor module in gate-section form only. The
queues, budgets, cooldowns and drains stay in monitor.py (v1's own audited
ladder); this module adds the two decision points v1 lacked:

1. `may_remediate` — the cause-gated ladder: a configured allowlist plus a
   NEVER_REMEDIATE floor applied AFTER it, so config can never re-enable
   auto-fixing a safety termination or an auth failure.
2. `plan_seam` — the per-chat verdict the tick applies before queueing:
   ``block`` (needs a human, durable until release), ``hold`` (visible, no
   auto-action), or ``act`` (ladder eligible).

Pure: no framework imports, no I/O, no clock reads.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from usr.plugins.chat_shepherd.helpers.constants import (
    CATEGORY_LABELS,
    CATEGORY_NONE,
    NEVER_REMEDIATE,
)

# Verdicts the tick understands.
ACT = 'act'
HOLD = 'hold'
BLOCK = 'block'


def may_remediate(category: str, allowed: Any) -> bool:
    """Whether the ladder may act on this cause at all.

    ``NEVER_REMEDIATE`` is a floor applied after the allowlist, not a default
    entry in it: adding a category to the config cannot re-enable auto-fixing
    a safety termination or an auth failure.
    """
    cat = category or CATEGORY_NONE
    if cat in NEVER_REMEDIATE:
        return False
    if not allowed:
        return False
    return cat in set(allowed)


def plan_seam(
    category: str,
    allowed: Any,
    *,
    released_signature: str = '',
    category_signature: str = '',
) -> tuple[str, str]:
    """One chat's remediation verdict: act, hold, or block.

    The semantics are the donor plan_chat's two lower gates, lifted out of
    the ladder. Ordering inside is load-bearing:

    - NEVER_REMEDIATE first: a chat can be running AND carry a safety
      verdict, and the verdict is the thing that must win.
    - The released-signature hold exists because a human release must not be
      undone by the verdict that caused the block. Without it the release is
      decorative: the human comes back, the chat leaves blocked, and the same
      tick re-blocks it from the still-valid error.txt. The signature
      distinguishes "the same failure the human already looked at" from "a
      new failure".
    - Non-allowlisted categories HOLD, not block: nothing has actually gone
      wrong that needs a human; the ladder just has no cause-directed move.
    """
    cat = category or CATEGORY_NONE
    if cat in NEVER_REMEDIATE:
        released = str(released_signature or '')
        if released and released == str(category_signature or ''):
            return HOLD, 'released by a human; holding until the verdict changes'
        return (
            BLOCK,
            f'{CATEGORY_LABELS.get(cat, cat)} (no auto-remediation by design)',
        )
    if cat == CATEGORY_NONE or may_remediate(cat, allowed):
        return ACT, ''
    return HOLD, f'category {cat} is not in auto_remediate_categories'


def human_arrived_after_block(last_type: str, human_ts: float, blocked_at: str) -> bool:
    """Did a genuine human turn arrive AFTER this chat was blocked?

    Both halves are required and neither is sufficient alone: the newest
    relevant log entry must be a human `user` turn AND that turn's own
    timestamp must be newer than `blocked_at`. A chat blocked by a build
    without a `blocked_at` stamp is NOT released automatically — absent
    evidence must not become a release.
    """
    if last_type != 'user':
        return False
    if not blocked_at:
        return False
    if isinstance(blocked_at, datetime):
        dt = blocked_at
    else:
        try:
            dt = datetime.fromisoformat(str(blocked_at))
        except Exception:
            return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        human = float(human_ts or 0.0)
    except (TypeError, ValueError):
        return False
    if human <= 0.0:
        return False
    return human > dt.timestamp()


def block_budget(already_blocked: int) -> int:
    """Per-tick cap on NEW blocked transitions, bounded 1-5.

    Scaled down as blocked transitions accumulate in state so one chaotic
    tick cannot block half the fleet; the next tick picks it up again.
    """
    return max(1, min(5, 10 - max(0, int(already_blocked or 0))))


# v1.22.0 (stage 5, ported from chat_shepherd2): the continuity-wake decision.
# The due check and the *reason* are separate: a wake that is on schedule but
# has nothing to report is the common case, and spending a full investigation
# on it is how a health check becomes the biggest cost on the box. A reason is
# required unless `continuity_always` is set.

ACTION_WAKE = 'wake'


def wakes_used(state: dict[str, Any], now: Any = None) -> int:
    continuity = state.get('continuity') or {}
    if not isinstance(continuity, dict):
        return 0
    now_dt = now if isinstance(now, datetime) else None
    day = (now_dt or datetime.now(timezone.utc)).strftime('%Y-%m-%d')
    try:
        return int(continuity.get('wakes_' + day) or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def plan_wake(
    chats: list[dict[str, Any]],
    state: dict[str, Any],
    cfg: dict[str, Any],
    *,
    due: bool = False,
    within_active_hours: bool = True,
    grace_ok: bool = True,
) -> dict[str, Any]:
    """Decide whether the continuity wake should fire, and why.

    Returns ``{action, reason, skipped}``. ``skipped`` is non-empty when the
    wake was deliberately not fired — that string is the audit trail (the
    monitor journals it only when the reason CHANGES, to keep the journal
    quiet).
    """
    from usr.plugins.chat_shepherd.helpers.clock import as_float, as_int

    out: dict[str, Any] = {'action': None, 'reason': '', 'skipped': ''}

    if not bool(cfg.get('continuity_enabled')):
        out['skipped'] = 'continuity disabled'
        return out
    if not grace_ok:
        out['skipped'] = 'startup grace period'
        return out
    if not due:
        out['skipped'] = 'not due'
        return out
    if not within_active_hours:
        out['skipped'] = 'outside active hours'
        return out

    ceiling = as_int(cfg.get('continuity_max_wakes_per_day'), 12)
    # `0` means disabled, like every other budget in this config. A truthiness
    # test here read `0` as "no ceiling" — the exact inverse of the documented
    # convention — so the disabled case has its own branch.
    if ceiling <= 0:
        out['skipped'] = 'daily wake ceiling disabled (0)'
        return out
    if wakes_used(state) >= ceiling:
        out['skipped'] = f'daily wake ceiling reached ({ceiling})'
        return out

    for chat in chats:
        status = str(chat.get('status') or '')
        if (
            bool(cfg.get('continuity_on_goal'))
            and status == 'awaiting_user'
            and chat.get('goal_open')
        ):
            out['action'] = ACTION_WAKE
            out['reason'] = f'open goal is waiting for a human ({chat.get("chat_id")})'
            return out
        if (
            bool(cfg.get('continuity_on_nudge_exhaustion'))
            and status == 'stalled'
            and as_int(chat.get('nudge_count'), 0) >= as_int(cfg.get('max_auto_nudges'), 3)
        ):
            out['action'] = ACTION_WAKE
            out['reason'] = f'chat exhausted its nudge budget ({chat.get("chat_id")})'
            return out
        if (
            bool(cfg.get('continuity_on_intervention'))
            and status in ('intervention', 'error', 'blocked')
            and as_float(chat.get('minutes_frozen'), 0.0)
            >= as_float(cfg.get('continuity_intervention_minutes'), 30.0)
        ):
            out['action'] = ACTION_WAKE
            out['reason'] = f'chat has been unresolved too long ({chat.get("chat_id")})'
            return out

    if bool(cfg.get('continuity_always')):
        out['action'] = ACTION_WAKE
        out['reason'] = 'continuity_always'
        return out

    out['skipped'] = 'no wake reason'
    return out