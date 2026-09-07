PLUGIN_NAME = 'chat_shepherd'
STATE_FILE = 'usr/plugins/chat_shepherd/data/state.json'
HISTORY_LIMIT = 50

STATUS_RUNNING = 'running'
STATUS_STALLED = 'stalled'
STATUS_NUDGED = 'nudged'
STATUS_INTERVENTION = 'intervention'
STATUS_AWAITING_USER = 'awaiting_user'
STATUS_ERROR = 'error'
STATUS_PAUSED = 'paused'
STATUS_IDLE = 'idle'

NUDGE_TEXT = (
    '[chat_shepherd] You stopped unexpectedly mid-task. '
    'If your work is not finished, continue now from where you left off. '
    'If you are actually done, state clearly that the task is complete.'
)
