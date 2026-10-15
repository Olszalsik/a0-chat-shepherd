from __future__ import annotations

import hashlib
import json
import os
import time
import uuid

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


def _parse_iso(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value or ''))
    except Exception:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def default_state() -> dict[str, Any]:
    # v1.22.0 (stage 3): `selfcheck` is the supervisor-health stamp
    # (findings, verdict, clocks); `channels`/`capabilities` arrive with the
    # stage-4/5 ports and default empty until then.
    return {
        'chats': {},
        'history': [],
        'drafts': [],
        'last_tick': '',
        'selfcheck': {},
        # v1.22.0 (stage 4): write-ahead intents for remediation deliveries.
        'inflight': [],
        # v1.22.0 (stage 5): continuity bookkeeping, capability probes and
        # per-day action counters.
        'continuity': {},
        'capabilities': {},
        'daily': {},
    }


# v1.22.0 (stage 4, ported from chat_shepherd2): the write-ahead in-flight
# WAL for remediation deliveries. The order is load-bearing: record the
# intent, deliver, then clear it. An exception between the first and second
# step leaves a durable intent the NEXT tick reconciles — the only honest way
# to represent "the supervisor tried to act and died mid-action" (a mutable
# counter cannot be replayed and cannot be charged).


def begin_inflight(
    state: dict[str, Any], action: str, chat_id: str,
    tier: int = 1, detail: str = '',
) -> str:
    entry = {'id': uuid.uuid4().hex[:12], 'action': str(action or ''),
             'chat_id': str(chat_id or ''), 'tier': int(tier or 1),
             'detail': str(detail or '')[:200],
             'queued_at': _now_iso()}
    inflight = state.get('inflight')
    if not isinstance(inflight, list):
        inflight = []
        state['inflight'] = inflight
    inflight.append(entry)
    return entry['id']


def end_inflight(state: dict[str, Any], intent_id: str) -> None:
    inflight = state.get('inflight')
    if isinstance(inflight, list):
        state['inflight'] = [e for e in inflight
                             if isinstance(e, dict) and e.get('id') != intent_id]


def reconcile_inflight(
    state: dict[str, Any], timeout_minutes: float = 5.0,
) -> list[dict[str, Any]]:
    """Reap timed-out intents; never replays them.

    Returns the abandoned intents (the caller charges their budgets against
    the action each intent CLAIMED). A fresh, un-timed-out intent is left
    alone: a delivery running right now may still finish.
    """
    inflight = state.get('inflight')
    if not isinstance(inflight, list):
        state['inflight'] = []
        return []
    now = datetime.now(timezone.utc)
    kept: list[dict[str, Any]] = []
    abandoned: list[dict[str, Any]] = []
    for entry in inflight:
        if not isinstance(entry, dict):
            continue
        ts = _parse_iso(entry.get('queued_at', ''))
        age_min = 99999.0 if ts is None else (
            now - ts if ts.tzinfo else now - ts.replace(tzinfo=timezone.utc)
        ).total_seconds() / 60.0
        if age_min >= timeout_minutes:
            abandoned.append(entry)
        else:
            kept.append(entry)
    state['inflight'] = kept
    return abandoned


# v1.21.0 (A1): persisted per-chat counters are hand-editable state that
# survives plugin upgrades, so any of them can arrive as a string, None or a
# list. Every one of them is used in an int comparison or an int `+ 1`, and
# each unguarded site raised `TypeError` - the per-chat guard contains it, but
# the queues drained AFTER the per-chat loop (`nudges_sent + 1`,
# `wedge_nudges_sent + 1`) had no guard at all and killed the WHOLE tick for
# every chat. v1.20.2 fixed exactly one of these reads (inside classify_chat);
# the fix belongs at the load boundary, next to the non-dict entry repair
# below, so it cannot be forgotten at a new call site.
#   value -> int, clamped at 0; sentinel fields -> -1 when unusable.
COUNTER_FIELDS: tuple[str, ...] = (
    'nudge_count',
    'nudges_sent',
    'nudges_effective',
    'wedge_nudge_count',
    'wedge_nudges_sent',
    'wedge_nudges_effective',
    'goal_gate_count',
    'goal_gate_nudges_sent',
)
# -1 means "unknown" for these two; an unusable value must stay -1 so the
# wedge detector and the liveness probe take their legacy/unknown paths.
SENTINEL_COUNTER_FIELDS: tuple[str, ...] = ('last_log_len', 'last_updates_len')


