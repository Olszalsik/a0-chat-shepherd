import json
import os
import sys
import threading
import types
from datetime import datetime, timedelta, timezone

# P6.2: portable bootstrap - derive the repo root from __file__
# (tests/ lives at <root>/usr/plugins/chat_shepherd/tests/) instead of
# hardcoding /a0, so the suite also runs off-Docker (Windows-verified).
_REPO_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '..',
))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
os.chdir(_REPO_ROOT)

from usr.plugins.chat_shepherd.helpers import constants, monitor
from usr.plugins.chat_shepherd.helpers import state as state_mod
from helpers import files

TEST_STATE = 'usr/plugins/chat_shepherd/data/test_state.json'
FAKE_CHAT_A = 'csTstA01'
FAKE_CHAT_B = 'csTstB02'
FAKE_CHAT_C = 'csTstC03'

TEST_JOURNAL = 'usr/plugins/chat_shepherd/data/test_history.jsonl'

# v1.21.3 (P6.2 scratch-dir hygiene, closed): the fixture chats used to be
# created as REAL `usr/chats/csTst*` directories. They match
# CHAT_ID_PATTERN, so a live tick on a running server could track them - and a
# run that died before `cleanup()` left them behind to be classified (and
# potentially nudged) for real. Deferred since v1.13.0 because it needed a
# monitor-side redirect; `monitor._CHAT_DIR_BASE` + `monitor._chat_dir()` (the
# single seam added in v1.21.2) provide exactly that, so the whole redirect is
# one assignment. Everything below that builds a chat path goes through
# `_chat_dir`, and TEST45X pins the seam - if a future change bypasses it, this
# suite starts writing to the real tree again and fails loudly.
TEST_CHAT_BASE = 'tmp/chat_shepherd_suite_chats'

def _chat_dir(cid):
    """Fixture chat dir under the redirected base."""
    return files.get_abs_path(TEST_CHAT_BASE + '/' + cid)

def _truncate_journal():
    try:
        os.unlink(files.get_abs_path(TEST_JOURNAL))
    except Exception:
        pass


class FakeCtx:
    def __init__(self, cid, log_types, idle_minutes, ctype=None, running=False, progress=''):
        self.id = cid
        self.log = types.SimpleNamespace(
            _lock=threading.Lock(),
            logs=[types.SimpleNamespace(type=t) for t in log_types],
            progress=progress,
        )
        self.paused = False
        self.last_message = datetime.now(timezone.utc) - timedelta(minutes=idle_minutes)
        self.communicated = []
        self.type = ctype if ctype is not None else monitor.AgentContextType.USER
        self._running = running
        self.nudge_calls = 0

    def is_running(self):
        return self._running

    def communicate(self, msg):
        self.communicated.append(msg)

    def nudge(self):
        self.nudge_calls += 1


class FakeAgentContext:
    _all = []

    @classmethod
    def all(cls):
        return list(cls._all)



def write_state(last_tick, chats=None):
    payload = {'chats': chats or {}, 'history': [], 'last_tick': last_tick}
    files.write_file(TEST_STATE, json.dumps(payload, ensure_ascii=False))
    _truncate_journal()



def read_state():
    st = json.loads(files.read_file(files.get_abs_path(TEST_STATE)))
    try:
        st['history'] = state_mod.read_journal()
    except Exception:
        st['history'] = []
    return st


def make_chat_dirs():
    # v1.21.3: these live under TEST_CHAT_BASE (redirected via
    # monitor._CHAT_DIR_BASE in main()), never in the real usr/chats.
    for cid in (FAKE_CHAT_A, FAKE_CHAT_B, FAKE_CHAT_C):
        os.makedirs(_chat_dir(cid), exist_ok=True)
    # error path for the no-context classification test
    with open(os.path.join(_chat_dir(FAKE_CHAT_C), 'error.txt'), 'w') as f:
        f.write('boom')
    # v1.21.3: a chat.json so the title/name resolver has something real to
    # read. Without it the suite passed only because the resolver returns ''
    # for a missing file - it never exercised the cache path against a real
    # file, which is where the v1.21.0 LRU eviction lives.
    with open(os.path.join(_chat_dir(FAKE_CHAT_A), 'chat.json'), 'w',
              encoding='utf-8') as f:
        f.write(json.dumps({'name': 'Suite Chat A'}))
    with open(os.path.join(_chat_dir(FAKE_CHAT_B), 'chat.json'), 'w',
              encoding='utf-8') as f:
        f.write(json.dumps({'name': 'Suite Chat B'}))


def cleanup():
    import shutil
    for cid in (FAKE_CHAT_A, FAKE_CHAT_B, FAKE_CHAT_C):
        try:
            shutil.rmtree(_chat_dir(cid), ignore_errors=True)
        except Exception:
            pass
    try:
        # drop the (now empty) scratch base itself
        shutil.rmtree(files.get_abs_path(TEST_CHAT_BASE), ignore_errors=True)
    except Exception:
        pass

    try:
        os.unlink(files.get_abs_path(TEST_STATE))
        _truncate_journal()
    except Exception:
        pass


