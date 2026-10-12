from __future__ import annotations

import asyncio
import os

from typing import Any

from helpers.api import ApiHandler, Request, Response

from usr.plugins.chat_shepherd.helpers import constants
from usr.plugins.chat_shepherd.helpers.constants import CHAT_ID_PATTERN, JOURNAL_KEEP
# v1.21.0 (A8): state is reached through the MODULE, never through a pinned
# `from ...state import load_state, read_journal`. Those bindings survive a
# `helpers/state.py` hot-reload, so /status kept running the OLD load_state
# while the tick (module-attr) ran the new one - two generations of state code
# inside a single request, and the reload was silently a no-op for this
# endpoint. Same fix in api/export.py and api/history.py.
from usr.plugins.chat_shepherd.helpers import state as state_mod
from usr.plugins.chat_shepherd.helpers import selfcheck as selfcheck_mod
from usr.plugins.chat_shepherd.helpers import notify as _notify_mod
from usr.plugins.chat_shepherd.helpers import monitor
from usr.plugins.chat_shepherd.helpers import hotreload
from usr.plugins.chat_shepherd.helpers import adaptive
from usr.plugins.chat_shepherd.helpers import aggregates

# v1.21.0 (O2): aggregates are a pure function of the journal, so they only
# need recomputing when the journal actually changed. Keyed on the same stat
# tuple `read_journal` uses internally plus the requested limit.
_AGG_CACHE: dict[str, Any] = {'key': None, 'value': None}


def _aggregates_block() -> dict[str, Any]:
    entries = state_mod.read_journal(JOURNAL_KEEP)
    try:
        st = os.stat(state_mod.files.get_abs_path(state_mod.JOURNAL_FILE))
        key = (st.st_mtime_ns, st.st_size, JOURNAL_KEEP)
    except OSError:
        key = ('nostat', len(entries), JOURNAL_KEEP)
    if _AGG_CACHE['key'] == key and isinstance(_AGG_CACHE['value'], dict):
        return _AGG_CACHE['value']
    block = aggregates.compute(entries)
    _AGG_CACHE['key'] = key
    _AGG_CACHE['value'] = block
    return block

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


