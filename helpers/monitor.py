from __future__ import annotations

import os
import threading
import json
import re
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
    NUDGE_TEXT,
    RESUME_TEXT,
    WEDGE_NUDGE_TEXT,
    WEDGE_NUDGE_AFTER_MIN,
    WEDGE_MAX_REMEDIATIONS,
    WEDGE_REMEDIATION_COOLDOWN_MIN,
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

# The job loop ticks roughly every 60s; a last_tick gap far beyond one
# tick interval means the server process (or at least the job loop) was
# restarted and the in-memory contexts were rebuilt from disk.
RESTART_GAP_MINUTES = 5.0


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


def _minutes_since(dt: datetime | None) -> float:
    if dt is None:
        return 9999.0
    return (_now() - dt).total_seconds() / 60.0


def _check_error_file(chat_id: str) -> bool:
    try:
        chat_dir = files.get_abs_path(f'usr/chats/{chat_id}')
        if not chat_dir:
            return False
        error_path = os.path.join(chat_dir, 'error.txt')
        return files.exists(error_path)
    except Exception:
        return False


def _last_log_type(context: AgentContext) -> str:
    try:
        with context.log._lock:
            logs = list(context.log.logs)
        for item in reversed(logs):
            if item.type in ('response', 'tool', 'user', 'error'):
                return item.type
    except Exception:
        pass
    return ''


def _last_entry_type(context: AgentContext) -> str:
    """Type of the very last log entry (unfiltered)."""
    try:
        with context.log._lock:
            logs = list(context.log.logs)
            if logs:
                return str(getattr(logs[-1], 'type', '') or '')
    except Exception:
        pass
    return ''


def _log_len(context: AgentContext) -> int:
    """Number of log entries right now (-1 = unknown, disables wedge detection)."""
    try:
        with context.log._lock:
            return len(context.log.logs)
    except Exception:
        return -1


def _has_chat_dir(chat_id: str) -> bool:
    """P1: a real UI chat persists its transcript under usr/chats/<id>.

    Two guards, both cheap and 9p-bounded:
    1. the id must match the framework's pattern (AgentContext.generate_id
       always yields exactly 8 alphanumerics) — script-created throwaway
       contexts ("verify-1788992102", "ctx-hook-1") pass an explicit id= and
       never match, even though message_async DOES persist their chat dir;
    2. the usr/chats/<id> directory must exist (ad-hoc contexts that never
       persist are invisible here). files.exists is the 9p-bounded guard, so
       this never wedges the JobLoop thread on a lost stat.
    """
    try:
        if not CHAT_ID_PATTERN.match(chat_id or ''):
            return False
        return bool(files.exists(files.get_abs_path(f'usr/chats/{chat_id}')))
    except Exception:
        return False


def _wedge_minutes(context: AgentContext, prev_entry: dict[str, Any]) -> float:
    """P3: frozen-log minutes for a context (mirror of tick()'s clock).

    Read-only companion for the live status endpoint; tick() owns the
    log_len_since mutation.
    """
    cur = _log_len(context)
    prev_len = prev_entry.get('last_log_len', -1)
    if not (
        context.is_running() and cur >= 0 and prev_len >= 0 and cur == prev_len
    ):
        return 0.0
    since = _parse_dt(prev_entry.get('log_len_since', ''))
    if since is None:
        return 0.0
    return _minutes_since(since)