def _as_int(value: Any, fallback: int) -> int:
    # v1.21.0 (cycle 2): this is the NEVER-RAISING seam every persisted counter
    # goes through, so it must also survive the two shapes `except (TypeError,
    # ValueError)` does not cover:
    #   - bool: accepted as int (a hand-edited `true` is a plausible mistake),
    #     but `int(True)` is fine so this is just the early-out;
    #   - float NaN / +-inf: `int(float('inf'))` raises **OverflowError**, not
    #     ValueError, which escaped and made `load_state()` itself fail - so a
    #     single `nudge_count: 1e400`-style value in state.json turned the
    #     repair into the very whole-tick crash it exists to prevent, on a path
    #     (`load_state`) that has NO exception guard. Caught by the cycle-2
    #     boundary probe.
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    # OverflowError is the load-bearing addition: `int(float('inf'))` and
    # `int(float('nan'))` raise OverflowError / ValueError respectively, and
    # the original pair-only handler let the former escape. No non-finite
    # filter is needed after that - every non-finite input raises inside int()
    # rather than returning a value - and a cycle-2 mutation that removed such a
    # filter proved that a filter there was unreachable.
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback


def _repair_counters(entry: dict[str, Any]) -> bool:
    """Coerce every counter of one chat entry in place. True when changed."""
    dirty = False
    for key in COUNTER_FIELDS:
        if key not in entry:
            continue
        want = max(0, _as_int(entry.get(key), 0))
        if entry.get(key) != want:
            entry[key] = want
            dirty = True
    for key in SENTINEL_COUNTER_FIELDS:
        if key not in entry:
            continue
        want = _as_int(entry.get(key), -1)
        if want < -1:
            want = -1
        if entry.get(key) != want:
            entry[key] = want
            dirty = True
    return dirty


