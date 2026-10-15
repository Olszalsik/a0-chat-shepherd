# Chat Shepherd

> A background supervisor for your Agent Zero chats. It watches every live chat, keeps stalled and wedged agents moving, and flags the ones that genuinely need you.

[![Plugin ID](https://img.shields.io/badge/plugin-chat__shepherd-8a5cf5)](https://github.com/agent0ai/agent-zero)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](./LICENSE)

> **v1.22.0** merges the `chat_shepherd2` experiment back into this plugin: failure-cause
> diagnosis with a blocked/hold state (a safety termination or an auth failure is blocked
> for a human no matter what; rate limits and context exhaustion are held, not nudged),
> a supervisor self-check (`health` on the dashboard), alert-channel health with degraded
> routing, a write-ahead in-flight ledger so a crashed remediation is charged and visible,
> an operating window (active hours + startup grace), and an opt-in **continuity wake** —
> a periodic health check that can bring work into existence, not just react (off by
> default; wake ceiling 0 = off).

> **v1.24.0** is audit cycle 2: the settings modal renders ALL its cards again (a
> multi-root `x-if` in the Alpine template had silently hidden every card but the
> first since v1.20.0), corrupt `state.json` bytes are quarantined as
> `state.json.corrupt-<ts>` with a journal row instead of being silently wiped,
> the restart-resume burst actually fires after a real server restart, drafted
> sends refuse a BLOCKED chat, and every remaining event-loop block in the API
> layer is offloaded.
>
> **v1.23.1** is a full audit cycle over v1.23.0: the `POST /ack` stamp now actually
> persists (a save-signature exclusion silently dropped ack-only writes), the
> stall-escalation restart now respects the diagnosis gate and the operating window,
> the exhaustion scanner's loop-break test is ordering-based (a healthy turn with
> tool work or any reply/human turn newer than the last failure is never held), the
> streaming verdict is explicit (`streaming` — long streams are never wedged/nudged),
> soft stamps no longer re-inherit across restarts, and re-paged alerts become
> unread again after an ack.
>
> **v1.23.0** closes the gap a live 6-hour provider-exhaustion loop exposed: a running chat
> cycling "All chat model candidates exhausted" retries is now categorized
> `provider_exhaustion`, **held** (no nudges — a rate-limited chat cannot be continued into
> working), paged once, and optionally killed after `exhaustion_kill_after_minutes` (off by
> default). Also: the streaming false-positive fix (a long streamed reply no longer reads as
> a "spinning dead loop", which makes `wedge_soft_restart` safe to enable), opt-in one-shot
> stall escalation to a soft restart after nudges are exhausted, opt-in
> substantive-progress liveness (`no_response_minutes`), and **durable alert rows** — pages
> now survive restarts as an unread alert banner + sidebar badge instead of vanishing
> 3-second toasts, with an Acknowledge button (`/ack`).

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

### Nudging a chat that ended in an error

Shepherd never auto-nudges a chat whose last run stopped on a real error or a
security termination — a nudge just restarts the code path that already failed,
and Shepherd cannot know whether the underlying bug was fixed.

You can still nudge it by hand, and the dashboard will make you confirm it
first:

1. The **Nudge** button on an `error` row carries a tooltip saying so.
2. Clicking it opens a confirmation explaining why the automatic nudges are
   suppressed. **Nothing is sent and no state changes** at that point.
3. If you confirm, the nudge is sent and the chat's timeline records a
   `manual nudge override` entry naming the reason it was overridden.

Your chat keeps its `error` status afterwards. Shepherd does not mark an
errored chat as healthy just because you nudged it — it waits for a real
signal, and the next tick re-classifies the chat from the live log.

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
| `exhaustion_minutes` | `20` | Running chat cycling provider-exhaustion retries with no response → held + paged once (category `provider_exhaustion`). |
| `exhaustion_kill_after_minutes` | `0` | Kill the futile retry task after this long (0 = never; a recovered provider can still finish unattended). |
| `no_response_minutes` | `0` | Running + entries advancing + no NEW assistant response for N min → classic wedge ladder (`spinning_soft`). 0 = off. |
| `stall_escalate_to_restart` | `false` | One soft restart after the nudge budget is exhausted, before `intervention`. |
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
| No sidebar icons | Confirm the plugin is enabled and the WebUI was refreshed. The store self-heals a missed init handoff within 2s. Icons render in **both** the flat chat list and the folder view; if only nested (expanded) chats show an icon, your sidebar list view is a plugin list that the plugin cannot yet read — see *Troubleshooting* below. |
| Chats never nudge | Check `stall_minutes`, `max_auto_nudges`, and whether `supervised_mode` is queueing drafts instead. |
| Want to inspect a stall | Open the chat's timeline in the dashboard, or the **Export** button for full JSON. |
| Edited a helper and nothing changed | `hot_reload_enabled: true` reloads helpers between ticks; `api/*` endpoint changes still need a plugin refresh. |
| Chat says "done" but the goal is open | The goal gate needs the bundled `_goal` plugin installed. |
| Icons show only after expanding a parent chat | Fixed in v1.20.1 — update the plugin. Older builds only recognised the flat list's row markup, so rows rendered by a folder/list-view plugin got no icon until a nested chat was expanded. |

### Sidebar icons and third-party list views

The per-chat icon is drawn next to the chat name in the sidebar and works with the
built-in flat chat list, the built-in parent/child chat tree, and plugin list views
such as **Sidebar Folders** (`folder_view: true`).

Chat Shepherd identifies a row by, in order: a `data-context-id` / `data-folder-thread` /
`data-chat-id` attribute on the row or one of its ancestors, or an Alpine scope exposing
`context`, `child`, `item`, or `task`. Agent Zero's own chat rows publish `data-context-id`
directly, and any list view built on the core row components inherits it. A custom list
view that renders its own rows should publish an equivalent id; without one, the dashboard
still shows every chat's status — only the inline icon is skipped.

---

## Development

```bash
# self-test suite (112 markers)
python usr/plugins/chat_shepherd/tests/suite_monitor.py

# sidebar badge-injector DOM suite (23 assertions, no external deps)
node usr/plugins/chat_shepherd/tests/suite_sidebar.mjs

# shared frontend store suite (26 assertions, no external deps)
node usr/plugins/chat_shepherd/tests/suite_store.mjs

# byte-compile check
python -m py_compile usr/plugins/chat_shepherd/helpers/*.py

# javascript syntax check
node --check usr/plugins/chat_shepherd/webui/shepherd-sidebar.js
```

`tests/suite_monitor.py` runs fully offline against a temporary state directory and
covers classification, nudge budgeting, wedge ladders, the journal, hot reload,
the settings contract, secret redaction, the live-aware cap, and state-corruption
resilience. It prints `ALL_TESTS_PASSED` on success.

`tests/suite_sidebar.mjs` runs the real sidebar injector against every chat-row
shape Agent Zero renders (flat list, folder view, nested children) and checks
placement, repaint on status change, idle/blank-icon clearing, and repeat-load
idempotence.

`tests/suite_store.mjs` covers the store shared by the sidebar and the dashboard:
the poll-failure toast latch, the page-scoped poll contract, timer stacking, and
draft-edit pruning.

`AGENTS.md` in this folder is the full engineering contract: architecture, state
schema, config keys, and the version history.

---

## License

[MIT](./LICENSE) © Chat Shepherd contributors.
