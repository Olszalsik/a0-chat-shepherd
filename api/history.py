from __future__ import annotations

from helpers.api import ApiHandler, Request, Response

from usr.plugins.chat_shepherd.helpers.constants import CHAT_ID_PATTERN
from usr.plugins.chat_shepherd.helpers.state import read_journal
from usr.plugins.chat_shepherd.helpers import monitor

class History(ApiHandler):
    # Per-chat timeline: this chat's entries from the shared monitor
    # history (newest first), plus its human-readable display name.
    # v1.18.4 (P7): reads the journal with a chat_id filter directly -
    # the old load_state() path trimmed the GLOBAL 200-entry mirror
    # before filtering, so this chat's older entries silently vanished
    # on busy instances.
    async def process(self, input: dict, request: Request) -> dict | Response:
        body = input or {}
        chat_id = str(body.get('chat_id', '')).strip()
        # v1.20.0 (P8): enforce the framework chat-id pattern, not just a
        # length cap - the id is passed to _chat_display_name ->
        # _read_chat_title, which builds a usr/chats/<id>/chat.json path
        # from the raw value. POST + auth + CSRF already gate this
        # endpoint; the pattern check closes the path-shaping corner.
        if not chat_id or not CHAT_ID_PATTERN.match(chat_id):
            return {'success': False, 'error': 'chat_id required'}
        try:
            limit = int(body.get('limit', 30))
        except (TypeError, ValueError):
            limit = 30
        limit = max(1, min(50, limit))
        items = read_journal(limit, chat_id=chat_id)
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
