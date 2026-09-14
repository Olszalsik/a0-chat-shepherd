from __future__ import annotations

# v1.9.0 graceful reload: watch the plugin's helper modules and re-import
# them between ticks when their source changes, so monitor fixes land
# without a server restart. Safety rails:
#   - compile gate: a watched file that does not compile is never loaded
#   - all-or-nothing: per-module namespace snapshots; any exec failure
#     rolls every module reloaded in this pass back to pre-reload state
#   - lock preservation: module-level RLocks named *lock* (e.g.
#     state._journal_lock) survive reloads so old and new call sites
#     keep sharing one lock
#   - check_and_reload() never raises: the tick loop must survive a
#     broken patch attempt (failed passes back off for 60s)
# Scope: helpers only. api/* handlers and the framework keep their
# from-import bindings until the next plugin refresh. hotreload.py
# itself reloads last, without rollback (the executing frame belongs
# to the old code); its status counters reset after a self-reload.
import importlib.machinery
import importlib.util
import os
import sys
import threading
import time
from datetime import datetime
from typing import Any

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# dependency order: constants -> state -> monitor -> hotreload (self last)
_DEFAULT_WATCH = [
    ('usr.plugins.chat_shepherd.helpers.constants', 'helpers/constants.py'),
    ('usr.plugins.chat_shepherd.helpers.state', 'helpers/state.py'),
    ('usr.plugins.chat_shepherd.helpers.monitor', 'helpers/monitor.py'),
    ('usr.plugins.chat_shepherd.helpers.hotreload', 'helpers/hotreload.py'),
]

_SELF = 'usr.plugins.chat_shepherd.helpers.hotreload'
_SELF_REL = 'helpers/hotreload.py'

_lock = threading.RLock()
_mtimes: dict[str, float] = {}
_primed = False
_fail_until = 0.0
_STATUS: dict[str, Any] = {
    'last_check': '',
    'last_reload': '',
    'last_result': 'never',
    'last_detail': '',
    'reloaded': [],
}


def _abs(rel: str) -> str:
    return os.path.join(_PLUGIN_DIR, rel.replace('/', os.sep))


def status() -> dict[str, Any]:
    with _lock:
        return dict(_STATUS)


def _set_status(result: str, detail: str = '', reloaded=None) -> None:
    global _STATUS
    snap = dict(_STATUS)
    snap['last_check'] = datetime.now().isoformat(timespec='seconds')
    if result == 'reloaded':
        snap['last_reload'] = snap['last_check']
    snap['last_result'] = result
    snap['last_detail'] = detail
    snap['reloaded'] = list(reloaded or [])
    _STATUS = snap
    if result in ('reloaded', 'failed', 'compile_error', 'primed'):
        # v1.9.0: mirror notable reload events into the plugin debug log
        try:
            mon = sys.modules.get('usr.plugins.chat_shepherd.helpers.monitor')
            if mon is not None and hasattr(mon, '_debug_log'):
                mon._debug_log('hot_reload', result + ' ' + (detail or '') + ' [' + ', '.join(reloaded or []) + ']')
        except Exception:
            pass


def _snapshot(mod) -> dict:
    return dict(vars(mod))


def _rollback(mod, snap: dict) -> None:
    try:
        vars(mod).clear()
        vars(mod).update(snap)
    except Exception:
        pass


def _preserve_locks(mod, snap: dict) -> None:
    # reload re-creates module-level locks; rebind the pre-reload objects
    # so old and new function refs keep sharing one lock
    rlock = type(threading.RLock())
    for k, v in snap.items():
        if 'lock' in k.lower() and isinstance(v, rlock):
            try:
                setattr(mod, k, v)
            except Exception:
                pass


def _exec_source(mod, path: str) -> None:
    # re-exec the watched source file into the existing module object
    # (identity preserved). Unlike importlib.reload this does not need
    # the module to be findable through the regular import system -
    # it pins the exact watched file, which is what we intend to
    # load anyway.
    loader = importlib.machinery.SourceFileLoader(mod.__name__, path)
    spec = importlib.util.spec_from_file_location(
        mod.__name__, path, loader=loader
    )
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError('no loader for ' + mod.__name__)
    mod.__spec__ = spec
    mod.__loader__ = loader
    loader.exec_module(mod)