def load_state() -> dict[str, Any]:
    # `global` is required: without it the baseline adoption at the end of
    # this function binds a function-local and the module global keeps its
    # stale value (silently disabling the quiet-tick save gate).
    global _last_saved_sig, _last_loaded_sig
    # v1.20.0 (P8): snapshot the save serial BEFORE reading the file; a
    # save landing during the read bumps it and the baseline adoption at
    # the end is skipped (all-or-nothing).
    serial_before = _save_serial
    path = files.get_abs_path(STATE_FILE)
    payload = None
    # v1.21.3: keep the exact bytes the payload was parsed from - they are the
    # identity of the cached fingerprint (see _state_signature_cached).
    raw = ''
    if files.exists(path):
        try:
            raw = files.read_file(path)
            payload = json.loads(raw)
        except Exception:
            payload = None
            raw = ''
    state = default_state()
    if isinstance(payload, dict):
        state.update(payload)
    if not isinstance(state.get('chats'), dict):
        state['chats'] = {}
    if not isinstance(state.get('inflight'), list):
        state['inflight'] = []
    # v1.20.2: repair non-dict chat entries at the load boundary. A hand-edited
    # or half-migrated state.json can hold `"chats": {"x": "oops"}`, and every
    # downstream consumer (classify_chat, the nudge-budget reset loop, /status)
    # assumes a dict - one such entry raised AttributeError and took down the
    # whole tick. Entries without usable bookkeeping are dropped here, once,
    # instead of guarded at a dozen call sites.
    _chats36 = state.get('chats') or {}
    _bad36 = [cid for cid, ent in _chats36.items() if not isinstance(ent, dict)]
    if _bad36:
        for cid in _bad36:
            _chats36.pop(cid, None)
        state['chats'] = _chats36
        state['repaired_entries'] = len(_bad36)
    # v1.21.0 (A1): same boundary, same reason - repair unusable counters in
    # every surviving entry. A repair is a durable change, so it must also be
    # visible to the save gate (state_signature) or the file would keep the
    # bad value until some unrelated transition forced a write.
    _repaired_counters = 0
    for _ent36 in _chats36.values():
        if isinstance(_ent36, dict) and _repair_counters(_ent36):
            _repaired_counters += 1
    if _repaired_counters:
        state['repaired_counters'] = _repaired_counters
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
    # v1.20.0 (P8): adopt the loaded payload as the saved baseline ONLY
    # when no save raced this read (serial_before was snapshotted at the
    # top, before the file read). /status calls load_state() from worker
    # threads on every poll; a save finishing mid-read used to clobber
    # _last_saved_sig with a stale signature, which could suppress one
    # due save (or force one redundant save) on the next tick.
    # v1.21.0 (A1): a load that REPAIRED something must NOT adopt the
    # repaired payload as the baseline - the file on disk still holds the bad
    # value, so adopting it would let the quiet-tick gate skip the one write
    # that would persist the repair. Leaving the old baseline forces exactly
    # that single save on the next tick, after which ticks are quiet again.
    sig = _state_signature_cached(state, raw)
    # v1.21.3: `sig` is the fingerprint of the bytes THIS call read, so it is
    # also the correct pre-apply baseline for a transaction - no second
    # serialize of an unchanged file, and no dependence on whether the baseline
    # adoption below is allowed (a repair, or a save that raced the read, both
    # decline it while `sig` stays valid).
    _last_loaded_sig = sig
    with _sig_lock:
            if _save_serial == serial_before and not (
                state.get('repaired_entries') or state.get('repaired_counters')
            ):
                _last_saved_sig = sig
    return state



def read_chat_entry(chat_id: str) -> dict[str, Any]:
    """Read ONE persisted chat entry, without the journal mirror.

    v1.20.4: guard checks (e.g. the durable `terminated_at` stamp) only need
    the entry's own fields. `load_state()` also reads and caches the journal,
    which is far heavier and would make a request handler pay for a file it
    does not use - the v1.18.1 event-loop work exists precisely because those
    reads used to block the loop. Read-only, never raises, never seeds.
    """
    try:
        path = files.get_abs_path(STATE_FILE)
        if not files.exists(path):
            return {}
        payload = json.loads(files.read_file(path))
        if not isinstance(payload, dict):
            return {}
        chats = payload.get('chats')
        if not isinstance(chats, dict):
            return {}
        entry = chats.get(chat_id)
        return entry if isinstance(entry, dict) else {}
    except Exception:
        return {}



def _atomic_replace(src_rel: str, dst_rel: str, attempts: int = 4) -> None:
    """Rename over the destination, retrying briefly on lock contention.

    v1.21.0: `os.replace` is atomic on Linux and never raises here, but on
    Windows (and on a network/9p mount) a concurrent reader - the /status poll
    reading state.json, an indexer, a virus scanner - can hold the destination
    or the temp file open and make the very first replace fail with
    PermissionError/EBUSY. A lost durable write is worse than a few
    milliseconds of waiting, so a short bounded retry turns a lost tick into a
    slightly later one. The temp file is left in place for the next attempt.
    """
    src = files.get_abs_path(src_rel)
    dst = files.get_abs_path(dst_rel)
    delay = 0.02
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            # PermissionError (Windows / network mount) is the common case and
            # is an OSError; anything else here is retried too, because the
            # alternative to a retry is losing the durable write entirely.
            last_exc = exc
            if attempt == attempts - 1:
                break
            time.sleep(delay)
            delay *= 2
    raise last_exc if last_exc else OSError('replace failed')