def _to_int(value: Any, fallback: int = 0) -> int:
    """Coerce a persisted field for the payload without ever raising.

    v1.21.0 (cycle 3): OverflowError joins the pair, for the same reason it
    did in `state._as_int` and `monitor._as_int` - `int(float('inf'))` raises
    it, and a JSON body or a hand-edited state file can carry a non-finite
    float. It matters here because the two unguarded reads in `_build_snapshot`
    (`int(_th.get('budget', ...))` and `int(poll_seconds)`) sit in the block
    the v1.21.0 degrade guard covers, so the symptom was a `degraded: true`
    payload instead of a working status - i.e. this seam's own guard firing on
    its own unguarded neighbour.

    `fallback` exists because a caller may need to tell "unusable" from a
    legitimate 0 (a `budget` of 0 means auto-nudging off, and substituting the
    default for it would paint a false "capped"/budget-full reading).
    """
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return max(0, int(fallback))


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
        # v1.18.1: everything in _snapshot_sync is BLOCKING work —
        # state.json + journal reads, per-chat stat probes, context log
        # locks. It used to run directly on the request event loop while
        # every open tab polls it every poll_seconds, so one slow stat
        # (bounded files.exists can block up to 5s on a degraded Docker
        # 9p mount) froze the whole loop — which surfaced in the browser
        # as "CSRF token request timed out" (the CSRF fetch deadline is
        # also 5s). Offload to a worker thread; response shape unchanged.
        return await asyncio.to_thread(self._snapshot_sync)

    def _snapshot_sync(self) -> dict | Response:
        """Guarded entry point (v1.21.0, A2).

        Every open tab polls this endpoint every `poll_seconds`, and the
        sidebar icons depend on it, so a 500 here is not one broken card: it
        freezes every chat icon on stale data for the rest of the page
        session, and the store only toasts once. Per-chat classification was
        already guarded, but the effectiveness / adaptive / throttle / payload
        blocks above it were not, so one malformed field anywhere in
        state.json took the whole endpoint down.

        A failure now degrades to a minimal, still-valid payload instead of a
        500, and records the reason so the UI's own failure path can show it.
        """
        try:
            return self._build_snapshot()
        except Exception as exc:  # noqa: BLE001 - a poll must never 500
            return {
                'success': True,
                'degraded': True,
                'error': str(exc)[:200],
                'config': {
                    'enabled': True,
                    'icons': dict(DEFAULT_ICONS),
                    'poll_seconds': 5,
                },
                'last_tick': '',
                'hot_reload': {},
                'chats': [],
                'counts': {},
                'effectiveness': {
                    'sent': 0,
                    'effective': 0,
                    'hit_rate': None,
                    'wedge_sent': 0,
                    'wedge_effective': 0,
                },
                'throttle': {
                    'nudges_this_tick': 0,
                    'budget': 1,
                    'capped': False,
                    'stalled_queued': 0,
                    'stalled_nudged': 0,
                    'resume_nudged': 0,
                    'wedge_this_tick': 0,
                    'wedge_budget': 1,
                    'pruned': 0,
                    'cap_evicted': 0,
                    'timestamp': '',
                },
                'aggregates': None,
                'drafts': [],
                'history': [],
            }

    def _build_snapshot(self) -> dict | Response:
        # v1.20.3: defaults are re-applied locally, so a get_plugin_config
        # hook regression cannot hand this endpoint a partial config.
        cfg = monitor._load_config()
        state = state_mod.load_state()
        icons = _resolve_icons(cfg)

        contexts: dict[str, dict[str, Any]] = {}
        try:
            from agent import AgentContext
            for ctx in AgentContext.all():
                # v1.20.2: apply the SAME real-chat predicate tick() uses.
                # /status used to list every live AgentContext, so
                # script-created throwaway contexts (verify-*, ctx-hook-1)
                # appeared in the dashboard and counts even though the
                # shepherd refuses to nudge or track them - the user then
                # got "not a tracked chat" from /nudge for a row the UI
                # had just offered them.
                if not _is_tracked_chat(getattr(ctx, 'id', '')):
                    continue
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
            # v1.21.0 (O5): one log snapshot per chat for this pass, shared by
            # the frozen clock and the classifier - /status used to take the
            # agent's log lock twice per chat per poll, per open tab.
            view = monitor._log_snapshot(ctx_obj)
            try:
                # Pass the frozen-log clock so a wedged context shows as
                # intervention in the UI too, matching what tick() decided.
                frozen_minutes = monitor._wedge_minutes(ctx_obj, prev, view)
                live_status, reason = monitor.classify_chat(
                    ctx_obj, chat_id, cfg, prev, frozen_minutes, view=view
                )
            except Exception:
                live_status, reason = prev.get('status', 'idle'), ''
            chat_list.append({
                'chat_id': chat_id,
                'status': live_status,
                'nudge_count': _to_int(prev.get('nudge_count', 0)),
                'last_nudge_at': str(prev.get('last_nudge_at') or ''),
                'last_classification': reason or str(prev.get('last_classification', '') or ''),
                'last_log_type': str(prev.get('last_log_type', '') or ''),
                'error_detected': bool(prev.get('error_detected', False)),
                # v1.22.0 (stage 2): cause + block evidence for the UI.
                'category': str(prev.get('category', 'none') or 'none'),
                'category_label': constants.CATEGORY_LABELS.get(
                    str(prev.get('category', 'none') or 'none'), ''),
                'blocked_reason': str(prev.get('blocked_reason', '') or ''),
                'blocked_at': str(prev.get('blocked_at', '') or ''),
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
            # v1.22.0 (stage 2): a blocked persisted entry stays blocked even
            # without a live context — that is exactly the shape of a chat
            # blocked for a human that no one has attended to yet.
            if str(entry.get('status', '') or '') == 'blocked':
                fallback_status = 'blocked'
            _cat_entry = str(entry.get('category', 'none') or 'none')
            chat_list.append({
                'chat_id': chat_id,
                'status': fallback_status,
                'nudge_count': _to_int(entry.get('nudge_count', 0)),
                'last_nudge_at': str(entry.get('last_nudge_at') or ''),
                'last_classification': str(entry.get('last_classification', '') or ''),
                'last_log_type': str(entry.get('last_log_type', '') or ''),
                'error_detected': bool(entry.get('error_detected', False)),
                'category': _cat_entry,
                'category_label': constants.CATEGORY_LABELS.get(_cat_entry, ''),
                'blocked_reason': str(entry.get('blocked_reason', '') or ''),
                'blocked_at': str(entry.get('blocked_at', '') or ''),
                'running': False,
                'paused': False,
                'name': str(entry.get('name', '') or '') or chat_id,
            })

        status_order = {
            'intervention': 0,
            'blocked': 1,
            'error': 2,
            'interrupted': 3,
            'stalled': 4,
            'nudged': 5,
            'running': 6,
            'awaiting_user': 7,
            'paused': 8,
            'idle': 9,
        }
        chat_list.sort(key=lambda c: status_order.get(c['status'], 99))

        # R2: nudge-effectiveness per chat + global aggregate (v1.2.0 also
        # carries the wedge remediation ladder counters).
        for c in chat_list:
            src = chats_state.get(c['chat_id'], {})
            # v1.21.0 (A1): every counter read is coerced. state.json is
            # hand-editable and survives upgrades, so these values can be a
            # string or None - `100.0 * eff / sent` and the four sums below
            # then raised TypeError and 500'd the whole poll, taking the
            # sidebar icons down with it.
            sent = _to_int(src.get('nudges_sent', 0))
            eff = _to_int(src.get('nudges_effective', 0))
            c['nudges_sent'] = sent
            c['nudges_effective'] = eff
            c['hit_rate'] = round(100.0 * eff / sent) if sent else None
            c['wedge_nudge_count'] = _to_int(src.get('wedge_nudge_count', 0))
            c['wedge_nudges_sent'] = _to_int(src.get('wedge_nudges_sent', 0))
            c['wedge_nudges_effective'] = _to_int(src.get('wedge_nudges_effective', 0))
            c['liveness'] = str(src.get('liveness', '') or '')
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
        # R4: dashboard aggregates. v1.20.7: computed from the journal read
        # directly, NOT from state['history'] — load_state() fills that
        # mirror with JOURNAL_READ_LIMIT (200) entries, so on a busy instance
        # the advertised 7-day window covered only a few hours and silently
        # undercounted every metric. read_journal is (mtime,size)-keyed
        # cached, so this costs no extra parse; JOURNAL_KEEP is the full
        # journal (the same limit api/export.py uses).
        try:
            aggregates_block = _aggregates_block()
        except Exception:
            aggregates_block = None
        counts: dict[str, int] = {}
        for c in chat_list:
            counts[c['status']] = counts.get(c['status'], 0) + 1

        # v1.12.0: per-tick throttle snapshot persisted by tick(); old state
        # files fall back to zeroed defaults with a config-derived budget.
        _th_raw = state.get('throttle')
        _th = _th_raw if isinstance(_th_raw, dict) else {}
        # v1.21.0 (cycle 3): the persisted budget no longer needs its own
        # try/except - it had the same two-arm handler that missed
        # OverflowError, so a non-finite budget degraded the whole snapshot.
        # The fallback is the CONFIG-derived default, and `_to_int` takes an
        # explicit fallback precisely so an unusable value can be told apart
        # from a legitimate 0 (0 = auto-nudging off).
        _budget_default = _to_int(cfg.get('max_nudges_per_tick', 1), 1) or 1
        _th_budget = max(0, min(10, _to_int(_th.get('budget'), _budget_default)))
        throttle = {
         'nudges_this_tick': _to_int(_th.get('nudges_this_tick')),
         'budget': _th_budget,
         'capped': bool(_th.get('capped', False)),
         'stalled_queued': _to_int(_th.get('stalled_queued')),
         'stalled_nudged': _to_int(_th.get('stalled_nudged')),
         'resume_nudged': _to_int(_th.get('resume_nudged')),
         'wedge_this_tick': _to_int(_th.get('wedge_nudged')) + _to_int(_th.get('wedge_restarts')),
         'wedge_budget': max(1, _th_budget),
         # v1.20.0: dead-chat pruning vs dead-surplus cap eviction, kept
         # separate so the dashboard can show that live chats are protected.
         'pruned': _to_int(_th.get('pruned')),
         'cap_evicted': _to_int(_th.get('cap_evicted')),
         'timestamp': str(_th.get('timestamp', '') or ''),
        }
        # Same story as the budget: `_to_int`'s explicit fallback separates
        # "unusable" from a legitimate 0, and 0 must clamp UP to the 2 s floor
        # rather than collapse to the default (an `or 5` would).
        poll_seconds = max(2, min(120, _to_int(cfg.get('poll_seconds'), 5)))
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
                'supervised_mode': bool(cfg.get('supervised_mode', False)),
                'save_state_interval_seconds': _to_int(cfg.get('save_state_interval_seconds', 120)),
            },
            'last_tick': state.get('last_tick', ''),
            # v1.22.0 (stage 3): the supervisor's own health stamp.
            'channels': _notify_mod.channel_view(
                state,
                _to_int(cfg.get('notify_degraded_after_failures', 3)),
            ),
            'inflight': [dict(i) for i in (state.get('inflight') or [])
                         if isinstance(i, dict)],
            'health': selfcheck_mod.health_summary(
                state.get('selfcheck') or {},
                (state.get('selfcheck') or {}).get('findings') or [],
            ),
    'hot_reload': hotreload.status(),
            'chats': chat_list,
            'counts': counts,
            'effectiveness': effectiveness,
            'throttle': throttle,
            'aggregates': aggregates_block,
            'drafts': state.get('drafts') if isinstance(state.get('drafts'), list) else [],
            'history': state.get('history', [])[:20],
        }