def check_and_reload(cfg: Any = None, force: bool = False, modules=None) -> dict[str, Any]:
    """Tick-loop entry point; never raises."""
    try:
        with _lock:
            return _check(cfg, force, modules)
    except Exception as e:  # absolute last resort
        try:
            _set_status('failed', repr(e))
        except Exception:
            pass
        return {'reloaded': [], 'status': 'failed', 'detail': repr(e)}


def _check(cfg: Any, force: bool, modules) -> dict[str, Any]:
    global _primed, _fail_until
    watch = modules if modules is not None else _DEFAULT_WATCH
    override = modules is not None
    enabled = True if force else bool((cfg or {}).get('hot_reload_enabled', True))

    def p(rel: str) -> str:
        return rel if override else _abs(rel)

    cur: dict[str, float] = {}
    for name, rel in watch:
        try:
            cur[name] = os.stat(p(rel)).st_mtime
        except OSError:
            cur[name] = -1.0

    if not _primed:
        # first call only primes the mtime cache: the live modules were
        # imported at startup, there is nothing to reload yet
        _mtimes.update(cur)
        _primed = True
        _set_status('primed' if enabled else 'disabled', 'first check')
        return {'reloaded': [], 'status': 'primed' if enabled else 'disabled'}

    if not enabled:
        _set_status('disabled')
        return {'reloaded': [], 'status': 'disabled'}

    changed = [n for n, m in cur.items() if _mtimes.get(n) != m]
    if not changed:
        _set_status('clean')
        return {'reloaded': [], 'status': 'clean'}

    if time.monotonic() < _fail_until:
        _set_status('backoff', 'previous failure; retrying later')
        return {'reloaded': [], 'status': 'backoff'}

    # compile gate: every changed file must compile before anything reloads
    for name, rel in watch:
        if name not in changed:
            continue
        path = p(rel)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                src = f.read()
            compile(src, path, 'exec')
        except (OSError, SyntaxError, ValueError) as e:
            _fail_until = time.monotonic() + 60.0
            _set_status('compile_error', name + ': ' + repr(e))
            return {'reloaded': [], 'status': 'compile_error', 'detail': repr(e)}

    # all-or-nothing reload: every watched module re-executes in
    # dependency order; any exec failure rolls the whole pass back
    snaps = []
    done = []
    fail_mod = None
    fail_snap = None
    try:
        for name, rel in watch:
            mod = sys.modules.get(name)
            if mod is None:
                continue
            snap = _snapshot(mod)
            try:
                _exec_source(mod, p(rel))
            except Exception:
                fail_mod = mod
                fail_snap = snap
                raise
            _preserve_locks(mod, snap)
            snaps.append((mod, snap))
            done.append(name)
    except Exception as e:
        # roll back the module whose exec failed mid-way too, then all
        # earlier modules of this pass
        if fail_mod is not None:
            _rollback(fail_mod, fail_snap)
        for mod, snap in reversed(snaps):
            _rollback(mod, snap)
        _fail_until = time.monotonic() + 60.0
        _set_status('failed', repr(e), done)
        return {
            'reloaded': [],
            'status': 'failed',
            'detail': repr(e),
            'rolled_back': True,
        }

    # self-reload (real mode only) so patched hotreload code lands too;
    # no rollback here: the executing frame belongs to the old code.
    # Failure just means the old engine keeps running; next tick retries.
    if not override and _SELF in changed:
        try:
            _exec_source(sys.modules[_SELF], _abs(_SELF_REL))
        except Exception as e:
            _fail_until = time.monotonic() + 60.0
            return {'reloaded': done, 'status': 'failed', 'detail': 'self: ' + repr(e)}

    _mtimes.update(cur)
    _set_status('reloaded', ', '.join(done) if done else 'nothing loaded', done)
    return {'reloaded': done, 'status': 'reloaded'}
