"""chat_shepherd — failure diagnosis: turn a stopped chat into a *cause*.

Ported from the chat_shepherd2 donor module and adapted to this plugin's
v1.21.0 audit contract: a chat's log is read through ONE bounded snapshot per
pass — `monitor._log_snapshot` / `_LogView` (duck-typed here: any object with
a ``tail`` list) — and every scanner takes that view instead of touching
`context.log._lock` itself. Blocking the agent's own log writer is the real
cost; per-scan lock re-acquisition would re-introduce it.

The taxonomy answers "why did it stop?"; the category is what decides
whether remediation may act at all (see planner.may_remediate).

Automation is transparent
-------------------------
Every message this plugin injects is prefixed ``[chat_shepherd]``. Scans
treat such a user entry as automation, not as "a human came back", so this
plugin's own nudges can never clear a fatal verdict and re-arm the actions
that verdict exists to suppress.
"""

from __future__ import annotations

import hashlib
import re
import time as _time
from typing import Any

from usr.plugins.chat_shepherd.helpers.constants import (
    CATEGORY_AUTH,
    CATEGORY_CONTEXT_LENGTH,
    CATEGORY_EXTERNAL_BLOCK,
    CATEGORY_FRAMEWORK_CRASH,
    CATEGORY_JSON_MALFORMED,
    CATEGORY_MODEL_REFUSAL,
    CATEGORY_NETWORK,
    CATEGORY_NONE,
    CATEGORY_PROVIDER_EXHAUSTION,
    CATEGORY_RATE_LIMIT,
    CATEGORY_SAFETY_TERMINATION,
    CATEGORY_TOOL_ERROR,
    CHAT_ID_PATTERN,
    PLUGIN_NAME,
)

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


# Order matters: the first match wins, so the specific causes are tested
# before the catch-all. A safety termination is checked first because a
# termination message often also contains words like "error" or "failed".
#
# v1.23.0: the exhaustion rule is FIRST, ahead of even safety_termination.
# Its text ("all chat model candidates exhausted", "critical error occurred,
# retrying") is far more specific than any other rule, and during a retry
# storm the same tail also carries RateLimitError/429/auth fragments that
# rotated into the newest entry — on the live incident the log_tail cause
# flickered auth <-> framework_crash <-> tool_error every ~15 min as the
# storm rotated which error entry was newest. A sustained loop needs a
# sustained verdict, so the loop's own signature is tested before the
# fragments it contains.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        CATEGORY_PROVIDER_EXHAUSTION,
        re.compile(
            r'all chat model candidates exhausted|candidates exhausted'
            r'|critical error occurred, retrying',
            re.IGNORECASE,
        ),
    ),
    (
        CATEGORY_SAFETY_TERMINATION,
        re.compile(
            r'terminat\w*|terminated|infection check|safety (?:check|stop|kill)'
            r'|blocked by safety|policy violation detected',
            re.IGNORECASE,
        ),
    ),
    (
        CATEGORY_AUTH,
        re.compile(
            r'\b(?:401|403)\b|unauthoriz|unauthenticated|forbidden'
            r'|invalid[\s_-]*api[\s_-]*key|missing[\s_-]*api[\s_-]*key'
            r'|authentication|permission[\s_-]*denied|credential',
            re.IGNORECASE,
        ),
    ),
    (
        CATEGORY_CONTEXT_LENGTH,
        re.compile(
            r'maximum context|context[\s_-]*(?:length|window|size)'
            r'|too many tokens|reduce the length|context limit'
            r'|maximum[\s_-]*tokens|string too long',
            re.IGNORECASE,
        ),
    ),
    (
        CATEGORY_RATE_LIMIT,
        re.compile(
            r'\b429\b|rate[\s_-]*limit|too many requests|quota (?:exceeded|exhausted)'
            r'|overloaded|insufficient_quota|billing',
            re.IGNORECASE,
        ),
    ),
    (
        CATEGORY_MODEL_REFUSAL,
        re.compile(
            r'content[\s_-]*policy|contentpolicyviolation'
            r'|\brefus\w+ (?:to|answer)|i (?:cannot|can\'t) (?:help|assist|comply)',
            re.IGNORECASE,
        ),
    ),
    (
        CATEGORY_JSON_MALFORMED,
        re.compile(
            r'jsondecodeerror|expecting value|expecting property name'
            r'|unterminated string|extra data\b|invalid \\?escape'
            r'|could not parse.*(?:tool|json)',
            re.IGNORECASE,
        ),
    ),
    (
        CATEGORY_NETWORK,
        re.compile(
            r'timeout|timed out|connection (?:error|reset|refused|aborted)'
            r'|sslcert|ssl error|name or service not known|temporary failure'
            r'|network is unreachable|read timed out',
            re.IGNORECASE,
        ),
    ),
    (
        CATEGORY_TOOL_ERROR,
        re.compile(
            r'tool[\s_-]*(?:error|failed|failure)|traceback \(most recent call last\)'
            r'|command not found|no such file or directory|permission denied'
            r'|\bexit(?:ed with)? (?:code|status) [1-9]',
            re.IGNORECASE,
        ),
    ),
)

