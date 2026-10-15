"""chat_shepherd — acknowledge the durable alert stream.

Route: POST /api/plugins/chat_shepherd/ack
body: {} (acknowledges everything up to now)

v1.23.0: pages used to exist only as an in-memory 3-second toast wiped on
restart, so an operator returning after hours saw nothing. The tick now folds
every page into the durable state['alerts'] rows (monitor._fold_alert) and
/status reports them with an unread count. This endpoint is the counterpart of
that mechanism: it stamps state['alerts_seen_at'] with the current time, which
`_alerts_block` uses to compute `alerts_unread`. Without it, unread rows would
accumulate forever and the badge would stop meaning anything — an alarm that
never rests is as invisible as no alarm at all.

Journals `alerts_ack`, so the audit trail of "when did the human look" is in
the same timeline as the pages themselves.

NOTE: api/* handlers keep their from-import bindings until a plugin refresh
(AGENTS.md known limitation); state is reached through the module for exactly
that reason.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from helpers.api import ApiHandler, Request, Response

from usr.plugins.chat_shepherd.helpers import state as state_mod


def _ack(state: dict) -> str:
    stamp = datetime.now(timezone.utc).isoformat()
    state['alerts_seen_at'] = stamp
    state_mod.append_history(state, {
        'chat_id': '',
        'action': 'alerts_ack',
        'timestamp': stamp,
    })
    return stamp


class Ack(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        stamped = await asyncio.to_thread(state_mod.state_transaction_apply, _ack)
        return {'success': bool(stamped), 'acknowledged_at': str(stamped or '')}