from __future__ import annotations

from typing import Any

from helpers.extension import Extension
from helpers import plugins

from usr.plugins.chat_shepherd.helpers import hotreload, monitor
from usr.plugins.chat_shepherd.helpers.constants import PLUGIN_NAME


class ChatShepherdTick(Extension):
    async def execute(self, **kwargs: Any) -> None:
        cfg = plugins.get_plugin_config(PLUGIN_NAME) or {}
        if not cfg.get("enabled", False):
            return
        try:
            # v1.9.0 graceful reload: land patched helper code between
            # ticks; never blocks the tick loop on failure
            hotreload.check_and_reload(cfg)
        except Exception:
            pass
        try:
            # module-attr access keeps this live across reloads (a
            # from-import of tick would pin the pre-reload function)
            monitor.tick(cfg)
        except Exception as e:
            try:
                monitor._debug_log('tick_fail', repr(e))
            except Exception:
                pass
