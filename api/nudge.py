from __future__ import annotations

import asyncio

from typing import Any

from helpers.api import ApiHandler, Request, Response

from usr.plugins.chat_shepherd.helpers.constants import NUDGE_TEXT
from usr.plugins.chat_shepherd.helpers import state as state_mod
from usr.plugins.chat_shepherd.helpers import monitor
from usr.plugins.chat_shepherd.helpers.state import get_chat, update_chat, append_history
from datetime import datetime, timezone


def _get_context(chat_id: str):
    # Module-level seam so tests can patch the framework lookup
    # (same pattern as api/draft.py).
    from agent import AgentContext

    return AgentContext.get(chat_id)


def _is_tracked_chat(chat_id: str) -> bool:
    # v1.18.3 (P7): the exact predicate tick() applies - 8-alnum framework
    # id pattern plus a persisted usr/chats/<id> transcript dir. Manual
    # nudges may only target real UI chats, never script-created contexts.
    try:
        return monitor._has_chat_dir(chat_id)
    except Exception:
        return False


# v1.20.4: states the auto-nudge path deliberately refuses to act on, paired
# with the operator-facing explanation. The AUTO ladder is guarded by
# monitor._last_log_fatal() + the durable terminated_at stamp, because a blind
# auto-nudge on a chat that died from a real error just restarts the same
# failing code path (and on a security termination it re-triggers the safety
# check that killed it). A HUMAN must always be able to override - the operator
# may well have fixed the underlying bug - but until now the button gave no hint
# that this was the one case the shepherd refuses to do on its own.
GUARDS = {
    'fatal_stop': (
        'This chat stopped on an error or a security termination. Chat '
        'Shepherd will not auto-nudge it, because nudging restarts the same '
        'code path that just failed.'
    ),
    'termination_cooldown': (
        'Automatic nudges are still suppressed for this chat after a recent '
        'security termination or error.'
    ),
    'blocked': (
        'This chat is BLOCKED pending human release. The deliberate escape '
        'hatch is /release (single or bulk with confirm); a manual nudge is '
        'an override that releases it implicitly.'
    ),
}


# v1.23.2 (audit fix 7, cycle 2): a HELD exhaustion chat gets an explicit
# guard — manual nudging a held chat makes sense only as a conscious
# override that deliberately re-enters an exhausted provider.
HOLD_GUARD_KEY = 'provider_exhaustion'
GUARDS[HOLD_GUARD_KEY] = (
    'This chat is HELD in a provider-exhaustion retry loop (or on the '
    'post-kill hold) — the provider is exhausted and every continue '
    're-enters an unusable endpoint. A manual nudge is an override.'
)


def _evaluate_guards(ctx: Any, chat_id: str) -> tuple[str, str]:
    """Return (guard_key, operator message), or ('', '') when nudging is safe."""
    try:
        entry = state_mod.read_chat_entry(chat_id)
        # v1.22.0 (stage 2): a blocked chat needs a deliberate human decision.
        # A nudge still works as an override (and releases it implicitly via
        # the standard override bookkeeping), but it must be a conscious one.
        # v1.23.2 (audit fix 30, cycle 2): ONE entry read serves every guard
        # branch (the second full state.json parse is gone).
        if str((entry or {}).get('status', '') or '') == 'blocked':
            return ('blocked', GUARDS['blocked'])
        if monitor._termination_cooldown_active(entry or {}):
            return ('termination_cooldown', GUARDS['termination_cooldown'])
        if (entry or {}).get('provider_exhausted_at', '') or (
            entry or {}).get('exhaustion_since', ''):
            return (HOLD_GUARD_KEY, GUARDS[HOLD_GUARD_KEY])
    except Exception:
        pass
    try:
        if monitor._last_log_fatal(ctx):
            return ('fatal_stop', GUARDS['fatal_stop'])
    except Exception:
        pass
    return ('', '')


def _as_bool(val: Any) -> bool:
    # Strict-ish truthiness: the API is JSON, but a hand-rolled client may send
    # the string "false", which Python's bool() would happily call True.
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(val)


