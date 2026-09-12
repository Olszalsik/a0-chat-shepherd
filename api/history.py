from __future__ import annotations

from helpers.api import ApiHandler, Request, Response

from usr.plugins.chat_shepherd.helpers.state import load_state
from usr.plugins.chat_shepherd.helpers import monitor


class History(ApiHandler):
    # Per-chat timeline: this chat's entries from the shared monitor
    # history (newest first), plus its human-readable display name.
    async def process(self, input: dict, request: Request) -> dict | Response:
        body = input or {}
        chat_id = str(body.get('chat_id', '')).strip()
        if not chat_id or len(chat_id) > 64:
            return {'success': False, 'error': 'chat_id required'}
        try:
            limit = int(body.get('limit', 30))
        except (TypeError, ValueError):
            limit = 30
        limit = max(1, min(50, limit))
        state = load_state()
        items = [
            h for h in state.get('history', [])
            if isinstance(h, dict) and h.get('chat_id') == chat_id
        ][:limit]
        try:
            name = monitor._chat_display_name(chat_id)
        except Exception:
            name = chat_id
        return {
            'success': True,
            'chat_id': chat_id,
            'name': name,
            'history': items,
        }
