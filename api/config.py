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
        overrides = input_data or {}
        if not isinstance(overrides, dict):
            return {"ok": False, "error": "body must be a JSON object"}

        # No body keys = pure read
        readable = {k: v for k, v in overrides.items() if k in _KNOWN_KEYS}
        if not readable:
            return {
                "ok": True,
                "success": True,
                "config": _read_disk(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        # Sanitize before persisting (pure logic in helpers/config_defaults.py)
        current = _read_disk()
        current = apply_overrides(current, readable)

        try:
            plugins_helper.save_plugin_config(PLUGIN_NAME, "", "", current)
        except Exception as e:
            return {"ok": False, "success": False, "error": f"save failed: {e}"}

        try:
            plugins_helper.clear_plugin_cache([PLUGIN_NAME])
        except Exception:
            pass

        return {
            "ok": True,
            "success": True,
            "updated": sorted(readable.keys()),
            "config": _read_disk(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
