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
    "diagnose_enabled": True,
    "selfcheck_enabled": True,
    "selfcheck_every_minutes": 15,
    "stale_tick_minutes": 10,
    "notify_after_consecutive_failures": 3,
    "notify_on_selfcheck": True,
    "notify_degraded_after_failures": 3,
    "inflight_enabled": True,
    "inflight_timeout_minutes": 5,
    "startup_grace_ticks": 3,
    "active_hours_enabled": False,
    "active_hours_start": "09:00",
    "active_hours_end": "22:00",
    "active_hours_timezone": "",
    "goal_integration": True,
    "external_block_integration": True,
    "continuity_enabled": False,
    "continuity_every_minutes": 30,
    "continuity_max_wakes_per_day": 12,
    "continuity_isolated": True,
    "continuity_ack_max_chars": 300,
    "continuity_prompt": "",
    "continuity_on_goal": True,
    "continuity_on_nudge_exhaustion": True,
    "continuity_on_intervention": True,
    "continuity_intervention_minutes": 30,
    "continuity_always": False,
    "stall_minutes": 5,
    "max_auto_nudges": 3,
    "max_nudges_per_tick": 1,
    "nudge_cooldown_minutes": 10,
    "poll_seconds": 5,
    "save_state_interval_seconds": 120,
    "hot_reload_enabled": True,
    "allowed_chat_ids": [],
    "auto_remediate_categories": [
        "tool_error",
        "framework_crash",
        "network",
        "json_malformed",
    ],
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
    # v1.23.0: provider-exhaustion retry loops + substantive-progress liveness.
    "exhaustion_minutes": 20,
    "exhaustion_kill_after_minutes": 0,
    "no_response_minutes": 0,
    "stall_escalate_to_restart": False,
    # v1.24.1: rate-limited soft-dep probing feeding the dashboard widget
    # and the self-check (stage-5 capability registry, live-wired).
    "capabilities_probe_enabled": False,
    "adaptive_thresholds": False,
    "adaptive_interval_minutes": 60,
    "adaptive_min_samples": 10,
    "adaptive_step_pct": 10,
    "supervised_mode": False,
    "webhook_url": "",
    "webhook_allow_private": False,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "icons": {
        "running": "🏃",
        "stalled": "⚠️",
        "nudged": "🔄",
        "intervention": "🚨",
        "blocked": "⛔",
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


def prune_to_explicit(merged: dict[str, Any]) -> dict[str, Any]:
    # v1.20.6: reduce a FULLY MERGED config back to the values the operator
    # actually set.
    #
    # The save paths used to persist all 33 keys, which silently PINNED every
    # shipped default into the user's file: a later release that changed a
    # default could never reach an existing install, because the file's stale
    # copy always won over DEFAULTS in deep_merge_defaults. That is why
    # config.json on disk was "fully merged 33/33" - not a correctness feature
    # (both read paths already apply deep_merge_defaults independently and
    # agree either way), just a side effect of saving the merged view.
    #
    # Rules: keep a known key only when its value differs from DEFAULTS, and
    # keep EVERY unknown key untouched (a newer/other plugin version may own
    # it - dropping those would destroy configuration we do not understand).
    # For a dict value (icons) keep only the sub-keys the user changed.
    # Prune is idempotent: prune(prune(x)) == prune(x).
    out: dict[str, Any] = {}
    if not isinstance(merged, dict):
        return out
    for key, val in merged.items():
        if key not in DEFAULTS:
            out[key] = val
            continue
        default = DEFAULTS[key]
        if isinstance(default, dict) and isinstance(val, dict):
            child = {ck: cv for ck, cv in val.items() if default.get(ck) != cv}
            if child:
                out[key] = child
            continue
        if val != default:
            out[key] = val
    return out


def apply_overrides(current: dict[str, Any], readable: dict[str, Any]) -> dict[str, Any]:
    # Sanitize readable overrides onto current. Flat top-level ifs only:
    # a partial save must never touch keys it does not mention (the P6.1
    # mis-nesting hazard, regression-guarded by TEST13A).
    if "enabled" in readable:
        current["enabled"] = coerce_bool(readable["enabled"])
    if "watch_all" in readable:
        current["watch_all"] = coerce_bool(readable["watch_all"])
    if "diagnose_enabled" in readable:
        current["diagnose_enabled"] = coerce_bool(readable["diagnose_enabled"])
    if "selfcheck_enabled" in readable:
        current["selfcheck_enabled"] = coerce_bool(readable["selfcheck_enabled"])
    if "notify_on_selfcheck" in readable:
        current["notify_on_selfcheck"] = coerce_bool(readable["notify_on_selfcheck"])
    if "selfcheck_every_minutes" in readable:
        current["selfcheck_every_minutes"] = coerce_int(
            readable["selfcheck_every_minutes"],
            current["selfcheck_every_minutes"], 0, 1440,
        )
    if "stale_tick_minutes" in readable:
        current["stale_tick_minutes"] = coerce_int(
            readable["stale_tick_minutes"],
            current["stale_tick_minutes"], 2, 240,
        )
    if "notify_after_consecutive_failures" in readable:
        current["notify_after_consecutive_failures"] = coerce_int(
            readable["notify_after_consecutive_failures"],
            current["notify_after_consecutive_failures"], 1, 20,
        )
    if "inflight_enabled" in readable:
        current["inflight_enabled"] = coerce_bool(readable["inflight_enabled"])
    if "startup_grace_ticks" in readable:
        current["startup_grace_ticks"] = coerce_int(
            readable["startup_grace_ticks"], current["startup_grace_ticks"], 0, 20)
    if "continuity_enabled" in readable:
        current["continuity_enabled"] = coerce_bool(readable["continuity_enabled"])
    if "continuity_always" in readable:
        current["continuity_always"] = coerce_bool(readable["continuity_always"])
    if "continuity_isolated" in readable:
        current["continuity_isolated"] = coerce_bool(readable["continuity_isolated"])
    if "continuity_on_goal" in readable:
        current["continuity_on_goal"] = coerce_bool(readable["continuity_on_goal"])
    if "continuity_on_nudge_exhaustion" in readable:
        current["continuity_on_nudge_exhaustion"] = coerce_bool(
            readable["continuity_on_nudge_exhaustion"])
    if "continuity_on_intervention" in readable:
        current["continuity_on_intervention"] = coerce_bool(
            readable["continuity_on_intervention"])
    if "active_hours_enabled" in readable:
        current["active_hours_enabled"] = coerce_bool(readable["active_hours_enabled"])
    if "goal_integration" in readable:
        current["goal_integration"] = coerce_bool(readable["goal_integration"])
    if "external_block_integration" in readable:
        current["external_block_integration"] = coerce_bool(
            readable["external_block_integration"])
    if "continuity_every_minutes" in readable:
        current["continuity_every_minutes"] = coerce_int(
            readable["continuity_every_minutes"],
            current["continuity_every_minutes"], 1, 1440)
    if "continuity_max_wakes_per_day" in readable:
        current["continuity_max_wakes_per_day"] = coerce_int(
            readable["continuity_max_wakes_per_day"],
            current["continuity_max_wakes_per_day"], 0, 200)
    if "continuity_ack_max_chars" in readable:
        current["continuity_ack_max_chars"] = coerce_int(
            readable["continuity_ack_max_chars"],
            current["continuity_ack_max_chars"], 100, 2000)
    if "continuity_intervention_minutes" in readable:
        current["continuity_intervention_minutes"] = coerce_int(
            readable["continuity_intervention_minutes"],
            current["continuity_intervention_minutes"], 1, 720)
    if "continuity_prompt" in readable:
        current["continuity_prompt"] = str(readable["continuity_prompt"] or "")[:4000]
    if "active_hours_start" in readable:
        current["active_hours_start"] = str(readable["active_hours_start"] or "").strip()[:5]
    if "active_hours_end" in readable:
        current["active_hours_end"] = str(readable["active_hours_end"] or "").strip()[:5]
    if "active_hours_timezone" in readable:
        current["active_hours_timezone"] = str(readable["active_hours_timezone"] or "").strip()[:40]
    if "inflight_timeout_minutes" in readable:
        current["inflight_timeout_minutes"] = coerce_int(
            readable["inflight_timeout_minutes"],
            current["inflight_timeout_minutes"], 1, 120,
        )
    if "notify_degraded_after_failures" in readable:
        current["notify_degraded_after_failures"] = coerce_int(
            readable["notify_degraded_after_failures"],
            current["notify_degraded_after_failures"], 1, 20,
        )
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
    if "adaptive_thresholds" in readable:
        current["adaptive_thresholds"] = coerce_bool(readable["adaptive_thresholds"])
    if "supervised_mode" in readable:
        current["supervised_mode"] = coerce_bool(readable["supervised_mode"])
    if "adaptive_interval_minutes" in readable:
        current["adaptive_interval_minutes"] = coerce_int(
            readable["adaptive_interval_minutes"],
            current["adaptive_interval_minutes"],
            15,
            1440,
        )
    if "adaptive_min_samples" in readable:
        current["adaptive_min_samples"] = coerce_int(
            readable["adaptive_min_samples"],
            current["adaptive_min_samples"],
            5,
            100,
        )
    if "adaptive_step_pct" in readable:
        current["adaptive_step_pct"] = coerce_int(
            readable["adaptive_step_pct"],
            current["adaptive_step_pct"],
            5,
            25,
        )
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
    if "save_state_interval_seconds" in readable:
        current["save_state_interval_seconds"] = coerce_int(
            readable["save_state_interval_seconds"],
            current["save_state_interval_seconds"],
            0,
            180,
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
    # v1.23.0: exhaustion + substantive-progress knobs. kill 0 = off.
    if "exhaustion_minutes" in readable:
        current["exhaustion_minutes"] = coerce_int(
            readable["exhaustion_minutes"],
            current["exhaustion_minutes"],
            2,
            240,
        )
    if "exhaustion_kill_after_minutes" in readable:
        current["exhaustion_kill_after_minutes"] = coerce_int(
            readable["exhaustion_kill_after_minutes"],
            current["exhaustion_kill_after_minutes"],
            0,
            720,
        )
    if "no_response_minutes" in readable:
        current["no_response_minutes"] = coerce_int(
            readable["no_response_minutes"],
            current["no_response_minutes"],
            0,
            240,
        )
    if "stall_escalate_to_restart" in readable:
        current["stall_escalate_to_restart"] = coerce_bool(
            readable["stall_escalate_to_restart"]
        )
    if "capabilities_probe_enabled" in readable:
        current["capabilities_probe_enabled"] = coerce_bool(
            readable["capabilities_probe_enabled"]
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
    if "auto_remediate_categories" in readable:
        # v1.22.0 (stage 2): the cause allowlist. Unknown names are REPORTED
        # back as `rejected` by the save paths, never silently dropped here
        # (same contract as unknown top-level keys). NEVER_REMEDIATE members
        # remain accepted config values but are inert: planner.may_remediate
        # applies the floor after this allowlist.
        raw_cats = readable["auto_remediate_categories"]
        if isinstance(raw_cats, str):
            raw_cats = raw_cats.split(",")
        if isinstance(raw_cats, list):
            cats: list[str] = []
            for item in raw_cats:
                s = str(item).strip()
                if s and s not in cats:
                    cats.append(s)
            current["auto_remediate_categories"] = cats[:20]
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
