from __future__ import annotations

from typing import Any

from helpers.extension import Extension
from helpers import files, plugins

from usr.plugins.chat_shepherd.helpers.constants import PLUGIN_NAME
from usr.plugins.chat_shepherd.helpers.monitor import tick


class ChatShepherdTick(Extension):
    async def execute(self, **kwargs: Any) -> None:
        cfg = plugins.get_plugin_config(PLUGIN_NAME) or {}
        if not cfg.get("enabled", False):
            return
        try:
            tick(cfg)
        except Exception as e:
            from usr.plugins.chat_shepherd.helpers.monitor import _debug_log
            _debug_log('tick_fail', repr(e))