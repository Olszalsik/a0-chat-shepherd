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
}


def _evaluate_guards(ctx: Any, chat_id: str) -> tuple[str, str]:
    """Return (guard_key, operator message), or ('', '') when nudging is safe."""
    try:
        if monitor._last_log_fatal(ctx):
            return ('fatal_stop', GUARDS['fatal_stop'])
    except Exception:
        pass
    try:
        entry = state_mod.read_chat_entry(chat_id)
        if monitor._termination_cooldown_active(entry):
            return ('termination_cooldown', GUARDS['termination_cooldown'])
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
        chat_id = (input.get('chat_id') or '').strip()
        text = (input.get('text') or '').strip() or None
        # v1.20.4: an explicit operator override of the fatal-stop guard.
        force = _as_bool(input.get('force'))

        if not chat_id:
            return {'success': False, 'error': 'chat_id is required'}

        if not _is_tracked_chat(chat_id):
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
            new_count = entry.get('nudge_count', 0) + 1
            # v1.20.4: an override must NOT erase the evidence. The old code
            # hardcoded status='nudged', so nudging an errored chat flipped the
            # row to a healthy-looking "nudged" and replaced the reason it died
            # - hiding exactly the signal the operator needs after an override.
            # Keep the prior status and annotate instead.
            if override:
                prior_status = str(entry.get('status', '') or '')
                update_chat(st, chat_id,
                    nudges_sent=entry.get('nudges_sent', 0) + 1,
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
                    nudges_sent=entry.get('nudges_sent', 0) + 1,
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
