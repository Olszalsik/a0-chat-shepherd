from __future__ import annotations

from typing import Any

from helpers.api import ApiHandler, Request, Response
from helpers import plugins

from usr.plugins.chat_shepherd.helpers.constants import PLUGIN_NAME
from usr.plugins.chat_shepherd.helpers.state import load_state
from usr.plugins.chat_shepherd.helpers import monitor

DEFAULT_ICONS: dict[str, str] = {
    'running': '🏃',
    'stalled': '⚠️',
    'nudged': '🔄',
    'intervention': '🚨',
    'interrupted': '🔌',
    'awaiting_user': '💬',
    'error': '❌',
    'paused': '⏸️',
    'idle': '',
}


def _resolve_icons(cfg: dict[str, Any]) -> dict[str, str]:
    icons = dict(DEFAULT_ICONS)
    custom = cfg.get('icons')
    if isinstance(custom, dict):
        for key, value in custom.items():
            if key in icons:
                icons[key] = str(value)[:16]
    return icons


def _serialize_context(ctx: Any) -> dict[str, Any]:
    is_running = False
    if hasattr(ctx, 'is_running'):
        is_running = bool(ctx.is_running())
    else:
        is_running = bool(getattr(ctx, 'running', False))
    return {
        'id': getattr(ctx, 'id', ''),
        'name': getattr(ctx, 'name', '') or getattr(ctx, 'id', ''),
        'running': is_running,
        'paused': bool(getattr(ctx, 'paused', False)),
        'last_message': str(getattr(ctx, 'last_message', '') or ''),
        'ctx': ctx,
    }


class Status(ApiHandler):
    async def process(self, input: dict, request: Request) -> dict | Response:
        cfg = plugins.get_plugin_config(PLUGIN_NAME) or {}
        state = load_state()
        icons = _resolve_icons(cfg)

        contexts: dict[str, dict[str, Any]] = {}
        try:
            from agent import AgentContext
            for ctx in AgentContext.all():
                info = _serialize_context(ctx)
                contexts[info['id']] = info
        except Exception:
            pass

        chats_state: dict[str, Any] = state.get('chats', {})
        chat_list: list[dict[str, Any]] = []
        seen: set[str] = set()

        # Live contexts first — always freshly classified, so the UI never
        # shows a stale pictogram, and brand-new chats appear immediately.
        for chat_id, info in contexts.items():
            prev = chats_state.get(chat_id, {})
            ctx_obj = info.pop('ctx')
            try:
                # Pass the frozen-log clock so a wedged context shows as
                # intervention in the UI too, matching what tick() decided.
                frozen_minutes = monitor._wedge_minutes(ctx_obj, prev)
                live_status, reason = monitor.classify_chat(
                    ctx_obj, chat_id, cfg, prev, frozen_minutes
                )
            except Exception:
                live_status, reason = prev.get('status', 'idle'), ''
            chat_list.append({
                'chat_id': chat_id,
                'status': live_status,
                'nudge_count': prev.get('nudge_count', 0),
                'last_nudge_at': prev.get('last_nudge_at', ''),
                'last_classification': reason or prev.get('last_classification', ''),
                'last_log_type': prev.get('last_log_type', ''),
                'error_detected': prev.get('error_detected', False),
                'running': info['running'],
                'paused': info['paused'],
                'name': info['name'] or chat_id,
            })
            seen.add(chat_id)

        # Persisted entries whose context is no longer live: their stored
        # status is stale by definition, so re-derive it instead of
        # trusting state.json (this is what previously left a stuck ⏸️).
        for chat_id, entry in chats_state.items():
            if chat_id in seen:
                continue
            try:
                if monitor._check_error_file(chat_id):
                    fallback_status = 'error'
                else:
                    fallback_status = 'idle'
            except Exception:
                fallback_status = 'idle'
            chat_list.append({
                'chat_id': chat_id,
                'status': fallback_status,
                'nudge_count': entry.get('nudge_count', 0),
                'last_nudge_at': entry.get('last_nudge_at', ''),
                'last_classification': entry.get('last_classification', ''),
                'last_log_type': entry.get('last_log_type', ''),
                'error_detected': entry.get('error_detected', False),
                'running': False,
                'paused': False,
                'name': entry.get('name', '') or chat_id,
            })

        status_order = {
            'intervention': 0,
            'error': 1,
            'interrupted': 2,
            'stalled': 3,
            'nudged': 4,
            'running': 5,
            'awaiting_user': 6,
            'paused': 7,
            'idle': 8,
        }
        chat_list.sort(key=lambda c: status_order.get(c['status'], 99))

        counts: dict[str, int] = {}
        for c in chat_list:
            counts[c['status']] = counts.get(c['status'], 0) + 1

        return {
            'success': True,
            'config': {
                'enabled': bool(cfg.get('enabled', False)),
                'stall_minutes': cfg.get('stall_minutes', 5),
                'max_auto_nudges': cfg.get('max_auto_nudges', 3),
                'max_nudges_per_tick': cfg.get('max_nudges_per_tick', 1),
                'nudge_cooldown_minutes': cfg.get('nudge_cooldown_minutes', 10),
                'icons': icons,
            },
            'last_tick': state.get('last_tick', ''),
            'chats': chat_list,
            'counts': counts,
            'history': state.get('history', [])[:20],
        }
