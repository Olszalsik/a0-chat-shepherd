from __future__ import annotations

import glob
import os
import threading
import time
import json
import re
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

from helpers import files
from agent import AgentContext, AgentContextType

from usr.plugins.chat_shepherd.helpers.constants import (
    STATUS_RUNNING,
    STATUS_STALLED,
    STATUS_NUDGED,
    STATUS_INTERVENTION,
    STATUS_INTERRUPTED,
    STATUS_AWAITING_USER,
    STATUS_ERROR,
    STATUS_PAUSED,
    STATUS_IDLE,
    STATUS_BLOCKED,
    NUDGE_TEXT,
    RESUME_TEXT,
    WEDGE_NUDGE_TEXT,
    WEDGE_NUDGE_AFTER_MIN,
    WEDGE_MAX_REMEDIATIONS,
    WEDGE_LIVENESS_PROBE,
    WEDGE_REMEDIATION_COOLDOWN_MIN,
    CATEGORY_PROVIDER_EXHAUSTION,
    EXHAUSTION_REASON_PREFIX,
    NO_RESPONSE_REASON_PREFIX,
    LIVENESS_SPINNING_SOFT,
    LIVENESS_STREAMING,
    STALL_RESTART_REASON,
    EXHAUSTION_HOLD_MIN,
    ALERTS_MAX,
    PLUGIN_NAME,
    MAX_TRACKED_CHATS,
    CHAT_ID_PATTERN,
    NOTIFY_AFTER_MIN,
    NOTIFY_REARM_MIN,
    GOAL_GATE_TEXT,
    GOAL_GATE_MAX_NUDGES,
    GOAL_GATE_COOLDOWN_MIN,
    GOAL_GATE_OBJECTIVE_MAX,
    NUDGE_EFFECTIVE_WINDOW_MIN,
)
from usr.plugins.chat_shepherd.helpers import state as state_mod
from usr.plugins.chat_shepherd.helpers import adaptive as adaptive_mod
from usr.plugins.chat_shepherd.helpers import diagnose as diag_mod
from usr.plugins.chat_shepherd.helpers import planner as planner_mod
from usr.plugins.chat_shepherd.helpers import selfcheck as selfcheck_mod
from usr.plugins.chat_shepherd.helpers import notify as notify_mod
from usr.plugins.chat_shepherd.helpers import wake as wake_mod
from usr.plugins.chat_shepherd.helpers import capabilities as caps_mod
from usr.plugins.chat_shepherd.helpers import clock as clock_mod

# v1.22.0 (stage 1, ported from chat_shepherd2's 2026-10-09 fix): the
# wedged-completion reason classify_chat stamps, and the age parser for it.
HK_REASON_PREFIX = 'Response logged, but housekeeping never completed'
_HK_AGE = re.compile(r'%s \((\d+)m' % re.escape(HK_REASON_PREFIX))

# v1.22.0 (stage 4): which state counter a WAL intent's claimed action
# charges when the delivery turn out to have crashed (reconcile). The wedge
# ladder (wedge_*) charges wedge_nudge_count; everything else rides
# nudge_count.
class _SkipCharge(Exception):
    # Internal (v1.23.2 cycle 2): the reconcile phase raises it to skip a
    # budget charge for an extinct chat while still journaling the
    # abandonment row.
    pass


_BUDGET_KEY_WAL = {
    'restart_resume': 'nudge_count',
    'stall_nudge': 'nudge_count',
    'stall_restart': 'nudge_count',
    'exhaustion_kill': 'nudge_count',
    'wedge_auto_continue': 'wedge_nudge_count',
    'wedge_soft_restart': 'wedge_nudge_count',
}

# The job loop ticks roughly every 60s; a last_tick gap far beyond one
# tick interval means the server process (or at least the job loop) was
# restarted and the in-memory contexts were rebuilt from disk.
RESTART_GAP_MINUTES = 5.0

# v1.22.0 (stage 3, ported from chat_shepherd2): consecutive failed state
# writes, in this process. Deliberately a module global: when the blob cannot
# be written, a counter inside the blob is useless. Fed to the self-check so
# a disk that has stopped accepting writes becomes a visible finding instead
# of a supervisor that quietly stops learning anything.
_save_failures = 0

# v1.22.0 (stage 5): ticks completed since this process started the loop -
# the startup-grace window (`startup_grace_ticks`) reads this.
_TICKS_SINCE_START = 0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    # Accepts ISO strings AND datetime objects: context.last_message is a
    # datetime, and feeding one to fromisoformat raised TypeError, which
    # made every idle/stall computation report 9999 minutes (false stall
    # classifications and post-restart nudge storms). Naive datetimes
    # are assumed to be UTC.
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _as_float(val: Any, fallback: float) -> float:
    # v1.20.7 companion to _as_int for the timing knobs: monitor._load_config()
    # merges defaults but does NOT type-coerce (the sanitizer lives in the save
    # paths only), so a hand-edited config.json carrying e.g.
    # "nudge_cooldown_minutes": null raised float(None) at the top of
    # _tick_impl — outside the per-chat guard — and took down the WHOLE tick
    # every iteration (the extension only logged tick_fail to the debug log).
    # A bad value degrades to the shipped default instead.
    # v1.21.0 (cycle 3): OverflowError joins the pair, and non-finite results are
    # rejected. `float(10**400)` raises OverflowError (JSON permits
    # arbitrary-precision integer literals, so a hand-edited config can carry
    # one), and `float('inf')` / `float('nan')` SUCCEED - which is worse than
    # raising: an inf cooldown makes every chat instantly off-cooldown and an
    # inf stall threshold makes every chat instantly stalled, i.e. a hand-edited
    # value silently inverts the supervision policy instead of degrading.
    if isinstance(val, bool):
        val = int(val)
    try:
        out = float(val)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if out != out or out in (float('inf'), float('-inf')):
        return fallback
    return out


def _minutes_since(dt: datetime | None) -> float:
    if dt is None:
        return 9999.0
    return (_now() - dt).total_seconds() / 60.0


def _as_int(val: Any, fallback: int = 0) -> int:
    """Coerce a state field to int without ever raising.

    v1.20.2: state.json is hand-editable and survives plugin upgrades, so a
    counter can arrive as a string, None, or a list (`"nudge_count": "3"`).
    Comparing that against an int raised TypeError inside classify_chat and
    aborted the whole tick. Counters are best-effort bookkeeping, so a bad
    value degrades to the fallback instead of taking supervision down.
    v1.21.0 (cycle 2): NaN/inf now covered. `int(float('inf'))` raises
    OverflowError, which `except (TypeError, ValueError)` does not catch, so a
    single non-finite counter escaped this seam. On the load-boundary path that
    meant `load_state()` itself raised - with no guard around it - which is the
    same whole-tick blindness the seam exists to prevent. Keep this in step
    with `state._as_int`, which is the authority; this mirror exists for the
    drain arithmetic.
    """
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, int):
        return val
    # Cycle 2: kept deliberately minimal. `int()` itself rejects every
    # non-finite float (OverflowError for +-inf, ValueError for NaN), so the
    # OverflowError arm is the whole fix - a post-hoc non-finite filter on the
    # result is unreachable, and a cycle-2 mutation confirmed removing it
    # changed nothing.
    try:
        return int(val)
    except (TypeError, ValueError, OverflowError):
        return fallback


# v1.18.1: fs-probe caches. tick() and every /status poll stat-probed one
# path per tracked chat (usr/chats/<id> + error.txt) through files.exists,
# whose bounded stat can block the calling thread up to 5s on a degraded
# Docker 9p mount. With ~100 tracked chats that was 200+ blocked stats per
# poll. One scandir of usr/chats (parent mtime+size keyed, short TTL) and a
# short-TTL error.txt cache cut that to a couple of syscalls per poll.
_CHAT_DIR_BASE = 'usr/chats'


def _chat_dir(chat_id: str) -> str:
    """Absolute path of one persisted chat dir.

    v1.21.1: every runtime consumer now goes through this instead of
    interpolating `'usr/chats/' + chat_id` inline. `_CHAT_DIR_BASE` already
    existed for the scandir, but four sites bypassed it - and one of them was
    the FALLBACK inside `_has_chat_dir`, the exact probe the id-verdict of every
    nudged chat depends on. A single seam means the base can never drift
    between the listing and the per-path probe (they would then disagree, and
    `_has_chat_dir` returns False on exception, i.e. a chat silently stops
    being supervised). It also unblocks redirecting the base for tests without
    touching five call sites.
    """
    return files.get_abs_path(_CHAT_DIR_BASE + '/' + chat_id)


_chat_listing_cache: dict[str, tuple[tuple[int, int], float, frozenset]] = {}
_CHAT_LISTING_TTL_S = 2.0
_error_file_cache: dict[str, tuple[float, bool]] = {}
_ERROR_FILE_TTL_S = 3.0


def _chat_dir_ids() -> frozenset[str] | None:
    # Ids of the persisted chat dirs under usr/chats, from ONE scandir.
    # Returns None when the listing cannot be produced (caller falls back
    # to the per-path bounded stat, the v1.1.0 behavior).
    base = files.get_abs_path(_CHAT_DIR_BASE)
    now = time.monotonic()
    try:
        st = os.stat(base)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    cached = _chat_listing_cache.get(base)
    if cached and cached[0] == key and (now - cached[1]) < _CHAT_LISTING_TTL_S:
        return cached[2]
    try:
        with os.scandir(base) as it:
            ids = frozenset(e.name for e in it if e.is_dir())
    except OSError:
        return None
    _chat_listing_cache[base] = (key, now, ids)
    return ids


def _check_error_file(chat_id: str) -> bool:
    # Short-TTL cached error.txt probe (see the fs-probe cache note above).
    key = chat_id or ''
    now = time.monotonic()
    cached = _error_file_cache.get(key)
    if cached and (now - cached[0]) < _ERROR_FILE_TTL_S:
        return cached[1]
    result = False
    try:
        chat_dir = _chat_dir(chat_id)
        if not chat_dir:
            result = False
        else:
            error_path = os.path.join(chat_dir, 'error.txt')
            result = files.exists(error_path)
    except Exception:
        result = False
    _error_file_cache[key] = (now, result)
    return result


# v1.20.2: how many trailing log entries the tail scanners read. Every one of
# these helpers used to copy the ENTIRE log while holding the agent's log lock
# (blocking the agent's own writer) and then looked at the last handful of
# items. Slicing the tail under the lock keeps the scans identical on chats of
# any length. Generous enough to still see a fatal stop that a few shepherd
# nudges have pushed down the log.
_LOG_TAIL = 50


class _LogView:
    """One bounded, point-in-time snapshot of a chat's log (v1.21.0, O5).

    v1.20.2 bounded every scanner's copy but each scanner still took
    `context.log._lock` and re-copied the same tail - up to eight lock
    acquisitions and five copies per chat per tick (and again for every
    `/status` poll per open tab). Blocking the agent's own log writer is the
    real cost, so the tick and the status endpoint now take ONE snapshot per
    chat per pass and every scanner reads it.

    Fields: `length` (entry count, -1 unknown), `updates` (log mutation
    counter, -1 unknown), `tail` (newest-last slice of at most `_LOG_TAIL`
    entries), `last_type` (type of the very last entry, unfiltered).
    """

    __slots__ = ('length', 'updates', 'tail', 'last_type')

    def __init__(self, length: int = -1, updates: int = -1,
                 tail: list | None = None, last_type: str = '') -> None:
        self.length = length
        self.updates = updates
        self.tail = tail if tail is not None else []
        self.last_type = last_type


def _log_snapshot(context: AgentContext) -> _LogView:
    """Single lock acquisition + single bounded tail copy for one chat."""
    try:
        with context.log._lock:
            logs = context.log.logs
            tail = list(logs[-_LOG_TAIL:])
            last_type = (
                str(getattr(logs[-1], 'type', '') or '') if logs else ''
            )
            length = len(logs)
            # `updates` is the liveness-probe counter and is absent on some
            # log implementations. Reading it must NOT sink the whole snapshot:
            # -1 is the documented "unknown" value and simply disables the
            # probe for that chat, which is the correct degradation.
            updates = getattr(context.log, 'updates', None)
            updates_len = len(updates) if updates is not None else -1
            return _LogView(length, updates_len, tail, last_type)
    except Exception:
        return _LogView()


def _last_log_type(context: AgentContext, view: _LogView | None = None) -> str:
    # Reads a shared snapshot instead of `list(context.log.logs)` under the
    # lock: the tail is already sliced there, and taking the agent's log lock
    # eight times per chat per pass blocked its own writer for no reason.
    view = view if view is not None else _log_snapshot(context)
    try:
        for item in reversed(view.tail):
            if item.type in ('response', 'tool', 'user', 'error'):
                return item.type
    except Exception:
        pass
    return ''


def _last_log_fatal(context: AgentContext, view: _LogView | None = None) -> bool:
    # v1.18.1: True when the chat's recent log tail shows it stopped for a
    # reason a blind auto-nudge must not paper over - an error entry, or a
    # security-termination warning (e.g. the infection check logs
    # "Infection check: TERMINATED" before raising HandledException, which
    # the framework logs NOTHING for). Auto-nudging such a chat restarts the
    # very behavior that got it killed and churns safety checks.
    # Scans backwards until the first user/response item (a fresh human
    # turn or a completed response resets the verdict).
    # v1.18.2 fix (terminate-loop, part 2): this log-window scan alone is
    # NOT durable - a chat_shepherd nudge injects a `user` log item and
    # pushes the TERMINATED warning out of the window, re-arming the very
    # nudges the guard exists to prevent. Callers now ALSO persist a
    # `terminated_at` stamp in the state entry (see tick()) and suppress
    # stall nudges for TERMINATION_NUDGE_COOLDOWN_MIN after it; a fresh
    # human turn clears the stamp.
    view = view if view is not None else _log_snapshot(context)
    try:
        for item in reversed(view.tail):
            ltype = str(getattr(item, 'type', '') or '')
            if ltype == 'user' and '[chat_shepherd]' not in str(
                getattr(item, 'content', '') or ''
            ):
                return False
            if ltype == 'response':
                return False
            if ltype == 'error':
                return True
            if ltype == 'warning' and 'terminated' in str(
                getattr(item, 'heading', '') or ''
            ).lower():
                return True
    except Exception:
        pass
    return False


# v1.18.2 fix (terminate-loop, part 2): after a chat stops on a
# security termination / error, auto-nudges stay suppressed for this
# long (state-persisted, survives restarts). A fresh human message
# clears the suppression early — a human returning to the chat is an
# explicit signal to let it run again.
TERMINATION_NUDGE_COOLDOWN_MIN = 30.0


def _scan_log_for_termination(
    context: AgentContext, view: _LogView | None = None
) -> tuple[bool, bool]:
    """Scan the log tail for a security termination / error stop.

    Returns (found, fresh_human_turn):
    - found: the tail shows a TERMINATED warning or an error entry with no
      completed response / user turn after it.
    - fresh_human_turn: a user or response item appears after the fatal
      item, meaning the chat already moved on (clears the suppression).
    Unlike _last_log_fatal this is a full-tail scan (not just 5 items) so
    the stamp is stamped once, durably, even if later log items hide it.
    The scan is bounded to the last _LOG_TAIL entries (v1.20.2) and, since
    v1.21.0, reads the caller's shared snapshot, so a long-running chat
    neither copies its whole log nor takes the agent's log lock twice.
    """
    view = view if view is not None else _log_snapshot(context)
    try:
        found = False
        for item in reversed(view.tail):
            ltype = str(getattr(item, 'type', '') or '')
            if ltype == 'user' and '[chat_shepherd]' not in str(
                getattr(item, 'content', '') or ''
            ):
                # A human turn newer than any fatal item = the chat moved
                # on; older than a found fatal item = pre-termination
                # history, stop scanning.
                return found, not found
            if ltype == 'response':
                return found, not found
            if ltype == 'error':
                found = True
            elif ltype == 'warning' and 'terminated' in str(
                getattr(item, 'heading', '') or ''
            ).lower():
                found = True
        return found, False
    except Exception:
        return False, False


def _termination_cooldown_active(prev_entry: dict[str, Any]) -> bool:
    """True while the persisted termination suppression is still in force."""
    terminated_at = _parse_dt(prev_entry.get('terminated_at', ''))
    if terminated_at is None:
        return False
    return _minutes_since(terminated_at) < TERMINATION_NUDGE_COOLDOWN_MIN


def _last_entry_type(context: AgentContext, view: _LogView | None = None) -> str:
    """Type of the very last log entry (unfiltered)."""
    # v1.20.2: read the last entry in place; the full copy was pure waste.
    # v1.21.0: served from the shared snapshot, so it costs no lock at all.
    view = view if view is not None else _log_snapshot(context)
    return view.last_type


def _log_len(context: AgentContext, view: _LogView | None = None) -> int:
    """Number of log entries right now (-1 = unknown, disables wedge detection)."""
    view = view if view is not None else _log_snapshot(context)
    return view.length

