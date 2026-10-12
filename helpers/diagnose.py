"""chat_shepherd — failure diagnosis, stage 1: wedged completions.

Ported from the chat_shepherd2 donor module (2026-10-09 fix, "awaiting_user
vs. a turn that never completed"), adapted to this plugin's v1.21.0 audit
contract: a chat's log is read through ONE bounded snapshot per pass —
`monitor._log_snapshot` / `_LogView` — and every scanner takes that view
instead of touching `context.log._lock` itself. Blocking the agent's own
log writer is the real cost, and the donor's per-scan lock re-acquisition
would have re-introduced it.

Stage 1 carries only what the classifier needs; the full cause taxonomy
(`classify_text`, fingerprints, `fatal_text`, ...) arrives with the
cause-gated remediation stage.

Automation is transparent
-------------------------
Every message this plugin injects is prefixed ``[chat_shepherd]``. Scans
treat such a user entry as automation, not as "a human came back", so this
plugin's own nudges can never clear a fatal verdict and re-arm the actions
that verdict exists to suppress.
"""

from __future__ import annotations

import re
import time as _time
from typing import Any

from usr.plugins.chat_shepherd.helpers.constants import PLUGIN_NAME

_AUTOMATION = re.compile(r'^\s*\[' + re.escape(PLUGIN_NAME) + r'\]')


def is_automation_entry(item: Any) -> bool:
    """True when a log entry was written by a supervisor, not a human.

    Recognised by the ``[chat_shepherd]`` prefix the plugin stamps on its
    own messages. This is what stops a nudge from looking like a human turn
    and clearing a fatal verdict.
    """
    content = str(getattr(item, 'content', '') or '')
    return bool(_AUTOMATION.match(content))


def entry_type(item: Any) -> str:
    return str(getattr(item, 'type', '') or '')


# The memory plugin's background housekeeping logs a `util` entry when a task
# (memory recall, memorization) STARTS and updates the same LogItem in place
# when the task finishes. A wedged utility call therefore leaves one of these
# start markers as the newest log entry with empty content, forever — that is
# the shape of every "the response rendered, the spinner never stopped"
# failure. Matching the headings couples this plugin to the memory plugin's
# labels, which is the accepted cost: the alternative — any empty `util` as
# newest entry — also matches benign terminal labels like "Memory recall
# timed out", whose empty content is a completed outcome, and would flag
# every chat whose last housekeeping entry happened to carry no content.
_INFLIGHT_HOUSEKEEPING = re.compile(
    r'^(?:Preloading knowledge|Searching memories'
    r'|Memorizing (?:new information|succesful solutions|successful solutions))'
)


def pending_housekeeping(view: Any, now: float | None = None) -> tuple[str, float]:
    """Heading and age of an in-flight housekeeping marker, newest in the log.

    Takes an already-sliced `_LogView` (the v1.21.0 O5 contract): when the
    caller has no snapshot, it takes one and passes it in — this function
    itself never touches the agent's log lock.

    Returns ``('', 0.0)`` when the tail does not end in an in-flight marker
    (or the view is empty/unknown). The age is how long ago the marker was
    logged — i.e. how long the corresponding call has been missing its
    completion update — so a call that finished between two ticks (a result
    entry follows) stops matching the moment its result lands.
    """
    if view is None:
        return '', 0.0
    tail = getattr(view, 'tail', None) or []
    if not tail:
        return '', 0.0
    item = tail[-1]
    if entry_type(item) != 'util':
        return '', 0.0
    heading = str(getattr(item, 'heading', '') or '')
    content = str(getattr(item, 'content', '') or '')
    if not _INFLIGHT_HOUSEKEEPING.match(heading) or content.strip():
        return '', 0.0
    try:
        ts = float(getattr(item, 'timestamp', 0.0) or 0.0)
    except (TypeError, ValueError):
        return '', 0.0
    if ts <= 0.0:
        return '', 0.0
    reference = _time.time() if now is None else now
    return heading, max(0.0, reference - ts)