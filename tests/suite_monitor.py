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

def _truncate_journal():
    try:
        os.unlink(files.get_abs_path(TEST_JOURNAL))
    except Exception:
        pass


class FakeCtx:
    def __init__(self, cid, log_types, idle_minutes, ctype=None, running=False):
        self.id = cid
        self.log = types.SimpleNamespace(
            _lock=threading.Lock(),
            logs=[types.SimpleNamespace(type=t) for t in log_types],
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
    for cid in (FAKE_CHAT_A, FAKE_CHAT_B, FAKE_CHAT_C):
        d = files.get_abs_path('usr/chats/' + cid)
        os.makedirs(d, exist_ok=True)
    # error path for the no-context classification test
    with open(os.path.join(files.get_abs_path('usr/chats/' + FAKE_CHAT_C), 'error.txt'), 'w') as f:
        f.write('boom')


def cleanup():
    for cid in (FAKE_CHAT_A, FAKE_CHAT_B, FAKE_CHAT_C):
        d = files.get_abs_path('usr/chats/' + cid)
        try:
            import shutil
            shutil.rmtree(d, ignore_errors=True)
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
    state_mod.STATE_FILE = TEST_STATE

    state_mod.JOURNAL_FILE = TEST_JOURNAL
    monitor.AgentContext = FakeAgentContext
    try:
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
        _dir_a = files.get_abs_path('usr/chats/' + FAKE_CHAT_A)
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
        print('ALL_TESTS_PASSED')
    finally:
        state_mod.STATE_FILE = orig_state_file

        state_mod.JOURNAL_FILE = orig_journal_file
        monitor.AgentContext = orig_agent_ctx
        FakeAgentContext._all = []
        cleanup()


if __name__ == '__main__':
    main()