_FATAL_TYPES = ('error',)
_TERMINATION_HEADING = re.compile(r'terminat', re.IGNORECASE)

# Digits are collapsed before hashing so "attempt 1" and "attempt 2" of the
# same failure share a signature, and volatile ids/timestamps do not fragment
# the counts.
_VOLATILE = re.compile(r'\b[0-9a-f]{8,}\b|\b\d{4}-\d{2}-\d{2}[T ][\d:.+\-Z]+\b|\b\d+\b')

_EXCERPT_MAX = 200
ERROR_HEAD_BYTES = 4096


def classify_text(text: str) -> str:
    """Map free text to a category. `none` when nothing matches."""
    if not text or not str(text).strip():
        return CATEGORY_NONE
    haystack = str(text)
    for category, pattern in _RULES:
        if pattern.search(haystack):
            return category
    return CATEGORY_NONE


def excerpt(text: str, limit: int = _EXCERPT_MAX) -> str:
    compact = ' '.join(str(text or '').split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)] + '...'


def normalize(text: str) -> str:
    """Collapse volatile tokens so equivalent failures share a signature.

    The volatile-token pass runs on case-PRESERVED text and only the result
    is lowercased. Lowercasing first turns the `T` in `2026-01-01T00:00:00`
    into `t`, which the timestamp pattern's `[T ]` class no longer matches —
    and an un-collapsed timestamp makes every retry of the same failure look
    like a new one, so the counts never aggregate.
    """
    collapsed = ' '.join(str(text or '').split())
    return _VOLATILE.sub('#', collapsed)[:600].lower()


def signature(category: str, text: str) -> str:
    blob = category + '|' + normalize(text)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()[:12]


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


# --- view-adapted tail scanners (the O5 contract: pass the _LogView) -------

def last_relevant_type(view: Any) -> str:
    """Newest relevant entry type, newest-first, automation filtered out.

    Unlike monitor._last_log_type (which keeps the plugin's own nudges) the
    release/block work needs the *human* view of the log: a `[chat_shepherd]`
    user entry is automation, never a human turn.
    """
    if view is None:
        return ''
    for item in reversed(getattr(view, 'tail', None) or []):
        kind = entry_type(item)
        if kind in ('response', 'tool', 'user', 'error'):
            if kind == 'user' and is_automation_entry(item):
                continue
            return kind
    return ''