def _debug_log(kind: str, message: str) -> None:
    # R2: no more silently swallowed exceptions - kind-tagged,
    # daily-rotated debug log under the plugin data dir.
    try:
        day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        rel = 'usr/plugins/chat_shepherd/data/debug-' + day + '.log'
        line = (
            datetime.now(timezone.utc).isoformat() + ' ' + kind + ' ' + str(message) + '\n'
        )
        with open(files.get_abs_path(rel), 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception:
        pass


_NAME_CACHE: dict[str, tuple[Any, str]] = {}
_NAME_CACHE_MAX = 120

def _read_chat_title(chat_id: str) -> str:
    # Human-readable chat title from usr/chats/<id>/chat.json.
    # The 'name' field sits in the first bytes, so a bounded head
    # read is enough - never parse the embedded context payload.
    path = files.get_abs_path('usr/chats/' + chat_id + '/chat.json')
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
        path = files.get_abs_path('usr/chats/' + chat_id + '/chat.json')
        try:
            st = os.stat(path)
            key = (st.st_mtime, st.st_size)
        except OSError:
            key = None
        cached = _NAME_CACHE.get(chat_id)
        if key is not None and cached and cached[0] == key:
            name = cached[1]
        elif key is not None:
            name = _read_chat_title(chat_id)
            if len(_NAME_CACHE) >= _NAME_CACHE_MAX and chat_id not in _NAME_CACHE:
                _NAME_CACHE.clear()
            _NAME_CACHE[chat_id] = (key, name)
    name = name.strip()[:80]
    return name if name else chat_id


def _post_json(url: str, payload: dict, timeout: float = 5.0) -> bool:
    # R3: minimal JSON POST for external alert channels. Returns
    # True on 2xx; kept separate so tests can monkeypatch it.
    import requests
    resp = requests.post(url, json=payload, timeout=timeout)
    return 200 <= resp.status_code < 300

def _dispatch_external(cfg: dict, kind: str, message: str, priority: str = 'normal', sync: bool = False) -> None:
    # R3: fan the alert out to optional external channels (generic
    # webhook + Telegram). URL validation happens on the calling
    # (tick) thread because it is cheap; the actual POST runs in a
    # daemon thread so the event-loop tick never blocks. sync=True
    # is for tests only.
    if not isinstance(cfg, dict):
        return
    jobs: list[tuple[str, str, dict]] = []
    webhook_url = str(cfg.get('webhook_url') or '').strip()
    if webhook_url and not bool(cfg.get('webhook_allow_private', False)):
        try:
            from helpers.network import validate_public_http_url
            validate_public_http_url(webhook_url)
        except Exception as e:
            _debug_log('webhook_skip', 'URL rejected: ' + repr(e) + ' :: ' + webhook_url)
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
    if token and tg_chat:
        jobs.append((
            'telegram',
            'https://api.telegram.org/bot' + token + '/sendMessage',
            {'chat_id': tg_chat, 'text': '[Chat Shepherd/' + kind + '] ' + message},
        ))
    if not jobs:
        return

    def _run() -> None:
        for channel, url, payload in jobs:
            try:
                if not _post_json(url, payload):
                    _debug_log(channel + '_fail', 'non-2xx :: ' + message)
            except Exception as e:
                _debug_log(channel + '_fail', repr(e) + ' :: ' + message)

    if sync:
        _run()
    else:
        threading.Thread(target=_run, daemon=True, name='chat_shepherd_alert').start()

def _notify(kind: str, message: str, priority: str = 'normal', cfg: dict | None = None) -> bool:
    # R2/R3: central notification helper. Returns True when the
    # in-framework notification was accepted; failures are logged,
    # never silently swallowed. R3: also fans out to optional
    # external channels (webhook/Telegram) without blocking.
    fw_ok = False
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
            ntype, npri, message, title='Chat Shepherd'
        )
        fw_ok = True
    except Exception as e:
        _debug_log('notify_fail', repr(e) + ' :: ' + message)
    try:
        if cfg is None:
            from helpers import plugins as _plugins
            from usr.plugins.chat_shepherd.helpers.constants import PLUGIN_NAME as _PN
            cfg = _plugins.get_plugin_config(_PN) or {}
        _dispatch_external(cfg, kind, message, priority)
    except Exception as e:
        _debug_log('alert_dispatch_fail', repr(e))
    return fw_ok


def _record_nudge_outcome(state: dict, chat_id: str, now_iso: str) -> None:
    # R2 nudge effectiveness: when a nudged chat reaches running or
    # awaiting_user within NUDGE_EFFECTIVE_WINDOW_MIN minutes of its
    # last nudge, credit that nudge as effective and log it.
    entry = state_mod.get_chat(state, chat_id)
    if entry.get('nudge_count', 0) <= 0 or not entry.get('last_nudge_at', ''):
        return
    nudged_at = _parse_dt(entry.get('last_nudge_at', ''))
    if nudged_at is None:
        return
    minutes = _minutes_since(nudged_at)
    if minutes > NUDGE_EFFECTIVE_WINDOW_MIN:
        return
    entry['nudges_effective'] = entry.get('nudges_effective', 0) + 1
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
    if entry.get('wedge_nudge_count', 0) <= 0 or not entry.get('last_wedge_nudge_at', ''):
        return
    nudged_at = _parse_dt(entry.get('last_wedge_nudge_at', ''))
    if nudged_at is None:
        return
    minutes = _minutes_since(nudged_at)
    entry['wedge_nudge_count'] = 0
    entry['last_wedge_nudge_at'] = ''
    if minutes > NUDGE_EFFECTIVE_WINDOW_MIN:
        return
    entry['wedge_nudges_effective'] = entry.get('wedge_nudges_effective', 0) + 1
    entry['last_wedge_outcome_at'] = now_iso
    state_mod.append_history(state, {
        'chat_id': chat_id,
        'action': 'wedge_nudge_effective',
        'minutes_after_nudge': round(minutes, 1),
        'timestamp': now_iso,
    })


