"""chat_shepherd — manual release from `blocked`.

Route: POST /api/plugins/chat_shepherd/release
body {chat_id} | {chat_ids: [...], confirm: true}

Ported from the chat_shepherd2 donor endpoint (the shape, budgets and error
strings are carried verbatim; the imports and journal calls are v1's).

The whole point of the blocked state is that the ladder never releases it, so
the escape hatch must be a human action with an audit record. Without one, the
state is either unreachable (nothing can ever clear it) or self-clearing (and
therefore decorative). A fresh human turn also releases a block, but that is a
convenience — this endpoint is the deliberate one, and it journals the reason
it was previously blocked.

`chat_ids` exists because the supervisor can populate `blocked` in bulk and a
single-chat escape hatch makes clearing one cohort a thirty-call ritual. It is
deliberately an EXPLICIT list: there is no "release everything" form, because
`blocked` mixes held-budget endings with genuine security terminations and a
wildcard would sweep the second into the first.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from helpers.api import ApiHandler, Request, Response

from usr.plugins.chat_shepherd.helpers.constants import (
    CHAT_ID_PATTERN,
    STATUS_BLOCKED,
    STATUS_NUDGED,
)
from usr.plugins.chat_shepherd.helpers import state as state_mod

# A cohort is bounded by how many chats the supervisor tracks; anything larger
# is a mistake or an attack, and an unbounded loop over request input is a
# denial-of-service surface.
MAX_BULK_RELEASE = 500


def _valid_ids(raw: Any, single: Any) -> tuple[list[str], list[str]]:
    """Return (accepted ids, rejected descriptions)."""
    candidates: list[Any] = []
    if isinstance(single, str) and single.strip():
        candidates.append(single)
    if isinstance(raw, (list, tuple)):
        candidates.extend(raw)
    good: list[str] = []
    bad: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        text = str(item or '').strip()
        if not text:
            continue
        if not CHAT_ID_PATTERN.match(text):
            bad.append(f'invalid chat_id: {text!r}')
            continue
        if text in seen:
            continue
        seen.add(text)
        if len(good) >= MAX_BULK_RELEASE:
            bad.append('bulk release capped at %d ids' % MAX_BULK_RELEASE)
            break
        good.append(text)
    return good, bad


def _release_one(state: dict[str, Any], chat_id: str) -> dict[str, Any]:
    """Release one chat from `blocked`, journalling what it was blocked for.

    Extracted from the handler so the state transition is testable without an
    event loop or an HTTP request, and so both the single and bulk paths are
    provably the same code.
    """
    entry = (state.get('chats') or {}).get(chat_id)
    if not isinstance(entry, dict):
        return {'chat_id': chat_id, 'released': False,
                'reason': 'not a tracked chat'}
    if str(entry.get('status') or '') != STATUS_BLOCKED:
        return {'chat_id': chat_id, 'released': False, 'reason': 'not blocked'}
    previous = str(entry.get('blocked_reason') or '')
    entry['status'] = STATUS_NUDGED
    entry['blocked_reason'] = ''
    entry['blocked_at'] = ''
    entry['supervisor_settled'] = False
    # A manual release outranks an earlier one: the supervisor must not
    # re-block on the same verdict without new evidence. Without this the
    # release would be undone on the very next tick by an unchanged error.txt.
    entry['released_signature'] = str(entry.get('category_signature') or '')
    state_mod.append_history(state, {
        'chat_id': chat_id,
        'action': 'blocked_released',
        'detail': 'manual release',
        'previous_reason': previous[:200],
        'timestamp': datetime.now(timezone.utc).isoformat(),
    })
    return {'chat_id': chat_id, 'released': True, 'previous_reason': previous[:200]}


class Release(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        body = input if isinstance(input, dict) else {}
        ids, rejected = _valid_ids(body.get('chat_ids'), body.get('chat_id'))
        if not ids:
            # The single-chat error strings are part of the endpoint's
            # contract (the dashboard toasts them verbatim), so they are
            # preserved exactly rather than generalised.
            if rejected:
                return {'success': False, 'error': rejected[0]}
            return {'success': False, 'error': 'chat_id required'}
        bulk = len(ids) > 1
        if bulk and not body.get('confirm'):
            # A bulk release is a bulk human decision; require it to be
            # stated rather than inferred from a list a client built by
            # accident.
            return {
                'success': False,
                'error': 'releasing %d chats: resend with confirm=true' % len(ids),
                'would_release': ids[:20],
                'count': len(ids),
            }

        def _release(state: dict) -> list[dict[str, Any]]:
            return [_release_one(state, chat_id) for chat_id in ids]

        outcomes = await asyncio.to_thread(state_mod.state_transaction_apply, _release)
        if not bulk:
            # Single-chat response shape, unchanged.
            outcome = outcomes[0]
            result: dict[str, Any] = {
                'success': True,
                'chat_id': ids[0],
                'released': outcome.get('released') is True,
                'reason': outcome.get('reason') or '',
            }
            if outcome.get('previous_reason'):
                result['previous_reason'] = outcome['previous_reason']
            if rejected:
                result['rejected'] = rejected
            return result

        released = sum(1 for row in outcomes if row.get('released'))
        result = {
            'success': True,
            'requested': len(ids),
            'released': released,
            'failed': len(outcomes) - released,
            'outcomes': outcomes,
        }
        if rejected:
            result['rejected'] = rejected
        return result