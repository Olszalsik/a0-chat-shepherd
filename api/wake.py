"""chat_shepherd — manual wake.

Route: POST /api/plugins/chat_shepherd/wake
body {text?, force?}

Ported from the chat_shepherd2 donor endpoint (contract unchanged; imports
and the load path are v1's).

A manual wake bypasses the schedule and the active-hours window, but still
refuses to interrupt a busy target unless ``force`` is set. Queueing behind
live work is what separates a health check from a second agent fighting over
one chat.

The wake path itself lives in `helpers/wake.py` and is shared with the tick,
so a manual wake and a scheduled one behave identically.
"""

from __future__ import annotations

import asyncio

from helpers.api import ApiHandler, Request, Response

from usr.plugins.chat_shepherd.helpers import monitor, wake


class Wake(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        body = input if isinstance(input, dict) else {}
        cfg = monitor._load_config()
        text = str(body.get('text') or '').strip()
        if not text and not bool(cfg.get('continuity_enabled')):
            # Without the toggle on there is no default prompt, so say so
            # instead of firing an empty message that would look like a
            # successful wake.
            return {
                'success': False,
                'error': 'no text given and continuity is disabled',
                'hint': 'send {text: "..."} to wake with an explicit prompt',
            }
        result = await asyncio.to_thread(
            wake.fire_wake,
            cfg,
            prompt=text,
            manual=True,
            force=bool(body.get('force')),
        )
        return {'success': bool(result.get('success')), **result}