def _nudge_context(context: AgentContext, text: str | None = None) -> bool:
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
        try:
            import os
            from datetime import datetime as _dt
            dbg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'nudge_debug.log')
            with open(dbg, 'a', encoding='utf-8') as f:
                f.write(f"{_dt.now().isoformat()} auto_nudge error: {e!r}\n")
        except Exception:
            pass
        return False


def classify_chat(
    context: AgentContext | None,
    chat_id: str,
    cfg: dict[str, Any],
    prev_entry: dict[str, Any],
    frozen_minutes: float = 0.0,
) -> tuple[str, str]:
    if context is None:
        if _check_error_file(chat_id):
            return STATUS_ERROR, 'Chat has error.txt but no active context'
        return STATUS_IDLE, 'No active context'

    if context.paused:
        return STATUS_PAUSED, 'Context is paused'

    if context.is_running():
        # P3 (v1.1.0), superseded in v1.2.0: frozen != stalled. A running
        # context whose log stopped growing for stall_minutes is wedged and
        # classified as intervention — but it is no longer left for a human
        # only: communicate() on an alive task sets agent.intervention (a
        # plain attribute write that drains at the agent's next safe point),
        # the same mechanics that make a manual "continue" unwedge these
        # chats. tick() runs the v1.2.0 remediation ladder on them.
        stall_minutes = float(cfg.get('stall_minutes', 5))
        if frozen_minutes >= stall_minutes:
            reason = (
                f'Agent appears wedged: running but log frozen for {frozen_minutes:.0f}m'
            )
            wedge_count = int(prev_entry.get('wedge_nudge_count', 0) or 0)
            max_remediations = max(0, min(5, int(cfg.get(
                'wedge_max_remediations', WEDGE_MAX_REMEDIATIONS
            ))))
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

    last_log_type = _last_log_type(context)
    last_msg_dt = _parse_dt(getattr(context, 'last_message', ''))
    minutes_idle = _minutes_since(last_msg_dt)

    if last_log_type == '':
        return STATUS_IDLE, 'New chat, no messages yet'

    if last_log_type == 'response':
        return STATUS_AWAITING_USER, 'Agent finished response, waiting for user'

    stall_minutes = float(cfg.get('stall_minutes', 5))
    nudge_count = prev_entry.get('nudge_count', 0)
    max_nudges = int(cfg.get('max_auto_nudges', 3))

    if minutes_idle > stall_minutes:
        if nudge_count >= max_nudges:
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


