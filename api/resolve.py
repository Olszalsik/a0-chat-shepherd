from __future__ import annotations

from typing import Any

from helpers.api import ApiHandler, Request, Response

from usr.plugins.chat_shepherd.helpers.state import load_state, save_state, get_chat, update_chat, append_history
from datetime import datetime, timezone


class Resolve(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        chat_id = (input.get('chat_id') or '').strip()
        action = (input.get('action') or 'resolve').strip()

        if not chat_id:
            return {'success': False, 'error': 'chat_id is required'}

        now_iso = datetime.now(timezone.utc).isoformat()
        state = load_state()

        if action == 'dismiss':
            chats = state.get('chats', {})
            if chat_id in chats:
                del chats[chat_id]
                append_history(state, {
                    'chat_id': chat_id,
                    'action': 'dismissed',
                    'timestamp': now_iso,
                })
                save_state(state)
                return {'success': True, 'message': f'Chat {chat_id} removed from watch list'}
            return {'success': False, 'error': f'Chat {chat_id} not found in state'}

        update_chat(state, chat_id,
            status='awaiting_user',
            nudge_count=0,
            last_nudge_at='',
            last_classification='Intervention resolved by user',
        )
        append_history(state, {
            'chat_id': chat_id,
            'action': 'resolved',
            'timestamp': now_iso,
        })
        save_state(state)

        return {
            'success': True,
            'chat_id': chat_id,
            'message': 'Intervention resolved, nudge count reset',
        }
