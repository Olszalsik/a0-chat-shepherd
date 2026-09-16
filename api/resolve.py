from __future__ import annotations

import asyncio

from typing import Any

from helpers.api import ApiHandler, Request, Response

from usr.plugins.chat_shepherd.helpers import state as state_mod
from usr.plugins.chat_shepherd.helpers.state import get_chat, update_chat, append_history
from datetime import datetime, timezone


class Resolve(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        chat_id = (input.get('chat_id') or '').strip()
        action = (input.get('action') or 'resolve').strip()

        if not chat_id:
            return {'success': False, 'error': 'chat_id is required'}

        def _apply(st: dict) -> dict:
            now_iso = datetime.now(timezone.utc).isoformat()
            chats = st.get('chats', {})
            if action == 'dismiss':
                if chat_id in chats:
                    del chats[chat_id]
                    append_history(st, {
                        'chat_id': chat_id,
                        'action': 'dismissed',
                        'timestamp': now_iso,
                    })
                    return {'success': True, 'message': f'Chat {chat_id} removed from watch list'}
                return {'success': False, 'error': f'Chat {chat_id} not found in state'}
            if chat_id not in chats:
                return {'success': False, 'error': f'Chat {chat_id} not found in state'}
            update_chat(st, chat_id,
                status='awaiting_user',
                nudge_count=0,
                last_nudge_at='',
                last_classification='Intervention resolved by user',
            )
            append_history(st, {
                'chat_id': chat_id,
                'action': 'resolved',
                'timestamp': now_iso,
            })
            return {
                'success': True,
                'chat_id': chat_id,
                'message': 'Intervention resolved, nudge count reset',
            }

        # v1.18.0 (P7): dismiss/resolve mutate under the shared state lock;
        # unknown-chat paths change nothing, so the signature gate keeps
        # them write-free.
        return await asyncio.to_thread(state_mod.state_transaction_apply, _apply)
