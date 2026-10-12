from __future__ import annotations

import asyncio

from helpers.api import ApiHandler, Request, Response

from usr.plugins.chat_shepherd.helpers.constants import CHAT_ID_PATTERN
# v1.21.0 (A8): module access, not a pinned `from ...state import
# read_journal` - that binding survived a `helpers/state.py` hot-reload, so
# /history served the pre-patch read path until a plugin refresh.
from usr.plugins.chat_shepherd.helpers import state as state_mod
from usr.plugins.chat_shepherd.helpers import monitor


def _build(chat_id: str, limit: int) -> dict:
    """Sync body: the journal parse and the chat.json title probe both touch
    disk, so the whole thing runs off the event loop (v1.20.5; the same rule
    v1.18.1 applied to /status and /draft)."""
    items = state_mod.read_journal(limit, chat_id=chat_id)
    try:
        name = monitor._chat_display_name(chat_id)
    except Exception:
        name = chat_id
    return {'success': True, 'chat_id': chat_id, 'name': name, 'history': items}


class History(ApiHandler):
    # Per-chat timeline: this chat's entries from the shared monitor
    # history (newest first), plus its human-readable display name.
    # v1.18.4 (P7): reads the journal with a chat_id filter directly -
    # the old load_state() path trimmed the GLOBAL 200-entry mirror
    # before filtering, so this chat's older entries silently vanished
    # on busy instances.
    async def process(self, input: dict, request: Request) -> dict | Response:
        # v1.21.0 (cycle 2): the framework normalizes a non-dict body to `{}`
        # before the handler runs, so this is defence in depth for a direct
        # call - but the other three endpoints added exactly this guard in
        # v1.20.7 and /history was left out, so a list/string/number body
        # still raised AttributeError on `.get`.
        body = input if isinstance(input, dict) else {}
        chat_id = str(body.get('chat_id', '')).strip()
        # v1.20.0 (P8): enforce the framework chat-id pattern, not just a
        # length cap - the id is passed to _chat_display_name ->
        # _read_chat_title, which builds a usr/chats/<id>/chat.json path
        # from the raw value. POST + auth + CSRF already gate this
        # endpoint; the pattern check closes the path-shaping corner.
        if not chat_id:
            return {'success': False, 'error': 'chat_id required'}
        if not CHAT_ID_PATTERN.match(chat_id):
            # v1.20.5: this used to also say "chat_id required", which sent the
            # caller looking for a missing field when it had sent a bad one.
            return {'success': False, 'error': f'invalid chat_id: {chat_id!r}'}
        try:
            limit = int(body.get('limit', 30))
        except (TypeError, ValueError, OverflowError):
            # v1.21.0 (cycle 2): OverflowError joins the pair. A JSON body may
            # legally carry `Infinity` (Python's json.loads accepts it), and
            # `int(float('inf'))` raises OverflowError - which the old
            # two-arm handler let escape as a bare 500. Same hole as the
            # `_as_int` seams, one layer out.
            limit = 30
        # v1.21.0 (cycle 2): clamp BEFORE the range check can be bypassed by a
        # non-finite value. NaN compares False against everything, so the old
        # `max(1, min(50, nan))` returned 1 by accident rather than by intent;
        # coercing the clamp result makes every outcome explicit.
        limit = max(1, min(50, int(limit) if limit == limit else 30))
        return await asyncio.to_thread(_build, chat_id, limit)