def save_state(state: dict[str, Any], signature: str | None = None) -> None:
    # Atomic replace: a crash mid-write must never truncate state.json.
    # v1.6.0: history no longer round-trips through state.json; the
    # append-only journal is its source of truth.
    # v1.21.0 (O4): compact separators instead of indent=2. state.json is
    # machine-managed (config.json is the hand-edited one) and the tick
    # rewrites the whole file on every durable transition; indent=2 inflated
    # a 298-entry file to ~295 KB, so every such write moved ~40% more bytes
    # than the payload needs across a 9p bind mount.
    payload = dict(state)
    payload.pop('history', None)
    # v1.20.2: transient load-time repair marker, never persisted.
    payload.pop('repaired_entries', None)
    # v1.21.0 (A1): same for the counter-repair marker.
    payload.pop('repaired_counters', None)
    tmp_rel = STATE_FILE + '.tmp'
    files.write_file(
        tmp_rel,
        json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
    )
    _atomic_replace(tmp_rel, STATE_FILE)
    global _state_last_save, _last_saved_sig, _save_serial
    # v1.20.0 (P8): the baseline bookkeeping (monotonic timestamp, save
    # serial, durable signature) is updated atomically under _sig_lock so
    # concurrent /status load_state() reads can never interleave it.
    # v1.21.0 (O3): callers that already computed the signature pass it in -
    # state_signature is a full json.dumps + sha256 of every chat entry, and
    # the transaction path used to compute it three times per save.
    with _sig_lock:
        _state_last_save = time.monotonic()
        _save_serial += 1
        _last_saved_sig = (
            signature if isinstance(signature, str) and signature
            else state_signature(payload)
        )


# v1.22.0 (stage 3): the per-tick mutable clock fields of the selfcheck
# stamp — excluded from the durable signature (they churn every tick and
# would defeat the quiet-tick save gate; see state_signature).
SELFCHECK_VOLATILE_KEYS = frozenset(
    ('last_at', 'since', 'paged_at', 'consecutive_tick_failures')
)


def selfcheck_signature(stamp: Any) -> dict[str, Any]:
    """Semantic (churn-free) view of the supervisor-health stamp."""
    if not isinstance(stamp, dict):
        return {}
    return {
        k: v for k, v in stamp.items() if k not in SELFCHECK_VOLATILE_KEYS
    }


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
            # v1.22.0 (stage 3): the supervisor-health stamp is durable
            # semantic state — a recovery tick reports its outage even when
            # nothing else in the chats changed, so it must force a save.
            # Only the SEMANTIC parts take part: the per-tick mutable clocks
            # (last_at, since, paged_at, the failure counter cleared after
            # every persist) churn every tick and would defeat the whole
            # point of the quiet-tick save gate (donor contract C9).
            'selfcheck': selfcheck_signature(state.get('selfcheck')),
            # v1.22.0 (stage 4): only the COUNT matters — the ids churn and
            # would defeat the whole point of the gate (donor contract).
            'inflight': len(state.get('inflight') or [])
            if isinstance(state.get('inflight'), list) else 0,
            # v1.22.0 (stage 4): channel health is durable semantic state —
            # the fold transaction must not be treated as a no-op.
            'channels': state.get('channels') if isinstance(
                state.get('channels'), dict) else {},
            # v1.23.1 (audit fix 1): the durable alert fold is semantic
            # state TOO — every page and every fold changes it, so without
            # it in the basis an ack-only transaction (alerts_seen_at) or a
            # fold-only save computed sig_after == sig_before and was
            # SILENTLY DROPPED: /ack returned success while state.json never
            # gained the stamp, and the unread badge never cleared (and the
            # ack was lost on restart). These only change on notify/ack —
            # eventful ticks save anyway — so including them does not churn
            # the quiet-tick gate.
            'alerts': state.get('alerts') if isinstance(state.get('alerts'), dict) else {},
            'alerts_seen_at': str(state.get('alerts_seen_at', '') or ''),
        }
        blob = json.dumps(basis, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode('utf-8')).hexdigest()
    except Exception:
        return 'err:%d' % time.time_ns()


