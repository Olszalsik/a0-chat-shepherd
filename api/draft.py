# chat_shepherd — supervised resume draft endpoint (v1.16.0).
#
# Route: POST /api/plugins/chat_shepherd/draft
# body {draft_id, action: 'send'|'dismiss', text?}
#   send    -> communicate the (optionally edited) draft text to the
#              chat, drop the draft, journal 'draft_sent'; reuses the
#              audited manual-nudge mechanics (api/nudge.py)
#   dismiss -> drop the draft without sending, journal 'draft_dismissed'
#              and stamp draft_dismissed_at on the chat so monitor does
#              not re-queue for DRAFT_REQUEUE_MIN minutes
#
# The context lookup goes through _get_context so tests can patch it.

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from helpers.api import ApiHandler, Request, Response

from usr.plugins.chat_shepherd.helpers.state import (
    load_state,
    save_state,
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


class Draft(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        body = input if isinstance(input, dict) else {}
        action = str(body.get('action') or 'send').strip()
        draft_id = str(body.get('draft_id') or '').strip()
        edited_text = str(body.get('text') or '').strip()

        if not draft_id:
            return {'success': False, 'error': 'draft_id is required'}

        state = load_state()
        draft = None
        for d in state.get('drafts', []):
            if isinstance(d, dict) and d.get('id') == draft_id:
                draft = d
                break
        if draft is None:
            return {'success': False, 'error': f'Draft {draft_id} not found'}

        chat_id = str(draft.get('chat_id') or '')
        if not chat_id:
            return {'success': False, 'error': 'draft has no chat_id'}

        if action == 'dismiss':
            now_iso = _now_iso()
            update_chat(state, chat_id,
                draft_dismissed_at=now_iso,
                last_classification='Supervised draft dismissed by user',
            )
            append_history(state, {
                'chat_id': chat_id,
                'action': 'draft_dismissed',
                'detail': str(draft.get('kind') or ''),
                'timestamp': now_iso,
            })
            remove_draft(state, draft_id)
            save_state(state)
            return {'success': True, 'chat_id': chat_id, 'message': 'Draft dismissed'}

        if action != 'send':
            return {'success': False, 'error': f'Unknown action {action!r}'}

        text = edited_text or str(draft.get('text') or '').strip()
        if not text:
            return {'success': False, 'error': 'draft text is empty'}

        try:
            from agent import UserMessage
        except Exception as e:
            return {'success': False, 'error': f'Import error: {e}'}

        try:
            ctx = _get_context(chat_id)
        except Exception as e:
            return {'success': False, 'error': f'Context lookup failed: {e}'}
        if ctx is None:
            return {'success': False, 'error': f'Context {chat_id} not found'}

        try:
            ctx.communicate(UserMessage(message=text))
        except Exception as e:
            return {'success': False, 'error': f'Send failed: {e}'}

        now_iso = _now_iso()
        entry = get_chat(state, chat_id)
        new_count = entry.get('nudge_count', 0) + 1
        update_chat(state, chat_id,
            nudges_sent=entry.get('nudges_sent', 0) + 1,
            nudge_count=new_count,
            last_nudge_at=now_iso,
            status='nudged',
            last_classification=f'Supervised resume sent (draft {draft_id})',
        )
        append_history(state, {
            'chat_id': chat_id,
            'action': 'draft_sent',
            'nudge_count': new_count,
            'detail': str(draft.get('kind') or ''),
            'timestamp': now_iso,
        })
        remove_draft(state, draft_id)
        save_state(state)
        return {
            'success': True,
            'chat_id': chat_id,
            'nudge_count': new_count,
            'message': 'Draft sent',
        }