def _last_response_text(context: AgentContext) -> str:
    # v1.8.0: heading + content of the most recent response log item
    # (completion-claim evidence). Same lock discipline as _last_log_type.
    try:
        with context.log._lock:
            logs = list(context.log.logs)
        for item in reversed(logs):
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
) -> bool:
    # v1.8.0 goal completion gate: silent nudge when a USER chat's latest
    # response claims completion while its native goal is still open.
    # Budget (goal_gate_count) applies per goal version and resets when
    # the goal record changes (goal_gate_key = goal updated_at). Returns
    # True when a nudge was sent.
    if not bool(cfg.get('goal_gate_enabled', True)):
        return False
    max_nudges = max(0, min(10, int(cfg.get(
        'goal_gate_max_nudges', GOAL_GATE_MAX_NUDGES
    ))))
    if max_nudges <= 0:
        return False
    cooldown = float(cfg.get(
        'goal_gate_cooldown_minutes', GOAL_GATE_COOLDOWN_MIN
    ))
    goal = _active_goal(chat_id)
    if not goal:
        return False
    if not _looks_like_completion(_last_response_text(ctx)):
        return False
    goal_key = str(goal.get('updated_at') or goal.get('objective') or '')
    if str(entry.get('goal_gate_key', '')) != goal_key:
        entry['goal_gate_key'] = goal_key
        entry['goal_gate_count'] = 0
    count = int(entry.get('goal_gate_count', 0) or 0)
    if count >= max_nudges:
        return False
    if cooldown > 0 and _minutes_since(
        _parse_dt(entry.get('last_goal_gate_at', ''))
    ) < cooldown:
        return False
    objective = str(goal.get('objective', ''))[:GOAL_GATE_OBJECTIVE_MAX]
    if not _nudge_context(
        ctx, GOAL_GATE_TEXT.replace('{objective}', objective)
    ):
        return False
    entry['goal_gate_count'] = count + 1
    entry['last_goal_gate_at'] = now_iso
    entry['goal_gate_nudges_sent'] = (
        int(entry.get('goal_gate_nudges_sent', 0) or 0) + 1
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
    if not cfg.get('enabled', False):
        return {'skipped': True, 'reason': 'disabled'}

    state = state_mod.load_state()
    prev_last_tick = _parse_dt(state.get('last_tick', ''))
    # Restart detection: a last_tick gap far beyond one tick interval
    # means the server (or job loop) restarted and contexts were
    # rebuilt from disk. A missing last_tick (first ever tick) is not
    # a restart.
    server_restarted = (
        prev_last_tick is not None
        and _minutes_since(prev_last_tick) > RESTART_GAP_MINUTES
    )
    state['last_tick'] = datetime.now(timezone.utc).isoformat()

    nudge_cooldown = float(cfg.get('nudge_cooldown_minutes', 10))
    max_nudges = int(cfg.get('max_auto_nudges', 3))
    notify_on_intervention = bool(cfg.get('notify_on_intervention', True))
    # v1.7.0 quiet bell: persistence-gated intervention paging.
    notify_after = float(cfg.get('notify_after_minutes', NOTIFY_AFTER_MIN))
    notify_rearm = float(cfg.get('notify_rearm_minutes', NOTIFY_REARM_MIN))
    notify_on_resume = bool(cfg.get('notify_on_resume', False))
    # v1.8.0 goal completion gate (thresholds read inside the check).
    goal_gate_enabled = bool(cfg.get('goal_gate_enabled', True))
    # P2: global burst throttle — at most this many auto-nudges per tick,
    # oldest stall first. 0 disables auto-nudging entirely.
    max_nudges_per_tick = max(0, min(10, int(cfg.get('max_nudges_per_tick', 1))))

    # v1.2.0 wedge remediation ladder. wedge_max_remediations 0 disables
    # wedge auto-remediation entirely (chats stay intervention-only).
    wedge_nudge_after = float(cfg.get('wedge_nudge_after_minutes', WEDGE_NUDGE_AFTER_MIN))
    wedge_max_remediations = max(0, min(5, int(cfg.get(
        'wedge_max_remediations', WEDGE_MAX_REMEDIATIONS
    ))))
    wedge_cooldown = float(cfg.get(
        'wedge_remediation_cooldown_minutes', WEDGE_REMEDIATION_COOLDOWN_MIN
    ))
    wedge_soft_restart = bool(cfg.get('wedge_soft_restart', False))

    # P1: only real UI chats are tracked. Contexts without a persisted
    # usr/chats/<id> directory (ad-hoc message_async ids) are invisible here.
    watch_all = bool(cfg.get('watch_all', True))
    allowed_chat_ids = cfg.get('allowed_chat_ids') or []
    live_contexts: dict[str, Any] = {}
    try:
        for ctx in AgentContext.all():
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
        'error': 0,
        'awaiting': 0,
        'idle': 0,
        'paused': 0,
        'nudges_this_tick': 0,
        'interrupted': 0,
        'resumed': 0,
        'notify_failures': 0,
        'goal_gate_nudged': 0,
        'pruned': 0,
    }

    # P2: stalled chats that are cooldown-eligible, collected for the
    # post-loop throttle pass (never nudged inline in this loop).
    nudge_queue: list[tuple[float, str, Any, dict, float]] = []
    resume_queue: list[tuple[float, str, Any, dict, str, Any]] = []
    resumed_names: list[str] = []
    wedge_queue: list[tuple[float, str, Any, dict, float]] = []

    for chat_id, ctx in live_contexts.items():
        summary['checked'] += 1
        now_iso = datetime.now(timezone.utc).isoformat()
        prev = state_mod.get_chat(state, chat_id)
        old_status = prev.get('status', '')

        # P3 wedge detection: capture the log length and compare with the
        # previous tick. Unchanged length while running => start/continue the
        # frozen clock; any growth resets it.
        cur_log_len = _log_len(ctx)
        prev_log_len = prev.get('last_log_len', -1)
        frozen_minutes = 0.0
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
        else:
            prev['log_len_since'] = ''
            # v1.2.0: the log grew again (or the task stopped) — a pending
            # wedge remediation either worked or is moot; credit it if recent.
            _record_wedge_outcome(state, chat_id, now_iso)

        new_status, reason = classify_chat(ctx, chat_id, cfg, prev, frozen_minutes)

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
            and _last_entry_type(ctx) != 'response'
        ):
            new_status = STATUS_INTERRUPTED
            reason = (
                f'Server restarted while chat was {old_status}; '
                'mid-task work interrupted'
            )

        fname = _chat_display_name(chat_id, ctx, prev)
        entry = state_mod.update_chat(
            state,
            chat_id,
            name=fname,
            status=new_status,
            last_classification=reason,
            last_log_type=_last_log_type(ctx) if ctx else '',
            last_log_len=cur_log_len,
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
                    state, chat_id, ctx, entry, cfg, now_iso, summary
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
            nudge_count = prev.get('nudge_count', 0)
            if cooldown_ok and nudge_count < max_nudges:
                # Oldest stall first; final ordering happens post-loop.
                nudge_queue.append((minutes_idle, chat_id, ctx, entry, now_iso))
        elif new_status == STATUS_INTERVENTION:
            summary['intervention'] += 1
            if 'log frozen' in reason:
                summary['wedged'] += 1
                # v1.2.0 remediation ladder: USER chats frozen past the grace
                # period get an automatic "continue" (Tier 1), with the
                # framework's ctx.nudge() (kill + fw.msg_nudge.md) available
                # as the final attempt when wedge_soft_restart is enabled.
                # Scheduler TASK contexts are excluded — the scheduler
                # resumes its own task chats on its own cadence.
                wedge_count = int(prev.get('wedge_nudge_count', 0) or 0)
                last_wedge_dt = _parse_dt(prev.get('last_wedge_nudge_at', ''))
                wedge_cooldown_ok = _minutes_since(last_wedge_dt) >= wedge_cooldown
                if (
                    wedge_max_remediations > 0
                    and wedge_count < wedge_max_remediations
                    and wedge_cooldown_ok
                    and frozen_minutes >= wedge_nudge_after
                    and getattr(ctx, 'type', None) == AgentContextType.USER
                ):
                    # Oldest freeze first; final ordering post-loop.
                    wedge_queue.append((frozen_minutes, chat_id, ctx, entry, now_iso))
            # v1.7.0 quiet bell: persistence-gated paging. A fresh episode starts
            # the clock silently; the bell rings only once the chat has
            # persistently needed human help for >= notify_after minutes
            # (0 = page immediately). notify_rearm re-pages an ongoing
            # episode (0 = one page per episode). Self-healed episodes (e.g.
            # the wedge auto-continue worked) never page.
            if notify_on_intervention:
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
                            ):
                            summary['notify_failures'] += 1
                        entry['intervention_notify_at'] = now_iso
        elif new_status == STATUS_INTERRUPTED:
            summary['interrupted'] += 1
            nudge_count = entry.get('nudge_count', 0)
            if nudge_count < max_nudges:
                minutes_idle = _minutes_since(_parse_dt(getattr(ctx, 'last_message', '')))
                resume_queue.append(
                    (minutes_idle, chat_id, ctx, entry, now_iso, RESUME_TEXT)
                )
        elif new_status == STATUS_IDLE:
            summary['idle'] += 1

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
            if _nudge_context(ctx, text):
                prev = state_mod.get_chat(state, chat_id)
                entry['nudge_count'] = prev.get('nudge_count', 0) + 1
                entry['nudges_sent'] = entry.get('nudges_sent', 0) + 1
                entry['last_nudge_at'] = now_iso
                entry['status'] = STATUS_NUDGED
                entry['last_classification'] = (
                    'Resume nudge after restart (attempt '
                    + str(entry['nudge_count']) + '/' + str(max_nudges) + ')'
                )
                summary['resumed'] += 1
                resumed_names.append(_chat_display_name(chat_id, ctx, entry))
                summary['nudges_this_tick'] += 1
                state_mod.append_history(state, {
                    'chat_id': chat_id,
                    'action': 'restart_resume',
                    'nudge_count': entry['nudge_count'],
                    'timestamp': now_iso,
                })

    if nudge_queue and max_nudges_per_tick > 0:
        nudge_queue.sort(key=lambda item: item[0], reverse=True)
        for minutes_idle, chat_id, ctx, entry, now_iso in nudge_queue:
            if summary['nudges_this_tick'] >= max_nudges_per_tick:
                break
            if _nudge_context(ctx):
                prev = state_mod.get_chat(state, chat_id)
                entry['nudge_count'] = prev.get('nudge_count', 0) + 1
                entry['nudges_sent'] = entry.get('nudges_sent', 0) + 1
                entry['last_nudge_at'] = now_iso
                entry['status'] = STATUS_NUDGED
                entry['last_classification'] = (
                    f'Auto-nudged (attempt {entry["nudge_count"]}/{max_nudges})'
                )
                summary['nudged'] += 1
                summary['nudges_this_tick'] += 1
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
            prev = state_mod.get_chat(state, chat_id)
            attempt = prev.get('wedge_nudge_count', 0) + 1
            final_attempt = attempt >= wedge_max_remediations
            action = 'wedge_auto_continue'
            if final_attempt and wedge_soft_restart:
                # Tier 2: kill_process() is a non-blocking future.cancel()
                # (helpers/defer.py), safe to call from this thread; on a
                # truly dead loop it is a harmless no-op and the chat stays
                # intervention for a human.
                try:
                    ctx.nudge()
                    action = 'wedge_soft_restart'
                    summary['wedge_restarts'] += 1
                except Exception as e:
                    _debug_log('wedge_restart_fail', chat_id + ' ' + repr(e))
                    continue
            else:
                if not _nudge_context(ctx, WEDGE_NUDGE_TEXT):
                    continue
                summary['wedge_nudged'] += 1
            entry['wedge_nudge_count'] = attempt
            entry['wedge_nudges_sent'] = prev.get('wedge_nudges_sent', 0) + 1
            entry['last_wedge_nudge_at'] = now_iso
            entry['status'] = STATUS_INTERVENTION
            entry['last_classification'] = (
                f'Wedged: auto-continue sent (attempt {attempt}'
                f'/{wedge_max_remediations})'
            )
            state_mod.append_history(state, {
                'chat_id': chat_id,
                'action': action,
                'wedge_nudge_count': attempt,
                'frozen_minutes': round(frozen_minutes, 1),
                'timestamp': now_iso,
            })

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
            if not _notify('info', msg, cfg=cfg):
                summary['notify_failures'] += 1

    for chat_id, entry in list(state.get('chats', {}).items()):
        if entry.get('status') in (STATUS_RUNNING, STATUS_AWAITING_USER, STATUS_PAUSED):
            entry['nudge_count'] = 0
            entry['last_nudge_at'] = ''
            # v1.2.0: wedge budget clears on recovery (crediting already
            # happened in the frozen-clock reset above, when applicable).
            entry['wedge_nudge_count'] = 0
            entry['last_wedge_nudge_at'] = ''

    # P1: prune state entries that are neither live nor persisted chats
    # (the chat dir is gone and no context exists -> dead history), then cap
    # the dict to the most recently ticked MAX_TRACKED_CHATS entries.
    pruned = 0
    for chat_id in list(state.get('chats', {}).keys()):
        if chat_id in live_contexts:
            continue
        if not _has_chat_dir(chat_id):
            state['chats'].pop(chat_id, None)
            pruned += 1
    dropped = state_mod.cap_chats(state, MAX_TRACKED_CHATS)
    pruned += len(dropped)
    if pruned:
        summary['pruned'] = pruned
        state_mod.append_history(state, {
            'chat_id': '',
            'action': 'prune_state',
            'detail': f'{pruned} dead/stale chat entries removed',
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })

    state_mod.save_state(state)
    return summary
