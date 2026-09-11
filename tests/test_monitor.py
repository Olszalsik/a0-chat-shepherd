import json
import os
import sys
import threading
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, '/a0')
os.chdir('/a0')

from usr.plugins.chat_shepherd.helpers import constants, monitor
from usr.plugins.chat_shepherd.helpers import state as state_mod
from helpers import files

TEST_STATE = 'usr/plugins/chat_shepherd/data/test_state.json'
FAKE_CHAT_A = 'csTstA01'
FAKE_CHAT_B = 'csTstB02'
FAKE_CHAT_C = 'csTstC03'


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


def read_state():
    return json.loads(files.read_file(files.get_abs_path(TEST_STATE)))


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
    except Exception:
        pass


def main():
    orig_state_file = state_mod.STATE_FILE
    orig_agent_ctx = monitor.AgentContext
    state_mod.STATE_FILE = TEST_STATE
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

        print('ALL_TESTS_PASSED')
    finally:
        state_mod.STATE_FILE = orig_state_file
        monitor.AgentContext = orig_agent_ctx
        FakeAgentContext._all = []
        cleanup()


if __name__ == '__main__':
    main()
