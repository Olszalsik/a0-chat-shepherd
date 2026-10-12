"""chat_shepherd — the wake verb: the one way chat_shepherd starts work.

A supervisor that can only react to what it can already see is a supervisor.
The nudge ladder injects a message into a context that exists and is already
stuck; it cannot bring anything new into existence. That is the difference
between watching and operating continuously, and it is the capability this
module provides.

Two delivery paths, one contract
--------------------------------
``heartbeat`` already knows how to build a scheduled task, honour active
hours, and route an alert to a chosen chat. If it is installed, chat_shepherd uses
it — it is strictly better than re-implementing that. If it is absent,
``_vendored_wake`` does the minimum: resolve or create a context, stamp it so
the supervisor can recognise its own wake, and hand it a message.

The fallback is not a degraded stub. It is the primary path, and it is small
enough to read in one sitting. Chat Shepherd 2 must be fully functional on an install
that has never heard of the heartbeat plugin, and must not break when the
heartbeat plugin is updated or removed.

The quiet token
---------------
The wake prompt asks the agent to answer with exactly ``SHEPHERD2_OK`` when
nothing needs attention. A bounded remainder is suppressed, a longer one is
an alert. This is what makes a recurring health check affordable: the common
case costs one short reply instead of a full investigation, and "nothing is
wrong" becomes a positive confirmation rather than an absence of evidence.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from usr.plugins.chat_shepherd.helpers.clock import iso
from usr.plugins.chat_shepherd.helpers.clock import as_int
from usr.plugins.chat_shepherd.helpers.constants import (
    CTX_SHEPHERD,
    CTX_WAKE_ISOLATED,
    CTX_WAKE_MANUAL,
    CTX_WAKE_PROMPT,
    CTX_WAKE_TARGET,
    CONTINUITY_PROMPT_DEFAULT,
    PLUGIN_NAME,
    WAKE_OK_TOKEN,
)
from usr.plugins.chat_shepherd.helpers.state import append_journal


def _debug(kind: str, msg: str) -> None:
    """Lazy debug seam: monitor's rotating sink, tolerated when absent."""
    try:
        from usr.plugins.chat_shepherd.helpers.monitor import _debug_log as log
        log(kind, msg)
    except Exception:
        pass

WAKED = 'waked'
SKIPPED = 'skipped'
FAILED = 'failed'


def build_prompt(cfg: dict[str, Any]) -> str:
    """The configured prompt, with the quiet token substituted in."""
    text = str((cfg or {}).get('continuity_prompt') or '').strip()
    if not text:
        text = CONTINUITY_PROMPT_DEFAULT.strip()
    if '{token}' in text:
        return text.replace('{token}', WAKE_OK_TOKEN)
    return text + '\n\nIf nothing needs attention, reply with exactly ' + WAKE_OK_TOKEN + '.'


def classify_response(text: str, ack_max_chars: Any) -> dict[str, Any]:
    """Split the agent's reply into "quiet" and "needs a human".

    A token with a short remainder is a quiet check-in; the remainder is
    dropped. A token with a long remainder is ambiguous, so it is treated as
    an alert — better to read a paragraph than to swallow a real problem
    because a model prefixed it politely.
    """
    raw = str(text or '').strip()
    limit = as_int(ack_max_chars, 300)
    remainder = raw
    has_token = False
    if raw.startswith(WAKE_OK_TOKEN):
        has_token = True
        remainder = raw[len(WAKE_OK_TOKEN):].strip()
    elif raw.endswith(WAKE_OK_TOKEN):
        has_token = True
        remainder = raw[: -len(WAKE_OK_TOKEN)].strip()
    if has_token and len(remainder) <= max(0, limit):
        return {'status': 'suppressed', 'summary': '', 'deliver_text': ''}
    body = remainder if has_token else raw
    compact = ' '.join(body.split())
    return {
        'status': 'alert',
        'summary': compact[:160],
        'deliver_text': body,
    }


# --- delivery --------------------------------------------------------------

def _live_contexts() -> list[Any]:
    try:
        from agent import AgentContext, AgentContextType
    except Exception:
        return []
    live = []
    for context in AgentContext.all():
        try:
            if getattr(context, 'type', None) != AgentContextType.USER:
                continue
        except Exception:
            pass
        live.append(context)
    return live


