
from __future__ import annotations

from datetime import datetime, timezone

from helpers.api import ApiHandler, Request, Response

from usr.plugins.chat_shepherd.helpers.constants import PLUGIN_NAME, JOURNAL_KEEP
from usr.plugins.chat_shepherd.helpers.state import load_state, read_journal


class Export(ApiHandler):
    # v1.6.0: full JSON export - merged chat state plus the journal tail.
    async def process(self, input: dict, request: Request) -> dict | Response:
        state = load_state()
        return {
            'success': True,
            'plugin': PLUGIN_NAME,
            'exported_at': datetime.now(timezone.utc).isoformat(),
            'state': {
                'chats': state.get('chats', {}),
                'last_tick': state.get('last_tick', ''),
            },
            'history': read_journal(JOURNAL_KEEP),
        }