def last_saved_signature() -> str:
    # v1.20.0 (P8): read under _sig_lock so a concurrent save_state never
    # hands out a half-updated baseline (timestamp vs signature mismatch).
    with _sig_lock:
        return _last_saved_sig


# v1.21.3: sha256(raw file bytes) -> signature of the payload those bytes
# produced.
# Why a CONTENT digest and not the (mtime_ns, size) stat key used for the
# journal cache: state.json is REWRITTEN IN WHOLE on every save, and the
# audit host proved the stat key is not an identity here. Two payloads of
# identical length rewritten inside the filesystem's timestamp granularity
# produce the same (mtime_ns, size) - which TEST30 hit immediately - and a
# stale fingerprint is far worse than a slow one: it silently disarms the
# quiet-tick save gate. A digest of the bytes we already read is an exact
# identity and, at ~1.5 ms against ~7 ms for the signature itself, still saves
# the bulk of the work.
# Bounded to one entry: only the most recent content can still be loaded.
_state_sig_cache: tuple[str, str] | None = None


def _state_signature_cached(state: dict[str, Any], raw: str) -> str:
    """Signature of `state`, reusing the cached one for identical content.

    `raw` must be the exact text the payload was parsed from ("" when the file
    is absent or unparseable - which is itself a distinct, valid identity).
    """
    global _state_sig_cache
    digest = hashlib.sha256(raw.encode('utf-8', 'replace')).hexdigest()
    cached = _state_sig_cache
    if cached is not None and cached[0] == digest:
        return cached[1]
    sig = state_signature(state)
    _state_sig_cache = (digest, sig)
    return sig


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
    # v1.21.0 (O3): the post-apply signature is computed once and handed to
    # save_state, which used to recompute the very same fingerprint again.
    with _state_lock:
        state = load_state()
        # v1.21.3: `_last_loaded_sig` is the fingerprint of the bytes THIS
        # load_state read, which is exactly the pre-apply baseline. Using the
        # conditional `_last_saved_sig` here instead would be wrong whenever
        # adoption was declined (a racing save, or a repair): that value then
        # describes different bytes, and the change detection below would
        # either skip a real save or force a pointless one.
        sig_before = _last_loaded_sig or state_signature(state)
        result = apply_fn(state)
        changed = True
        sig_after: str | None = None
        try:
            sig_after = state_signature(state)
            changed = sig_after != sig_before
        except Exception:
            pass
        if changed:
            save_state(state, sig_after)
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
            # v1.22.0 (stage 2): cause diagnosis + the blocked/hold bookkeeping.
            'category': 'none',
            'category_signature': '',
            'category_excerpt': '',
            'category_since': '',
            'blocked_reason': '',
            'blocked_at': '',
            'supervisor_settled': False,
            'released_signature': '',
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
# v1.18.1: parse cache for read_journal(). The /status endpoint polls the
# journal every poll_seconds per open tab; re-reading + re-parsing the whole
# file each time is wasted I/O. Reset harmlessly on hot-reload; keyed by
# absolute path so tests that swap JOURNAL_FILE never see a foreign cache.
# v1.21.0 (O1): the cache also carries the byte offset just past the last
# COMPLETE line, so a poll after an append parses only the appended tail. The
# old (mtime_ns, size) key alone invalidated the WHOLE parse on every single
# append - with a 379 KB journal and a 5 s poll that is a full re-parse per
# poll, forever.
#   (mtime_ns, size_at_read), offset_of_last_complete_line, parsed_lines, head
_journal_read_cache: dict[str, tuple[tuple[int, int], int, list, bytes]] = {}
# v1.21.0 (O1): head fingerprint proving the bytes BEFORE the offset are still
# the same bytes. Growth alone is NOT proof of an append: `_compact_journal`
# rewrites the file in place and the following append can leave the result
# LARGER than the pre-compaction file, which would make a tail read start in
# the middle of an unrelated line and return garbage. The journal head moves
# whenever a rewrite drops old entries, so comparing it separates "appended"
# from "rewritten". TEST8C covers the compaction case.
# Known and accepted: a journal smaller than this window changes its own
# fingerprint on every append, so a fresh install re-parses (trivially, it is
# a handful of bytes) until the file outgrows the window. Correctness never
# depends on the tail path.
_JOURNAL_HEAD_BYTES = 512
_state_last_save = 0.0  # v1.17.0: monotonic ts of the last full state.json write
_last_saved_sig = ''  # v1.17.0: signature of the last written durable payload
# v1.21.3: signature of the bytes the most recent load_state() actually read.
# Distinct from _last_saved_sig on purpose: adoption of that one is CONDITIONAL
# (skipped when a save raced the read, or when the load repaired something), and
# a transaction needs the fingerprint of what it actually loaded either way.
_last_loaded_sig = ''
# v1.20.0 (P8): serialized baseline bookkeeping. The name ends in lock so
# hotreload lock preservation keeps one object across re-exec; the save
# serial lets load_state() detect a save that raced its file read.
_sig_lock = threading.Lock()
_save_serial = 0

