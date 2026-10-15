# chat_shepherd — supervised resume draft endpoint (v1.18.0).
#
# Route: POST /api/plugins/chat_shepherd/draft
# body {draft_id, action: 'send'|'dismiss', text?}
# send -> communicate the (optionally edited) draft text to the
# chat, drop the draft, journal 'draft_sent'; reuses the
# audited manual-nudge mechanics (api/nudge.py)
# dismiss -> drop the draft without sending, journal 'draft_dismissed'
# and stamp draft_dismissed_at on the chat so monitor does
# not re-queue for DRAFT_REQUEUE_MIN minutes
#
# The context lookup goes through _get_context so tests can patch it.
# v1.18.0 (P7): dismiss and the send bookkeeping run as serialized
# read-modify-write transactions under the shared state lock; the
# peek below stays lock-free but the transaction re-verifies the
# draft on the fresh snapshot, and communicate() stays outside the
# lock (audited cross-thread contract).

from __future__ import annotations

import asyncio

from datetime import datetime, timezone
from typing import Any

from helpers.api import ApiHandler, Request, Response

from usr.plugins.chat_shepherd.helpers import state as state_mod
from usr.plugins.chat_shepherd.helpers import monitor
from usr.plugins.chat_shepherd.helpers.state import (
    get_chat,
    update_chat,
    append_history,
    remove_draft,
)


def _get_context(chat_id: str):
    from agent import AgentContext

    return AgentContext.get(chat_id)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _restore_claimed_draft(claim: dict) -> None:
    # v1.20.7: put a claimed draft back after a failed send so the review
    # item is not lost. Best-effort: a failed restore leaves the send
    # error response as the operator's signal to re-check the dashboard.
    claimed = claim.get('draft')
    if not isinstance(claimed, dict):
        return

    def _restore(st: dict) -> dict:
        drafts = st.get('drafts')
        if not isinstance(drafts, list):
            drafts = []
            st['drafts'] = drafts
        if not isinstance(claimed.get('id'), str):
            return {'success': False}
        # v1.23.2 (audit fix 14, cycle 2): dedupe by draft id AND by chat —
        # a failed send raced with a newly queued draft of ANY kind for the
        # same chat would previously restore next to it, violating the
        # one-draft-per-chat invariant add_draft enforces (add_draft's
        # dedupe would then keep refusing new drafts for the chat).
        if not any(
            isinstance(d, dict) and d.get('id') == claimed.get('id')
            for d in drafts
        ) and not any(
            isinstance(d, dict) and d.get('chat_id') == claimed.get('chat_id')
            for d in drafts
        ):
            if len(drafts) < state_mod.DRAFT_MAX:
                drafts.append(claimed)
        return {'success': True}

    try:
        await asyncio.to_thread(state_mod.state_transaction_apply, _restore)
    except Exception:
        pass


