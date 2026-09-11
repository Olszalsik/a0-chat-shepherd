from __future__ import annotations

import os
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


def _notify(kind: str, message: str, priority: str = 'normal') -> bool:
    # R2: central notification helper. Returns True when accepted;
    # failures are logged, never silently swallowed.
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
        return True
    except Exception as e:
        _debug_log('notify_fail', repr(e) + ' :: ' + message)
        return False


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
        'pruned': 0,
    }

    # P2: stalled chats that are cooldown-eligible, collected for the
    # post-loop throttle pass (never nudged inline in this loop).
    nudge_queue: list[tuple[float, str, Any, dict, float]] = []
    resume_queue: list[tuple[float, str, Any, dict, str, Any]] = []
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

        entry = state_mod.update_chat(
            state,
            chat_id,
            status=new_status,
            last_classification=reason,
            last_log_type=_last_log_type(ctx) if ctx else '',
            last_log_len=cur_log_len,
            last_ticked=now_iso,
        )

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
            if old_status != STATUS_INTERVENTION and notify_on_intervention:
                if not _notify(
                    'warning',
                    f'Chat {chat_id} needs human intervention: {reason}',
                    priority='high',
                ):
                    summary['notify_failures'] += 1
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
        if summary['resumed'] and notify_on_intervention:
            msg = (
                'Server restarted: resumed ' + str(summary['resumed'])
                + ' of ' + str(summary['interrupted']) + ' interrupted chat(s)'
            )
            if not _notify('info', msg):
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
