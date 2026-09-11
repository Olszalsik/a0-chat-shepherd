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
            try:
                from datetime import datetime as _dt
                dbg = files.get_abs_path('usr/plugins/chat_shepherd/data/tick_debug.log')
                with open(dbg, 'a', encoding='utf-8') as f:
                    f.write(_dt.now().isoformat() + ' tick error: ' + repr(e) + chr(10))
            except Exception:
                pass