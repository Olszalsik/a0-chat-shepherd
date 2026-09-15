from __future__ import annotations

from typing import Any

from helpers.api import ApiHandler, Request, Response
from helpers import plugins

from usr.plugins.chat_shepherd.helpers.constants import CHAT_ID_PATTERN, PLUGIN_NAME
from usr.plugins.chat_shepherd.helpers.state import load_state
from usr.plugins.chat_shepherd.helpers import monitor
from usr.plugins.chat_shepherd.helpers import hotreload
from usr.plugins.chat_shepherd.helpers import adaptive
from usr.plugins.chat_shepherd.helpers import aggregates

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


def _to_int(value: Any) -> int:
 try:
  return max(0, int(value))
 except (TypeError, ValueError):
  return 0


def _resolve_icons(cfg: dict[str, Any]) -> dict[str, str]:
    icons = dict(DEFAULT_ICONS)
    custom = cfg.get('icons')
    if isinstance(custom, dict):
        for key, value in custom.items():
            if key in icons:
                icons[key] = str(value)[:16]
    return icons


def _is_tracked_chat(chat_id: str) -> bool:
    # P6.4: the exact predicate tick() applies - framework id pattern
    # (8 alphanumerics) plus a persisted usr/chats/<id> transcript dir.
    if not CHAT_ID_PATTERN.match(chat_id or ''):
        return False
    try:
        return monitor._has_chat_dir(chat_id)
    except Exception:
        return False


def _tracked_contexts(raw: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {cid: info for cid, info in raw.items() if _is_tracked_chat(cid)}


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

        # R2: nudge-effectiveness per chat + global aggregate (v1.2.0 also
        # carries the wedge remediation ladder counters).
        for c in chat_list:
            src = chats_state.get(c['chat_id'], {})
            sent = src.get('nudges_sent', 0)
            eff = src.get('nudges_effective', 0)
            c['nudges_sent'] = sent
            c['nudges_effective'] = eff
            c['hit_rate'] = round(100.0 * eff / sent) if sent else None
            c['wedge_nudge_count'] = src.get('wedge_nudge_count', 0)
            c['wedge_nudges_sent'] = src.get('wedge_nudges_sent', 0)
            c['wedge_nudges_effective'] = src.get('wedge_nudges_effective', 0)
            c['liveness'] = src.get('liveness', '')
        _total_sent = sum(c['nudges_sent'] for c in chat_list)
        _total_eff = sum(c['nudges_effective'] for c in chat_list)
        effectiveness = {
            'sent': _total_sent,
            'effective': _total_eff,
            'hit_rate': round(100.0 * _total_eff / _total_sent) if _total_sent else None,
            'wedge_sent': sum(c['wedge_nudges_sent'] for c in chat_list),
            'wedge_effective': sum(c['wedge_nudges_effective'] for c in chat_list),
        }
        # R4: adaptive-threshold learning reflection (learned overlay).
        try:
         _ad = state.get('adaptive')
         _ad = _ad if isinstance(_ad, dict) else {}
         _learned = _ad.get('learned')
         if cfg.get('adaptive_thresholds', False):
          _vals = adaptive.effective_values(cfg, _learned)
         else:
          _vals = adaptive.configured_values(cfg)
         effectiveness['adaptive'] = {
          'enabled': bool(cfg.get('adaptive_thresholds', False)),
          'values': _vals,
          'last_eval': _ad.get('last_eval', ''),
          'window_start': _ad.get('window_start', ''),
         }
        except Exception:
         pass
        # R4: dashboard aggregates (pure journal-tail math, no extra I/O).
        try:
            aggregates_block = aggregates.compute(state.get('history'))
        except Exception:
            aggregates_block = None
        counts: dict[str, int] = {}
        for c in chat_list:
            counts[c['status']] = counts.get(c['status'], 0) + 1

        # v1.12.0: per-tick throttle snapshot persisted by tick(); old state
        # files fall back to zeroed defaults with a config-derived budget.
        _th_raw = state.get('throttle')
        _th = _th_raw if isinstance(_th_raw, dict) else {}
        try:
         _th_budget = max(0, min(10, int(_th.get('budget', cfg.get('max_nudges_per_tick', 1)))))
        except (TypeError, ValueError):
         _th_budget = 1
        throttle = {
         'nudges_this_tick': _to_int(_th.get('nudges_this_tick')),
         'budget': _th_budget,
         'capped': bool(_th.get('capped', False)),
         'stalled_queued': _to_int(_th.get('stalled_queued')),
         'stalled_nudged': _to_int(_th.get('stalled_nudged')),
         'resume_nudged': _to_int(_th.get('resume_nudged')),
         'wedge_this_tick': _to_int(_th.get('wedge_nudged')) + _to_int(_th.get('wedge_restarts')),
         'wedge_budget': max(1, _th_budget),
         'timestamp': str(_th.get('timestamp', '') or ''),
        }
        poll_seconds = cfg.get('poll_seconds', 5)
        try:
            poll_seconds = int(poll_seconds)
        except (TypeError, ValueError):
            poll_seconds = 5
        poll_seconds = max(2, min(120, poll_seconds))
        return {
            'success': True,
            'config': {
                'enabled': bool(cfg.get('enabled', False)),
                'stall_minutes': cfg.get('stall_minutes', 5),
                'max_auto_nudges': cfg.get('max_auto_nudges', 3),
                'max_nudges_per_tick': cfg.get('max_nudges_per_tick', 1),
                'nudge_cooldown_minutes': cfg.get('nudge_cooldown_minutes', 10),
                'wedge_nudge_after_minutes': cfg.get('wedge_nudge_after_minutes', 10),
                'wedge_max_remediations': cfg.get('wedge_max_remediations', 2),
                'wedge_remediation_cooldown_minutes': cfg.get(
                    'wedge_remediation_cooldown_minutes', 10
                ),
                'wedge_soft_restart': bool(cfg.get('wedge_soft_restart', False)),
                'wedge_liveness_probe': bool(cfg.get('wedge_liveness_probe', True)),
                'icons': icons,
                'poll_seconds': poll_seconds,
                'hot_reload_enabled': bool(cfg.get('hot_reload_enabled', True)),
                'adaptive_thresholds': bool(cfg.get('adaptive_thresholds', False)),
                'adaptive_interval_minutes': cfg.get('adaptive_interval_minutes', 60),
                'adaptive_min_samples': cfg.get('adaptive_min_samples', 10),
                'adaptive_step_pct': cfg.get('adaptive_step_pct', 10),
            },
            'last_tick': state.get('last_tick', ''),
    'hot_reload': hotreload.status(),
            'chats': chat_list,
            'counts': counts,
            'effectiveness': effectiveness,
            'throttle': throttle,
            'aggregates': aggregates_block,
            'history': state.get('history', [])[:20],
        }
