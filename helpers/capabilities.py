"""chat_shepherd — optional-dependency registry.

Chat Shepherd 2 is designed to be useful alone. ``_goal`` gives it a reason to wake
a chat; the ``heartbeat`` plugin already knows how to build a scheduled task;
``paperclip_zero`` may have blocked a chat for a reason of its own. All three
are optional, all three can be updated or removed independently, and none of
them may break the tick.

So every optional integration is declared once here as a probe, and the tick
sees a uniform record: ``{name, available, required, detail, checked_at}``.
That record is surfaced on the dashboard and feeds the self-check, so a
silently missing dependency is visible instead of merely absent.

A missing *soft* dependency is not a degradation — it is the expected state
on an install that does not have that plugin. Only a dependency that was
observed working and then stopped being importable is worth a warning, which
is why the probe result is compared against what state remembers.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from usr.plugins.chat_shepherd.helpers.clock import iso
from usr.plugins.chat_shepherd.helpers.constants import EXTERNAL_BLOCK_SOURCES

# Parsed foreign block state, keyed by source path -> (mtime_ns, size, mapping).
# Bounded by the number of declared sources, so it cannot grow.
_BLOCK_CACHE: dict[str, tuple[tuple[int, int], dict[str, str]]] = {}


def _abs(rel_path: str) -> str:
    """Path seam, same as `state._abs` / `observe._abs`.

    Reading the framework's helper directly would make this module untestable
    offline AND inconsistent with the rest of the plugin: the suite would
    redirect every other path to a sandbox and read paperclip_zero's real
    state file out of the repository.
    """
    try:
        from helpers import files

        return files.get_abs_path(rel_path)
    except Exception:
        return rel_path


def _probe_goal() -> tuple[bool, str]:
    from plugins._goal.tools import goal

    return callable(goal.get_goal), 'goal.get_goal'


def _probe_heartbeat() -> tuple[bool, str]:
    from usr.plugins.heartbeat.helpers.runner import fire_heartbeat

    return callable(fire_heartbeat), 'heartbeat.fire_heartbeat'


def _probe_paperclip() -> tuple[bool, str]:
    return os.path.exists(_abs(_PAPERCLIP_STATE)), 'paperclip_zero state file'


_PAPERCLIP_STATE = EXTERNAL_BLOCK_SOURCES[0][1]


# name -> (required, probe, description)
PROBES: dict[str, tuple[bool, Callable[[], tuple[bool, str]], str]] = {
    'goal': (False, _probe_goal, 'open chat goals (plugins._goal)'),
    'heartbeat': (False, _probe_heartbeat, 'scheduled wake helper (heartbeat plugin)'),
    'paperclip': (False, _probe_paperclip, 'externally blocked work (paperclip_zero)'),
}


def probe_one(name: str) -> dict[str, Any]:
    """Run one probe. A broken probe is a failed probe, never an exception."""
    entry = PROBES.get(name)
    if entry is None:
        return {
            'name': name,
            'available': False,
            'required': False,
            'detail': 'unknown capability',
            'checked_at': iso(),
        }
    required, probe, description = entry
    available = False
    detail = description
    try:
        available, detail = probe()
    except Exception as exc:
        available = False
        detail = f'{description}: {type(exc).__name__}'
    return {
        'name': name,
        'available': bool(available),
        'required': bool(required),
        'detail': str(detail)[:200],
        'checked_at': iso(),
    }


def probe_all() -> dict[str, dict[str, Any]]:
    """Probe every declared capability. Never raises."""
    out: dict[str, dict[str, Any]] = {}
    for name in PROBES:
        out[name] = probe_one(name)
    return out


def merge_into_state(
    state: dict[str, Any],
    results: dict[str, dict[str, Any]],
) -> list[str]:
    """Fold probe results into state; return the names that newly degraded.

    A capability that was previously available and is now unavailable is a
    regression worth reporting. One that was never available is not.
    """
    previous = state.get('capabilities')
    if not isinstance(previous, dict):
        previous = {}
        state['capabilities'] = previous
    regressed: list[str] = []
    for name, record in (results or {}).items():
        before = previous.get(name)
        was_available = bool(isinstance(before, dict) and before.get('available'))
        if was_available and not record.get('available'):
            regressed.append(name)
        previous[name] = record
    return sorted(regressed)


# --- chat-scoped integrations ---------------------------------------------

def active_goal(chat_id: str) -> dict[str, Any] | None:
    """The chat's open goal, or None. Import failure is silent by design."""
    try:
        from plugins._goal.tools.goal import ACTIVE_STATUSES, get_goal

        goal = get_goal(chat_id)
    except Exception:
        return None
    if not goal or str(goal.get('status')) not in ACTIVE_STATUSES:
        return None
    return goal


def external_blocks() -> dict[str, str]:
    """Chat ids another plugin has deliberately blocked, and why.

    Read-only and tolerant of every failure shape: a missing file, an
    unreadable file, a schema that grew a new key. A supervisor that starts
    nudging a chat another plugin parked for a budget stop is a real
    conflict, and this is the cheapest way to avoid it.

    The reads are cached on each source's `(mtime_ns, size)`, so a tick that
    asks once a minute stops re-parsing foreign state 60 times a minute (O3).
    Only a real change invalidates an entry, and a missing or unreadable source
    is never cached, so installing a foreign plugin still takes effect on the
    next tick.
    """
    out: dict[str, str] = {}
    for plugin, rel in EXTERNAL_BLOCK_SOURCES:
        path = _abs(rel)
        try:
            stat = os.stat(path)
            key = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            continue
        cached = _BLOCK_CACHE.get(path)
        if cached is not None and cached[0] == key:
            out.update(cached[1])
            continue
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                payload = json.load(handle)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        parsed: dict[str, str] = {}
        for item in payload.get('work_items') or []:
            if not isinstance(item, dict) or item.get('status') != 'blocked':
                continue
            context_id = str(item.get('linked_context_id') or '').strip()
            if not context_id:
                continue
            parsed[context_id] = '{} blocked: {}'.format(
                plugin, str(item.get('title') or item.get('notes') or 'no reason given')[:120]
            )
        _BLOCK_CACHE[path] = (key, parsed)
        out.update(parsed)
    return out
