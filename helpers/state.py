from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from helpers import files

from usr.plugins.chat_shepherd.helpers.constants import (
    STATE_FILE,
    HISTORY_LIMIT,
    MAX_TRACKED_CHATS,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_state() -> dict[str, Any]:
    return {'chats': {}, 'history': [], 'last_tick': ''}


def load_state() -> dict[str, Any]:
    path = files.get_abs_path(STATE_FILE)
    if not files.exists(path):
        return default_state()
    try:
        payload = json.loads(files.read_file(path))
    except Exception:
        return default_state()
    state = default_state()
    if isinstance(payload, dict):
        state.update(payload)
    if not isinstance(state.get('chats'), dict):
        state['chats'] = {}
    if not isinstance(state.get('history'), list):
        state['history'] = []
    return state


def save_state(state: dict[str, Any]) -> None:
    # Atomic replace: a crash mid-write must never truncate state.json.
    tmp_rel = STATE_FILE + '.tmp'
    files.write_file(tmp_rel, json.dumps(state, ensure_ascii=False, indent=2))
    os.replace(files.get_abs_path(tmp_rel), files.get_abs_path(STATE_FILE))


def get_chat(state: dict, chat_id: str) -> dict[str, Any]:
    chats = state.setdefault('chats', {})
    entry = chats.get(chat_id)
    if entry is None:
        entry = {
            'chat_id': chat_id,
            'status': 'idle',
            'nudge_count': 0,
            'last_nudge_at': '',
            'last_seen_running': '',
            'last_classification': '',
            'last_log_type': '',
            'first_seen': _now_iso(),
            'last_ticked': '',
            'error_detected': False,
            # P3 wedge detection: log length at last tick + when it froze.
            'last_log_len': -1,
            'log_len_since': '',
            # R2 nudge-effectiveness counters (lifetime per chat).
            'nudges_sent': 0,
            'nudges_effective': 0,
            'last_nudge_outcome_at': '',
            # v1.2.0 wedge remediation ladder (budget is per wedge episode).
            'wedge_nudge_count': 0,
            'wedge_nudges_sent': 0,
            'wedge_nudges_effective': 0,
            'last_wedge_nudge_at': '',
            'last_wedge_outcome_at': '',
        }
        chats[chat_id] = entry
    return entry


def update_chat(state: dict, chat_id: str, **fields) -> dict:
    entry = get_chat(state, chat_id)
    entry.update(fields)
    return entry


def append_history(state: dict, item: dict[str, Any]) -> None:
    history = state.setdefault('history', [])
    history.insert(0, item)
    state['history'] = history[:HISTORY_LIMIT]


def cap_chats(state: dict, keep: int = MAX_TRACKED_CHATS) -> list[str]:
    """P1: cap the chats dict to the `keep` most recently ticked entries.

    Returns the chat_ids that were dropped (callers use this for history).
    Ties / missing timestamps fall back to insertion order (dicts preserve it).
    """
    chats = state.get('chats')
    if not isinstance(chats, dict) or len(chats) <= keep:
        return []
    ranked = sorted(
        chats.items(),
        key=lambda kv: kv[1].get('last_ticked', '') or '',
        reverse=True,
    )
    doomed = {chat_id for chat_id, _ in ranked[keep:]}
    for chat_id in doomed:
        chats.pop(chat_id, None)
    return sorted(doomed)
