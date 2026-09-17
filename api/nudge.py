from __future__ import annotations

import asyncio

from typing import Any

from helpers.api import ApiHandler, Request, Response
from helpers import plugins

from usr.plugins.chat_shepherd.helpers.constants import PLUGIN_NAME, NUDGE_TEXT
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


class Nudge(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        chat_id = (input.get('chat_id') or '').strip()
        text = (input.get('text') or '').strip() or None

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

        try:
            msg = UserMessage(message=text or NUDGE_TEXT)
            ctx.communicate(msg)
        except Exception as e:
            return {'success': False, 'error': f'Nudge failed: {e}'}

        def _apply(st: dict) -> dict:
            now_iso = datetime.now(timezone.utc).isoformat()
            entry = get_chat(st, chat_id)
            new_count = entry.get('nudge_count', 0) + 1
            update_chat(st, chat_id,
                nudges_sent=entry.get('nudges_sent', 0) + 1,
                nudge_count=new_count,
                last_nudge_at=now_iso,
                status='nudged',
                last_classification=f'Manual nudge (attempt {new_count})',
            )
            append_history(st, {
                'chat_id': chat_id,
                'action': 'manual_nudge',
                'nudge_count': new_count,
                'timestamp': now_iso,
            })
            return {
                'success': True,
                'chat_id': chat_id,
                'nudge_count': new_count,
                'message': 'Nudge sent successfully',
            }

        # v1.18.0 (P7): serialized RMW under the shared state lock; the
        # communicate() above stays outside (audited cross-thread contract).
        return await asyncio.to_thread(state_mod.state_transaction_apply, _apply)