class Draft(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        body = input if isinstance(input, dict) else {}
        action = str(body.get('action') or 'send').strip()
        draft_id = str(body.get('draft_id') or '').strip()
        edited_text = str(body.get('text') or '').strip()

        if not draft_id:
            return {'success': False, 'error': 'draft_id is required'}
        # v1.21.0 (O7): action is validated BEFORE any state read, and the
        # full `load_state()` peek is gone. It existed only to give cheap
        # early returns, but it re-read and re-parsed the whole state file
        # (and the journal) on every single review action, and the transaction
        # below then did the same work again - so a send cost two full state
        # loads to move one draft. The claim transaction now returns the draft
        # (with its chat_id) and the dismiss transaction finds the draft
        # itself, so nothing needs the peek. The atomic-claim race fix from
        # v1.20.7 is unaffected: it lived in `_claim`, not in the peek.
        if action not in ('send', 'dismiss'):
            return {'success': False, 'error': f'Unknown action {action!r}'}

        if action == 'dismiss':
            def _apply(st: dict) -> dict:
                draft = next(
                    (d for d in st.get('drafts', [])
                     if isinstance(d, dict) and d.get('id') == draft_id),
                    None,
                )
                if draft is None:
                    return {'success': False, 'error': f'Draft {draft_id} not found'}
                chat_id = str(draft.get('chat_id') or '')
                if not chat_id:
                    return {'success': False, 'error': 'draft has no chat_id'}
                now_iso = _now_iso()
                update_chat(st, chat_id,
                    draft_dismissed_at=now_iso,
                    last_classification='Supervised draft dismissed by user',
                )
                append_history(st, {
                    'chat_id': chat_id,
                    'action': 'draft_dismissed',
                    'detail': str(draft.get('kind') or ''),
                    'timestamp': now_iso,
                })
                remove_draft(st, draft_id)
                return {'success': True, 'chat_id': chat_id, 'message': 'Draft dismissed'}

            return await asyncio.to_thread(state_mod.state_transaction_apply, _apply)

        # action == 'send': validate + communicate OUTSIDE the lock.
        # v1.20.7: CLAIM the draft in its own transaction BEFORE communicating.
        # The old code communicated first and only re-verified inside the
        # bookkeeping transaction below - which it never actually did (only
        # dismiss had the found check), so two racing sends (double-click /
        # two tabs) both passed the lock-free peek, both delivered the message
        # to the agent, and both bumped nudge_count/nudges_sent and journaled
        # draft_sent. The claim is atomic under the state lock, so the second
        # racer is rejected before it ever communicates.
        try:
            from agent import UserMessage
        except Exception as e:
            return {'success': False, 'error': f'Import error: {e}'}

        def _claim(st: dict) -> dict:
            claimed = next(
                (d for d in st.get('drafts', [])
                 if isinstance(d, dict) and d.get('id') == draft_id),
                None,
            )
            if claimed is None:
                return {'success': False, 'error': f'Draft {draft_id} not found'}
            remove_draft(st, draft_id)
            # v1.23.2 (audit fix 17, cycle 2): report the chat's CURRENT
            # status in the same transaction — the send path refuses a
            # drafted send to a chat that became BLOCKED since the draft
            # was queued (a stale review item had released a supervisor
            # block by status overwrite, with no release bookkeeping).
            _ent = (st.get('chats') or {}).get(
                str(claimed.get('chat_id') or '')) or {}
            return {'success': True, 'draft': claimed,
                    'chat_status': str(_ent.get('status') or '')}

        claim = await asyncio.to_thread(state_mod.state_transaction_apply, _claim)
        if not claim.get('success'):
            return {'success': False, 'error': claim.get('error', 'Draft not found')}
        # v1.21.0 (O7): chat_id now comes from the claimed draft itself.
        draft = claim.get('draft') or {}
        chat_id = str(draft.get('chat_id') or '')
        if not chat_id:
            await _restore_claimed_draft(claim)
            return {'success': False, 'error': 'draft has no chat_id'}
        text = edited_text or str(draft.get('text') or '').strip()
        if not text:
            await _restore_claimed_draft(claim)
            return {'success': False, 'error': 'draft text is empty'}

        try:
            ctx = _get_context(chat_id)
        except Exception as e:
            await _restore_claimed_draft(claim)
            return {'success': False, 'error': f'Context lookup failed: {e}'}
        if ctx is None:
            await _restore_claimed_draft(claim)
            return {'success': False, 'error': f'Context {chat_id} not found'}

        # v1.23.2 (audit fix 17, cycle 2): a draft is a snapshot in time —
        # the chat may have become BLOCKED between draft creation and send.
        # The send path had no blocked check (unlike /nudge), so it flipped
        # the terminal status to 'nudged' with the block fields dangling —
        # a stale review item releasing a block the supervisor judged
        # human-only, with no release bookkeeping. The status rides the
        # claim transaction (no extra read); refuse with the hint.
        if str(claim.get('chat_status') or '') == 'blocked':
            await _restore_claimed_draft(claim)
            return {
                'success': False,
                'error': ('Chat is BLOCKED pending human release; send the '
                          'draft only after a deliberate /release'),
                'hint': 'POST /api/plugins/chat_shepherd/release with chat_id',
            }

        try:
            ctx.communicate(UserMessage(message=text))
        except Exception as e:
            # Restore the claimed draft: a transient send failure must not
            # silently destroy the queued review item.
            await _restore_claimed_draft(claim)
            return {'success': False, 'error': f'Send failed: {e}'}

        def _apply(st: dict) -> dict:
            # The draft was already claimed/removed by _claim; this transaction
            # is bookkeeping only (remove_draft below stays as an idempotent
            # safety net).
            entry = get_chat(st, chat_id)
            now_iso = _now_iso()
            # v1.21.0 (A1): coerce the persisted counters before the arithmetic
            # - a string counter here raised out of the transaction AFTER the
            # draft had been delivered, so the operator saw a 500 for a send
            # that worked, with the draft already gone.
            new_count = monitor._as_int(entry.get('nudge_count', 0)) + 1
            update_chat(st, chat_id,
                nudges_sent=monitor._as_int(entry.get('nudges_sent', 0)) + 1,
                nudge_count=new_count,
                last_nudge_at=now_iso,
                status='nudged',
                last_classification=f'Supervised resume sent (draft {draft_id})',
            )
            append_history(st, {
                'chat_id': chat_id,
                'action': 'draft_sent',
                'nudge_count': new_count,
                'detail': str(draft.get('kind') or ''),
                'timestamp': now_iso,
            })
            remove_draft(st, draft_id)
            return {
                'success': True,
                'chat_id': chat_id,
                'nudge_count': new_count,
                'message': 'Draft sent',
            }

        return await asyncio.to_thread(state_mod.state_transaction_apply, _apply)
