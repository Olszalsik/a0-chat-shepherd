# chat_shepherd — config read/update endpoint.
#
# Route: POST /api/plugins/chat_shepherd/config
#   body {}              -> returns current merged config
#   body {...overrides}  -> persists overrides, then returns merged config

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from helpers.api import ApiHandler  # type: ignore
from helpers import plugins as plugins_helper  # type: ignore
from helpers import files  # type: ignore

import json
import os

PLUGIN_NAME = "chat_shepherd"

_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "watch_all": True,
    "stall_minutes": 5,
    "max_auto_nudges": 3,
    "max_nudges_per_tick": 1,
    "nudge_cooldown_minutes": 10,
    "poll_seconds": 5,
    "hot_reload_enabled": True,
 "allowed_chat_ids": [],
    "intervention_after_failed_nudges": True,
    "notify_on_intervention": True,
    "notify_after_minutes": 30,
    "notify_rearm_minutes": 60,
    "notify_on_resume": False,
    "goal_gate_enabled": True,
    "goal_gate_max_nudges": 2,
    "goal_gate_cooldown_minutes": 15,
    "wedge_nudge_after_minutes": 10,
    "wedge_max_remediations": 2,
    "wedge_remediation_cooldown_minutes": 10,
    "wedge_soft_restart": False,
    "webhook_url": "",
    "webhook_allow_private": False,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "icons": {
        "running": "🏃",
        "stalled": "⚠️",
        "nudged": "🔄",
        "intervention": "🚨",
        "interrupted": "🔌",
        "awaiting_user": "💬",
        "error": "❌",
        "paused": "⏸️",
        "idle": "",
    },
}

_KNOWN_KEYS = set(_DEFAULTS.keys())
_ICON_STATUSES = set(_DEFAULTS["icons"].keys())


def _config_path() -> str:
    return files.get_abs_path(files.USER_DIR, "plugins", PLUGIN_NAME, "config.json")


def _read_disk() -> dict[str, Any]:
    merged = dict(_DEFAULTS)
    try:
        path = _config_path()
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                persisted = json.load(f)
            if isinstance(persisted, dict):
                for k, v in persisted.items():
                    if k in _KNOWN_KEYS:
                        merged[k] = v
    except Exception:
        pass
    return merged


def _coerce_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return bool(val)


def _coerce_int(val: Any, fallback: int, lo: int, hi: int) -> int:
    try:
        n = int(val)
    except (TypeError, ValueError):
        return fallback
    return max(lo, min(hi, n))


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

        # Sanitize before persisting
        current = _read_disk()
        if "enabled" in readable:
            current["enabled"] = _coerce_bool(readable["enabled"])
        if "watch_all" in readable:
            current["watch_all"] = _coerce_bool(readable["watch_all"])
        if "intervention_after_failed_nudges" in readable:
            current["intervention_after_failed_nudges"] = _coerce_bool(
                readable["intervention_after_failed_nudges"]
            )
        if "notify_on_intervention" in readable:
            current["notify_on_intervention"] = _coerce_bool(
                readable["notify_on_intervention"]
            )
            if "notify_on_resume" in readable:
                current["notify_on_resume"] = _coerce_bool(readable["notify_on_resume"])
                if "goal_gate_enabled" in readable:
                 current["goal_gate_enabled"] = _coerce_bool(readable["goal_gate_enabled"])
                if "hot_reload_enabled" in readable:
                 current["hot_reload_enabled"] = _coerce_bool(
                     readable["hot_reload_enabled"]
                 )
                if "goal_gate_max_nudges" in readable:
                 current["goal_gate_max_nudges"] = _coerce_int(
                 readable["goal_gate_max_nudges"],
                 current["goal_gate_max_nudges"],
                 0,
                 10,
                 )
                if "goal_gate_cooldown_minutes" in readable:
                 current["goal_gate_cooldown_minutes"] = _coerce_int(
                 readable["goal_gate_cooldown_minutes"],
                 current["goal_gate_cooldown_minutes"],
                 0,
                 240,
                 )
            if "notify_after_minutes" in readable:
                current["notify_after_minutes"] = _coerce_int(
                    readable["notify_after_minutes"],
                    current["notify_after_minutes"],
                    0,
                    720,
                )
            if "notify_rearm_minutes" in readable:
                current["notify_rearm_minutes"] = _coerce_int(
                    readable["notify_rearm_minutes"],
                    current["notify_rearm_minutes"],
                    0,
                    720,
                )
        if "wedge_soft_restart" in readable:
            current["wedge_soft_restart"] = _coerce_bool(
                readable["wedge_soft_restart"]
            )
        if "stall_minutes" in readable:
            current["stall_minutes"] = _coerce_int(
                readable["stall_minutes"], current["stall_minutes"], 1, 240
            )
        if "max_auto_nudges" in readable:
            current["max_auto_nudges"] = _coerce_int(
                readable["max_auto_nudges"], current["max_auto_nudges"], 0, 20
            )
        if "max_nudges_per_tick" in readable:
            current["max_nudges_per_tick"] = _coerce_int(
                readable["max_nudges_per_tick"],
                current["max_nudges_per_tick"],
                0,
                10,
            )
        if "nudge_cooldown_minutes" in readable:
            current["nudge_cooldown_minutes"] = _coerce_int(
                readable["nudge_cooldown_minutes"],
                current["nudge_cooldown_minutes"],
                1,
                240,
            )
        if "poll_seconds" in readable:
            current["poll_seconds"] = _coerce_int(
                readable["poll_seconds"],
                current["poll_seconds"],
                2,
                120,
            )
        if "wedge_nudge_after_minutes" in readable:
            current["wedge_nudge_after_minutes"] = _coerce_int(
                readable["wedge_nudge_after_minutes"],
                current["wedge_nudge_after_minutes"],
                2,
                240,
            )
        if "wedge_max_remediations" in readable:
            current["wedge_max_remediations"] = _coerce_int(
                readable["wedge_max_remediations"],
                current["wedge_max_remediations"],
                0,
                5,
            )
        if "wedge_remediation_cooldown_minutes" in readable:
            current["wedge_remediation_cooldown_minutes"] = _coerce_int(
                readable["wedge_remediation_cooldown_minutes"],
                current["wedge_remediation_cooldown_minutes"],
                1,
                240,
            )
        if "allowed_chat_ids" in readable:
            raw = readable["allowed_chat_ids"]
            if isinstance(raw, str):
                raw = raw.split(",")
            cleaned: list[str] = []
            if isinstance(raw, list):
                for item in raw:
                    s = str(item).strip()
                    if s and len(s) <= 64 and s not in cleaned:
                        cleaned.append(s)
            current["allowed_chat_ids"] = cleaned[:200]

        if "webhook_url" in readable:
            current["webhook_url"] = str(readable["webhook_url"]).strip()[:500]
        if "webhook_allow_private" in readable:
            current["webhook_allow_private"] = _coerce_bool(readable["webhook_allow_private"])
        if "telegram_bot_token" in readable:
            current["telegram_bot_token"] = str(readable["telegram_bot_token"]).strip()[:200]
        if "telegram_chat_id" in readable:
            current["telegram_chat_id"] = str(readable["telegram_chat_id"]).strip()[:64]
        
        if "icons" in readable:
            icons_in = readable["icons"]
            if isinstance(icons_in, dict):
                merged_icons = dict(current.get("icons") or _DEFAULTS["icons"])
                for key, value in icons_in.items():
                    if key in _ICON_STATUSES:
                        merged_icons[key] = str(value)[:16]
                current["icons"] = merged_icons

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
