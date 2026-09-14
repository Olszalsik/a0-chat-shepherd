"""Adaptive thresholds (R4 feedback loop): learn the timing thresholds from
the accumulated nudge / wedge remediation outcomes.

v1.12.0, roadmap R4. Conservative by design:
- opt-in (`adaptive_thresholds: false` default);
- runtime-only overlay: learned values live in state.json under the
  `adaptive` key and are applied inside ``monitor.tick()`` — the persisted
  plugin config is never modified;
- bounded: every tunable keeps hard clamps and moves at most
  `adaptive_step_pct` percent per evaluation (minimum step 1 minute);
- gated: an evaluation runs at most every `adaptive_interval_minutes`
  AND only counts outcomes recorded since the previous evaluation
  (journal-windowed rates — stale history cannot re-titrate old data);
- respectful: a manual settings change re-seeds the learned baseline
  from the configured values on the next tick (settings always win),
  and a configured value outside the clamp pins that knob (never touched);
- never touches Tier-2 (task kill), budgets, notification cadence or
  the liveness probe.

Rate semantics (per window, from the journal):
- nudge family: sent = `auto_nudge` + `restart_resume`, outcome =
  `nudge_effective` -> tunes `nudge_cooldown_minutes`
  (high rate = relax: re-nudge sooner; low rate = tighten: back off).
- wedge family: sent = `wedge_auto_continue` + `wedge_soft_restart`,
  outcome = `wedge_nudge_effective` -> tunes
  `wedge_nudge_after_minutes` + `wedge_remediation_cooldown_minutes`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from usr.plugins.chat_shepherd.helpers import state as state_mod

# tunable -> hard (lo, hi) clamp in minutes
CLAMPS: dict[str, tuple[float, float]] = {
    'nudge_cooldown_minutes': (5.0, 60.0),
    'wedge_nudge_after_minutes': (5.0, 120.0),
    'wedge_remediation_cooldown_minutes': (5.0, 120.0),
}
TUNABLES = tuple(CLAMPS.keys())

GOOD_RATE = 0.7  # >= this: relax (remediate sooner)
BAD_RATE = 0.4   # < this: tighten (back off)

# journal actions per metric family
_ACTIONS: dict[str, tuple[set, set]] = {
    'nudge': ({'auto_nudge', 'restart_resume'}, {'nudge_effective'}),
    'wedge': (
        {'wedge_auto_continue', 'wedge_soft_restart'},
        {'wedge_nudge_effective'},
    ),
}

_WINDOW_ENTRIES = 500  # hard journal-read cap for one evaluation


def _coerce_int(val: Any, fallback: int, lo: int, hi: int) -> int:
    try:
        n = int(val)
    except (TypeError, ValueError):
        return fallback
    return max(lo, min(hi, n))


def _clamp(name: str, value: float) -> float:
    lo, hi = CLAMPS[name]
    return max(lo, min(hi, value))


def _step(current: float, pct: int, direction: int, name: str) -> float:
    # direction: +1 tighten (longer wait), -1 relax (shorter wait).
    # Max pct% of the current value, minimum step 1 minute, clamped.
    raw = abs(current) * (pct / 100.0)
    delta = raw if raw >= 1.0 else 1.0
    return round(_clamp(name, current + direction * delta), 1)


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def configured_values(cfg: dict) -> dict[str, float]:
    return {
        'nudge_cooldown_minutes': float(cfg.get('nudge_cooldown_minutes', 10) or 10),
        'wedge_nudge_after_minutes': float(
            cfg.get('wedge_nudge_after_minutes', 10) or 10
        ),
        'wedge_remediation_cooldown_minutes': float(
            cfg.get('wedge_remediation_cooldown_minutes', 10) or 10
        ),
    }


def window_rates(window_start: str) -> dict[str, dict[str, int]]:
    # Sent/outcome counts inside the journal window (ts > window_start).
    # Empty window_start counts the whole readable journal tail.
    try:
        entries = state_mod.read_journal(_WINDOW_ENTRIES)
    except Exception:
        entries = []
    start_dt = _parse_iso(window_start)
    out: dict[str, dict[str, int]] = {}
    for metric, (sent_actions, outcome_actions) in _ACTIONS.items():
        sent = 0
        effective = 0
        for item in entries:
            if not isinstance(item, dict):
                continue
            ts = _parse_iso(item.get('timestamp', ''))
            if start_dt is not None and (ts is None or ts <= start_dt):
                continue
            action = item.get('action', '')
            if action in sent_actions:
                sent += 1
            elif action in outcome_actions:
                effective += 1
        out[metric] = {'sent': sent, 'effective': effective}
    return out


def evaluate(state: dict, cfg: dict, now_iso: str = '') -> dict:
    """One adaptive evaluation against ``state``; may append one history
    entry when values change. Never raises on bad input values (defensive
    coercion + clamps); tick() additionally wraps the call.
    Returns an info dict: {'enabled', 'evaluated', 'changed', 'values',
    'reason', 'decisions', 'last_eval'}.
    """
    now = datetime.now(timezone.utc)
    if not now_iso:
        now_iso = now.isoformat()
    if not bool(cfg.get('adaptive_thresholds', False)):
        return {'enabled': False, 'evaluated': False, 'reason': 'disabled'}

    ad = state.get('adaptive')
    if not isinstance(ad, dict):
        ad = {}
    state['adaptive'] = ad

    interval = _coerce_int(cfg.get('adaptive_interval_minutes', 60), 60, 15, 1440)
    min_samples = _coerce_int(cfg.get('adaptive_min_samples', 10), 10, 5, 100)
    step_pct = _coerce_int(cfg.get('adaptive_step_pct', 10), 10, 5, 25)

    configured = configured_values(cfg)
    seed = ad.get('seed')
    learned = ad.get('learned')
    if not isinstance(learned, dict) or set(learned) != set(TUNABLES):
        learned = dict(configured)
    else:
        for k in TUNABLES:
            try:
                learned[k] = float(learned[k])
            except (TypeError, ValueError):
                learned = dict(configured)
                break
    if not isinstance(seed, dict) or seed != configured:
        # manual settings change (or first run): settings win, window resets
        ad['seed'] = dict(configured)
        ad['learned'] = dict(configured)
        ad['window_start'] = now_iso
        ad['last_eval'] = ''
        return {
            'enabled': True,
            'evaluated': False,
            'changed': False,
            'reason': 'reseeded',
            'values': dict(configured),
            'last_eval': '',
        }

    ad['learned'] = dict(learned)
    last_eval_dt = _parse_iso(ad.get('last_eval', ''))
    if last_eval_dt is not None:
        waited = (now - last_eval_dt).total_seconds() / 60.0
        if waited < interval:
            return {
                'enabled': True,
                'evaluated': False,
                'reason': 'not_due',
                'next_eval_in_min': round(interval - waited, 1),
                'values': dict(learned),
                'last_eval': ad.get('last_eval', ''),
            }

    rates = window_rates(ad.get('window_start', '') or '')
    decisions: dict[str, Any] = {}
    changed = False
    for metric in ('nudge', 'wedge'):
        window = rates.get(metric, {'sent': 0, 'effective': 0})
        if window['sent'] < min_samples:
            decisions[metric] = {
                'sent': window['sent'],
                'effective': window['effective'],
                'rate': None,
                'reason': 'insufficient_samples',
            }
            continue
        rate = window['effective'] / float(window['sent'])
        if rate >= GOOD_RATE:
            direction = -1
            tag = 'relax'
        elif rate < BAD_RATE:
            direction = 1
            tag = 'tighten'
        else:
            decisions[metric] = {
                'sent': window['sent'],
                'effective': window['effective'],
                'rate': round(rate, 3),
                'reason': 'hold',
            }
            continue
        targets = ('nudge_cooldown_minutes',) if metric == 'nudge' else (
            'wedge_nudge_after_minutes',
            'wedge_remediation_cooldown_minutes',
        )
        moved: list[str] = []
        pinned: list[str] = []
        for name in targets:
            lo, hi = CLAMPS[name]
            base = configured[name]
            if base < lo or base > hi:
                # user set this knob outside the learnable band: pin it
                pinned.append(name)
                continue
            new_val = _step(learned[name], step_pct, direction, name)
            if abs(new_val - learned[name]) >= 0.05:
                learned[name] = new_val
                moved.append(name)
                changed = True
        decisions[metric] = {
            'sent': window['sent'],
            'effective': window['effective'],
            'rate': round(rate, 3),
            'reason': tag,
            'moved': moved,
            'pinned': pinned,
        }

    ad['last_eval'] = now_iso
    ad['window_start'] = now_iso
    ad['learned'] = dict(learned)
    # summary tag for tick()/status: strongest action this evaluation took
    _tags = [
        str((d or {}).get('reason') or '')
        for d in decisions.values() if isinstance(d, dict)
    ]
    if 'relax' in _tags:
        _reason = 'relax'
    elif 'tighten' in _tags:
        _reason = 'tighten'
    elif 'hold' in _tags:
        _reason = 'hold'
    else:
        _reason = 'no_samples'
    result = {
        'enabled': True,
        'evaluated': True,
        'changed': changed,
        'reason': _reason,
        'values': dict(learned),
        'decisions': decisions,
        'last_eval': now_iso,
    }
    if changed:
        try:
            state_mod.append_history(state, {
                'chat_id': '',
                'action': 'adaptive_adjusted',
                'detail': ', '.join(k + '=' + str(learned[k]) for k in TUNABLES),
                'decisions': decisions,
                'timestamp': now_iso,
            })
        except Exception:
            pass
    return result


def effective_values(cfg: dict, learned: Any) -> dict[str, float]:
    """Runtime-only threshold overlay for tick(): returns the three timing
    knobs with learned values applied where legal.

    A learned value is applied only when the configured value sits inside
    the learnable band (a user-set out-of-band value pins that knob) and
    the learned dict is structurally valid. Everything falls back to the
    configured value — the persisted config is never written.
    """
    out = configured_values(cfg)
    if not isinstance(learned, dict) or set(learned) != set(TUNABLES):
        return out
    for name in TUNABLES:
        lo, hi = CLAMPS[name]
        base = out[name]
        if base < lo or base > hi:
            continue  # pinned: user set this knob outside the band
        try:
            val = float(learned[name])
        except (TypeError, ValueError):
            continue
        out[name] = _clamp(name, val)
    return out
