# Chat Shepherd

> A background supervisor for your Agent Zero chats. It watches every live chat, keeps stalled and wedged agents moving, and flags the ones that genuinely need you.

[![Plugin ID](https://img.shields.io/badge/plugin-chat__shepherd-8a5cf5)](https://github.com/agent0ai/agent-zero)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](./LICENSE)

An Agent Zero agent is not always working, even when it looks busy. A chat can stall
after a tool call, hang on a long streaming request, forget to close its goal, or
silently die mid-task while the UI still shows a spinner. Chat Shepherd is a
`job_loop` extension that notices these conditions and acts on them — nudging the
agent to continue, restarting wedged tasks, and raising a clear alert when a chat
truly needs a human.

It is an **active supervisor**, not a passive indicator. The sidebar
`chat_status_lights` plugin only *shows* status; Chat Shepherd *acts* on it.

---

## What it does

- **Stall detection** — classifies every live `AgentContext` into one of 8 statuses
  and auto-sends a gentle continue-nudge to agents that stopped making progress.
- **Wedge remediation** — a two-tier ladder for chats that are *running* but frozen
  (hung tool call or spinning dead loop): nudge first, soft-restart the task only
  after remediations are exhausted.
- **Intervention alerting** — when a chat burns through its nudge budget, it is
  flagged and (optionally) paged via desktop/OS notification, webhook, or Telegram.
- **Supervised mode** — instead of auto-sending, queue an editable draft for human
  review first. Wedge remediation stays automatic.
- **Goal completion gate** — catches agents that declare "done" while their goal is
  still open, and nudges them to finish or formally close it.
- **Per-chat status icons** — a live pictogram next to each chat in the sidebar,
  with one-click nudge / resolve / dismiss and a per-chat timeline.
- **Dashboard** — a full status panel with severity-sorted chats, counts, recent
  events, draft review, and JSON export.

### The 8 statuses

| Status | Meaning |
| --- | --- |
| 🏃 `running` | Actively producing log entries. |
| ⚠️ `stalled` | Running but idle past `stall_minutes` — eligible for a nudge. |
| 🔄 `nudged` | Shepherd already nudged it; waiting for a response. |
| 🚨 `intervention` | Nudge budget exhausted; a human is needed. |
| 💬 `awaiting_user` | The agent is waiting on your reply. |
| ❌ `error` | The chat ended with an error. |
| ⏸️ `paused` | The context is paused. |
| 🔌 `interrupted` | Interrupted by a server restart; resume draft queued. |

Icons are fully configurable.

---

## Install

**Plugin store (recommended):** search for *Chat Shepherd* in Agent Zero's plugin
store and install it.

**Manual:**

```bash
git clone <this-repo> usr/plugins/chat_shepherd
```

Restart Agent Zero (or refresh the plugin) — the `job_loop` extension starts
automatically on the next loop iteration.

### Requirements

- Agent Zero framework (`v2.x` or newer).
- **Optional soft dependency:** the bundled `_goal` plugin. It powers the goal
  completion gate only. If it is absent the gate is skipped and everything else
  works unchanged.

---

## Configuration

Open **Settings → Plugins → Chat Shepherd**. Settings are stored in
`usr/plugins/chat_shepherd/config.json`; `default_config.yaml` in the plugin folder
documents every default.

The most useful knobs:

| Setting | Default | What it does |
| --- | --- | --- |
| `enabled` | `true` | Master switch. |
| `watch_all` | `true` | When off, only chats that received a message are monitored. |
| `stall_minutes` | `5` | Idle time before a running chat counts as stalled. |
| `max_auto_nudges` | `3` | Nudges before a chat escalates to `intervention`. |
| `nudge_cooldown_minutes` | `10` | Minimum gap between two nudges on the same chat. |
| `max_nudges_per_tick` | `1` | Per-tick nudge budget, so a busy instance never floods. |
| `wedge_nudge_after_minutes` | `10` | Frozen-log delay before wedge Tier 1 fires. |
| `wedge_max_remediations` | `2` | Wedge attempts before escalation (0 disables). |
| `wedge_soft_restart` | `false` | Tier 2: kill the hung task and re-nudge. Off by default so long streaming calls are never killed. |
| `notify_after_minutes` | `30` | "Quiet bell": page only after the chat needs help this long. `0` = page immediately. |
| `supervised_mode` | `false` | Queue editable drafts instead of auto-sending nudges. |
| `save_state_interval_seconds` | `120` | Rate-limits quiet-tick `state.json` rewrites. |
| `hot_reload_enabled` | `true` | Hot-reload patched helpers between ticks (restart-based deploys if off). |

Per-agent scope is supported, so you can run a stricter profile for a specific agent.

### External alerts (optional, off by default)

Set `webhook_url` to receive a JSON POST per alert, and/or `telegram_bot_token` +
`telegram_chat_id` for Telegram. Private/LAN webhook targets require
`webhook_allow_private: true`. Use the **Test** buttons on the settings page to
verify delivery. Tokens are redacted from the plugin's debug log and API responses.

### Adaptive thresholds (opt-in)

Set `adaptive_thresholds: true` to let accumulated nudge/wedge outcomes tune the
three timing thresholds at runtime. Learned values live in plugin state as a
runtime-only overlay — your saved config is never overwritten, explicitly
configured values are pinned, and the per-tick budget and Tier-2 escalation are
left alone.

---

## Data and privacy

Everything stays local by default.

| Path | Contents |
| --- | --- |
| `usr/plugins/chat_shepherd/data/state.json` | Per-chat bookkeeping (statuses, counters, drafts). |
| `usr/plugins/chat_shepherd/data/history.jsonl` | Append-only action journal (self-compacting). |
| `usr/plugins/chat_shepherd/data/debug-*.log` | Rotating daily debug log. |
| `usr/plugins/chat_shepherd/config.json` | Your settings. |

The plugin makes **no network requests** unless you explicitly configure a webhook
or Telegram channel. `config.json`, `data/` and all logs are gitignored, so secrets
and chat history stay out of version control.

---

## Removal

1. Disable it in **Settings → Plugins** (or set `enabled: false`) and refresh.
2. Uninstall the plugin from the plugin store, or delete the folder:

   ```bash
   rm -rf usr/plugins/chat_shepherd
   ```

3. Restart Agent Zero.

The `uninstall` hook deletes the accumulated debug logs. `state.json` and
`config.json` are **deliberately kept** so a reinstall resumes monitoring with
history intact. To wipe everything, remove the whole `usr/plugins/chat_shepherd`
directory — nothing outside it is touched, and no chat data is modified.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| No sidebar icons | Confirm the plugin is enabled and the WebUI was refreshed. The store self-heals a missed init handoff within 2s. |
| Chats never nudge | Check `stall_minutes`, `max_auto_nudges`, and whether `supervised_mode` is queueing drafts instead. |
| Want to inspect a stall | Open the chat's timeline in the dashboard, or the **Export** button for full JSON. |
| Edited a helper and nothing changed | `hot_reload_enabled: true` reloads helpers between ticks; `api/*` endpoint changes still need a plugin refresh. |
| Chat says "done" but the goal is open | The goal gate needs the bundled `_goal` plugin installed. |

---

## Development

```bash
# self-test suite (99 markers)
python usr/plugins/chat_shepherd/tests/suite_monitor.py

# byte-compile check
python -m py_compile usr/plugins/chat_shepherd/helpers/*.py
```

`tests/suite_monitor.py` runs fully offline against a temporary state directory and
covers classification, nudge budgeting, wedge ladders, the journal, hot reload,
the settings contract, secret redaction, and the live-aware cap. It prints
`ALL_TESTS_PASSED` on success.

`AGENTS.md` in this folder is the full engineering contract: architecture, state
schema, config keys, and the version history.

---

## License

[MIT](./LICENSE) © Chat Shepherd contributors.