def last_human_user_at(view: Any) -> float:
    """Epoch seconds of the newest genuine human turn, or 0.0.

    A release needs an ORDERING, not a type: the verdict that blocked the chat
    usually came from `error.txt`, which never expires, while the newest user
    entry can be hours old — so "newest entry is a user turn" alone cannot
    release a block. The human turn must be newer than the block (the caller
    compares this against `blocked_at`).
    """
    if view is None:
        return 0.0
    for item in reversed(getattr(view, 'tail', None) or []):
        if entry_type(item) != 'user' or is_automation_entry(item):
            continue
        try:
            return float(getattr(item, 'timestamp', 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


# v1.23.0: the provider-exhaustion retry loop's log signature. The retry
# machinery (plugins/_error_retry) logs a NEW warning/error entry per failed
# attempt — "Critical error occurred, retrying..." heading, "All chat model
# candidates exhausted..." content — so a futile loop ADVANCES the entry
# count and is invisible to the frozen-log wedge detector by construction.
_EXHAUSTION_HEADING_RE = re.compile(
    r'critical error occurred,\s*retrying', re.IGNORECASE
)
_EXHAUSTION_CONTENT_RE = re.compile(
    r'all chat model candidates exhausted|candidates exhausted', re.IGNORECASE
)
_EXHAUSTION_TYPES = ('warning', 'error')


def _is_exhaustion_marker(item: Any) -> bool:
    kind = entry_type(item)
    if kind not in _EXHAUSTION_TYPES:
        return False
    heading = str(getattr(item, 'heading', '') or '')
    content = str(getattr(item, 'content', '') or '')
    return bool(
        _EXHAUSTION_HEADING_RE.search(heading)
        or _EXHAUSTION_CONTENT_RE.search(heading)
        or _EXHAUSTION_CONTENT_RE.search(content)
    )


def exhaustion_loop(view: Any) -> tuple[bool, int, str]:
    """Is the log tail a provider-exhaustion retry loop?

    Returns ``(matched, marker_count, excerpt_of_newest_marker)``. Matching
    needs ALL of:

    - at least two exhaustion markers (warning/error entries) in the bounded
      tail — one entry is one failed attempt, not a loop;
    - the newest *relevant* log entry is not a `response` — a real reply
      after the failures means the loop broke;
    - no genuine human turn newer than the newest marker — a human taking
      over is the loop's owner now.

    Takes the caller's `_LogView` (the O5 contract: never the log lock).
    """
    if view is None:
        return False, 0, ''
    tail = getattr(view, 'tail', None) or []
    newest_marker_ts = 0.0
    count = 0
    newest_text = ''
    for item in tail:
        if not _is_exhaustion_marker(item):
            continue
        count += 1
        newest_text = (
            (getattr(item, 'heading', '') or '') + ' '
            + (getattr(item, 'content', '') or '')
        )
        try:
            newest_marker_ts = max(
                newest_marker_ts, float(getattr(item, 'timestamp', 0.0) or 0.0)
            )
        except (TypeError, ValueError):
            pass
    if count < 2:
        return False, 0, ''
    if last_relevant_type(view) == 'response':
        return False, 0, ''
    human_ts = last_human_user_at(view)
    if human_ts > 0.0 and human_ts >= newest_marker_ts:
        return False, 0, ''
    return True, count, newest_text


def last_response_at(view: Any) -> float:
    """Epoch seconds of the newest assistant `response` entry, or 0.0.

    Shape-mirror of `last_human_user_at`: the callers need an ORDERING
    against state stamps (exhaustion recovery, post-kill hold), not a type.
    """
    if view is None:
        return 0.0
    for item in reversed(getattr(view, 'tail', None) or []):
        if entry_type(item) != 'response':
            continue
        try:
            return float(getattr(item, 'timestamp', 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def fatal_text(view: Any) -> str:
    """Text of the newest fatal-looking entry, or ''.

    Scans the bounded tail backwards and stops at the first genuine human
    turn or completed response: those are the chat moving on, and everything
    older is pre-failure history. Automation entries are skipped rather than
    treated as human traffic, so a nudge cannot push the fatal entry out of
    reach.
    """
    if view is None:
        return ''
    for item in reversed(getattr(view, 'tail', None) or []):
        kind = entry_type(item)
        if kind == 'user':
            if is_automation_entry(item):
                continue
            return ''
        if kind == 'response':
            return ''
        if kind in _FATAL_TYPES:
            return str(getattr(item, 'content', '') or '')
        if kind == 'warning' and _TERMINATION_HEADING.search(
            str(getattr(item, 'heading', '') or '')
        ):
            return str(
                (getattr(item, 'heading', '') or '') + ' '
                + (getattr(item, 'content', '') or '')
            )
    return ''


def read_error_head(abs_path: str) -> str:
    """Bounded head read of the framework crash marker. Never raises."""
    try:
        with open(abs_path, 'rb') as handle:
            return handle.read(ERROR_HEAD_BYTES).decode('utf-8', errors='replace')
    except Exception:
        return ''


def fatal_category(text: str) -> str:
    """The cause of a fatal entry, defaulting to framework_crash.

    `classify_text` returns the string 'none' for no match, and 'none' is
    truthy — so the obvious `classify_text(text) or CATEGORY_FRAMEWORK_CRASH`
    silently yields 'none' and an unrecognised crash is classified as "no
    failure at all". The comparison is written out for that reason.
    """
    found = classify_text(text)
    return CATEGORY_FRAMEWORK_CRASH if found == CATEGORY_NONE else found


def fingerprint(
    chat_id: str,
    *,
    error_text: str = '',
    fatal: str = '',
    external_block: str = '',
) -> dict[str, Any]:
    """Classify why `chat_id` is not progressing.

    Returns a dict with a stable shape so callers never have to guard for a
    missing key: ``{category, signature, source, excerpt, fatal}``.

    Precedence, strongest signal first:
    1. another plugin owns this chat and has blocked it (stage 5 feed);
    2. the framework's ``error.txt`` crash marker;
    3. the newest fatal log entry (the caller slices it from its _LogView).
    """
    result: dict[str, Any] = {
        'category': CATEGORY_NONE,
        'signature': '',
        'source': '',
        'excerpt': '',
        'fatal': False,
    }

    if external_block:
        result.update(
            category=CATEGORY_EXTERNAL_BLOCK,
            signature=signature(CATEGORY_EXTERNAL_BLOCK, external_block),
            source='external',
            excerpt=excerpt(external_block),
        )
        return result

    if not isinstance(chat_id, str) or not CHAT_ID_PATTERN.match(chat_id or ''):
        return result

    if error_text:
        result['fatal'] = True
        result['source'] = 'error.txt'
        result['excerpt'] = excerpt(error_text)
        result['category'] = fatal_category(error_text)
        result['signature'] = signature(result['category'], error_text)
        return result

    fatal = fatal or ''
    if fatal:
        result['fatal'] = True
        result['source'] = 'log_tail'
        result['excerpt'] = excerpt(fatal)
        result['category'] = fatal_category(fatal)
        result['signature'] = signature(result['category'], fatal)
        return result

    return result


def category_changed(previous: str, current: str) -> bool:
    """True when the cause changed — the moment a new verdict is warranted."""
    return (previous or CATEGORY_NONE) != (current or CATEGORY_NONE)