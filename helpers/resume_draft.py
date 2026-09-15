# chat_shepherd — supervised resume drafts (v1.16.0).
#
# Pure module: builds the editable resume text that supervised_mode
# queues for human review instead of auto-sending. One draft per chat
# situation kind; the classification reason is embedded so the human
# sees why the chat was flagged. No I/O, no config reads, never raises.

from __future__ import annotations

from typing import Any

from usr.plugins.chat_shepherd.helpers.constants import RESUME_TEXT

KIND_RESTART = 'restart_resume'
KIND_STALL = 'stall_nudge'
_KINDS = (KIND_RESTART, KIND_STALL)

_BASE_TEXTS = {
    KIND_RESTART: (
        '[chat_shepherd] The Agent Zero server was restarted and this chat '
        'was interrupted mid-task. If your work is not finished, continue '
        'now from where you left off. If you are actually done, state '
        'clearly that the task is complete.'
    ),
    KIND_STALL: (
        '[chat_shepherd] You stopped unexpectedly mid-task. If your work '
        'is not finished, continue now from where you left off. If you '
        'are actually done, state clearly that the task is complete.'
    ),
}


def valid_kind(kind: object) -> str:
    k = str(kind or '')
    return k if k in _KINDS else ''


def draft_text(kind: str, reason: str = '') -> str:
    '''Proposed resume message for a chat situation.

    Falls back to the shipped RESUME_TEXT for unknown kinds. The
    classification reason is appended as context for the human (and,
    after editing, for the resumed agent).
    '''
    base = _BASE_TEXTS.get(valid_kind(kind), RESUME_TEXT)
    reason = str(reason or '').strip()
    if reason:
        return f'{base} (situation: {reason})'
    return base
