# chat_shepherd — config read/update endpoint.
#
# Route: POST /api/plugins/chat_shepherd/config
# body {} -> returns current merged config
# body {...overrides} -> persists overrides, then returns merged config
#
# v1.11.0 (roadmap P5/P6.1): defaults + sanitize logic moved to
# helpers/config_defaults.py (shared with hooks.py and tests);
# _read_disk() reads through the framework get_plugin_config so the
# hook's deep-merge applies (defaults UNDER config.json, unknown keys
# and per-status icons preserved) and deep_merge_defaults keeps the
# result complete even before the hook is active.

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from helpers.api import ApiHandler  # type: ignore
from helpers import plugins as plugins_helper  # type: ignore

from usr.plugins.chat_shepherd.helpers.config_defaults import (
    DEFAULTS as _DEFAULTS,
    ICON_STATUSES as _ICON_STATUSES,
    KNOWN_KEYS as _KNOWN_KEYS,
    apply_overrides,
    coerce_bool as _coerce_bool,
    coerce_int as _coerce_int,
    deep_merge_defaults,
    prune_to_explicit,
)

PLUGIN_NAME = "chat_shepherd"



def _read_disk() -> dict[str, Any]:
    try:
        cfg = plugins_helper.get_plugin_config(PLUGIN_NAME)
    except Exception:
        cfg = None
    return deep_merge_defaults(cfg)


class Config(ApiHandler):
    async def process(self, input_data: dict, request: Any) -> dict:
        # v1.20.5: validate the RAW body. The old `overrides = input_data or {}`
        # turned any falsy non-dict (0, "", []) into an empty dict, so a
        # malformed body was answered as a legitimate pure read.
        if input_data is None:
            input_data = {}
        if not isinstance(input_data, dict):
            return {
                "ok": False,
                # v1.20.5: every error path carries BOTH keys. The old
                # "body must be a JSON object" reply was the one shape without
                # `success`, and config.html gates on `r.success && r.ok` - it
                # only behaved because undefined is falsy.
                "success": False,
                "error": "body must be a JSON object",
            }
        overrides = input_data

        # v1.20.5: separate KNOWN keys from rejected ones. The endpoint used to
        # drop them silently and still answer ok/success, so a typo (or a
        # third-party client sending an unknown field) looked like a clean save
        # while persisting nothing at all.
        readable = {k: v for k, v in overrides.items() if k in _KNOWN_KEYS}
        rejected = sorted(k for k in overrides if k not in _KNOWN_KEYS)

        # No body keys = pure read
        if not readable:
            body = {
                "ok": True,
                "success": True,
                "config": await asyncio.to_thread(_read_disk),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if rejected:
                # An all-unknown body is NOT a read; say so instead of
                # returning a success that changed nothing.
                body["ok"] = False
                body["success"] = False
                body["rejected"] = rejected
                body["error"] = "unknown config keys: " + ", ".join(rejected)
            return body

        # Sanitize before persisting (pure logic in helpers/config_defaults.py).
        # v1.20.5: offloaded - get_plugin_config can touch disk, and this is a
        # request handler on the event loop (v1.18.1 contract).
        current = await asyncio.to_thread(_read_disk)
        current = apply_overrides(current, readable)
        # v1.20.6: write only the values the operator actually set. Persisting
        # the merged 33-key view pinned every shipped default into the file, so
        # a later release's new defaults could never reach this install.
        # hooks.save_plugin_config prunes too (it is the path the framework
        # settings modal takes); pruning here keeps the contract even if the
        # hook is inactive. prune_to_explicit is idempotent.
        to_save = prune_to_explicit(current)

        try:
            plugins_helper.save_plugin_config(PLUGIN_NAME, "", "", to_save)
        except Exception as e:
            return {"ok": False, "success": False, "error": f"save failed: {e}"}

        try:
            plugins_helper.clear_plugin_cache([PLUGIN_NAME])
        except Exception:
            pass

        result = {
            "ok": True,
            "success": True,
            "updated": sorted(readable.keys()),
            "config": await asyncio.to_thread(_read_disk),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if rejected:
            # Partial save: the known keys landed, the rest did not. Report
            # both so the caller is never left guessing.
            result["rejected"] = rejected
        return result
