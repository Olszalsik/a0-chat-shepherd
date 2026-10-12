from __future__ import annotations

import asyncio

from typing import Any

from helpers.api import ApiHandler, Request, Response

from usr.plugins.chat_shepherd.helpers import state as state_mod
from usr.plugins.chat_shepherd.helpers.state import get_chat, update_chat, append_history
from datetime import datetime, timezone


class Resolve(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        # v1.20.7: non-dict bodies (list / string / number) reached .get and
        # raised AttributeError → raw 500. Same guard api/draft.py applies.
        body = input if isinstance(input, dict) else {}
        # v1.21.0 (A3): `helpers/api.py:79-83` already normalizes a non-dict
        # BODY to `{}` before the handler runs, but a dict field VALUE still
        # arrives raw - `{"chat_id": 123}` raised AttributeError on `.strip()`
        # and became a bare 500 for a merely mistyped request. Coerce both.
        chat_id = str(body.get('chat_id') or '').strip()
        # v1.21.0 (A3): also lowercase + validate. `action` used to be matched
        # raw and anything that was not "dismiss" fell through to the resolve
        # branch, so a typo (`"dismiss "`, `"Dismiss"`, `"resolv"`) silently
        # cleared the chat's nudge budget instead of erroring.
        action = str(body.get('action') or 'resolve').strip().lower()

        if not chat_id:
            return {'success': False, 'error': 'chat_id is required'}
        if action not in ('resolve', 'dismiss'):
            return {
                'success': False,
                'error': f"unknown action '{action}' (expected 'resolve' or 'dismiss')",
            }

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
