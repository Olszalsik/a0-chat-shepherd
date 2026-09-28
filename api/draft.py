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
        if not any(
            isinstance(d, dict) and d.get('id') == claimed.get('id')
            for d in drafts
        ):
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

        # Lock-free peek for cheap early returns only; the transaction
        # below re-finds the draft on a fresh locked snapshot.
        # v1.18.1: sync file reads stay off the request event loop.
        peek = await asyncio.to_thread(state_mod.load_state)
        draft = None
        for d in peek.get('drafts', []):
            if isinstance(d, dict) and d.get('id') == draft_id:
                draft = d
                break
        chat_id = str(draft.get('chat_id') or '') if draft else ''

        if draft is None:
            return {'success': False, 'error': f'Draft {draft_id} not found'}
        if not chat_id:
            return {'success': False, 'error': 'draft has no chat_id'}
        if action not in ('send', 'dismiss'):
            return {'success': False, 'error': f'Unknown action {action!r}'}

        if action == 'dismiss':
            def _apply(st: dict) -> dict:
                found = any(
                    isinstance(d, dict) and d.get('id') == draft_id
                    for d in st.get('drafts', [])
                )
                if not found:
                    return {'success': False, 'error': f'Draft {draft_id} not found'}
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
            return {'success': True, 'draft': claimed}

        claim = await asyncio.to_thread(state_mod.state_transaction_apply, _claim)
        if not claim.get('success'):
            return {'success': False, 'error': claim.get('error', 'Draft not found')}
        # Prefer the claimed snapshot's text (the peek is stale by definition).
        text = edited_text or str((claim.get('draft') or {}).get('text') or '').strip()
        if not text:
            return {'success': False, 'error': 'draft text is empty'}

        try:
            ctx = _get_context(chat_id)
        except Exception as e:
            await _restore_claimed_draft(claim)
            return {'success': False, 'error': f'Context lookup failed: {e}'}
        if ctx is None:
            await _restore_claimed_draft(claim)
            return {'success': False, 'error': f'Context {chat_id} not found'}

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
            new_count = entry.get('nudge_count', 0) + 1
            update_chat(st, chat_id,
                nudges_sent=entry.get('nudges_sent', 0) + 1,
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