def _log_updates(context: AgentContext, view: _LogView | None = None) -> int:
    # v1.10.0 liveness probe: mutation counter of the log next to the
    # entry count. Entries frozen + mutations advancing = spinning
    # dead loop; both frozen = hung call. -1 = unknown (legacy path).
    # v1.21.0: served from the shared snapshot, so it costs no lock at all.
    # NOTE: since the snapshot refactor the tick and `/status` read
    # `view.updates` DIRECTLY (they already hold the snapshot); this wrapper is
    # the standalone equivalent and is kept because it is the documented way to
    # read the counter for a chat whose snapshot you do not otherwise have.
    # Do not "optimize" the two call sites back to calling this - that would
    # re-introduce a second log lock per chat per pass (TEST45I).
    view = view if view is not None else _log_snapshot(context)
    return view.updates


def _has_chat_dir(chat_id: str) -> bool:
    """P1: a real UI chat persists its transcript under usr/chats/<id>.

    Two guards, both cheap:
    1. the id must match the framework's pattern (AgentContext.generate_id
       always yields exactly 8 alphanumerics) — script-created throwaway
       contexts ("verify-1788992102", "ctx-hook-1") pass an explicit id= and
       never match, even though message_async DOES persist their chat dir;
    2. the usr/chats/<id> directory must exist. v1.18.1: resolved from one
       cached scandir of usr/chats instead of one bounded stat per chat per
       call; falls back to the per-path files.exists probe when the listing
       is unavailable.
    """
    try:
        if not CHAT_ID_PATTERN.match(chat_id or ''):
            return False
        ids = _chat_dir_ids()
        if ids is not None:
            return chat_id in ids
        return bool(files.exists(_chat_dir(chat_id)))
    except Exception:
        return False


def _wedge_minutes(
    context: AgentContext,
    prev_entry: dict[str, Any],
    view: _LogView | None = None,
) -> float:
    """P3: frozen-log minutes for a context (mirror of tick()'s clock).

    Read-only companion for the live status endpoint; tick() owns the
    log_len_since mutation. v1.21.0: takes the caller's shared snapshot so
    one /status poll takes ONE log lock per chat, not two.
    """
    cur = _log_len(context, view)
    prev_len = prev_entry.get('last_log_len', -1)
    if not (
        context.is_running() and cur >= 0 and prev_len >= 0 and cur == prev_len
    ):
        return 0.0
    since = _parse_dt(prev_entry.get('log_len_since', ''))
    if since is None:
        return 0.0
    return _minutes_since(since)


def _load_config() -> dict[str, Any]:
    """Read the plugin config with the shipped defaults ALWAYS applied.

    v1.20.3: `config.json` is hand-editable and routinely partial (it only
    carries the keys someone actually changed). The `get_plugin_config` hook
    in `hooks.py` deep-merges DEFAULTS underneath it, but no runtime path
    should *depend* on that hook being installed and healthy - if it ever
    regressed, every unwritten key would silently fall back to a hard-coded
    `cfg.get(key, default)` that can drift from `config_defaults.DEFAULTS`
    (the exact hazard the hook was added to close). This seam re-applies the
    merge locally, so the two paths cannot disagree.
    """
    raw: Any = {}
    try:
        from helpers import plugins as _plugins

        raw = _plugins.get_plugin_config(PLUGIN_NAME) or {}
    except Exception:
        raw = {}
    try:
        from usr.plugins.chat_shepherd.helpers.config_defaults import (
            deep_merge_defaults,
        )

        return deep_merge_defaults(raw)
    except Exception:
        return dict(raw) if isinstance(raw, dict) else {}


# v1.21.0 (O9): the day whose retention pass already ran. Rotation is a
# directory glob + up to 7 unlinks; doing it on every log line (hot-reload
# events, per-chat tick failures, nudge delivery failures) meant a directory
# listing per line. One pass per UTC day is all retention ever needed - a new
# file can only appear with a new day stamp.
_DEBUG_LOG_ROTATED_DAY = ''


def _debug_log(kind: str, message: str) -> None:
    # R2: no more silently swallowed exceptions - kind-tagged,
    # daily-rotated debug log under the plugin data dir.
    global _DEBUG_LOG_ROTATED_DAY
    try:
        now = datetime.now(timezone.utc)
        day = now.strftime('%Y-%m-%d')
        rel = 'usr/plugins/chat_shepherd/data/debug-' + day + '.log'
        line = now.isoformat() + ' ' + kind + ' ' + str(message) + '\n'
        with open(files.get_abs_path(rel), 'a', encoding='utf-8') as f:
            f.write(line)
        # v1.17.1: keep only the newest 7 daily debug files.
        # v1.21.0 (O9): once per day, not once per line.
        if _DEBUG_LOG_ROTATED_DAY != day:
            try:
                old = sorted(glob.glob(files.get_abs_path('usr/plugins/chat_shepherd/data/debug-*.log')))[:-7]
                for pth in old:
                    try:
                        os.remove(pth)
                    except OSError:
                        pass
                _DEBUG_LOG_ROTATED_DAY = day
            except Exception:
                pass
    except Exception:
        pass


# v1.21.0 (O10): OrderedDict so it can be a real LRU (move_to_end on hit,
# popitem(last=False) on overflow) instead of a full flush at the cap.
_NAME_CACHE: 'OrderedDict[str, tuple[Any, str]]' = OrderedDict()
_NAME_CACHE_MAX = 120

def _read_chat_title(chat_id: str) -> str:
    # Human-readable chat title from usr/chats/<id>/chat.json.
    # The 'name' field sits in the first bytes, so a bounded head
    # read is enough - never parse the embedded context payload.
    # v1.21.1: resolved through `_chat_dir` so the base cannot drift from the
    # scandir in `_chat_dir_ids`.
    path = _chat_dir(chat_id) + '/chat.json'
    try:
        with open(path, 'rb') as f:
            head = f.read(4096).decode('utf-8', errors='replace')
        m = re.search(r'"name"\s*:\s*"((?:[^"\\]|\\.)*)"', head)
        if not m:
            return ''
        raw = m.group(1)
        try:
            return str(json.loads('"' + raw + '"'))
        except Exception:
            return raw
    except Exception:
        return ''

def _chat_display_name(chat_id: str, ctx: Any = None, prev: dict | None = None) -> str:
    # v1.4.0: resolve the chat title a human sees in the WebUI for
    # notifications. Precedence: live ctx.name, persisted state
    # name, cached chat.json lookup (mtime-keyed), then the raw id.
    name = ''
    if ctx is not None:
        name = str(getattr(ctx, 'name', '') or '')
    if not name and isinstance(prev, dict):
        name = str(prev.get('name', '') or '')
    if not name:
        key = None
        path = _chat_dir(chat_id) + '/chat.json'
        try:
            st = os.stat(path)
            key = (st.st_mtime, st.st_size)
        except OSError:
            key = None
        cached = _NAME_CACHE.get(chat_id)
        # v1.21.0 (O10): touch on hit so the dict is a true recency order.
        if cached is not None:
            _NAME_CACHE.move_to_end(chat_id)
        if key is not None and cached and cached[0] == key:
            name = cached[1]
        elif key is not None:
            name = _read_chat_title(chat_id)
            if chat_id in _NAME_CACHE:
                _NAME_CACHE.pop(chat_id, None)
            # v1.21.0 (O10): evict the LEAST recently used entry instead of
            # clearing the whole dict. This instance has ~345 chats against a
            # 120-entry cache, so the old full flush threw away every warm
            # entry at once and re-read up to 120 chat.json files in a burst -
            # a per-notification stat storm on the 9p mount. Name resolution
            # also became an LRU here: `move_to_end` on a hit, `popitem` on
            # overflow, so the working set survives.
            while len(_NAME_CACHE) >= _NAME_CACHE_MAX:
                _NAME_CACHE.popitem(last=False)
            _NAME_CACHE[chat_id] = (key, name)
    name = name.strip()[:80]
    return name if name else chat_id


def _redact_text(text: Any, *secrets: str) -> str:
    # v1.20.0 (P8): strip credential-bearing strings from error text
    # before it reaches the debug log or a caller. Telegram URLs embed
    # the bot token; webhook URLs may embed auth credentials.
    out = str(text or '')
    for s in secrets:
        s = str(s or '')
        if s and s in out:
            out = out.replace(s, '***')
    return out


def _post_json(url: str, payload: dict, timeout: float = 5.0) -> bool:
    # R3: minimal JSON POST for external alert channels. Returns
    # True on 2xx; kept separate so tests can monkeypatch it.
    # v1.20.7: redirects are refused. requests follows 3xx by default, and a
    # 307/308 preserves the POST body — a public webhook URL that redirects to
    # a private/link-local target would silently bypass the
    # validate_public_http_url SSRF guard (which only ever saw the original
    # URL). Legitimate webhook receivers answer in place; a 3xx is an error.
    import requests
    resp = requests.post(url, json=payload, timeout=timeout, allow_redirects=False)
    return 200 <= resp.status_code < 300


def _external_jobs(cfg: dict, kind: str, message: str, priority: str = 'normal',
                   skip: tuple = ()) -> list[tuple[str, str, dict]]:
    # v1.19.0: job-building seam extracted from _dispatch_external so the
    # /test_notify endpoint reuses the exact job list the real fan-out
    # sends (same SSRF validation + webhook_skip logging, same payloads).
    if not isinstance(cfg, dict):
        return []
    jobs: list[tuple[str, str, dict]] = []
    webhook_url = str(cfg.get('webhook_url') or '').strip()
    if 'webhook' in set(skip or ()):
        # v1.22.0 (stage 4): a degraded channel is routed around — the alert
        # goes through the still-alive legs instead of being pumped into the
        # dead one every tick.
        _debug_log('webhook_skip', 'channel degraded (health routing)')
        webhook_url = ''
    if webhook_url and not bool(cfg.get('webhook_allow_private', False)):
        try:
            from helpers.network import validate_public_http_url
            validate_public_http_url(webhook_url)
        except Exception as e:
            # v1.20.7: redact the URL. Webhook URLs routinely embed
            # credentials in the query string, and validation errors may quote
            # the URL back - the v1.20.0 redaction contract applies here too
            # (only _post_jobs followed it before).
            _debug_log('webhook_skip', 'URL rejected: ' + _redact_text(repr(e), webhook_url))
            webhook_url = ''
    if webhook_url:
        jobs.append((
            'webhook', webhook_url,
            {
                'plugin': 'chat_shepherd',
                'kind': kind,
                'priority': priority,
                'message': message,
            },
        ))
    token = str(cfg.get('telegram_bot_token') or '').strip()
    tg_chat = str(cfg.get('telegram_chat_id') or '').strip()
    if token and tg_chat and 'telegram' not in set(skip or ()):
        jobs.append((
            'telegram',
            'https://api.telegram.org/bot' + token + '/sendMessage',
            {'chat_id': tg_chat, 'text': '[Chat Shepherd/' + kind + '] ' + message},
        ))
    return jobs


def _post_jobs(jobs: list[tuple[str, str, dict]], message: str = '') -> dict[str, str | None]:
    # v1.19.0: synchronous multi-channel post returning per-channel results
    # (None = delivered, string = error). Failure logging matches the
    # original fan-out loop exactly (channel + '_fail' kinds, ' :: ' + msg).
    # v1.20.0 (P8): errors are redacted against the job's own URL before
    # they reach the debug log or a caller - Telegram URLs embed the bot
    # token and webhook URLs may embed auth credentials, and requests
    # exceptions print the full URL.
    results: dict[str, str | None] = {}
    for channel, url, payload in jobs:
        try:
            if not _post_json(url, payload):
                results[channel] = 'non-2xx response'
                _debug_log(channel + '_fail', 'non-2xx :: ' + message)
            else:
                results[channel] = None
        except Exception as e:
            results[channel] = _redact_text(repr(e), url)
            _debug_log(channel + '_fail', _redact_text(repr(e), url) + ' :: ' + message)
    return results


def _fold_channel_results(state: dict | None, results: dict[str, str | None]) -> None:
    # v1.22.0 (stage 4): fold one delivery round into durable channel health,
    # journaling channels that just went down. A stateless caller (tests via
    # sync=True without state) only posts.
    if not isinstance(state, dict):
        return
    try:
        _threshold = _load_config().get('notify_degraded_after_failures', 3)
    except Exception:
        _threshold = 3

    def _apply(st: dict) -> dict:
        newly = notify_mod.record_channels(st, results, threshold=_threshold)
        if newly:
            state_mod.append_history(st, {
                'chat_id': '',
                'action': 'notify_degraded',
                'channels': newly,
                'timestamp': datetime.now(timezone.utc).isoformat(),
            })
        return {}
    try:
        state_mod.state_transaction_apply(_apply)
    except Exception as e:
        _debug_log('channel_fold_fail', repr(e))


def _dispatch_external(cfg: dict, kind: str, message: str, priority: str = 'normal', sync: bool = False, state: dict | None = None, skip: tuple = ()) -> None:
    # R3: fan the alert out to optional external channels (generic
    # webhook + Telegram). URL validation happens on the calling
    # (tick) thread because it is cheap; the actual POST runs in a
    # daemon thread so the event-loop tick never blocks. sync=True
    # is for tests only.
    # v1.19.0: job building and posting live in _external_jobs /
    # _post_jobs so the /test_notify endpoint shares this code path.
    jobs = _external_jobs(cfg, kind, message, priority, skip=skip)
    if not jobs:
        return
    # v1.22.0 (stage 4): a configured channel that produced NO job was
    # refused (SSRF guard) or routed around as degraded — record a real
    # outcome instead of leaving it invisible forever (donor F7).
    _attempted = {j[0] for j in jobs}
    _refused = {
        name: 'rejected by the SSRF guard / degraded routing'
        for name in notify_mod.configured_channels(cfg, skip)
        if name not in _attempted
    }
    def _run() -> None:
        results = _post_jobs(jobs, message)
        results.update(_refused)
        _fold_channel_results(state, results)

    if sync:
        _run()
    else:
        threading.Thread(target=_run, daemon=True, name='chat_shepherd_alert').start()


def _notify_framework(kind: str, message: str, priority: str = 'normal') -> bool:
    # v1.19.0: in-framework notification leg of _notify, lifted verbatim
    # so the /test_notify endpoint can verify it in isolation. Returns
    # True when NotificationManager accepted the notification; failures
    # log notify_fail exactly like the original inline block.
    # v1.23.0: display_time=15 — the default is a 3-second toast, which is
    # how an operator who walked away never sees the page they are being
    # paged about.
    try:
        from helpers.notification import (
            NotificationManager,
            NotificationPriority,
            NotificationType,
        )
        ntype = NotificationType.INFO
        if kind == 'warning':
            ntype = NotificationType.WARNING
        elif kind == 'error':
            ntype = NotificationType.ERROR
        npri = (
            NotificationPriority.HIGH if priority == 'high' else NotificationPriority.NORMAL
        )
        NotificationManager.send_notification(
            ntype, npri, message, title='Chat Shepherd', display_time=15
        )
        return True
    except Exception as e:
        _debug_log('notify_fail', repr(e) + ' :: ' + message)
        return False


def _fold_alert(state: dict, chat_id: str, kind: str, message: str) -> None:
    # v1.23.0: durable alert fold. The in-framework notification is an
    # in-memory 3-second toast wiped on restart (helpers/notification.py) and
    # visible only to a connected browser, so an operator returning after
    # hours sees NOTHING — on the live incident 8 chats sat in intervention
    # invisibly. Every page is folded into state['alerts'] keyed chat_id|kind:
    # re-pages of the same episode UPDATE the row's count instead of stacking.
    # Capped at ALERTS_MAX (oldest last_at dropped). Journals nothing: the
    # fold IS the record, and unknown top-level state keys round-trip through
    # load/save and force an immediate save via the state signature.
    try:
        alerts = state.get('alerts')
        if not isinstance(alerts, dict):
            alerts = {}
            state['alerts'] = alerts
        key = f'{chat_id or ""}|{kind or ""}'
        now_iso = datetime.now(timezone.utc).isoformat()
        row = alerts.get(key)
        if isinstance(row, dict):
            row['message'] = str(message)[:500]
            row['last_at'] = now_iso
            row['count'] = _as_int(row.get('count', 0)) + 1
        else:
            alerts[key] = {
                'chat_id': chat_id or '',
                'kind': kind or '',
                'message': str(message)[:500],
                'first_at': now_iso,
                'last_at': now_iso,
                'count': 1,
            }
        excess = len(alerts) - ALERTS_MAX
        if excess > 0:
            oldest = sorted(
                alerts.keys(),
                key=lambda k: str(alerts[k].get('last_at', ''))
                if isinstance(alerts[k], dict) else '',
            )
            for k in oldest[:excess]:
                alerts.pop(k, None)
    except Exception as e:
        _debug_log('alert_fold_fail', repr(e))


