from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from helpers.api import ApiHandler, Request, Response

from usr.plugins.chat_shepherd.helpers.constants import PLUGIN_NAME
from usr.plugins.chat_shepherd.helpers import monitor
from usr.plugins.chat_shepherd.helpers import state as state_mod


def _load_plugin_config() -> dict:
    # Module-level seam so tests can patch the config source
    # (plugins.get_plugin_config attribute patch).
    from helpers import plugins as _plugins

    return _plugins.get_plugin_config(PLUGIN_NAME) or {}


def _redact(text: str, secrets: list[str]) -> str:
    out = str(text)
    for s in secrets:
        if s and s in out:
            out = out.replace(s, '***')
    return out


class TestNotify(ApiHandler):
    # v1.19.0: on-demand verification of every alert channel against the
    # SAVED plugin config (framework notification leg + webhook +
    # Telegram), reusing the exact job-building and posting code the real
    # fan-out uses. A dead channel is discovered by a button click with
    # the same failure logging as the real path - instead of when a real
    # intervention pages.
    async def process(self, input: dict, request: Request) -> dict | Response:
        channel = str((input or {}).get('channel') or 'all').strip().lower()
        if channel not in ('framework', 'webhook', 'telegram', 'all'):
            return {'success': False, 'error': f'Unknown channel: {channel}'}

        try:
            cfg = _load_plugin_config()
        except Exception as e:
            return {'success': False, 'error': f'Config load failed: {e}'}

        secrets = [str(cfg.get('telegram_bot_token') or '')]
        results: dict = {}
        details: dict = {}
        tested: list = []

        if channel in ('framework', 'all'):
            tested.append('framework')
            fw_ok = await asyncio.to_thread(
                monitor._notify_framework,
                'info',
                'Chat Shepherd test notification (framework leg)',
                'normal',
            )
            results['framework'] = None if fw_ok else 'framework notification rejected'

        if channel in ('webhook', 'telegram', 'all'):
            jobs = monitor._external_jobs(
                cfg, 'info', 'Chat Shepherd test notification (external legs)'
            )
            wanted = ('webhook', 'telegram') if channel == 'all' else (channel,)
            for ch in wanted:
                if ch == 'webhook':
                    configured = bool(str(cfg.get('webhook_url') or '').strip())
                    in_jobs = any(j[0] == 'webhook' for j in jobs)
                    if not configured:
                        if channel == 'webhook':
                            return {'success': False, 'error': 'No webhook_url configured - save the config first'}
                        details['webhook'] = 'not configured'
                        continue
                    if not in_jobs:
                        if channel == 'webhook':
                            return {'success': False, 'error': 'webhook_url rejected by the SSRF guard (see debug log)'}
                        details['webhook'] = 'URL rejected by SSRF guard'
                        continue
                else:
                    configured = bool(
                        str(cfg.get('telegram_bot_token') or '').strip()
                        and str(cfg.get('telegram_chat_id') or '').strip()
                    )
                    in_jobs = any(j[0] == 'telegram' for j in jobs)
                    if not configured:
                        if channel == 'telegram':
                            return {'success': False, 'error': 'telegram_bot_token / telegram_chat_id not configured - save the config first'}
                        details['telegram'] = 'not configured'
                        continue
                    if not in_jobs:
                        if channel == 'telegram':
                            return {'success': False, 'error': 'Telegram job rejected (see debug log)'}
                        details['telegram'] = 'rejected'
                        continue
                tested.append(ch)

            net_jobs = [j for j in jobs if j[0] in tested]
            if net_jobs:
                raw = await asyncio.to_thread(
                    monitor._post_jobs, net_jobs, 'Chat Shepherd test notification'
                )
                for ch, err in raw.items():
                    # None (delivered) must stay None: _redact str()s its input,
                    # which would turn a clean delivery into the error string 'None'.
                    results[ch] = _redact(err, secrets) if err is not None else None

        ok = bool(results) and all(v is None for v in results.values())
        try:
            for ch, err in results.items():
                state_mod.append_journal({
                    'chat_id': '',
                    'action': 'notify_test',
                    'channel': ch,
                    'ok': err is None,
                    'error': err or '',
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                })
        except Exception:
            pass

        return {'success': True, 'ok': ok, 'results': results, 'details': details, 'tested': tested}
