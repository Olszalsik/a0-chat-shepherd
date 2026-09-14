# chat_shepherd — framework plugin hooks (v1.11.0, roadmap P5/P6.1b).
#
# get_plugin_config: deep-merge the plugin defaults UNDER the winning
# config.json (config.json values always win), so runtime paths (tick
# loop, api handlers, webui) see every default key even when config.json
# is partial; unknown/future keys in config.json are preserved; the
# icons map merges per status (the old wholesale-replace hid icons).
#
# save_plugin_config: preserve unknown keys currently on disk when the
# settings endpoint persists sanitized known keys (P6.1b).
#
# Both hooks fail open: on any error the framework default/settings pass
# through unchanged, and save never returns None (None would skip the
# framework write entirely).

from __future__ import annotations

from typing import Any

from usr.plugins.chat_shepherd.helpers.config_defaults import deep_merge_defaults

_PLUGIN_NAME = "chat_shepherd"


def _read_persisted(project_name: str = "", agent_profile: str = "") -> dict[str, Any]:
    # Monkeypatch point for tests: winning config.json content only.
    from helpers import files, plugins

    entries = plugins.find_plugin_assets(
        plugins.CONFIG_FILE_NAME,
        plugin_name=_PLUGIN_NAME,
        project_name=project_name or "",
        agent_profile=agent_profile or "",
        only_first=True,
    )
    path = entries[0].get("path", "") if entries else ""
    if path and files.exists(path):
        data = files.read_file_json(path)
        if isinstance(data, dict):
            return data
    return {}


def get_plugin_config(default=None, **kwargs):
    try:
        return deep_merge_defaults(default if isinstance(default, dict) else {})
    except Exception:
        return default if isinstance(default, dict) else {}


def save_plugin_config(settings=None, **kwargs):
    try:
        if not isinstance(settings, dict):
            return {}
        try:
            current = _read_persisted(
                project_name=str(kwargs.get("project_name") or ""),
                agent_profile=str(kwargs.get("agent_profile") or ""),
            )
        except Exception:
            current = {}
        out: dict[str, Any] = {}
        out.update(current)
        out.update(settings)
        return out
    except Exception:
        return settings if isinstance(settings, dict) else {}