def _journal_abs() -> str:
    return files.get_abs_path(JOURNAL_FILE)


def _decode_journal_chunk(blob: bytes) -> list:
    """Parse journal lines out of raw bytes, skipping anything unparseable."""
    out: list = []
    for raw_line in blob.split(b'\n'):
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line.decode('utf-8'))
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _journal_scan(
    p: str, cached: tuple | None
) -> tuple[tuple[int, int], int, list, bytes]:
    """Return ((mtime_ns, size), offset, parsed, head) for the journal at `p`.

    v1.21.0 (O1/O8): three paths, cheapest first.
      1. nothing changed since the last scan - reuse the cache, no I/O at all.
         This is the steady-state /status poll.
      2. the file only GREW and its head fingerprint is unchanged - read and
         parse just the appended tail from the cached offset. Both conditions
         are required: compaction rewrites the file in place, so growth alone
         can mean "this is a different file body" and a tail read would start
         mid-line and return garbage (found by TEST8C).
      3. anything else (cold cache, head changed, truncation) - full re-read.

    Only COMPLETE lines are consumed and `offset` always points just past the
    last complete one, so a torn final line (a crash mid-append) is not
    counted yet and is re-read whole once its terminator arrives - which is why
    the incremental path can never split a line in half.

    The I/O deliberately runs OUTSIDE `_journal_lock`: the journal is
    append-only, a scan is idempotent, and holding the lock across a full parse
    used to stall every appender - including the tick, which holds
    `_state_lock` while appending, so a slow parse blocked API transactions
    too. The post-read `fstat` is authoritative, so an append racing the scan
    is picked up by the next call instead of being lost.
    """
    st = os.stat(p)
    stat_key = (st.st_mtime_ns, st.st_size)

    if cached is not None:
        c_key, c_off, c_parsed, c_head = cached
        if c_key == stat_key:
            return stat_key, c_off, c_parsed, c_head
        if stat_key[1] > c_key[1] >= c_off:
            with open(p, 'rb') as f:
                # Fresh handle: position 0 here, so this reads the head.
                head = f.read(_JOURNAL_HEAD_BYTES)
                if head == c_head:
                    f.seek(c_off)
                    blob = f.read()
                    st2 = os.fstat(f.fileno())
                    parsed = c_parsed + _decode_journal_chunk(blob)
                    return (
                        (st2.st_mtime_ns, st2.st_size),
                        st2.st_size,
                        parsed,
                        head,
                    )

    with open(p, 'rb') as f:
        blob = f.read()
        st2 = os.fstat(f.fileno())
    head = blob[:_JOURNAL_HEAD_BYTES]
    cut = blob.rfind(b'\n')
    if cut < 0:
        # not a single complete line yet
        return (st2.st_mtime_ns, st2.st_size), 0, [], head
    return (
        (st2.st_mtime_ns, st2.st_size),
        cut + 1,
        _decode_journal_chunk(blob[:cut + 1]),
        head,
    )


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
            # v1.21.0 (O1): the cache entry is deliberately KEPT here. It
            # carries the offset just past the last complete line, so the next
            # read sees a grown file and parses only this append (the tail
            # path) instead of the whole file. Popping it here - as an earlier
            # draft did - forced a full re-parse on the first poll after every
            # append, which is exactly the cost this cache exists to remove.
        except Exception as e:
            print('[chat_shepherd] journal append failed: ' + repr(e))