def _notify(kind: str, message: str, priority: str = 'normal', cfg: dict | None = None,
            state: dict | None = None, chat_id: str = '') -> bool:
    # R2/R3: central notification helper. Returns True when the
    # in-framework notification was accepted; failures are logged,
    # never silently swallowed. R3: also fans out to optional
    # external channels (webhook/Telegram) without blocking.
    # v1.19.0: the framework leg moved verbatim to _notify_framework.
    # v1.23.0: pages are additionally folded into the durable
    # state['alerts'] rows (chat_id keys the fold when the caller has it).
    fw_ok = _notify_framework(kind, message, priority)
    try:
        # v1.23.0: pages are additionally folded into the durable
        # state['alerts'] rows (chat_id keys the fold when the caller has it).
        # v1.23.1 (audit fix 11): only warning/error fold — INFO pages
        # (restart-resume summaries) are notices, not cries for help, and
        # folding them let routine rows evict unacknowledged alarm rows at
        # the ALERTS_MAX cap.
        if isinstance(state, dict) and kind in ('warning', 'error'):
            _fold_alert(state, chat_id, kind, message)
        # v1.21.0 (A9): the fallback now goes through the v1.20.3 seam instead
        # of `plugins.get_plugin_config` directly. Calling it directly
        # violated the "no runtime path may call it directly" rule and, being
        # unreachable until now (both call sites pass cfg=cfg), was never
        # noticed: a raw partial read carries no `webhook_url` and no telegram
        # token, so a caller passing cfg=None would silently lose every
        # external alert while the in-app notification still succeeded.
        if not isinstance(cfg, dict):
            cfg = _load_config()
        # v1.22.0 (stage 4): degraded channels are routed around (health
        # state read when the caller passed it; the fold is what maintains it).
        skip = () if state is None else tuple(
            notify_mod.degraded_channels(
                state, cfg.get('notify_degraded_after_failures', 3))
        )
        _dispatch_external(cfg, kind, message, priority,
                           state=state, skip=skip)
    except Exception as e:
        _debug_log('alert_dispatch_fail', repr(e))
    return fw_ok


def _record_nudge_outcome(state: dict, chat_id: str, now_iso: str) -> None:
    # R2 nudge effectiveness: when a nudged chat reaches running or
    # awaiting_user within NUDGE_EFFECTIVE_WINDOW_MIN minutes of its
    # last nudge, credit that nudge as effective and log it.
    entry = state_mod.get_chat(state, chat_id)
    if _as_int(entry.get('nudge_count', 0)) <= 0 or not entry.get('last_nudge_at', ''):
        return
    nudged_at = _parse_dt(entry.get('last_nudge_at', ''))
    if nudged_at is None:
        return
    minutes = _minutes_since(nudged_at)
    if minutes > NUDGE_EFFECTIVE_WINDOW_MIN:
        return
    entry['nudges_effective'] = _as_int(entry.get('nudges_effective', 0)) + 1
    entry['last_nudge_outcome_at'] = now_iso
    entry['nudge_count'] = 0
    entry['last_nudge_at'] = ''
    state_mod.append_history(state, {
        'chat_id': chat_id,
        'action': 'nudge_effective',
        'minutes_after_nudge': round(minutes, 1),
        'timestamp': now_iso,
    })


def _record_wedge_outcome(state: dict, chat_id: str, now_iso: str) -> None:
    # v1.2.0 wedge ladder: when a remediated wedge recovers (log grows or the
    # turn ends) within NUDGE_EFFECTIVE_WINDOW_MIN minutes of the last wedge
    # remediation, credit it and clear the wedge budget for a fresh episode.
    entry = state_mod.get_chat(state, chat_id)
    if _as_int(entry.get('wedge_nudge_count', 0)) <= 0 or not entry.get('last_wedge_nudge_at', ''):
        return
    nudged_at = _parse_dt(entry.get('last_wedge_nudge_at', ''))
    if nudged_at is None:
        return
    minutes = _minutes_since(nudged_at)
    entry['wedge_nudge_count'] = 0
    entry['last_wedge_nudge_at'] = ''
    if minutes > NUDGE_EFFECTIVE_WINDOW_MIN:
        return
    entry['wedge_nudges_effective'] = _as_int(entry.get('wedge_nudges_effective', 0)) + 1
    entry['last_wedge_outcome_at'] = now_iso
    state_mod.append_history(state, {
        'chat_id': chat_id,
        'action': 'wedge_nudge_effective',
        'minutes_after_nudge': round(minutes, 1),
        'timestamp': now_iso,
    })


