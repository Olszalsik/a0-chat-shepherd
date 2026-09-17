from __future__ import annotations

import hashlib
import json
import os
import time

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
    return {'chats': {}, 'history': [], 'drafts': [], 'last_tick': ''}



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
    if not isinstance(state.get('drafts'), list):
        state['drafts'] = []
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
    global _last_saved_sig
    _last_saved_sig = state_signature(state)
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
    global _state_last_save, _last_saved_sig
    _state_last_save = time.monotonic()
    _last_saved_sig = state_signature(payload)


def state_signature(state: dict[str, Any]) -> str:
    """v1.17.0: stable fingerprint of the durable payload save_state
    writes. Volatile per-tick bookkeeping is excluded (per-chat
    last_ticked, top-level last_tick, the throttle snapshot); every
    other change - status, counters, cooldown/wedge bookkeeping,
    drafts, adaptive overlay - changes the signature and forces an
    immediate save. Serialization errors return a unique sentinel so
    the caller treats the state as changed (fail-safe to writing)."""
    try:
        chats = {}
        for cid, entry in (state.get('chats') or {}).items():
            if isinstance(entry, dict):
                chats[cid] = {k: v for k, v in entry.items() if k != 'last_ticked'}
            else:
                chats[cid] = entry
        basis = {
            'chats': chats,
            'drafts': state.get('drafts') if isinstance(state.get('drafts'), list) else [],
            'adaptive': state.get('adaptive'),
        }
        blob = json.dumps(basis, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode('utf-8')).hexdigest()
    except Exception:
        return 'err:%d' % time.time_ns()


def last_saved_signature() -> str:
    return _last_saved_sig


# v1.18.0 (P7): serialized read-modify-write. The tick and the API
# handlers each ran load_state -> mutate -> save_state unlocked, so
# a manual action landing mid-tick could lose tick updates. One
# module RLock serializes them; the name ends in lock so hotreload
# lock preservation keeps one object across re-exec.
_state_lock = threading.RLock()

def state_lock():
    # Reentrant state RMW lock; use directly as a context manager.
    return _state_lock

def state_transaction_apply(apply_fn):
    # Run apply_fn(fresh_state) under the state lock; persist only on
    # durable-signature change (early returns stay write-free). Sync
    # by design: async handlers call it via asyncio.to_thread so the
    # event loop never blocks on the tick lock hold. Exceptions
    # propagate WITHOUT saving - partial mutations are discarded.
    with _state_lock:
        state = load_state()
        sig_before = state_signature(state)
        result = apply_fn(state)
        changed = True
        try:
            changed = state_signature(state) != sig_before
        except Exception:
            pass
        if changed:
            save_state(state)
        return result


def save_due(min_interval_seconds: float) -> bool:
    """v1.17.0 durability gate: True when a full state.json write should
    happen now. interval <= 0 always reports due, and a freshly
    re-executed module (hot reload resets _state_last_save to 0.0)
    also reports due - fail-safe to writing."""
    try:
        interval = float(min_interval_seconds or 0)
    except Exception:
        interval = 0.0
    if interval <= 0:
        return True
    return (time.monotonic() - _state_last_save) >= interval


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
# v1.18.1: (mtime_ns, size)-keyed parse cache for read_journal(). The
# /status endpoint polls the journal every poll_seconds per open tab;
# re-reading + re-parsing the whole file each time is wasted I/O. Reset
# harmlessly on hot-reload; keyed by absolute path so tests that swap
# JOURNAL_FILE never see a foreign cache.
_journal_read_cache: dict[str, tuple[tuple[int, int], list]] = {}
_state_last_save = 0.0  # v1.17.0: monotonic ts of the last full state.json write
_last_saved_sig = ''  # v1.17.0: signature of the last written durable payload

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
    parsed: list = []
    with _journal_lock:
        p = _journal_abs()
        # v1.6.0 fix: open directly - files.exists() stat cache can hold a
        # stale False for a freshly created journal, silently returning
        # empty history. FileNotFoundError simply means no journal yet.
        try:
            st = os.stat(p)
            stat_key = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            _journal_read_cache.pop(p, None)
            return items
        except Exception as e:
            print('[chat_shepherd] journal stat failed: ' + repr(e))
            return items
        # v1.18.1: reuse the parsed lines while the file stat is unchanged.
        cached = _journal_read_cache.get(p)
        if cached is not None and cached[0] == stat_key:
            parsed = cached[1]
        else:
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    raw = f.read()
            except FileNotFoundError:
                _journal_read_cache.pop(p, None)
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
                    parsed.append(obj)
            _journal_read_cache[p] = (stat_key, parsed)
    items = parsed if (not limit or len(parsed) <= limit) else parsed[-limit:]
    items = list(items)
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



# v1.16.0: supervised resume drafts (one editable draft per chat).
DRAFT_MAX = 40
DRAFT_REQUEUE_MIN = 30


def _drafts_list(state: dict) -> list:
    drafts = state.get('drafts')
    return drafts if isinstance(drafts, list) else []


def add_draft(state: dict, chat_id: str, kind: str, reason: str = '', text: str = '') -> bool:
    from usr.plugins.chat_shepherd.helpers.resume_draft import draft_text, valid_kind

    kind = valid_kind(kind)
    if not kind or not chat_id:
        return False
    drafts = _drafts_list(state)
    for d in drafts:
        if isinstance(d, dict) and d.get('chat_id') == chat_id:
            return False
    entry = (state.get('chats') or {}).get(chat_id) or {}
    dismissed = str(entry.get('draft_dismissed_at') or '')
    if dismissed:
        try:
            ddt = datetime.fromisoformat(dismissed)
            if ddt.tzinfo is None:
                ddt = ddt.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - ddt).total_seconds() / 60.0
            if 0 <= age_min < DRAFT_REQUEUE_MIN:
                return False
        except Exception:
            pass
    if len(drafts) >= DRAFT_MAX:
        return False
    if not isinstance(state.get('drafts'), list):
        state['drafts'] = []
    import uuid
    state['drafts'].append({
        'id': uuid.uuid4().hex[:12],
        'chat_id': chat_id,
        'kind': kind,
        'text': str(text or '').strip() or draft_text(kind, reason),
        'reason': str(reason or ''),
        'created_at': _now_iso(),
    })
    return True


def remove_draft(state: dict, draft_id: str) -> bool:
    drafts = _drafts_list(state)
    keep = [d for d in drafts if not (isinstance(d, dict) and d.get('id') == draft_id)]
    if len(keep) == len(drafts):
        return False
    state['drafts'] = keep
    return True