def main():
    orig_state_file = state_mod.STATE_FILE

    orig_journal_file = state_mod.JOURNAL_FILE
    orig_agent_ctx = monitor.AgentContext
    # v1.21.0 (A15): state/journal are redirected to scratch files, but
    # `_debug_log` was NOT, so every suite run wrote its own test events into
    # the LIVE production debug log (usr/plugins/chat_shepherd/data/debug-*.log
    # shipped with hot_reload/auto_nudge_fail/chat_tick_fail rows naming the
    # test fixtures) and its newest-7 rotation could push a real day out of
    # the window. Redirect it to a scratch file for the whole run; tests that
    # want to assert on log content stub `_debug_log` themselves (TEST24A),
    # which keeps working because the stub replaces the attribute outright.
    orig_debug_log = monitor._debug_log
    # v1.21.3 (P6.2): redirect the chat-dir base so fixture chats are created
    # under tmp/ instead of the live usr/chats. Single assignment thanks to the
    # `_chat_dir` seam; the listing cache is dropped so nothing resolved
    # against the real base is reused.
    orig_chat_base = monitor._CHAT_DIR_BASE
    state_mod.STATE_FILE = TEST_STATE

    state_mod.JOURNAL_FILE = TEST_JOURNAL
    monitor.AgentContext = FakeAgentContext
    monitor._CHAT_DIR_BASE = TEST_CHAT_BASE
    monitor._chat_listing_cache.clear()
    monitor._error_file_cache.clear()
    monitor._NAME_CACHE.clear()
    try:
        # v1.21.0 (A15): the scratch debug sink must be an appender, because
        # TEST5C/TEST24A-style stubs restore this original afterwards.
        _scratch_debug_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            'data', 'scratch_debug.log',
        )
        os.makedirs(os.path.dirname(_scratch_debug_path), exist_ok=True)

        def _scratch_debug(kind, message):
            with open(_scratch_debug_path, 'a', encoding='utf-8') as _f:
                _f.write('%s %s %s\n' % (kind, message, ''))

        monitor._debug_log = _scratch_debug
        make_chat_dirs()
        now_iso = datetime.now(timezone.utc).isoformat()
        cfg = {
            'enabled': True,
            'watch_all': True,
            'stall_minutes': 5,
            'max_auto_nudges': 3,
            'max_nudges_per_tick': 5,
            'nudge_cooldown_minutes': 0,
            'intervention_after_failed_nudges': True,
            'notify_on_intervention': False,
        }

        # ---- regression: _parse_dt accepts datetime objects (9999m bug) ----
        dt = datetime.now(timezone.utc) - timedelta(minutes=30)
        parsed = monitor._parse_dt(dt)
        assert parsed is not None and abs(monitor._minutes_since(parsed) - 30) < 2, (
            'datetime object rejected by _parse_dt (9999m regression)'
        )
        assert monitor._parse_dt('') is None and monitor._parse_dt(None) is None
        iso_parsed = monitor._parse_dt(now_iso)
        assert iso_parsed is not None

        # ================= TEST 1: restart recovery =================
        stale_tick = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        write_state(stale_tick, chats={
            FAKE_CHAT_A: {'status': 'running', 'nudge_count': 0, 'last_nudge_at': ''},
            FAKE_CHAT_B: {'status': 'running', 'nudge_count': 0, 'last_nudge_at': ''},
            'pruneMe1': {'status': 'stalled', 'nudge_count': 1, 'last_ticked': now_iso},
        })
        ctx_a = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
        ctx_b = FakeCtx(FAKE_CHAT_B, ['user', 'response'], idle_minutes=30)
        FakeAgentContext._all = [ctx_a, ctx_b]

        s1 = monitor.tick(cfg)
        print('SUMMARY1:', json.dumps(s1))
        assert s1['checked'] == 2, s1
        assert s1['interrupted'] == 1, s1
        assert s1['resumed'] == 1, s1
        assert s1['nudges_this_tick'] == 1, s1
        assert s1['awaiting'] == 1, s1

        st = read_state()
        ea = st['chats'][FAKE_CHAT_A]
        assert ea['status'] == 'nudged', ea
        assert ea['nudge_count'] == 1, ea
        assert 'Resume nudge after restart' in ea.get('last_classification', ''), ea
        assert st['chats'][FAKE_CHAT_B]['status'] == 'awaiting_user', st['chats'][FAKE_CHAT_B]
        assert 'pruneMe1' not in st['chats'], 'stale entry not pruned'
        actions = [h.get('action') for h in st['history']]
        assert 'restart_detected' in actions, actions
        assert 'restart_resume' in actions, actions
        assert len(ctx_a.communicated) == 1, ctx_a.communicated
        assert 'Agent Zero server was restarted' in str(ctx_a.communicated[0])
        assert len(ctx_b.communicated) == 0, 'response-ended chat must not be resumed'
        print('TEST1_RESTART_RECOVERY_OK')

        # ================= TEST 2: no restart => old behavior =================
        write_state((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), chats={
            FAKE_CHAT_A: {
                'status': 'running',
                'nudge_count': 0,
                'last_nudge_at': (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
            },
        })
        ctx_a2 = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
        FakeAgentContext._all = [ctx_a2]

        s2 = monitor.tick(cfg)
        print('SUMMARY2:', json.dumps(s2))
        assert s2['interrupted'] == 0 and s2['resumed'] == 0, s2
        assert s2['nudged'] == 1, s2
        assert len(ctx_a2.communicated) == 1
        assert 'You stopped unexpectedly' in str(ctx_a2.communicated[0])
        st2 = read_state()
        assert st2['chats'][FAKE_CHAT_A]['status'] == 'nudged', st2['chats'][FAKE_CHAT_A]
        print('TEST2_NO_RESTART_OK')

        # ================= TEST 3: classify unit tests =================
        idle30 = datetime.now(timezone.utc) - timedelta(minutes=30)
        prev_max = {'status': 'stalled', 'nudge_count': 3, 'last_nudge_at': now_iso}
        status, reason = monitor.classify_chat(ctx_a2, FAKE_CHAT_A, cfg, prev_max, 0.0)
        assert status == constants.STATUS_INTERVENTION, (status, reason)

        cfg_no_esc = dict(cfg, intervention_after_failed_nudges=False)
        status, reason = monitor.classify_chat(ctx_a2, FAKE_CHAT_A, cfg_no_esc, prev_max, 0.0)
        assert status == constants.STATUS_STALLED and 'no escalation' in reason, (status, reason)

        ctx_b2 = FakeCtx(FAKE_CHAT_B, ['user', 'response'], idle_minutes=30)
        status, reason = monitor.classify_chat(ctx_b2, FAKE_CHAT_B, cfg, {}, 0.0)
        assert status == constants.STATUS_AWAITING_USER, (status, reason)

        paused = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=0)
        paused.paused = True
        status, reason = monitor.classify_chat(paused, FAKE_CHAT_A, cfg, {}, 0.0)
        assert status == constants.STATUS_PAUSED, (status, reason)

        status, reason = monitor.classify_chat(None, FAKE_CHAT_C, cfg, {}, 0.0)
        assert status == constants.STATUS_ERROR, (status, reason)
        print('TEST3_CLASSIFY_OK')

        # ================= TEST 4: wedge remediation ladder (v1.2.0) =====
        wedge_cfg = dict(
            cfg,
            wedge_nudge_after_minutes=10,
            wedge_max_remediations=2,
            wedge_remediation_cooldown_minutes=0,
            wedge_soft_restart=False,
        )
        fresh_tick = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()

        def wedge_state(frozen_ago_min, wedge_count=0, wedge_ago_min=0):
            return {
                FAKE_CHAT_A: {
                    'status': 'intervention' if wedge_count else 'running',
                    'nudge_count': 0,
                    'last_nudge_at': '',
                    'last_log_len': 2,
                    'log_len_since': (
                        datetime.now(timezone.utc) - timedelta(minutes=frozen_ago_min)
                    ).isoformat(),
                    'wedge_nudge_count': wedge_count,
                    'last_wedge_nudge_at': (
                        datetime.now(timezone.utc) - timedelta(minutes=wedge_ago_min)
                    ).isoformat() if wedge_count else '',
                },
            }

        # --- 4a: frozen 6m (< 10m grace) -> intervention, no auto-continue
        write_state(fresh_tick, chats=wedge_state(6))
        ctx_g = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30, running=True)
        FakeAgentContext._all = [ctx_g]
        s4g = monitor.tick(wedge_cfg)
        assert s4g['wedged'] == 1 and s4g['wedge_nudged'] == 0, s4g
        assert len(ctx_g.communicated) == 0, 'grace period not respected'
        print('TEST4A_WEDGE_GRACE_OK')

        # --- 4b: frozen 12m -> one Tier-1 wedge nudge
        write_state(fresh_tick, chats=wedge_state(12))
        ctx_w = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30, running=True)
        FakeAgentContext._all = [ctx_w]
        s4 = monitor.tick(wedge_cfg)
        assert s4['wedged'] == 1 and s4['wedge_nudged'] == 1, s4
        assert len(ctx_w.communicated) == 1, ctx_w.communicated
        assert 'No output for several minutes' in str(ctx_w.communicated[0])
        st4 = read_state()
        ea4 = st4['chats'][FAKE_CHAT_A]
        assert ea4['status'] == 'intervention', ea4
        assert ea4['wedge_nudge_count'] == 1, ea4
        assert any(h.get('action') == 'wedge_auto_continue' for h in st4['history']), st4['history']
        print('TEST4B_WEDGE_TIER1_OK')

        # --- 4c: budget exhausted -> no more nudges, reason says exhausted
        write_state(fresh_tick, chats=wedge_state(12, wedge_count=2, wedge_ago_min=30))
        ctx_e = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30, running=True)
        FakeAgentContext._all = [ctx_e]
        s4e = monitor.tick(wedge_cfg)
        assert s4e['wedge_nudged'] == 0 and s4e['wedge_restarts'] == 0, s4e
        assert len(ctx_e.communicated) == 0, 'budget exhausted but nudge sent'
        st4e = read_state()
        assert 'remediation exhausted' in st4e['chats'][FAKE_CHAT_A]['last_classification'], st4e['chats'][FAKE_CHAT_A]
        print('TEST4C_WEDGE_EXHAUSTED_OK')

        # --- 4d: log grows -> wedge budget credited + cleared
        write_state(fresh_tick, chats=wedge_state(12, wedge_count=1, wedge_ago_min=3))
        ctx_r = FakeCtx(FAKE_CHAT_A, ['user', 'tool', 'tool'], idle_minutes=1, running=True)
        FakeAgentContext._all = [ctx_r]
        s4r = monitor.tick(wedge_cfg)
        assert s4r['running'] == 1, s4r
        st4r = read_state()
        ea4r = st4r['chats'][FAKE_CHAT_A]
        assert ea4r['wedge_nudge_count'] == 0, ea4r
        assert ea4r['wedge_nudges_effective'] == 1, ea4r
        assert any(h.get('action') == 'wedge_nudge_effective' for h in st4r['history']), st4r['history']
        print('TEST4D_WEDGE_RECOVERY_OK')

        # --- 4e: wedge_soft_restart on + final attempt -> ctx.nudge() (Tier 2)
        wedge_sr_cfg = dict(wedge_cfg, wedge_soft_restart=True)
        write_state(fresh_tick, chats=wedge_state(12, wedge_count=1, wedge_ago_min=30))
        ctx_s = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30, running=True)
        FakeAgentContext._all = [ctx_s]
        s4s = monitor.tick(wedge_sr_cfg)
        assert s4s['wedge_restarts'] == 1 and s4s['wedge_nudged'] == 0, s4s
        assert ctx_s.nudge_calls == 1, ctx_s.nudge_calls
        st4s = read_state()
        assert any(h.get('action') == 'wedge_soft_restart' for h in st4s['history']), st4s['history']
        print('TEST4E_WEDGE_SOFT_RESTART_OK')

        # --- 4f: wedge_soft_restart off (default) -> nudge() never called
        write_state(fresh_tick, chats=wedge_state(12, wedge_count=1, wedge_ago_min=30))
        ctx_n = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30, running=True)
        FakeAgentContext._all = [ctx_n]
        s4n = monitor.tick(wedge_cfg)
        assert s4n['wedge_nudged'] == 1 and s4n['wedge_restarts'] == 0, s4n
        assert ctx_n.nudge_calls == 0, ctx_n.nudge_calls
        print('TEST4F_WEDGE_SOFT_RESTART_OFF_OK')

        # --- 4g: scheduler TASK context -> never wedge-remediated
        write_state(fresh_tick, chats=wedge_state(12))
        ctx_t = FakeCtx(
            FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30,
            ctype=monitor.AgentContextType.TASK, running=True,
        )
        FakeAgentContext._all = [ctx_t]
        s4t = monitor.tick(wedge_sr_cfg)
        assert s4t['wedged'] == 1 and s4t['wedge_nudged'] == 0 and s4t['wedge_restarts'] == 0, s4t
        assert ctx_t.nudge_calls == 0, ctx_t.nudge_calls
        print('TEST4G_WEDGE_TASK_EXCLUDED_OK')

        # --- 4h: wedge_max_remediations 0 -> ladder disabled
        wedge_off_cfg = dict(wedge_cfg, wedge_max_remediations=0)
        write_state(fresh_tick, chats=wedge_state(12))
        ctx_o = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30, running=True)
        FakeAgentContext._all = [ctx_o]
        s4o = monitor.tick(wedge_off_cfg)
        assert s4o['wedged'] == 1 and s4o['wedge_nudged'] == 0, s4o
        assert len(ctx_o.communicated) == 0, 'ladder disabled but nudge sent'
        print('TEST4H_WEDGE_DISABLED_OK')

        # --- 5: external alert channels
        orig_post = monitor._post_json
        orig_dbg = monitor._debug_log
        import ipaddress as _cs_ipa
        from helpers import network as _cs_net
        orig_resolve = _cs_net.resolve_host_ips
        def _cs_fake_resolve(hostname):
            # Offline-deterministic DNS stub for the SSRF gate:
            # IP literals resolve to themselves (keeps the private-IP
            # rejection honest), names resolve to a fake public IP.
            # Mirrors resolve_host_ips() semantics.
            try:
                return (_cs_ipa.ip_address(hostname),)
            except ValueError:
                return (_cs_ipa.ip_address('93.184.216.34'),)
        _cs_net.resolve_host_ips = _cs_fake_resolve
        try:
            posted = []
            monitor._post_json = lambda url, payload, timeout=5.0: posted.append((url, payload)) or True
            cfg_w = {'webhook_url': 'https://hooks.example.com/abc'}
            monitor._dispatch_external(cfg_w, 'warning', 'test message', 'high', sync=True)
            assert len(posted) == 1 and posted[0][0] == 'https://hooks.example.com/abc', posted
            assert posted[0][1]['kind'] == 'warning' and posted[0][1]['priority'] == 'high', posted[0][1]
            print('TEST5A_WEBHOOK_OK')
            dbg = []
            monitor._debug_log = lambda kind, msg: dbg.append((kind, msg)) or None
            cfg_p = {'webhook_url': 'http://127.0.0.1:9999/hook', 'webhook_allow_private': False}
            monitor._dispatch_external(cfg_p, 'warning', 'test', sync=True)
            assert len(posted) == 1, posted  # no new post for private target
            assert any(k == 'webhook_skip' for k, _ in dbg), dbg
            cfg_p2 = {'webhook_url': 'http://127.0.0.1:9999/hook', 'webhook_allow_private': True}
            monitor._dispatch_external(cfg_p2, 'warning', 'test', sync=True)
            assert len(posted) == 2 and posted[1][0].startswith('http://127.0.0.1'), posted
            print('TEST5B_SSRF_GUARD_OK')
            cfg_t = {'telegram_bot_token': 'TOK123', 'telegram_chat_id': '42'}
            monitor._dispatch_external(cfg_t, 'info', 'hello', sync=True)
            assert len(posted) == 3 and '/botTOK123/sendMessage' in posted[2][0], posted
            assert posted[2][1]['chat_id'] == '42', posted[2][1]
            print('TEST5C_TELEGRAM_OK')
        finally:
            monitor._post_json = orig_post
            _cs_net.resolve_host_ips = orig_resolve
            monitor._debug_log = orig_dbg
        
        # --- 6: human-readable chat names in alerts (v1.4.0)
        import json as _json6
        _dir_a = _chat_dir(FAKE_CHAT_A)
        with open(os.path.join(_dir_a, 'chat.json'), 'w') as _f:
            _json6.dump({'id': FAKE_CHAT_A, 'name': 'Alpha Rescue Chat'}, _f)
        assert monitor._chat_display_name(FAKE_CHAT_A) == 'Alpha Rescue Chat', monitor._chat_display_name(FAKE_CHAT_A)
        print('TEST6A_FILE_NAME_OK')
        _ctx_n = FakeCtx(FAKE_CHAT_A, ['user', 'response'], idle_minutes=0)
        _ctx_n.name = 'Ctx Name Wins'
        assert monitor._chat_display_name(FAKE_CHAT_A, _ctx_n) == 'Ctx Name Wins'
        print('TEST6B_CTX_NAME_OK')
        assert monitor._chat_display_name('zzzzzzzz') == 'zzzzzzzz'
        print('TEST6C_FALLBACK_ID_OK')
        # regression: _notify('info', msg, cfg=cfg) signature (msgcfg bug)
        from helpers import notification as _cs_notif
        _orig_send = _cs_notif.NotificationManager.send_notification
        _sent = []
        _cs_notif.NotificationManager.send_notification = lambda *a, **k: _sent.append(k) or True
        try:
            assert monitor._notify('info', 'restart smoke', cfg={}) is True
            assert _sent, 'fw send_notification not called'
        finally:
            _cs_notif.NotificationManager.send_notification = _orig_send
        print('TEST6D_NOTIFY_SIG_OK')
        _orig_notify = monitor._notify
        _captured = []
        monitor._notify = lambda kind, message, priority='normal', cfg=None: _captured.append(message) or True
        try:
            write_state(fresh_tick, chats={FAKE_CHAT_A: {'status': 'stalled', 'nudge_count': 3, 'last_nudge_at': (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()}})
            _ctx_i = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
            FakeAgentContext._all = [_ctx_i]
            monitor.tick(dict(cfg, notify_on_intervention=True, notify_after_minutes=0))
            assert _captured, 'no intervention notification captured'
            assert 'Alpha Rescue Chat' in _captured[-1] and FAKE_CHAT_A in _captured[-1], _captured[-1]
        finally:
            monitor._notify = _orig_notify
        print('TEST6E_ALERT_NAME_OK')
        
        # --- 9: quiet bell (v1.7.0): persistence-gated intervention paging ---
        _orig_notify9 = monitor._notify
        _captured9 = []
        monitor._notify = lambda kind, message, priority='normal', cfg=None: _captured9.append(message) or True
        try:
            qb_cfg = dict(cfg, notify_on_intervention=True, notify_after_minutes=30, notify_rearm_minutes=60)
            stale_nudge = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
            def _qb_entry(since_min=0, notify_min=None):
                e9 = {'status': 'stalled', 'nudge_count': 3, 'last_nudge_at': stale_nudge}
                if since_min:
                    e9['intervention_since'] = (datetime.now(timezone.utc) - timedelta(minutes=since_min)).isoformat()
                if notify_min is not None:
                    e9['intervention_notify_at'] = (datetime.now(timezone.utc) - timedelta(minutes=notify_min)).isoformat()
                return e9
            # 9a: fresh episode stays silent; the clock starts
            write_state(fresh_tick, chats={FAKE_CHAT_A: _qb_entry()})
            FakeAgentContext._all = [FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)]
            monitor.tick(qb_cfg)
            assert not _captured9, _captured9
            st9 = read_state()['chats'][FAKE_CHAT_A]
            assert st9.get('intervention_since'), st9
            print('TEST9A_FRESH_EPISODE_SILENT_OK')
            # 9b: episode persisted 45m -> exactly one page
            write_state(fresh_tick, chats={FAKE_CHAT_A: _qb_entry(since_min=45)})
            FakeAgentContext._all = [FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)]
            monitor.tick(qb_cfg)
            assert len(_captured9) == 1, _captured9
            st9 = read_state()['chats'][FAKE_CHAT_A]
            assert st9.get('intervention_notify_at'), st9
            print('TEST9B_PERSISTENT_PAGE_OK')
            # 9c: re-arm while the episode persists
            write_state(fresh_tick, chats={FAKE_CHAT_A: _qb_entry(since_min=105, notify_min=70)})
            FakeAgentContext._all = [FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)]
            monitor.tick(qb_cfg)
            assert len(_captured9) == 2, _captured9
            print('TEST9C_REARM_OK')
            # 9d: recovery clears the quiet-bell clock without paging
            write_state(fresh_tick, chats={FAKE_CHAT_A: _qb_entry(since_min=45, notify_min=15)})
            FakeAgentContext._all = [FakeCtx(FAKE_CHAT_A, ['user', 'response'], idle_minutes=1)]
            monitor.tick(qb_cfg)
            st9 = read_state()['chats'][FAKE_CHAT_A]
            assert st9['status'] == 'awaiting_user', st9
            assert not st9.get('intervention_since') and not st9.get('intervention_notify_at'), st9
            assert len(_captured9) == 2, _captured9
            print('TEST9D_RECOVERY_CLEARS_OK')
        finally:
            monitor._notify = _orig_notify9
        # --- 10: goal completion gate (v1.8.0) ---
        _orig_active_goal = monitor._active_goal
        _goal_state = {'objective': 'Build the demo widget', 'status': 'active', 'updated_at': '2026-09-13T10:00:00+00:00'}
        monitor._active_goal = lambda cid: dict(_goal_state)
        try:
            gcfg = dict(cfg, goal_gate_enabled=True, goal_gate_max_nudges=2, goal_gate_cooldown_minutes=0)
            def _goal_ctx(claim=True):
                cg = FakeCtx(FAKE_CHAT_A, ['user'], idle_minutes=1)
                cg.log.logs.append(types.SimpleNamespace(
                 type='response',
                 heading='Done' if claim else 'Question',
                 content=(
                 'The task is now complete. All done!'
                 if claim else 'Which database should I use?'
                 ),
                ))
                return cg
            # 10a: completion claim + active goal -> one silent gate nudge
            write_state(fresh_tick, chats={FAKE_CHAT_A: {'status': 'running'}})
            ctx_g = _goal_ctx()
            FakeAgentContext._all = [ctx_g]
            s10 = monitor.tick(gcfg)
            assert len(ctx_g.communicated) == 1, ctx_g.communicated
            _m10 = ctx_g.communicated[0]
            _txt10 = str(getattr(_m10, 'message', None) or getattr(_m10, 'content', None) or _m10)
            assert 'goal' in _txt10.lower(), _txt10
            assert 'Build the demo widget' in _txt10, _txt10
            st10 = read_state()['chats'][FAKE_CHAT_A]
            assert st10['status'] == 'nudged', st10
            assert st10.get('goal_gate_count') == 1, st10
            assert s10.get('goal_gate_nudged') == 1, s10
            print('TEST10A_GATE_NUDGE_OK')
            # 10b: per-goal budget exhausted -> silent
            write_state(fresh_tick, chats={FAKE_CHAT_A: {'status': 'nudged', 'goal_gate_key': '2026-09-13T10:00:00+00:00', 'goal_gate_count': 2}})
            ctx_g2 = _goal_ctx()
            FakeAgentContext._all = [ctx_g2]
            monitor.tick(gcfg)
            assert len(ctx_g2.communicated) == 0, ctx_g2.communicated
            print('TEST10B_BUDGET_EXHAUSTED_OK')
            # 10c: goal record changed -> budget reset -> nudges again
            _goal_state['updated_at'] = '2026-09-13T11:30:00+00:00'
            write_state(fresh_tick, chats={FAKE_CHAT_A: {'status': 'nudged', 'goal_gate_key': '2026-09-13T10:00:00+00:00', 'goal_gate_count': 2}})
            ctx_g3 = _goal_ctx()
            FakeAgentContext._all = [ctx_g3]
            monitor.tick(gcfg)
            assert len(ctx_g3.communicated) == 1, ctx_g3.communicated
            st10 = read_state()['chats'][FAKE_CHAT_A]
            assert st10.get('goal_gate_count') == 1, st10
            assert st10.get('goal_gate_key') == '2026-09-13T11:30:00+00:00', st10
            print('TEST10C_BUDGET_RESET_OK')
            # 10d: no completion claim -> silent
            _goal_state['updated_at'] = '2026-09-13T12:00:00+00:00'
            write_state(fresh_tick, chats={FAKE_CHAT_A: {'status': 'running'}})
            ctx_g4 = _goal_ctx(claim=False)
            FakeAgentContext._all = [ctx_g4]
            monitor.tick(gcfg)
            assert len(ctx_g4.communicated) == 0, ctx_g4.communicated
            print('TEST10D_NO_CLAIM_SILENT_OK')
            # 10e: goal already final -> silent
            write_state(fresh_tick, chats={FAKE_CHAT_A: {'status': 'running'}})
            ctx_g5 = _goal_ctx()
            FakeAgentContext._all = [ctx_g5]
            monitor._active_goal = lambda cid: None
            monitor.tick(gcfg)
            assert len(ctx_g5.communicated) == 0, ctx_g5.communicated
            print('TEST10E_GOAL_FINAL_SILENT_OK')
            # 10f: gate disabled -> silent
            monitor._active_goal = lambda cid: dict(_goal_state)
            write_state(fresh_tick, chats={FAKE_CHAT_A: {'status': 'running'}})
            ctx_g6 = _goal_ctx()
            FakeAgentContext._all = [ctx_g6]
            monitor.tick(dict(gcfg, goal_gate_enabled=False))
            assert len(ctx_g6.communicated) == 0, ctx_g6.communicated
            print('TEST10F_DISABLED_SILENT_OK')
            # 10g: cooldown active -> silent
            write_state(fresh_tick, chats={FAKE_CHAT_A: {'status': 'nudged', 'goal_gate_key': '2026-09-13T12:00:00+00:00', 'goal_gate_count': 0, 'last_goal_gate_at': (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()}})
            ctx_g7 = _goal_ctx()
            FakeAgentContext._all = [ctx_g7]
            monitor.tick(dict(gcfg, goal_gate_cooldown_minutes=15))
            assert len(ctx_g7.communicated) == 0, ctx_g7.communicated
            print('TEST10G_COOLDOWN_OK')
        finally:
            monitor._active_goal = _orig_active_goal
        # --- 7: per-chat history API (v1.5.0)
        import asyncio as _aio7
        from usr.plugins.chat_shepherd.api.history import History as _CSHistory
        _h7 = _CSHistory(None, None)
        _hist7 = [
            {'chat_id': FAKE_CHAT_A, 'action': 'manual_nudge', 'nudge_count': 2, 'timestamp': '2026-09-12T08:02:00+00:00'},
            {'chat_id': FAKE_CHAT_B, 'action': 'auto_nudge', 'nudge_count': 1, 'timestamp': '2026-09-12T08:01:00+00:00'},
            {'chat_id': FAKE_CHAT_A, 'action': 'auto_nudge', 'nudge_count': 1, 'timestamp': '2026-09-12T08:00:00+00:00'},
        ]

        write_state(fresh_tick, chats={})
        for h in reversed(_hist7):
            state_mod.append_journal(h)
        _r7 = _aio7.run(_h7.process({'chat_id': FAKE_CHAT_A}, None))
        assert _r7.get('success') is True, _r7
        assert [i7['action'] for i7 in _r7['history']] == ['manual_nudge', 'auto_nudge'], _r7
        assert all(i7['chat_id'] == FAKE_CHAT_A for i7 in _r7['history']), _r7
        assert isinstance(_r7['name'], str) and _r7['name'], _r7
        _r7b = _aio7.run(_h7.process({}, None))
        assert _r7b.get('success') is False, _r7b
        _r7c = _aio7.run(_h7.process({'chat_id': FAKE_CHAT_A, 'limit': 999}, None))
        assert _r7c['success'] is True and len(_r7c['history']) == 2, _r7c
        print('TEST7_HISTORY_API_OK')

        # --- 8: append-only history journal (v1.6.0)
        import asyncio as _aio8
        write_state(fresh_tick, chats={})
        state_mod.append_journal({'chat_id': FAKE_CHAT_A, 'action': 'ev_one', 'timestamp': '2026-09-12T09:00:00+00:00'})
        files.write_file(TEST_STATE, json.dumps({'chats': {}, 'history': [], 'last_tick': fresh_tick}, ensure_ascii=False))
        state_mod.append_journal({'chat_id': FAKE_CHAT_A, 'action': 'ev_two', 'timestamp': '2026-09-12T09:01:00+00:00'})
        _j8 = state_mod.read_journal()
        assert [h['action'] for h in _j8][:2] == ['ev_two', 'ev_one'], _j8
        with open(files.get_abs_path(TEST_JOURNAL), 'a', encoding='utf-8') as _f8:
            _f8.write(chr(123) + 'broken' + chr(10))
        _j8b = state_mod.read_journal()
        assert [h['action'] for h in _j8b][:2] == ['ev_two', 'ev_one'], _j8b
        _st8 = state_mod.load_state()
        assert [h['action'] for h in _st8['history']][:2] == ['ev_two', 'ev_one'], _st8['history']
        files.write_file(TEST_STATE, json.dumps({'chats': {}, 'history': [{'chat_id': FAKE_CHAT_A, 'action': 'legacy_ev', 'timestamp': '2026-09-12T08:30:00+00:00'}], 'last_tick': fresh_tick}, ensure_ascii=False))
        os.unlink(files.get_abs_path(TEST_JOURNAL))
        _st8m = state_mod.load_state()
        assert any(h.get('action') == 'legacy_ev' for h in _st8m['history']), _st8m['history']
        _j8m = state_mod.read_journal()
        assert any(h.get('action') == 'legacy_ev' for h in _j8m), _j8m
        _ok8a = state_mod.JOURNAL_MAX_BYTES
        _ok8b = state_mod.JOURNAL_KEEP
        state_mod.JOURNAL_MAX_BYTES = 10
        state_mod.JOURNAL_KEEP = 2
        try:
            # v1.6.0: every append triggers compaction (MAX_BYTES=10) and the
            # newest-two invariant must keep all three entries (KEEP >= written).
            # Truncate instead of unlink: the 9p bind mount can diverge
            # path->inode resolution after unlink+recreate churn, and production
            # never unlinks the journal (append-only).
            open(files.get_abs_path(TEST_JOURNAL), 'w', encoding='utf-8').close()
            state_mod.append_journal({'chat_id': FAKE_CHAT_A, 'action': 'c1'})
            state_mod.append_journal({'chat_id': FAKE_CHAT_A, 'action': 'c2'})
            state_mod.append_journal({'chat_id': FAKE_CHAT_A, 'action': 'c3'})
            _j8c = state_mod.read_journal()
            assert [h['action'] for h in _j8c] == ['c3', 'c2', 'c1'], _j8c
        finally:
            state_mod.JOURNAL_MAX_BYTES = _ok8a
            state_mod.JOURNAL_KEEP = _ok8b
        from usr.plugins.chat_shepherd.api.export import Export as _CSExport
        _x8 = _CSExport(None, None)
        _r8 = _aio8.run(_x8.process({}, None))
        assert _r8.get('success') is True, _r8
        assert any(h.get('action') == 'c3' for h in _r8.get('history', [])), _r8.get('history', [])[:5]
        assert 'chats' in _r8.get('state', {}), list(_r8.keys())
        print('TEST8_JOURNAL_EXPORT_OK')

        # --- TEST11: graceful reload engine (v1.9.0) ---
        import importlib.util
        import time as _time

        from usr.plugins.chat_shepherd.helpers import hotreload

        mod_name = 'cs_hr_test_mod'
        tmp_mod = files.get_abs_path('usr/plugins/chat_shepherd/data/hr_tmp_mod.py')
        try:
            V1 = 'VALUE = 1\n\ndef get():\n    return VALUE\n'
            V2 = 'VALUE = 2\n\ndef get():\n    return VALUE\n'
            with open(tmp_mod, 'w', encoding='utf-8') as f:
                f.write(V1)
            spec = importlib.util.spec_from_file_location(mod_name, tmp_mod)
            hr_mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = hr_mod
            spec.loader.exec_module(hr_mod)
            watch = [(mod_name, tmp_mod)]

            # 11a: first call primes, second is clean
            r1 = hotreload.check_and_reload(None, modules=watch)
            assert r1['status'] == 'primed', r1
            r2 = hotreload.check_and_reload(None, modules=watch)
            assert r2['status'] == 'clean', r2

            # 11b: source change -> reload, new code live, same module object
            with open(tmp_mod, 'w', encoding='utf-8') as f:
                f.write(V2)
            _bump = _time.time() + 10
            os.utime(tmp_mod, (_bump, _bump))
            r3 = hotreload.check_and_reload(None, modules=watch)
            assert r3['status'] == 'reloaded', r3
            assert mod_name in r3['reloaded'], r3
            assert hr_mod.get() == 2, hr_mod.VALUE

            # 11c: compile error -> nothing loads, old code stays, backoff on
            with open(tmp_mod, 'w', encoding='utf-8') as f:
                f.write('def broken(:\n    pass\n')
            _bump += 10
            os.utime(tmp_mod, (_bump, _bump))
            r4 = hotreload.check_and_reload(None, modules=watch)
            assert r4['status'] == 'compile_error', r4
            assert hr_mod.get() == 2, hr_mod.VALUE
            r5 = hotreload.check_and_reload(None, modules=watch)
            assert r5['status'] == 'backoff', r5
            hotreload._fail_until = 0.0

            # 11d: exec failure -> rolled back to the previous namespace
            with open(tmp_mod, 'w', encoding='utf-8') as f:
                f.write("VALUE = 3\nraise RuntimeError('cs_hr_boom')\n")
            _bump += 10
            os.utime(tmp_mod, (_bump, _bump))
            r6 = hotreload.check_and_reload(None, modules=watch)
            assert r6['status'] == 'failed', r6
            assert r6.get('rolled_back'), r6
            assert hr_mod.get() == 2, hr_mod.VALUE
            hotreload._fail_until = 0.0

            # 11e: restore good source; config gate honors hot_reload_enabled
            with open(tmp_mod, 'w', encoding='utf-8') as f:
                f.write(V2)
            _bump += 10
            os.utime(tmp_mod, (_bump, _bump))
            r7 = hotreload.check_and_reload({'hot_reload_enabled': False}, modules=watch)
            assert r7['status'] == 'disabled', r7

            # 11f: force=True overrides the gate and lands V2
            r8 = hotreload.check_and_reload(None, force=True, modules=watch)
            assert r8['status'] == 'reloaded', r8
            assert hr_mod.get() == 2, hr_mod.VALUE
            print('TEST11_HOTRELOAD_OK')
        finally:
            try:
                os.unlink(tmp_mod)
            except Exception:
                pass
            sys.modules.pop(mod_name, None)
            hotreload._fail_until = 0.0


        # ============ TEST 12: wedge liveness probe (v1.10.0) ============
        probe_cfg = dict(wedge_cfg, wedge_liveness_probe=True)

        def liveness_state(frozen_ago_min, wedge_count=0, wedge_ago_min=0,
                           prev_updates=-1):
            d = wedge_state(frozen_ago_min, wedge_count, wedge_ago_min)
            d[FAKE_CHAT_A]['last_updates_len'] = prev_updates
            return d

        def live_ctx(cid, n_updates):
            ctx = FakeCtx(cid, ['user', 'tool'], idle_minutes=30, running=True)
            ctx.log.updates = list(range(n_updates))
            return ctx

        # --- 12a: spinning (entries frozen, mutations advancing) -> no
        # Tier-1 continue (it can never drain a dead loop), budget intact
        write_state(fresh_tick, chats=liveness_state(12, prev_updates=3))
        ctx_sp = live_ctx(FAKE_CHAT_A, 4)
        FakeAgentContext._all = [ctx_sp]
        s12a = monitor.tick(probe_cfg)
        assert s12a['wedged'] == 1 and s12a['wedge_spinning'] == 1, s12a
        assert s12a['wedge_nudged'] == 0 and s12a['wedge_restarts'] == 0, s12a
        assert len(ctx_sp.communicated) == 0, 'spinning wedge got a continue nudge'
        st12a = read_state()
        ea12a = st12a['chats'][FAKE_CHAT_A]
        assert ea12a['liveness'] == 'spinning', ea12a
        assert ea12a['wedge_nudge_count'] == 0, ('budget burned on a dead loop', ea12a)
        assert 'spinning dead loop' in ea12a['last_classification'], ea12a
        print('TEST12A_SPIN_SKIP_OK')

        # --- 12b: hung (entries + mutations both frozen) -> classic Tier 1
        write_state(fresh_tick, chats=liveness_state(12, prev_updates=3))
        ctx_h = live_ctx(FAKE_CHAT_A, 3)
        FakeAgentContext._all = [ctx_h]
        s12b = monitor.tick(probe_cfg)
        assert s12b['wedged'] == 1 and s12b['wedge_spinning'] == 0, s12b
        assert s12b['wedge_nudged'] == 1, s12b
        assert len(ctx_h.communicated) == 1, ctx_h.communicated
        st12b = read_state()
        ea12b = st12b['chats'][FAKE_CHAT_A]
        assert ea12b['liveness'] == 'hung', ea12b
        assert ea12b['wedge_nudge_count'] == 1, ea12b
        print('TEST12B_HUNG_CLASSIC_OK')

        # --- 12c: spinning + soft_restart on -> immediate kill, no Tier-1
        probe_sr_cfg = dict(probe_cfg, wedge_soft_restart=True)
        write_state(fresh_tick, chats=liveness_state(12, prev_updates=3))
        ctx_k = live_ctx(FAKE_CHAT_A, 4)
        FakeAgentContext._all = [ctx_k]
        s12c = monitor.tick(probe_sr_cfg)
        assert s12c['wedge_restarts'] == 1 and s12c['wedge_nudged'] == 0, s12c
        assert ctx_k.nudge_calls == 1, ctx_k.nudge_calls
        st12c = read_state()
        assert any(
            h.get('action') == 'wedge_soft_restart'
            and h.get('liveness') == 'spinning'
            for h in st12c['history']
        ), st12c['history']
        print('TEST12C_SPIN_KILL_OK')

        # --- 12d: probe off -> legacy ladder even with mutations advancing
        legacy_cfg = dict(wedge_cfg, wedge_liveness_probe=False)
        write_state(fresh_tick, chats=liveness_state(12, prev_updates=3))
        ctx_l = live_ctx(FAKE_CHAT_A, 4)
        FakeAgentContext._all = [ctx_l]
        s12d = monitor.tick(legacy_cfg)
        assert s12d['wedge_nudged'] == 1 and s12d['wedge_spinning'] == 0, s12d
        assert len(ctx_l.communicated) == 1, 'legacy ladder broken'
        st12d = read_state()
        assert st12d['chats'][FAKE_CHAT_A]['liveness'] == '', st12d['chats'][FAKE_CHAT_A]
        print('TEST12D_PROBE_OFF_LEGACY_OK')

        # --- 12e: classify_chat tags the reason from the stamped verdict
        status12, reason12 = monitor.classify_chat(
            FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30, running=True),
            FAKE_CHAT_A, probe_cfg, {'liveness': 'spinning'}, 12.0,
        )
        assert status12 == constants.STATUS_INTERVENTION, (status12, reason12)
        assert 'spinning dead loop' in reason12, reason12
        status12h, reason12h = monitor.classify_chat(
            FakeCtx(FAKE_CHAT_B, ['user', 'tool'], idle_minutes=30, running=True),
            FAKE_CHAT_B, probe_cfg, {'liveness': 'hung'}, 12.0,
        )
        assert status12h == constants.STATUS_INTERVENTION, (status12h, reason12h)
        assert 'hung call' in reason12h, reason12h
        print('TEST12E_REASON_TAGS_OK')


        # ============ TEST 13 - config integrity (v1.11.0 P5/P6.1) ============
        from usr.plugins.chat_shepherd import hooks as cs_hooks
        from usr.plugins.chat_shepherd.helpers import config_defaults as cfg_defaults

        # --- 13a: partial sanitize leaves unmentioned keys alone (P6.1a guard);
        # bool strings coerce properly; unknown override keys are dropped
        base13 = dict(cfg_defaults.DEFAULTS)
        out13 = cfg_defaults.apply_overrides(
            base13,
            {'stall_minutes': 7, 'wedge_liveness_probe': 'false', 'not_a_key': 'x'},
        )
        assert out13['stall_minutes'] == 7, out13
        assert out13['wedge_liveness_probe'] is False, out13
        assert out13['goal_gate_enabled'] is True, ('P6.1a regression: partial save dropped goal_gate_enabled', out13)
        assert out13['hot_reload_enabled'] is True, out13
        assert out13['notify_after_minutes'] == 30, out13
        assert 'not_a_key' not in out13, out13
        print('TEST13A_PARTIAL_SAVE_OK')

        # --- 13b: get_plugin_config hook merges defaults UNDER config.json,
        # preserves unknown keys and merges icons per status
        disk13 = {'enabled': False, 'future_key': 1, 'icons': {'running': 'X'}}
        merged13 = cs_hooks.get_plugin_config(default=disk13)
        assert merged13['enabled'] is False, merged13
        assert merged13['stall_minutes'] == 5, merged13
        assert merged13['future_key'] == 1, ('unknown key erased', merged13)
        assert merged13['icons']['running'] == 'X', merged13['icons']
        assert merged13['icons']['paused'] == cfg_defaults.DEFAULTS['icons']['paused'], merged13['icons']
        assert merged13['icons']['interrupted'] == cfg_defaults.DEFAULTS['icons']['interrupted'], merged13['icons']
        print('TEST13B_HOOK_MERGE_OK')

        # --- 13c: save hook preserves unknown disk keys on save (P6.1b)
        orig_read = cs_hooks._read_persisted
        cs_hooks._read_persisted = lambda project_name='', agent_profile='': {'legacy_key': 'v', 'stall_minutes': 5}
        try:
            saved13 = cs_hooks.save_plugin_config(settings={'stall_minutes': 7})
        finally:
            cs_hooks._read_persisted = orig_read
        assert saved13['stall_minutes'] == 7, saved13
        assert saved13['legacy_key'] == 'v', ('P6.1b regression: unknown key erased on save', saved13)
        print('TEST13C_SAVE_PRESERVE_OK')
        # ================= TEST 14: throttle snapshot (v1.12.0) =================
        # 14a: resume-drain snapshot integrity - no false capped flag
        _cfg14 = {'enabled': True, 'watch_all': True, 'stall_minutes': 5, 'max_auto_nudges': 3, 'max_nudges_per_tick': 5, 'nudge_cooldown_minutes': 0, 'notify_on_intervention': False}
        _th_stale = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        write_state(_th_stale, chats={FAKE_CHAT_A: {'status': 'running', 'nudge_count': 0, 'last_nudge_at': ''}})
        _ctx14a = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
        FakeAgentContext._all = [_ctx14a]
        monitor.tick(_cfg14)
        _th14a = read_state().get('throttle')
        assert isinstance(_th14a, dict), _th14a
        assert _th14a['nudges_this_tick'] == 1, _th14a
        assert _th14a['budget'] == 5, _th14a
        assert _th14a['capped'] is False, _th14a
        assert _th14a['stalled_queued'] == 0 and _th14a['stalled_nudged'] == 0, _th14a
        assert _th14a['resume_nudged'] == 1, _th14a
        assert _th14a['wedge_nudged'] == 0 and _th14a['wedge_restarts'] == 0, _th14a
        assert _th14a['wedge_budget'] == 5, _th14a
        assert _th14a['timestamp'], _th14a
        print('TEST14A_THROTTLE_RESUME_OK')
        # 14b: stalled queue truncated by the per-tick budget -> capped
        _cfg14b = dict(_cfg14, max_nudges_per_tick=1)
        _now14 = datetime.now(timezone.utc)
        write_state((_now14 - timedelta(minutes=1)).isoformat(), chats={
         FAKE_CHAT_A: {'status': 'running', 'nudge_count': 0, 'last_nudge_at': (_now14 - timedelta(minutes=20)).isoformat()},
         FAKE_CHAT_B: {'status': 'running', 'nudge_count': 0, 'last_nudge_at': (_now14 - timedelta(minutes=25)).isoformat()},
        })
        _ctx14a2 = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
        _ctx14b2 = FakeCtx(FAKE_CHAT_B, ['user', 'tool'], idle_minutes=35)
        FakeAgentContext._all = [_ctx14a2, _ctx14b2]
        _s14b = monitor.tick(_cfg14b)
        assert _s14b['nudged'] == 1, _s14b
        assert _s14b['nudges_this_tick'] == 1, _s14b
        _th14b = read_state()['throttle']
        assert _th14b['budget'] == 1, _th14b
        assert _th14b['stalled_queued'] == 2, _th14b
        assert _th14b['stalled_nudged'] == 1, _th14b
        assert _th14b['capped'] is True, _th14b
        print('TEST14B_THROTTLE_CAPPED_OK')

        # ============ TEST 15 - adaptive thresholds (v1.12.0 R4) ============
        from usr.plugins.chat_shepherd.helpers import adaptive as adaptive_mod

        def _adaptive_cfg(**over):
            c = {
                'adaptive_thresholds': True,
                'adaptive_interval_minutes': 60,
                'adaptive_min_samples': 10,
                'adaptive_step_pct': 10,
                'nudge_cooldown_minutes': 10,
                'wedge_nudge_after_minutes': 10,
                'wedge_remediation_cooldown_minutes': 10,
            }
            c.update(over)
            return c

        def _journal_actions(action, count, minutes_ago=0):
            # v1.18.1: +1s nudge for fresh entries. Windows datetime.now()
            # is quantized (~15.6ms ticks): a fresh fixture timestamp can
            # land on the SAME clock tick as the adaptive window_start,
            # and window_rates' deliberate `ts <= window_start → skip`
            # boundary then silently drops the whole batch (TEST15E
            # intermittently saw wedge sent=0/effective=3). Entries must
            # sit strictly INSIDE the window; the +1s shift is invisible
            # to the stale (minutes_ago) fixtures and to the rates math.
            ts = (
                datetime.now(timezone.utc)
                + timedelta(seconds=1)
                - timedelta(minutes=minutes_ago)
            ).isoformat()
            for _ in range(count):
                state_mod.append_journal({
                    'chat_id': FAKE_CHAT_A,
                    'action': action,
                    'timestamp': ts,
                })

        # --- 15a: off by default - evaluate() inert, tick tags 'off'
        write_state(now_iso)
        r15a = adaptive_mod.evaluate({}, {'enabled': True})
        assert r15a['enabled'] is False and r15a.get('reason') == 'disabled', r15a
        FakeAgentContext._all = [FakeCtx(FAKE_CHAT_A, ['user', 'response'], idle_minutes=1)]
        cfg15a = dict(cfg)
        cfg15a['adaptive_thresholds'] = False
        s15a = monitor.tick(cfg15a)
        assert s15a['adaptive'] == 'off', s15a
        print('TEST15A_DISABLED_OK')

        # --- 15b: first run seeds the baseline from configured values
        write_state(now_iso)
        st15b = {}
        cfg15b = _adaptive_cfg()
        r15b = adaptive_mod.evaluate(st15b, cfg15b)
        assert r15b['reason'] == 'reseeded', r15b
        assert st15b['adaptive']['learned']['nudge_cooldown_minutes'] == 10.0, st15b
        assert st15b['adaptive']['learned']['wedge_nudge_after_minutes'] == 10.0, st15b
        assert st15b['adaptive']['last_eval'] == '', st15b
        print('TEST15B_SEED_OK')

        # --- 15c: journal window counts only fresh outcomes; relax moves
        # the wedge knobs and the nudge family independently
        write_state(now_iso)
        st15c = {}
        cfg15c = _adaptive_cfg()
        assert adaptive_mod.evaluate(st15c, cfg15c)['reason'] == 'reseeded'
        _journal_actions('wedge_auto_continue', 5, minutes_ago=180)  # stale
        _journal_actions('wedge_auto_continue', 12)
        _journal_actions('wedge_nudge_effective', 9)
        _journal_actions('auto_nudge', 10)
        _journal_actions('nudge_effective', 8)
        rates15 = adaptive_mod.window_rates(st15c['adaptive']['window_start'])
        assert rates15['wedge'] == {'sent': 12, 'effective': 9}, rates15
        assert rates15['nudge'] == {'sent': 10, 'effective': 8}, rates15
        r15c = adaptive_mod.evaluate(st15c, cfg15c)
        assert r15c['evaluated'] is True and r15c['changed'] is True, r15c
        d15c = r15c['decisions']
        assert d15c['wedge']['reason'] == 'relax' and d15c['wedge']['rate'] == 0.75, d15c
        assert d15c['nudge']['reason'] == 'relax' and d15c['nudge']['rate'] == 0.8, d15c
        learned15 = st15c['adaptive']['learned']
        assert learned15['wedge_nudge_after_minutes'] == 9.0, learned15
        assert learned15['wedge_remediation_cooldown_minutes'] == 9.0, learned15
        assert learned15['nudge_cooldown_minutes'] == 9.0, learned15
        assert any(h.get('action') == 'adaptive_adjusted' for h in st15c['history']), st15c['history']
        r15c2 = adaptive_mod.evaluate(st15c, cfg15c)
        assert r15c2['reason'] == 'not_due', r15c2
        print('TEST15C_WINDOW_RELAX_OK')

        # --- 15d: tick() applies the learned overlay - a chat frozen for
        # 9.5m wedges at the learned 9.0 grace (configured 10 would not fire)
        write_state(now_iso, chats={
            FAKE_CHAT_A: {
                'status': 'running', 'nudge_count': 0, 'last_nudge_at': '',
                'last_log_len': 2,
                'log_len_since': (
                    datetime.now(timezone.utc) - timedelta(minutes=9.5)
                ).isoformat(),
                'last_updates_len': -1, 'liveness': '',
                'wedge_nudge_count': 0, 'wedge_nudges_sent': 0,
                'wedge_nudges_effective': 0, 'last_wedge_nudge_at': '',
            },
        })
        st15d = json.loads(files.read_file(files.get_abs_path(TEST_STATE)))
        st15d['adaptive'] = {
            'seed': {
                'nudge_cooldown_minutes': 10.0,
                'wedge_nudge_after_minutes': 10.0,
                'wedge_remediation_cooldown_minutes': 10.0,
            },
            'learned': {
                'nudge_cooldown_minutes': 10.0,
                'wedge_nudge_after_minutes': 9.0,
                'wedge_remediation_cooldown_minutes': 10.0,
            },
            'window_start': (
                datetime.now(timezone.utc) - timedelta(hours=2)
            ).isoformat(),
            'last_eval': '',
        }
        files.write_file(TEST_STATE, json.dumps(st15d))
        _truncate_journal()
        _journal_actions('wedge_auto_continue', 6)
        _journal_actions('wedge_nudge_effective', 6)
        cfg15d = {
            'enabled': True, 'watch_all': True,
            'stall_minutes': 5, 'max_auto_nudges': 3,
            'max_nudges_per_tick': 5, 'nudge_cooldown_minutes': 10,
            'notify_on_intervention': False,
            'wedge_nudge_after_minutes': 10,
            'wedge_max_remediations': 2,
            'wedge_remediation_cooldown_minutes': 10,
            'wedge_soft_restart': False,
            'wedge_liveness_probe': True,
            'adaptive_thresholds': True,
            'adaptive_interval_minutes': 60,
            'adaptive_min_samples': 5,
            'adaptive_step_pct': 10,
        }
        ctx15d = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=1, running=True)
        FakeAgentContext._all = [ctx15d]
        s15d = monitor.tick(cfg15d)
        assert s15d['adaptive'] == 'relax', s15d
        assert s15d['wedge_nudged'] == 1, s15d
        assert len(ctx15d.communicated) == 1, ctx15d.communicated
        st15d2 = read_state()
        assert st15d2['adaptive']['learned']['wedge_nudge_after_minutes'] == 8.0, st15d2['adaptive']
        print('TEST15D_TICK_OVERLAY_OK')

        # --- 15e: low remediation success tightens the wedge grace
        write_state(now_iso)
        st15e = {}
        cfg15e = _adaptive_cfg()
        adaptive_mod.evaluate(st15e, cfg15e)
        _journal_actions('wedge_auto_continue', 12)
        _journal_actions('wedge_nudge_effective', 3)
        r15e = adaptive_mod.evaluate(st15e, cfg15e)
        d15e = r15e['decisions']['wedge']
        assert d15e['reason'] == 'tighten' and d15e['rate'] == 0.25, d15e
        assert st15e['adaptive']['learned']['wedge_nudge_after_minutes'] == 11.0, st15e
        print('TEST15E_TIGHTEN_OK')

        # --- 15f: a manual settings change re-seeds (settings always win)
        r15f = adaptive_mod.evaluate(st15e, _adaptive_cfg(nudge_cooldown_minutes=12))
        assert r15f['reason'] == 'reseeded', r15f
        assert st15e['adaptive']['learned']['nudge_cooldown_minutes'] == 12.0, st15e
        assert st15e['adaptive']['seed']['nudge_cooldown_minutes'] == 12.0, st15e
        print('TEST15F_RESEED_OK')

        # --- 15g: an out-of-band configured knob is pinned, never learned
        write_state(now_iso)
        st15g = {}
        cfg15g = _adaptive_cfg(wedge_remediation_cooldown_minutes=240)
        adaptive_mod.evaluate(st15g, cfg15g)
        _journal_actions('wedge_auto_continue', 12)
        _journal_actions('wedge_nudge_effective', 9)
        r15g = adaptive_mod.evaluate(st15g, cfg15g)
        d15g = r15g['decisions']['wedge']
        assert d15g['reason'] == 'relax', d15g
        assert 'wedge_remediation_cooldown_minutes' in d15g['pinned'], d15g
        # learned dict is seeded from configured values, so the pinned knob
        # still holds the out-of-band 240 there - but it is never stepped
        # and never applied at runtime (effective keeps the user setting)
        assert st15g['adaptive']['learned']['wedge_remediation_cooldown_minutes'] == 240.0, st15g
        assert st15g['adaptive']['learned']['wedge_nudge_after_minutes'] == 9.0, st15g
        eff15 = adaptive_mod.effective_values(cfg15g, st15g['adaptive']['learned'])
        assert eff15['wedge_remediation_cooldown_minutes'] == 240.0, eff15
        assert eff15['wedge_nudge_after_minutes'] == 9.0, eff15
        assert adaptive_mod.effective_values(cfg15g, None) == adaptive_mod.configured_values(cfg15g)
        print('TEST15G_PINNED_KNOB_OK')

        # --- 16: R4 dashboard aggregates (v1.15.0, adopted nested schema)
        import asyncio as _aio16
        from usr.plugins.chat_shepherd.helpers import aggregates as _agg16
        from usr.plugins.chat_shepherd.api.status import Status as _CSStatus16
        _h16 = _CSStatus16(None, None)
        _now16 = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
        _h16a = [
            {'chat_id': FAKE_CHAT_A, 'action': 'auto_nudge', 'timestamp': '2026-09-15T08:00:00+00:00'},
            {'chat_id': FAKE_CHAT_A, 'action': 'manual_nudge', 'timestamp': '2026-09-15T09:30:00+00:00'},
            {'chat_id': FAKE_CHAT_B, 'action': 'auto_nudge', 'timestamp': '2026-09-14T23:10:00+00:00'},
            {'chat_id': FAKE_CHAT_A, 'action': 'nudge_effective', 'minutes_after_nudge': 2.0, 'timestamp': '2026-09-15T08:03:00+00:00'},
            {'chat_id': FAKE_CHAT_A, 'action': 'nudge_effective', 'minutes_after_nudge': 4.0, 'timestamp': '2026-09-15T09:34:00+00:00'},
            {'chat_id': FAKE_CHAT_B, 'action': 'wedge_auto_continue', 'timestamp': '2026-09-13T10:00:00+00:00'},
            {'chat_id': FAKE_CHAT_B, 'action': 'wedge_soft_restart', 'timestamp': '2026-09-13T10:20:00+00:00'},
            {'chat_id': FAKE_CHAT_B, 'action': 'wedge_nudge_effective', 'minutes_after_nudge': 1.5, 'timestamp': '2026-09-13T10:25:00+00:00'},
            {'chat_id': FAKE_CHAT_A, 'action': 'auto_nudge', 'timestamp': '2026-08-01T10:00:00+00:00'},
            {'chat_id': FAKE_CHAT_A, 'action': 'auto_nudge', 'timestamp': '2026-09-16T10:00:00+00:00'},
            {'chat_id': FAKE_CHAT_A, 'action': 'restart_resume', 'timestamp': '2026-09-15T07:00:00+00:00'},
            {'chat_id': FAKE_CHAT_A, 'action': 'nudge_effective', 'minutes_after_nudge': 'bogus', 'timestamp': '2026-09-15T06:30:00+00:00'},
        ]
        _a16 = _agg16.compute(_h16a, now=_now16)
        assert _a16['window_days'] == 7, _a16
        assert _a16['events_in_window'] == 10, _a16
        assert _a16['stalls']['episodes'] == 2, _a16
        assert _a16['stalls']['nudges_sent'] == 2, _a16
        assert _a16['stalls']['recovered'] == 3, _a16
        assert abs(_a16['stalls']['per_day'] - 1.0) < 0.1, _a16
        assert _a16['resume']['stall']['samples'] == 2, _a16
        assert _a16['resume']['stall']['mean_minutes'] == 3.0, _a16
        assert _a16['resume']['stall']['median_minutes'] == 3.0, _a16
        assert _a16['wedge']['attempts'] == 2, _a16
        assert _a16['wedge']['resolved'] == 1, _a16
        assert _a16['wedge']['success_rate_pct'] == 50, _a16
        assert _a16['wedge']['continues'] == 1 and _a16['wedge']['soft_restarts'] == 1, _a16
        assert _a16['resume']['wedge']['samples'] == 1, _a16
        assert _a16['resume']['wedge']['mean_minutes'] == 1.5, _a16
        print('TEST16A_COMPUTE_OK')
        _a16b = _agg16.compute([], now=_now16)
        assert _a16b['stalls']['episodes'] == 0 and _a16b['resume']['stall']['mean_minutes'] is None, _a16b
        assert _a16b['wedge']['success_rate_pct'] is None, _a16b
        _a16c = _agg16.compute('garbage', now=_now16)
        assert _a16c['stalls']['episodes'] == 0, _a16c
        _a16d = _agg16.compute(_h16a, now=_now16, window_days='bogus')
        assert _a16d['window_days'] == 7, _a16d
        print('TEST16B_DEGENERATE_OK')
        write_state(datetime.now(timezone.utc).isoformat(), chats={})
        _today16 = datetime.now(timezone.utc).isoformat()
        state_mod.append_journal({'chat_id': FAKE_CHAT_A, 'action': 'auto_nudge', 'timestamp': _today16})
        state_mod.append_journal({'chat_id': FAKE_CHAT_A, 'action': 'nudge_effective', 'minutes_after_nudge': 3.0, 'timestamp': _today16})
        _r16 = _aio16.run(_h16.process({}, None))
        assert _r16.get('success') is True, _r16
        _agg16s = _r16.get('aggregates')
        assert isinstance(_agg16s, dict) and _agg16s['stalls']['episodes'] == 1, _agg16s
        assert _agg16s['resume']['stall']['mean_minutes'] == 3.0 and _agg16s['resume']['stall']['samples'] == 1, _agg16s
        assert _agg16s['wedge']['success_rate_pct'] is None, _agg16s
        assert _agg16s['window_days'] == 7, _agg16s
        print('TEST16C_STATUS_BLOCK_OK')

        # ================= TEST 17: supervised resume drafts (v1.16.0) =================
        import asyncio as _aio17
        from usr.plugins.chat_shepherd.api.draft import Draft as _CSDraft17
        from usr.plugins.chat_shepherd.api import draft as _draftmod17

        _cfg17 = dict(cfg)
        _cfg17['supervised_mode'] = True

        # --- 17A: restart -> editable draft, nothing auto-sent ---
        write_state((datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(), chats={
            FAKE_CHAT_A: {'status': 'running', 'nudge_count': 0, 'last_nudge_at': ''},
        })
        ctx_17a = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
        FakeAgentContext._all = [ctx_17a]
        s17a = monitor.tick(_cfg17)
        assert s17a['interrupted'] == 1, s17a
        assert s17a['resumed'] == 0, s17a
        assert s17a['drafted'] == 1, s17a
        assert s17a['nudges_this_tick'] == 0, s17a
        assert len(ctx_17a.communicated) == 0, ctx_17a.communicated
        st17a = read_state()
        assert len(st17a.get('drafts', [])) == 1, st17a.get('drafts')
        _d17a = st17a['drafts'][0]
        assert _d17a['kind'] == 'restart_resume', _d17a
        assert _d17a['chat_id'] == FAKE_CHAT_A, _d17a
        assert _d17a['text'].startswith('[chat_shepherd] The Agent Zero server was restarted'), _d17a['text']
        assert '(situation: Server restarted while chat was running' in _d17a['text'], _d17a['text']
        assert st17a['chats'][FAKE_CHAT_A]['status'] == 'interrupted', st17a['chats'][FAKE_CHAT_A]
        print('TEST17A_SUPERVISED_RESTART_DRAFT_OK')

        # --- 17B: stall -> draft instead of auto-nudge ---
        def _stall_setup17():
            write_state((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), chats={
                FAKE_CHAT_A: {
                    'status': 'stalled',
                    'nudge_count': 0,
                    'last_nudge_at': (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
                },
            })

        _stall_setup17()
        ctx_17b = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
        FakeAgentContext._all = [ctx_17b]
        s17b = monitor.tick(_cfg17)
        assert s17b['stalled'] == 1, s17b
        assert s17b['nudged'] == 0, s17b
        assert s17b['drafted'] == 1, s17b
        assert len(ctx_17b.communicated) == 0, ctx_17b.communicated
        st17b = read_state()
        assert len(st17b.get('drafts', [])) == 1, st17b.get('drafts')
        _d17b = st17b['drafts'][0]
        assert _d17b['kind'] == 'stall_nudge', _d17b
        assert _d17b['text'].startswith('[chat_shepherd] You stopped unexpectedly mid-task'), _d17b['text']
        assert st17b['chats'][FAKE_CHAT_A]['status'] == 'stalled', st17b['chats'][FAKE_CHAT_A]
        print('TEST17B_SUPERVISED_STALL_DRAFT_OK')

        # --- 17C: supervised drafts ignore max_nudges_per_tick=0 ---
        _stall_setup17()
        ctx_17c = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
        FakeAgentContext._all = [ctx_17c]
        s17c = monitor.tick(dict(_cfg17, max_nudges_per_tick=0))
        assert s17c['drafted'] == 1, s17c
        assert len(ctx_17c.communicated) == 0, ctx_17c.communicated
        print('TEST17C_SUPERVISED_BUDGET_ZERO_OK')

        # --- 17D: supervised off -> legacy auto-send, no drafts ---
        _stall_setup17()
        ctx_17d = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
        FakeAgentContext._all = [ctx_17d]
        s17d = monitor.tick(cfg)
        assert s17d['nudged'] == 1, s17d
        assert s17d['drafted'] == 0, s17d
        assert len(ctx_17d.communicated) == 1, ctx_17d.communicated
        assert read_state().get('drafts', []) == []
        print('TEST17D_SUPERVISED_OFF_OK')

        # --- 17E: one draft per chat + dismiss requeue gate ---
        _stall_setup17()
        ctx_17e = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
        FakeAgentContext._all = [ctx_17e]
        assert monitor.tick(_cfg17)['drafted'] == 1
        ctx_17e2 = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
        FakeAgentContext._all = [ctx_17e2]
        assert monitor.tick(_cfg17)['drafted'] == 0, 'dedupe failed'
        assert len(read_state().get('drafts', [])) == 1
        st17e = read_state()
        assert state_mod.remove_draft(st17e, st17e['drafts'][0]['id'])
        state_mod.save_state(st17e)
        _chats17e = read_state()['chats']
        _chats17e[FAKE_CHAT_A]['draft_dismissed_at'] = datetime.now(timezone.utc).isoformat()
        write_state((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), chats=_chats17e)
        ctx_17e3 = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
        FakeAgentContext._all = [ctx_17e3]
        assert monitor.tick(_cfg17)['drafted'] == 0, 'requeue gate failed'
        _chats17e[FAKE_CHAT_A]['draft_dismissed_at'] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        write_state((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), chats=_chats17e)
        ctx_17e4 = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
        FakeAgentContext._all = [ctx_17e4]
        assert monitor.tick(_cfg17)['drafted'] == 1, 'requeue gate stuck closed'
        print('TEST17E_DEDUPE_REQUEUE_OK')

        # --- 17F: /draft API send + dismiss + unknown id ---
        write_state((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), chats={
            FAKE_CHAT_A: {'status': 'stalled', 'nudge_count': 0, 'last_nudge_at': ''},
        })
        st17f = state_mod.load_state()
        assert state_mod.add_draft(st17f, FAKE_CHAT_A, 'stall_nudge', reason='unit stall')
        did17 = st17f['drafts'][0]['id']
        state_mod.save_state(st17f)
        ctx_17f = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=5)
        _orig17 = _draftmod17._get_context
        _draftmod17._get_context = lambda cid: ctx_17f
        try:
            _h17 = _CSDraft17(None, None)
            _r17 = _aio17.run(_h17.process({'draft_id': did17, 'action': 'send', 'text': 'please resume carefully'}, None))
            assert _r17.get('success') is True, _r17
            assert len(ctx_17f.communicated) == 1, ctx_17f.communicated
            assert 'please resume carefully' in str(ctx_17f.communicated[0])
            st17f2 = read_state()
            assert st17f2.get('drafts', []) == [], st17f2.get('drafts')
            _e17f = st17f2['chats'][FAKE_CHAT_A]
            assert _e17f['status'] == 'nudged', _e17f
            assert _e17f['nudge_count'] == 1, _e17f
            assert 'draft_sent' in [h.get('action') for h in st17f2.get('history', [])], st17f2.get('history')
            st17f3 = state_mod.load_state()
            assert state_mod.add_draft(st17f3, FAKE_CHAT_A, 'restart_resume', reason='r2')
            did17b = st17f3['drafts'][0]['id']
            state_mod.save_state(st17f3)
            _r17b = _aio17.run(_h17.process({'draft_id': did17b, 'action': 'dismiss'}, None))
            assert _r17b.get('success') is True, _r17b
            st17f4 = read_state()
            assert st17f4.get('drafts', []) == []
            assert st17f4['chats'][FAKE_CHAT_A].get('draft_dismissed_at'), st17f4['chats'][FAKE_CHAT_A]
            assert 'draft_dismissed' in [h.get('action') for h in st17f4.get('history', [])], st17f4.get('history')
            _r17c = _aio17.run(_h17.process({'draft_id': 'nope123', 'action': 'send'}, None))
            assert _r17c.get('success') is False, _r17c
        finally:
            _draftmod17._get_context = _orig17
        FakeAgentContext._all = []
        print('TEST17F_DRAFT_API_OK')
        # ================= TEST 18: state save durability gate (v1.17.0) =================
        import time as _time18
        _orig_save18 = state_mod.save_state
        _saves18 = []
        state_mod.save_state = lambda st: _saves18.append(st)
        try:
            _cfg18 = {'enabled': True, 'watch_all': True, 'stall_minutes': 5, 'max_auto_nudges': 3, 'max_nudges_per_tick': 5, 'nudge_cooldown_minutes': 0, 'notify_on_intervention': False, 'save_state_interval_seconds': 120}
            # 18a: quiet tick inside the interval skips the write
            write_state((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), chats={})
            FakeAgentContext._all = []
            state_mod._state_last_save = _time18.monotonic()
            _saves18.clear()
            _s18a = monitor.tick(_cfg18)
            assert len(_saves18) == 0, 'quiet tick must skip the save'
            assert _s18a.get('state_save_skipped') is True, _s18a
            assert read_state().get('throttle', {}).get('state_save_skipped') is None, (
                'skipped tick must not persist a throttle snapshot'
            )
            print('TEST18A_QUIET_SKIP_OK')
            # 18b: eventful tick saves immediately despite fresh _state_last_save
            _saves18.clear()
            write_state((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), chats={
                FAKE_CHAT_A: {
                    'status': 'running',
                    'nudge_count': 0,
                    'last_nudge_at': (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
                },
            })
            _ctx18 = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
            FakeAgentContext._all = [_ctx18]
            _s18b = monitor.tick(_cfg18)
            assert (_s18b.get('nudged', 0) + _s18b.get('resumed', 0)) >= 1, _s18b
            assert len(_saves18) == 1, 'eventful tick must save immediately'
            assert _s18b.get('state_save_skipped') is False, _s18b
            print('TEST18B_EVENTFUL_FORCE_OK')
            # 18c: interval 0 = legacy write-every-tick on a quiet tick
            _saves18.clear()
            FakeAgentContext._all = []
            write_state((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), chats={})
            _s18c = monitor.tick(dict(_cfg18, save_state_interval_seconds=0))
            assert len(_saves18) == 1, 'interval 0 must always save'
            assert _s18c.get('state_save_skipped') is False, _s18c
            print('TEST18C_INTERVAL_ZERO_OK')
            # 18d: quiet tick saves once the interval has elapsed
            _saves18.clear()
            state_mod._state_last_save = _time18.monotonic() - 9999.0
            _s18d = monitor.tick(_cfg18)
            assert len(_saves18) == 1, 'due quiet tick must save'
            assert _s18d.get('state_save_skipped') is False, _s18d
            print('TEST18D_DUE_SAVE_OK')
        finally:
            state_mod.save_state = _orig_save18
        FakeAgentContext._all = []
        print('TEST18_DURABILITY_OK')
        # ================= TEST 19: serialized state RMW (v1.18.0) =================
        import time as _time19
        _orig_save19 = state_mod.save_state
        _saves19 = []
        # v1.21.0 (O3): save_state takes the transaction's already-computed
        # signature as an optional second argument, so the stub must accept it.
        state_mod.save_state = lambda st, signature=None: _saves19.append(st)
        try:
            # 19a: transaction persists a durable change exactly once, returns its result
            write_state((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), chats={
                FAKE_CHAT_A: {'status': 'running', 'nudge_count': 0, 'last_nudge_at': ''},
            })
            _saves19.clear()
            def _txn19a(st):
                state_mod.update_chat(st, FAKE_CHAT_A, status='nudged', nudge_count=1)
                return {'ok': 1}
            _r19a = state_mod.state_transaction_apply(_txn19a)
            assert _r19a == {'ok': 1}, _r19a
            assert len(_saves19) == 1, _saves19
            _saved19a = _saves19[0]['chats'][FAKE_CHAT_A]
            assert _saved19a['nudge_count'] == 1 and _saved19a['status'] == 'nudged', _saved19a
            print('TEST19A_TXN_SAVE_OK')

            # 19b: mutation-free transaction stays write-free (early-return paths)
            _saves19.clear()
            def _txn19b(st):
                return {'peek': True}
            assert state_mod.state_transaction_apply(_txn19b) == {'peek': True}
            assert len(_saves19) == 0, 'no-durable-change txn must not save'
            print('TEST19B_NOCHANGE_WRITEFREE_OK')

            # 19c: exception inside apply_fn -> propagates, nothing saved, no partial state
            _saves19.clear()
            def _txn19c(st):
                state_mod.update_chat(st, FAKE_CHAT_A, nudge_count=99)
                raise RuntimeError('boom19')
            _raised19 = False
            try:
                state_mod.state_transaction_apply(_txn19c)
            except RuntimeError:
                _raised19 = True
            assert _raised19, 'exception must propagate'
            assert _saves19 == [], 'failed txn must not save'
            print('TEST19C_EXCEPTION_ATOMIC_OK')

            # 19d: handler transaction blocks while the state lock is held, then completes
            _saves19.clear()
            _seen19 = []
            def _txn19d(st):
                _seen19.append('inside')
                return {'ok': True}
            with state_mod.state_lock():
                _th19 = threading.Thread(target=lambda: state_mod.state_transaction_apply(_txn19d))
                _th19.daemon = True
                _th19.start()
                _time19.sleep(0.3)
                assert _seen19 == [], 'txn must block while the tick lock is held'
            _th19.join(timeout=5)
            assert _seen19 == ['inside'], _seen19
            print('TEST19D_LOCK_SERIALIZES_OK')

            # 19e: the whole tick holds the lock, so handler txns cannot interleave
            FakeAgentContext._all = []
            write_state((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), chats={})
            _saves19.clear()
            _tick_done19 = []
            _cfg19 = {'enabled': True, 'watch_all': True, 'stall_minutes': 5, 'max_auto_nudges': 3, 'max_nudges_per_tick': 5, 'nudge_cooldown_minutes': 0, 'notify_on_intervention': False}
            def _run_tick19():
                monitor.tick(_cfg19)
                _tick_done19.append(True)
            with state_mod.state_lock():
                _th19b = threading.Thread(target=_run_tick19)
                _th19b.daemon = True
                _th19b.start()
                _time19.sleep(0.4)
                assert _tick_done19 == [], 'tick must hold the state lock for its whole RMW'
            _th19b.join(timeout=5)
            assert _tick_done19 == [True], _tick_done19
            print('TEST19E_TICK_HOLDS_LOCK_OK')
        finally:
            state_mod.save_state = _orig_save19
            FakeAgentContext._all = []
            print('TEST19_SERIALIZATION_OK')

        # ============ TEST 20 - v1.18.1 declog + fatal-stop guard ============
        # 20a: last log item type 'error' classifies as error (no nudge)
        ctx_e = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
        ctx_e.log.logs.append(types.SimpleNamespace(type='error'))
        status20a, reason20a = monitor.classify_chat(ctx_e, FAKE_CHAT_A, cfg, {}, 0.0)
        assert status20a == constants.STATUS_ERROR, (status20a, reason20a)
        print('TEST20A_ERROR_LOG_IS_FATAL_OK')

        # 20b: infection-check style TERMINATED warning is fatal
        ctx_t = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
        ctx_t.log.logs.append(types.SimpleNamespace(
            type='warning', heading='Infection check: TERMINATED'))
        status20b, reason20b = monitor.classify_chat(ctx_t, FAKE_CHAT_A, cfg, {}, 0.0)
        assert status20b == constants.STATUS_ERROR, (status20b, reason20b)
        print('TEST20B_TERMINATED_WARNING_IS_FATAL_OK')

        # 20c: a plain warning is NOT fatal (stalled classification unchanged)
        ctx_w = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
        ctx_w.log.logs.append(types.SimpleNamespace(type='warning', heading='just a note'))
        status20c, _r20c = monitor.classify_chat(ctx_w, FAKE_CHAT_A, cfg, {}, 0.0)
        assert status20c in (constants.STATUS_STALLED, constants.STATUS_INTERVENTION), status20c
        print('TEST20C_PLAIN_WARNING_NOT_FATAL_OK')

        # 20d: a fresh response after the error clears the fatal verdict
        ctx_r = FakeCtx(FAKE_CHAT_A, ['user', 'tool', 'error'], idle_minutes=30)
        ctx_r.log.logs.append(types.SimpleNamespace(type='response'))
        status20d, _r20d = monitor.classify_chat(ctx_r, FAKE_CHAT_A, cfg, {}, 0.0)
        assert status20d == constants.STATUS_AWAITING_USER, status20d
        print('TEST20D_RESPONSE_CLEARS_FATAL_OK')

        # 20e: the tick must NOT auto-nudge a fatally-stopped chat
        write_state(fresh_tick, chats={FAKE_CHAT_A: {'status': 'stalled', 'nudge_count': 0, 'last_nudge_at': ''}})
        ctx_n = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
        ctx_n.log.logs.append(types.SimpleNamespace(type='error'))
        FakeAgentContext._all = [ctx_n]
        s20 = monitor.tick(dict(cfg, notify_on_intervention=False))
        assert len(ctx_n.communicated) == 0, ctx_n.communicated
        st20 = read_state()
        assert st20['chats'][FAKE_CHAT_A]['status'] == 'error', st20['chats'][FAKE_CHAT_A]
        print('TEST20E_NO_NUDGE_ON_FATAL_OK')

        # 20f: chat-dir listing cache consistent with real dirs
        ids20 = monitor._chat_dir_ids()
        assert ids20 is not None and {FAKE_CHAT_A, FAKE_CHAT_B, FAKE_CHAT_C} <= set(ids20), ids20
        assert monitor._has_chat_dir(FAKE_CHAT_A) is True
        assert monitor._has_chat_dir('zzzzzzzz') is False
        print('TEST20F_DIR_CACHE_OK')

        # 20g: journal read cache sees appends (stat-key invalidation)
        _jr1 = state_mod.read_journal()
        state_mod.append_journal({'chat_id': FAKE_CHAT_A, 'action': 'cache_probe', 'timestamp': now_iso})
        _jr2 = state_mod.read_journal()
        # A stale cached parse would not contain the freshly appended entry;
        # the read cap may hide the length delta on busy journals.
        assert _jr2 and _jr2[0]['action'] == 'cache_probe', _jr2[:1]
        assert any(e.get('action') == 'cache_probe' for e in _jr2), 'probe entry invisible'
        print('TEST20G_JOURNAL_CACHE_OK')

        # ============ TEST 21 - v1.18.2 terminate-loop hardening ============
        # 21a: an in-flight infection check (log.progress) is RUNNING, never
        # stalled/wedged - the analysis freeze must not be nudged.
        ctx_ic = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30, running=True, progress='Infection check: analyzing...')
        s21a, r21a = monitor.classify_chat(ctx_ic, FAKE_CHAT_A, cfg, {}, 30.0)
        assert s21a == constants.STATUS_RUNNING, (s21a, r21a)
        assert 'Safety check in progress' in r21a, r21a
        # control: same frozen running chat without the gate wedges
        ctx_nc = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30, running=True, progress='')
        s21a2, _r21a2 = monitor.classify_chat(ctx_nc, FAKE_CHAT_A, cfg, {}, 30.0)
        assert s21a2 == constants.STATUS_INTERVENTION, (s21a2, _r21a2)
        print('TEST21A_INFECTION_CHECK_RUNNING_OK')

        # 21b: TERMINATED warning stamps terminated_at durably; nudges stay
        # suppressed even when the warning is buried under shepherd-nudge items
        write_state(fresh_tick, chats={FAKE_CHAT_A: {'status': 'stalled', 'nudge_count': 0, 'last_nudge_at': ''}})
        ctx_b1 = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
        ctx_b1.log.logs.append(types.SimpleNamespace(type='warning', heading='Infection check: TERMINATED'))
        ctx_b1.log.logs.append(types.SimpleNamespace(type='user', content='[chat_shepherd] nudge'))
        FakeAgentContext._all = [ctx_b1]
        monitor.tick(dict(cfg, notify_on_intervention=False))
        st21b = read_state()
        assert st21b['chats'][FAKE_CHAT_A].get('terminated_at'), st21b['chats'][FAKE_CHAT_A]
        assert len(ctx_b1.communicated) == 0, ctx_b1.communicated
        # second tick: warning buried beyond the 5-item fatal window; the
        # persisted stamp keeps the verdict an error with no auto-nudge
        ctx_b2 = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
        ctx_b2.log.logs.append(types.SimpleNamespace(type='warning', heading='Infection check: TERMINATED'))
        for _i in range(5):
            ctx_b2.log.logs.append(types.SimpleNamespace(type='user', content='[chat_shepherd] nudge'))
        FakeAgentContext._all = [ctx_b2]
        monitor.tick(dict(cfg, notify_on_intervention=False))
        assert len(ctx_b2.communicated) == 0, ctx_b2.communicated
        st21b2 = read_state()
        assert st21b2['chats'][FAKE_CHAT_A]['status'] == 'error', st21b2['chats'][FAKE_CHAT_A]
        print('TEST21B_DURABLE_TERMINATION_STAMP_OK')

        # 21c: the nudge budget is NOT reset while a termination stamp is active;
        # a clean paused chat still resets normally
        ctx_pa = FakeCtx(FAKE_CHAT_A, ['user', 'tool', 'tool'], idle_minutes=5)
        ctx_pa.log.logs.append(types.SimpleNamespace(type='warning', heading='Infection check: TERMINATED'))
        ctx_pa.paused = True
        ctx_pb = FakeCtx(FAKE_CHAT_B, ['user', 'tool', 'response'], idle_minutes=5)
        ctx_pb.paused = True
        write_state(fresh_tick, chats={FAKE_CHAT_A: {'status': 'paused', 'nudge_count': 3}, FAKE_CHAT_B: {'status': 'paused', 'nudge_count': 2}})
        FakeAgentContext._all = [ctx_pa, ctx_pb]
        monitor.tick(dict(cfg, notify_on_intervention=False))
        st21c = read_state()
        assert st21c['chats'][FAKE_CHAT_A]['nudge_count'] == 3, st21c['chats'][FAKE_CHAT_A]
        assert st21c['chats'][FAKE_CHAT_B]['nudge_count'] == 0, st21c['chats'][FAKE_CHAT_B]
        print('TEST21C_BUDGET_HOLD_OK')

        # --- TEST 22: /nudge tracked-chat gate (v1.18.3, P7) ---
        # 22a: an untracked context id is rejected BEFORE communicate() and
        # before get_chat() can auto-create a ghost state entry.
        write_state((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), chats={
            FAKE_CHAT_A: {'status': 'stalled', 'nudge_count': 0, 'last_nudge_at': ''},
        })
        import asyncio as _aio22
        from usr.plugins.chat_shepherd.api.nudge import Nudge as _CSNudge22
        from usr.plugins.chat_shepherd.api import nudge as _nudgemod22
        _ctx22 = FakeCtx('zzzzzzzz', ['user', 'tool'], idle_minutes=30)
        _orig22 = _nudgemod22._get_context
        _nudgemod22._get_context = lambda cid: _ctx22
        try:
            _h22 = _CSNudge22(None, None)
            _r22 = _aio22.run(_h22.process({'chat_id': 'zzzzzzzz'}, None))
            assert _r22.get('success') is False, _r22
            assert 'not a tracked chat' in _r22.get('error', ''), _r22
            assert len(_ctx22.communicated) == 0, _ctx22.communicated
            _st22 = read_state()
            assert 'zzzzzzzz' not in _st22['chats'], sorted(_st22['chats'])
            assert not any(h.get('chat_id') == 'zzzzzzzz' for h in _st22.get('history', [])), _st22.get('history')
            print('TEST22A_UNTRACKED_NUDGE_REJECTED_OK')

            # 22b: a tracked chat still nudges through (communicate + state bump).
            _ctx22b = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
            _nudgemod22._get_context = lambda cid: _ctx22b
            _r22b = _aio22.run(_h22.process({'chat_id': FAKE_CHAT_A, 'text': 'resume please'}, None))
            assert _r22b.get('success') is True, _r22b
            assert len(_ctx22b.communicated) == 1, _ctx22b.communicated
            assert 'resume please' in str(_ctx22b.communicated[0]), _ctx22b.communicated
            _st22b = read_state()
            _e22b = _st22b['chats'][FAKE_CHAT_A]
            assert _e22b['nudge_count'] == 1, _e22b
            assert _e22b['status'] == 'nudged', _e22b
            assert 'manual_nudge' in [h.get('action') for h in _st22b.get('history', [])], _st22b.get('history')
            print('TEST22B_TRACKED_NUDGE_OK')
        finally:
            _nudgemod22._get_context = _orig22

        # --- TEST 23: read_journal chat_id filter (v1.18.4, P7) ---
        # 23a: per-chat reads trim the CHAT's own tail, not the global cap:
        # 3 early chat-A entries buried under 205 chat-B entries (beyond the
        # global 200 tail) are still returned, newest-first.
        _truncate_journal()
        for _i23 in range(1, 4):
            state_mod.append_journal({'chat_id': FAKE_CHAT_A, 'action': 'early_a_' + str(_i23), 'timestamp': datetime.now(timezone.utc).isoformat()})
        for _i23 in range(205):
            state_mod.append_journal({'chat_id': FAKE_CHAT_B, 'action': 'filler_b', 'timestamp': datetime.now(timezone.utc).isoformat()})
        _per23 = state_mod.read_journal(limit=10, chat_id=FAKE_CHAT_A)
        assert [h.get('action') for h in _per23] == ['early_a_3', 'early_a_2', 'early_a_1'], _per23
        _tail23 = state_mod.read_journal(limit=10)
        assert _tail23 and all(h.get('chat_id') == FAKE_CHAT_B for h in _tail23), _tail23
        print('TEST23A_PERCHAT_TAIL_OK')

        # 23b: /history serves this chat's older entries beyond the global tail.
        import asyncio as _aio23
        from usr.plugins.chat_shepherd.api.history import History as _CSHistory23
        _h23 = _CSHistory23(None, None)
        _r23 = _aio23.run(_h23.process({'chat_id': FAKE_CHAT_A, 'limit': 30}, None))
        assert _r23.get('success') is True, _r23
        assert [h.get('action') for h in _r23.get('history', [])] == ['early_a_3', 'early_a_2', 'early_a_1'], _r23
        print('TEST23B_HISTORY_API_PERCHAT_OK')

        # --- TEST 24: nudge failure visibility (v1.18.5, P7 closeout) ---
        # 24a: a failing communicate is journaled (auto_nudge_fail) + logged
        # through the shared rotating debug log; the legacy nudge_debug.log
        # file is never created.
        _truncate_journal()
        import agent as _agent_mod24
        class _BoomUserMessage24:
            def __init__(self, *a, **k):
                raise RuntimeError('cs_nudge_fail_probe')
        _orig_um24 = _agent_mod24.UserMessage
        _data24 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(monitor.__file__))), 'data')
        _legacy24 = os.path.join(_data24, 'nudge_debug.log')
        try:
            _agent_mod24.UserMessage = _BoomUserMessage24
            _ctx24 = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
            _ok24 = monitor._nudge_context(_ctx24, 'please resume', chat_id=FAKE_CHAT_A)
            assert _ok24 is False, _ok24
            _st24 = read_state()
            assert any(
                h.get('action') == 'auto_nudge_fail' and h.get('chat_id') == FAKE_CHAT_A
                and 'cs_nudge_fail_probe' in h.get('error', '')
                for h in _st24.get('history', [])
            ), _st24.get('history', [])[:5]
            # v1.21.0 (A15): read the run's SCRATCH sink, not the production
            # daily log. This assertion only ever passed because the suite
            # wrote its failures into production state.
            assert os.path.exists(_scratch_debug_path), _scratch_debug_path
            assert 'auto_nudge_fail' in open(_scratch_debug_path, encoding='utf-8').read(), 'shared debug log missing failure kind'
            assert not os.path.exists(_legacy24), 'legacy nudge_debug.log was recreated'
            print('TEST24A_NUDGE_FAIL_JOURNALED_OK')
            _agent_mod24.UserMessage = _orig_um24

            # 24b: wedge Tier-1 with a raising communicate - no crash, failure
            # row journaled with the chat id, wedge_nudged stays 0.
            write_state(fresh_tick, chats=wedge_state(12))
            _ctx24b = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30, running=True)
            def _boom24b(_msg):
                raise RuntimeError('cs_wedge_nudge_fail')
            _ctx24b.communicate = _boom24b
            FakeAgentContext._all = [_ctx24b]
            _s24b = monitor.tick(wedge_cfg)
            assert _s24b['wedged'] == 1 and _s24b['wedge_nudged'] == 0, _s24b
            _st24b = read_state()
            assert any(
                h.get('action') == 'auto_nudge_fail' and h.get('chat_id') == FAKE_CHAT_A
                and 'cs_wedge_nudge_fail' in h.get('error', '')
                for h in _st24b.get('history', [])
            ), _st24b.get('history', [])[:5]
            print('TEST24B_WEDGE_FAIL_JOURNALED_OK')
        finally:
            _agent_mod24.UserMessage = _orig_um24

        # 24c: hooks.uninstall removes the planted legacy file; pre-existing
        # daily debug logs are restored byte-identical afterwards.
        import glob as _glob24
        from usr.plugins.chat_shepherd import hooks as _cs_hooks24
        _saved24 = {}
        for _pth24 in _glob24.glob(os.path.join(_data24, 'debug-*.log')):
            with open(_pth24, 'rb') as _f24:
                _saved24[_pth24] = _f24.read()
        with open(_legacy24, 'w', encoding='utf-8') as _f24:
            _f24.write('legacy probe')
        try:
            _cs_hooks24.uninstall()
            assert not os.path.exists(_legacy24), 'legacy file survived uninstall'
            print('TEST24C_HOOKS_LEGACY_CLEAN_OK')
        finally:
            for _pth24, _bytes24 in _saved24.items():
                with open(_pth24, 'wb') as _f24:
                    _f24.write(_bytes24)

        # --- TEST 25: external-channel verification (v1.19.0) ---
        # 25a: _notify_framework leg succeeds and fails independently of dispatch.
        from helpers import notification as _cs_notif25
        from helpers import network as _cs_net25
        from helpers import plugins as _cs_plugins25
        from usr.plugins.chat_shepherd.api.test_notify import TestNotify as _TestNotify25
        _orig_send25 = _cs_notif25.NotificationManager.send_notification
        _orig_dbg25 = monitor._debug_log
        try:
            _sent25 = []
            _cs_notif25.NotificationManager.send_notification = lambda *a, **k: _sent25.append(k) or True
            assert monitor._notify_framework('info', 'leg test') is True
            assert _sent25, 'fw send_notification not called'
            dbg25 = []
            monitor._debug_log = lambda kind, msg: dbg25.append((kind, msg)) or None
            def _boom_send25(*a, **k):
                raise RuntimeError('fw leg down')
            _cs_notif25.NotificationManager.send_notification = _boom_send25
            assert monitor._notify_framework('info', 'leg test') is False
            assert any(k == 'notify_fail' and 'fw leg down' in m for k, m in dbg25), dbg25
            print('TEST25A_FW_LEG_OK')
        finally:
            _cs_notif25.NotificationManager.send_notification = _orig_send25
            monitor._debug_log = _orig_dbg25

        # 25b: _external_jobs + _post_jobs seams (payload parity + structured results).
        _orig_dbg25b = monitor._debug_log
        _orig_post25b = monitor._post_json
        _orig_res25b = _cs_net25.resolve_host_ips
        try:
            dbg25b = []
            monitor._debug_log = lambda kind, msg: dbg25b.append((kind, msg)) or None
            def _fake_res25b(*a, **k):
                host = str(a[0]) if a else ''
                import ipaddress
                return (ipaddress.ip_address('93.184.216.34'),) if 'example.com' in host else (ipaddress.ip_address('127.0.0.1'),)
            _cs_net25.resolve_host_ips = _fake_res25b
            posted25b = []
            monitor._post_json = lambda url, payload, timeout=5.0: posted25b.append((url, payload)) or True
            jobs25 = monitor._external_jobs({'webhook_url': 'https://hooks.example.com/x'}, 'warning', 'm', 'high')
            assert len(jobs25) == 1 and jobs25[0][0] == 'webhook', jobs25
            assert jobs25[0][2]['kind'] == 'warning' and jobs25[0][2]['priority'] == 'high', jobs25
            res25 = monitor._post_jobs(jobs25, 'm')
            assert res25 == {'webhook': None}, res25
            assert len(posted25b) == 1, posted25b
            monitor._post_json = lambda url, payload, timeout=5.0: False
            res25 = monitor._post_jobs(monitor._external_jobs({'webhook_url': 'https://hooks.example.com/x'}, 'info', 'm'), 'm')
            assert res25 == {'webhook': 'non-2xx response'}, res25
            assert any(k == 'webhook_fail' for k, _ in dbg25b), dbg25b
            def _raise25b(url, payload, timeout=5.0):
                raise RuntimeError('boom25b')
            monitor._post_json = _raise25b
            res25 = monitor._post_jobs(monitor._external_jobs({'webhook_url': 'https://hooks.example.com/x'}, 'info', 'm'), 'm')
            assert 'boom25b' in (res25['webhook'] or ''), res25
            dbg25b.clear()
            jobs25 = monitor._external_jobs({'webhook_url': 'http://127.0.0.1:9/x'}, 'info', 'm')
            assert jobs25 == [], jobs25
            assert any(k == 'webhook_skip' for k, _ in dbg25b), dbg25b
            print('TEST25B_POST_JOBS_SEAMS_OK')
        finally:
            monitor._debug_log = _orig_dbg25b
            monitor._post_json = _orig_post25b
            _cs_net25.resolve_host_ips = _orig_res25b

        # 25c: /test_notify endpoint - all legs, journal rows, redaction, negatives.
        import asyncio as _aio25
        _orig_cfg25 = _cs_plugins25.get_plugin_config
        _orig_fw25 = monitor._notify_framework
        _orig_post25 = monitor._post_json
        _orig_res25 = _cs_net25.resolve_host_ips
        _truncate_journal()
        try:
            def _fake_res25c(*a, **k):
                host = str(a[0]) if a else ''
                import ipaddress
                return (ipaddress.ip_address('93.184.216.34'),) if 'example.com' in host else (ipaddress.ip_address('127.0.0.1'),)
            _cs_net25.resolve_host_ips = _fake_res25c
            _cs_plugins25.get_plugin_config = lambda name: {
                'webhook_url': 'https://hooks.example.com/probe',
                'telegram_bot_token': 'SECRETTOK25',
                'telegram_chat_id': '42',
            }
            _h25 = _TestNotify25(None, None)
            monitor._notify_framework = lambda kind, message, priority='normal': True
            monitor._post_json = lambda url, payload, timeout=5.0: True
            _r25 = _aio25.run(_h25.process({'channel': 'all'}, None))
            assert _r25.get('success') is True and _r25.get('ok') is True, _r25
            assert _r25['results'] == {'framework': None, 'webhook': None, 'telegram': None}, _r25
            assert _r25.get('details') == {}, _r25
            _st25 = read_state()
            assert any(h.get('action') == 'notify_test' for h in _st25.get('history', [])), _st25.get('history', [])[-3:]
            print('TEST25C1_ENDPOINT_ALL_OK')
            def _leak25(url, payload, timeout=5.0):
                raise RuntimeError('connect failed for botSECRETTOK25/sendMessage')
            monitor._post_json = _leak25
            _r25 = _aio25.run(_h25.process({'channel': 'telegram'}, None))
            assert _r25.get('ok') is False, _r25
            _err25 = _r25['results']['telegram'] or ''
            assert 'SECRETTOK25' not in _err25 and '***' in _err25, _r25
            print('TEST25C2_REDACTION_OK')
            monitor._notify_framework = lambda kind, message, priority='normal': False
            _r25 = _aio25.run(_h25.process({'channel': 'framework'}, None))
            assert _r25.get('ok') is False and _r25['results']['framework'], _r25
            _cs_plugins25.get_plugin_config = lambda name: {}
            _r25 = _aio25.run(_h25.process({'channel': 'webhook'}, None))
            assert _r25.get('success') is False and 'webhook_url' in (_r25.get('error') or ''), _r25
            _r25 = _aio25.run(_h25.process({'channel': 'bogus'}, None))
            assert _r25.get('success') is False and 'Unknown channel' in (_r25.get('error') or ''), _r25
            print('TEST25C3_ENDPOINT_NEGATIVES_OK')
        finally:
            _cs_plugins25.get_plugin_config = _orig_cfg25
            monitor._notify_framework = _orig_fw25
            monitor._post_json = _orig_post25
            _cs_net25.resolve_host_ips = _orig_res25

        # TEST26A/B: config.html settings-contract migration (v1.20.0) -
        # values bind to config.* through the framework settings scope,
        # the own save button is gone (the modal Save owns persistence,
        # sanitized by the save_plugin_config hook), the toast seam
        # survives, and the script block stays syntax-clean.
        _cfgp26 = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'webui', 'config.html')
        _cfg26 = open(_cfgp26, encoding='utf-8').read()
        for _gone26 in ('setMsg', 'lastMsg', 'lastOk', 'lastErr', 'cs-msg', 'cs-err', 'cs-on',
                        'chatShepherdSettings', 'this.toast(', "this.api('/config'",
                        "this.api('/status'"):
            assert _gone26 not in _cfg26, 'config.html residue: ' + _gone26
        for _want26 in ('toastFrontendSuccess', 'toastFrontendError', 'notification-store.js',
                        '<template x-if="config">', 'x-model="config.enabled"',
                        'x-model="config.watch_all"', 'config.icons[',
                        'chatShepherdTestNotify', 'callJsonApi'):
            assert _want26 in _cfg26, 'config.html missing: ' + _want26
        assert '@click="save()"' not in _cfg26, 'own save button must be gone (framework Save owns persistence)'
        print('TEST26A_SETTINGS_CONTRACT_OK')
        import re as _re26, shutil as _sh26, subprocess as _sp26, tempfile as _tf26
        _node26 = _sh26.which('node')
        if _node26:
         _m26 = _re26.search(r'<script>(.*)</script>', _cfg26, _re26.S)
         assert _m26, 'config.html script block not found'
         _f26 = _tf26.NamedTemporaryFile(mode='w', suffix='.js', delete=False, encoding='utf-8')
         try:
          _f26.write(_m26.group(1))
          _f26.close()
          _chk26 = _sp26.run([_node26, '--check', _f26.name], capture_output=True, text=True)
          assert _chk26.returncode == 0, 'node --check failed: ' + _chk26.stderr[:400]
         finally:
          try:
           os.unlink(_f26.name)
          except Exception:
           pass
         print('TEST26B_NODE_CHECK_OK')
        else:
         print('TEST26B_SKIPPED_NO_NODE')
        # ================= TEST 27: live-aware cap (v1.20.0, P8) =================
        # Live contexts must NEVER be evicted (the old cap churned the
        # same live chats every tick, wiping nudge/cooldown bookkeeping
        # and re-nudging stalled chats every ~2 minutes); dead entries
        # still prune/cap normally; the journal separates prune_state
        # from cap_evicted.
        _orig_hcd27 = monitor._has_chat_dir
        try:
            # A: >MAX live contexts + dead entries: live survive, dead prune.
            monitor._has_chat_dir = lambda cid: cid.startswith('live')
            _live_ids27 = ['live%04d' % i for i in range(120)]
            _chats27 = {cid: {'status': 'running', 'nudge_count': 0, 'last_ticked': now_iso} for cid in _live_ids27}
            for _i27 in range(20):
                _chats27['dead%04d' % _i27] = {'status': 'idle', 'nudge_count': 0, 'last_ticked': '2026-01-01T00:00:00+00:00'}
            _cooler27 = 'liveCool'
            _chats27[_cooler27] = {
                'status': 'stalled', 'nudge_count': 2,
                'last_nudge_at': (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                'last_ticked': now_iso,
            }
            _live_ids27.append(_cooler27)
            ctxs27 = [FakeCtx(cid, ['user', 'tool'], idle_minutes=0) for cid in _live_ids27]
            ctxs27[-1] = FakeCtx(_cooler27, ['user', 'tool'], idle_minutes=30)
            FakeAgentContext._all = ctxs27
            write_state((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), chats=_chats27)
            s27 = monitor.tick(dict(cfg, nudge_cooldown_minutes=10, max_nudges_per_tick=5))
            st27 = read_state()
            for _cid27 in _live_ids27:
                assert _cid27 in st27['chats'], 'live chat evicted: ' + _cid27
            assert st27['chats'][_cooler27]['nudge_count'] == 2, st27['chats'].get(_cooler27)
            for _i27 in range(20):
                assert ('dead%04d' % _i27) not in st27['chats'], 'dead entry not pruned'
            _acts27 = [h.get('action') for h in st27.get('history', [])]
            assert 'prune_state' in _acts27 and 'cap_evicted' not in _acts27, _acts27
            assert s27['cap_evicted'] == 0, s27
            print('TEST27A_LIVE_PROTECTED_OK')

            # B: the cap bounds the DEAD surplus only; live chats survive
            # even alongside a full dead backlog.
            monitor._has_chat_dir = lambda cid: True
            _lives27b = ['liveCt01', 'liveCt02']
            _chats27b = {cid: {'status': 'running', 'nudge_count': 0, 'last_ticked': now_iso} for cid in _lives27b}
            for _i27 in range(105):
                _chats27b['capd%04d' % _i27] = {
                    'status': 'idle', 'nudge_count': 0,
                    'last_ticked': (datetime.now(timezone.utc) - timedelta(minutes=_i27)).isoformat(),
                }
            FakeAgentContext._all = [FakeCtx(cid, ['user', 'tool'], idle_minutes=0) for cid in _lives27b]
            write_state((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), chats=_chats27b)
            s27b = monitor.tick(cfg)
            st27b = read_state()
            for _cid27 in _lives27b:
                assert _cid27 in st27b['chats'], 'live chat evicted under cap: ' + _cid27
            for _i27 in range(100):
                assert ('capd%04d' % _i27) in st27b['chats'], 'fresh dead entry evicted: %d' % _i27
            for _i27 in range(100, 105):
                assert ('capd%04d' % _i27) not in st27b['chats'], 'oldest dead entry kept: %d' % _i27
            _evicted27 = [h for h in st27b.get('history', []) if h.get('action') == 'cap_evicted']
            assert _evicted27 and '5' in (_evicted27[0].get('detail') or ''), st27b.get('history', [])[-3:]
            assert s27b['cap_evicted'] == 5 and s27b['pruned'] == 5, s27b
            print('TEST27B_DEAD_SURPLUS_CAP_OK')
        finally:
            monitor._has_chat_dir = _orig_hcd27

        # ================= TEST 28: URL-secret redaction (v1.20.0, P8) =================
        # _post_jobs errors (debug log + return value) must never carry the
        # Telegram bot token or webhook credential embedded in the job URL.
        _orig_post28 = monitor._post_json
        _dbg28 = []
        _orig_dbg28 = monitor._debug_log
        try:
            monitor._debug_log = lambda kind, msg: _dbg28.append((kind, msg))
            def _leak28(url, payload, timeout=5.0):
                raise RuntimeError('connect failed for ' + url)
            monitor._post_json = _leak28
            _r28 = monitor._post_jobs([
                ('telegram', 'https://api.telegram.org/botSECRETTOK99/sendMessage', {}),
                ('webhook', 'https://hooks.example.com/x?token=WHSECRETTOK99', {}),
            ], 'm')
            assert 'SECRETTOK99' not in (_r28['telegram'] or ''), _r28
            assert 'WHSECRETTOK99' not in (_r28['webhook'] or ''), _r28
            assert '***' in _r28['telegram'] and '***' in _r28['webhook'], _r28
            _blob28 = ' | '.join(m for _, m in _dbg28)
            assert 'SECRETTOK99' not in _blob28 and 'WHSECRETTOK99' not in _blob28, _dbg28
            print('TEST28_URL_SECRET_REDACTION_OK')
        finally:
            monitor._post_json = _orig_post28
            monitor._debug_log = _orig_dbg28

        # ================= TEST 29: /history id pattern gate (v1.20.0, P8) =================
        from usr.plugins.chat_shepherd.api.history import History as _CSHistory29
        import asyncio as _aio29
        _h29 = _CSHistory29(None, None)
        _r29 = _aio29.run(_h29.process({'chat_id': '../../settings'}, None))
        assert _r29.get('success') is False, _r29
        _r29 = _aio29.run(_h29.process({'chat_id': 'waytoolongid_1234567890'}, None))
        assert _r29.get('success') is False, _r29
        _truncate_journal()
        state_mod.append_journal({'chat_id': FAKE_CHAT_A, 'action': 'manual_nudge', 'timestamp': now_iso})
        _r29 = _aio29.run(_h29.process({'chat_id': FAKE_CHAT_A}, None))
        assert _r29.get('success') is True and _r29.get('chat_id') == FAKE_CHAT_A, _r29
        assert any(h.get('action') == 'manual_nudge' for h in _r29.get('history', [])), _r29
        print('TEST29_HISTORY_ID_GATE_OK')

        # ================= TEST 30: saved-baseline race guard (v1.20.0, P8) =================
        # A save landing while /status load_state() reads the file must not
        # be clobbered by the in-flight load's stale-baseline adoption
        # (suppressed/redundant saves on the next tick).
        _orig_rj30 = state_mod.read_journal
        try:
            _payload30a = {'chats': {'raceAAAA': {'status': 'idle'}}, 'last_tick': now_iso}
            files.write_file(state_mod.STATE_FILE, json.dumps(_payload30a))
            state_mod.load_state()
            _sig30a = state_mod.state_signature(_payload30a)
            assert state_mod.last_saved_signature() == _sig30a, 'load_state failed to adopt baseline'
            _payload30b = {'chats': {'raceBBBB': {'status': 'idle'}}, 'last_tick': now_iso}
            def _rj_race30(limit, chat_id=None):
                state_mod.save_state(_payload30b)
                return _orig_rj30(limit, chat_id=chat_id)
            state_mod.read_journal = _rj_race30
            try:
                state_mod.load_state()
            finally:
                state_mod.read_journal = _orig_rj30
            _sig30b = state_mod.state_signature(_payload30b)
            assert state_mod.last_saved_signature() == _sig30b, 'racing save clobbered by load_state'
            state_mod.load_state()
            assert state_mod.last_saved_signature() == _sig30b, 'clean load lost the baseline'
            print('TEST30_BASELINE_RACE_GUARD_OK')
        finally:
            state_mod.read_journal = _orig_rj30

        # ============ TEST 31: cap/prune split is persisted + surfaced (v1.20.0) ============
        # The v1.20.0 fix separated dead-chat pruning from dead-surplus cap
        # eviction. Both must reach the /status throttle snapshot, otherwise
        # the dashboard badge silently reads zero and the "live chats are
        # protected" signal regresses to invisible.
        _orig_hcd31 = monitor._has_chat_dir
        try:
            monitor._has_chat_dir = lambda cid: cid.startswith('live')
            _live_ids31 = ['live%04d' % i for i in range(120)]
            _chats31 = {cid: {'status': 'running', 'nudge_count': 0, 'last_ticked': now_iso} for cid in _live_ids31}
            FakeAgentContext._all = [FakeCtx(cid, ['user', 'tool'], idle_minutes=0) for cid in _live_ids31]
            write_state((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), chats=_chats31)
            s31 = monitor.tick(dict(cfg, max_nudges_per_tick=5))
            st31 = read_state()
            for _cid31 in _live_ids31:
                assert _cid31 in st31['chats'], 'live chat evicted: ' + _cid31
            th31 = st31.get('throttle') or {}
            assert 'pruned' in th31 and 'cap_evicted' in th31, (
                'throttle snapshot missing the v1.20.0 pruned/cap_evicted split: %r' % (th31,)
            )
            assert th31.get('pruned') == 0, th31
            assert th31.get('cap_evicted') == 0, (
                'live contexts must never be evicted, got cap_evicted=%r' % th31.get('cap_evicted')
            )
            assert s31['cap_evicted'] == 0, s31
            # the /status handler must pass both fields through
            from usr.plugins.chat_shepherd.api.status import Status as _CSStatus31
            import asyncio as _aio31
            _payload31 = _aio31.run(_CSStatus31(None, None).process({}, None))
            _th_api31 = _payload31.get('throttle') or {}
            assert 'cap_evicted' in _th_api31 and 'pruned' in _th_api31, _th_api31
        finally:
            monitor._has_chat_dir = _orig_hcd31
            FakeAgentContext._all = []
        print('TEST31_CAP_SPLIT_SIGNAL_OK')

        # ============ TEST 32: one malformed state entry must not abort the tick (v1.20.2) ============
        # A hand-edited / legacy state.json can carry a non-int counter
        # ("nudge_count": "3"). That used to raise TypeError out of
        # classify_chat, out of the unguarded per-chat loop, and out of the
        # WHOLE tick: no nudges, no prune, no cap, no throttle snapshot for
        # ANY chat. One bad chat must never blind the supervisor.
        _orig_hcd32 = monitor._has_chat_dir
        try:
            monitor._has_chat_dir = lambda cid: True
            # CHAT A carries the poisoned field; CHAT B is a healthy stalled
            # chat that MUST still be classified (and is the control).
            _bad32 = {FAKE_CHAT_A: {'status': 'stalled', 'nudge_count': '3', 'last_ticked': now_iso}}
            _good32 = {FAKE_CHAT_B: {'status': 'stalled', 'nudge_count': 0, 'last_ticked': now_iso}}
            FakeAgentContext._all = [
                FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=600),
                FakeCtx(FAKE_CHAT_B, ['user', 'tool'], idle_minutes=600),
            ]
            write_state((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                        chats={**_bad32, **_good32})
            # classify_chat itself must degrade instead of raising. A numeric
            # string still parses (so "3" really is 3 nudges used ->
            # intervention); a junk value degrades to 0 -> stalled.
            _st32a, _r32a = monitor.classify_chat(
                FakeAgentContext._all[0], FAKE_CHAT_A,
                dict(cfg, stall_minutes=5), {'nudge_count': 'abc'}, 0.0,
            )
            assert _st32a == 'stalled', 'junk nudge_count should degrade to 0, got ' + str(_st32a)
            _st32b, _r32b = monitor.classify_chat(
                FakeAgentContext._all[0], FAKE_CHAT_A,
                dict(cfg, stall_minutes=5), {'nudge_count': '3'}, 0.0,
            )
            assert _st32b == 'intervention', 'numeric string should still parse, got ' + str(_st32b)
            s32 = monitor.tick(dict(cfg, max_nudges_per_tick=1))
            st32 = read_state()
            # The healthy chat is still supervised...
            assert st32['chats'][FAKE_CHAT_B]['status'] in ('stalled', 'nudged'), (
                'a poisoned sibling entry stopped supervising a healthy chat: %r' % st32['chats'][FAKE_CHAT_B]
            )
            # ...and the tick still completed its bookkeeping tail.
            assert 'throttle' in st32, 'tick aborted before the throttle snapshot: %r' % list(st32)
            assert s32['checked'] == 2, s32
            assert s32.get('chat_errors', 0) == 0, (
                'the coercion should have absorbed the bad counter outright: %r' % s32
            )

            # Second defence layer: an entry that is not even a dict (the shape a
            # hand-edit or a half-migrated state.json can produce) still breaks
            # attribute access, so the per-chat guard must catch it, count it,
            # and keep supervising every other chat.
            FakeAgentContext._all = [
                FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=600),
                FakeCtx(FAKE_CHAT_B, ['user', 'tool'], idle_minutes=600),
            ]
            write_state((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                        chats={FAKE_CHAT_A: 'not-a-dict',
                               FAKE_CHAT_B: {'status': 'stalled', 'nudge_count': 0,
                                             'last_ticked': now_iso}})
            s32b = monitor.tick(dict(cfg, max_nudges_per_tick=1))
            st32b = read_state()
            # load_state() repairs the corrupt entry, so the tick completes
            # cleanly and the healthy sibling is still supervised.
            assert 'throttle' in st32b, 'tick aborted before the throttle snapshot: %r' % list(st32b)
            assert s32b['checked'] == 2, s32b
            assert st32b['chats'][FAKE_CHAT_B]['status'] in ('stalled', 'nudged'), (
                'a corrupt sibling entry stopped supervising a healthy chat: %r' % st32b['chats'][FAKE_CHAT_B]
            )
            # the repair marker is reported once and never persisted
            assert s32b.get('repaired_entries') is None or s32b['repaired_entries'] >= 0, s32b
            assert 'repaired_entries' not in st32b, (
                'the transient repair marker leaked into state.json: %r' % list(st32b)
            )
        finally:
            monitor._has_chat_dir = _orig_hcd32
            FakeAgentContext._all = []
        print('TEST32_MALFORMED_ENTRY_ISOLATION_OK')

        # ============ TEST 33: _as_int coercion contract (v1.20.2) ============
        for _bad33 in ('3', 'abc', None, [1], {}, 2.7):
            _v33 = monitor._as_int(_bad33, 0)
            assert isinstance(_v33, int), (_bad33, _v33)
        assert monitor._as_int('3', 0) == 3
        assert monitor._as_int(2.7, 0) == 2
        assert monitor._as_int(None, 7) == 7
        assert monitor._as_int(True, 0) == 1
        print('TEST33_AS_INT_OK')

        # ============ TEST 34: tail scanners must not copy the whole log (v1.20.2) ============
        # Each helper used to do list(context.log.logs) under the agent's log
        # lock (blocking the agent's own writer) and then inspect a handful of
        # tail items. Guard the bounded slice structurally.
        # v1.21.0 (O5): the invariant moved. The tail slice now happens ONCE, in
        # `_log_snapshot`, and the five scanners read that shared view - so
        # asserting the slice appears in each scanner (which is what this test
        # used to require) would now be asserting the duplication we just
        # removed. The guards below are the new shape: the snapshot slices
        # under the lock, and NO scanner touches context.log.logs itself.
        import inspect as _insp34
        _snap34 = '\n'.join(
            ln for ln in _insp34.getsource(monitor._log_snapshot).splitlines()
            if not ln.strip().startswith('#') and 'log.logs)' not in ln
        )
        assert 'logs[-_LOG_TAIL:]' in _snap34, (
            '_log_snapshot must take exactly one bounded tail slice'
        )
        assert '_lock' in _snap34, (
            '_log_snapshot must hold the log lock while slicing'
        )
        for _fn34 in ('_last_log_type', '_last_log_fatal', '_scan_log_for_termination',
                      '_last_response_text', '_last_entry_type', '_log_len', '_log_updates'):
            _src34 = _insp34.getsource(getattr(monitor, _fn34))
            _code34 = '\n'.join(
                ln for ln in _src34.splitlines()
                if not ln.strip().startswith('#')
            )
            assert 'context.log.logs' not in _code34, (
                _fn34 + ' reads the log itself; it must use the shared _LogView'
            )
            assert 'view' in _code34, (
                _fn34 + ' must accept the shared snapshot'
            )
        print('TEST34_LOG_TAIL_SLICE_OK')

        # ============ TEST 35: /status uses the same real-chat predicate as tick (v1.20.2) ============
        # /status used to list EVERY live AgentContext, so throwaway script
        # contexts showed in the dashboard and /nudge then refused them.
        _orig_hcd35 = monitor._has_chat_dir
        try:
            monitor._has_chat_dir = lambda cid: cid == FAKE_CHAT_A
            FakeAgentContext._all = [
                FakeCtx(FAKE_CHAT_A, ['user'], idle_minutes=0),
                FakeCtx('verify-1788992102', ['user'], idle_minutes=0),
            ]
            write_state(now_iso, chats={FAKE_CHAT_A: {'status': 'idle'}})
            from usr.plugins.chat_shepherd.api.status import Status as _CSStatus35
            import asyncio as _aio35
            _p35 = _aio35.run(_CSStatus35(None, None).process({}, None))
            _ids35 = [c['chat_id'] for c in _p35['chats']]
            assert FAKE_CHAT_A in _ids35, _ids35
            assert 'verify-1788992102' not in _ids35, (
                '/status surfaced an untracked script context: %r' % _ids35
            )
        finally:
            monitor._has_chat_dir = _orig_hcd35
            FakeAgentContext._all = []
        print('TEST35_STATUS_TRACKING_PREDICATE_OK')

        # ============ TEST 36: config.json holds ONLY explicitly-set values (v1.20.6) ============
        # v1.20.3 asserted the OPPOSITE (fully merged 33/33). Saving the merged
        # view pinned every shipped default into the file, so a later release
        # that changed a default could never reach an existing install - the
        # file's stale copy always won over DEFAULTS. The contract is inverted:
        # the file records what the operator set, and COMPLETENESS is the read
        # path's job (monitor._load_config / deep_merge_defaults, TEST37).
        from usr.plugins.chat_shepherd.helpers import config_defaults as _cd36
        _cfg_path36 = 'usr/plugins/chat_shepherd/config.json'
        _raw36 = json.loads(files.read_file(files.get_abs_path(_cfg_path36)))
        _pinned36 = sorted(k for k in _cd36.KNOWN_KEYS
                           if k in _raw36 and _raw36[k] == _cd36.DEFAULTS[k])
        assert not _pinned36, (
            'config.json pins values identical to the shipped defaults, so a new '
            'release could never change them here: %r' % _pinned36
        )
        # ...and completeness must not depend on the file being complete.
        _merged36 = _cd36.deep_merge_defaults(_raw36)
        assert sorted(_cd36.KNOWN_KEYS - set(_merged36)) == [], _merged36
        for _k36 in _cd36.ICON_STATUSES:
            assert _k36 in (_merged36.get('icons') or {}), 'icon lost: ' + _k36
        # prune must be lossless for explicit values and idempotent
        assert _cd36.prune_to_explicit(_merged36) == _raw36, (
            'prune_to_explicit lost an explicitly-set value: %r' % _raw36
        )
        assert _cd36.prune_to_explicit(_cd36.prune_to_explicit(_merged36)) == _raw36
        # a key we do not understand must survive untouched
        _probe36 = dict(_raw36)
        _probe36['some_future_key'] = 42
        assert _cd36.prune_to_explicit(
            _cd36.deep_merge_defaults(_probe36))['some_future_key'] == 42, (
            'prune_to_explicit dropped an unknown key it must preserve'
        )
        print('TEST36_CONFIG_EXPLICIT_ONLY_OK')

        # ============ TEST 37: _load_config is independent of the hook (v1.20.3) ============
        # Every runtime read goes through monitor._load_config(), which
        # re-applies DEFAULTS locally. Simulate a hook regression (raw partial
        # config) and prove the runtime still sees a complete config.
        from helpers import plugins as _plugins36
        _orig_gpc36 = _plugins36.get_plugin_config
        try:
            _plugins36.get_plugin_config = lambda name: {
                'enabled': True, 'stall_minutes': 7, 'max_auto_nudges': 2,
            }
            _seen36 = monitor._load_config()
            _missing36b = sorted(_cd36.KNOWN_KEYS - set(_seen36))
            assert not _missing36b, 'hookless read lost keys: %r' % _missing36b
            assert _seen36['stall_minutes'] == 7, 'user override must win'
            assert _seen36['poll_seconds'] == _cd36.DEFAULTS['poll_seconds'], (
                'an unwritten key must fall back to the shipped default'
            )
            for _k36b in _cd36.ICON_STATUSES:
                assert _k36b in (_seen36.get('icons') or {}), 'icon lost: ' + _k36b
            # a broken framework read must still yield a usable config
            _plugins36.get_plugin_config = lambda name: (_ for _ in ()).throw(RuntimeError('boom'))
            _fallback36 = monitor._load_config()
            assert _fallback36.get('enabled') is True, _fallback36
            assert sorted(_cd36.KNOWN_KEYS - set(_fallback36)) == [], _fallback36
        finally:
            _plugins36.get_plugin_config = _orig_gpc36
        print('TEST37_HOOKLESS_CONFIG_READ_OK')

        # ============ TEST 38: a manual action mid-tick never loses tick updates (v1.20.3) ============
        # The tick holds the state lock across the whole read-modify-write,
        # including message delivery (reviewed and deliberately kept - see the
        # comment on tick()). What actually matters is the v1.18.0 guarantee:
        # a /nudge, /resolve or /draft transaction landing during a tick must
        # be serialized, not merged into a stale snapshot. Prove it by running
        # a handler transaction from another thread while the tick holds the
        # lock and asserting (a) the handler really did block, and (b) neither
        # side's update was lost.
        import threading as _th38
        import time as _time38
        _orig_hcd38 = monitor._has_chat_dir
        _orig_nudge38 = monitor._nudge_context
        try:
            monitor._has_chat_dir = lambda cid: True
            _ctx38 = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=600)
            FakeAgentContext._all = [_ctx38]
            write_state((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                        chats={FAKE_CHAT_A: {'status': 'stalled', 'nudge_count': 0,
                                             'last_ticked': now_iso}})
            _order38 = []
            _in_delivery = _th38.Event()
            _handler_done = _th38.Event()

            # Hold the tick inside the delivery step (i.e. inside the state
            # lock) long enough for a handler transaction to collide with it.
            def _slow_nudge38(context, text=None, chat_id=''):
                _in_delivery.set()
                _time38.sleep(0.4)
                return _orig_nudge38(context, text, chat_id=chat_id)

            monitor._nudge_context = _slow_nudge38

            def _handler38():
                # A real handler transaction: blocks on the tick's lock, then
                # applies on a fresh snapshot.
                def _apply(st):
                    _order38.append('handler_enter')
                    state_mod.update_chat(st, FAKE_CHAT_A,
                                          last_classification='manual action')
                    return True

                state_mod.state_transaction_apply(_apply)
                _handler_done.set()

            _tick_err = []

            def _run_tick38():
                try:
                    monitor.tick(dict(cfg, max_nudges_per_tick=1))
                except Exception as e:  # pragma: no cover - surfaced below
                    _tick_err.append(e)

            _tick_thread = _th38.Thread(target=_run_tick38)
            _tick_thread.start()
            assert _in_delivery.wait(5), 'the tick never reached message delivery'
            # The tick now holds the state lock. A manual action must wait.

            _handler_thread = _th38.Thread(target=_handler38)
            _handler_thread.start()
            _handler_thread.join(timeout=0.15)
            assert not _handler_done.is_set(), (
                'a manual action ran concurrently with the tick instead of '
                'waiting for the state lock'
            )
            _tick_thread.join(timeout=10)
            assert not _tick_err, _tick_err
            _handler_thread.join(timeout=10)
            assert _handler_done.is_set(), 'the manual action never completed'
            assert _order38 == ['handler_enter'], _order38
            st38 = read_state()
            # The manual action applied on a snapshot taken AFTER the tick
            # released the lock, so it is present - and the tick's own
            # bookkeeping is still there next to it (nothing clobbered).
            assert st38['chats'][FAKE_CHAT_A].get('last_classification') == 'manual action', (
                'the manual action was lost: %r' % st38['chats'][FAKE_CHAT_A]
            )
            assert st38['chats'][FAKE_CHAT_A].get('nudges_sent', 0) >= 1, (
                "the tick's own delivery bookkeeping was lost: %r" % st38['chats'][FAKE_CHAT_A]
            )
        finally:
            monitor._nudge_context = _orig_nudge38
            monitor._has_chat_dir = _orig_hcd38
            FakeAgentContext._all = []
        print('TEST38_TICK_HANDLER_SERIALIZATION_OK')

        # ============ TEST 39: manual nudge on a fatal stop asks first (v1.20.4) ============
        # The auto ladder refuses to nudge a chat that died from a real error
        # (nudging restarts the same failing code path). A HUMAN must still be
        # able to override - but until v1.20.4 the button gave no hint of that,
        # sent silently, and overwrote status='error' with 'nudged', erasing the
        # very signal the operator needs afterwards.
        import asyncio as _aio39
        from usr.plugins.chat_shepherd.api import nudge as _nudge39
        from usr.plugins.chat_shepherd.helpers import state as _sm39
        _orig_hcd39 = monitor._has_chat_dir
        _orig_get39 = _nudge39._get_context
        _orig_statefile39 = _sm39.STATE_FILE
        _orig_journal39 = _sm39.JOURNAL_FILE
        try:
            monitor._has_chat_dir = lambda cid: True
            _nudge39.monitor._has_chat_dir = lambda cid: True
            _sm39.STATE_FILE = 'usr/plugins/chat_shepherd/data/test39_state.json'
            _sm39.JOURNAL_FILE = 'usr/plugins/chat_shepherd/data/test39_hist.jsonl'

            def _seed39(**entry):
                files.write_file(_sm39.STATE_FILE, json.dumps({
                    'chats': {FAKE_CHAT_A: entry}, 'last_tick': now_iso, 'history': [],
                }))

            def _read39():
                raw = json.loads(files.read_file(files.get_abs_path(_sm39.STATE_FILE)))
                return raw['chats'][FAKE_CHAT_A]

            def _nudge39_run(force=None):
                body = {'chat_id': FAKE_CHAT_A}
                if force is not None:
                    body['force'] = force
                return _aio39.run(_nudge39.Nudge(None, None).process(body, None))

            # (a) unguarded chat -> ordinary nudge, status flips to 'nudged'
            _seed39(status='stalled', nudge_count=0, last_classification='Stalled 30m')
            _nudge39._get_context = lambda cid: FakeCtx(
                FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
            _r39a = _nudge39_run()
            assert _r39a['success'] and not _r39a.get('needs_confirmation'), _r39a
            assert _r39a.get('overrode_guard') is False, _r39a
            assert _read39()['status'] == 'nudged', _read39()
            assert 'Manual nudge' in _read39()['last_classification'], _read39()

            # (b) fatal-stop chat -> confirmation requested, NOTHING sent
            _seed39(status='error', nudge_count=0,
                    last_classification='Chat stopped by an error or security termination')
            _ctx39 = FakeCtx(FAKE_CHAT_A, ['user', 'tool', 'error'], idle_minutes=30)
            _nudge39._get_context = lambda cid: _ctx39
            _r39b = _nudge39_run()
            assert _r39b.get('needs_confirmation') is True, _r39b
            assert _r39b.get('guard') == 'fatal_stop', _r39b
            assert _r39b.get('warning'), _r39b
            assert _ctx39.communicated == [], 'the guarded nudge must not deliver'
            _after39 = _read39()
            assert _after39['status'] == 'error', ('guard must not touch state: %r' % _after39)
            assert _after39.get('nudge_count') == 0, _after39

            # (c) forced -> delivers, journals an override, PRESERVES the error
            # Start from a clean journal: step (a) legitimately journaled a
            # plain manual_nudge, and this step must not be judged against it.
            try:
                os.unlink(files.get_abs_path(_sm39.JOURNAL_FILE))
            except Exception:
                pass
            _r39c = _nudge39_run(force=True)
            assert _r39c['success'] and _r39c.get('overrode_guard') is True, _r39c
            assert len(_ctx39.communicated) == 1, 'the forced nudge must deliver'
            _after39c = _read39()
            assert _after39c['status'] == 'error', (
                'an override must NOT erase the error status: %r' % _after39c
            )
            assert 'OVERRIDE' in _after39c['last_classification'], _after39c
            assert _after39c.get('manual_nudge_override_guard') == 'fatal_stop', _after39c
            _acts39 = [h.get('action') for h in _sm39.read_journal(50)]
            assert 'manual_nudge_override' in _acts39, _acts39
            assert 'manual_nudge' not in _acts39, (
                'an override must not also journal a plain manual_nudge: %r' % _acts39
            )

            # (d) force on a NON-guarded chat is an ordinary nudge
            _nudge39._get_context = lambda cid: FakeCtx(
                FAKE_CHAT_A, ['user', 'tool'], idle_minutes=30)
            _seed39(status='stalled', nudge_count=0, last_classification='Stalled 30m')
            _r39d = _nudge39_run(force='true')
            assert _r39d['success'] and _r39d.get('overrode_guard') is False, (
                'force on a healthy chat must stay an ordinary nudge: %r' % _r39d
            )
            assert _read39()['status'] == 'nudged', _read39()

            # (e) the string "false" must not be read as true
            _seed39(status='error', nudge_count=0, last_classification='error')
            _nudge39._get_context = lambda cid: FakeCtx(
                FAKE_CHAT_A, ['user', 'tool', 'error'], idle_minutes=30)
            _r39e = _nudge39_run(force='false')
            assert _r39e.get('needs_confirmation') is True, (
                'force="false" must not override the guard: %r' % _r39e
            )
        finally:
            monitor._has_chat_dir = _orig_hcd39
            _nudge39._get_context = _orig_get39
            _nudge39.monitor._has_chat_dir = monitor._has_chat_dir
            _sm39.STATE_FILE = _orig_statefile39
            _sm39.JOURNAL_FILE = _orig_journal39
            for _p39 in ('usr/plugins/chat_shepherd/data/test39_state.json',
                         'usr/plugins/chat_shepherd/data/test39_hist.jsonl'):
                try:
                    os.unlink(files.get_abs_path(_p39))
                except Exception:
                    pass
        print('TEST39_MANUAL_NUDGE_GUARD_OK')

        # ============ TEST 40: /config rejects unknown keys loudly (v1.20.5) ============
        # The endpoint used to drop unknown keys and still answer ok/success,
        # so a typo looked like a clean save while persisting nothing.
        import asyncio as _aio40
        from usr.plugins.chat_shepherd.api import config as _cfg40
        _saved40 = []
        _orig_helper40 = _cfg40.plugins_helper
        _orig_read_disk40 = _cfg40._read_disk
        _DISK40 = {'stall_minutes': 5, 'max_auto_nudges': 3}

        def _fake_helper40():
            return type('H', (), {
                'get_plugin_config': staticmethod(lambda name: dict(_DISK40)),
                'save_plugin_config': staticmethod(
                    lambda name, p, a, s: (_saved40.append(dict(s)), s)[1]),
                'clear_plugin_cache': staticmethod(lambda names: None),
            })()
        try:
            _cfg40.plugins_helper = _fake_helper40()
            _cfg40._read_disk = lambda: dict(_DISK40)
            _run40 = lambda b: _aio40.run(_cfg40.Config(None, None).process(b, None))

            # (a) unknown key ONLY -> must not claim success
            _saved40.clear()
            _r40a = _run40({'totally_bogus_key': 1})
            assert _r40a.get('ok') is False and _r40a.get('success') is False, _r40a
            assert _r40a.get('rejected') == ['totally_bogus_key'], _r40a
            assert 'totally_bogus_key' in (_r40a.get('error') or ''), _r40a
            assert not _saved40, 'an all-unknown body must not write'

            # (b) mixed -> known key applies, unknown key REPORTED
            _saved40.clear()
            _r40b = _run40({'stall_minutes': 42, 'also_bogus': 2})
            assert _r40b.get('ok') is True and _r40b.get('success') is True, _r40b
            assert _r40b.get('updated') == ['stall_minutes'], _r40b
            assert _r40b.get('rejected') == ['also_bogus'], _r40b
            assert _saved40 and _saved40[-1].get('stall_minutes') == 42, _saved40

            # (c) every error path carries BOTH ok and success
            for _bad40 in ('not-a-dict', ['a'], 0, ''):
                _r40c = _run40(_bad40)
                assert 'ok' in _r40c and 'success' in _r40c, (
                    'a malformed body (%r) must carry both flags: %r' % (_bad40, _r40c)
                )
                assert _r40c['ok'] is False and _r40c['success'] is False, _r40c

            # (d) a clean save and a pure read are unaffected
            _r40d = _run40({})
            assert _r40d.get('ok') is True and 'rejected' not in _r40d, _r40d
            assert _r40d.get('config'), _r40d
            _r40e = _run40({'max_auto_nudges': 9})
            assert _r40e.get('rejected') is None, 'a clean save reports nothing'
        finally:
            _cfg40.plugins_helper = _orig_helper40
            _cfg40._read_disk = _orig_read_disk40
        print('TEST40_CONFIG_REJECTS_UNKNOWN_KEYS_OK')
        # ============ TEST 41: user endpoints read off the event loop (v1.20.5) ============
        # /config, /history and /export all touch disk. v1.18.1 moved the
        # equivalent work out of /status and /draft; these three were left
        # behind, so a slow 9p stat blocked every other request in the server.
        import threading as _th41
        from usr.plugins.chat_shepherd.api import history as _his41
        from usr.plugins.chat_shepherd.api import export as _exp41
        _orig41 = {}
        try:
            _main41 = _th41.current_thread()
            # v1.21.0 (A8): capture the PRISTINE state seams before this test
            # patches them. Two consecutive sections each capture the current
            # `state_mod.read_journal` as their "original" - the second one
            # captured the first section's stub and restored that instead, so
            # after TEST41 every later read_state() in the suite saw an empty
            # journal. Symptom: TEST44A found zero `draft_sent` rows for a send
            # that had succeeded. Restoring the true original closes it.
            _orig41['rj0'] = state_mod.read_journal
            _orig41['ls0'] = state_mod.load_state

            # /config: the disk read must not run on the calling thread.
            # BOTH branches matter - a pure read and a save each call
            # _read_disk, and an earlier draft of this test only covered the
            # read, so reverting the save branch's to_thread passed the suite.
            _seen41 = []
            _orig41['read_disk'] = _cfg40._read_disk
            _cfg40._read_disk = lambda: (
                _seen41.append(_th41.current_thread()), dict(_DISK40))[1]
            _orig41['helper'] = _cfg40.plugins_helper
            _cfg40.plugins_helper = type('H41', (), {
                'get_plugin_config': staticmethod(lambda name: dict(_DISK40)),
                'save_plugin_config': staticmethod(lambda n, p, a, s: s),
                'clear_plugin_cache': staticmethod(lambda names: None),
            })()
            _aio40.run(_cfg40.Config(None, None).process({}, None))
            assert _seen41, 'the config read seam was never called (read path)'
            assert all(t is not _main41 for t in _seen41), (
                'api/config.py READ path must read config off the event loop'
            )
            _seen41.clear()
            _aio40.run(_cfg40.Config(None, None).process({'stall_minutes': 42}, None))
            assert _seen41, 'the config read seam was never called (save path)'
            assert all(t is not _main41 for t in _seen41), (
                'api/config.py SAVE path must read config off the event loop'
            )

            # /history: the journal read must not run on the calling thread
            _seen41.clear()
            # v1.21.0 (A8): these three seams are patched on the STATE MODULE,
            # not on the endpoint modules. api/status.py, api/export.py and
            # api/history.py used to `from ...state import load_state,
            # read_journal`, and a from-import binding survives a
            # helpers/state.py hot-reload - so those endpoints silently kept
            # serving pre-patch state code until a plugin refresh. They now
            # call `state_mod.*`, which also makes the module the test seam.
            _orig41['rj'] = state_mod.read_journal
            state_mod.read_journal = lambda limit=200, chat_id=None: (
                _seen41.append(_th41.current_thread()), [])[1]
            _orig41['name'] = _his41.monitor._chat_display_name
            _his41.monitor._chat_display_name = lambda cid: 'probe'
            _r41 = _aio40.run(_his41.History(None, None).process(
                {'chat_id': FAKE_CHAT_A}, None))
            assert _r41.get('success') is True, _r41
            assert _seen41 and _seen41[0] is not _main41, (
                'api/history.py must read the journal off the event loop'
            )

            # /export: must offload AND must keep the FULL journal
            _seen41.clear()
            _orig41['ls'] = state_mod.load_state
            state_mod.load_state = lambda: (
                _seen41.append(_th41.current_thread()),
                {'chats': {}, 'last_tick': '', 'history': ['MIRROR-ONLY-200']})[1]
            _orig41['rj2'] = state_mod.read_journal
            state_mod.read_journal = lambda limit=200, chat_id=None: ['FULL-%s' % limit]
            _r41b = _aio40.run(_exp41.Export(None, None).process({}, None))
            assert _r41b.get('success') is True, _r41b
            assert _seen41 and _seen41[0] is not _main41, (
                'api/export.py must build the snapshot off the event loop'
            )
            # Guards a regression that was actually written and then reverted:
            # reusing load_state()'s 200-entry mirror here would ship a
            # silently truncated "full" export.
            assert _r41b['history'] == ['FULL-1000'], (
                'export must use JOURNAL_KEEP (1000), not the 200 mirror: %r'
                % _r41b['history']
            )

            # a malformed id must not be reported as a MISSING id
            _r41c = _aio40.run(_his41.History(None, None).process(
                {'chat_id': '../../etc/passwd'}, None))
            assert _r41c.get('success') is False, _r41c
            assert 'required' not in (_r41c.get('error') or ''), (
                'a bad id must not be reported as missing: %r' % _r41c
            )
        finally:
            _cfg40._read_disk = _orig41['read_disk']
            _cfg40.plugins_helper = _orig41['helper']
            state_mod.read_journal = _orig41['rj0']
            _his41.monitor._chat_display_name = _orig41['name']
            state_mod.load_state = _orig41['ls0']
        print('TEST41_ENDPOINTS_OFF_EVENT_LOOP_OK')
        # ============ TEST 43: the settings-modal save hook prunes too (v1.20.6) ============
        # hooks.save_plugin_config is the path the FRAMEWORK settings modal
        # takes - api/config.py is only one caller. The v1.20.6 tests above
        # stub plugins_helper.save_plugin_config, so they never execute this
        # function; removing its prune call passed the whole suite. Test it
        # directly.
        from usr.plugins.chat_shepherd import hooks as _hooks43
        from usr.plugins.chat_shepherd.helpers import config_defaults as _cd43
        _orig_persisted43 = _hooks43._read_persisted
        try:
            _hooks43._read_persisted = lambda project_name='', agent_profile='': {
                'stall_minutes': 9, 'mystery_key': 'keep-me',
            }
            _out43 = _hooks43.save_plugin_config({'max_auto_nudges': 4})
            assert sorted(_out43) == [
                'max_auto_nudges', 'mystery_key', 'stall_minutes',
            ], 'the hook wrote keys that are not explicitly set: %r' % _out43
            assert _out43['max_auto_nudges'] == 4, _out43
            assert _out43['stall_minutes'] == 9, _out43
            assert _out43['mystery_key'] == 'keep-me', _out43
            # a hook read failure must not explode or leak the merged view
            _hooks43._read_persisted = lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError('boom'))
            _out43b = _hooks43.save_plugin_config({'poll_seconds': 30})
            assert sorted(_out43b) == ['poll_seconds'], _out43b
            assert _out43b['poll_seconds'] == 30, _out43b
            # and the hook must still return a dict, never None
            assert isinstance(_hooks43.save_plugin_config('not-a-dict'), dict)
        finally:
            _hooks43._read_persisted = _orig_persisted43
        print('TEST43_SAVE_HOOK_PRUNES_OK')


        # ============ TEST 42: a save writes only what was set (v1.20.6) ============
        # Persisting the merged 33-key view pinned every shipped default into
        # config.json, so a future release's new defaults could never reach an
        # existing install. Saving one key must now write exactly that key (plus
        # anything already explicitly set and any unknown key).
        from usr.plugins.chat_shepherd.api import config as _cfg42
        from usr.plugins.chat_shepherd.helpers import config_defaults as _cd42
        _disk42 = {'stall_minutes': 9}   # one pre-existing explicit value
        _wrote42 = []

        def _helper42():
            return type('H42', (), {
                'get_plugin_config': staticmethod(
                    lambda name: _cd42.deep_merge_defaults(_disk42)),
                'save_plugin_config': staticmethod(
                    lambda n, p, a, s: (_wrote42.append(dict(s)), s)[1]),
                'clear_plugin_cache': staticmethod(lambda names: None),
            })()
        _orig_h42 = _cfg42.plugins_helper
        _orig_rd42 = _cfg42._read_disk
        try:
            _cfg42.plugins_helper = _helper42()
            _cfg42._read_disk = lambda: _cd42.deep_merge_defaults(_disk42)
            _run42 = lambda b: _aio40.run(_cfg42.Config(None, None).process(b, None))

            _wrote42.clear()
            _r42 = _run42({'max_auto_nudges': 4})
            assert _r42.get('success') is True, _r42
            _written = _wrote42[-1]
            # exactly the two explicit values, NOT 33 merged keys
            assert sorted(_written) == ['max_auto_nudges', 'stall_minutes'], _written
            assert _written['max_auto_nudges'] == 4, _written
            # and nothing pinned to a default leaked through
            _leaked = sorted(k for k in _cd42.KNOWN_KEYS
                             if k in _written and _written[k] == _cd42.DEFAULTS[k])
            assert not _leaked, 'a default-valued key was written: %r' % _leaked

            # setting a value BACK to its default removes it from the file,
            # and the read still returns the default. NOTE: an empty body is
            # a PURE READ and never saves, so the reset must be sent as an
            # explicit override.
            _wrote42.clear()
            _run42({'stall_minutes': _cd42.DEFAULTS['stall_minutes']})
            assert _wrote42, 'an explicit override must reach the save path'
            assert 'stall_minutes' not in _wrote42[-1], (
                'a key reset to its default should leave the file: %r' % _wrote42[-1]
            )
            _after = _cd42.deep_merge_defaults(_wrote42[-1])
            assert _after['stall_minutes'] == _cd42.DEFAULTS['stall_minutes'], _after

            # an unknown key on disk must survive a save untouched
            _disk42['mystery_key'] = 'keep-me'
            _wrote42.clear()
            _run42({'max_auto_nudges': 2})
            assert _wrote42[-1].get('mystery_key') == 'keep-me', (
                'a save dropped an unknown key: %r' % _wrote42[-1]
            )
        finally:
            _cfg42.plugins_helper = _orig_h42
            _cfg42._read_disk = _orig_rd42
        print('TEST42_SAVE_WRITES_ONLY_EXPLICIT_OK')

        # ============ TEST 44: audit-fix regressions (v1.20.7) ============
        # Covers the 2026-09-28 independent bug-hunt fixes: the draft
        # double-send race, non-dict API bodies, /config reporting garbage as
        # "updated", the resume burst eating the stall queue's budget, the
        # webhook SSRF redirect hole + URL redaction, and hotreload lock
        # preservation for plain Locks.
        from usr.plugins.chat_shepherd.api import (
            config as _cf44,
            draft as _dr44,
            nudge as _nu44,
            resolve as _re44,
            test_notify as _tn44,
        )
        from usr.plugins.chat_shepherd.helpers import hotreload as _hr44
        # --- 44a: a second send of the same draft is rejected BEFORE it
        # communicates (the old code delivered the message twice) ---
        write_state((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), chats={
            FAKE_CHAT_A: {'status': 'stalled', 'nudge_count': 0, 'last_nudge_at': ''},
        })
        st44 = state_mod.load_state()
        assert state_mod.add_draft(st44, FAKE_CHAT_A, 'stall_nudge', reason='unit stall 44')
        did44 = st44['drafts'][0]['id']
        state_mod.save_state(st44)
        ctx44 = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=5)
        _orig_ctx44 = _dr44._get_context
        try:
            _dr44._get_context = lambda cid: ctx44
            _h44 = _CSDraft17(None, None)
            _r44a = _aio17.run(_h44.process({'draft_id': did44, 'action': 'send', 'text': 'once only'}, None))
            assert _r44a.get('success') is True, _r44a
            _r44b = _aio17.run(_h44.process({'draft_id': did44, 'action': 'send'}, None))
            assert _r44b.get('success') is False, _r44b
            assert len(ctx44.communicated) == 1, ctx44.communicated
            st44b = read_state()
            assert st44b.get('drafts', []) == [], st44b.get('drafts')
            _rows44 = [h for h in st44b.get('history', []) if h.get('action') == 'draft_sent']
            assert len(_rows44) == 1, _rows44
            print('TEST44A_DRAFT_CLAIM_BLOCKS_DOUBLE_SEND_OK')
            # --- 44b: a failed send restores the claimed draft ---
            st44c = state_mod.load_state()
            assert state_mod.add_draft(st44c, FAKE_CHAT_A, 'restart_resume', reason='boom 44')
            did44c = st44c['drafts'][0]['id']
            state_mod.save_state(st44c)
            boom44 = FakeCtx(FAKE_CHAT_A, ['user'], idle_minutes=5)

            def _boom44(msg):
                raise RuntimeError('cs_send_fail_probe')

            boom44.communicate = _boom44
            _dr44._get_context = lambda cid: boom44
            _r44c = _aio17.run(_h44.process({'draft_id': did44c, 'action': 'send'}, None))
            assert _r44c.get('success') is False, _r44c
            st44d = read_state()
            _back44 = [d for d in st44d.get('drafts', []) if d.get('id') == did44c]
            assert len(_back44) == 1, st44d.get('drafts')
            print('TEST44B_DRAFT_RESTORED_ON_SEND_FAILURE_OK')
        finally:
            _dr44._get_context = _orig_ctx44
        # --- 44c: non-dict JSON bodies answer an error, never a raw 500 ---
        for _body44 in ([1, 2], 'nope', 5):
            _rn44 = _aio17.run(_nu44.Nudge(None, None).process(_body44, None))
            assert isinstance(_rn44, dict) and _rn44.get('success') is False, _rn44
            _rr44 = _aio17.run(_re44.Resolve(None, None).process(_body44, None))
            assert isinstance(_rr44, dict) and _rr44.get('success') is False, _rr44
            _rt44 = _aio17.run(_tn44.TestNotify(None, None).process(_body44, None))
            assert isinstance(_rt44, dict), _rt44
        print('TEST44C_NON_DICT_BODIES_GUARDED_OK')
        # --- 44d: /config does not report a garbage value as updated ---
        _disk44 = {'stall_minutes': 9}
        _orig_h44 = _cf44.plugins_helper
        _orig_rd44 = _cf44._read_disk

        def _helper44():
            return type('H44', (), {
                'get_plugin_config': staticmethod(
                    lambda name: _cd42.deep_merge_defaults(_disk44)),
                'save_plugin_config': staticmethod(lambda n, p, a, s: s),
                'clear_plugin_cache': staticmethod(lambda names: None),
            })()

        try:
            _cf44.plugins_helper = _helper44()
            _cf44._read_disk = lambda: _cd42.deep_merge_defaults(_disk44)
            _run44 = lambda b: _aio17.run(_cf44.Config(None, None).process(b, None))
            _r44e = _run44({'poll_seconds': 'garbage'})
            assert _r44e.get('success') is True, _r44e
            assert _r44e.get('updated') == [], _r44e
            _r44f = _run44({'max_auto_nudges': 5})
            assert _r44f.get('updated') == ['max_auto_nudges'], _r44f
        finally:
            _cf44.plugins_helper = _orig_h44
            _cf44._read_disk = _orig_rd44
        print('TEST44D_CONFIG_UPDATED_REPORTS_CHANGES_OK')
        # --- 44e: a restart-resume burst does not eat the stall budget ---
        # B's persisted status must sit OUTSIDE the restart-resume set
        # (running/stalled/nudged/intervention) or B joins the resume
        # queue; 'idle' plus a 40-minute-old last_message routes it through
        # classify into the stalled queue. B's log tail must be a
        # non-response entry ('response' would classify as awaiting_user).
        write_state((datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(), chats={
            FAKE_CHAT_A: {'status': 'running', 'nudge_count': 0, 'last_nudge_at': ''},
            FAKE_CHAT_B: {'status': 'idle', 'nudge_count': 0,
                          'last_nudge_at': (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()},
        })
        ctxA44 = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=20, running=False)
        ctxB44 = FakeCtx(FAKE_CHAT_B, ['user', 'tool'], idle_minutes=40, running=False)
        FakeAgentContext._all = [ctxA44, ctxB44]
        try:
            _s44 = monitor.tick(dict(cfg, max_nudges_per_tick=1))
            assert _s44.get('resumed') == 1, _s44
            assert _s44.get('nudged') == 1, _s44
            assert len(ctxA44.communicated) == 1, ctxA44.communicated
            # pre-fix this was 0: the resume burst consumed the stalled
            # queue's whole per-tick budget and the dashboard showed "capped".
            assert len(ctxB44.communicated) == 1, (_s44, ctxB44.communicated)
            _th44 = read_state().get('throttle', {})
            assert _th44.get('stalled_nudged') == 1, _th44
            assert _th44.get('capped') is False, _th44
        finally:
            FakeAgentContext._all = []
        print('TEST44E_STALL_BUDGET_INDEPENDENT_OF_RESUME_OK')
        # --- 44f: _post_json refuses redirects (SSRF re-validation gap) ---
        import inspect as _insp44
        _src44 = _insp44.getsource(monitor._post_json)
        assert 'allow_redirects=False' in _src44, _src44
        print('TEST44F_POST_JSON_NO_REDIRECTS_OK')
        # --- 44g: a rejected webhook URL never lands in the debug log ---
        _cfg44g = {'webhook_url': 'http://127.0.0.1/hook?token=CSREDACT44',
                   'webhook_allow_private': False}
        assert monitor._external_jobs(_cfg44g, 'info', 'probe') == []
        # v1.21.0 (A15): assert against the run's SCRATCH sink. This test used
        # to read the real production debug-YYYY-MM-DD.log - which is why it
        # only passed because the suite was writing into production state in
        # the first place.
        _tail44 = ''
        try:
            with open(_scratch_debug_path, 'r', encoding='utf-8') as f:
                _tail44 = f.read()
        except Exception:
            pass
        assert 'CSREDACT44' not in _tail44, 'webhook URL leaked into the debug log'
        assert 'webhook_skip' in _tail44, 'webhook_skip was not logged at all'
        print('TEST44G_WEBHOOK_SKIP_REDACTED_OK')
        # --- 44h: hotreload preserves plain Locks too (state._sig_lock) ---
        _m44h = types.ModuleType('cs_lock_test_44')
        _m44h._rlock44 = threading.RLock()
        _m44h._sig_lock44 = threading.Lock()
        _orig44h = {'r': _m44h._rlock44, 's': _m44h._sig_lock44}
        _snap44h = dict(vars(_m44h))
        _m44h._rlock44 = threading.RLock()
        _m44h._sig_lock44 = threading.Lock()
        _hr44._preserve_locks(_m44h, _snap44h)
        assert _m44h._rlock44 is _orig44h['r'], 'RLock identity not preserved'
        assert _m44h._sig_lock44 is _orig44h['s'], (
            'plain Lock identity not preserved (the v1.20.7 _sig_lock bug)')
        print('TEST44H_LOCK_PRESERVATION_COVERS_PLAIN_LOCK_OK')
        print('TEST44_AUDIT_FIXES_OK')

        # ============ TEST 45: v1.21.0 audit fixes (A1-A9, O1-O13) ============
        from usr.plugins.chat_shepherd.api import nudge as _nudge45
        from usr.plugins.chat_shepherd.api import resolve as _resolve45
        from usr.plugins.chat_shepherd.api import draft as _draft45
        from usr.plugins.chat_shepherd.api import status as _status45
        from usr.plugins.chat_shepherd.api import config as _config45
        from usr.plugins.chat_shepherd.helpers import adaptive as _adaptive45

        # --- 45a: load_state repairs unusable counters (A1, load boundary) ---
        # The load-boundary repair is the layer that closes the whole-tick
        # blast radius: every counter site in the drains sits OUTSIDE the
        # per-chat guard, so a string in state.json used to abort every tick.
        _bad45 = {
            'nudge_count': '3',
            'nudges_sent': '2',
            'nudges_effective': None,
            'wedge_nudge_count': ['x'],
            'wedge_nudges_sent': '1',
            'wedge_nudges_effective': '0',
            'goal_gate_count': '9',
            'goal_gate_nudges_sent': None,
            'last_log_len': '12',
            'last_updates_len': 'nope',
            'status': 'stalled',
        }
        files.write_file(state_mod.STATE_FILE, json.dumps({
            'chats': {FAKE_CHAT_A: _bad45},
            'last_tick': now_iso,
            'history': [],
        }))
        _rep45 = state_mod.load_state()
        _e45 = _rep45['chats'][FAKE_CHAT_A]
        for _k45 in ('nudge_count', 'nudges_sent', 'nudges_effective',
                     'wedge_nudge_count', 'wedge_nudges_sent',
                     'wedge_nudges_effective', 'goal_gate_count',
                     'goal_gate_nudges_sent'):
            assert isinstance(_e45[_k45], int), (
                'counter %s left as %r after load' % (_k45, _e45[_k45])
            )
        assert _e45['nudge_count'] == 3 and _e45['nudges_sent'] == 2, _e45
        assert _e45['last_log_len'] == 12, _e45
        assert _e45['last_updates_len'] == -1, _e45
        assert _rep45.get('repaired_counters') == 1, _rep45.get('repaired_counters')
        # ...and the arithmetic that used to raise now works.
        assert _e45['nudges_sent'] + 1 == 3
        assert not (_e45['nudge_count'] < 3)
        # a repair must NOT be adopted as the saved baseline, or the quiet-tick
        # gate would skip the one write that persists the repair
        assert state_mod.last_saved_signature() != state_mod.state_signature(_rep45), (
            'repaired payload adopted as baseline - the fix would never persist'
        )
        print('TEST45A_LOAD_REPAIRS_COUNTERS_OK')

        # --- 45b: a string counter cannot kill a tick (A1, drain arithmetic) ---
        # The drains run AFTER the per-chat loop with no guard, so
        # `entry['nudges_sent'] = entry.get('nudges_sent', 0) + 1` used to raise
        # on a string and take down the whole tick - no nudges, no prune, no cap
        # for ANY chat, traceable only in the debug log.
        # 45a already proves load_state() repairs the file, so this case feeds a
        # state straight to the tick with UNREPAIRED counters - i.e. any
        # in-process writer that bypassed the load boundary (an extension, a
        # hot-reloaded state module, a handler mutating its own snapshot). The
        # call sites must hold on their own.
        _raw45b = {
            'chats': {
                FAKE_CHAT_A: {
                    'status': 'stalled',
                    'nudge_count': '0',
                    'nudges_sent': '2',
                    'last_nudge_at': '',
                    'last_message': '',
                    'last_ticked': fresh_tick,
                },
            },
            'drafts': [], 'history': [], 'last_tick': fresh_tick,
            'adaptive': {},
        }
        _orig_load45b = state_mod.load_state
        _nudged45 = []
        _orig_nc45 = monitor._nudge_context
        _orig_all45b = FakeAgentContext._all
        _ctx45b = FakeCtx(FAKE_CHAT_A, ['user'], idle_minutes=30)
        FakeAgentContext._all = [_ctx45b]
        try:
            state_mod.load_state = lambda: json.loads(json.dumps(_raw45b))
            monitor._nudge_context = lambda ctx, text=None, chat_id=None: (
                _nudged45.append(chat_id) or True
            )
            _s45b = monitor.tick(wedge_cfg)
        finally:
            state_mod.load_state = _orig_load45b
            monitor._nudge_context = _orig_nc45
            FakeAgentContext._all = _orig_all45b
        assert _nudged45, 'a malformed counter silently stopped the nudge ladder'
        assert _s45b.get('tick_fail') is None, _s45b
        _st45b = read_state()['chats'][FAKE_CHAT_A]
        assert _st45b['nudge_count'] == 1 and _st45b['nudges_sent'] == 3, _st45b

        # ...and the SAME for the restart-resume drain, which is a separate
        # block of arithmetic with its own counter reads. Mutation testing
        # showed the stall-drain assertion alone does not cover it.
        _stale45b2 = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        _raw45b2 = {
            'chats': {
                FAKE_CHAT_B: {
                    'status': 'running',
                    'nudge_count': '0',
                    'nudges_sent': '4',
                    'last_nudge_at': '',
                    'last_message': '',
                    'last_ticked': _stale45b2,
                },
            },
            'drafts': [], 'history': [],
            # a >RESTART_GAP_MINUTES gap is what puts a chat on the resume queue
            'last_tick': _stale45b2,
            'adaptive': {},
        }
        _resumed45 = []
        _ctx45b2 = FakeCtx(FAKE_CHAT_B, ['user'], idle_minutes=30)
        FakeAgentContext._all = [_ctx45b2]
        try:
            state_mod.load_state = lambda: json.loads(json.dumps(_raw45b2))
            _orig_comm45b2 = monitor._nudge_context
            monitor._nudge_context = lambda ctx, text=None, chat_id=None: (
                _resumed45.append(chat_id) or True
            )
            _s45b2 = monitor.tick(wedge_cfg)
        finally:
            state_mod.load_state = _orig_load45b
            monitor._nudge_context = _orig_comm45b2
            FakeAgentContext._all = _orig_all45b
        assert _resumed45 == [FAKE_CHAT_B], (
            'the restart-resume drain must deliver despite string counters, '
            'got %r' % (_resumed45,)
        )
        assert _s45b2.get('tick_fail') is None, _s45b2
        print('TEST45B_BAD_COUNTER_CANNOT_KILL_TICK_OK')

        # --- 45c: /status degrades instead of 500 (A2) ---
        _orig_hcd45 = monitor._has_chat_dir
        try:
            monitor._has_chat_dir = lambda cid: True
            _p45 = _aio35.run(_CSStatus35(None, None).process({}, None))
            assert _p45.get('success') is True, _p45
            assert not _p45.get('degraded'), _p45
            # now make the whole snapshot explode
            _orig_agg45 = _status45.aggregates.compute
            _status45.aggregates.compute = lambda entries: 1 / 0
            _status45._AGG_CACHE['key'] = None
            try:
                _p45b = _aio35.run(_CSStatus35(None, None).process({}, None))
            finally:
                _status45.aggregates.compute = _orig_agg45
            # aggregates is individually guarded, so the snapshot still builds;
            # assert the GUARD exists by breaking an unguarded layer instead.
            assert _p45b.get('success') is True, _p45b
            _orig_bld45 = _CSStatus35._build_snapshot
            _CSStatus35._build_snapshot = lambda self: 1 / 0
            try:
                _p45c = _aio35.run(_CSStatus35(None, None).process({}, None))
            finally:
                _CSStatus35._build_snapshot = _orig_bld45
            assert _p45c.get('success') is True and _p45c.get('degraded') is True, _p45c
            assert _p45c.get('chats') == [] and _p45c.get('error'), _p45c
        finally:
            monitor._has_chat_dir = _orig_hcd45
        print('TEST45C_STATUS_DEGRADES_NOT_500_OK')

        # --- 45d: mistyped request fields are a 400-shaped answer, not a 500 ---
        # A3: the framework normalizes a non-dict BODY, but dict VALUES are raw.
        _orig_hcd45d = monitor._has_chat_dir
        _orig_ctx45d = _nudge45._get_context
        _sent45d = []
        try:
            monitor._has_chat_dir = lambda cid: True
            _nudge45._get_context = lambda chat_id: type(
                'C45', (), {'communicate': lambda self, m: _sent45d.append(chat_id)}
            )()
            _r45d = _aio35.run(_nudge45.Nudge(None, None).process(
                {'chat_id': 12345}, None))
            assert _r45d.get('success') is True, _r45d
            assert _sent45d == ['12345'], _sent45d
            _r45e = _aio35.run(_resolve45.Resolve(None, None).process(
                {'chat_id': 12345, 'action': 'Dismiss'}, None))
            assert _r45e.get('success') is True, _r45e
            _r45f = _aio35.run(_resolve45.Resolve(None, None).process(
                {'chat_id': FAKE_CHAT_A, 'action': 'dismissed'}, None))
            assert _r45f.get('success') is False and 'unknown action' in (
                _r45f.get('error') or ''
            ), 'an unknown action must not fall through to resolve: %r' % (_r45f,)
        finally:
            monitor._has_chat_dir = _orig_hcd45d
            _nudge45._get_context = _orig_ctx45d
        print('TEST45D_FIELD_COERCION_AND_ACTION_VALIDATION_OK')

        # --- 45e: adaptive config coercion (A4) ---
        _cfg45e = {
            'nudge_cooldown_minutes': 'abc',
            'wedge_nudge_after_minutes': None,
            'wedge_remediation_cooldown_minutes': 0,
        }
        _vals45e = _adaptive45.configured_values(_cfg45e)
        assert _vals45e['nudge_cooldown_minutes'] == 10.0, _vals45e
        assert _vals45e['wedge_nudge_after_minutes'] == 10.0, _vals45e
        # a legitimate 0 must survive (the old `or 10` turned it into 10)
        assert _vals45e['wedge_remediation_cooldown_minutes'] == 0.0, _vals45e
        print('TEST45E_ADAPTIVE_COERCION_OK')

        # --- 45f: /config save runs off the event loop (A5) ---
        _orig_helper45f = _config45.plugins_helper
        _seen45f = []

        class _H45:
            @staticmethod
            def get_plugin_config(name):
                return {'stall_minutes': 6}

            @staticmethod
            def save_plugin_config(name, p, a, s):
                _seen45f.append(('save', threading.current_thread().name))
                return s

            @staticmethod
            def clear_plugin_cache(names):
                _seen45f.append(('cache', threading.current_thread().name))
        try:
            _config45.plugins_helper = _H45()
            _caller45f = threading.current_thread().name
            _r45f = _aio35.run(_config45.Config(None, None).process(
                {'stall_minutes': 9}, None))
            assert _r45f.get('success') is True, _r45f
            kinds45f = [k for k, _ in _seen45f]
            assert 'save' in kinds45f and 'cache' in kinds45f, _seen45f
            for _k45f, _thr45f in _seen45f:
                assert _thr45f != _caller45f, (
                    '%s ran on the event loop (the v1.20.5 read-only offload '
                    'never covered the write)' % _k45f
                )
        finally:
            _config45.plugins_helper = _orig_helper45f
        print('TEST45F_CONFIG_WRITE_OFF_EVENT_LOOP_OK')

        # --- 45g: journal reads are incremental + parse outside the lock (O1/O8) ---
        _j45 = state_mod.JOURNAL_FILE
        _abs45 = files.get_abs_path(_j45)
        state_mod.JOURNAL_FILE = TEST_JOURNAL
        try:
            with open(files.get_abs_path(TEST_JOURNAL), 'w', encoding='utf-8') as _f45g:
                _f45g.write(''.join(
                    json.dumps({'action': 'nudge', 'i': _i}) + '\n' for _i in range(5)
                ))
            state_mod._journal_read_cache.pop(
                files.get_abs_path(TEST_JOURNAL), None)
            _all45 = state_mod.read_journal(100)
            assert [_x['i'] for _x in _all45] == [4, 3, 2, 1, 0], _all45
            _off45 = state_mod._journal_read_cache[
                files.get_abs_path(TEST_JOURNAL)][1]
            assert _off45 == os.path.getsize(files.get_abs_path(TEST_JOURNAL)), _off45
            # appending 3 more and reading again must see them
            for _i45 in (5, 6, 7):
                state_mod.append_journal({'action': 'nudge', 'i': _i45})
            _all45b = state_mod.read_journal(100)
            assert [_x['i'] for _x in _all45b] == [7, 6, 5, 4, 3, 2, 1, 0], _all45b
            # a torn final line must not corrupt the cache for the NEXT read
            with open(files.get_abs_path(TEST_JOURNAL), 'a', encoding='utf-8') as _f45g:
                _f45g.write(json.dumps({'action': 'nudge', 'i': 8})[:5])
            _all45c = state_mod.read_journal(100)
            assert [_x['i'] for _x in _all45c] == [7, 6, 5, 4, 3, 2, 1, 0], _all45c
            with open(files.get_abs_path(TEST_JOURNAL), 'a', encoding='utf-8') as _f45g:
                _f45g.write(json.dumps({'action': 'nudge', 'i': 8})[5:] + '\n')
            _all45d = state_mod.read_journal(100)
            assert [_x['i'] for _x in _all45d] == [8, 7, 6, 5, 4, 3, 2, 1, 0], _all45d
        finally:
            state_mod.JOURNAL_FILE = _j45

        # The RESULT alone cannot prove the tail path: a full re-parse returns
        # the same entries, so a mutation that disables the tail branch passes
        # every assertion above. Prove it by BYTES READ - after an append the
        # scan must read only the new tail, not the whole file.
        import builtins as _bi45g
        _real_open45g = _bi45g.open
        _read_bytes45g = [0]

        class _CountingFile:
            def __init__(self, inner):
                self._inner = inner

            def __enter__(self):
                self._inner.__enter__()
                return self

            def __exit__(self, *a):
                return self._inner.__exit__(*a)

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def read(self, *a):
                data = self._inner.read(*a)
                _read_bytes45g[0] += len(data)
                return data

        def _counting_open45g(path, *a, **k):
            handle = _real_open45g(path, *a, **k)
            if str(path).endswith(os.path.basename(TEST_JOURNAL)):
                return _CountingFile(handle)
            return handle

        state_mod.JOURNAL_FILE = TEST_JOURNAL
        try:
            with open(files.get_abs_path(TEST_JOURNAL), 'w', encoding='utf-8') as _f45g:
                _f45g.write(''.join(
                    json.dumps({'action': 'nudge', 'i': _i, 'pad': 'p' * 120})
                    + '\n' for _i in range(200)
                ))
            state_mod._journal_read_cache.pop(
                files.get_abs_path(TEST_JOURNAL), None)
            _bi45g.open = _counting_open45g
            state_mod.read_journal(500)
            _cold_bytes = _read_bytes45g[0]
            assert _cold_bytes > 20000, (
                'precondition: a cold read must pull the whole file, got %d bytes'
                % _cold_bytes
            )
            # steady state: nothing appended -> no journal I/O at all
            _read_bytes45g[0] = 0
            state_mod.read_journal(500)
            assert _read_bytes45g[0] == 0, (
                'an unchanged journal must not be re-read, read %d bytes'
                % _read_bytes45g[0]
            )
            # one appended row -> only that row is parsed
            state_mod.append_journal({'action': 'nudge', 'i': 999, 'pad': 'p' * 120})
            _read_bytes45g[0] = 0
            _after = state_mod.read_journal(500)
            assert _after[0].get('i') == 999, _after[:1]
            assert 0 < _read_bytes45g[0] < _cold_bytes // 4, (
                'an appended row must be read as a tail, not a full re-parse '
                '(read %d of %d bytes)' % (_read_bytes45g[0], _cold_bytes)
            )
        finally:
            _bi45g.open = _real_open45g
            state_mod.JOURNAL_FILE = _j45
            state_mod._journal_read_cache.pop(
                files.get_abs_path(TEST_JOURNAL), None)
        print('TEST45G_JOURNAL_INCREMENTAL_PARSE_OK')

        # --- 45h: one signature per save (O3) + compact payload (O4) ---
        _calls45h = []
        _orig_sig45h = state_mod.state_signature
        _orig_save45h = state_mod.save_state

        def _counting_save(st, signature=None):
            _calls45h.append(signature)
            return _orig_save45h(st, signature)
        try:
            state_mod.save_state = _counting_save
            def _bump(st):
                state_mod.get_chat(st, FAKE_CHAT_A)['status'] = 'stalled'
                return {'ok': True}
            state_mod.state_transaction_apply(_bump)
            assert len(_calls45h) == 1, _calls45h
            assert isinstance(_calls45h[0], str) and _calls45h[0], (
                'save_state recomputed the signature instead of using the '
                'one the transaction already computed'
            )
        finally:
            state_mod.save_state = _orig_save45h
            state_mod.state_signature = _orig_sig45h
        _raw45 = files.read_file(files.get_abs_path(state_mod.STATE_FILE))
        assert '\n  "' not in _raw45[:400], (
            'state.json is back to indent=2; that was ~40% extra bytes per '
            'durable write'
        )
        assert '"chats"' in _raw45 and state_mod.load_state()['chats'], _raw45[:200]
        print('TEST45H_SIGNATURE_REUSE_AND_COMPACT_OK')

        # --- 45i: one log lock per chat per pass (O5) ---
        _locks45i = []
        _ctx45i = FakeCtx(FAKE_CHAT_A, ['user', 'tool'], idle_minutes=1)
        _real_lock45i = _ctx45i.log._lock

        class _CountingLock:
            def __init__(self, inner):
                self._inner = inner

            def __enter__(self):
                _locks45i.append(1)
                return self._inner.__enter__()

            def __exit__(self, *a):
                return self._inner.__exit__(*a)
        try:
            _ctx45i.log._lock = _CountingLock(_real_lock45i)
            monitor._log_snapshot(_ctx45i)
            _view45i = monitor._log_snapshot(_ctx45i)
            assert len(_locks45i) == 2, len(_locks45i)
            for _fn45i in (
                monitor._log_len, monitor._log_updates, monitor._last_log_type,
                monitor._last_entry_type, monitor._last_response_text,
                monitor._last_log_fatal,
            ):
                _fn45i(_ctx45i, _view45i)
            assert len(_locks45i) == 2, (
                'six scanners over a shared snapshot took %d extra log locks; '
                'every one of them blocks the agent\'s own log writer'
                % (len(_locks45i) - 2)
            )
            # ...and with no snapshot the helper still works standalone
            assert monitor._log_len(_ctx45i) == 2, 'standalone _log_len broken'
            assert len(_locks45i) == 3, len(_locks45i)
        finally:
            _ctx45i.log._lock = _real_lock45i
        print('TEST45I_ONE_LOG_LOCK_PER_PASS_OK')

        # --- 45j: state module is reached through module-attr (A8) ---
        # The real invariant: no endpoint may PIN `load_state` / `read_journal`
        # with a from-import (such a binding survives a helpers/state.py
        # hot-reload, so the endpoint silently keeps serving pre-patch state
        # code), and no call site may invoke the bare name. Checked with `ast`
        # so docstrings and comments can never mask (or trip) it.
        import ast as _ast45j
        import inspect as _insp45j
        _export45j = __import__(
            'usr.plugins.chat_shepherd.api.export', fromlist=['x'])
        _history45j = __import__(
            'usr.plugins.chat_shepherd.api.history', fromlist=['x'])
        for _mod45j, _names45j in (
            (_status45, ('load_state', 'read_journal')),
            (_export45j, ('load_state', 'read_journal')),
            (_history45j, ('read_journal',)),
        ):
            _src45j = _insp45j.getsource(_mod45j)
            for _ln45j in _src45j.splitlines():
                _st45j = _ln45j.strip()
                if _st45j.startswith('from usr.plugins.chat_shepherd.helpers.state import'):
                    _imported45j = _st45j.split('import', 1)[1]
                    for _n45j in _names45j:
                        assert _n45j not in _imported45j, (
                            '%s pins %s via a from-import; a state.py '
                            'hot-reload would be a silent no-op there'
                            % (_mod45j.__name__, _n45j)
                        )
            for _node45j in _ast45j.walk(_ast45j.parse(_src45j)):
                if (
                    isinstance(_node45j, _ast45j.Call)
                    and isinstance(_node45j.func, _ast45j.Name)
                    and _node45j.func.id in _names45j
                ):
                    raise AssertionError(
                        '%s calls %s() as a bare name; use state_mod.%s() so a '
                        'state.py hot-reload reaches this endpoint'
                        % (_mod45j.__name__, _node45j.func.id, _node45j.func.id)
                    )
        print('TEST45J_STATE_ACCESS_IS_HOT_RELOAD_SAFE_OK')

        # --- 45k: every config read goes through the _load_config seam (A9) ---
        # v1.20.3 rule: only `_load_config` may call plugins.get_plugin_config.
        # Checked by source, because a runtime call site is the whole point -
        # and matched with the trailing '(' so a comment mentioning the API
        # name cannot mask (or trip) it.
        import glob as _glob45k
        _plugin_root45k = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        import ast as _ast45k
        _seam45k = _insp45j.getsource(monitor._load_config)
        assert '_plugins.get_plugin_config(' in _seam45k, (
            'the seam itself must be the one that reads the framework config'
        )
        for _rel45k in ('helpers/monitor.py', 'helpers/adaptive.py',
                        'helpers/aggregates.py', 'helpers/resume_draft.py',
                        'api/status.py', 'api/nudge.py', 'api/resolve.py',
                        'api/draft.py', 'api/history.py', 'api/export.py',
                        'api/test_notify.py'):
            _p45k = os.path.join(_plugin_root45k, _rel45k.replace('/', os.sep))
            with open(_p45k, 'r', encoding='utf-8') as _f45k:
                _tree45k = _ast45k.parse(_f45k.read(), filename=_p45k)
            for _node45k in _ast45k.walk(_tree45k):
                _fn45k = _node45k.func if isinstance(_node45k, _ast45k.Call) else None
                if (
                    isinstance(_fn45k, _ast45k.Attribute)
                    and _fn45k.attr == 'get_plugin_config'
                    and isinstance(_fn45k.value, _ast45j.Name)
                    and _fn45k.value.id == '_plugins'
                ):
                    _owner45k = [
                        _n45k.name for _n45k in _ast45k.walk(_tree45k)
                        if isinstance(_n45k, (_ast45j.FunctionDef, _ast45j.AsyncFunctionDef))
                        and _n45k is not _node45k
                        and any(_sub is _node45k for _sub in _ast45k.walk(_n45k))
                    ]
                    assert _owner45k == ['_load_config'], (
                        '%s calls plugins.get_plugin_config inside %s; only '
                        'monitor._load_config may (v1.20.3 seam)'
                        % (_rel45k, _owner45k)
                    )
        _notify45k = _insp45j.getsource(monitor._notify)
        assert '_plugins.get_plugin_config(' not in _notify45k, _notify45k
        assert '_load_config()' in _notify45k, _notify45k
        print('TEST45K_NOTIFY_USES_CONFIG_SEAM_OK')

        # --- 45l: hot-reload keys on nanosecond mtime (O13) ---
        _src45l = _insp45j.getsource(_hr44._check)
        assert 'st_mtime_ns' in _src45l and '.st_mtime\n' not in _src45l, _src45l
        print('TEST45L_HOTRELOAD_NANOSECOND_MTIME_OK')

        # --- 45m: "capped" must mean suppressed (A6) ---
        # Supervised mode with a zero per-tick budget queues editable drafts and
        # suppresses NOTHING, yet the throttle snapshot ORed
        # `max_nudges_per_tick <= 0` into `capped` unconditionally - the
        # dashboard showed a red "capped" badge on a run where no nudge was
        # ever withheld.
        _ctx45m = FakeCtx(FAKE_CHAT_A, ['user'], idle_minutes=30)
        FakeAgentContext._all = [_ctx45m]
        _cfg45m = dict(wedge_cfg, supervised_mode=True, max_nudges_per_tick=0)
        monitor.tick(_cfg45m)
        _thr45m = read_state().get('throttle', {})
        assert _thr45m.get('stalled_queued', 0) >= 1, _thr45m
        assert _thr45m.get('capped') is False, (
            'supervised drafts are not a cap, yet capped=%r' % _thr45m.get('capped')
        )
        # control: a real truncation still reports capped
        _ctx45m2 = [FakeCtx(FAKE_CHAT_A + '1', ['user'], idle_minutes=30),
                    FakeCtx(FAKE_CHAT_A + '2', ['user'], idle_minutes=30)]
        for _c45m in _ctx45m2:
            _c45m.name = 'Chat ' + _c45m.id
        FakeAgentContext._all = list(_ctx45m2)
        monitor.tick(dict(wedge_cfg, supervised_mode=False, max_nudges_per_tick=1))
        _thr45mb = read_state().get('throttle', {})
        if _thr45mb.get('stalled_queued', 0) > 1:
            assert _thr45mb.get('capped') is True, (
                'a truncated queue must still report capped: %r' % _thr45mb
            )
        print('TEST45M_SUPERVISED_NOT_CAPPED_OK')

        # --- 45n: aggregates are cached per journal stat key (O2) ---
        _calls45n = []
        _orig_compute45n = _status45.aggregates.compute
        try:
            _status45.aggregates.compute = lambda entries: (
                _calls45n.append(len(entries)), _orig_compute45n(entries))[1]
            _status45._AGG_CACHE['key'] = None
            _orig_hcd45n = monitor._has_chat_dir
            monitor._has_chat_dir = lambda cid: True
            _aio35.run(_CSStatus35(None, None).process({}, None))
            _first45n = len(_calls45n)
            _aio35.run(_CSStatus35(None, None).process({}, None))
            assert len(_calls45n) == _first45n, (
                'aggregates recomputed for an unchanged journal (%d -> %d)'
                % (_first45n, len(_calls45n))
            )
            state_mod.append_journal({'chat_id': FAKE_CHAT_A, 'action': 'agg_probe'})
            _aio35.run(_CSStatus35(None, None).process({}, None))
            assert len(_calls45n) == _first45n + 1, (
                'an appended journal row must invalidate the aggregates cache'
            )
        finally:
            _status45.aggregates.compute = _orig_compute45n
            monitor._has_chat_dir = _orig_hcd45n
        print('TEST45N_AGGREGATES_CACHE_OK')

        # --- 45o: /draft reads state twice, not three times (O7) ---
        _loads45o = []
        _orig_load45o = state_mod.load_state
        try:
            state_mod.load_state = lambda: (
                _loads45o.append(1), _orig_load45o())[1]
            _st45o = state_mod.load_state()
            # earlier blocks (45M) may have left a draft for this chat; the
            # one-per-chat dedupe would reject ours
            _st45o['drafts'] = [
                d for d in _st45o.get('drafts', [])
                if not (isinstance(d, dict) and d.get('chat_id') == FAKE_CHAT_A)
            ]
            assert state_mod.add_draft(
                _st45o, FAKE_CHAT_A, 'stall_nudge', reason='peek probe 45o')
            _did45o = _st45o['drafts'][0]['id']
            state_mod.save_state(_st45o)
            _dctx45o = FakeCtx(FAKE_CHAT_A, ['user'], idle_minutes=30)
            _orig_gc45o = _draft45._get_context
            _draft45._get_context = lambda cid: _dctx45o
            _loads45o.clear()
            try:
                _r45o = _aio35.run(
                    _CSDraft17(None, None).process(
                        {'draft_id': _did45o, 'action': 'send'}, None))
            finally:
                _draft45._get_context = _orig_gc45o
            assert _r45o.get('success') is True, _r45o
            assert len(_loads45o) == 2, (
                'a send must read state twice (claim + bookkeeping); the '
                'removed peek made it three, got %d' % len(_loads45o)
            )
        finally:
            state_mod.load_state = _orig_load45o
        print('TEST45O_DRAFT_PEEK_REMOVED_OK')

        # --- 45p: debug-log rotation runs once per day, not per line (O9) ---
        _globs45p = []
        _orig_glob45p = monitor.glob.glob
        try:
            monitor.glob.glob = lambda pat: (
                _globs45p.append(pat), _orig_glob45p(pat))[1]
            _real_debug45p = orig_debug_log
            monitor._debug_log = _real_debug45p
            _DEBUG_DAY = monitor._DEBUG_LOG_ROTATED_DAY
            monitor._DEBUG_LOG_ROTATED_DAY = _DEBUG_DAY
            _real_debug45p('probe45p_a', 'x')
            _real_debug45p('probe45p_b', 'x')
            _real_debug45p('probe45p_c', 'x')
            assert len(_globs45p) == 1, (
                'three log lines must glob the data dir once, saw %d'
                % len(_globs45p)
            )
            # a new UTC day rotates again
            monitor._DEBUG_LOG_ROTATED_DAY = '1999-01-01'
            _real_debug45p('probe45p_d', 'x')
            assert len(_globs45p) == 2, len(_globs45p)
        finally:
            monitor.glob.glob = _orig_glob45p
            monitor._debug_log = _scratch_debug
        print('TEST45P_DEBUG_ROTATION_ONCE_PER_DAY_OK')

        # --- 45q: the name cache evicts LRU instead of flushing (O10) ---
        # The old code called _NAME_CACHE.clear() at the cap, throwing away
        # every warm entry at once - with ~345 chats against a 120-entry cap
        # that is a burst of chat.json re-reads on a 9p mount. Exercised
        # through the real resolver, not the dict, because the eviction lives
        # inside the function.
        _probe45q = ['nme45q01', 'nme45q02', 'nme45q03', 'nme45q04']
        _orig_max45q = monitor._NAME_CACHE_MAX
        import shutil as _shutil45q
        try:
            for _n45q, _id45q in enumerate(_probe45q):
                # v1.21.3: under the redirected base, not the live usr/chats
                _dir45q = files.get_abs_path(TEST_CHAT_BASE + '/' + _id45q)
                os.makedirs(_dir45q, exist_ok=True)
                # the cache is keyed on chat.json's (mtime, size), so the file
                # has to exist for a miss to become a cached entry
                with open(os.path.join(_dir45q, 'chat.json'), 'w', encoding='utf-8') as _f45q:
                    _f45q.write(json.dumps({'name': 'Probe %d' % _n45q}))
            monitor._NAME_CACHE.clear()
            monitor._NAME_CACHE_MAX = 3
            for _id45q in _probe45q:
                monitor._chat_display_name(_id45q, None, {})
            assert len(monitor._NAME_CACHE) <= 3, sorted(monitor._NAME_CACHE)
            assert 'nme45q01' not in monitor._NAME_CACHE, (
                'the LEAST recently used entry must be evicted, kept: %s'
                % sorted(monitor._NAME_CACHE)
            )
            assert 'nme45q04' in monitor._NAME_CACHE, sorted(monitor._NAME_CACHE)
            # a hit must refresh recency rather than age out
            monitor._chat_display_name('nme45q02', None, {})
            os.makedirs(
                files.get_abs_path(TEST_CHAT_BASE + '/nme45q05'), exist_ok=True)
            with open(
                files.get_abs_path(TEST_CHAT_BASE + '/nme45q05/chat.json'), 'w',
                encoding='utf-8',
            ) as _f45q:
                _f45q.write(json.dumps({'name': 'Probe 5'}))
            monitor._chat_display_name('nme45q05', None, {})
            assert 'nme45q02' in monitor._NAME_CACHE, sorted(monitor._NAME_CACHE)
            for _id45q in _probe45q + ['nme45q05']:
                _shutil45q.rmtree(
                    files.get_abs_path(TEST_CHAT_BASE + '/' + _id45q),
                    ignore_errors=True,
                )
        finally:
            monitor._NAME_CACHE_MAX = _orig_max45q
            monitor._NAME_CACHE.clear()
        print('TEST45Q_NAME_CACHE_LRU_OK')

        # --- 45r: the suite itself must not write to the live debug log (A15) ---
        assert monitor._debug_log is not orig_debug_log, (
            'the suite must keep a scratch _debug_log for the whole run; '
            'otherwise every run appends its events to the production log'
        )
        assert monitor._debug_log.__name__ == '_scratch_debug', (
            monitor._debug_log.__name__
        )
        print('TEST45R_SUITE_DEBUG_LOG_IS_SCRATCH_OK')

        # --- 45s: coercion covers what TypeError/ValueError do not (cycle 2) ---
        # int(float('inf')) raises OverflowError, which the original
        # `except (TypeError, ValueError)` did not catch. On the LOAD path that
        # meant load_state() itself raised - with no guard around it - turning a
        # single hand-edited non-finite counter into exactly the whole-tick
        # blindness the repair exists to prevent.
        for _fn45s, _label45s in ((state_mod._as_int, 'state'),
                                  (monitor._as_int, 'monitor')):
            for _bad45s in (None, 'x', [], {}, float('nan'), float('inf'),
                            float('-inf'), object()):
                assert _fn45s(_bad45s, -1) == -1, (
                    '%s._as_int raised or leaked a value for %r' % (_label45s, _bad45s)
                )
            assert _fn45s('7', -1) == 7 and _fn45s(3.9, -1) == 3, _label45s
            assert _fn45s(True, -1) == 1, _label45s
        # Structural, TEST34-style: the guard must include OverflowError. A
        # mutation that dropped that arm (and kept only the two original
        # exceptions) let `load_state` itself raise on a non-finite counter -
        # i.e. it reintroduced the exact whole-tick failure class this repair
        # exists to prevent. Deliberately a source check rather than a value
        # check: once `int()` rejects every non-finite input on its own, a
        # behavioural test for that arm cannot exist.
        for _fn45s, _label45s in ((state_mod._as_int, 'state'),
                                  (monitor._as_int, 'monitor')):
            _src45s = _insp45j.getsource(_fn45s)
            _body45s = '\n'.join(
                _ln45s for _ln45s in _src45s.splitlines()
                if not _ln45s.strip().startswith('#')
            )
            assert 'OverflowError' in _body45s, (
                '%s._as_int must catch OverflowError: int(float("inf")) raises '
                'it, and TypeError/ValueError do not cover it' % _label45s
            )
            assert 'except (TypeError, ValueError):' not in _body45s, (
                '%s._as_int still has the pre-cycle-2 handler' % _label45s
            )
        # ...and end to end: an inf counter on disk must load, not explode
        files.write_file(state_mod.STATE_FILE, json.dumps({
            'chats': {FAKE_CHAT_A: {'nudge_count': float('inf'),
                                    'nudges_sent': float('nan')}},
            'last_tick': now_iso, 'history': [],
        }))
        _inf45 = state_mod.load_state()
        assert _inf45['chats'][FAKE_CHAT_A]['nudge_count'] == 0, _inf45['chats'][FAKE_CHAT_A]
        print('TEST45S_NONFINITE_COUNTERS_COERCED_OK')

        # --- 45t: read_journal limit semantics (cycle 2) ---
        # limit=0 / negative / None / non-numeric must not silently drop
        # entries. `selected[-limit]` with a negative limit returned NOTHING for
        # a short journal (data loss on a read path).
        # Its OWN journal file: earlier tests appended rows to TEST_JOURNAL, so
        # counting against that would assert on their leftovers, not on this.
        _jl45t = 'tmp/chat_shepherd_journal_limit.jsonl'
        _abs45t = files.get_abs_path(_jl45t)
        files.write_file(_jl45t, '')
        _jt45 = state_mod.JOURNAL_FILE
        try:
            state_mod.JOURNAL_FILE = _jl45t
            state_mod._journal_read_cache.pop(_abs45t, None)
            for _i45t in range(6):
                state_mod.append_journal({'action': 'nudge', 'i': _i45t})
            for _lim45t in (0, -3, None, 'x'):
                assert len(state_mod.read_journal(_lim45t)) == 6, (
                    'limit=%r silently dropped journal entries' % (_lim45t,)
                )
            assert len(state_mod.read_journal(3)) == 3
            assert [e['i'] for e in state_mod.read_journal(3)] == [5, 4, 3]
        finally:
            state_mod.JOURNAL_FILE = _jt45
            state_mod._journal_read_cache.pop(_abs45t, None)
            try:
                os.unlink(_abs45t)
            except OSError:
                pass
        print('TEST45T_JOURNAL_LIMIT_SEMANTICS_OK')

        # --- 45u: hostile state shapes degrade, never raise (cycle 2) ---
        _shapes45u = (
            {'chats': 'not-a-dict'},
            {'chats': {FAKE_CHAT_A: 'junk-entry'}},
            {'chats': {FAKE_CHAT_A: {'nudge_count': None, 'last_log_len': 'x'}}},
        )
        for _shape45u in _shapes45u:
            files.write_file(state_mod.STATE_FILE, json.dumps(_shape45u))
            _ok45u = state_mod.load_state()
            assert isinstance(_ok45u.get('chats'), dict), _shape45u
            assert all(
                isinstance(_e, dict)
                for _e in _ok45u['chats'].values()
            ), _ok45u['chats']
        # cap_chats must survive a non-dict entry mid-dict
        _cap45u = {'chats': {FAKE_CHAT_A: {'last_ticked': '2026-01-01'},
                             FAKE_CHAT_B: 'junk',
                             FAKE_CHAT_C: {'last_ticked': '2026-01-03'}}}
        state_mod.cap_chats(_cap45u, keep=1, protected=set())
        assert FAKE_CHAT_C in _cap45u['chats'], _cap45u
        print('TEST45U_HOSTILE_STATE_SHAPES_DEGRADE_OK')

        # --- 45v: hostile INPUT bodies on every endpoint (cycle 3) ---
        # Driven through the real handlers with values a JSON body can carry
        # (Python's json.loads accepts Infinity/NaN, so these are not
        # theoretical). v1.20.7 added `isinstance(input, dict)` to four
        # handlers and missed /history; the numeric holes below are the same
        # class in the fields those guards do not cover.
        from usr.plugins.chat_shepherd.api import history as _his45v
        from usr.plugins.chat_shepherd.api import test_notify as _tn45v
        _jl45v = 'tmp/chat_shepherd_cycle3.jsonl'
        files.write_file(_jl45v, '')
        _abs45v = files.get_abs_path(_jl45v)
        _jj45v = state_mod.JOURNAL_FILE
        try:
            state_mod.JOURNAL_FILE = _jl45v
            state_mod._journal_read_cache.pop(_abs45v, None)
            for _i45v in range(6):
                state_mod.append_journal({'chat_id': FAKE_CHAT_A, 'action': 'x'})
            # /history must answer, never raise, for every body shape
            for _bad45v in (['a'], 'str', 5, None, {'chat_id': FAKE_CHAT_A,
                                                    'limit': float('inf')},
                            {'chat_id': FAKE_CHAT_A, 'limit': float('nan')},
                            {'chat_id': FAKE_CHAT_A, 'limit': {}},
                            {'chat_id': FAKE_CHAT_A, 'limit': [3]}):
                _r45v = _aio35.run(
                    _his45v.History(None, None).process(_bad45v, None))
                assert isinstance(_r45v, dict) and 'success' in _r45v, (
                    '/history raised or answered wrongly for %r' % (_bad45v,)
                )
            # a legit limit still trims; a junk one falls back to the default
            assert len(_aio35.run(_his45v.History(None, None).process(
                {'chat_id': FAKE_CHAT_A, 'limit': 3}, None))['history']) == 3
        finally:
            state_mod.JOURNAL_FILE = _jj45v
            state_mod._journal_read_cache.pop(_abs45v, None)
            try:
                os.unlink(_abs45v)
            except OSError:
                pass
        # /status must not degrade on a non-int persisted throttle field
        _orig_hcd45v = monitor._has_chat_dir
        _orig_cfg45v = monitor._load_config
        try:
            monitor._has_chat_dir = lambda cid: True
            monitor._load_config = lambda: {
                'enabled': True, 'icons': {}, 'max_nudges_per_tick': 3,
            }
            for _th45v in ({'budget': float('inf')}, {'budget': 'abc'},
                           {'budget': None}, 'junk', {}):
                files.write_file(state_mod.STATE_FILE, json.dumps({
                    'chats': {}, 'drafts': [], 'history': [], 'last_tick': '',
                    'throttle': _th45v,
                }))
                _p45v = _aio35.run(_CSStatus35(None, None).process({}, None))
                assert _p45v.get('degraded') is None, (
                    'a junk throttle budget degraded the whole snapshot: %r'
                    % (_th45v,)
                )
                assert (_p45v.get('throttle') or {}).get('budget') == 3, (
                    'an unusable budget must fall back to the config value, '
                    'not 0 (which would read as "budget fully used"): %r'
                    % (_p45v.get('throttle'),)
                )
            # ...but a legitimate 0 budget must survive as 0
            files.write_file(state_mod.STATE_FILE, json.dumps({
                'chats': {}, 'drafts': [], 'history': [], 'last_tick': '',
                'throttle': {'budget': 0},
            }))
            _p45w = _aio35.run(_CSStatus35(None, None).process({}, None))
            assert (_p45w.get('throttle') or {}).get('budget') == 0, (
                'budget 0 means auto-nudging off and must not be replaced by '
                'the default: %r' % (_p45w.get('throttle'),)
            )
            # the CONFIG side of the same seam: a non-int poll_seconds must be
            # clamped, not raise into the degrade guard. This is what a
            # hand-edited config.json looks like, and the dashboard poll
            # interval comes from exactly this key.
            for _cfg45v, _want in (
                ({'poll_seconds': float('inf')}, 5),
                ({'poll_seconds': 'abc'}, 5),
                ({'poll_seconds': None}, 5),
                ({'poll_seconds': 0}, 2),          # clamped up to the floor
                ({'poll_seconds': 10 ** 9}, 120),   # clamped down to the ceiling
                ({'poll_seconds': 7}, 7),
            ):
                monitor._load_config = lambda c=dict(_cfg45v): dict(
                    c, enabled=True, icons={}, max_nudges_per_tick=3)
                files.write_file(state_mod.STATE_FILE, json.dumps({
                    'chats': {}, 'drafts': [], 'history': [], 'last_tick': '',
                }))
                _pc45v = _aio35.run(_CSStatus35(None, None).process({}, None))
                assert _pc45v.get('degraded') is None, (
                    'config %r degraded the snapshot: %s'
                    % (_cfg45v, _pc45v.get('error'))
                )
                assert (_pc45v.get('config') or {}).get('poll_seconds') == _want, (
                    'poll_seconds %r -> %r, expected %r'
                    % (_cfg45v, (pc45v_cfg := _pc45v.get('config') or {}).get('poll_seconds'), _want)
                )
        finally:
            monitor._has_chat_dir = _orig_hcd45v
            monitor._load_config = _orig_cfg45v
        print('TEST45V_HOSTILE_BODIES_NEVER_RAISE_OK')

        # --- 45w: the float seams (cycle 3) ---
        # `_as_float` guards the tick HEADER - outside the per-chat guard - so
        # the v1.20.7 reasoning applies: raising there kills the whole tick.
        # Two holes survived that fix. (1) `float(10**400)` raises
        # OverflowError, which the two-arm handler did not catch, and JSON
        # permits arbitrary-precision integer literals. (2) `float('inf')` and
        # `float('nan')` SUCCEED - worse than raising: an inf cooldown makes
        # every chat instantly off-cooldown and an inf stall threshold makes
        # every chat instantly stalled, so a hand-edited value silently INVERTS
        # the supervision policy instead of degrading to the default.
        _INF_NAN = (float('inf'), float('-inf'), float('nan'))
        for _fn45w, _label45w in ((monitor._as_float, 'monitor._as_float'),
                                  (lambda v, f: _adaptive45._num(
                                      {'k': v}, 'k', f), 'adaptive._num')):
            _out45w = _fn45w(10 ** 400, 7.0)
            assert _out45w == 7.0, (_label45w, '10**400 ->', _out45w)
            for _nf45w in _INF_NAN:
                _out45w = _fn45w(_nf45w, 7.0)
                assert _out45w == 7.0, (
                    '%s passed through %r instead of the fallback: %r'
                    % (_label45w, _nf45w, _out45w)
                )
                assert _out45w == _out45w and _out45w not in _INF_NAN, (
                    '%s returned a non-finite value' % _label45w
                )
            assert _fn45w('5.5', 7.0) == 5.5, _label45w
            assert _fn45w('abc', 7.0) == 7.0, _label45w
            assert _fn45w(None, 7.0) == 7.0, _label45w
            # a legitimate 0 must survive (it is a real threshold, not a
            # "missing" marker)
            assert _fn45w(0, 7.0) == 0.0, _label45w
        # ...and end to end through the public entry point
        _vals45w = _adaptive45.configured_values({
            'nudge_cooldown_minutes': 10 ** 400,
            'wedge_nudge_after_minutes': float('inf'),
            'wedge_remediation_cooldown_minutes': 'z',
        })
        assert _vals45w == {
            'nudge_cooldown_minutes': 10.0,
            'wedge_nudge_after_minutes': 10.0,
            'wedge_remediation_cooldown_minutes': 10.0,
        }, _vals45w
        print('TEST45W_FLOAT_SEAMS_NEVER_RAISE_OR_GO_NONFINITE_OK')

        # --- 45x: one seam for the chat-dir base (v1.21.1) ---
        # `_CHAT_DIR_BASE` existed only for the scandir; four sites interpolated
        # 'usr/chats/' inline, including the FALLBACK inside `_has_chat_dir` -
        # the probe that decides whether a chat is supervised at all. If the
        # base and the listing disagreed, `_has_chat_dir` returns False on
        # exception and a real chat stops being nudged with no visible error.
        import inspect as _insp45x
        import ast as _ast45x
        # Checked with `ast`, not by grepping text: docstrings legitimately
        # mention the literal, and a text check would either miss a real inline
        # concatenation or have to exempt prose by hand.
        _tree45x = _ast45x.parse(
            _insp45x.getsource(monitor), filename='monitor.py')
        # No chat path may be built with an f-string over chat_id...
        # (the one legitimate f-string over `chat_id` is an intervention
        # MESSAGE, not a path - so the check requires a path-shaped literal.)
        _fstrings45x = [
            _n45x for _n45x in _ast45x.walk(_tree45x)
            if isinstance(_n45x, _ast45x.JoinedStr)
            and any(
                isinstance(_p45x, _ast45x.FormattedValue)
                and isinstance(getattr(_p45x, 'value', None), _ast45x.Name)
                and getattr(_p45x.value, 'id', '') == 'chat_id'
                for _p45x in _n45x.values
            )
            and any(
                isinstance(_p45x, _ast45x.Constant)
                and isinstance(_p45x.value, str)
                and ('usr/chats' in _p45x.value or _p45x.value.endswith('/'))
                for _p45x in _n45x.values
            )
        ]
        assert not _fstrings45x, (
            'monitor builds a chat path with an f-string over chat_id; route '
            'it through _chat_dir() so the base cannot drift'
        )
        # ...nor by carrying the literal base anywhere else. Docstring nodes are
        # skipped by identity rather than by text: `get_docstring` cleandocs
        # (re-indents) the value, so comparing the two never matches.
        _litfuncs45x = []
        for _fn45x in _tree45x.body:
            if not isinstance(_fn45x, (_ast45x.FunctionDef,
                                       _ast45x.AsyncFunctionDef)):
                continue
            if _fn45x.name == '_chat_dir':
                continue
            _docnode45x = _fn45x.body[0] if _fn45x.body else None
            _docnode45x = (
                _docnode45x.value
                if isinstance(_docnode45x, _ast45x.Expr)
                and isinstance(_docnode45x.value, _ast45x.Constant)
                else None
            )
            for _node45x in _ast45x.walk(_fn45x):
                if _node45x is _docnode45x:
                    continue
                if (
                    isinstance(_node45x, _ast45x.Constant)
                    and isinstance(_node45x.value, str)
                    and 'usr/chats' in _node45x.value
                ):
                    _litfuncs45x.append((_fn45x.name, _node45x.value.strip()[:40]))
        assert not _litfuncs45x, (
            'functions besides _chat_dir still carry the usr/chats literal: %s'
            % _litfuncs45x
        )
        # ...and the seam actually resolves under the configured base
        _orig_base45x = monitor._CHAT_DIR_BASE
        _probe45x = files.get_abs_path('tmp/chat_shepherd_base_probe/' + FAKE_CHAT_A)
        try:
            monitor._CHAT_DIR_BASE = 'tmp/chat_shepherd_base_probe'
            monitor._chat_listing_cache.clear()
            os.makedirs(_probe45x, exist_ok=True)
            assert monitor._has_chat_dir(FAKE_CHAT_A) is True, (
                'the seam must resolve the chat under the configured base'
            )
            monitor._chat_dir_ids = lambda: None      # force the fallback path
            assert monitor._has_chat_dir(FAKE_CHAT_A) is True, (
                'the scandir-less fallback must use the same seam'
            )
            assert monitor._chat_dir(FAKE_CHAT_A) == _probe45x, monitor._chat_dir(
                FAKE_CHAT_A)
        finally:
            monitor._CHAT_DIR_BASE = _orig_base45x
            monitor._chat_listing_cache.clear()
            import shutil as _shutil45x
            _shutil45x.rmtree(
                files.get_abs_path('tmp/chat_shepherd_base_probe'),
                ignore_errors=True,
            )
        print('TEST45X_CHAT_DIR_BASE_HAS_ONE_SEAM_OK')

        # --- 45y: this suite must never touch the live chat tree (P6.2) ---
        # The behavioural half of the seam guarantee. TEST45X pins the code
        # shape; this pins the effect: no fixture chat may exist under the
        # REAL usr/chats at any point. Those ids match CHAT_ID_PATTERN, so a
        # leftover from a crashed run would be picked up by a live tick and
        # classified - and potentially nudged - as a real chat.
        _leaked45y = [
            _n45y for _n45y in os.listdir(files.get_abs_path('usr/chats'))
            if _n45y.startswith(('csTst', 'nme45q'))
        ] if os.path.isdir(files.get_abs_path('usr/chats')) else []
        assert not _leaked45y, (
            'test fixtures leaked into the live chat tree: %s' % _leaked45y
        )
        # and the fixtures really are under the scratch base, not absent
        assert monitor._CHAT_DIR_BASE == TEST_CHAT_BASE, (
            'the chat base must stay redirected for the whole run, got %r'
            % (monitor._CHAT_DIR_BASE,)
        )
        assert monitor._has_chat_dir(FAKE_CHAT_A) is True, (
            'the fixture chat must resolve under the redirected base'
        )
        assert os.path.isdir(_chat_dir(FAKE_CHAT_A)), (
            'fixture chat missing from the scratch base'
        )
        print('TEST45Y_SUITE_NEVER_TOUCHES_LIVE_CHAT_TREE_OK')

        # --- 45z: the signature cache (v1.21.3) ---
        # `load_state` needs the fingerprint only to adopt the saved baseline,
        # and the basis comes entirely from the file, so unchanged bytes can
        # only produce an unchanged fingerprint. It was 50% of load_state on
        # the audit host - and load_state runs on every /status poll per tab.
        # What matters is not "it is faster" but that it never returns a stale
        # fingerprint, which would silently disarm the quiet-tick gate.
        _abs45z = files.get_abs_path(state_mod.STATE_FILE)
        # self-contained: do not depend on what the previous block left behind
        _seed45z = {
            'chats': {
                FAKE_CHAT_A: {'status': 'stalled', 'nudge_count': 1},
                FAKE_CHAT_B: {'status': 'idle', 'nudge_count': 0},
            },
            'drafts': [], 'history': [], 'last_tick': now_iso,
        }
        with open(_abs45z, 'w', encoding='utf-8') as _f45z:
            json.dump(_seed45z, _f45z)
        state_mod._state_sig_cache = None
        _st45z = state_mod.load_state()
        assert _st45z.get('chats'), 'expected chats in the scratch state'
        _fresh45z = state_mod.state_signature(_st45z)
        assert state_mod._state_signature_cached(_st45z, _abs45z) == _fresh45z, (
            'a cached signature disagreed with a fresh compute'
        )
        # ...and the cache must be invalidated by a write. The seam takes the
        # RAW TEXT (the cache is keyed on a digest of it), so this must go
        # through the same call shape load_state uses - passing the mutated
        # state with the OLD bytes would ask the cache about bytes that no
        # longer exist.
        _mut45z = json.loads(json.dumps(_st45z))
        _first45z = sorted(_mut45z['chats'])[0]
        _mut45z['chats'][_first45z]['status'] = 'MUTATED45Z'
        _raw45z = state_mod._state_sig_cache
        _mut_raw45z = json.dumps(_mut45z)
        with open(_abs45z, 'w', encoding='utf-8') as _f45z:
            _f45z.write(_mut_raw45z)
        try:
            assert state_mod._state_signature_cached(_mut45z, _mut_raw45z) != _fresh45z, (
                'the cache returned a STALE signature after the content '
                'changed - the quiet-tick gate would skip a real save'
            )
            # ...and an UNCHANGED reload must still hit the cache
            _calls45z = []
            _state_mod_save45z = state_mod.state_signature
            try:
                state_mod.state_signature = (
                    lambda st_, _o=_state_mod_save45z: (
                        _calls45z.append(1), _o(st_))[1]
                )
                _again45z = state_mod._state_signature_cached(_mut45z, _mut_raw45z)
            finally:
                state_mod.state_signature = _state_mod_save45z
            assert _again45z == state_mod.state_signature(_mut45z), (
                'an identical reload must agree with a fresh compute'
            )
            assert not _calls45z, (
                'a repeated load re-serialized the file (%d times) - the cache '
                'is not doing its job' % len(_calls45z)
            )
        finally:
            with open(_abs45z, 'w', encoding='utf-8') as _f45z:
                json.dump(_st45z, _f45z)
            state_mod._state_sig_cache = _raw45z
            state_mod._last_saved_sig = ''
            state_mod._last_loaded_sig = ''
        # a transaction must still save exactly once on a real change, and not
        # at all on a no-op (the gate the cache feeds)
        _saves45z = []
        _orig_save45z = state_mod.save_state
        state_mod.save_state = lambda st_, sig=None: _saves45z.append(sig)
        try:
            state_mod.state_transaction_apply(lambda st_: {'noop': True})
            assert not _saves45z, 'a no-op transaction saved %d times' % len(_saves45z)
            def _bump45z(st_):
                st_['chats'][_first45z]['status'] = 'NUDGED45Z'
                return {'ok': True}
            state_mod.state_transaction_apply(_bump45z)
            assert len(_saves45z) == 1, 'a real change saved %d times' % len(_saves45z)
            assert isinstance(_saves45z[0], str) and _saves45z[0], (
                'save_state must receive the signature the transaction computed'
            )
        finally:
            state_mod.save_state = _orig_save45z
            state_mod._state_sig_cache = None
            write_state(now_iso, chats={FAKE_CHAT_A: {'status': 'idle'}})
        print('TEST45Z_SIGNATURE_CACHE_NEVER_STALE_OK')

        # --- 45aa: the two identity/key contracts, pinned directly ---
        # (a) CONTENT identity, not the file stat. state.json is rewritten in
        # whole on every save, and two payloads of identical length written
        # inside the filesystem's timestamp granularity share (mtime_ns, size) -
        # that is the bug this cache was born with, and it silently disarmed the
        # quiet-tick gate. Same-length content is constructed here on purpose.
        import hashlib as _hl45aa
        _raw_a45aa = json.dumps({'chats': {'aaaaaaaa': {'status': 'idle'}}})
        _raw_b45aa = json.dumps({'chats': {'bbbbbbbb': {'status': 'idle'}}})
        assert len(_raw_a45aa) == len(_raw_b45aa), 'fixture lengths must match'
        assert _raw_a45aa != _raw_b45aa
        state_mod._state_sig_cache = None
        _pa45aa = json.loads(_raw_a45aa)
        _sig_a45aa = state_mod._state_signature_cached(_pa45aa, _raw_a45aa)
        _pb45aa = json.loads(_raw_b45aa)
        _sig_b45aa = state_mod._state_signature_cached(_pb45aa, _raw_b45aa)
        assert _sig_a45aa != _sig_b45aa, (
            'same-length different content reused a fingerprint - the cache is '
            'keyed on something that is not the content'
        )
        # and identical content must be stable regardless of spacing/ordering
        _raw_a2_45aa = json.dumps({'chats': {'aaaaaaaa': {'status': 'idle'}}},
                                  separators=(', ', ': '))
        _pa2_45aa = json.loads(_raw_a2_45aa)
        assert state_mod._state_signature_cached(_pa2_45aa, _raw_a2_45aa) == _sig_a45aa, (
            'a byte-identical payload must not change the fingerprint'
        )
        # (b) the transaction must baseline on what the LOAD read. Structural,
        # and deliberately so: `state_transaction_apply` holds `_state_lock`
        # and calls `load_state()` itself, which re-establishes BOTH values, so
        # a behavioural test cannot distinguish them - mutation testing
        # confirmed both alternatives escape. Two attempts to pin this
        # behaviourally failed for exactly that reason. `_last_loaded_sig` is
        # still the right value to read (it is unconditional, while the save
        # baseline is adopted only when no save raced and nothing was
        # repaired), and the assertion below keeps the choice explicit.
        _txn_src45aa = _insp45j.getsource(state_mod.state_transaction_apply)
        assert '_last_loaded_sig' in _txn_src45aa, (
            'the transaction must baseline on the loaded fingerprint'
        )
        assert 'last_saved_signature()' not in _txn_src45aa, (
            'the transaction must not baseline on the conditionally-adopted '
            'save baseline - that value can describe different bytes'
        )
        # ...and it must not RESERIALIZE the file just to find that out: the
        # whole point of _last_loaded_sig is that the pre-apply baseline is
        # already known. This is the assertion that catches the pure-perf
        # mutation (recomputing it is correct, just slow), which a
        # correctness-only test cannot see.
        _sigcalls45aa = []
        _seed45aa = {'chats': {'aaaaaaaa': {'status': 'stalled', 'nudge_count': 0}},
                     'drafts': [], 'history': [], 'last_tick': now_iso}
        _abs45aa = files.get_abs_path(state_mod.STATE_FILE)
        with open(_abs45aa, 'w', encoding='utf-8') as _f45aa:
            json.dump(_seed45aa, _f45aa)
        state_mod._state_sig_cache = None
        state_mod.load_state()
        _orig_sig45aa = state_mod.state_signature
        _orig_save45aa = state_mod.save_state
        try:
            def _counting_sig45aa(st_, _o=_orig_sig45aa):
                _sigcalls45aa.append(1)
                return _o(st_)
            state_mod.state_signature = _counting_sig45aa
            state_mod.save_state = lambda st_, sig=None: None
            # a no-op transaction: the pre-apply baseline must come from
            # _last_loaded_sig, so the only signature call is the POST-apply one
            state_mod.state_transaction_apply(lambda st_: {'noop': True})
            assert len(_sigcalls45aa) <= 1, (
                'the transaction re-serialized the state to learn a baseline '
                'it already had (%d signature calls for a no-op; expected 1 '
                'post-apply)' % len(_sigcalls45aa)
            )
        finally:
            state_mod.state_signature = _orig_sig45aa
            state_mod.save_state = _orig_save45aa
        _st45aa = state_mod.load_state()
        _loaded45aa = state_mod.state_signature(_st45aa)
        assert state_mod._last_loaded_sig == _loaded45aa, (
            'load_state must record the fingerprint of the bytes it read'
        )
        # and the end-to-end contract that actually matters: a real change is
        # saved exactly once, a no-op not at all.
        _calls45aa = []
        _orig_save45aa = state_mod.save_state
        try:
            state_mod.save_state = lambda st_, sig=None: _calls45aa.append(sig)
            state_mod.state_transaction_apply(lambda st_: {'noop': True})
            assert not _calls45aa, (
                'a no-op transaction saved %d times' % len(_calls45aa)
            )
            def _bump45aa(st_):
                st_['chats']['aaaaaaaa']['nudge_count'] = 1
                return {'ok': True}
            state_mod.state_transaction_apply(_bump45aa)
        finally:
            state_mod.save_state = _orig_save45aa
        assert len(_calls45aa) == 1, (
            'a real change must save exactly once, got %d' % len(_calls45aa)
        )
        state_mod._last_saved_sig = ''
        state_mod._last_loaded_sig = ''
        state_mod._state_sig_cache = None
        print('TEST45AA_IDENTITY_IS_CONTENT_NOT_STAT_OK')

        print('TEST45_V121_AUDIT_FIXES_OK')

        print('ALL_TESTS_PASSED')
    finally:
        state_mod.STATE_FILE = orig_state_file

        state_mod.JOURNAL_FILE = orig_journal_file
        monitor.AgentContext = orig_agent_ctx
        # v1.21.3: restore the real chat base before cleanup() runs, so the
        # teardown removes only the scratch tree this run created.
        monitor._CHAT_DIR_BASE = orig_chat_base
        monitor._chat_listing_cache.clear()
        monitor._error_file_cache.clear()
        monitor._NAME_CACHE.clear()
        # v1.21.0 (A15): restore the real sink, and drop the scratch file so a
        # run leaves nothing behind in the plugin tree.
        monitor._debug_log = orig_debug_log
        try:
            if os.path.exists(_scratch_debug_path):
                os.unlink(_scratch_debug_path)
        except OSError:
            pass
        FakeAgentContext._all = []
        cleanup()


if __name__ == '__main__':
    main()
