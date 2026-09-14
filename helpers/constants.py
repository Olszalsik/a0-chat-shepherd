import re

PLUGIN_NAME = 'chat_shepherd'
STATE_FILE = 'usr/plugins/chat_shepherd/data/state.json'
HISTORY_LIMIT = 50

# v1.6.0: append-only history journal (state.json no longer carries history).
JOURNAL_FILE = 'usr/plugins/chat_shepherd/data/history.jsonl'
JOURNAL_MAX_BYTES = 1000000
JOURNAL_KEEP = 1000
JOURNAL_READ_LIMIT = 200
# P1 hard cap: keep only the N most recently ticked chats in state.json.
MAX_TRACKED_CHATS = 100
# P1: framework chat ids are exactly 8 alphanumerics (AgentContext.generate_id).
# Script-created throwaway contexts (e.g. "verify-1788992102", "ctx-hook-1")
# pass an explicit id= and never match this, so they are never tracked.
CHAT_ID_PATTERN = re.compile(r'^[A-Za-z0-9]{8}$')
# R2: a nudge counts as effective when the chat reaches running or
# awaiting_user within this many minutes after the nudge.
NUDGE_EFFECTIVE_WINDOW_MIN = 15

# v1.7.0 quiet bell: the intervention bell rings only after a chat has
# persistently needed human help for NOTIFY_AFTER_MIN minutes; pages
# again every NOTIFY_REARM_MIN while it persists (0 = once per episode).
NOTIFY_AFTER_MIN = 30.0
NOTIFY_REARM_MIN = 60.0

# v1.8.0 goal completion gate: when a USER chat's latest response
# claims the work is done while its native goal-system goal is still
# open, it gets a silent nudge to finish the goal or close it via the
# goal tool. The per-goal budget resets whenever the goal record
# changes (goal_gate_key = goal updated_at).
GOAL_GATE_TEXT = (
    '[chat_shepherd] Your last message indicates the work is finished, but '
    'this chat still has an active goal: "{objective}". If the goal is '
    'fully achieved, call the goal tool with action=complete to close it '
    'out. If it is not fully achieved, continue working on it now.'
)
GOAL_GATE_MAX_NUDGES = 2
GOAL_GATE_COOLDOWN_MIN = 15.0
GOAL_GATE_OBJECTIVE_MAX = 120

STATUS_RUNNING = 'running'
STATUS_STALLED = 'stalled'
STATUS_NUDGED = 'nudged'
STATUS_INTERVENTION = 'intervention'
STATUS_INTERRUPTED = 'interrupted'
STATUS_AWAITING_USER = 'awaiting_user'
STATUS_ERROR = 'error'
STATUS_PAUSED = 'paused'
STATUS_IDLE = 'idle'

NUDGE_TEXT = (
    '[chat_shepherd] You stopped unexpectedly mid-task. '
    'If your work is not finished, continue now from where you left off. '
    'If you are actually done, state clearly that the task is complete.'
)

RESUME_TEXT = (
    '[chat_shepherd] The Agent Zero server was restarted and this chat '
    'was interrupted mid-task. If your work is not finished, continue now '
    'from where you left off. If you are actually done, state clearly '
    'that the task is complete.'
)

# v1.2.0 wedge remediation ladder, Tier 1. A running context whose log is
# frozen for wedge_nudge_after_minutes gets this instead of a human nudge:
# communicate() on an alive task sets agent.intervention (plain attribute
# write), which the agent consumes at its next safe point - the same
# mechanics that make a manual "continue" unwedge these chats.
WEDGE_NUDGE_TEXT = (
    '[chat_shepherd] No output for several minutes - you may be stuck. '
    'If a step is hanging, abandon it and continue with the task. '
    'If the task is actually complete, state that clearly.'
)

# v1.2.0 wedge ladder defaults (see default_config.yaml + api/config.py).
WEDGE_NUDGE_AFTER_MIN = 10
WEDGE_MAX_REMEDIATIONS = 2
WEDGE_REMEDIATION_COOLDOWN_MIN = 10
 # v1.10.0: liveness probe classifies wedges (hung call vs spinning
 # dead loop) via the log mutation counter; off = legacy ladder.
WEDGE_LIVENESS_PROBE = True
