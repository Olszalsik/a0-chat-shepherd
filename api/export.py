
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from helpers.api import ApiHandler, Request, Response

from usr.plugins.chat_shepherd.helpers.constants import PLUGIN_NAME, JOURNAL_KEEP
from usr.plugins.chat_shepherd.helpers.state import load_state, read_journal


def _snapshot() -> dict:
    """Sync body, off the event loop (v1.20.5).

    v1.20.5 NOTE - a rejected "optimization" is recorded here on purpose.
    This originally reused load_state()'s `history` mirror instead of calling
    read_journal() again, to avoid parsing the journal twice. That was wrong
    twice over: (1) read_journal is already cached by (mtime_ns, size), so the
    second call never re-parsed and the saving was zero; (2) load_state() fills
    the mirror with JOURNAL_READ_LIMIT (200) while this endpoint is a FULL
    export and must use JOURNAL_KEEP (1000), so the reuse would have silently
    truncated every export to 200 entries. Kept as JOURNAL_KEEP below.
    """
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


class Export(ApiHandler):
    # v1.6.0: full JSON export - merged chat state plus the journal tail.
    async def process(self, input: dict, request: Request) -> dict | Response:
        return await asyncio.to_thread(_snapshot)