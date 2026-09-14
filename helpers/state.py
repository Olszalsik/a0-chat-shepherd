from __future__ import annotations

import json
import os

import threading
from datetime import datetime, timezone
from typing import Any

from helpers import files


from usr.plugins.chat_shepherd.helpers.constants import (
    STATE_FILE,
    HISTORY_LIMIT,
    MAX_TRACKED_CHATS,
    JOURNAL_FILE,
    JOURNAL_MAX_BYTES,
    JOURNAL_KEEP,
    JOURNAL_READ_LIMIT,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_state() -> dict[str, Any]:
    return {'chats': {}, 'history': [], 'last_tick': ''}



def load_state() -> dict[str, Any]:
    path = files.get_abs_path(STATE_FILE)
    payload = None
    if files.exists(path):
        try:
            payload = json.loads(files.read_file(path))
        except Exception:
            payload = None
    state = default_state()
    if isinstance(payload, dict):
        state.update(payload)
    if not isinstance(state.get('chats'), dict):
        state['chats'] = {}
    # v1.6.0: one-time migration - seed the journal from legacy history.
    legacy = state.get('history')
    if isinstance(legacy, list) and legacy:
        # v1.6.0 fix: probe the journal directly - files.exists() staleness
        # could re-seed legacy history and duplicate entries.
        try:
            os.path.getsize(_journal_abs())
        except OSError:
            _seed_journal(legacy)
    # v1.6.0: history of record is the append-only journal (newest first).
    state['history'] = read_journal(JOURNAL_READ_LIMIT)
    return state



def save_state(state: dict[str, Any]) -> None:
    # Atomic replace: a crash mid-write must never truncate state.json.
    # v1.6.0: history no longer round-trips through state.json; the
    # append-only journal is its source of truth.
    payload = dict(state)
    payload.pop('history', None)
    tmp_rel = STATE_FILE + '.tmp'
    files.write_file(tmp_rel, json.dumps(payload, ensure_ascii=False, indent=2))
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
 # v1.10.0 liveness probe: log mutation counter + wedge verdict.
 'last_updates_len': -1,
 'liveness': '',
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
    # v1.6.0: durable append to the journal first, then mirror into the
    # in-memory state so same-tick readers see the entry.
    append_journal(item)
    history = state.setdefault('history', [])
    history.insert(0, item)
    state['history'] = history[:HISTORY_LIMIT]




# v1.6.0: append-only history journal -------------------------------
_journal_lock = threading.RLock()

def _journal_abs() -> str:
    return files.get_abs_path(JOURNAL_FILE)

def append_journal(item) -> None:
    # One locked line-append: a crash can lose at most the line being
    # written, never the whole history. Oversize triggers compaction.
    line = json.dumps(item, ensure_ascii=False) + chr(10)
    with _journal_lock:
        p = _journal_abs()
        try:
            # v1.6.0 fix: no files.exists() here - its stat cache can hold a
            # stale False for a file created moments ago (9p guard TTL 60s).
            # Direct size probe; a missing file simply means size 0.
            try:
                jsize = os.path.getsize(p)
            except OSError:
                jsize = 0
            if jsize > JOURNAL_MAX_BYTES:
                # v1.6.0 fix: compaction is best-effort - its failure must
                # never swallow the append below it.
                try:
                    _compact_journal_locked(p)
                except Exception as _ce:
                    print('[chat_shepherd] journal compaction failed: ' + repr(_ce))
            with open(p, 'a', encoding='utf-8') as f:
                f.write(line)
        except Exception as e:
            print('[chat_shepherd] journal append failed: ' + repr(e))

def read_journal(limit=200):
    items = []
    with _journal_lock:
        p = _journal_abs()
        # v1.6.0 fix: open directly - files.exists() stat cache can hold a
        # stale False for a freshly created journal, silently returning
        # empty history. FileNotFoundError simply means no journal yet.
        try:
            with open(p, 'r', encoding='utf-8') as f:
                raw = f.read()
        except FileNotFoundError:
            return items
        except Exception as e:
            print('[chat_shepherd] journal read failed: ' + repr(e))
            return items
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        if isinstance(obj, dict):
            items.append(obj)
    if limit and len(items) > limit:
        items = items[-limit:]
    items.reverse()
    return items

def _seed_journal(legacy) -> None:
    # Legacy state.json history is newest-first; the journal is append
    # order (oldest first), so write it reversed. One-time migration.
    try:
        lines = [json.dumps(h, ensure_ascii=False) for h in reversed(legacy) if isinstance(h, dict)]
        if not lines:
            return
        with open(_journal_abs(), 'a', encoding='utf-8') as f:
            f.write(chr(10).join(lines) + chr(10))
    except Exception as e:
        print('[chat_shepherd] journal migration failed: ' + repr(e))

def _compact_journal_locked(p: str) -> None:
    # v1.6.0 fix: rewrite in place (same inode). The previous tmp+os.replace
    # approach created a new inode per compaction, and on the Docker 9p bind
    # mount a following open() could still resolve the path to the stale
    # pre-replace inode, silently diverging appends/reads from the visible
    # file. Caller holds _journal_lock; best-effort (caller isolates errors).
    with open(p, 'r', encoding='utf-8') as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    tail = lines[-JOURNAL_KEEP:] if JOURNAL_KEEP else lines
    with open(p, 'w', encoding='utf-8') as f:
        f.write(chr(10).join(tail) + (chr(10) if tail else ''))

def compact_journal() -> None:
    with _journal_lock:
        p = _journal_abs()
        if not files.exists(p):
            return
        try:
            if os.path.getsize(p) > JOURNAL_MAX_BYTES:
                _compact_journal_locked(p)
        except Exception as e:
            print('[chat_shepherd] journal compaction failed: ' + repr(e))

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