def read_journal(limit=200, chat_id=None):
    items = []
    p = _journal_abs()
    # v1.6.0 fix: open directly - files.exists() stat cache can hold a
    # stale False for a freshly created journal, silently returning
    # empty history. FileNotFoundError simply means no journal yet.
    try:
        stat_key, _offset, parsed, _head = _journal_scan(
            p, _journal_read_cache.get(p)
        )
    except FileNotFoundError:
        _journal_read_cache.pop(p, None)
        return items
    except Exception as e:
        print('[chat_shepherd] journal read failed: ' + repr(e))
        return items
    with _journal_lock:
        # Two polls racing is harmless (both scans are idempotent); publish
        # only if nothing newer already landed.
        cur = _journal_read_cache.get(p)
        if cur is None or cur[0] <= stat_key:
            _journal_read_cache[p] = (stat_key, _offset, parsed, _head)
    # v1.18.4 (P7): chat_id filters per-chat AFTER the full parse (cache
    # reuse unchanged) so per-chat reads trim the chat's own tail instead
    # of the global tail cap - api/history.py keeps this chat's older
    # entries on busy instances.
    if chat_id:
        selected = [obj for obj in parsed if obj.get('chat_id') == chat_id]
    else:
        selected = parsed
    # v1.21.0 (cycle 2): a non-positive or non-int limit now means "no tail
    # trimming" instead of accidentally trimming to 0 or to len-5. `limit=0`
    # used to fall through `len(selected) <= limit` only by luck (0 entries
    # matched), and a NEGATIVE limit produced `selected[5:]`, which returns
    # NOTHING for a short journal and everything-minus-five for a long one -
    # both silent data loss on a read path whose callers all pass a sane
    # positive limit today.
    try:
        cap = int(limit)
    except (TypeError, ValueError, OverflowError):
        cap = 0
    if cap > 0 and len(selected) > cap:
        items = selected[-cap:]
    else:
        items = list(selected)
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

def cap_chats(
    state: dict,
    keep: int = MAX_TRACKED_CHATS,
    protected: Any = None,
) -> list[str]:
    """Cap the tracked-chats dict: only DEAD entries (ids not in
    `protected`) are evicted, oldest last_ticked first, once the dead
    surplus exceeds `keep`.

    v1.20.0 (P8) live-aware fix: evicting a live chat wiped its
    nudge/cooldown/wedge bookkeeping, so on instances with more live
    contexts than the cap the same chats were evicted and re-created
    every tick - the cooldown and max_auto_nudges budget never engaged
    and stalled chats were re-nudged every ~2 minutes (73 auto-nudges
    observed on one chat). Live entries are now never dropped; `keep`
    bounds the dead surplus (persisted-only chats) instead of the total,
    which is exactly the growth the original P1 cap was guarding.

    Returns the chat_ids that were dropped (callers use this for history).
    Ties / missing timestamps fall back to insertion order (dicts preserve it).
    """
    chats = state.get('chats')
    if not isinstance(chats, dict):
        return []
    prot = set(protected) if protected else set()
    dead = [(cid, ent) for cid, ent in chats.items() if cid not in prot]
    if len(dead) <= keep:
        return []

    def _ticked(kv):
        ent = kv[1]
        return (ent.get('last_ticked', '') or '') if isinstance(ent, dict) else ''

    ranked = sorted(dead, key=_ticked, reverse=True)
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
