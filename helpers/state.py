from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from helpers import files

from usr.plugins.chat_shepherd.helpers.constants import STATE_FILE, HISTORY_LIMIT


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
    files.write_file(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2))


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
            'error_detected': False,
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