def _nudge_context(context: AgentContext, text: str | None = None, chat_id: str = '') -> bool:
    try:
        # P4 cross-thread contract (audited 2026-09-10): tick() runs on the
        # JobLoop EventLoopThread, but `context.communicate()` is safe to call
        # from any thread. If the target's task is alive it only sets
        # `agent.intervention` (a plain attribute write, GIL-atomic for this
        # purpose); otherwise it schedules the chain via DeferredTask ->
        # EventLoopThread.run_coroutine, which is `asyncio.
        # run_coroutine_threadsafe` onto the context's name-keyed shared loop
        # (helpers/defer.py) — the official cross-thread scheduling API. No
        # nudge ever executes inline on the JobLoop thread.
        # UserMessage lives in the framework's agent module, not helpers.messages.
        from agent import UserMessage
        msg = UserMessage(message=text or NUDGE_TEXT)
        context.communicate(msg)
        return True
    except Exception as e:
        # v1.18.5 (P7): the private non-rotating nudge_debug.log is retired -
        # failures flow through the shared kind-tagged rotating _debug_log
        # AND a journal row so delivery failures are visible in the dashboard
        # timeline, not only in a side file.
        _debug_log('auto_nudge_fail', repr(e) + (' chat=' + chat_id if chat_id else ''))
        try:
            state_mod.append_journal({
                'chat_id': chat_id or '',
                'action': 'auto_nudge_fail',
                'error': repr(e),
                'timestamp': datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass
        return False


def classify_chat(
    context: AgentContext | None,
    chat_id: str,
    cfg: dict[str, Any],
    prev_entry: dict[str, Any],
    frozen_minutes: float = 0.0,
    view: _LogView | None = None,
) -> tuple[str, str]:
    # v1.22.0 (stage 2): a blocked chat is terminal until a human acts — the
    # release check runs in tick() before classify, so by the time we get
    # here a still-blocked entry stays blocked regardless of how the log
    # looks now (the verdict that blocked it usually lives in error.txt,
    # which never expires).
    if str(prev_entry.get('status', '')) == STATUS_BLOCKED:
        return (
            STATUS_BLOCKED,
            str(
                prev_entry.get('blocked_reason')
                or 'blocked pending human release'
            ),
        )
    if context is None:
        if _check_error_file(chat_id):
            return STATUS_ERROR, 'Chat has error.txt but no active context'
        return STATUS_IDLE, 'No active context'

    if context.paused:
        return STATUS_PAUSED, 'Context is paused'

    if context.is_running():
        # v1.18.2 fix (terminate-loop, part 1): an in-flight infection check
        # blocks tool execution in `gate()` and freezes the agent's log for
        # the whole analysis (up to a full main-model clarification loop).
        # That freeze used to be misread as a stall/wedge and auto-nudged,
        # which restarted the very behavior the check was evaluating and
        # produced repeated "Infection check: TERMINATED" cascades. While
        # the check's progress string is live, the chat is RUNNING — never
        # stalled, never wedged, never nudged.
        try:
            _progress = str(getattr(context.log, 'progress', '') or '')
        except Exception:
            _progress = ''
        if _progress.startswith('Infection check:') and 'passed' not in _progress:
            return STATUS_RUNNING, f'Safety check in progress ({_progress})'

        # v1.23.0 provider-exhaustion retry loop: a running chat cycling
        # "All chat model candidates exhausted"/"Critical error occurred,
        # retrying..." markers (stamped by tick()) with no assistant response.
        # Specific beats general: checked BEFORE the frozen-clock wedge,
        # because the loop's warning entries ADVANCE the log and the wedge
        # clock never starts. Nudging a rate-limited chat is futile, so this
        # is intervention-for-a-human with the ladder suppressed downstream.
        #
        # v1.23.1 (audit A3b): the verdict requires the tail to STILL match
        # (exhaustion_last_matched fresh within stall_minutes). A stale
        # episode stamp must not hold the classification when the failure
        # changed shape underneath it — e.g. the storm ended and the chat is
        # now a genuinely hung stream/frozen wedge, which the wedge ladder
        # below (with its nudges/kill) is the right tool for; the exhaustion
        # arm would have suppressed every ladder action forever.
        _ex_since = str(prev_entry.get('exhaustion_since', '') or '')
        if _ex_since:
            _ex_age = _minutes_since(_parse_dt(_ex_since))
            _last_m = str(prev_entry.get('exhaustion_last_matched', '') or '')
            # Missing stamp = state written by a build without the freshness
            # gate (v1.23.0 state survives hot-reload into this build) — treat
            # as fresh rather than instantly unfreshening a live episode.
            _fresh = _last_m == '' or (
                _minutes_since(_parse_dt(_last_m))
                <= max(2.0, _as_float(cfg.get('stall_minutes', 5), 5.0) * 2.0)
            )
            if (
                _ex_age >= _as_float(cfg.get('exhaustion_minutes', 20), 20.0)
                and _fresh
            ):
                return STATUS_INTERVENTION, (
                    f'{EXHAUSTION_REASON_PREFIX} ({_ex_age:.0f}m, '
                    f'{_as_int(prev_entry.get("exhaustion_count", 0))} retry '
                    'markers, no assistant response)'
                )
        # v1.23.0 substantive-progress liveness: entries advancing but no NEW
        # assistant response for N minutes (0 = off) is `spinning_soft` — the
        # wedge ladder can drain it with a tier-1 continue (unlike the hard
        # spinning verdict, these code paths reach safe points).
        _nr_since = str(prev_entry.get('no_response_since', '') or '')
        if _nr_since and _as_float(cfg.get('no_response_minutes', 0), 0.0) > 0:
            _nr_age = _minutes_since(_parse_dt(_nr_since))
            if _nr_age >= _as_float(cfg.get('no_response_minutes', 0), 0.0):
                return STATUS_INTERVENTION, (
                    f'{NO_RESPONSE_REASON_PREFIX} {_nr_age:.0f}m '
                    '(entries advancing, no response)'
                )

        # P3 (v1.1.0), superseded in v1.2.0: frozen != stalled. A running
        # context whose log stopped growing for stall_minutes is wedged and
        # classified as intervention — but it is no longer left for a human
        # only: communicate() on an alive task sets agent.intervention (a
        # plain attribute write that drains at the agent's next safe point),
        # the same mechanics that make a manual "continue" unwedge these
        # chats. tick() runs the v1.2.0 remediation ladder on them.
        # v1.20.7: _as_float — a hand-edited non-numeric stall_minutes used to
        # raise inside classify_chat (per-chat guard caught it, but the chat
        # then fell through unsupervised for the tick).
        stall_minutes = _as_float(cfg.get('stall_minutes', 5), 5.0)
        if frozen_minutes >= stall_minutes:
            # v1.23.1 (audit fix 4): a live stream IS "running without new
            # entries" — the per-chunk in-place updates (liveness stamped
            # 'streaming' by tick()) must not be wedged, nudged, or killed:
            # the pre-v1.23.0 exemption cleared only the liveness field, so
            # the frozen arm still classified long streams intervention and
            # the ladder injected a tier-1 continue into a healthy reply.
            if str(prev_entry.get('liveness', '')) == LIVENESS_STREAMING:
                return STATUS_RUNNING, (
                    f'Streamed reply in progress ({frozen_minutes:.0f}m, no new entries)'
                )
            reason = (
                f'Agent appears wedged: running but log frozen for {frozen_minutes:.0f}m'
            )
            # v1.10.0 liveness probe: refine the classification when the
            # probe has a verdict (stamped by tick(); empty = unknown).
            live = str(prev_entry.get('liveness', ''))
            if live == 'spinning':
                reason += ' (liveness: spinning dead loop, only a task kill drains it)'
            elif live == 'hung':
                reason += ' (liveness: hung call)'
            wedge_count = _as_int(prev_entry.get('wedge_nudge_count', 0))
            max_remediations = max(0, min(5, _as_int(cfg.get(
                'wedge_max_remediations', WEDGE_MAX_REMEDIATIONS
            ), WEDGE_MAX_REMEDIATIONS)))
            if wedge_count > 0:
                if wedge_count >= max_remediations:
                    reason += ', remediation exhausted'
                else:
                    reason += (
                        f', auto-continue sent (attempt {wedge_count}'
                        f'/{max_remediations})'
                    )
            return STATUS_INTERVENTION, reason
        return STATUS_RUNNING, 'Agent is actively running'

    if _check_error_file(chat_id):
        return STATUS_ERROR, 'Chat has error.txt'

    # v1.23.0 post-kill hold: after the exhaustion kill the chat is stopped
    # with the failing retry tail still newest — which is also a fatal-log
    # tail by construction, so this MUST precede the v1.18.1 fatal guard.
    # Without the hold, the stall ladder would start nudging the chat back
    # into the exhausted provider and the tail's 429 fragments would classify
    # it auth (BLOCK). Held for a human until a human turn or a fresh
    # response arrives, or the window expires.
    _pk_at = str(prev_entry.get('provider_exhausted_at', '') or '')
    if _pk_at and bool(cfg.get('diagnose_enabled', True)):
        _pk_dt = _parse_dt(_pk_at)
        _pk_ts = _pk_dt.timestamp() if _pk_dt else 0.0
        _scan_view = view if view is not None else (
            _log_snapshot(context) if context is not None else None
        )
        _resp_ts = diag_mod.last_response_at(_scan_view)
        _hum_ts = diag_mod.last_human_user_at(_scan_view)
        _pk_cleared = bool(
            _pk_ts
            and ((_resp_ts and _resp_ts > _pk_ts) or (_hum_ts and _hum_ts > _pk_ts))
        )
        if (
            not _pk_cleared
            and _pk_dt
            and _minutes_since(_pk_dt) < EXHAUSTION_HOLD_MIN
        ):
            return STATUS_INTERVENTION, (
                f'{EXHAUSTION_REASON_PREFIX}: killed futile retry task, '
                'holding until human or provider window resets'
            )

    # v1.18.1: a chat that stopped on an error or a security termination
    # (e.g. infection check) is an error, not a stall — auto-nudging it
    # back to life just reproduces the termination.
    if _last_log_fatal(context, view):
        return (
            STATUS_ERROR,
            'Chat stopped by an error or security termination (no auto-nudge)',
        )
    # v1.18.2 fix (terminate-loop, part 2): the durable state stamp keeps
    # the error verdict alive even when later log items (e.g. chat_shepherd
    # nudges) bury the fatal entry. Cleared by a fresh human turn in tick().
    if _termination_cooldown_active(prev_entry):
        return (
            STATUS_ERROR,
            'Recent security termination/error; nudges suppressed for '
            f'{int(TERMINATION_NUDGE_COOLDOWN_MIN)}m',
        )

    last_log_type = _last_log_type(context, view)
    last_msg_dt = _parse_dt(getattr(context, 'last_message', ''))
    minutes_idle = _minutes_since(last_msg_dt)

    if last_log_type == '':
        return STATUS_IDLE, 'New chat, no messages yet'

    if last_log_type == 'response':
        # v1.22.0 (stage 1, ported from chat_shepherd2's 2026-10-09 fix):
        # a turn whose completion path died after logging the response leaves
        # an in-flight housekeeping marker ("Memorizing new information...")
        # as the newest log entry forever. Without this check the chat is
        # indistinguishable from a healthy finished chat - both end in a
        # response entry. A marker older than stall_minutes classifies as a
        # wedged completion (intervention) and routes into the wedge ladder;
        # a fresh marker is within grace and stays awaiting_user.
        if bool(cfg.get('diagnose_enabled', True)):
            hk_stall = _as_float(cfg.get('stall_minutes', 5), 5.0) * 60.0
            hk_heading, hk_age = diag_mod.pending_housekeeping(
                view if view is not None else _log_snapshot(context)
            )
            if hk_heading and hk_age >= hk_stall:
                return STATUS_INTERVENTION, (
                    f'{HK_REASON_PREFIX} ({hk_age / 60.0:.0f}m, marker: {hk_heading})'
                )
        return STATUS_AWAITING_USER, 'Agent finished response, waiting for user'

    stall_minutes = _as_float(cfg.get('stall_minutes', 5), 5.0)
    nudge_count = _as_int(prev_entry.get('nudge_count', 0))
    max_nudges = _as_int(cfg.get('max_auto_nudges', 3), 3)

    if minutes_idle > stall_minutes:
        if nudge_count >= max_nudges:
            # v1.23.0: escalation instead of giving up — exactly ONE soft
            # restart per stall episode (kill the dead task + framework nudge
            # prompt) before flipping to intervention. `ctx.nudge()`'s
            # kill_process is a no-op on an already-dead task, so this is
            # mechanically safe; budget 1 per episode, reset on recovery.
            if bool(cfg.get('stall_escalate_to_restart', False)) and _as_int(
                prev_entry.get('stall_restart_count', 0)
            ) < 1:
                return STATUS_NUDGED, STALL_RESTART_REASON
            if bool(cfg.get('intervention_after_failed_nudges', True)):
                return (
                    STATUS_INTERVENTION,
                    f'Stalled for {minutes_idle:.0f}m, exhausted {max_nudges} nudges',
                )
            return (
                STATUS_STALLED,
                f'Stalled for {minutes_idle:.0f}m, nudges exhausted (no escalation)',
            )
        if nudge_count > 0:
            return STATUS_STALLED, f'Stalled for {minutes_idle:.0f}m, nudge {nudge_count}/{max_nudges} used'
        return STATUS_STALLED, f'Stalled for {minutes_idle:.0f}m, no nudges yet'

    return STATUS_IDLE, f'Idle {minutes_idle:.0f}m, within grace period'


def _active_goal(chat_id: str) -> dict[str, Any] | None:
    # v1.8.0 goal completion gate: read the chat's native goal-system
    # goal. Returns it only when it exists and is still open
    # (active/paused); import or read failures are logged and silent so
    # the gate can never break the tick loop.
    try:
        from plugins._goal.tools.goal import get_goal, ACTIVE_STATUSES
        goal = get_goal(chat_id)
    except Exception as e:
        _debug_log('goal_gate_read_fail', chat_id + ' ' + repr(e))
        return None
    if not goal or goal.get('status') not in ACTIVE_STATUSES:
        return None
    return goal


def _last_response_text(context: AgentContext, view: _LogView | None = None) -> str:
    # v1.8.0: heading + content of the most recent response log item
    # (completion-claim evidence). v1.20.2: bounded tail slice under the lock
    # instead of copying the whole log; v1.21.0: reads the caller's shared
    # snapshot, so the goal gate adds no lock of its own.
    view = view if view is not None else _log_snapshot(context)
    try:
        for item in reversed(view.tail):
            if getattr(item, 'type', '') == 'response':
                heading = str(getattr(item, 'heading', '') or '')
                content = str(getattr(item, 'content', '') or '')
                return heading + '\n' + content
    except Exception:
        pass
    return ''


# v1.8.0: conservative completion-claim heuristic - subject word followed
# by a completion verb, global claims ("all done"), explicit first-person
# finishes, and bare "is complete". Windows containing negation or
# pending/remaining language are rejected.
_GOAL_GATE_RE = re.compile(
    r'\b(?:task|work|job|goal|implementation|refactor\w*|migration|setup|repair|'
    r'fix(?:es)?|plan|roadmap|build|port|update|upgrade|install(?:ation)?|'
    r'feature|module|script|tool|plugin|test(?:s)?)\b'
    r'[^.?!]{0,60}?\b(?:complete[ds]?|completion|finished|done|achieved|fulfilled)\b'
    r'|\b(?:all|everything)\b[^.?!]{0,40}?\b(?:complete[ds]?|finished|done)\b'
    r"|\bI\s*(?:'ve|ve| have|'m|m| am| will|'ll)?\s*(?:now\s+|just\s+)?"
    r'(?:complete[ds]?|finished|done)\b'
    r'|\bis\s+(?:now\s+|successfully\s+)?complete[ds]?\b',
    re.IGNORECASE,
)
_GOAL_GATE_NEG_RE = re.compile(
    r"\b(?:not|no|never|cannot|can't|won't|isn't|aren't|hasn't|haven't|"
    r"didn't|doesn't|don't|without|pending|remaining|incomplete)\b",
    re.IGNORECASE,
)


def _looks_like_completion(text: str) -> bool:
    if not text:
        return False
    snippet = text[-4000:]
    for match in _GOAL_GATE_RE.finditer(snippet):
        window = snippet[max(0, match.start() - 40):match.end() + 10]
        if not _GOAL_GATE_NEG_RE.search(window):
            return True
    return False


def _goal_gate_check(
    state: dict[str, Any],
    chat_id: str,
    ctx: AgentContext,
    entry: dict[str, Any],
    cfg: dict[str, Any],
    now_iso: str,
    summary: dict[str, Any],
    view: _LogView | None = None,
) -> bool:
    # v1.8.0 goal completion gate: silent nudge when a USER chat's latest
    # response claims completion while its native goal is still open.
    # Budget (goal_gate_count) applies per goal version and resets when
    # the goal record changes (goal_gate_key = goal updated_at). Returns
    # True when a nudge was sent.
    if not bool(cfg.get('goal_gate_enabled', True)):
        return False
    max_nudges = max(0, min(10, _as_int(cfg.get(
        'goal_gate_max_nudges', GOAL_GATE_MAX_NUDGES
    ), GOAL_GATE_MAX_NUDGES)))
    if max_nudges <= 0:
        return False
    cooldown = _as_float(cfg.get(
        'goal_gate_cooldown_minutes', GOAL_GATE_COOLDOWN_MIN
    ), float(GOAL_GATE_COOLDOWN_MIN))
    goal = _active_goal(chat_id)
    if not goal:
        return False
    if not _looks_like_completion(_last_response_text(ctx, view)):
        return False
    goal_key = str(goal.get('updated_at') or goal.get('objective') or '')
    if str(entry.get('goal_gate_key', '')) != goal_key:
        entry['goal_gate_key'] = goal_key
        entry['goal_gate_count'] = 0
    # v1.21.0 (A1): counters from state.json are coerced here too - the
    # load-boundary repair covers the file, and this covers an entry mutated
    # in-process or written by an older version.
    count = _as_int(entry.get('goal_gate_count', 0))
    if count >= max_nudges:
        return False
    if cooldown > 0 and _minutes_since(
        _parse_dt(entry.get('last_goal_gate_at', ''))
    ) < cooldown:
        return False
    objective = str(goal.get('objective', ''))[:GOAL_GATE_OBJECTIVE_MAX]
    if not _nudge_context(
        ctx, GOAL_GATE_TEXT.replace('{objective}', objective), chat_id=chat_id
    ):
        return False
    entry['goal_gate_count'] = count + 1
    entry['last_goal_gate_at'] = now_iso
    entry['goal_gate_nudges_sent'] = (
        _as_int(entry.get('goal_gate_nudges_sent', 0)) + 1
    )
    summary['goal_gate_nudged'] = summary.get('goal_gate_nudged', 0) + 1
    try:
        state_mod.append_history(state, {
            'chat_id': chat_id,
            'action': 'goal_gate_nudge',
            'goal_key': goal_key,
            'timestamp': now_iso,
        })
    except Exception as e:
        _debug_log('goal_gate_history_fail', repr(e))
    _debug_log(
        'goal_gate',
        chat_id + ' nudge #' + str(count + 1) + ' (goal still active)',
    )
    return True



def tick(cfg: dict[str, Any]) -> dict[str, Any]:
    #
    # v1.20.3 (reviewed, NOT changed): the lock IS held across message
    # delivery (`_nudge_context` / `ctx.nudge()`), which looks inconsistent
    # with api/nudge.py + api/draft.py, where `communicate()` deliberately runs
    # outside the lock. It was left as is on purpose:
    #   - delivery is non-blocking by the audited cross-thread contract (an
    #     alive task takes a plain `agent.intervention` attribute write; a dead
    #     one schedules onto the context's shared loop via DeferredTask), so
    #     the extra hold is microseconds, not the tick's real cost;
    #   - the alternative - plan the sends, release the lock, deliver, then
    #     re-acquire the lock to apply the bookkeeping - splits one durable
    #     transition into two. A crash in between would persist a tick that
    #     claims nudges were sent when they were not, and re-reading state in
    #     the second phase risks discarding the in-memory work the first phase
    #     built. That is a worse failure mode than a short lock hold.
    # TEST38 pins the invariant that actually matters: a manual API action
    # landing during a tick never loses the tick's updates.
    # v1.22.0 (stage 3): the wrapper now reports its own death — a tick that
    # raises here must leave durable evidence (the failure counter + the
    # loop-suspended self-check stamp), because the dashboard keeps showing
    # the last good snapshot and the job loop only logs one line.
    global _TICKS_SINCE_START
    try:
        with state_mod.state_lock():
            _result = _tick_impl(cfg)
            _TICKS_SINCE_START += 1
            return _result
    except Exception as exc:
        _debug_log('tick_fail', repr(exc))
        try:
            state_mod.state_transaction_apply(
                lambda st: _record_tick_failure(st, cfg)
            )
        except Exception:
            pass
        return {'skipped': True, 'reason': 'tick_failed', 'error': repr(exc)[:300]}


def _record_tick_failure(state: dict[str, Any], cfg: dict[str, Any] | None = None) -> None:
    # v1.22.0 (stage 3, ported from chat_shepherd2): increment the durable
    # failure counter and evaluate the self-check inline with
    # loop_suspended=True — a permanent failure is self-sustaining (the
    # evaluating phase never arrives), so the death must write its own
    # findings. Journal the event too.
    stamp = state.get('selfcheck')
    if not isinstance(stamp, dict):
        stamp = {}
        state['selfcheck'] = stamp
    stamp['consecutive_tick_failures'] = selfcheck_mod.as_int(
        stamp.get('consecutive_tick_failures'), 0
    ) + 1
    stamp['last_tick_failure_at'] = datetime.now(timezone.utc).isoformat()
    try:
        state_mod.append_history(state, {
            'chat_id': '',
            'action': 'tick_fail',
            'timestamp': stamp['last_tick_failure_at'],
        })
    except Exception:
        return
    try:
        findings = selfcheck_mod.evaluate(
            {
                # loop_suspended suppresses tick_late (one finding naming the
                # outage beats two) and adds the loop_suspended finding.
                'last_tick': '',
                'loop_suspended': True,
                'consecutive_tick_failures': selfcheck_mod.as_int(
                    stamp.get('consecutive_tick_failures'), 0
                ),
                'inflight': [],
                'channels': {},
                'capabilities': {},
                'live_contexts': 0,
                'checked': 0,
                'save_failures': _save_failures,
            },
            cfg if isinstance(cfg, dict) else {},
            now=datetime.now(timezone.utc),
        )
        stamp['findings'] = findings
        stamp['verdict'] = selfcheck_mod.health_verdict(findings)
        stamp['score'] = selfcheck_mod.health_score(findings)
        stamp['since_signature'] = '|'.join(sorted(
            str(r.get('kind')) for r in selfcheck_mod.pageable(findings)))
        if not stamp.get('since'):
            stamp['since'] = stamp['last_tick_failure_at']
    except Exception:
        pass

def _persist_selfcheck(
    state: dict[str, Any], cfg: dict[str, Any], findings: list[dict[str, Any]]
) -> None:
    # v1.22.0 (stage 3, ported from chat_shepherd2): stamp, roll up, journal
    # TRANSITIONS only. What counts as a FAILURE is "a human should look at
    # this" (warn + error); keying the counter on the list instead of the
    # severity made consecutive_failures climb forever on a healthy
    # supervisor, so the health panel read "verdict ok, score 100, failures 3".
    stamp = state.get('selfcheck')
    # An ABSENT stamp is not distinguishable from a {}-backfilled one (load
    # repairs merge default shapes), so treat both as "nothing recorded yet".
    _stamp_absent = not isinstance(stamp, dict) or not stamp
    if _stamp_absent:
        if not findings:
            # v1.22.0 (stage 3): a fully quiet tick with no prior stamp says
            # nothing about nothing — creating a healthy stamp here would
            # churn the durable signature on the first tick of every session
            # and defeat the quiet-tick save gate (TEST18A). The dashboard
            # derives ok/unknown from /status's default view instead; the
            # FIRST tick (first_run finding) and any fault do stamp.
            return
        if not isinstance(stamp, dict):
            stamp = {}
            state['selfcheck'] = stamp
    now_iso = datetime.now(timezone.utc).isoformat()
    pageable_rows = selfcheck_mod.pageable(findings)
    failed = bool(pageable_rows)
    previous_failures = selfcheck_mod.as_int(stamp.get('consecutive_failures'), 0)
    stamp['last_at'] = now_iso
    stamp['findings'] = findings
    stamp['consecutive_failures'] = previous_failures + 1 if failed else 0
    stamp['last_ok'] = not failed

    previous_signature = str(stamp.get('since_signature') or '')
    signature = '|'.join(sorted(str(row.get('kind')) for row in pageable_rows))
    if signature != previous_signature:
        stamp['since_signature'] = signature
        stamp['since'] = now_iso if signature else ''

    verdict = selfcheck_mod.health_verdict(findings)
    previous_verdict = str(stamp.get('verdict') or '')
    stamp['verdict'] = verdict
    stamp['score'] = selfcheck_mod.health_score(findings)

    if signature != previous_signature or verdict != previous_verdict:
        stamp['findings'] = findings
        state_mod.append_history(state, {
            'chat_id': '',
            'action': 'selfcheck_fail' if failed else 'selfcheck',
            'verdict': verdict,
            'kinds': sorted({str(row.get('kind')) for row in findings}),
            'timestamp': now_iso,
        })
    stamp['findings'] = findings
    # The failure counter is cleared only after persisting (T11C order).
    stamp['consecutive_tick_failures'] = 0


def _tick_impl(cfg: dict[str, Any]) -> dict[str, Any]:
    # v1.22.0 (stage 3): the save-failure counter is a module global; the
    # assignment below must not silently turn reads of it into a local
    # (UnboundLocalError killed the self-check phase wholesale).
    global _save_failures
    if not cfg.get('enabled', False):
        return {'skipped': True, 'reason': 'disabled'}

    state = state_mod.load_state()
    prev_last_tick = _parse_dt(state.get('last_tick', ''))
    # v1.22.0 (stage 3): the self-check compares the PREVIOUS tick stamp;
    # capture it before the tick overwrites it.
    _prev_tick_iso = str(state.get('last_tick', '') or '')
    # Restart detection: a last_tick gap far beyond one tick interval
    # means the server (or job loop) restarted and contexts were
    # rebuilt from disk. A missing last_tick (first ever tick) is not
    # a restart.
    server_restarted = (
        prev_last_tick is not None
        and _minutes_since(prev_last_tick) > RESTART_GAP_MINUTES
    )
    state['last_tick'] = datetime.now(timezone.utc).isoformat()

    # v1.20.7: these conversions run OUTSIDE the per-chat try (they feed the
    # whole tick), so they go through the never-raising _as_int/_as_float
    # coercers. The save paths sanitize, but monitor._load_config() only
    # deep-merges defaults - a hand-edited config.json with a null/string
    # timing knob used to raise float(None) here and silently kill every
    # tick (the extension only logged tick_fail to the debug log).
    nudge_cooldown = _as_float(cfg.get('nudge_cooldown_minutes', 10), 10.0)
    max_nudges = max(0, _as_int(cfg.get('max_auto_nudges', 3), 3))
    notify_on_intervention = bool(cfg.get('notify_on_intervention', True))
    # v1.7.0 quiet bell: persistence-gated intervention paging.
    notify_after = _as_float(cfg.get('notify_after_minutes', NOTIFY_AFTER_MIN), float(NOTIFY_AFTER_MIN))
    notify_rearm = _as_float(cfg.get('notify_rearm_minutes', NOTIFY_REARM_MIN), float(NOTIFY_REARM_MIN))
    notify_on_resume = bool(cfg.get('notify_on_resume', False))
    supervised = bool(cfg.get('supervised_mode', False))
    # v1.8.0 goal completion gate (thresholds read inside the check).
    goal_gate_enabled = bool(cfg.get('goal_gate_enabled', True))
    # P2: global burst throttle — at most this many auto-nudges per tick,
    # oldest stall first. 0 disables auto-nudging entirely.
    max_nudges_per_tick = max(0, min(10, _as_int(cfg.get('max_nudges_per_tick', 1), 1)))

    # v1.2.0 wedge remediation ladder. wedge_max_remediations 0 disables
    # wedge auto-remediation entirely (chats stay intervention-only).
    wedge_nudge_after = _as_float(cfg.get('wedge_nudge_after_minutes', WEDGE_NUDGE_AFTER_MIN), float(WEDGE_NUDGE_AFTER_MIN))
    wedge_max_remediations = max(0, min(5, _as_int(cfg.get(
        'wedge_max_remediations', WEDGE_MAX_REMEDIATIONS
    ), WEDGE_MAX_REMEDIATIONS)))
    wedge_cooldown = _as_float(cfg.get(
        'wedge_remediation_cooldown_minutes', WEDGE_REMEDIATION_COOLDOWN_MIN
    ), float(WEDGE_REMEDIATION_COOLDOWN_MIN))
    wedge_soft_restart = bool(cfg.get('wedge_soft_restart', False))
    # v1.10.0 liveness probe: classify wedges as hung (classic) vs
    # spinning dead loop via the log mutation counter. Off = legacy.
    wedge_probe = bool(cfg.get('wedge_liveness_probe', WEDGE_LIVENESS_PROBE))

    # v1.23.0: provider-exhaustion loops + substantive-progress liveness +
    # the stall-escalation restart. All three degrade to shipped defaults on
    # a hand-edited value (the _as_float contract).
    exhaustion_minutes = _as_float(cfg.get('exhaustion_minutes', 20), 20.0)
    exhaustion_kill_after = _as_float(
        cfg.get('exhaustion_kill_after_minutes', 0), 0.0)
    no_response_minutes = _as_float(cfg.get('no_response_minutes', 0), 0.0)
    stall_escalate = bool(cfg.get('stall_escalate_to_restart', False))

    # R4 adaptive thresholds: opt-in learning overlay. evaluate() updates
    # the learned values (persisted under state['adaptive']) at most every
    # adaptive_interval_minutes using journal-windowed outcome rates; a
    # manual settings change re-seeds the baseline on the next tick.
    # effective_values() applies the learned values as a runtime-only
    # overlay: the persisted config is never written, and configured
    # knobs outside the learnable band stay pinned.
    adaptive_tag = 'off'
    if bool(cfg.get('adaptive_thresholds', False)):
        try:
            info = adaptive_mod.evaluate(state, cfg)
            overlay = adaptive_mod.effective_values(
                cfg, (state.get('adaptive') or {}).get('learned')
            )
            nudge_cooldown = float(overlay['nudge_cooldown_minutes'])
            wedge_nudge_after = float(overlay['wedge_nudge_after_minutes'])
            wedge_cooldown = float(overlay['wedge_remediation_cooldown_minutes'])
            adaptive_tag = str(info.get('reason') or 'evaluated')
        except Exception as e:
            _debug_log('adaptive_fail', repr(e))
            adaptive_tag = 'error'

    # P1: only real UI chats are tracked. Contexts without a persisted
    # usr/chats/<id> directory (ad-hoc message_async ids) are invisible here.
    watch_all = bool(cfg.get('watch_all', True))
    allowed_chat_ids = cfg.get('allowed_chat_ids') or []
    live_contexts: dict[str, Any] = {}
    try:
        for ctx in AgentContext.all():
            # v1.22.0 (stage 5): the supervisor must never watch its own
            # watchdog - a wake context is a real context with a real id and
            # a persisted chat dir; only its data stamp says whose it is.
            if wake_mod.is_shepherd_context(ctx):
                continue
            if not _has_chat_dir(ctx.id):
                continue
            if allowed_chat_ids and ctx.id not in allowed_chat_ids:
                continue
            if not watch_all and _log_len(ctx) <= 0:
                continue
            live_contexts[ctx.id] = ctx
    except Exception:
        pass

    summary = {
        'checked': 0,
        'running': 0,
        'stalled': 0,
        'nudged': 0,
        'intervention': 0,
        'wedged': 0,
        'wedge_nudged': 0,
        'wedge_restarts': 0,
        'wedge_spinning': 0,
        'error': 0,
        'awaiting': 0,
        'idle': 0,
        'paused': 0,
        'nudges_this_tick': 0,
        'interrupted': 0,
        'resumed': 0,
        'drafted': 0,
        'notify_failures': 0,
        'goal_gate_nudged': 0,
        'pruned': 0,
        'cap_evicted': 0,
        # v1.22.0 (stage 2): blocked/hold bookkeeping.
        'blocked': 0,
        'blocked_released': 0,
        'held': 0,
        'inflight_abandoned': 0,
        'actions_withheld': '',
        'wakes': 0,
        # v1.23.0: exhaustion loops, exhaustion kills, stall restarts.
        'provider_exhausted': 0,
        'exhaustion_kills': 0,
        'stall_restarts': 0,
    }
    summary['adaptive'] = adaptive_tag

    # P2: stalled chats that are cooldown-eligible, collected for the
    # post-loop throttle pass (never nudged inline in this loop).
    nudge_queue: list[tuple[float, str, Any, dict, float]] = []
    resume_queue: list[tuple[float, str, Any, dict, str, Any]] = []
    resumed_names: list[str] = []
    wedge_queue: list[tuple[float, str, Any, dict, float]] = []
    # v1.23.0 feature C: one soft restart per stall episode (drain below).
    stall_restart_queue: list[tuple[float, str, Any, dict, float]] = []
    # v1.22.0 (stage 2): NEW blocked transitions, applied post-loop under a
    # per-tick budget; the rest re-evaluate on the next tick.
    block_queue: list[tuple[str, str, str, dict]] = []
    inflight_on = bool(cfg.get('inflight_enabled', True))
    inflight_timeout = _as_float(cfg.get('inflight_timeout_minutes', 5), 5.0)
    inflight_timeout = max(0.5, min(120.0, inflight_timeout))
    inflight_blocked = 0
    # v1.22.0 (stage 5): the operating window and the continuity wake. The
    # window gates EXECUTION (the three drains) while classification and
    # blocking still run — a suppressed window is visible, not blind. The
    # startup grace holds auto-actions for the first N ticks after process
    # start so a cold boot does not escalate immediately.
    _grace_ticks = max(0, int(_as_float(cfg.get('startup_grace_ticks', 3), 3)))
    _within_hours = clock_mod.in_active_hours(cfg)
    _grace_ok = _TICKS_SINCE_START >= _grace_ticks
    _can_act = _within_hours and _grace_ok
    _interval_txt = str(int(_as_float(cfg.get('continuity_every_minutes', 30), 30))) + 'm'
    _continuity = state.get('continuity') if isinstance(
        state.get('continuity'), dict) else {}
    _continuity_due = clock_mod.is_due(
        _continuity.get('last_at', ''), _interval_txt)
    # v1.22.0 (stage 5): foreign plugins may have parked a chat for their own
    # reasons; the fingerprint treats that as the strongest signal.
    _ext_blocks = caps_mod.external_blocks() if bool(
        cfg.get('external_block_integration', True)) else {}
    # v1.22.0 (stage 4): reconcile the in-flight write-ahead intents. A
    # delivery that crashed between "recorded" and "delivered" is reaped
    # here and CHARGES the budget of the action it claimed (never replayed).
    if inflight_on:
        for _intent in state_mod.reconcile_inflight(state, inflight_timeout):
            summary['inflight_abandoned'] += 1
            try:
                inflight_blocked += 1
                _bchat = str(_intent.get('chat_id') or '')
                _act = str(_intent.get('action') or '')
                # v1.23.2 (audit fix 9, cycle 2): charging a budget on a
                # chat whose dir is gone used to RESURRECT a default entry
                # via get_chat that the same tick's prune then popped again
                # — create/destroy churn. The abandonment is still journaled.
                if _bchat and not _has_chat_dir(_bchat):
                    raise _SkipCharge()
                _bentry = state_mod.get_chat(state, _bchat)
                if _act.startswith('wedge'):
                    _bentry['wedge_nudge_count'] = _as_int(
                        _bentry.get('wedge_nudge_count', 0)) + 1
                else:
                    _bkey = _BUDGET_KEY_WAL.get(_act, 'nudge_count')
                    _bentry[_bkey] = _as_int(
                        _bentry.get(_bkey, 0)) + 1
                # v1.23.1 (audit fix 7): the stall-restart episode budget and
                # the exhaustion-kill guard are the ANTI-LOOP budgets —
                # reconcile must charge them too, or a failed delivery
                # re-escalates the same action within one episode.
                if _act == 'stall_restart':
                    _bentry['stall_restart_count'] = _as_int(
                        _bentry.get('stall_restart_count', 0)) + 1
                elif _act == 'exhaustion_kill':
                    # Never replayed (the phase-0 contract): mark the kill
                    # spent. If the kill did NOT land, the loop keeps
                    # cycling and the exhaustion arm keeps holding/paging —
                    # a second kill attempt is a policy change, not a
                    # delivery retry.
                    if not _bentry.get('provider_exhausted_killed', ''):
                        _bentry['provider_exhausted_killed'] = (
                            datetime.now(timezone.utc).isoformat()
                        )
            except _SkipCharge:
                # Extinct chat: the abandonment journal row below is the
                # record; no budget exists to charge.
                pass
            except Exception as e:
                _debug_log('inflight_reconcile_fail', repr(e))
            try:
                state_mod.append_history(state, {
                    'chat_id': str(_intent.get('chat_id') or ''),
                    'action': 'inflight_abandoned',
                    'detail': str(_intent.get('action') or ''),
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                })
            except Exception:
                pass

    # v1.22.0 (stage 2): the diagnosis gate's inputs, read once per tick.
    diag_enabled = bool(cfg.get('diagnose_enabled', True))
    allowed_cats = cfg.get('auto_remediate_categories') if isinstance(
        cfg.get('auto_remediate_categories'), list
    ) else []
    # v1.22.0 (stage 4): write-ahead intents for remediation deliveries.


    for chat_id, ctx in live_contexts.items():
        # v1.20.2: isolate per-chat failures. A single malformed state entry
        # (e.g. a hand-edited nudge_count string) used to raise out of this
        # loop and abort the WHOLE tick - no nudges, no prune, no cap, no
        # throttle snapshot for any chat. One bad chat must not blind the
        # supervisor to every other chat.
        try:
          summary['checked'] += 1
          now_iso = datetime.now(timezone.utc).isoformat()
          prev = state_mod.get_chat(state, chat_id)
          old_status = prev.get('status', '')

          # v1.21.0 (O5): ONE log lock acquisition for this chat, shared by
          # the frozen clock, the liveness probe, the classifier, the
          # termination scan and the persisted last_log_type. Reading it five
          # times also let the classifier and the frozen clock disagree by one
          # entry on a chat that was logging while the tick ran.
          view = _log_snapshot(ctx)

          # P3 wedge detection: capture the log length and compare with the
          # previous tick. Unchanged length while running => start/continue the
          # frozen clock; any growth resets it.
          cur_log_len = view.length
          prev_log_len = _as_int(prev.get('last_log_len', -1), -1)
          # v1.10.0 liveness probe: read the log mutation counter next
          # to the entry count. Entries frozen + mutations advancing =
          # spinning dead loop; both frozen = hung call. O(1) reads
          # under the same lock; unknown counter = legacy behavior.
          cur_updates = view.updates if wedge_probe else -1
          frozen_minutes = 0.0
          liveness = ''
          if (
              ctx.is_running()
              and cur_log_len >= 0
              and prev_log_len >= 0
              and cur_log_len == prev_log_len
          ):
              frozen_since = _parse_dt(prev.get('log_len_since', ''))
              if frozen_since is None:
                  prev['log_len_since'] = now_iso
                  frozen_minutes = 0.0
              else:
                  frozen_minutes = _minutes_since(frozen_since)
                  prev_updates = _as_int(prev.get('last_updates_len', -1), -1)
                  if cur_updates >= 0 and prev_updates >= 0:
                      if cur_updates > prev_updates:
                          # v1.23.0: mutations advancing while entries are
                          # frozen is ALSO the signature of healthy STREAMING
                          # — the response/agent LogItem is created once and
                          # updated in place per streamed chunk, so a long
                          # streamed reply looks exactly like a spinning dead
                          # loop here. An in-flight model call/streamed
                          # response (last entry is the agent/response item)
                          # is not a dead loop. Without this, enabling
                          # wedge_soft_restart would kill the agent
                          # mid-stream on the first frozen clock check.
                          # v1.23.1: the verdict is the explicit 'streaming'
                          # value so classify's frozen arm can recognise it.
                          if view.last_type in ('agent', 'response'):
                              liveness = LIVENESS_STREAMING
                          else:
                              liveness = 'spinning'
                      elif cur_updates == prev_updates:
                          liveness = 'hung'
                  prev['liveness'] = liveness
          else:
              prev['log_len_since'] = ''
              # v1.23.1 (audit fix 5): when the task is NOT running, a stale
              # 'spinning_soft' no-response stamp must not re-inherit into
              # the next turn (before this, only the run-side upkeep cleared
              # these, so a restarted chat classified intervention with the
              # PRE-restart age on its first healthy turn back). The log
              # GREW case deliberately keeps its soft stamps: growth with no
              # response is the signal the feature watches.
              if not ctx.is_running():
                  prev['no_response_since'] = ''
                  if prev.get('liveness', '') == LIVENESS_SPINNING_SOFT:
                      prev['liveness'] = ''
              # v1.2.0: the log grew again (or the task stopped) - a pending
              # wedge remediation either worked or is moot; credit it if recent.
              _record_wedge_outcome(state, chat_id, now_iso)

          # v1.23.0 exhaustion + substantive-progress tracking. The stamps
          # feed classify_chat below; recovery = a fresh assistant response
          # or a genuine human turn newer than the episode start.
          if diag_enabled:
              try:
                  _hum_ts = diag_mod.last_human_user_at(view)
                  _resp_ts = diag_mod.last_response_at(view)
                  # v1.23.1 (audit fix 13/18): unparseable hand-edited stamps
                  # used to read as 9999 minutes (instant hold) and could
                  # never be resolved-clear. Recharge them so the machine
                  # re-anchors instead of holding on a corrupt value.
                  for _stamp_key in (
                      'exhaustion_since', 'no_response_since',
                      'provider_exhausted_at',
                  ):
                      if prev.get(_stamp_key, '') and _parse_dt(
                          prev[_stamp_key]
                      ) is None:
                          prev[_stamp_key] = now_iso
                  # post-kill hold upkeep: cleared by human/response, or by
                  # expiry (classify stops holding; the stamp must too).
                  if prev.get('provider_exhausted_at', ''):
                      _pk_dt = _parse_dt(prev['provider_exhausted_at'])
                      _pk_ts = _pk_dt.timestamp() if _pk_dt else 0.0
                      if (
                          _pk_ts
                          and (
                              (_resp_ts and _resp_ts > _pk_ts)
                              or (_hum_ts and _hum_ts > _pk_ts)
                          )
                      ) or (_pk_dt and _minutes_since(_pk_dt) >= EXHAUSTION_HOLD_MIN):
                          prev.pop('provider_exhausted_at', None)
                          prev.pop('provider_exhausted_killed', None)
                          # v1.23.1 (audit fix 8): the hold ending by EXPIRY
                          # means the supervisor itself may re-enter the
                          # exhausted provider on this chat (the stall
                          # ladder nudges; the arm re-fires) — the page-once
                          # stamp must reset so that re-entry pages again.
                          # (fix 1's review: without this, expiry left the
                          # chat silently held forever by notified_at.)
                          prev['exhaustion_notified_at'] = ''
                  # post-detection hold upkeep: same clearing rule while the
                  # episode is over (chat no longer cycling markers).
                  _ex_matched = False
                  _ex_count = 0
                  if ctx.is_running():
                      # A live storm logs a marker per retry attempt; the
                      # incident cycled every ~12 min, so the freshness
                      # window must be at least that patient. The episode
                      # threshold itself is the honest bound: no new
                      # marker for that long = no longer an active loop.
                      _ex_matched, _ex_count, _ex_txt = diag_mod.exhaustion_loop(
                          view, max_marker_age_s=exhaustion_minutes * 60.0
                      )
                      if _ex_matched:
                          if not prev.get('exhaustion_since', ''):
                              prev['exhaustion_since'] = now_iso
                          prev['exhaustion_count'] = _ex_count
                          prev['exhaustion_last_matched'] = now_iso
                          prev['last_exhaustion_text'] = str(_ex_txt)[:200]
                      elif prev.get('exhaustion_since', ''):
                          _ex_ts = _parse_dt(prev['exhaustion_since'])
                          if _ex_ts is not None and (
                              (_resp_ts and _resp_ts > _ex_ts.timestamp())
                              or (_hum_ts and _hum_ts > _ex_ts.timestamp())
                          ):
                              prev['exhaustion_since'] = ''
                              prev['exhaustion_count'] = 0
                              prev['exhaustion_notified_at'] = ''
                              prev.pop('exhaustion_last_matched', None)
                              # v1.23.1 (audit fix 9/19): the episode's
                              # text is episode-scoped too — a stale
                              # excerpt must not outlive the episode.
                              prev.pop('last_exhaustion_text', None)
                              state_mod.append_history(state, {
                                  'chat_id': chat_id,
                                  'action': 'provider_exhaustion_resolved',
                                  'detail': 'response or human turn newer than episode',
                                  'timestamp': now_iso,
                              })
                      # v1.23.0 feature B: entries advancing but the newest
                      # assistant response is older than no_response_minutes.
                      if no_response_minutes > 0:
                          _resp_old = not _resp_ts or (
                              (time.time() - _resp_ts) / 60.0 > no_response_minutes
                          )
                          if (
                              cur_log_len >= 0 and prev_log_len >= 0
                              and cur_log_len > prev_log_len and _resp_old
                          ):
                              if not prev.get('no_response_since', ''):
                                  prev['no_response_since'] = now_iso
                              # Re-stamp every active tick: the growth branch
                              # above wiped `liveness` again this tick.
                              prev['liveness'] = LIVENESS_SPINNING_SOFT
                          elif prev.get('no_response_since', ''):
                              # a fresh response landed (or entries did not
                              # advance): the soft verdict is moot.
                              prev['no_response_since'] = ''
                              if prev.get('liveness', '') == LIVENESS_SPINNING_SOFT:
                                  prev['liveness'] = ''
              except Exception as e:
                  # Best-effort: a bad state value here must not sink the
                  # per-chat pass (same contract as the reset loop).
                  _debug_log('exhaustion_track_fail', chat_id + ' ' + repr(e))

          # v1.22.0 (stage 2): a blocked chat is released by a human, never
          # by the ladder. Release = a genuine human turn newer than
          # `blocked_at` (the plugin's own nudges are automation, not a human
          # turn, so they cannot manufacture the release). The old verdict's
          # signature is remembered so the same failure does not immediately
          # re-block on the unchanged error.txt. Must run BEFORE classify:
          # the blocked rule in classify_chat returns early on the stored
          # status, and a release has to unblock the entry first.
          _was_blocked = old_status == STATUS_BLOCKED
          _released_now = False
          if _was_blocked and diag_enabled:
              _rel_type = diag_mod.last_relevant_type(view)
              _human_ts = diag_mod.last_human_user_at(view)
              if planner_mod.human_arrived_after_block(
                  _rel_type, _human_ts, prev.get('blocked_at', '')
              ):
                  prev['released_signature'] = prev.get('category_signature', '')
                  prev['status'] = STATUS_NUDGED
                  prev['blocked_reason'] = ''
                  prev['blocked_at'] = ''
                  prev['supervisor_settled'] = False
                  _was_blocked = False
                  _released_now = True
                  summary['blocked_released'] += 1
                  state_mod.append_history(state, {
                      'chat_id': chat_id,
                      'action': 'blocked_released',
                      'detail': 'released by a human turn (newer than blocked_at)',
                      'timestamp': now_iso,
                  })

          new_status, reason = classify_chat(
              ctx, chat_id, cfg, prev, frozen_minutes, view=view
          )

          # v1.22.0 (stage 2): diagnose why the chat stopped, persist the
          # category, and apply the gate verdict (act / hold / block) before
          # the queues see the chat. The safety floor lives inside plan_seam,
          # AFTER the allowlist, so config cannot re-enable auto-fixing a
          # safety termination or an auth failure.
          gate_verdict, gate_reason = planner_mod.ACT, ''
          if diag_enabled:
              _error_txt = ''
              if _check_error_file(chat_id):
                  _error_txt = diag_mod.read_error_head(
                      os.path.join(_chat_dir(chat_id), 'error.txt')
                  )
              _fp = diag_mod.fingerprint(
                  chat_id,
                  error_text=_error_txt,
                  fatal=diag_mod.fatal_text(view),
                  external_block=_ext_blocks.get(chat_id, ''),
              )
              _cat = _fp['category']
              _cat_sig = _fp['signature']
              # v1.23.0: the classifier's exhaustion verdict outranks the
              # fragment text. During a retry storm the same tail carries
              # RateLimitError/traceback/429 fragments that rotate the
              # fingerprint between auth/rate_limit/network every tick (the
              # live incident flickered auth <-> framework_crash every ~15
              # min) — and an auth fragment would BLOCK a chat that only
              # needs a provider window reset. The sustained loop is ONE
              # category: provider_exhaustion, which plan_seam HOLDs
              # (outside auto_remediate_categories, outside NEVER_REMEDIATE).
              if reason.startswith(EXHAUSTION_REASON_PREFIX):
                  _cat = CATEGORY_PROVIDER_EXHAUSTION
                  _cat_sig = diag_mod.signature(_cat, reason)
                  _fp['source'] = 'classify'
                  _fp['excerpt'] = diag_mod.excerpt(reason)
              if diag_mod.category_changed(prev.get('category', 'none'), _cat):
                  prev['category_since'] = now_iso
                  state_mod.append_history(state, {
                      'chat_id': chat_id,
                      'action': 'classify',
                      'category': _cat,
                      'signature': _cat_sig,
                      'source': _fp['source'],
                      'timestamp': now_iso,
                  })
              prev['category'] = _cat
              prev['category_signature'] = _cat_sig
              prev['category_excerpt'] = _fp['excerpt']
              gate_verdict, gate_reason = planner_mod.plan_seam(
                  _cat, allowed_cats,
                  released_signature=prev.get('released_signature', ''),
                  category_signature=_cat_sig,
              )
              if gate_verdict == planner_mod.BLOCK and not _was_blocked:
                  block_queue.append((now_iso, chat_id, gate_reason, prev))
                  # The block supersedes the ladder for this chat this tick.
                  gate_verdict = planner_mod.HOLD
              elif gate_verdict == planner_mod.BLOCK and _was_blocked:
                  # Still blocked and the floor says block again — the
                  # classify rule already keeps it blocked; do not queue.
                  gate_verdict = planner_mod.HOLD
          elif _was_blocked:
              # Diagnose off: a blocked chat keeps its terminal status; the
              # only gate that still runs is the (config-dependent) ladder.
              gate_verdict = planner_mod.HOLD

          # v1.18.2 fix (terminate-loop, part 2): persist the termination
          # stamp durably in state. A full-tail scan catches a TERMINATED
          # warning / error stop even when later items (shepherd nudges,
          # progress entries) bury it; a fresh human turn (shepherd nudges
          # excluded) clears the suppression early. The stamp gates both
          # nudge queues below for TERMINATION_NUDGE_COOLDOWN_MIN minutes.
          _term_found, _term_fresh_human = _scan_log_for_termination(ctx, view)
          if _term_found:
              prev['terminated_at'] = now_iso
          elif _term_fresh_human:
              prev['terminated_at'] = ''
          _term_cooldown_active = _termination_cooldown_active(prev)

          # Restart recovery: chat was active before the restart, is
          # restored-but-idle now, and its log does not end with a completed
          # response => it was interrupted mid-task. USER contexts only -
          # the scheduler resumes its own task chats on its own cadence.
          if (
              server_restarted
              and old_status in (
                  STATUS_RUNNING, STATUS_STALLED, STATUS_NUDGED, STATUS_INTERVENTION
              )
              and getattr(ctx, 'type', None) == AgentContextType.USER
              and not ctx.is_running()
              and not getattr(ctx, 'paused', False)
              and _last_entry_type(ctx, view) != 'response'
          ):
              new_status = STATUS_INTERRUPTED
              # v1.23.1 (audit fix 5b): the restart re-anchors EVERYTHING
              # about the previous run — an exhaustion episode stamp (or a
              # no-response stamp) carried across the restart would make
              # the first re-match classify intervention with the
              # PRE-restart age and instantly suppress the ladder on a
              # chat that started clean.
              for _stamp_key in (
                  'exhaustion_since', 'exhaustion_notified_at',
                  'no_response_since', 'provider_exhausted_at',
                  'provider_exhausted_killed',
              ):
                  prev[_stamp_key] = ''
              prev['exhaustion_count'] = 0
              prev.pop('exhaustion_last_matched', None)
              prev.pop('last_exhaustion_text', None)
              if prev.get('liveness', '') == LIVENESS_SPINNING_SOFT:
                  prev['liveness'] = ''
              reason = (
                  f'Server restarted while chat was {old_status}; '
                  'mid-task work interrupted'
              )

          # v1.22.0 (stage 2): one per-chat hold count for the actionable
          # statuses; the dashboard shows the category next to the status,
          # so a held chat stays readable instead of silently idle.
          if (
              gate_verdict == planner_mod.HOLD
              and new_status in (STATUS_STALLED, STATUS_INTERRUPTED, STATUS_INTERVENTION)
          ):
              summary['held'] += 1
          # v1.22.0 (stage 5): the operating window gates EXECUTION only —
          # classification and blocking above still ran. Withheld actions are
          # visible in the summary, never silent.
          if (
              not _can_act
              and new_status in (STATUS_STALLED, STATUS_INTERRUPTED, STATUS_INTERVENTION)
              and not summary.get('actions_withheld')
          ):
              summary['actions_withheld'] = (
                  'outside active hours' if not _within_hours
                  else 'startup grace period'
              )

          fname = _chat_display_name(chat_id, ctx, prev)
          entry = state_mod.update_chat(
              state,
              chat_id,
              name=fname,
              status=new_status,
              last_classification=reason,
               last_log_type=_last_log_type(ctx, view) if ctx else '',
              last_log_len=cur_log_len,
              last_updates_len=cur_updates,
              last_ticked=now_iso,
          )
          if new_status != STATUS_INTERVENTION:
              # v1.7.0: episode ended (recovered / awaiting / error) - clear the
              # quiet-bell clock so a future episode pages on its own merits.
              entry['intervention_since'] = ''
              entry['intervention_notify_at'] = ''

          if new_status == STATUS_RUNNING:
              entry['last_seen_running'] = now_iso
              summary['running'] += 1
              _record_nudge_outcome(state, chat_id, now_iso)
          elif new_status == STATUS_PAUSED:
              summary['paused'] += 1
          elif new_status == STATUS_ERROR:
              entry['error_detected'] = True
              summary['error'] += 1
          elif new_status == STATUS_AWAITING_USER:
              summary['awaiting'] += 1
              _record_nudge_outcome(state, chat_id, now_iso)
              # v1.8.0 goal completion gate: a response that claims the work is
              # done while the chat's native goal is still open gets a silent
              # nudge to finish the goal or close it via the goal tool.
              if (
                  goal_gate_enabled
                  and getattr(ctx, 'type', None) == AgentContextType.USER
                  and _goal_gate_check(
                      state, chat_id, ctx, entry, cfg, now_iso, summary,
                      view=view,
                  )
              ):
                  entry['status'] = STATUS_NUDGED
                  entry['last_classification'] = (
                      'Goal gate: completion claim while goal still active'
                  )
          elif new_status == STATUS_STALLED:
              summary['stalled'] += 1
              last_nudge_dt = _parse_dt(prev.get('last_nudge_at', ''))
              cooldown_ok = _minutes_since(last_nudge_dt) >= nudge_cooldown
              minutes_idle = _minutes_since(_parse_dt(getattr(ctx, 'last_message', '')))
              # v1.21.0 (A1): coerce - classify_chat already coerced this same
              # field, so an unusable value used to yield a status of "nudge
              # 3/3 used" in the UI while this comparison raised and the chat
              # was silently never queued again.
              nudge_count = _as_int(prev.get('nudge_count', 0))
              if (
                  cooldown_ok
                  and not _term_cooldown_active
                  and nudge_count < max_nudges
                  and gate_verdict == planner_mod.ACT
                  and _can_act
              ):
                  # v1.22.0 (stage 2): the diagnosis gate (hold/block)
                  # precedes the ladder — a non-allowlisted cause is held,
                  # a safety/auth verdict is queued for block, never nudged.
                  # Oldest stall first; final ordering happens post-loop.
                  nudge_queue.append((minutes_idle, chat_id, ctx, entry, now_iso))
          elif new_status == STATUS_INTERVENTION:
              summary['intervention'] += 1
              if 'log frozen' in reason:
                  summary['wedged'] += 1
                  if prev.get('liveness', '') == 'spinning':
                      summary['wedge_spinning'] += 1
                  # v1.2.0 remediation ladder: USER chats frozen past the grace
                  # period get an automatic "continue" (Tier 1), with the
                  # framework's ctx.nudge() (kill + fw.msg_nudge.md) available
                  # as the final attempt when wedge_soft_restart is enabled.
                  # Scheduler TASK contexts are excluded — the scheduler
                  # resumes its own task chats on its own cadence.
                  wedge_count = _as_int(prev.get('wedge_nudge_count', 0))
                  last_wedge_dt = _parse_dt(prev.get('last_wedge_nudge_at', ''))
                  wedge_cooldown_ok = _minutes_since(last_wedge_dt) >= wedge_cooldown
                  if (
                      wedge_max_remediations > 0
                      and wedge_count < wedge_max_remediations
                      and wedge_cooldown_ok
                      and not _term_cooldown_active
                      and frozen_minutes >= wedge_nudge_after
                      and getattr(ctx, 'type', None) == AgentContextType.USER
                      and gate_verdict == planner_mod.ACT
                      and _can_act
                  ):
                      # Oldest freeze first; final ordering post-loop.
                      wedge_queue.append((frozen_minutes, chat_id, ctx, entry, now_iso))
              elif reason.startswith(HK_REASON_PREFIX):
                  # v1.22.0 (stage 1): a wedged completion - the response was
                  # logged but the post-response housekeeping never finished,
                  # so the context is NOT running and there is no hung task
                  # to kill. Same tier-1 ladder, donor semantics: auto-
                  # continue only (the drain is marked housekeeping_wedge and
                  # its kill branch is skipped for these); the queue urgency
                  # is the marker age parsed back out of the reason.
                  summary['wedged'] += 1
                  summary['housekeeping_wedged'] = (
                      summary.get('housekeeping_wedged', 0) + 1
                  )
                  entry['housekeeping_wedge'] = reason
                  wedge_count = _as_int(prev.get('wedge_nudge_count', 0))
                  last_wedge_dt = _parse_dt(prev.get('last_wedge_nudge_at', ''))
                  wedge_cooldown_ok = _minutes_since(last_wedge_dt) >= wedge_cooldown
                  if (
                      wedge_max_remediations > 0
                      and wedge_count < wedge_max_remediations
                      and wedge_cooldown_ok
                      and not _term_cooldown_active
                      and getattr(ctx, 'type', None) == AgentContextType.USER
                  ):
                      hk_age = _as_float(
                          _HK_AGE.search(reason).group(1) if _HK_AGE.search(reason) else 0.0,
                          0.0,
                      )
                      if gate_verdict == planner_mod.ACT and _can_act:
                          wedge_queue.append((hk_age, chat_id, ctx, entry, now_iso))
              elif reason.startswith(NO_RESPONSE_REASON_PREFIX):
                  # v1.23.0: entries advancing but no assistant response for
                  # N minutes (feature B). These code paths reach intervention
                  # safe points between entries, so they route down the
                  # CLASSIC ladder below (tier-1 continue; kill only on the
                  # final attempt with wedge_soft_restart) — the drain's
                  # spinning skip only matches the hard 'spinning' verdict.
                  summary['wedged'] += 1
                  wedge_count = _as_int(prev.get('wedge_nudge_count', 0))
                  last_wedge_dt = _parse_dt(prev.get('last_wedge_nudge_at', ''))
                  wedge_cooldown_ok = _minutes_since(last_wedge_dt) >= wedge_cooldown
                  if (
                      wedge_max_remediations > 0
                      and wedge_count < wedge_max_remediations
                      and wedge_cooldown_ok
                      and not _term_cooldown_active
                      and getattr(ctx, 'type', None) == AgentContextType.USER
                      and gate_verdict == planner_mod.ACT
                      and _can_act
                  ):
                      _nr_dt = _parse_dt(prev.get('no_response_since', ''))
                      # Unknown stamp age = queue last (9999 sorts after everything).
                      _nr_urgency = _minutes_since(_nr_dt) if _nr_dt else 9999.0
                      wedge_queue.append((_nr_urgency, chat_id, ctx, entry, now_iso))
              elif reason.startswith(EXHAUSTION_REASON_PREFIX):
                  # v1.23.0: a provider-exhaustion retry loop. Held for a
                  # human (the fingerprint override maps it to the HOLDed
                  # provider_exhaustion category), paged ONCE, never nudged.
                  summary['provider_exhausted'] += 1
                  if not prev.get('exhaustion_notified_at', ''):
                      entry['exhaustion_notified_at'] = now_iso
                      state_mod.append_history(state, {
                          'chat_id': chat_id,
                          'action': 'provider_exhaustion_page',
                          'detail': str(reason)[:200],
                          'timestamp': now_iso,
                      })
                      if notify_on_intervention and not _notify(
                          'warning',
                          f'Chat {fname!r} ({chat_id}) is stuck in a '
                          f'provider-exhaustion retry loop: {reason}. No '
                          'nudges will be sent; it needs a human or a '
                          'provider window reset.',
                          priority='high',
                          cfg=cfg,
                          state=state,
                          chat_id=chat_id,
                      ):
                          summary['notify_failures'] += 1
                  # Optional kill of the futile loop (opt-in; off by
                  # default). kill_process() is a non-blocking future.cancel
                  # safe from this thread; deliberately NO fresh chain — a
                  # new task would re-enter the same exhausted provider.
                  # After the kill the post-kill hold keeps the chat
                  # intervention and the planner keeps it HOLD until a human
                  # turn, a fresh response, or the hold window expires.
                  if (
                      exhaustion_kill_after > 0
                      and ctx.is_running()
                      and not prev.get('provider_exhausted_killed', '')
                      and getattr(ctx, 'type', None) == AgentContextType.USER
                      and gate_verdict != planner_mod.BLOCK
                      and _can_act
                      and _minutes_since(
                          _parse_dt(prev.get('exhaustion_since', ''))
                      ) >= exhaustion_kill_after
                  ):
                      if inflight_on:
                          _wal_id = state_mod.begin_inflight(
                              state, 'exhaustion_kill', chat_id, 2)
                      try:
                          ctx.kill_process()
                          entry['provider_exhausted_at'] = now_iso
                          entry['provider_exhausted_killed'] = now_iso
                          entry['status'] = STATUS_INTERVENTION
                          entry['last_classification'] = (
                              'Provider exhaustion loop: killed the futile '
                              'retry task (no auto-restart)'
                          )
                          summary['exhaustion_kills'] += 1
                          state_mod.append_history(state, {
                              'chat_id': chat_id,
                              'action': 'exhaustion_kill',
                              'detail': str(reason)[:200],
                              'timestamp': now_iso,
                          })
                          if inflight_on:
                              state_mod.end_inflight(state, _wal_id)
                      except Exception as e:
                          _debug_log('exhaustion_kill_fail', chat_id + ' ' + repr(e))
              # v1.7.0 quiet bell: persistence-gated paging. A fresh episode starts
              # the clock silently; the bell rings only once the chat has
              # persistently needed human help for >= notify_after minutes
              # (0 = page immediately). notify_rearm re-pages an ongoing
              # episode (0 = one page per episode). Self-healed episodes (e.g.
              # the wedge auto-continue worked) never page.
              # v1.23.0: the exhaustion arm pages once itself (with its own
              # message), so the generic bell is suppressed for it — the
              # dedicated page replaces it, not double-pages it.
              if notify_on_intervention and not reason.startswith(
                  EXHAUSTION_REASON_PREFIX
              ):
                  since_dt = _parse_dt(prev.get('intervention_since', ''))
                  if since_dt is None:
                      entry['intervention_since'] = now_iso
                      since_dt = _parse_dt(now_iso)
                  waited = _minutes_since(since_dt)
                  if waited >= notify_after:
                      notify_at_dt = _parse_dt(entry.get('intervention_notify_at', ''))
                      rearm_ok = notify_at_dt is None or (
                          notify_rearm > 0
                          and _minutes_since(notify_at_dt) >= notify_rearm
                      )
                      if rearm_ok:
                          if not _notify(
                              'warning',
                              f'Chat {fname!r} ({chat_id}) still needs human '
                              f'intervention after {waited:.0f}m: {reason}',
                              priority='high',
                              cfg=cfg,
                              state=state,
                              chat_id=chat_id,
                              ):
                              summary['notify_failures'] += 1
                          entry['intervention_notify_at'] = now_iso
          elif new_status == STATUS_INTERRUPTED:
              summary['interrupted'] += 1
              if supervised:
                  # v1.16.0 supervised mode: queue an editable draft for
                  # human review instead of auto-resuming; nothing is
                  # sent and the nudge budget stays untouched.
                  if state_mod.add_draft(
                      state, chat_id, 'restart_resume', reason=reason
                  ):
                      summary['drafted'] += 1
              else:
                  # v1.21.0 (A1): coerce the persisted budget read (same field
                  # as the stall branch above).
                  nudge_count = _as_int(entry.get('nudge_count', 0))
                  if (
                      nudge_count < max_nudges
                      and gate_verdict == planner_mod.ACT
                      and _can_act
                  ):
                      minutes_idle = _minutes_since(_parse_dt(getattr(ctx, 'last_message', '')))
                      resume_queue.append(
                          (minutes_idle, chat_id, ctx, entry, now_iso, RESUME_TEXT)
                      )
          elif new_status == STATUS_BLOCKED:
              # v1.22.0 (stage 2): currently-blocked chat (transitioned on an
              # earlier tick, or blocked-by-broken-prior-entry for diagnose-
              # off runs). Terminal until release; it pages through the
              # /release endpoint or a human turn, not through the ladder.
              summary['blocked'] += 1
          elif new_status == STATUS_IDLE:
              summary['idle'] += 1
          elif new_status == STATUS_NUDGED:
              # v1.23.0 feature C: classify queued exactly one soft restart
              # after nudge exhaustion; the drain below delivers it
              # (budget 1 per stall episode).
              # v1.23.1 (audit A1): supervised mode's contract is that
              # stall remediations are drafted, never auto-sent — the
              # escalation restart IS a stall remediation, so it drafts
              # like the others instead of bypassing review.
              if stall_escalate and reason == STALL_RESTART_REASON and supervised:
                  if state_mod.add_draft(
                      state, chat_id, 'stall_restart',
                      reason=str(entry.get('last_classification', '')),
                  ):
                      # v1.23.1 (audit fix 6): the episode budget is spent
                      # by the DRAFT too — otherwise after the human sends
                      # it, classify re-escalates (budget still < 1) and
                      # drafts a second restart: a send→redraft loop the
                      # auto path's budget explicitly cannot do.
                      entry['stall_restart_count'] = (
                          _as_int(entry.get('stall_restart_count', 0)) + 1
                      )
                      summary['drafted'] += 1
              elif stall_escalate and reason == STALL_RESTART_REASON:
                  # v1.23.1 (audit fix 2): the escalation restart is a
                  # nudge-family action — it must respect the diagnosis
                  # gate (a chat another plugin parked is not ours to
                  # restart), the termination cooldown, and the operating
                  # window, exactly like every sibling arm.
                  if (
                      _can_act
                      and not _term_cooldown_active
                      and gate_verdict == planner_mod.ACT
                  ):
                      minutes_idle = _minutes_since(
                          _parse_dt(getattr(ctx, 'last_message', '')))
                      stall_restart_queue.append(
                          (minutes_idle, chat_id, ctx, entry, now_iso)
                      )
                  else:
                      # v1.23.2 (audit fix 3, cycle 2): the escalation reason
                      # could not be delivered (gate HOLD — a chat another
                      # cause-allowlist does not authorize; or the window/
                      # cooldown withholds). Leaving status nudged with
                      # "queued" standing was a silent unbounded limbo: not
                      # counted as held, never paged, never fallen back to
                      # intervention. Flip to intervention with the
                      # withheld reason so the bell/dashboard own it.
                      _withheld = (
                          gate_reason if gate_verdict == planner_mod.HOLD
                          and gate_reason else 'outside the operating window'
                      )
                      new_status = STATUS_INTERVENTION
                      reason = (
                          f'Stalled for a long time, nudges exhausted; '
                          f'escalation restart withheld: {_withheld}'
                      )
                      entry['last_classification'] = reason
                      entry['status'] = new_status
                      summary['held'] += 1
                      # The generic quiet bell lives in the INTERVENTION
                      # branch and never sees this chat (classify returned
                      # NUDGED), so this arm maintains its own
                      # persistence-gated page with the same clock fields —
                      # otherwise a help-needing chat goes silent exactly
                      # under the combination meant to escalate it.
                      if notify_on_intervention:
                          _w_since = _parse_dt(entry.get('intervention_since', ''))
                          if _w_since is None:
                              entry['intervention_since'] = now_iso
                          elif (
                              _minutes_since(_w_since) >= notify_after
                              and (
                                  not entry.get('intervention_notify_at', '')
                                  or (notify_rearm > 0 and _minutes_since(
                                      _parse_dt(
                                          entry.get('intervention_notify_at', '')
                                      )) >= notify_rearm
                                  )
                              )
                          ):
                              if not _notify(
                                  'warning',
                                  f'Chat {fname!r} ({chat_id}) still needs '
                                  f'human intervention after '
                                  f'{_minutes_since(_w_since):.0f}m: {reason}',
                                  priority='high',
                                  cfg=cfg,
                                  state=state,
                                  chat_id=chat_id,
                              ):
                                  summary['notify_failures'] += 1
                              entry['intervention_notify_at'] = now_iso
        except Exception as e:
            # Best-effort: keep the other chats supervised and make the
            # failure visible instead of silently losing supervision.
            summary['chat_errors'] = summary.get('chat_errors', 0) + 1
            _debug_log('chat_tick_fail', chat_id + ' ' + repr(e))

    # P2: drain the nudge queue under the global per-tick throttle,
    # oldest stall first. Everything left keeps status `stalled` and is
    # re-considered on the next tick.
    # Restart recovery drain: all interrupted chats stalled at the
    # same moment (the restart), so they get their own small burst
    # budget; the regular stalled queue below keeps its per-tick
    # throttle. Resume nudges bypass the per-chat cooldown on purpose
    # (a fresh server start is exactly when they matter) but still
    # respect max_auto_nudges.
    if resume_queue:
        resume_budget = (
            0 if max_nudges_per_tick <= 0
            else max(3, min(10, max_nudges_per_tick))
        )
        resume_queue.sort(key=lambda item: item[0], reverse=True)
        for minutes_idle, chat_id, ctx, entry, now_iso, text in resume_queue:
            if summary['nudges_this_tick'] >= resume_budget:
                break
            if inflight_on:
                _wal_id = state_mod.begin_inflight(state, 'restart_resume', chat_id, 1)
            if _nudge_context(ctx, text, chat_id=chat_id):
                # v1.21.0 (O12): `entry` IS the dict get_chat returns for this
                # id, so the extra lookup only re-read the same object.
                # v1.21.0 (A1): coerce - these three counters are read straight
                # out of state.json and used in `+ 1` arithmetic, and this
                # drain runs OUTSIDE the per-chat guard, so one unusable value
                # used to abort the entire tick for every chat.
                entry['nudge_count'] = _as_int(entry.get('nudge_count', 0)) + 1
                entry['nudges_sent'] = _as_int(entry.get('nudges_sent', 0)) + 1
                entry['last_nudge_at'] = now_iso
                entry['status'] = STATUS_NUDGED
                entry['last_classification'] = (
                    'Resume nudge after restart (attempt '
                    + str(entry['nudge_count']) + '/' + str(max_nudges) + ')'
                )
                summary['resumed'] += 1
                resumed_names.append(_chat_display_name(chat_id, ctx, entry))
                summary['nudges_this_tick'] += 1
                if inflight_on:
                    state_mod.end_inflight(state, _wal_id)
                state_mod.append_history(state, {
                    'chat_id': chat_id,
                    'action': 'restart_resume',
                    'nudge_count': entry['nudge_count'],
                    'timestamp': now_iso,
                })

    # v1.20.7: the stalled queue gets its OWN budget counter. It used to break
    # on summary['nudges_this_tick'], which the restart-resume drain above
    # also increments - a restart burst (resume budget >= 3) therefore
    # consumed the stalled queue's whole per-tick budget (with
    # max_nudges_per_tick: 1, three resume nudges left every queued stalled
    # chat unnudged for that tick and the dashboard showed a false "capped").
    # The wedge drain was already independent (its own counters below).
    # v1.21.0 (A7): a `pre_drain_nudged` snapshot used to sit here, but
    # summary['nudged'] is only ever incremented inside THIS drain, so it was
    # always 0 and the subtraction was dead weight. Removed with the vestige;
    # `nudge_queue_len` still feeds the throttle snapshot's suppression test.
    nudge_queue_len = len(nudge_queue)
    stall_drained = 0
    if nudge_queue and (supervised or max_nudges_per_tick > 0):
        nudge_queue.sort(key=lambda item: item[0], reverse=True)
        for minutes_idle, chat_id, ctx, entry, now_iso in nudge_queue:
            if (
                not supervised
                and stall_drained >= max_nudges_per_tick
            ):
                break
            if supervised:
                # v1.16.0: draft instead of auto-send; no budget use.
                if state_mod.add_draft(
                    state, chat_id, 'stall_nudge',
                    reason=str(entry.get('last_classification', '')),
                ):
                    summary['drafted'] += 1
                continue
            if inflight_on:
                _wal_id = state_mod.begin_inflight(state, 'stall_nudge', chat_id, 1)
            if _nudge_context(ctx, chat_id=chat_id):
                # v1.21.0 (A1/O12): coerce the counters (this drain is outside
                # the per-chat guard) and drop the redundant re-lookup.
                entry['nudge_count'] = _as_int(entry.get('nudge_count', 0)) + 1
                entry['nudges_sent'] = _as_int(entry.get('nudges_sent', 0)) + 1
                entry['last_nudge_at'] = now_iso
                entry['status'] = STATUS_NUDGED
                entry['last_classification'] = (
                    f'Auto-nudged (attempt {entry["nudge_count"]}/{max_nudges})'
                )
                summary['nudged'] += 1
                summary['nudges_this_tick'] += 1
                stall_drained += 1
                if inflight_on:
                    state_mod.end_inflight(state, _wal_id)
                state_mod.append_history(state, {
                    'chat_id': chat_id,
                    'action': 'auto_nudge',
                    'nudge_count': entry['nudge_count'],
                    'timestamp': now_iso,
                })

    # v1.2.0 wedge drain: oldest freeze first, own small burst budget so a
    # wedge never starves behind the stalled queue. The last remediation
    # attempt of an episode escalates to the framework's ctx.nudge() (kill
    # the hung task + fw.msg_nudge.md) when wedge_soft_restart is enabled;
    # otherwise every attempt is a Tier 1 "continue" intervention nudge.
    if wedge_queue and wedge_max_remediations > 0:
        wedge_queue.sort(key=lambda item: item[0], reverse=True)
        wedge_budget = max(1, max_nudges_per_tick)
        for frozen_minutes, chat_id, ctx, entry, now_iso in wedge_queue:
            if summary['wedge_nudged'] + summary['wedge_restarts'] >= wedge_budget:
                break
            # v1.21.0 (A1/O12): `entry` is the live dict for this chat; the
            # wedge drain runs outside the per-chat guard, so its counters must
            # be coerced rather than trusted.
            prev = entry
            attempt = _as_int(prev.get('wedge_nudge_count', 0)) + 1
            final_attempt = attempt >= wedge_max_remediations
            # v1.10.0 liveness probe: a spinning dead loop never reaches a
            # safe point, so a continue intervention can never drain it and
            # would only burn budget. With the kill toggle on, go straight
            # to the task kill; without it the chat stays intervention for
            # a human without spending the episode budget.
            if prev.get('liveness', '') == 'spinning':
                if wedge_soft_restart and wedge_max_remediations > 0:
                    if inflight_on:
                        _wal_id = state_mod.begin_inflight(
                            state, 'wedge_soft_restart', chat_id, 2)
                    try:
                        ctx.nudge()
                        summary['wedge_restarts'] += 1
                        entry['wedge_nudge_count'] = attempt
                        entry['wedge_nudges_sent'] = _as_int(prev.get('wedge_nudges_sent', 0)) + 1
                        entry['last_wedge_nudge_at'] = now_iso
                        entry['status'] = STATUS_INTERVENTION
                        entry['last_classification'] = (
                            f'Wedged (spinning dead loop): killed task'
                            f' (attempt {attempt}/{wedge_max_remediations})'
                        )
                        state_mod.append_history(state, {
                            'chat_id': chat_id,
                            'action': 'wedge_soft_restart',
                            'wedge_nudge_count': attempt,
                            'liveness': 'spinning',
                            'frozen_minutes': round(frozen_minutes, 1),
                            'timestamp': now_iso,
                        })
                        if inflight_on:
                            state_mod.end_inflight(state, _wal_id)
                    except Exception as e:
                        _debug_log('wedge_restart_fail', chat_id + ' ' + repr(e))
                continue
            action = 'wedge_auto_continue'
            if (
                final_attempt
                and wedge_soft_restart
                and not prev.get('housekeeping_wedge')
            ):
                # Tier 2: kill_process() is a non-blocking future.cancel()
                # (helpers/defer.py), safe to call from this thread; on a
                # truly dead loop it is a harmless no-op and the chat stays
                # intervention for a human.
                if inflight_on:
                    _wal_id = state_mod.begin_inflight(state, action, chat_id, 2)
                try:
                    ctx.nudge()
                    action = 'wedge_soft_restart'
                    summary['wedge_restarts'] += 1
                except Exception as e:
                    _debug_log('wedge_restart_fail', chat_id + ' ' + repr(e))
                    continue
            else:
                if inflight_on:
                    _wal_id = state_mod.begin_inflight(
                        state, 'wedge_auto_continue', chat_id, 1)
                if not _nudge_context(ctx, WEDGE_NUDGE_TEXT, chat_id=chat_id):
                    continue
                summary['wedge_nudged'] += 1
            entry['wedge_nudge_count'] = attempt
            entry['wedge_nudges_sent'] = _as_int(prev.get('wedge_nudges_sent', 0)) + 1
            entry['last_wedge_nudge_at'] = now_iso
            entry['status'] = STATUS_INTERVENTION
            # v1.23.2 (audit fix 11, cycle 2): when the last attempt
            # escalated to a kill+restart, the label said 'auto-continue
            # sent' — the history row was correct, the entry text lied.
            entry['last_classification'] = (
                f'Wedged: kill+restart sent (attempt {attempt}'
                f'/{wedge_max_remediations})'
                if action == 'wedge_soft_restart'
                else f'Wedged: auto-continue sent (attempt {attempt}'
                f'/{wedge_max_remediations})'
            )
            if inflight_on:
                try:
                    state_mod.end_inflight(state, _wal_id)
                except Exception:
                    pass
            state_mod.append_history(state, {
                'chat_id': chat_id,
                'action': action,
                'wedge_nudge_count': attempt,
                'frozen_minutes': round(frozen_minutes, 1),
                'timestamp': now_iso,
            })

    # v1.23.0 feature C drain: exactly ONE soft restart per stall episode,
    # after the stall ladder exhausted its nudges (escalation instead of
    # giving up, opt-in via stall_escalate_to_restart). ctx.nudge() kills a
    # still-alive hung task AND re-sends the framework nudge prompt; on the
    # already-dead stalled task the kill is a no-op and the fresh chain just
    # carries the prompt. Budget 1 per episode (stall_restart_count), reset
    # when the chat recovers — a restart loop is structurally impossible.
    if stall_restart_queue:
        # v1.23.1 (audit A6): per-tick burst budget, mirroring the resume
        # drain (a mass stall — e.g. one that comes back with every chain
        # dead after a restart — must not spawn all chains in one tick).
        # max_nudges_per_tick=0 disables auto-nudging entirely, and this is
        # a nudge-family action, so 0 disables it too.
        restart_budget = (
            0 if max_nudges_per_tick <= 0
            else max(3, min(10, max_nudges_per_tick))
        )
        stall_restart_queue.sort(key=lambda item: item[0], reverse=True)
        restarts_delivered = 0
        for _sr_idle, chat_id, ctx, entry, now_iso in stall_restart_queue:
            if restarts_delivered >= restart_budget:
                break
            if _as_int(entry.get('stall_restart_count', 0)) >= 1:
                continue
            if inflight_on:
                _wal_id = state_mod.begin_inflight(state, 'stall_restart', chat_id, 1)
            try:
                ctx.nudge()
            except Exception as e:
                _debug_log('stall_restart_fail', chat_id + ' ' + repr(e))
                continue
            restarts_delivered += 1
            entry['nudge_count'] = _as_int(entry.get('nudge_count', 0)) + 1
            entry['nudges_sent'] = _as_int(entry.get('nudges_sent', 0)) + 1
            entry['stall_restart_count'] = (
                _as_int(entry.get('stall_restart_count', 0)) + 1
            )
            entry['last_nudge_at'] = now_iso
            entry['status'] = STATUS_NUDGED
            entry['last_classification'] = (
                f'Soft restart after nudges exhausted '
                f'(restart {entry["stall_restart_count"]}/1)'
            )
            if inflight_on:
                try:
                    state_mod.end_inflight(state, _wal_id)
                except Exception:
                    pass
            summary['stall_restarts'] += 1
            # v1.23.2 (audit fix 5, cycle 2): it IS a nudge-family send —
            # the dashboard's per-tick budget badge must count it.
            summary['nudges_this_tick'] += 1
            state_mod.append_history(state, {
                'chat_id': chat_id,
                'action': 'stall_restart',
                'timestamp': now_iso,
            })

    # v1.22.0 (stage 2): apply NEW blocked transitions, with a per-tick
    # budget (1-5, scaled by how many chats are already blocked) so one
    # chaotic tick cannot block half the fleet — the rest re-evaluate on
    # the next tick. A block is terminal until a human releases it (a turn
    # newer than blocked_at, or the /release endpoint).
    if block_queue:
        _already_blocked = sum(
            1 for _e in state.get('chats', {}).values()
            if isinstance(_e, dict) and _e.get('status') == STATUS_BLOCKED
        )
        _block_budget = planner_mod.block_budget(_already_blocked)
        for _b_at, _b_id, _b_reason, _b_entry in block_queue:
            if _block_budget <= 0:
                break
            if _b_entry.get('status') == STATUS_BLOCKED:
                continue
            _b_entry['status'] = STATUS_BLOCKED
            _b_entry['blocked_reason'] = str(_b_reason)[:300]
            _b_entry['blocked_at'] = _b_at
            _b_entry['supervisor_settled'] = False
            # A fresh block supersedes any earlier human release: the
            # supervisor judged a human is needed again, so the old release
            # no longer speaks for this verdict.
            _b_entry['released_signature'] = ''
            _block_budget -= 1
            summary['blocked'] += 1
            state_mod.append_history(state, {
                'chat_id': _b_id,
                'action': 'blocked',
                'reason': str(_b_reason)[:200],
                'timestamp': _b_at,
            })
            if notify_on_intervention:
                _b_name = _chat_display_name(_b_id, None, _b_entry)
                if not _notify(
                    'warning',
                    f'Chat {_b_name!r} ({_b_id}) blocked for a human: {_b_reason}',
                    priority='high',
                    cfg=cfg,
                    state=state,
                    chat_id=_b_id,
                ):
                    summary['notify_failures'] += 1

    if server_restarted:
        state_mod.append_history(state, {
            'chat_id': '',
            'action': 'restart_detected',
            'detail': (
                str(summary['interrupted']) + ' interrupted chats found, '
                + str(summary['resumed']) + ' resume nudges sent'
            ),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })
        if summary['resumed'] and notify_on_resume:
            name_list = ', '.join(resumed_names[:5]) + ('...' if len(resumed_names) > 5 else '')
            msg = (
                'Server restarted: resumed ' + str(summary['resumed'])
                + ' of ' + str(summary['interrupted']) + ' interrupted chat(s)'
                + (': ' + name_list if name_list else '')
            )
            if not _notify('info', msg, cfg=cfg, state=state):
                summary['notify_failures'] += 1

    for chat_id, entry in list(state.get('chats', {}).items()):
        # v1.20.2: load_state() already repairs non-dict entries; this guard
        # keeps the reset loop safe for any state mutated in-process.
        if not isinstance(entry, dict):
            continue
        if entry.get('status') in (STATUS_RUNNING, STATUS_AWAITING_USER, STATUS_PAUSED):
            # v1.18.2 fix (terminate-loop, part 3): a recent termination
            # stamp means the last "running" interval ended in a security
            # termination / error stop — resetting the nudge budget now
            # would let the next stall cycle nudge→terminate forever
            # (max_auto_nudges never actually capped anything). The budget
            # stays consumed until the stamp expires or a human returns.
            if _termination_cooldown_active(entry):
                continue
            entry['nudge_count'] = 0
            entry['last_nudge_at'] = ''
            # v1.23.0: the one-restart-per-episode budget clears with the
            # recovery, like the other ladder budgets.
            entry['stall_restart_count'] = 0
            # v1.2.0: wedge budget clears on recovery (crediting already
            # happened in the frozen-clock reset above, when applicable).
            entry['wedge_nudge_count'] = 0
            entry['last_wedge_nudge_at'] = ''
            # v1.22.0 (stage 1): the wedged-completion subclass mark expires
            # with the recovery — the drain's no-kill guard reads it, so a
            # stale True must never exempt a later run-context freeze from
            # the tier-2 kill.
            entry.pop('housekeeping_wedge', None)

    # P1: prune state entries that are neither live nor persisted chats
    # (the chat dir is gone and no context exists -> dead history), then cap
    # the dict. v1.20.0 (P8): the cap is live-aware - live contexts are
    # never evicted while dead entries remain, and dead surplus / live
    # eviction are journaled separately. The old lumped "dead/stale"
    # message hid the churn storm where the same live chats were evicted
    # and re-created every tick, wiping their nudge/cooldown bookkeeping.
    # v1.23.2 (audit fix 14, cycle 2): a dead BLOCKED chat is never
    # re-ticked (no live context), so its `last_ticked` ages and it is
    # ALWAYS the next cap-eviction victim — silently dropping a human's
    # pending block + released_signature. While its dir exists, refresh
    # the clock (state_signature ignores per-chat last_ticked: no churn).
    _cap_now = datetime.now(timezone.utc).isoformat()
    for _bcid, _bentry in (state.get('chats') or {}).items():
        if (
            isinstance(_bentry, dict)
            and _bentry.get('status') == STATUS_BLOCKED
            and _bcid not in live_contexts
            and _has_chat_dir(_bcid)
        ):
            _bentry['last_ticked'] = _cap_now
    pruned_dead = 0
    for chat_id in list(state.get('chats', {}).keys()):
        if chat_id in live_contexts:
            continue
        if not _has_chat_dir(chat_id):
            state['chats'].pop(chat_id, None)
            pruned_dead += 1
    dropped = state_mod.cap_chats(state, MAX_TRACKED_CHATS, protected=set(live_contexts))
    summary['pruned'] = pruned_dead + len(dropped)
    summary['cap_evicted'] = len(dropped)
    if pruned_dead:
        state_mod.append_history(state, {
            'chat_id': '',
            'action': 'prune_state',
            'detail': f'{pruned_dead} dead/stale chat entries removed',
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })
    if dropped:
        state_mod.append_history(state, {
            'chat_id': '',
            'action': 'cap_evicted',
            'detail': f'{len(dropped)} dead chat entries evicted (tracked-chats cap)',
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })

    # v1.12.0 (P5): persist the per-tick throttle snapshot for the
    # dashboard - delivered vs budget this tick, whether the stalled
    # queue was truncated by the budget (storm visibility), plus resume
    # and wedge drain usage. Unknown top-level keys round-trip through
    # load/save, so no state.py schema change is needed.
    # v1.21.0 (A7): `_stalled_delivered` was `summary['nudged'] - pre_drain_...`
    # where the subtracted snapshot was always 0 - it is exactly the count this
    # drain delivered.
    _stalled_delivered = summary['nudged']
    state['throttle'] = {
     'nudges_this_tick': summary['nudges_this_tick'],
     'budget': max_nudges_per_tick,
     # v1.21.0 (A6): "capped" must mean "the budget suppressed nudges".
     # Under supervised mode nothing is auto-sent at all - the queue drains
     # into editable drafts and every queued chat is delivered for review -
     # so the `max_nudges_per_tick <= 0` branch used to raise a red "capped"
     # badge on a run where no nudge was ever suppressed (v1.16.0 made
     # supervised mode explicitly budget-free). Only a real budget or a real
     # truncation counts as capped; drafts are reported separately below.
     'capped': bool(
      nudge_queue_len
      and not supervised
      and (
       max_nudges_per_tick <= 0
       or (
        stall_drained >= max_nudges_per_tick
        and nudge_queue_len > _stalled_delivered
       )
      )
     ),
     'supervised': bool(supervised),
     'stalled_queued': nudge_queue_len,
     'stalled_nudged': _stalled_delivered,
     'resume_nudged': summary['resumed'],
     'wedge_nudged': summary['wedge_nudged'],
     'wedge_restarts': summary['wedge_restarts'],
     'wedge_budget': max(1, max_nudges_per_tick),
     # v1.23.2 (audit fix 5, cycle 2): stall-restart deliveries + budget.
     'stall_restarts': summary['stall_restarts'],
     'restart_budget': 0 if max_nudges_per_tick <= 0 else max(3, min(10, max_nudges_per_tick)),
     # v1.20.0: tracked-chats maintenance, split so the dashboard can tell
     # dead-chat pruning (routine) from dead-surplus cap eviction (only
     # possible when dead entries outnumber the cap). A non-zero
     # cap_evicted on a busy instance is the visible proof that LIVE
     # contexts are being protected - before v1.20.0 this churn showed up
     # as a constant prune_state count instead.
     'pruned': pruned_dead,
     'cap_evicted': len(dropped),
     'timestamp': datetime.now(timezone.utc).isoformat(),
    }
    # v1.17.0 durability: skip the full state.json rewrite when nothing
    # semantic changed. The save fires when the interval elapsed
    # (periodic flush of volatile dashboard bookkeeping) OR the durable
    # payload signature (chats minus volatile last_ticked, drafts,
    # adaptive overlay) differs from the last written one, so every
    # meaningful state transition persists immediately.
    # v1.22.0 (stage 5): the continuity wake — the one action that can bring
    # work into existence instead of reacting to what it can already see.
    _wake_plan = {'action': None, 'reason': '', 'skipped': ''}
    try:
        if bool(cfg.get('continuity_enabled', False)):
            _wake_obs = []
            _wake_c = state.get('continuity') if isinstance(
                state.get('continuity'), dict) else {}
            for _wcid, _went in state.get('chats', {}).items():
                if not isinstance(_went, dict):
                    continue
                _wst = _went.get('status')
                if _wst in (STATUS_AWAITING_USER, STATUS_STALLED,
                            STATUS_INTERVENTION, STATUS_ERROR, STATUS_BLOCKED):
                    _wo = {
                        'chat_id': _wcid,
                        'status': _wst,
                        'nudge_count': _went.get('nudge_count', 0),
                        'minutes_frozen': 0.0,
                    }
                    if (
                        bool(cfg.get('goal_integration', True))
                        and _wst == STATUS_AWAITING_USER
                    ):
                        _wo['goal_open'] = caps_mod.active_goal(_wcid) is not None
                    _wake_obs.append(_wo)
            _wake_plan = planner_mod.plan_wake(
                _wake_obs, state, cfg,
                due=_continuity_due, within_active_hours=_within_hours,
                grace_ok=_grace_ok,
            )
            _skip_reason = str(_wake_plan.get('skipped') or '')
            if _skip_reason:
                if _skip_reason != str(_wake_c.get('last_skip') or ''):
                    _wake_c['last_skip'] = _skip_reason
                    state_mod.append_history(state, {
                        'chat_id': '',
                        'action': 'continuity_skipped',
                        'reason': _skip_reason,
                        'timestamp': datetime.now(timezone.utc).isoformat(),
                    })
            else:
                _wake_c['last_skip'] = ''
                _wd = wake_mod.fire_wake(cfg)
                wake_mod.record_wake(state, _wd, str(_wake_plan.get('reason') or ''))
                state_mod.append_history(state, {
                    'chat_id': '',
                    'action': ('continuity_wake' if _wd.get('status') == 'waked'
                               else 'continuity_skipped'),
                    'status': str(_wd.get('status') or ''),
                    'via': str(_wd.get('via') or ''),
                    'reason': str(_wake_plan.get('reason') or ''),
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                })
                if _wd.get('status') == 'waked':
                    summary['wakes'] = summary.get('wakes', 0) + 1
    except Exception as _wk_exc:
        _debug_log('wake_phase_fail', repr(_wk_exc))

    # v1.22.0 (stage 3): the self-check phase — judge this tick's facts and
    # page a human when a fault persists (info findings never page). The
    # failure counter is read HERE and cleared after persisting, so the
    # recovery tick reports the failures that preceded it and only then
    # clears them (T11C ordering from the donor).
    _sc_prev_failed = selfcheck_mod.as_int(
        (state.get('selfcheck') or {}).get('consecutive_tick_failures'), 0
    ) if isinstance(state.get('selfcheck'), dict) else 0
    try:
        # v1.23.0: did alerts fire in the last 24h, and does any external
        # channel exist to have carried them? Feeds the no_alert_channel
        # info finding — dashboard-only alerting becomes visible.
        _alerts_recent = 0
        _alerts_map = state.get('alerts')
        if isinstance(_alerts_map, dict):
            for _row in _alerts_map.values():
                if not isinstance(_row, dict):
                    continue
                try:
                    _row_dt = _parse_dt(str(_row.get('last_at', '') or ''))
                except Exception:
                    _row_dt = None
                if _row_dt is not None and _minutes_since(_row_dt) < 1440.0:
                    _alerts_recent += 1
        _findings = selfcheck_mod.evaluate(
            {
                'last_tick': _prev_tick_iso,
                'consecutive_tick_failures': _sc_prev_failed,
                'inflight': state.get('inflight') or [] if isinstance(
                    state.get('inflight'), list) else [],
                'channels': state.get('channels') or {},
                'capabilities': state.get('capabilities') or {},
                'live_contexts': len(live_contexts),
                'checked': summary.get('checked', 0),
                'save_failures': _save_failures,
                'action_cap_reached': False,
                'blocked_external': len(_ext_blocks),
                'alerts_recent': _alerts_recent,
                'channels_configured': bool(notify_mod.configured_channels(cfg)),
                'wake_cap_reached': bool(cfg.get('continuity_enabled', False))
                and planner_mod.wakes_used(state)
                >= max(0, int(_as_float(cfg.get('continuity_max_wakes_per_day'), 12))),
                'wake_ceiling': max(0, int(_as_float(
                    cfg.get('continuity_max_wakes_per_day'), 12))),
            },
            cfg,
        )
        _persist_selfcheck(state, cfg, _findings)
        # (the counter reset lives inside _persist_selfcheck so a tick that
        # did not stamp cannot conjure a stamp of zeros)
    except Exception as _sc_exc:
        _debug_log('selfcheck_fail', repr(_sc_exc))
        _findings = []
    if _findings:
        page_ok, page_reason = selfcheck_mod.should_page(
            _findings, state.get('selfcheck') or {}, cfg
        )
        if page_ok:
            if not _notify(
                'error' if any(
                    str(r.get('severity')) == selfcheck_mod.SEVERITY_ERROR
                    for r in _findings
                ) else 'warning',
                'Chat Shepherd self-check: ' + page_reason,
                priority='high',
                cfg=cfg,
                state=state,
            ):
                summary['notify_failures'] += 1
            sc_stamp = state.get('selfcheck')
            if isinstance(sc_stamp, dict):
                sc_stamp['paged_at'] = datetime.now(timezone.utc).isoformat()

    # save_state_interval_seconds=0 restores legacy write-every-tick.
    # state.py gates fail safe: any error there forces a write.
    _save_interval = 120.0
    try:
        _save_interval = float(cfg.get('save_state_interval_seconds', 120) or 0)
    except Exception:
        _save_interval = 120.0
    if _save_interval < 0:
        _save_interval = 0.0
    _do_save = state_mod.save_due(_save_interval)
    if not _do_save:
        try:
            _do_save = (
                state_mod.state_signature(state)
                != state_mod.last_saved_signature()
            )
        except Exception:
            _do_save = True
    state['throttle']['state_save_skipped'] = not _do_save
    summary['state_save_skipped'] = not _do_save
    if _do_save:
        # v1.22.0 (stage 3): a failing write feeds the self-check (module
        # global - the blob holding the counter cannot hold its own failure).
        try:
            state_mod.save_state(state)
            _save_failures = 0
        except Exception as e:
            _save_failures += 1
            _debug_log('state_save_fail', repr(e))
    return summary