class Nudge(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        # v1.20.7: the framework passes the raw JSON body through, so a
        # non-dict body (list / string / number) raised AttributeError → raw
        # 500. Same guard api/draft.py already applies.
        body = input if isinstance(input, dict) else {}
        # v1.21.0 (A3): `helpers/api.py:79-83` normalizes a non-dict BODY to
        # `{}` before the handler runs, but dict field VALUES still arrive raw,
        # so `{"chat_id": 123}` raised AttributeError on `.strip()` -> a bare
        # 500 for a merely mistyped request. Coerce every field; `str()` here
        # matches draft.py / history.py / test_notify.py.
        chat_id = str(body.get('chat_id') or '').strip()
        text = str(body.get('text') or '').strip() or None
        # v1.20.4: an explicit operator override of the fatal-stop guard.
        force = _as_bool(body.get('force'))

        if not chat_id:
            return {'success': False, 'error': 'chat_id is required'}

        # v1.23.2 (audit fix 4, cycle 2): the tracked predicate does disk
        # I/O (cached scandir + bounded stats) — off the event loop.
        if not await asyncio.to_thread(_is_tracked_chat, chat_id):
            # v1.18.3 (P7): reject BEFORE communicate() and before get_chat()
            # can auto-create a ghost state entry for a context the monitor
            # never tracks (e.g. script-created verify-* contexts).
            return {'success': False, 'error': f'Chat {chat_id} is not a tracked chat'}

        try:
            from agent import UserMessage
        except Exception as e:
            return {'success': False, 'error': f'Import error: {e}'}

        ctx = _get_context(chat_id)
        if ctx is None:
            return {'success': False, 'error': f'Context {chat_id} not found'}
        # v1.24.1 (audit fix P3-4, cycle 3): never nudge the supervisor's
        # own watchdog context.
        from usr.plugins.chat_shepherd.helpers import wake as _wk
        if _wk.is_shepherd_context(ctx):
            return {'success': False, 'error': f'Chat {chat_id} is a Chat Shepherd wake context'}


        # v1.20.4: ask before restarting a chat that died from a real error.
        # The guard is evaluated ALWAYS, not only on the unforced path - a
        # forced nudge still has to know WHICH guard it overrode so the journal
        # row and the preserved status make sense. This is a CONFIRMATION
        # REQUEST, not an error: nothing is sent and no state is touched, so
        # the caller can re-issue with force=true.
        guard, warning = await asyncio.to_thread(_evaluate_guards, ctx, chat_id)
        if guard and not force:
            return {
                'success': True,
                'chat_id': chat_id,
                'needs_confirmation': True,
                'guard': guard,
                'warning': warning,
            }

        # Guarded + forced = a deliberate operator override. Unguarded = a
        # routine manual nudge, even if the client sent force.
        override = bool(guard)

        try:
            msg = UserMessage(message=text or NUDGE_TEXT)
            ctx.communicate(msg)
        except Exception as e:
            return {'success': False, 'error': f'Nudge failed: {e}'}

        def _apply(st: dict) -> dict:
            now_iso = datetime.now(timezone.utc).isoformat()
            entry = get_chat(st, chat_id)
            # v1.21.0 (A1): the entry's counters come from state.json, which is
            # hand-editable and survives upgrades. `load_state` repairs unusable
            # counters at the boundary, and this coerces again so a value written
            # by an older version (or mutated in-process) cannot make the
            # bookkeeping raise - it would do so AFTER the message was already
            # delivered, turning a successful nudge into a 500 with no counter
            # and no journal row.
            new_count = monitor._as_int(entry.get('nudge_count', 0)) + 1
            # v1.20.4: an override must NOT erase the evidence. The old code
            # hardcoded status='nudged', so nudging an errored chat flipped the
            # row to a healthy-looking "nudged" and replaced the reason it died
            # - hiding exactly the signal the operator needs after an override.
            # Keep the prior status and annotate instead.
            if override:
                prior_status = str(entry.get('status', '') or '')
                # v1.22.0 (stage 2): an override nudge IS the release from
                # `blocked` — the message was delivered, so keeping the
                # terminal status and the reason would have the chat
                # re-blocked on the unchanged error.txt the next tick anyway.
                # Explicit release evidence: the current verdict's signature.
                if guard == 'blocked' and prior_status == 'blocked':
                    update_chat(st, chat_id,
                        nudges_sent=monitor._as_int(entry.get('nudges_sent', 0)) + 1,
                        nudge_count=new_count,
                        last_nudge_at=now_iso,
                        manual_nudge_override_at=now_iso,
                        manual_nudge_override_guard=guard,
                        released_signature=str(entry.get('category_signature') or ''),
                        blocked_reason='',
                        blocked_at='',
                        supervisor_settled=False,
                        status='nudged',
                        last_classification=(
                            f'Manual nudge OVERRIDE released the block: {warning} '
                            f'(attempt {new_count})'
                        ),
                    )
                    append_history(st, {
                        'chat_id': chat_id,
                        'action': 'blocked_released',
                        'detail': 'released by manual nudge override',
                        'previous_reason': str(entry.get('blocked_reason') or '')[:200],
                        'timestamp': now_iso,
                    })
                    return {
                        'success': True,
                        'chat_id': chat_id,
                        'nudge_count': new_count,
                        'overrode_guard': True,
                        'message': 'Nudge sent; block released (override)',
                    }
                update_chat(st, chat_id,
                    nudges_sent=monitor._as_int(entry.get('nudges_sent', 0)) + 1,
                    nudge_count=new_count,
                    last_nudge_at=now_iso,
                    manual_nudge_override_at=now_iso,
                    manual_nudge_override_guard=guard,
                    last_classification=(
                        f'Manual nudge OVERRIDE ({guard}) on a chat last seen as '
                        f'{prior_status or "unknown"}: {warning} '
                        f'(attempt {new_count})'
                    ),
                )
            else:
                update_chat(st, chat_id,
                    nudges_sent=monitor._as_int(entry.get('nudges_sent', 0)) + 1,
                    nudge_count=new_count,
                    last_nudge_at=now_iso,
                    status='nudged',
                    last_classification=f'Manual nudge (attempt {new_count})',
                )
            append_history(st, {
                'chat_id': chat_id,
                # v1.20.4: a distinct action so the timeline can tell a
                # deliberate restart of a broken chat from a routine nudge.
                'action': 'manual_nudge_override' if override else 'manual_nudge',
                'nudge_count': new_count,
                'detail': guard if override else '',
                'timestamp': now_iso,
            })
            return {
                'success': True,
                'chat_id': chat_id,
                'nudge_count': new_count,
                'overrode_guard': override,
                'message': (
                    'Nudge sent (guard overridden)' if override
                    else 'Nudge sent successfully'
                ),
            }

        # v1.18.0 (P7): serialized RMW under the shared state lock; the
        # communicate() above stays outside (audited cross-thread contract).
        return await asyncio.to_thread(state_mod.state_transaction_apply, _apply)
