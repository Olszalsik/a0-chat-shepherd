from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from helpers import files
from agent import AgentContext

from usr.plugins.chat_shepherd.helpers.constants import (
    STATUS_RUNNING,
    STATUS_STALLED,
    STATUS_NUDGED,
    STATUS_INTERVENTION,
    STATUS_AWAITING_USER,
    STATUS_ERROR,
    STATUS_PAUSED,
    STATUS_IDLE,
    NUDGE_TEXT,
    PLUGIN_NAME,
)
from usr.plugins.chat_shepherd.helpers import state as state_mod


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _minutes_since(dt: datetime | None) -> float:
    if dt is None:
        return 9999.0
    return (_now() - dt).total_seconds() / 60.0


def _check_error_file(chat_id: str) -> bool:
    try:
        chat_dir = files.get_abs_path(f'usr/chats/{chat_id}')
        if not chat_dir:
            return False
        error_path = files.join_paths(chat_dir, 'error.txt')
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


def _nudge_context(context: AgentContext, text: str | None = None) -> bool:
    try:
        from helpers.messages import UserMessage
        msg = UserMessage(text or NUDGE_TEXT)
        context.communicate(msg)
        return True
    except Exception:
        return False


def classify_chat(
    context: AgentContext | None,
    chat_id: str,
    cfg: dict[str, Any],
    prev_entry: dict[str, Any],
) -> tuple[str, str]:
    if context is None:
        if _check_error_file(chat_id):
            return STATUS_ERROR, 'Chat has error.txt but no active context'
        return STATUS_IDLE, 'No active context'

    if context.paused:
        return STATUS_PAUSED, 'Context is paused'

    if context.is_running():
        return STATUS_RUNNING, 'Agent is actively running'

    if _check_error_file(chat_id):
        return STATUS_ERROR, 'Chat has error.txt'

    last_log_type = _last_log_type(context)
    last_msg_dt = _parse_dt(getattr(context, 'last_message', ''))
    minutes_idle = _minutes_since(last_msg_dt)

    if last_log_type == 'response':
        return STATUS_AWAITING_USER, 'Agent finished response, waiting for user'

    stall_minutes = float(cfg.get('stall_minutes', 5))
    nudge_count = prev_entry.get('nudge_count', 0)
    max_nudges = int(cfg.get('max_auto_nudges', 3))

    if minutes_idle > stall_minutes:
        if nudge_count >= max_nudges:
            return STATUS_INTERVENTION, f'Stalled for {minutes_idle:.0f}m, exhausted {max_nudges} nudges'
        if nudge_count > 0:
            return STATUS_STALLED, f'Stalled for {minutes_idle:.0f}m, nudge {nudge_count}/{max_nudges} used'
        return STATUS_STALLED, f'Stalled for {minutes_idle:.0f}m, no nudges yet'

    return STATUS_IDLE, f'Idle {minutes_idle:.0f}m, within grace period'


def tick(cfg: dict[str, Any]) -> dict[str, Any]:
    if not cfg.get('enabled', False):
        return {'skipped': True, 'reason': 'disabled'}

    state = state_mod.load_state()
    state['last_tick'] = datetime.now(timezone.utc).isoformat()

    nudge_cooldown = float(cfg.get('nudge_cooldown_minutes', 10))
    max_nudges = int(cfg.get('max_auto_nudges', 3))
    notify_on_intervention = bool(cfg.get('notify_on_intervention', True))

    live_contexts: dict[str, Any] = {}
    try:
        for ctx in AgentContext.all():
            live_contexts[ctx.id] = ctx
    except Exception:
        pass

    summary = {
        'checked': 0,
        'running': 0,
        'stalled': 0,
        'nudged': 0,
        'intervention': 0,
        'error': 0,
        'awaiting': 0,
        'idle': 0,
        'paused': 0,
    }

    for chat_id, ctx in live_contexts.items():
        summary['checked'] += 1
        prev = state_mod.get_chat(state, chat_id)
        new_status, reason = classify_chat(ctx, chat_id, cfg, prev)
        now_iso = datetime.now(timezone.utc).isoformat()
        old_status = prev.get('status', '')

        entry = state_mod.update_chat(
            state,
            chat_id,
            status=new_status,
            last_classification=reason,
            last_log_type=_last_log_type(ctx) if ctx else '',
        )

        if new_status == STATUS_RUNNING:
            entry['last_seen_running'] = now_iso
            summary['running'] += 1
        elif new_status == STATUS_PAUSED:
            summary['paused'] += 1
        elif new_status == STATUS_ERROR:
            entry['error_detected'] = True
            summary['error'] += 1
        elif new_status == STATUS_AWAITING_USER:
            summary['awaiting'] += 1
        elif new_status == STATUS_STALLED:
            last_nudge_dt = _parse_dt(prev.get('last_nudge_at', ''))
            cooldown_ok = _minutes_since(last_nudge_dt) >= nudge_cooldown
            if cooldown_ok:
                nudged_ok = _nudge_context(ctx)
                if nudged_ok:
                    entry['nudge_count'] = prev.get('nudge_count', 0) + 1
                    entry['last_nudge_at'] = now_iso
                    entry['status'] = STATUS_NUDGED
                    entry['last_classification'] = (
                        f'Auto-nudged (attempt {entry["nudge_count"]}/{max_nudges})'
                    )
                    summary['nudged'] += 1
                    state_mod.append_history(state, {
                        'chat_id': chat_id,
                        'action': 'auto_nudge',
                        'nudge_count': entry['nudge_count'],
                        'timestamp': now_iso,
                    })
                else:
                    summary['stalled'] += 1
            else:
                summary['stalled'] += 1
        elif new_status == STATUS_INTERVENTION:
            summary['intervention'] += 1
            if old_status != STATUS_INTERVENTION and notify_on_intervention:
                try:
                    from helpers.notification import (
                        NotificationManager,
                        NotificationPriority,
                    )
                    nm = NotificationManager()
                    nm.notify(
                        title='Chat Shepherd',
                        message=f'Chat {chat_id} needs human intervention: {reason}',
                        priority=NotificationPriority.HIGH,
                    )
                except Exception:
                    pass
        elif new_status == STATUS_IDLE:
            summary['idle'] += 1

    for chat_id, entry in list(state.get('chats', {}).items()):
        if entry.get('status') in (STATUS_RUNNING, STATUS_AWAITING_USER, STATUS_PAUSED):
            entry['nudge_count'] = 0
            entry['last_nudge_at'] = ''

    state_mod.save_state(state)
    return summary
