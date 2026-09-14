# chat_shepherd — single source of truth for config defaults and the pure
# sanitize logic, shared by api/config.py, hooks.py and the tests.
#
# v1.11.0 (roadmap P5/P6.1): extracted from api/config.py so the
# get_plugin_config deep-merge hook and the settings endpoint cannot
# drift apart. Pure data + pure functions: no framework imports.

from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, Any] = {
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
    "wedge_liveness_probe": True,
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

KNOWN_KEYS = set(DEFAULTS.keys())
ICON_STATUSES = set(DEFAULTS["icons"].keys())


def coerce_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return bool(val)


def coerce_int(val: Any, fallback: int, lo: int, hi: int) -> int:
    try:
        n = int(val)
    except (TypeError, ValueError):
        return fallback
    return max(lo, min(hi, n))


def _copy_default(v: Any) -> Any:
    if isinstance(v, dict):
        return dict(v)
    if isinstance(v, list):
        return list(v)
    return v


def deep_merge_defaults(overlay: Any) -> dict[str, Any]:
    # Defaults UNDER the overlay: overlay values win, unknown overlay keys
    # are preserved (P6.1b read side), dict values (icons) merge per key
    # (the old wholesale-replace hid missing status icons).
    merged = {k: _copy_default(v) for k, v in DEFAULTS.items()}
    if not isinstance(overlay, dict):
        return merged
    for k, v in overlay.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            child = dict(merged[k])
            child.update(v)
            merged[k] = child
        else:
            merged[k] = _copy_default(v) if isinstance(v, (dict, list)) else v
    return merged


def apply_overrides(current: dict[str, Any], readable: dict[str, Any]) -> dict[str, Any]:
    # Sanitize readable overrides onto current. Flat top-level ifs only:
    # a partial save must never touch keys it does not mention (the P6.1
    # mis-nesting hazard, regression-guarded by TEST13A).
    if "enabled" in readable:
        current["enabled"] = coerce_bool(readable["enabled"])
    if "watch_all" in readable:
        current["watch_all"] = coerce_bool(readable["watch_all"])
    if "intervention_after_failed_nudges" in readable:
        current["intervention_after_failed_nudges"] = coerce_bool(
            readable["intervention_after_failed_nudges"]
        )
    if "notify_on_intervention" in readable:
        current["notify_on_intervention"] = coerce_bool(
            readable["notify_on_intervention"]
        )
    if "notify_on_resume" in readable:
        current["notify_on_resume"] = coerce_bool(readable["notify_on_resume"])
    if "goal_gate_enabled" in readable:
        current["goal_gate_enabled"] = coerce_bool(readable["goal_gate_enabled"])
    if "hot_reload_enabled" in readable:
        current["hot_reload_enabled"] = coerce_bool(readable["hot_reload_enabled"])
    if "goal_gate_max_nudges" in readable:
        current["goal_gate_max_nudges"] = coerce_int(
            readable["goal_gate_max_nudges"], current["goal_gate_max_nudges"], 0, 10
        )
    if "goal_gate_cooldown_minutes" in readable:
        current["goal_gate_cooldown_minutes"] = coerce_int(
            readable["goal_gate_cooldown_minutes"],
            current["goal_gate_cooldown_minutes"],
            0,
            240,
        )
    if "notify_after_minutes" in readable:
        current["notify_after_minutes"] = coerce_int(
            readable["notify_after_minutes"], current["notify_after_minutes"], 0, 720
        )
    if "notify_rearm_minutes" in readable:
        current["notify_rearm_minutes"] = coerce_int(
            readable["notify_rearm_minutes"], current["notify_rearm_minutes"], 0, 720
        )
    if "wedge_soft_restart" in readable:
        current["wedge_soft_restart"] = coerce_bool(readable["wedge_soft_restart"])
    if "wedge_liveness_probe" in readable:
        current["wedge_liveness_probe"] = coerce_bool(readable["wedge_liveness_probe"])
    if "stall_minutes" in readable:
        current["stall_minutes"] = coerce_int(
            readable["stall_minutes"], current["stall_minutes"], 1, 240
        )
    if "max_auto_nudges" in readable:
        current["max_auto_nudges"] = coerce_int(
            readable["max_auto_nudges"], current["max_auto_nudges"], 0, 20
        )
    if "max_nudges_per_tick" in readable:
        current["max_nudges_per_tick"] = coerce_int(
            readable["max_nudges_per_tick"], current["max_nudges_per_tick"], 0, 10
        )
    if "nudge_cooldown_minutes" in readable:
        current["nudge_cooldown_minutes"] = coerce_int(
            readable["nudge_cooldown_minutes"],
            current["nudge_cooldown_minutes"],
            1,
            240,
        )
    if "poll_seconds" in readable:
        current["poll_seconds"] = coerce_int(
            readable["poll_seconds"], current["poll_seconds"], 2, 120
        )
    if "wedge_nudge_after_minutes" in readable:
        current["wedge_nudge_after_minutes"] = coerce_int(
            readable["wedge_nudge_after_minutes"],
            current["wedge_nudge_after_minutes"],
            2,
            240,
        )
    if "wedge_max_remediations" in readable:
        current["wedge_max_remediations"] = coerce_int(
            readable["wedge_max_remediations"],
            current["wedge_max_remediations"],
            0,
            5,
        )
    if "wedge_remediation_cooldown_minutes" in readable:
        current["wedge_remediation_cooldown_minutes"] = coerce_int(
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
        current["webhook_allow_private"] = coerce_bool(readable["webhook_allow_private"])
    if "telegram_bot_token" in readable:
        current["telegram_bot_token"] = str(readable["telegram_bot_token"]).strip()[:200]
    if "telegram_chat_id" in readable:
        current["telegram_chat_id"] = str(readable["telegram_chat_id"]).strip()[:64]
    if "icons" in readable:
        icons_in = readable["icons"]
        if isinstance(icons_in, dict):
            merged_icons = dict(current.get("icons") or DEFAULTS["icons"])
            for key, value in icons_in.items():
                if key in ICON_STATUSES:
                    merged_icons[key] = str(value)[:16]
            current["icons"] = merged_icons
    return current
