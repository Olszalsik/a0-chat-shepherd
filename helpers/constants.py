import re

PLUGIN_NAME = 'chat_shepherd'
STATE_FILE = 'usr/plugins/chat_shepherd/data/state.json'
HISTORY_LIMIT = 50
# P1 hard cap: keep only the N most recently ticked chats in state.json.
MAX_TRACKED_CHATS = 100
# P1: framework chat ids are exactly 8 alphanumerics (AgentContext.generate_id).
# Script-created throwaway contexts (e.g. "verify-1788992102", "ctx-hook-1")
# pass an explicit id= and never match this, so they are never tracked.
CHAT_ID_PATTERN = re.compile(r'^[A-Za-z0-9]{8}$')

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
