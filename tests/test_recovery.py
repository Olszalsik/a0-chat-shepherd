"""Regression cases for bounded logs, failure recovery, and spent quotas.

Run with the Agent Zero framework on PYTHONPATH. All state and chats are
temporary; contexts never contact a provider or deliver a real message.
"""
import asyncio
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from usr.plugins.chat_shepherd.helpers import config_defaults as cd
from usr.plugins.chat_shepherd.helpers import diagnose, monitor, state

CID = 'Retry001'


def stamp(minutes=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def item(kind='error', content='TimeoutError: connection timed out', age=600, **kw):
    return NS(type=kind, content=content, heading='', timestamp=time.time() - age,
              no=1, guid='test-log', kvps={}, **kw)


class Context:
    def __init__(self, items, running=False):
        self.id, self.name = CID, 'Isolated regression'
        self.log = NS(_lock=threading.RLock(), logs=items, updates=[0] * 5000, progress='')
        self.paused, self.running = False, running
        self.last_message, self.type = stamp(60), monitor.AgentContextType.USER
        self.messages, self.kills = [], 0

    def is_running(self):
        return self.running

    def communicate(self, msg):
        self.messages.append(msg)

    def nudge(self):
        self.kills += 1


@pytest.fixture
def env(tmp_path, monkeypatch):
    chats = tmp_path / 'chats'
    (chats / CID).mkdir(parents=True)
    monkeypatch.setattr(monitor, '_CHAT_DIR_BASE', str(chats))
    monkeypatch.setattr(monitor, '_debug_log', lambda *a: None)
    monkeypatch.setattr(monitor, '_notify', lambda *a, **kw: True)
    monkeypatch.setattr(monitor, '_SESSION_ID', 'isolated-tests')
    monkeypatch.setattr(monitor, '_TICKS_SINCE_START', 10)
    monkeypatch.setattr(state, 'STATE_FILE', str(tmp_path / 'state.json'))
    monkeypatch.setattr(state, 'JOURNAL_FILE', str(tmp_path / 'history.jsonl'))
    monkeypatch.setattr(state, '_state_sig_cache', None)
    monkeypatch.setattr(state, '_last_saved_sig', '')
    monkeypatch.setattr(monitor.caps_mod, 'external_blocks', lambda: {})
    monitor._chat_listing_cache.clear()
    monitor._error_file_cache.clear()
    monitor._NAME_CACHE.clear()
    ctx = Context([item()])
    monkeypatch.setattr(monitor, 'AgentContext', NS(all=lambda: [ctx]))
    cfg = cd.deep_merge_defaults(dict(startup_grace_ticks=0, nudge_cooldown_minutes=0,
        goal_gate_enabled=False, notify_on_intervention=False, selfcheck_enabled=False,
        max_auto_nudges=2, save_state_interval_seconds=0))
    st = state.default_state()
    st.update(last_tick=stamp(), session_id='isolated-tests')
    state.save_state(st)
    yield ctx, cfg, tmp_path
    monitor._chat_listing_cache.clear()
    monitor._error_file_cache.clear()
    monitor._NAME_CACHE.clear()


@pytest.mark.parametrize('value,expected', [
    (True, cd.DEFAULTS['auto_remediate_categories']), (False, []),
    (None, cd.DEFAULTS['auto_remediate_categories']),
    ('network, network, json_malformed', ['network', 'json_malformed']),
    ([], []), (['network', {'bad': True}, 8], ['network']),
])
def test_category_normalization_at_read_and_save(value, expected):
    assert cd.deep_merge_defaults({'auto_remediate_categories': value})['auto_remediate_categories'] == expected
    assert cd.apply_overrides(cd.deep_merge_defaults({}),
        {'auto_remediate_categories': value})['auto_remediate_categories'] == expected


@pytest.mark.parametrize('message,category', [
    ('APIError: User has no quota left\nTraceback (most recent call last):\nopenai.PermissionDeniedError: User has no quota left', 'rate_limit'),
    ('PermissionDeniedError: User has no quota left', 'rate_limit'),
    ('403: You used all included Pro usage', 'rate_limit'),
    ('401 Unauthorized: quota exhausted', 'auth'),
    ('403 Forbidden: invalid API key', 'auth'),
    ('Infection check: TERMINATED; quota exhausted', 'safety_termination'),
    ('Critical error occurred, retrying... 429', 'provider_exhaustion'),
])
def test_primary_quota_message_beats_wrapper_class(message, category):
    assert diagnose.classify_text(message) == category


def test_capped_update_list_still_recognizes_streaming(env):
    ctx, cfg, _ = env
    ctx.running = True
    ctx.log.logs = [item('agent', 'first chunk')]
    before = monitor._log_snapshot(ctx)
    st = state.load_state()
    state.update_chat(st, CID, status='running', last_log_len=1, last_updates_len=5000,
        last_entry_key=before.entry_key, last_activity_key=before.activity_key,
        log_len_since=stamp(20), wedge_nudge_count=1, last_wedge_nudge_at=stamp(20))
    state.save_state(st)
    ctx.log.logs[0].content = 'a new healthy chunk'
    after = monitor._log_snapshot(ctx)
    assert before.updates == after.updates == 5000
    assert monitor._wedge_minutes(ctx, state.load_state()['chats'][CID], after) == 0
    result = monitor.tick(dict(cfg, wedge_soft_restart=True))
    assert result['running'] == 1 and not ctx.messages and ctx.kills == 0
    saved = state.load_state()['chats'][CID]
    assert saved['liveness'] == 'streaming'
    assert monitor._minutes_since(monitor._parse_dt(saved['log_len_since'])) < 1


def test_actual_framework_log_keeps_activity_after_retention_cap(env):
    from helpers.log import Log, _MAX_RETAINED_UPDATES
    ctx, _, _ = env
    ctx.log = Log()
    entry = ctx.log.log(type='agent', content='before')
    ctx.log.updates[:] = [entry.no] * _MAX_RETAINED_UPDATES
    before = monitor._log_snapshot(ctx)
    entry.update(content='after a real streamed chunk')
    after = monitor._log_snapshot(ctx)
    assert before.updates == after.updates == _MAX_RETAINED_UPDATES
    assert before.entry_key == after.entry_key
    assert before.activity_key != after.activity_key


@pytest.mark.parametrize('value', [float('inf'), float('-inf'), float('nan')])
def test_nonfinite_numeric_settings_keep_their_fallback(value):
    assert cd.coerce_int(value, 10, 0, 120) == 10


def test_trimmed_log_with_new_entry_is_progress(env):
    ctx, cfg, _ = env
    ctx.running = True
    ctx.log.logs = [item('tool', 'first result')]
    before = monitor._log_snapshot(ctx)
    st = state.load_state()
    state.update_chat(st, CID, status='running', last_log_len=1, last_updates_len=5000,
        last_entry_key=before.entry_key, last_activity_key=before.activity_key,
        log_len_since=stamp(20))
    state.save_state(st)
    ctx.log.logs[:] = [item('tool', 'next result', age=0)]
    result = monitor.tick(dict(cfg, wedge_soft_restart=True))
    assert result['running'] == 1 and not ctx.messages and ctx.kills == 0


def test_error_recovery_budget_survives_running_and_restart(env):
    ctx, cfg, _ = env
    assert monitor.tick(cfg)['nudged'] == 1
    assert len(ctx.messages) == 1
    ctx.running = True
    monitor.tick(cfg)
    assert state.load_state()['chats'][CID]['error_recovery_count'] == 1
    ctx.running = False
    assert monitor.tick(cfg)['nudged'] == 1
    assert state.load_state()['chats'][CID]['error_recovery_count'] == 2
    # A fresh process must inherit the spent budget, even with escalation on.
    st = state.load_state()
    st['last_tick'] = stamp(10)
    state.save_state(st)
    monitor._SESSION_ID = 'restarted-test-process'
    result = monitor.tick(dict(cfg, stall_escalate_to_restart=True))
    assert len(ctx.messages) == 2 and ctx.kills == 0
    assert result['intervention'] == 1
    assert 'budget exhausted' in state.load_state()['chats'][CID]['last_classification']
    rows = state.read_journal()
    assert sum(r['action'] == 'error_resume' for r in rows) == 2


def test_recovery_cooldown_and_human_reset(env):
    ctx, cfg, _ = env
    cfg['nudge_cooldown_minutes'] = 10
    monitor.tick(cfg)
    monitor.tick(cfg)
    assert len(ctx.messages) == 1
    ctx.log.logs.append(item('user', 'Please retry after I fixed the connection', age=-1))
    ctx.running = True
    monitor.tick(cfg)
    assert state.load_state()['chats'][CID]['error_recovery_count'] == 0


@pytest.mark.parametrize('message', [
    '401 Unauthorized: invalid API key', 'Infection check: TERMINATED',
    'APIError: User has no quota left', '429 Too many requests',
])
def test_nonrecoverable_failures_are_never_auto_resumed(env, message):
    ctx, cfg, _ = env
    ctx.log.logs = [item(content=message)]
    cfg['auto_remediate_categories'] = ['network', 'auth', 'safety_termination',
        'provider_exhaustion', 'rate_limit', 'tool_error', 'framework_crash']
    monitor.tick(cfg)
    assert ctx.messages == [] and ctx.kills == 0


@pytest.mark.parametrize('option', ['paused', 'task', 'blocked', 'window', 'disabled', 'budget_zero'])
def test_recovery_obeys_operational_gates(env, option):
    ctx, cfg, _ = env
    if option == 'paused':
        ctx.paused = True
    elif option == 'task':
        ctx.type = monitor.AgentContextType.TASK
    elif option == 'blocked':
        st = state.load_state()
        state.update_chat(st, CID, status='blocked', blocked_reason='human review', blocked_at=stamp())
        state.save_state(st)
    elif option == 'window':
        cfg['startup_grace_ticks'] = 20
        monitor._TICKS_SINCE_START = 0
    elif option == 'disabled':
        cfg['auto_remediate_categories'] = []
    else:
        cfg['max_nudges_per_tick'] = 0
    monitor.tick(cfg)
    assert ctx.messages == [] and ctx.kills == 0


def test_old_error_evidence_does_not_override_success(env):
    ctx, cfg, tmp = env
    error_file = tmp / 'chats' / CID / 'error.txt'
    error_file.write_text('TimeoutError: old failure')
    ctx.log.logs.append(item('response', 'Task completed', age=-1))
    monitor.tick(cfg)
    assert state.load_state()['chats'][CID]['status'] == 'awaiting_user'
    assert error_file.read_text() == 'TimeoutError: old failure'
    assert not ctx.messages


@pytest.mark.parametrize('heading', ['Memorizing new information...',
    'Memorizing succesful solutions...', 'Memorizing successful solutions...'])
def test_background_memory_does_not_restart_completed_work(env, heading):
    ctx, cfg, _ = env
    ctx.log.logs = [item('response', 'Task is complete')]
    background = item('util', '', age=300)
    background.heading = heading
    ctx.log.logs.append(background)
    monitor.tick(cfg)
    assert state.load_state()['chats'][CID]['status'] == 'awaiting_user'
    assert not ctx.messages and ctx.kills == 0


def test_supervised_error_draft_spends_recovery_budget_on_send(env, monkeypatch):
    from usr.plugins.chat_shepherd.api import draft
    ctx, cfg, _ = env
    cfg['supervised_mode'] = True
    monitor.tick(cfg)
    saved = state.load_state()
    proposed = saved['drafts'][0]
    assert proposed['kind'] == 'error_resume' and not ctx.messages
    monkeypatch.setattr(draft, '_get_context', lambda _: ctx)
    result = asyncio.run(draft.Draft(None, None).process(
        {'draft_id': proposed['id'], 'action': 'send'}, None))
    assert result['success'] and len(ctx.messages) == 1
    assert state.load_state()['chats'][CID]['error_recovery_count'] == 1


def test_failed_recovery_intent_is_charged_and_not_immediately_replayed(env):
    ctx, cfg, _ = env
    st = state.load_state()
    state.get_chat(st, CID)
    state.begin_inflight(st, 'error_resume', CID)
    st['inflight'][0]['queued_at'] = stamp(10)
    state.save_state(st)
    cfg['nudge_cooldown_minutes'] = 10
    monitor.tick(cfg)
    assert state.load_state()['chats'][CID]['error_recovery_count'] == 1
    assert not ctx.messages


def test_failure_delivery_claim_is_durable_before_send(env):
    ctx, cfg, _ = env
    claims_seen = []
    def fail_send(msg):
        persisted = json.loads(Path(state.STATE_FILE).read_text())
        claims_seen.append(persisted['inflight'])
        raise RuntimeError('delivery unavailable')
    ctx.communicate = fail_send
    monitor.tick(cfg)
    assert claims_seen and claims_seen[0][0]['action'] == 'error_resume'
    assert state.load_state()['inflight']
    # Until reconciliation, no second send should be attempted.
    ctx.communicate = lambda _: pytest.fail('pending intent was replayed')
    monitor.tick(cfg)


def test_historical_error_is_not_resurrected_by_update(env):
    ctx, cfg, _ = env
    ctx.log.logs[0].timestamp = time.time() - 86400
    monitor.tick(cfg)
    assert not ctx.messages


def test_successful_error_resume_is_credited_once_in_dashboard(env):
    from usr.plugins.chat_shepherd.helpers import aggregates
    ctx, cfg, _ = env
    monitor.tick(cfg)
    ctx.log.logs.append(item('response', 'Recovered and completed', age=-1))
    monitor.tick(cfg)
    monitor.tick(cfg)
    saved = state.load_state()['chats'][CID]
    assert saved['error_recovery_count'] == 0 and saved['nudges_effective'] == 1
    metrics = aggregates.compute(state.read_journal())
    assert metrics['stalls']['nudges_sent'] == 1
    assert metrics['stalls']['recovered'] == 1


def test_checkbox_model_is_initialized_as_an_array():
    from html.parser import HTMLParser
    import subprocess
    class Effects(HTMLParser):
        effect = ''
        def handle_starttag(self, tag, attrs):
            effect = dict(attrs).get('x-effect', '')
            if 'auto_remediate_categories' in effect:
                self.effect = effect
    parser = Effects()
    parser.feed((Path(__file__).resolve().parents[1] / 'webui/config.html').read_text())
    assert parser.effect
    code = 'const apply = new Function("config", ' + json.dumps(parser.effect) + ');'
    code += '''
    for (const value of [undefined, true, false, [], ['network']]) {
      const config = {auto_remediate_categories:value}; apply(config);
      if (!Array.isArray(config.auto_remediate_categories)) throw Error('boolean model');
      if (value === false && config.auto_remediate_categories.length) throw Error('false enabled');
      if (Array.isArray(value) && config.auto_remediate_categories !== value) throw Error('selection replaced');
    }
    '''
    subprocess.run(['node', '-e', code], check=True, capture_output=True)