def resolve_target(target: str | None = None) -> Any | None:
    """`none` for an isolated wake, `last`, or `context:<id>`."""
    value = (target or 'none').strip() or 'none'
    if value == 'none':
        return None
    candidates = _live_contexts()
    if not candidates:
        return None
    if value == 'last':
        return max(
            candidates,
            key=lambda ctx: getattr(ctx, 'last_message', '') or '',
            default=None,
        )
    explicit = value.split(':', 1)[1] if value.startswith('context:') else value
    for context in candidates:
        if getattr(context, 'id', '') == explicit:
            return context
    return None


def _stamp(context: Any, *, prompt: str, isolated: bool, manual: bool, target: str) -> None:
    """Mark a context as chat_shepherd's own.

    Without this the supervisor would eventually watch its own watchdog: a
    wake context is a real context with a real id and a persisted chat
    directory, so it is indistinguishable from user work unless it says so.
    """
    data = getattr(context, 'data', None)
    if not isinstance(data, dict):
        return
    data[CTX_SHEPHERD] = True
    data[CTX_WAKE_PROMPT] = prompt[:2000]
    data[CTX_WAKE_ISOLATED] = bool(isolated)
    data[CTX_WAKE_MANUAL] = bool(manual)
    data[CTX_WAKE_TARGET] = str(target or 'none')


def is_shepherd_context(context: Any) -> bool:
    data = getattr(context, 'data', None)
    return bool(isinstance(data, dict) and data.get(CTX_SHEPHERD))


def _heartbeat_cfg(cfg: dict[str, Any], prompt: str) -> dict[str, Any]:
    """Shape chat_shepherd's config into what heartbeat's runner expects."""
    return {
        'enabled': True,
        'every': str((cfg or {}).get('continuity_every_minutes') or 30) + 'm',
        'prompt': prompt,
        'ack_max_chars': (cfg or {}).get('continuity_ack_max_chars', 300),
        'light_context': False,
        'isolated_session': bool((cfg or {}).get('continuity_isolated', True)),
        'include_reasoning': False,
        'target': 'none',
        'active_hours': {
            'enabled': bool((cfg or {}).get('active_hours_enabled')),
            'start': str((cfg or {}).get('active_hours_start') or '00:00'),
            'end': str((cfg or {}).get('active_hours_end') or '24:00'),
            'timezone': str((cfg or {}).get('active_hours_timezone') or ''),
        },
    }


def _running_loop_here() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False
    except Exception:
        return False


def _try_heartbeat(cfg: dict[str, Any], prompt: str, manual: bool, force: bool) -> dict[str, Any] | None:
    """Use the heartbeat plugin when present. Returns None when it is not.

    `fire_heartbeat` is a coroutine function. Calling it from sync code and
    storing the coroutine does not deliver anything — it produces a
    "never awaited" warning and a result object that claims success while the
    wake silently never happened. So it is awaited, and only where awaiting is
    clean:

    * a running loop on this thread means we are inside the framework job loop.
      Re-entering it would re-enter the loop the tick is already running on,
      which is exactly the hazard `helpers/asyncio_bootstrap` documents (outer
      task callbacks get dropped). Refuse and fall back to the vendored path,
      which needs no loop.
    * the scheduled path never calls this at all — it always uses the vendored
      wake — so the only caller is the manual API handler, which runs on a
      worker thread with no running loop, where `asyncio.run` is clean.
    """
    try:
        from usr.plugins.heartbeat.helpers.runner import fire_heartbeat
    except Exception:
        return None
    if _running_loop_here():
        _debug('wake_heartbeat_skipped', 'called from inside a running loop')
        return None
    if inspect.iscoroutinefunction(fire_heartbeat):
        try:
            result = asyncio.run(
                fire_heartbeat(
                    _heartbeat_cfg(cfg, prompt), manual=manual, force=force, text=prompt
                )
            )
        except Exception as exc:
            _debug('wake_heartbeat_fail', repr(exc))
            return None
        return {
            'success': True,
            'status': WAKED,
            'via': 'heartbeat',
            'detail': str(result)[:200],
        }
    try:
        # A future release may make it synchronous. Handle that rather than
        # calling a coroutine and calling it a day.
        result = fire_heartbeat(
            _heartbeat_cfg(cfg, prompt), manual=manual, force=force, text=prompt
        )
    except Exception as exc:
        _debug('wake_heartbeat_fail', repr(exc))
        return None
    return {'success': True, 'status': WAKED, 'via': 'heartbeat', 'detail': str(result)[:200]}


def _vendored_wake(
    cfg: dict[str, Any],
    prompt: str,
    *,
    manual: bool,
    force: bool,
) -> dict[str, Any]:
    """Create-or-resolve a context and hand it the prompt. Self-contained."""
    try:
        from agent import AgentContext, AgentContextType, UserMessage
    except Exception as exc:
        return {'success': False, 'status': FAILED, 'error': str(exc)[:200]}

    isolated = bool(cfg.get('continuity_isolated', True))
    context = None
    if not isolated:
        context = resolve_target('last')
    if context is not None and hasattr(context, 'is_running'):
        if context.is_running() and not force:
            # A busy target is skipped rather than interrupted: queueing
            # behind live work is the difference between a health check and
            # a second agent fighting over one chat.
            return {
                'success': True,
                'status': SKIPPED,
                'skipped': 'target busy',
                'context_id': getattr(context, 'id', ''),
            }
    if context is None:
        try:
            from initialize import initialize_agent

            context = AgentContext(
                config=initialize_agent(),
                name='Chat Shepherd',
                type=AgentContextType.BACKGROUND if isolated else AgentContextType.USER,
            )
        except Exception as exc:
            return {'success': False, 'status': FAILED, 'error': str(exc)[:200]}

    _stamp(context, prompt=prompt, isolated=isolated, manual=manual, target='none')
    try:
        context.communicate(UserMessage(prompt))
    except Exception as exc:
        _debug('wake_deliver_fail', repr(exc))
        return {'success': False, 'status': FAILED, 'error': str(exc)[:200]}
    return {
        'success': True,
        'status': WAKED,
        'via': 'vendored',
        'context_id': getattr(context, 'id', ''),
        'isolated': isolated,
    }


def fire_wake(
    cfg: dict[str, Any],
    *,
    prompt: str = '',
    manual: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Fire one continuity wake. Never raises; always returns a verdict.

    Path selection, and the reasoning behind it:

    * **Non-isolated manual wake → vendored, always.** This case must honour
      chat_shepherd's own target resolution and its "refuse a busy target" guard.
      Routing it through the heartbeat plugin would silently bypass that
      guard — the runner would happily wake a chat that is mid-task, and the
      override would go unnoticed because the result still says success.
      A safety guard that only exists on one code path is not a guard.
    * **Isolated manual wake → heartbeat when available.** Nothing to guard,
      so the better-maintained runner is the better choice.
    * **Scheduled → vendored.** It runs on the job loop thread, where a
      coroutine cannot be awaited cleanly (see `_try_heartbeat`), and it needs
      no loop at all.

    In every case a heartbeat failure degrades to the vendored path, so a
    third-party plugin can only make the wake better — never unavailable.
    """
    cfg = cfg or {}
    text = str(prompt or '').strip() or build_prompt(cfg)
    isolated = bool(cfg.get('continuity_isolated', True))

    if manual and isolated:
        result = _try_heartbeat(cfg, text, True, force)
        if result is None:
            result = _vendored_wake(cfg, text, manual=True, force=force)
    else:
        result = _vendored_wake(cfg, text, manual=manual, force=force)

    result.setdefault('status', FAILED)
    result['timestamp'] = iso()
    append_journal({
        'chat_id': '',
        'action': 'continuity_wake' if result['status'] == WAKED else 'continuity_skipped',
        'status': result['status'],
        'via': result.get('via', ''),
        'manual': bool(manual),
        'context_id': result.get('context_id', ''),
        'error': str(result.get('error') or '')[:200],
        'timestamp': iso(),
    })
    return result


def record_wake(state: dict[str, Any], result: dict[str, Any], reason: str, now: Any = None) -> None:
    """Advance the continuity bookkeeping after a wake attempt."""
    from usr.plugins.chat_shepherd.helpers.clock import day_key

    continuity = state.get('continuity')
    if not isinstance(continuity, dict):
        continuity = {}
        state['continuity'] = continuity
    continuity['last_at'] = iso(now)
    continuity['last_status'] = str(result.get('status') or '')
    continuity['last_reason'] = str(reason or '')[:200]
    continuity['last_via'] = str(result.get('via') or '')
    if result.get('status') == WAKED:
        key = 'wakes_' + day_key(now)
        try:
            continuity[key] = as_int(continuity.get(key), 0) + 1
        except Exception:
            continuity[key] = 1


__all__ = [
    'FAILED', 'SKIPPED', 'WAKE_OK_TOKEN', 'WAKED', 'PLUGIN_NAME',
    'build_prompt', 'classify_response', 'fire_wake', 'is_shepherd_context',
    'record_wake', 'resolve_target',
]
