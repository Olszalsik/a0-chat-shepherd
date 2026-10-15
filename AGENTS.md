# chat_shepherd

> Continuously watches your chats, auto-nudges stalled agents back to work, auto-continues wedged chats, and flags anything needing human input. Glanceable dashboard with per-chat status icons.

**Version:** 1.25.0 · **Plugin ID:** `chat_shepherd` · **Last review:** 2026-10-05 full technical audit + three remediation cycles and two closing passes: 21 defects and 16 optimizations closed (coercion at every boundary — load, config and request — so no hand-edited value can abort a tick or degrade a poll; one seam for the chat-dir base; the state fingerprint cached by CONTENT digest, halving `load_state`; journal reads incremental; one log snapshot per chat per pass; dashboard poll cannot stack or orphan a chain; the suite writes nothing into live production state and needs no container runtime) · previous: 2026-09-28 independent bug-hunt: 10 findings fixed (→ v1.20.7; config no longer pins defaults → v1.20.6; endpoint offload + /config silent-drop fix → v1.20.5; manual-override confirmation → v1.20.4; row hook + hook-independent config + lock contract → v1.20.3)

## v1.23.0 — provider-exhaustion loops, streaming exemption, durable alerts (2026-10-15)

Live evidence that drove this release: on 2026-10-15 a USER chat ran ~6h in a
provider-exhaustion retry loop ("All chat model candidates exhausted" +
"Critical error occurred, retrying..." cycling every ~12 min; ollama.com 429
weekly limit). Shepherd was alive, classified it `intervention /
liveness=spinning` — and remediated nothing for 6h, because the loop's warning
entries ADVANCE the log (the frozen-clock wedge detector never fires) and
spinning loops skip Tier-1 by design while `wedge_soft_restart` ships off.
The same tick's log-tail cause flickered `auth ↔ framework_crash ↔
tool_error` every ~15 min as the storm rotated which fragment was newest,
BLOCKING the chat at one point. Alerting was also structurally invisible
(in-memory 3s toasts). What changed:

- **Streaming false-positive fix (latent v1.10.0 defect)**: while STREAMING,
  log entries are frozen while `log.updates` advances (the response LogItem is
  created once and updated in place per chunk), so a long streamed reply was
  misread as a spinning dead loop — and with `wedge_soft_restart: true` would
  have been killed mid-stream. Discriminator: frozen + mutations advancing but
  `view.last_type in ('agent','response')` → no verdict. Pinned by TEST53A.
  This is what makes it safe to flip `wedge_soft_restart` on (config change,
  still ships off).
- **Provider-exhaustion detection** (`diagnose.exhaustion_loop` +
  `CATEGORY_PROVIDER_EXHAUSTION`, first `_RULES` entry — specific beats
  general, ahead of the 429/auth fragments the storm tail also carries): ≥2
  retry markers, newest relevant entry not a response, no newer human turn.
  The tick stamps a per-chat episode (`exhaustion_since`), clears with a
  journal `provider_exhaustion_resolved` row on recovery, and
  `classify_chat` returns intervention with `EXHAUSTION_REASON_PREFIX` after
  `exhaustion_minutes` (20). The fingerprint override maps it to
  `provider_exhaustion`, which `plan_seam` HOLDs automatically (outside the
  allowlist, outside NEVER_REMEDIATE) — this both cures the category flicker
  and un-BLOCKs the auth-fragment path. Nudges are never sent for it (an
  exhausted provider cannot be continued into working); the episode pages
  ONCE with a dedicated message (generic quiet-bell suppressed).
- **Optional exhaustion kill** (`exhaustion_kill_after_minutes`, default 0 =
  off): after N minutes of sustained futile looping, `ctx.kill_process()` ONLY
  — no fresh chain, which would just re-enter the exhausted provider. The
  chat then sits on a post-kill HOLD (classify returns
  "killed futile retry task, holding...", cleared by a human turn, a fresh
  response, or the 240-min `EXHAUSTION_HOLD_MIN` window). WAL-gated
  (`exhaustion_kill`), USER-type only, gate-not-BLOCK. TEST52A–D.
- **One-shot stall escalation** (`stall_escalate_to_restart`, default off):
  after `max_auto_nudges` is exhausted, exactly ONE `ctx.nudge()` (kill is a
  no-op on the dead task) before flipping to intervention — escalation
  instead of giving up. Budget 1/episode (`stall_restart_count`, WAL
  `stall_restart` keyed to nudge_count, reset on recovery). TEST54A.
- **Substantive-progress liveness** (`no_response_minutes`, default 0 = off):
  running + entries advancing + no NEW assistant response for N min stamps
  `spinning_soft` and routes down the CLASSIC wedge ladder (tier-1 first;
  these paths reach safe points, unlike hard `spinning`). The exhaustion rule
  owns its specific case, so this is for other spin classes. TEST53B.
- **Durable alerts**: every `_notify` page folds into `state['alerts']` keyed
  `chat_id|kind` (`_fold_alert`, re-pages update one row, cap
  `ALERTS_MAX`=20); `/status` returns `alerts` (per-row `unread` vs
  `state['alerts_seen_at']`) + `alerts_unread`; new `POST /ack` stamps
  `alerts_seen_at` and journals `alerts_ack`; dashboard banner + Acknowledge
  button, sidebar red numeric badge via a second memoized badge element
  (`setMemo` now mutates in place — a wholesale replace orphaned the handle);
  in-framework pages now `display_time=15` (was the 3s toast); selfcheck
  gains `no_alert_channel` (info) when alerts fired while no webhook/Telegram
  is configured. TEST55A–B (incl. default_config.yaml ↔ DEFAULTS parity).
  Out-of-band delivery still requires webhook/Telegram — the rows are the
  durable record, not a new channel.

Status vocabulary unchanged (8 statuses); `provider_exhaustion` is a
CATEGORY. Suites: backend suite_monitor.py green through TEST55B, node
suites green with 7 new sidebar assertions.

## v1.23.1 — audit cycle 1 of the 2026-10-15 goal (report → roadmap → fixes)

Comprehensive audit ordered as an explicit goal (audit + roadmap + full
auto-remediation authorization, then two more full audit/remediation cycles).
Method: framework-semantics verification (log entry vocabulary, the
in-place stream-update mechanics, `_error_retry`'s exact LogItems,
`kill_process`/`nudge` semantics — several claims probe-verified in the
container) + an independent adversarial review of the v1.23.0 diff. This is
the report; everything found was then fixed and regression-pinned in this
release.

### Report — findings (severity-ordered; all fixed below unless noted)

- **P0 ack-never-persists**: `state_signature()`'s basis excluded
  `alerts`/`alerts_seen_at`, so an ack-only (or fold-only) transaction
  computed `sig_after == sig_before` and skipped the save — POST /ack
  returned `success: true` while state.json never gained the stamp; the
  unread badge never cleared and the ack died on restart. Probe-verified in
  the container. (`_fold_alert`'s "unknown top-level keys force an immediate
  save" comment was factually wrong.)
- **P1 escalation-restart gate bypass**: the `STALL_RESTART_REASON` queue arm
  checked neither `gate_verdict == ACT`, `_can_act`, nor the termination
  cooldown — every sibling arm does. Failure: `ctx.nudge()` into a chat
  another plugin parked (external BLOCK→HOLD), before the block drain; and
  outside active hours/grace.
- **P1 loop-break test wrong both ways** (`exhaustion_loop`): the break
  required "newest relevant entry is not a response". (a) Normal mid-turn
  work has a TOOL newest → a >exhaustion_minutes healthy turn still holding
  2 transient retry warnings stayed matched → false hold+page on a healthy
  chat. (b) A frozen-hung tail with old markers kept matching forever →
  `exhaustion_last_matched` refreshed forever → the v1.23.0 staleness gate
  could never engage → the exhaustion arm shadowed the wedge ladder
  indefinitely (the exact scenario its own comment claimed to fix).
- **P1 stream wedged anyway**: the streaming exemption cleared only
  `liveness`; classify's frozen arm never consulted it, so a stream >10m
  frozen still classified wedged and the (now-active) drain injected a
  tier-1 continue into a healthy reply (pre-v1.23.0 this state was inert).
- **P2 stamps re-inherit** across task death / restart-interrupt
  (`no_response_since`, `liveness=spinning_soft`, exhaustion episode
  stamps): a restarted chat classified intervention with PRE-restart ages.
- **P2 supervised escalation loop**: the escalation draft never spent the
  `stall_restart_count` budget (send→redraft→send loop; the "structurally
  impossible restart loop" budget only covered the auto path), and could
  not draft while any other draft existed. New draft kind `stall_restart`
  added to `resume_draft` (the whitelist silently returned '' → drafted 0).
- **P2 reconcile gaps**: `stall_restart` reconcile charged `nudge_count`
  but not the episode budget (failed delivery → second restart in the same
  episode); `exhaustion_kill` reconcile charged a NUDGE budget and left
  `provider_exhausted_killed` unset (a second kill attempt possible).
- **P2 expiry re-entry silent**: hold expiry did not clear
  `exhaustion_notified_at` → the supervisor's own re-nudge re-firing the arm
  paged nobody, forever.
- **P2 unread arithmetic**: `unread = first_at > seen_at` — an upserted
  (re-paged) row the operator already acked could never be unread again;
  the read marker is now per-row `last_at`.
- **P2 info fold**: info pages (restart summaries) folded into the alarm
  stream and could evict unacknowledged alarm rows at the cap; only
  warning/error fold now.
- **P2 dead fast path** (pre-existing since v1.20.1, found in this cycle):
  the O11 skip read `data-cs-status` from the CONTAINER, which no code ever
  sets (applyBadge stamps the badge) — every pass took the full anchor walk.
- **P3**: comment/code mismatch in the freshness window; two uncoerced
  `wedge_nudges_sent` reads; `last_exhaustion_text` outliving an episode;
  unparseable hand-edited stamps read 9999m and never resolved; missing
  trailing newline in api/ack.py.

### Design notes recorded (no code change intended tradeoffs)

- The first `_RULES` entry (provider_exhaustion) also HOLDs genuinely fatal
  auth crashes whose text mentions a historical retry line — accepted cost
  of specific-beats-fragments; the row is still held+paged, just not
  blocked. (P3-17 of the review.)
- The exhaustion PAGE (unlike the optional KILL) fires for TASK chats too:
  the scheduler retries its own chats, but the operator still needs to know
  a task is burning cycles (P3/finding 5-class, deliberate).
- Feature B (`no_response_minutes`) is tail-bound (last ~50 entries): a
  chat >50 entries past its last response is flagged even while legitimately
  working. Default off; the arm uses the classic (nudge-first) ladder.

### Roadmap executed in this cycle (all items above)

Fixed in order: state signature (P0) → gate bypass → scanner ordering
+ marker-recency (`exhaustion_loop` gains `max_marker_age_s`, wired to
`exhaustion_minutes` as the freshness bound: a live storm's inter-marker
gap from the incident was ~12 min, so anything tighter would kill a live
storm-match) → explicit `LIVENESS_STREAMING` verdict + frozen-arm
recognition → death/restart stamp clearing (non-running branch + budget
reset unchanged) → supervised budget spend → reconcile charges
(`stall_restart_count`, `provider_exhausted_killed`) → expiry clears
`exhaustion_notified_at` → unread keyed on `last_at` → info-fold gate →
fast path reads the badge attr → P3 set → suites → this document.

## v1.24.0 — audit cycle 2 (2026-10-15)

Two independent fresh-eyes auditors over state.py/drains/WAL and api/webui.
Findings and fixes:

- **P1 config.html multi-root `x-if`** — Alpine's x-if clones ONLY the first
  element child, and the settings template's ~16 sibling `.cs-card` divs
  made every card AFTER the first silently never-rendered SINCE THE v1.20.0
  SETTINGS MIGRATION (probe-verified; other plugins wrap in a single root,
  chat_shepherd alone deviated). The cause-gated, operating-window,
  escalation, wedge, exhaustion, liveness, adaptive, supervised,
  external-alert, allowlist and pictogram cards were all invisible; the
  v1.23.0 settings cards shipped INTO this hole. Fixed: one root `<div>`
  wrapper inside the template.
- **P1 corrupt state.json wiped silently** — load_state() treated
  unparseable bytes as "missing": defaults proceeded and the next durable
  change OVERWROTE the file (every pending draft/human-review item, alert
  fold and counter gone, nothing logged). Fixed: the corrupt bytes are
  RENAMED to `state.json.corrupt-<ts>` (data-integrity: never deleted) and
  a `state_corrupt` journal row records it.
- **P2 restart-resume burst structurally dead** on real process restarts:
  `server_restarted` is knowable only on tick 0-1, which is inside the
  startup-grace window (`_can_act` False), and afterwards the flag is gone —
  so the resume burst never fired; interrupted chats fell to the generic
  stall ladder ~4 min later, one per tick, with the wrong prompt. Fixed:
  chats already stamped `interrupted` re-queue into the resume burst on
  subsequent ticks once the window opens (RESUME_TEXT, cooldown+budget
  honored).
- **P2 escalation-HOLD limbo** — with `stall_escalate_to_restart: true`,
  gate-HOLD (non-allowlisted cause) or window-withheld left classify's
  `NUDGED + "one soft restart queued"` standing forever: not held-counted,
  never paged, no fallback. Fixed: un-deliverable escalation reasons fall
  through to intervention with the withheld reason, maintain the same
  persistence-gated page clock, and count in `summary['held']`.
- **P2 journal-scan race** — the growth path's post-read fstat published a
  cache offset/size including bytes the read never consumed; a racing
  append between read and fstat made the cache claim bytes it never parsed
  and the next scan SKIPPED them permanently (silent dashboard-timeline row
  loss + adaptive undercount) until compaction. Fixed: publish the
  CONSUMED size/offsets (c_off + len(blob); cut+1 vs the raced size).
- **P2/P3 event-loop discipline**: `coerce_int` catches OverflowError
  (the last int seam without it — a raw 500 otherwise); api/test_notify
  offloaded all four blocking sites (config read, the `_external_jobs`
  builder CONTAINING the SSRF DNS resolution, journal appends, the
  channel-health fold); api/wake's config read offloaded; api/nudge's
  tracked-chat disk probe offloaded and its double state.json parse folded
  into one guard read.
- **P2/P3 api semantics**: strict bools at release `confirm` / wake `force`
  (the string "false" must not confirm/force); a BLOCKED chat refuses a
  drafted send (the send path used to overwrite the terminal status with
  dangling block fields and no release bookkeeping — status rides the
  claim transaction, zero extra reads, hint points at /release);
  draft-restore dedupes by chat too and respects DRAFT_MAX.
- **P2 nudge hold guard**: a held exhaustion chat now answers a manual
  nudge with an explicit `provider_exhaustion` confirmation guard
  (previously it silently re-entered the exhausted provider, and the
  post-kill hold fired the wrong `fatal_stop` dialog).
- **P3 set**: status.py DEFAULT_ICONS gains `blocked` (a customized blocked
  pictogram was never served); per-iteration try in the AgentContext poll
  loop (one throwing context dropped the rest of the poll to stale
  fallbacks); empty-name chip omitted for system-only journal rows;
  `stall_restart` spends `nudges_this_tick` + the throttle snapshot gains
  `stall_restarts`/`restart_budget`; reconcile skips budget charges for
  extinct chats (no resurrect-then-prune churn) with an explicit
  `_SkipCharge` handler; post-Tier-2 label says kill+restart; both
  `wedge_nudges_sent` reads coerced; adaptive `_coerce_int` OverflowError +
  `str(action)` against unhashable journal rows; state_signature basis
  gains continuity + capabilities (a quiet wake tick no longer risks one
  duplicate wake attempt; 'daily' has no writers and stays out).
- **Recorded-not-changed**: supervised mode does NOT gate the wedge Tier-2/
  kills or the exhaustion kill (DOX frames supervised around the stall
  family); the WAL contract intentionally charges budget for a failed
  communicate (documented); status.py's per-poll error.txt stat cadence is
  noted as a future optimization if it ever shows in profiles.

Regression pins: TEST45O unchanged (2 reads/send — the probe rides the
claim), full backend + node suites green (46 sidebar assertions).

### Regression pins added

TEST53C (stale exhaustion stamp must NOT shadow the wedged verdict; fresh
stamp must), TEST54B (supervised escalation drafts + budget spend),
TEST53A strengthened (explicit 'streaming' value; stream never wedged or
nudged — second-frozen-tick sequence), sidebar suite scenarios 12/12b/12d
(alert badge paint/ack-removal/re-key-with-different-count; 46 assertions).
Container probes (sandbox temp dirs) for the ack/fold signature fix are
described in the review notes; the suite covers them via TEST55A.

## v1.22.0 — the chat_shepherd2 merge (2026-10-12)

`chat_shepherd2` (the clean-rewrite sibling created 2026-10-03, which ran live
from then on) was folded back into THIS plugin in five committed stages; after
its retirement shepherd2 stays on disk disabled as a reference. What was ported,
stage by stage (all stage commits green on the full suite):

- **Stage 1 — wedged-completion detection** (the donor's 2026-10-09 fix): a
  turn whose completion path died after logging the response leaves an
  in-flight housekeeping marker (`helpers/diagnose.pending_housekeeping`) as
  the newest log entry; it classifies `intervention` and rides the wedge
  ladder tier-1 with a no-kill guard (`housekeeping_wedge` mark, expires on
  recovery). TEST46A–H.
- **Stage 2 — cause-gated remediation + `blocked`**: `helpers/diagnose.py`
  (11-cause taxonomy, first-match-wins RULES, bounded error.txt head,
  fatal-text scans read the _LogView), `helpers/planner.py` (the two donor
  gates: `may_remediate` with the NEVER_REMEDIATE floor AFTER the allowlist,
  and `plan_seam` act/hold/block). New status `blocked` — terminal until a
  human releases (a genuine turn NEWER than `blocked_at`, or `api/release.py`,
  or a confirmed manual-nudge override; the released_signature remembers WHICH
  verdict the human saw so the unchanged error.txt cannot re-block).
  Non-allowlisted causes are HELD (visible, no auto-action). Blocks drain
  post-loop under a bounded per-tick budget. Semantics change: a safety
  termination or auth failure now blocks; rate_limit/context_length/
  model_refusal are held, not nudged. TEST47A–K.
- **Stage 3 — self-check** (`helpers/selfcheck.py`): pure evaluator over a
  facts snapshot + the tick wrapper reporting its own death (durable
  `consecutive_tick_failures` + inline `loop_suspended` evaluation — a
  permanent failure must write its own findings). `/status` gains `health`.
  The save signature includes the stamp's SEMANTIC view only (last_at/since/
  paged_at/failure counter excluded) and a truly quiet tick with no prior
  stamp creates none — the TEST18A quiet-tick contract held. TEST48A–G.
- **Stage 4 — channel health + in-flight WAL** (`helpers/notify.py` +
  `state.begin/end/reconcile_inflight`): real per-channel results fold into
  state (refused/routed-around channels get an outcome, never invisible);
  degraded legs are routed around in `_external_jobs`; the tick's deliveries
  are recorded BEFORE delivery and cleared after, a crash in between is
  reaped by the phase-0 reconcile and CHARGES the claimed action's budget
  (never replayed); the signature counts inflight by length only and treats
  `channels` as durable. TEST49A–C, TEST50A–D.
- **Stage 5 — wake / operating window / capabilities** (`helpers/clock.py`,
  `helpers/wake.py`, `helpers/capabilities.py`, `api/wake.py`, `planner.
  plan_wake`): the continuity wake (heartbeat proxy when present, vendored
  create-or-resolve otherwise; `SHEPHERD_OK` quiet token; wake contexts
  STAMPED in context.data and excluded from live_contexts), active-hours +
  `startup_grace_ticks` window gating EXECUTION only (withheld actions are
  surfaced, not silent), paperclip_zero external blocks as the strongest
  fingerprint signal, soft dependency probes with regression-only
  degradation. Wake ceiling 0 = disabled. TEST51A–G.

Not ported (deliberate): shepherd2's `plan_chat`/`plan_actions` ladder and
`observe.py`/`monitor.py` — v1's own audited tick stayed; the donor's gates
are injected at seam level instead. Shepherd2's `settled` blocks (budget
exhaustion "no enabled remedy left") map to v1's existing
intervention-after-exhaustion behaviour.

## v1.25.0 — audit cycle 3 (final, 2026-10-15)

Fresh-eyes audit over the cycle-2 diffs and the not-yet-covered modules.
Findings and fixes:

- **P1 my-own-bug: quarantine triggered on ANY read failure** — the
  cycle-2 fix wrapped the READ and the PARSE in one try, so a transient
  9p read error renamed (and thereby lost from live state) HEALTHY
  state.json bytes. Fixed: read failure = defaults WITHOUT
  renaming/journaling; quarantine now requires raw bytes that
  json.loads chokes on, OR a parsed non-dict (null/array/scalar — that
  class previously dodged the quarantine entirely).
- **P1 dead wake arm**: `minutes_frozen` was hardcoded `0.0` in the wake
  observation builder, making the settings-exposed
  `continuity_on_intervention` trigger permanently unfireable
  (`0.0 >= 30.0`). Fixed: measured from the entry's
  `intervention_since`/`blocked_at` clocks.
- **P2 goal gate bypassed the operational gates** — fired during closed
  active hours / grace, after a termination-cooldown stop, and into
  gate-HOLD chats. Fixed: `_can_act` + `not _term_cooldown_active`
  + `gate_verdict == ACT` at the call site, plus honest budget counting.
- **P2 draft-send residual TOCTOU**: a block_queue could apply BLOCKED
  between the claim-time snapshot and post-delivery bookkeeping — fixed
  by applying the /nudge-style release bookkeeping inside the
  bookkeeping transaction when the entry is blocked at apply time.
- **P2 SSRF DNS on the tick thread**: `_dispatch_external` built the jobs
  (synchronous `socket.getaddrinfo` with no timeout; up to five times
  serially per paging burst) on the shared JobLoop thread — the exact
  blocking the same cycle-2 fix had offloaded in /test_notify only.
  Fixed: the build runs inside the daemon worker; the refused-channel
  fold still goes through `record_channels` for channel health,
  unchanged.
- **P2 capability probes were test-only** (stage-5's registry never ran
  live; the widget stayed empty forever). Now wired into the tick at a
  15-minute cadence behind a NEW opt-in flag `capabilities_probe_enabled`
  (default off: an unconfigured probe run would convert quiet ticks into
  eventful ones — the same new-defaults-must-reach-existing-installs
  policy). Regressions journal `capability_regressed`.
- **P3 set**: restart detection now requires a persisted per-process
  `session_id` CHANGE in addition to the 5-min gap (a long scheduler
  task or a wedged tick no longer interrupts live chats); the withheld
  reason text has three honest arms (gate/cooldown/window); a restore
  that cannot re-queue journals `draft_restore_dropped`; wake contexts
  are excluded from /status listings and refuse /nudge; the dead
  `inflight_blocked` variable is removed; unique nanosecond quarantine
  suffixes (same-second renames previously REPLACED the earlier
  evidence).

Verified clean this cycle: the wrapper nesting, journal consumed-offsets
+ the caller's out-of-order gate, interrupted re-anchor ordering,
escalation fallthrough convergence, quarantine vs `_atomic_replace`,
restart corners (first tick / skew / future last_tick), all caches and
lock ordering. All suites green.

## Purpose

Agent monitoring: a `job_loop` extension ticks periodically, classifies every live `AgentContext` into one of 8 statuses (`running / stalled / nudged / intervention / awaiting_user / error / paused / idle`), auto-sends a nudge `UserMessage` to stalled chats (bounded by `max_auto_nudges` + `nudge_cooldown_minutes`), raises a HIGH-priority OS notification when a chat exhausts its nudges, and keeps per-chat state in `data/state.json` plus an append-only action journal in `data/history.jsonl` (v1.6.0); since v1.9.0 patched helper modules hot-reload between ticks. The WebUI sidebar (JS in `sidebar-chats-list-start`) shows a per-chat status icon via `GET /api/plugins/chat_shepherd/status`, with manual nudge / resolve / dismiss actions.

## Ownership / Layout

- `extensions/python/job_loop/_30_chat_shepherd.py` — `ChatShepherdTick(Extension)`; calls `helpers.monitor.tick(cfg)` synchronously each loop iteration (guarded, respects `enabled`); since v1.9.0 it resolves `monitor`/`hotreload` via module attributes and calls `hotreload.check_and_reload(cfg)` before every tick
- `helpers/monitor.py` — `classify_chat()` (pure classifier: paused → running → error.txt → last-log-type → idle-minutes ladder) + `tick()` (state mutation, auto-nudge via `context.communicate(UserMessage(NUDGE_TEXT))`, intervention notification via `helpers.notification.NotificationManager`, nudge-counter reset when a chat runs/answers/pauses).
- `helpers/state.py` — JSON persistence at `usr/plugins/chat_shepherd/data/state.json` (`{chats: {chat_id: entry}, last_tick}`; history lives in the append-only journal `data/history.jsonl` since v1.6.0); defensive `default_state()` on any parse failure; `load_state()` repairs unusable per-chat COUNTERS at the load boundary next to the v1.20.2 non-dict-entry drop (v1.21.0), and a repairing load deliberately does not adopt the saved baseline so exactly one write persists the repair; `read_journal()` parses incrementally from a cached byte offset with a head fingerprint guarding against an in-place rewrite (v1.21.0) and does its I/O outside `_journal_lock`; `save_state(state, signature=None)` writes compact JSON through a retrying atomic replace (v1.21.0); `cap_chats(state, keep, protected=...)` caps the dict to `MAX_TRACKED_CHATS` by `last_ticked` (v1.1.0) and, since v1.20.0, **never evicts an id in `protected`** (the tick passes the live-context set), so `keep` bounds only the dead/persisted-only surplus — see the size caveat in *Known limitations*.
- `helpers/constants.py` — `PLUGIN_NAME`, status string constants, `NUDGE_TEXT` (the auto-nudge prompt), `HISTORY_LIMIT`.
- `helpers/hotreload.py` (v1.9.0) — `check_and_reload(cfg)`: mtime watch on the helper modules, compile gate, all-or-nothing reload with rollback, RLock preservation, 60s backoff, never raises; `status()` feeds the `/status` `hot_reload` block.
- `api/export.py` (v1.6.0) — full JSON export (merged chat state + journal tail).
- `api/status.py` — `Status(ApiHandler)`; live re-classification per request (state.json is treated as stale by design — fixed the stuck ⏸️), persisted-only chats re-derived to `error|idle`, sorted by severity, counts + last 20 history items.
- `api/nudge.py` — `Nudge(ApiHandler)`; manual nudge (`chat_id`, optional `text`) via `AgentContext.use(chat_id).communicate(...)`; bumps `nudge_count`, writes history.
- `api/resolve.py` — `Resolve(ApiHandler)`; `action=resolve` resets a chat to `awaiting_user` with nudge count 0; `action=dismiss` deletes the state entry.
- `helpers/resume_draft.py` (v1.16.0) — pure supervised-draft text builder (`draft_text`, `valid_kind`); embeds the classification reason as `(situation: ...)`; falls back to `RESUME_TEXT`.
- `api/draft.py` (v1.16.0) — `Draft(ApiHandler)`; supervised send/dismiss endpoint for queued drafts (`send` reuses the audited manual-nudge mechanics and journals `draft_sent`; `dismiss` stamps the 30-minute requeue gate).
- `api/config.py` — `Config(ApiHandler)`; empty body = read, else sanitize (bool/int coercion + clamps: stall 1–240, nudges 0–20, **nudges-per-tick 0–10**, cooldown 1–240, wedge-grace 2–240, wedge-remediations 0–5, wedge-cooldown 1–240; icon strings ≤16 chars, known status keys only) and persist via `save_plugin_config`; merges its own `_DEFAULTS` under `config.json` for this endpoint.
- `webui/` — `config.html` (settings modal), `main.html` (dashboard), `shepherd-sidebar.js`/`shepherd-store.js` (sidebar chips), `shepherd-init.html` (webui extension in `sidebar-chats-list-start`), `thumbnail.png` (generated by `scripts/gen_chat_shepherd_thumbnail.py` — gold crook + cream flock). `tests/suite_sidebar.mjs` is the offline DOM harness for the badge injector (22 assertions).
- `README.md` (v1.20.0) — the human-facing store document: features, the 8 statuses, config reference, the optional `_goal` soft dependency, data/privacy table, removal, troubleshooting, dev/test commands. Store reviewers read this, not this DOX.
- `index.yaml` (v1.20.0) — the community plugin-index manifest (`title` / `description` / `github` / ≤5 `tags` from the a0-plugins `TAGS.md`). Copied into the a0-plugins index repo at `plugins/chat_shepherd/index.yaml`; `github` points at the public repo `https://github.com/Olszalsik/a0-chat-shepherd`.

## Local Contracts

- **Sidebar badge row resolution (v1.20.1)**: `webui/shepherd-sidebar.js` resolves a row's chat id in this order — (1) row attributes `data-folder-thread` / `data-context-id` / `data-chat-id` walking up from `.chat-container`; (2) `Alpine.$data(container)` probing the scope keys `context`, `child`, `item`, `task`; (3) the legacy `<li>` scope lookup; (4) `dataset.chatId`. The container's own scope must be read, not only the `<li>`: since the v2.10 sidebar refactor, chat rows come from the shared `sidebar/chats/chat-tree.html` component, and a row-list extension (`_sidebar_folders`) binds the model as `item` on a wrapper `x-data` while its `x-for` scope sits on the `<li>`. The old `<li>`-only lookup returned `null` for those rows, so no badge was painted for top-level chats and only nested children (visible after expanding a parent) showed one.
- **Config reads are hook-independent (v1.20.3)**: every runtime read goes through `monitor._load_config()`, which re-applies `config_defaults.deep_merge_defaults()` on top of whatever the framework returns. No runtime path may call `plugins.get_plugin_config` directly — the `hooks.py` deep-merge is the *primary* mechanism, but a hook regression must not silently zero every unwritten key. The tick extension, `/status`, and `/test_notify` all use the seam; `api/config.py` keeps its own `_read_disk()` (it must persist, not just read). **The on-disk `config.json` is the inverse of this: it holds ONLY explicitly-set values (v1.20.6), never the full 33-key merge** — a fully-merged file pins every shipped default and would block any future default change. Completeness is the read path's job, proven by TEST36 + TEST37.
- **Row identity comes from the core hook (v1.20.3)**: `chat-tree.html` now renders `:data-context-id="context.id"` on the same element that carries `.chat-container`, so the badge injector resolves a row from that attribute with no Alpine scope at all. The scope and legacy-attribute lookups remain as fallbacks for third-party row views that do not reuse the core component.
- **Manual nudges on a fatal stop are confirmed, audited, and non-destructive (v1.20.4)**: the AUTO ladder refuses to nudge a chat that ended on a real error or a security termination, because a nudge restarts the exact code path that just failed. A human must always be able to override that — the operator may have fixed the underlying bug — but the override is never silent and never destroys the evidence. See the v1.20.4 entry in the changelog below for the full request/response contract.
- **Badge placement**: the badge is hosted by `.chat-list-button` and inserted before `.chat-list-action-btn`, so it sits with the chat name and never shifts when the hover-only close/overflow buttons appear. `pointer-events: none` keeps the whole row clickable.
- **Badge identity**: repaint is keyed on `data-cs-status`, not on the glyph — two statuses may share one pictogram and must still update colour, tooltip, and `aria-label`. A badge already inside the current host is reused, never recreated; one found in a stale host is dropped.
- **Extension script idempotence**: the file is a classic script inside the `sidebar-chats-list-start` HTML extension, and the component loader re-executes it on every extension (re)load. The `window.__chatShepherdSidebar` guard makes repeat loads a no-op that only re-runs `refresh()`, so timers and observers cannot stack.
- The core sidebar exposes no per-row content extension point, so the badge is injected DOM. Keep it inside the row's own label host so Alpine re-renders (row add/remove/reorder) simply drop it and the 2.5 s poll repaints.
- **Shared store lifecycle (v1.20.2)**: `webui/shepherd-store.js` is ONE store shared by the sidebar injector and the dashboard modal, so its poll is page-scoped. `cleanup()` must stay a no-op — the dashboard's `x-destroy` used to stop the poll and froze the sidebar icons on stale data for the rest of the page session. The sidebar's 2.5 s loop re-arms `startPolling()` if it ever finds `_started && !pollTimer`, so no surface can silently freeze the icons. **v1.21.0: `startPolling()` is a self-scheduling `setTimeout` chain, not `setInterval`** — never more than one poll in flight (`_inFlight`), and a `_pollGen` generation counter makes re-arming from inside an in-flight poll safe (`applyPollFromConfig()` does exactly that when `poll_seconds` changes); without the counter the in-flight chain would reschedule on top of the fresh one and two chains would poll forever. `fetchStatus()` returns early while a poll is in flight and treats a `degraded: true` payload as a latched failure that does NOT overwrite `this.data`.
- **One log snapshot per chat per pass (v1.21.0)**: `monitor._log_snapshot(context)` takes `context.log._lock` exactly once and returns a `_LogView` (length, updates, bounded tail, last entry type). Every scanner — `_last_log_type`, `_last_log_fatal`, `_scan_log_for_termination`, `_last_response_text`, `_last_entry_type`, `_log_len`, `_log_updates` — plus `classify_chat` and `_wedge_minutes` takes an optional `view` and must never touch `context.log.logs` itself (TEST34 pins this structurally). A missing `log.updates` degrades that field to -1 ("unknown", disables the liveness probe) instead of sinking the whole snapshot. Blocking the agent's own log writer is the cost that matters here, which is why the lock count is the thing to protect.
- **Persisted counters must survive coercion (v1.21.0)**: anything read out of
  `state.json` and used in an int comparison or an `+ 1` goes through
  `state._repair_counters` (load boundary, `monitor._as_int`, or the handler's
  `_to_int`). The three drains that run after the per-chat loop have NO exception guard, so a raw read there aborts the whole tick — that is the failure mode v1.20.2 believed it had closed by fixing one call site.
- **The state fingerprint is cached by CONTENT, never by stat (v1.21.3).**
  `_state_signature_cached` keys a one-entry cache on a sha256 of the bytes
  `load_state` just read, and `state_transaction_apply` baselines on
  `_last_loaded_sig`. Do **not** key this cache on `(mtime_ns, size)` the way
  the journal cache does — `state.json` is rewritten in whole on every save, so
  two payloads of identical length written inside the filesystem's timestamp
  granularity share a stat key, and a stale fingerprint does not slow anything
  down, it silently disarms the quiet-tick save gate (that is exactly how the
  first attempt failed, caught by TEST30). Likewise do not baseline the
  transaction on `_last_saved_sig`: its adoption is conditional (skipped when a
  save raced the read, or when the load repaired something), so it can describe
  different bytes — `_last_loaded_sig` exists to be the unconditional
  alternative. TEST45AA pins both, the second structurally (the transaction
  holds `_state_lock` and re-loads, so no behavioural test can tell them apart).
- **Coercion seams must catch OverflowError AND reject non-finite values
  (v1.21.1)** — every int/float seam in this plugin (`state._as_int`,
  `monitor._as_int`, `monitor._as_float`, `adaptive._num`, `api/status._to_int`,
  `/history`'s limit) catches `(TypeError, ValueError, OverflowError)` and
  returns its fallback, never a non-finite result. Three separate incidents in
  three release cycles, all the same shape: `int()`/`float()` reject non-finite
  *floats* but `int(float('inf'))` and `float(10**400)` raise **OverflowError**,
  which a two-arm handler does not catch, and `float('inf')`/`float('nan')`
  *succeed* — an inf stall threshold makes every chat instantly stalled, so the
  value silently inverts the supervision policy instead of degrading. JSON
  bodies and hand-edited config/state files can all carry these (Python's
  `json.loads` accepts `Infinity`, `NaN` and arbitrary-precision integers).
  Prefer routing new reads through an existing seam over adding a bare
  `int(...)`/`float(...)`; TEST45S/45W and the two structural source checks pin
  the arms. Note also the fallback discipline: a seam that must distinguish
  "unusable" from a legitimate `0` (a throttle `budget` of 0 means auto-nudging
  off) needs an explicit `fallback` argument, never `or <default>`.
- **Row id must not be cached in the sidebar memo (v1.21.1)**: the WeakMap in
  `webui/shepherd-sidebar.js` holds DOM handles only (`{badge}`). Alpine's
  keyed `x-for` reuses elements across a key change and patches the bound data
  in place, so a memoized id survives a re-key and keeps painting the old
  chat's status on a row that now shows another one. The chat id is
  re-resolved every pass (one `getAttribute` on core rows); the container's own
  `data-cs-status` is the cross-check on the fast path.
- **The poll's in-flight latch belongs to the request, not the timer
  (v1.21.1)**: `stopPolling()` must NOT clear `_inFlight`. `fetchStatus`'s
  `finally` owns it; clearing it on a re-arm lets a second poll start while a
  slow snapshot is still awaiting.
- **Poll-failure toast latch (v1.20.2)**: `_fetchFailed` is a latch, not a flag. Set it **after** the `if (!this._fetchFailed)` check on both failure paths, and clear it only on a successful response. It was previously only ever assigned `false`, so the "toast once, not every 5 s" contract was dead code and a wedged backend toasted every poll.

- **Extension point**: `job_loop` fires on the framework's periodic loop; the extension is a `helpers.extension.Extension` subclass with `async execute(**kwargs)` — plain functions are never loaded (see [[extension-synthetic-class-gotcha]]).
- **Plugin config**: `plugins.get_plugin_config('chat_shepherd')` resolves config.json (falls back to `default_config.yaml` only when it does not exist), and the root `hooks.py` `get_plugin_config` hook (v1.11.0) deep-merges plugin defaults UNDER it: config.json values win, unknown/future keys are preserved, `icons` merges per status — runtime paths always see a complete config. Safe today because `api/config.py` persists the FULL merged dict on first save (all keys present afterwards) — but a hand-written partial `config.json` makes runtime paths lose every unwritten default, and `_read_disk()` replaces the whole `icons` dict with the disk copy, so a disk map missing one status icon hides that icon even in endpoint responses (this happened with `interrupted` right after v1.1.0 — fixed by adding the key to `config.json`). Adding a `get_plugin_config` hook (deep-merge defaults UNDER config.json, the `_model_fallback`/watch-skill pattern) closes this. The 2026-09-14 review added two concrete hazards to the same item: `Config.process` **erases unknown keys** from `config.json` on save (it writes back only `_KNOWN_KEYS`-filtered content, so hand-added or future-version keys are silently dropped), and the sanitizer block at `api/config.py:126-165` is mis-nested (see P6.1).
- **Nudge delivery** is `context.communicate(UserMessage(...))` — asynchronous injection into the chat's loop; there is no `agent.memory` on the base framework.
- **Stall detection** keys off `context.last_message` (ISO string) + the last `response|tool|user|error` entry in `context.log.logs` (accessed under `context.log._lock` — private attr, wrapped in try/except).
- **error.txt** at `usr/chats/<chat_id>/error.txt` is the framework's crash marker; presence ⇒ `error` status even with no live context.
- **API handlers**: `class X(ApiHandler)` + `async process(self, input, request)`; routes are `/api/plugins/chat_shepherd/<file basename>` (POST + auth + CSRF by default; `csrf_protect` needs the `X-CSRF-Token` header or the `csrf_token_<runtime_id>` cookie that `webui/js/api.js::getCsrfToken()` sets). **Known deviation:** `webui/shepherd-store.js` and `webui/config.html` use raw `fetch()` with no CSRF header — they work only by piggybacking on the cookie other WebUI components set, which breaks after a server restart (new runtime id → new cookie name) and in a fresh tab on standalone `main.html`. Migrate to `callJsonApi` from `/js/api.js` (roadmap P6.3).
- **State** is `data/state.json` (gitignored): nudge counters reset to 0 whenever a chat is seen `running`/`awaiting_user`/`paused` at tick end, so the 3-nudge budget is per-stall, not lifetime.
- **Real chats only (v1.1.0, P1; tightened v1.1.1)**: `tick()` tracks only contexts passing `_has_chat_dir`, which requires BOTH the id to match `CHAT_ID_PATTERN` (`helpers/constants.py`, exactly 8 alphanumerics — the only shape `AgentContext.generate_id` ever produces) AND the `usr/chats/<ctx.id>` directory to exist (9p-bounded `files.exists`). The id-pattern guard exists because `message_async` DOES persist throwaway script contexts (`verify-1788992102` had a chat dir); explicit-id script contexts like `verify-*`/`ctx-hook-1` never match. Dead state entries are pruned each tick and the dead surplus is capped to `MAX_TRACKED_CHATS` (100), oldest-`last_ticked` first — **live contexts are never evicted** (v1.20.0: evicting live chats wiped their nudge/cooldown bookkeeping every tick, re-nudging stalled chats every ~2 minutes; see the v1.20.0 entry).
- **No nudge storms (v1.1.0, P2)**: auto-nudges drain a post-classification queue, oldest stall first, at most `max_nudges_per_tick` (default 1, clamp 0–10) per tick; 0 disables auto-nudging. Per-chat `max_auto_nudges` + `nudge_cooldown_minutes` unchanged.
- **Wedged ≠ stalled (v1.1.0, P3; v1.2.0 adds remediation)**: a running context whose `context.log.logs` length is unchanged for ≥ `stall_minutes` classifies as `intervention` ("log frozen"). State entries carry `last_log_len` + `log_len_since` to drive the frozen clock. **v1.2.0 remediation ladder**: after `wedge_nudge_after_minutes` (default 10 = 2× stall) frozen, USER-type contexts get a Tier-1 auto-continue (`communicate(WEDGE_NUDGE_TEXT)`); the final attempt of `wedge_max_remediations` (default 2, 0 = off) escalates to the framework's `AgentContext.nudge()` (kill hung task + `fw.msg_nudge.md`) ONLY when `wedge_soft_restart` is enabled (default OFF — a false-positive wedge would lose the hung call's output). Separate `wedge_nudge_count` budget (per wedge episode, cleared on recovery with a `wedge_nudge_effective` history entry when within `NUDGE_EFFECTIVE_WINDOW_MIN`); never touches the stalled-nudge budget. Mechanics that make this safe (why the old "queued nudges never drain" fear was wrong for the common case): `communicate()` on an alive task only sets `agent.intervention` (plain attribute write, drained at the agent's next `handle_intervention` safe point — exactly why a manual "continue" unwedges these chats), and on a not-alive task it starts a fresh chain. Scheduler (`AgentContextType.TASK`) chats are never wedge-remediated. **v1.10.0 liveness probe**: the frozen-entry clock is paired with a log mutation counter (`log.updates`); entries frozen + mutations advancing = `spinning` dead loop, both frozen = `hung` call, unknown counter = legacy verdict. Spinning wedges skip the Tier-1 auto-continue and (with `wedge_soft_restart`) are killed on the first attempt; hung wedges keep the classic ladder.
- **Cross-thread `communicate()` (v1.1.0, P4 — audited, safe)**: `tick()` runs on the JobLoop thread but calls `context.communicate()` on other contexts. Verified safe: if the target task is alive it sets `agent.intervention` (plain attribute write); otherwise `DeferredTask.start_task` → `EventLoopThread.run_coroutine` = `asyncio.run_coroutine_threadsafe` onto the context's name-keyed shared loop (`helpers/defer.py`). No nudge executes inline on the JobLoop thread.
- **Restart recovery (v1.1.x)**: a `last_tick` gap > `RESTART_GAP_MINUTES` (5) flags a server/job-loop restart. USER-type contexts that were active before it, are not running after it, and whose log doesn't end with `response`, become `interrupted` (🔌) and get a `RESUME_TEXT` nudge from a dedicated resume queue (budget ≥ 3/tick, bypasses per-chat cooldown, still respects `max_auto_nudges`). Scheduler (`AgentContextType.TASK`) chats are excluded — the scheduler resumes its own chats. History gets a `restart_detected` entry.

## Known limitations (verified true as of v1.21.3)

Every bullet here was re-checked against the code on 2026-09-27. Earlier
versions of this list carried three entries that had already been fixed
(no `hooks.py`, a Docker-bound test harness, `/status` skipping the real-chat
predicate) - they are recorded in the changelog under the release that fixed
them, not here. Do not re-file them.

- `tick()` runs synchronously on the JobLoop thread (blocking `state.json` I/O + log scans). A filesystem wedge there freezes only shepherding, not chats, and `files.exists` is globally bounded. **As of v1.20.5 every request handler reads off the event loop** — `/status` and `/draft` (v1.18.1) plus `/config`, `/history` and `/export` (v1.20.5), so a slow 9p stat in a plugin request can no longer stall the rest of the server. What remains is the JobLoop side, which is by design: the tick is the shepherd's own work.
- `api/*` handlers keep their `from`-import bindings until the next plugin refresh, so an endpoint change needs a refresh or restart. The helper/tick path hot-reloads on its own (v1.9.0) and the `hot_reload` block in `/status` reports the last result. **v1.21.0: the three `state` bindings that made this matter for hot reload are gone** (`api/status.py`, `api/export.py`, `api/history.py` now call `state_mod.*`, pinned by TEST45J), so a `helpers/state.py` patch now reaches those endpoints too; anything else they still pin is a smaller surface than before.
- `state.json` grows with the number of LIVE contexts, not with `MAX_TRACKED_CHATS` (v1.21.0, recorded rather than fixed). Since v1.20.0 a live context is never evicted, so the 100 cap bounds only the dead surplus: the audit host carried 298 entries / 295 KB with 278 live-or-persisted and 20 already prunable. Every poll reads that file and every durable change rewrites it. **v1.21.3 measured this properly: the size was never the cost.** Profiling the 298-entry file gives read 1.1 ms + parse 2.5 ms, and `load_state()` ~19 ms of which **50% was the fingerprint** — which scales with entry count and runs on every poll and every transaction, while the file read does not. The signature is now cached by a digest of the bytes just read (**19 ms → 6 ms**), the transaction baselines on the loaded fingerprint instead of re-serializing, and serialization is compact (v1.21.0). The remaining per-poll cost is the read + parse, which is inherent. If an instance ever passes a *thousand* live contexts, split cold bookkeeping into a second file then — not before.
- Wedge detection compares the log ENTRY count (plus a mutation counter since v1.10.0, which separates an actively-streaming call from a fully hung one). A long legitimate tool call that emits no entries for `stall_minutes` can still classify as `intervention`. Mitigations: the immediate task-kill requires `wedge_soft_restart: true` (off by default), and the flag drains when the call returns. **v1.23.0: the actively-streaming misclassification is fixed** — frozen entries + advancing mutations with the log's newest entry being the in-flight agent/response item no longer read as `spinning` (TEST53A); only non-stream spins (warning/error tails) keep the hard verdict. A long SILENT tool call still freezes the clock — the remaining mitigation set is unchanged.
- `watch_all: false` skips any context with an empty log. The per-chat watch list (`allowed_chat_ids`) is implemented and enforced in `tick()`.
- `state.json` and `config.json` are deliberately NOT removed by `hooks.uninstall()` - a reinstall resumes monitoring with its history intact. Only the debug logs are deleted.

## Release readiness (v1.21.3)

**What is verified.** 148 backend markers (122 shipped + 26 audit
regressions), 41 shared-store and 35 sidebar assertions; `py_compile` and
`node --check` clean; an AST undefined-name sweep over all 19 modules clean;
`config.json` holds only explicitly-set values (v1.20.6) and `DEFAULTS` matches
`default_config.yaml` exactly (33/33, zero drift). Forty-one deliberate
mutations across the audit each fail the suite, so those tests are not vacuous
— and finding that six gaps escaped them is why the count is reported with the
caveat below rather than as a blanket guarantee.

**Open items: none.** All three carried from v1.21.2 are closed (scratch-dir
hygiene, the stale `TEST16C` container caveat, and the state-cost item — which
was measured rather than assumed, and turned out to be the fingerprint rather
than the file size). The remaining bullet under *Known limitations* about a
thousand live contexts is a scale threshold, not a task.

**What is NOT certified.** "Bug free" is not a claim this repo can make, and
the following are genuinely outside what the suites prove:

- Multi-day unattended runtime behaviour, and real 9p/network-filesystem
  latency. The suites are seconds-long and run against fake contexts.
- Real Telegram/webhook delivery (only the send path's shape is tested).
- ~~`TEST16C` needs the framework runtime~~ — **RESOLVED (v1.21.3): stale since
  v1.15.0, closed here.** The old caveat said this marker could only run under
  `/opt/venv-a0/bin/python` because the `/status` handler's lazy
  `get_plugin_config` → `helpers/projects.py` → `pathspec` import only exists
  in the container runtime. Verified directly: the project venv
  (`.venv/Scripts/python.exe`) has `pathspec`, the bare interpreter does not,
  and TEST16C runs and asserts green there — including proof that the import
  really is exercised (`"pathspec" in sys.modules` is `True` after the call, and
  the aggregates block is computed from the journal, not stubbed). The whole
  suite is now verified on this host. The container runtime is still the only
  place to test genuine 9p mount latency (see *What is NOT certified*).
- Third-party sidebar row views that do not reuse the core components and
  publish no id attribute (they lose badges - the `nudgeHint`/tooltip and the
  dashboard remain fully usable).
- The framework's own suite has pre-existing unrelated failures on this host;
  they were confirmed identical before and after every plugin change.

**Verdict: production-ready**, with the five limitations above accepted by
design rather than outstanding as defects. **No open items remain** as of
v1.21.3. Ongoing work is tracked in the changelog, not here.

## Roadmap — v1.1.0 "repair after the 2026-09-09 loop freeze" (IMPLEMENTED 2026-09-10)

Context: the plugin was being built by an agent whose chat died in the three 9p event-loop freezes of 2026-09-09 (see [[agent-zero-9p-wedged-stat-freezes-all-turns]]); the plugin's `UserMessage` import fix (`from agent import UserMessage`) is applied in `helpers/monitor.py` + `api/nudge.py`. Status of each item:

- **P1 done** — `tick()` filters `AgentContext.all()` through `_has_chat_dir` (`usr/chats/<id>`, bounded `files.exists`); each tick prunes entries whose chat dir is gone and that have no live context, then `state.cap_chats()` caps the dict to `MAX_TRACKED_CHATS`=100 by `last_ticked`; prune events go to `history` (`action: 'prune_state'`). New state entry fields: `last_ticked`, `last_log_len`, `log_len_since`.
- **P2 done** — `max_nudges_per_tick` (default 1, clamp 0–10, 0 = auto-nudge off) added to `default_config.yaml`, `config.json`, and the `api/config.py` sanitizer. Stalled+cooldown-eligible chats collect into a queue; after the classification loop it is sorted oldest-stall-first (by `last_message` age, unparseable = oldest) and drained up to the throttle; the rest keep `stalled` for the next tick.
- **P3 done** — `_log_len()` snapshot per tick stored as `last_log_len`; running + unchanged length ≥ `stall_minutes` (clock in `log_len_since`) → `classify_chat` returns `intervention` with a "log frozen" reason; `summary['wedged']` counts these. Never auto-nudged; manual nudge unaffected.
- **P4 done (audit, no code change needed)** — contract documented above in Local Contracts; comment lives in `helpers/monitor.py::_nudge_context`.
- **Also landed (v1.1.x, repair of a parallel in-chat edit)** — `_parse_dt` now accepts `datetime` objects (`context.last_message` is one; feeding it to `fromisoformat` raised and made every idle computation report 9999 minutes — false stalls / post-restart nudge storms). The `intervention_after_failed_nudges` toggle works again (a stray unreachable `return` had made it always escalate). A structurally broken restart-recovery edit (dispatch chain and restart-recovery `if` dedented out of the per-chat loop, plus a dead `elif STATUS_IDLE` inside `elif STATUS_INTERRUPTED`) was re-indented into the loop and completed; `interrupted` status wired into `api/status.py` (icons, severity order) and all three webui files; `max_nudges_per_tick` added to the settings modal + save payload.
- **P5 (done 2026-09-14)** — `hooks.py` deep-merge SHIPPED in v1.11.0 (live-verified); the dashboard throttle indicator SHIPPED in v1.12.0; the per-chat watch list turned out to be already implemented and "prune history on read" is obsolete since the v1.6.0 journal. Current breakdown in the dedicated P5 section below; new review findings in P6/P7.
- **v1.14.0 (2026-09-14) — R4 adaptive thresholds (feedback loop) + P6.3 CSRF transport + P6.4 /status predicate**: new opt-in `helpers/adaptive.py` learns the three timing knobs (`nudge_cooldown_minutes`, `wedge_nudge_after_minutes`, `wedge_remediation_cooldown_minutes`) from journal-windowed nudge/wedge outcome rates: per-tick `evaluate()` runs at most once per `adaptive_interval_minutes` and needs >= `adaptive_min_samples` fresh outcomes (stale windows relax learned values toward the configured defaults); a manual settings change re-seeds the baseline and re-applies the manual value; steps are capped at ±`adaptive_step_pct`% inside hard clamps (nudge cooldown 5–60, wedge nudge 2–30, remediation cooldown 5–60 min). Learned values apply ONLY as a runtime overlay in `tick()` via `effective_values()` — the persisted config is never written, and knobs configured outside the learnable band stay pinned to the user setting. Off by default (`adaptive_thresholds: false`); wired end-to-end: `default_config.yaml`, flat `apply_overrides()` coercion (bands 15–1440 / 5–100 / 5–25), settings card (toggle + interval/min-samples/step%), `/status` `config.adaptive_*` keys + `effectiveness.adaptive` reflection (enabled / values / last_eval / window_start), tick summary tag `adaptive`. P6.3 fixed: `webui/shepherd-store.js` (static import) and `webui/config.html` (lazy dynamic import — inline script cannot top-level import) now call the framework `callJsonApi` (central CSRF token, Origin, same-origin credentials), replacing the raw `fetch()` calls; same `(path, body)` contract and fallback shapes; `node --check` verified on both files. P6.4 fixed: `/status` filters `AgentContext.all()` through the tracking predicate (`CHAT_ID_PATTERN` + `monitor._has_chat_dir`) so script contexts never appear in the dashboard. Tests +7 (TEST15A disabled passthrough, 15B seed, 15C stale-window relax, 15D tick overlay, 15E tighten, 15F manual-reconfig re-seed, 15G pinned knob) = 50 markers `ALL_TESTS_PASSED` (two clean runs); framework-runtime import of the final tree verified. api/* status reflection activates on plugin refresh/restart per the v1.9.0 caveat.
- **v1.13.0 (2026-09-14) — P6.2 test-harness portability (review error)**: `tests/suite_monitor.py` (renamed from `test_monitor.py`) derives the repo root from `__file__`, so the suite runs off-Docker (Windows-verified logic) and a bare repo-root `pytest` collects zero tests (module-level `chdir` no longer a collection side effect). Under the new bootstrap the suite executes green through all 46 currently-passing test markers; the single in-tree failure is the concurrent adaptive session's fourth TEST15 sub-test (suite line 998: expected adaptive reason `relax`, got `evaluated`) in their uncommitted R4 work — unrelated to this change. Scratch-dir hygiene deferred (needs a monitor-side chat-dir redirect hook); P6.3 (WebUI CSRF transport) and the P6.4 `/status` predicate commit land with the concurrent adaptive release since they share `webui/config.html` / `api/status.py` with it.
- **v1.12.0 (2026-09-14) — dashboard throttle indicator (P5, last open item)**: `tick()` now persists a `throttle` snapshot into state.json right before the final save (unknown top-level keys round-trip through load/save, so no state.py schema change was needed): `nudges_this_tick` vs the per-tick `budget`, `capped` (true only when the stalled queue was actually truncated by the budget — restart-resume nudges are budgeted at `max(3, min(10, max_nudges_per_tick))` and are deliberately NOT flagged as capped), `stalled_queued`/`stalled_nudged`, `resume_nudged`, wedge drain usage (`wedge_nudged` + `wedge_restarts` vs `wedge_budget` = `max(1, max_nudges_per_tick)`) and a timestamp. `/status` exposes it as a defensively-typed `throttle` block (old state files → zeroed defaults, budget falls back to the config value). The dashboard summary row gains a ⚡ `N/M nudges this tick` badge that turns red with a `capped` suffix when `capped` is set, so a nudge storm is visible at a glance. Tests +2 (TEST14A resume-drain snapshot integrity incl. no false capped, 14B stalled queue truncated by budget → capped) = 38/38 `ALL_TESTS_PASSED`.
- **v1.11.0 (2026-09-14) — config integrity (P5 hooks deep-merge + P6.1)**: new root `hooks.py` wires the framework `get_plugin_config`/`save_plugin_config` plugin hooks (same contract as `_browser`/`_tool_access`). `get_plugin_config` deep-merges plugin defaults UNDER the winning config.json — config.json values always win, unknown/future keys are preserved, and the `icons` map merges per status (the old wholesale-replace could hide icons) — so the tick loop, api handlers and webui always see a complete config even when config.json is partial. `save_plugin_config` preserves unknown keys currently on disk when the settings endpoint persists sanitized known keys; both hooks fail open and the save hook never returns None (a None return would skip the framework write entirely). Defaults, coercion and the sanitize logic moved to `helpers/config_defaults.py` — a single source of truth shared by `api/config.py` (now a thin handler reading through the framework chain and deep-merging defensively), `hooks.py` and the tests. Latent bug fixed while porting: `wedge_liveness_probe` was persisted with raw `bool()`, so the string false coerced to True — it now goes through `_coerce_bool`. The externally reported P6.1a mis-nesting was confirmed in the shipped sources (v1.6.0-v1.8.0 and v1.9.0: goal_gate_enabled, goal_gate_max_nudges, hot_reload_enabled, notify_after_minutes, notify_on_resume sat deeper than the top-level ifs, so a partial save touching a parent key silently skipped them) and is already flat in the v1.10.0 tree; the hazard class is now structurally closed by the flat-by-construction `apply_overrides()` and behaviorally regression-guarded by TEST13A. Live-verified 2026-09-14 in a fresh framework-runtime process: `plugins.get_plugin_config('chat_shepherd')` turned the 9-key partial config.json into a complete 27-key merged config (missing defaults filled, disk values kept, icons complete). Tests +3 (TEST13A partial-save guard, 13B hook merge with unknown keys + per-status icons, 13C save preserves legacy keys) = 36/36 `ALL_TESTS_PASSED`. Note: a running server may hold a cached hooks module until `clear_plugin_cache` runs — every settings save triggers it — or until the next plugin refresh/restart; `api/config.py` deep-merges defensively regardless, so its responses are complete even before the hook activates on the live chain.
- **v1.10.0 (2026-09-14) — wedge liveness probe (hung vs spinning)**: `_log_updates()` reads the log mutation counter (`context.log.updates` — appended on every new item AND every in-place `_update_item`, under the same `_lock` as the entry count). While entries stay frozen for `stall_minutes`, the counter verdict splits the wedge: mutations advancing = `spinning` (a Tier-1 continue can never drain a dead loop, so it is skipped — the episode budget stays intact and the chat stays intervention for a human), counter frozen or unknown = `hung`/legacy → the classic v1.2.0 ladder is unchanged. With `wedge_soft_restart` enabled a spinning wedge goes straight to `ctx.nudge()` on the first attempt (a dead loop never reaches the final-attempt escalation); keep it OFF (default) so long streaming calls that emit no new entries are never killed. Wired: `WEDGE_LIVENESS_PROBE` constant; `wedge_liveness_probe` in `default_config.yaml`, the `api/config.py` sanitizer (`_KNOWN_KEYS` derives from `_DEFAULTS`, so saves persist it) and a settings checkbox; `wedge_spinning` in the tick summary; per-chat `liveness` in state + /status; `liveness` tag on the `wedge_soft_restart` history entry. Tests +5 (TEST12A spinning skip with budget intact, 12B hung classic ladder, 12C spinning immediate kill, 12D probe-off legacy, 12E reason tags) = 33/33 `ALL_TESTS_PASSED`. Live-verified 2026-09-14: the running tick loop hot-reloaded the patched helpers at 12:45:32 CEST (reload event in the plugin debug log) and all 47 live state entries carry `last_updates_len`/`liveness` with a fresh `last_tick`. Caveat: api/* handlers keep pre-reload bindings — the /status `liveness` field and the settings save for the new key activate on the next plugin refresh or server restart.
- **v1.9.0 (2026-09-14) — graceful hot-reload of patched helpers**: new `helpers/hotreload.py` mtime-watches `helpers/{constants,state,monitor}.py` plus itself; on change the next tick re-execs the watched sources into the live module objects in dependency order. Rails: compile gate (a non-compiling source never loads), all-or-nothing reload with namespace rollback (including a module that failed mid-exec), module-level RLocks (e.g. `state._journal_lock`) preserved so pre-reload API handlers share one lock with the reloaded monitor, 60s backoff after any failure, and `check_and_reload()` never raises — the tick loop survives a broken patch attempt. The job_loop extension dropped its `from ...monitor import tick` binding (it would pin the pre-reload function object) for module-attr access plus a reload check between ticks. `hot_reload_enabled` (default true) wired through `default_config.yaml`, the `api/config.py` sanitizer, the `/status` payload (`hot_reload` status block + config flag) and a Graceful-reload settings card. Limitation: `api/*` handlers keep their from-import bindings until the next plugin refresh — the tick path is fully live, endpoint changes still need a refresh. Tests: TEST11 (prime/clean, live swap, compile-error backoff, exec-failure rollback, config gate, force) — full suite ALL_TESTS_PASSED. Live-verified 2026-09-14 on the running server: the running tick loop detected the changed hotreload.py and re-executed all four helpers on its own; the reloaded monitor wrote `hot_reload reloaded [constants, state, monitor, hotreload]` into the plugin debug log. Notable reload events (reloaded/failed/compile_error/primed) are mirrored into the rotating debug log via the live monitor module, making reloads observable on disk.
- **v1.8.0 (2026-09-13) — goal completion gate**: when the latest response of a USER chat claims the work is done while its native goal-system goal is still open, it gets a silent nudge to finish the goal or close it via the goal tool. Conservative completion-claim heuristic (subject + completion verb, global claims, first-person finishes) with negation/pending-window rejection; per-goal-version budget (`goal_gate_key` = goal `updated_at`, resets when the goal record changes), cooldown + disable switch; `goal_gate_enabled` / `goal_gate_max_nudges` / `goal_gate_cooldown_minutes` wired end-to-end. Tests +7 (TEST10A-G).
- **v1.7.0 (2026-09-13) — quiet bell**: intervention alerting gains a re-arm window (`notify_rearm_minutes`); per-chat `intervention_since` / `intervention_notify_at` clocks; recovery clears the bell without re-paging; notify plumbing through `default_config.yaml` / `api/config.py` / settings UI.
- **v1.6.0 (2026-09-13) — append-only journal + JSON export**: history moved out of the state blob into a size-capped append-only journal (`data/history.jsonl`, `JOURNAL_MAX_BYTES` / `JOURNAL_KEEP`) so ticks stop rewriting the full state; `read_journal()` / `compact_journal()` in `helpers/state.py`; new `api/export.py` full-JSON export (merged chat state + journal tail) wired into the webui store; TEST8 covers the export.
- **v1.5.0 (2026-09-12) — dashboard: per-chat timeline, status filter, configurable poll**: new POST /api/plugins/chat_shepherd/history endpoint returns one chat's newest-first history entries plus its resolved display name (v1.4.0 resolver, 50-entry hard cap, clamped limit, chat_id required); the chat table gains a Timeline button expanding the entries inline (timestamp, action, nudge number, detail) and Recent Actions now shows chat names instead of raw ids. Client-side status filter dropdown (All statuses + per-status with counts) built from the existing /status payload, so filtering needs no round-trip. New poll_seconds config key (2-120, default 5) wired through default_config.yaml, the config sanitizer (coerced + clamped), the /status payload (re-clamped defensively), and the settings card; the store re-arms its setInterval only when the fetched value actually changes. Tests +1 (TEST7 history API: filtering, newest-first order, missing-chat_id rejection, limit clamp) = 21, two clean runs; JS verified with node --check, HTML with a tag-balance parser.
- **v1.4.0 (2026-09-12) — human-readable chat names in alerts**: intervention + restart-resume notifications identify chats by the title shown in the WebUI, e.g. Chat Alpha Rescue (G34Q4xbU) needs human intervention: ... . New `_chat_display_name()` precedence: live `ctx.name` > persisted state `name` > mtime-cached `usr/chats/<id>/chat.json` title via bounded 4KB head read (never parses the embedded context payload) > raw id; names persist into state entries so dead chats resolve too. Regression fix: the v1.3.0 restart `_notify` call used a typo kwarg (msgcfg=cfg) that raised TypeError, so restart alerts never fired; corrected to positional message + cfg=cfg, covered by TEST6D. Resolver stats chat.json directly instead of `files.exists()` (exists_bounded can transiently return False on a 9p timeout, silently dropping names). Tests +5 (TEST6A-E) = 20, two clean runs.
- **v1.3.0 (2026-09-11) — external alert channels + per-agent config**: `_notify()` now fans intervention + restart alerts out to optional channels via `_dispatch_external()`: generic webhook (JSON POST `plugin/kind/priority/message`) and Telegram (`bot<token>/sendMessage`). HTTP POSTs run in a daemon thread so the event-loop tick never blocks; failures go to `_debug_log` (`webhook_fail`/`telegram_fail`/`webhook_skip`), never raise. Webhook URLs pass framework `helpers.network.validate_public_http_url()` (SSRF guard: private/loopback targets blocked) unless `webhook_allow_private: true`. New config keys `webhook_url` (max 500), `webhook_allow_private`, `telegram_bot_token` (max 200), `telegram_chat_id` (max 64) wired through `api/config.py`, `default_config.yaml` and an External alerts card in `webui/config.html` (token input type=password). `per_agent_config: true` in `plugin.yaml` lets agent profiles override settings. Tests +3 (TEST5A webhook dispatch, TEST5B SSRF guard incl. opt-in, TEST5C telegram payload) = 15; tests stub `helpers.network.resolve_host_ips` offline (IP literals resolve to themselves so the private-IP rejection stays honest; hostnames get a fake public IP) — no DNS needed.
- **v1.2.0 (2026-09-11) — wedge remediation ladder**: replaces P3's "never auto-nudge wedged chats". User-observed gap: a manual "continue" always unwedged these chats, but the plugin only flagged for a human. New config keys (`wedge_nudge_after_minutes` 10, `wedge_max_remediations` 2, `wedge_remediation_cooldown_minutes` 10, `wedge_soft_restart` false) wired through `default_config.yaml`, `api/config.py` sanitizer, `webui/config.html` ("Wedge auto-recovery" card) and `api/status.py` (wedge counters per chat + `wedge_sent`/`wedge_effective` in effectiveness). New history actions: `wedge_auto_continue`, `wedge_soft_restart`, `wedge_nudge_effective`. Future work: a bounded liveness probe of the target loop to distinguish dead-loop wedges (remediation is a no-op, stays intervention) from hung-call wedges.
- **v1.1.1 (2026-09-11)** — user-approved P1 refinement: `_has_chat_dir` now also requires the chat id to match `CHAT_ID_PATTERN` (8 alphanumerics, `AgentContext.generate_id`'s only shape). Live finding that triggered it: `message_async` contexts DO create `usr/chats/<id>` dirs, so the throwaway `verify-1788992102` ping context was tracked despite the dir check. Verified on real data: of 60 chat dirs, exactly the 3 junk ids (`verify-*`, `ctx-hook-1`, `_corrupt`) fail the pattern; all 57 real chats match.

### P5 — state hygiene + config plumbing (refreshed 2026-09-14 after external review)

- DONE 2026-09-14 (v1.11.0) — root `hooks.py` added with the `get_plugin_config` deep-merge (defaults UNDER `config.json`, `_model_fallback`/`_tool_access`/`_browser` pattern) to close the partial-config hazard in Local Contracts. Fold two review findings into it: stop erasing unknown keys on save (P6.1b), and deep-merge the `icons` map per status in `_read_disk()` instead of replacing it wholesale (the old `interrupted` incident).
- DONE — `watch_all` and the per-chat watch list (`allowed_chat_ids`) are enforced in `tick()` (`monitor.py:652-661`), wired through the sanitizer and the settings UI. Confirmed in the 2026-09-14 review; the old "prune history on read" idea is obsolete since v1.6.0 (history is the append-only journal, capped by `JOURNAL_READ_LIMIT`).
- DONE 2026-09-14 (v1.12.0) — Dashboard throttle indicator: `tick()` persists a per-tick `throttle` snapshot (delivered vs budget, `capped` only when the stalled queue was actually truncated, stalled/resume/wedge usage + timestamp) into state.json; `/status` exposes it defensively; the dashboard renders a ⚡ N/M badge that turns red with a `capped` suffix on truncation.

### P6 — 2026-09-14 external review: confirmed errors (fix first)

- **P6.1 `api/config.py` sanitizer nesting bug — RESOLVED 2026-09-14 (v1.11.0)** — lines 126-165 are mis-nested: `notify_on_resume` + `notify_after_minutes`/`notify_rearm_minutes` sit inside `if "notify_on_intervention"`, and `goal_gate_enabled`, `hot_reload_enabled`, `goal_gate_max_nudges`, `goal_gate_cooldown_minutes` are additionally nested inside `if "notify_on_resume"`. Any partial save that omits the outer keys silently drops those settings (the WebUI save sends every key, masking it). Fix: dedent the whole run to top level, matching the `wedge_soft_restart` pattern. (b) Related hazard in the same handler: it persists only `_KNOWN_KEYS`-filtered content, so unknown/future keys in `config.json` are erased on the next UI save — fix properly via the P5 hooks.py deep-merge. **Update 2026-09-14 (v1.11.0):** hazard closed — the sanitizer now lives in the flat-by-construction `apply_overrides()` in `helpers/config_defaults.py` with TEST13A guarding partial saves; root `hooks.py` deep-merge landed (P5).
- **P6.2 Test harness portability + pytest shape** — `tests/test_monitor.py` does `sys.path.insert(0, '/a0')` + `os.chdir('/a0')` at module import: collection fails off-Docker (verified `FileNotFoundError` on the Windows host), and because the repo has no `pytest.ini`/`testpaths`, a bare repo-root `pytest` collects the file (module-level chdir runs as a collection side effect; zero tests collected since everything lives in `main()`). Fix: derive the repo root from `__file__` (four levels up from `usr/plugins/chat_shepherd/tests/`), shape as real `test_*` functions or rename off the `test_*` pattern, and create scratch dirs under `tmp/` instead of real `usr/chats/csTstA01`-style dirs (they match `CHAT_ID_PATTERN`, so a live tick briefly tracks them if a run crashes mid-test). Verified 2026-09-14: with only the bootstrap fix, the full suite passes on Windows. **RESOLVED 2026-09-14 (v1.13.0):** the bootstrap now derives the repo root from `__file__` and the file is renamed `tests/suite_monitor.py`, so bare `pytest` collects nothing and the suite runs off-Docker; scratch-dir hygiene (temp dirs instead of real `usr/chats/csTst*` fixtures) is deferred — it needs a monitor-side chat-dir redirect hook.
- **P6.3 WebUI transport: raw `fetch()` is CSRF-fragile** — `shepherd-store.js` and `config.html` send no CSRF header; plugin POST handlers default to `requires_auth() -> requires_csrf()` (`helpers/api.py`), currently satisfied only because other components' `getCsrfToken()` set the `csrf_token_<runtime_id>` cookie. Breaks after a server restart (new runtime id → new cookie name) until some `fetchApi` call re-establishes it, and in a fresh tab on standalone `main.html`. Fix: import `callJsonApi` from `/js/api.js` (the bundled-plugin convention) in both files. **RESOLVED 2026-09-14 (v1.14.0):** shipped with the adaptive release — static import in `shepherd-store.js`, lazy dynamic import in the inline `config.html` script; both parse clean and the fallback shapes are preserved.
- **P6.4 `/status` bypasses the real-chats predicate** — `api/status.py` classifies every `AgentContext.all()` entry with no `CHAT_ID_PATTERN`/chat-dir filter, so `verify-*`-style script contexts appear in the sidebar/dashboard while `tick()` never tracks them. Fix: export the tracking predicate from `monitor` and reuse it in the status endpoint. **Update 2026-09-14 (v1.13.0):** the predicate (`CHAT_ID_PATTERN` + `monitor._has_chat_dir`) is implemented in `api/status.py` in the working tree (py_compile-verified), but its commit is deferred to the next release: the concurrent adaptive-thresholds session has uncommitted edits to the same file (`adaptive` import), so staging it now would publish an import of an untracked module. **RESOLVED 2026-09-14 (v1.14.0):** shipped with the adaptive release — the predicate filter is live in `api/status.py`.

### P7 — review follow-ups: robustness + convention debt (after P6)

- State read-modify-write lock: wrap mutate+save sequences (tick, `api/nudge.py`, `api/resolve.py`) in a module-level `RLock` so JobLoop ticks and API handlers can't drop each other's updates; `hotreload._preserve_locks` already keeps `*lock*` module attributes alive across reloads. **DONE v1.18.0 (2026-09-16):** shipped as `state.state_lock()` / `state.state_transaction_apply()`; tick wrapped via `_tick_impl`; nudge/resolve/draft handlers transact (draft.py included beyond the original list); the cosmetic resolve.py:36 misindent retired by the rewrite.
- Inline error boxes → A0 notification toasts (`toastFrontendError` etc. from `notification-store.js`): `config.html` `setMsg`/`.cs-msg`, `main.html` `.shepherd-error`, `store.error`. Required by the plugin UI contract (`plugins/AGENTS.md`: no inline success/error boxes). **Update 2026-09-17 (v1.18.5):** dashboard scope RESOLVED in v1.17.1 — toastFrontendError for all fetch/nudge/resolve/dismiss/draft/history failures; `.shepherd-error` + `store.error` removed (0 hits verified). Remaining: `config.html` still uses its inline `setMsg`/`.cs-msg` save-status line (incl. `cs-err` on save failures) as save-button feedback — migrate to toasts only if the no-inline-boxes contract is read strictly. **RESOLVED 2026-09-18 (v1.19.1):** the remaining `config.html` save/test status line migrated to framework toasts — async `toast()` seam lazy-imports `notification-store.js` (success → `toastFrontendSuccess`, failure → `toastFrontendError`, 'Chat Shepherd' title), 8 call sites, progress lines dropped in favor of the busy gates, `setMsg` / `.cs-msg` / `.cs-err` / `.cs-on` and `lastMsg` state removed (0 residue, TEST26A-B, 94 markers ALL_TESTS_PASSED x2). The no-inline-boxes contract is now fully honored on both plugin UI surfaces.
- `shepherd-sidebar.js`: scope the MutationObserver to the chat list once found instead of `document.body` with `subtree: true` (every DOM change currently re-triggers badge injection). **RESOLVED 2026-09-16 (f7f6c54):** observer attaches to `.chats-config-list` when present; the temporary document.body fallback exists only to notice the list appearing (re-scopes immediately) and the 2.5s poll re-scopes after sidebar re-renders. (Doc stamp added 2026-09-17 — the work shipped in the webui-only commit.) **SUPERSEDED 2026-09-26 (v1.20.1):** `.chats-config-list` was not part of the sidebar row-list extension contract, so a plugin list view that does not reuse the class silently killed badge injection. The observer now anchors on the core-owned `#chats-section` (falling back to `.chats-list-container`, then `.chats-config-list`).
- `api/nudge.py`: don't auto-create state entries for untracked contexts — validate with the real-chats predicate; today the next tick prunes them but the history entries linger. **RESOLVED 2026-09-17 (v1.18.3):** the handler gates on `monitor._has_chat_dir` (the exact tick predicate: 8-alnum framework id pattern + persisted `usr/chats/<id>` dir) and rejects untracked ids BEFORE `communicate()` and before the state RMW; the context lookup moved to a module-level `_get_context` seam (draft.py pattern); TEST22A-B.
- `read_journal(chat_id=...)` filter so `api/history.py` per-chat timelines stop silently losing entries beyond the global 200-entry tail on busy instances. **RESOLVED 2026-09-17 (v1.18.4):** `state.read_journal()` gained a `chat_id` keyword that filters after the full parse (stat-keyed cache reuse unchanged) so per-chat reads trim the chat's own tail instead of the global 200-entry cap; `api/history.py` now calls the filtered read directly instead of the global `load_state()` mirror; TEST23A-B.
- Consolidate `_nudge_context`'s private `nudge_debug.log` into `_debug_log` (separate non-rotating file with naive local timestamps today), and record failed auto-nudges in history so delivery failures are visible outside the log file. **RESOLVED 2026-09-17 (v1.18.5):** `_nudge_context` routes failures through the shared rotating `_debug_log` (`auto_nudge_fail` kind, UTC stamps, newest-7 daily rotation) AND journals an `auto_nudge_fail` history row (chat_id + error) so delivery failures are visible in the dashboard timeline; `_nudge_context` gained a `chat_id` keyword threaded through all four auto-nudge call sites (goal gate, restart resume, stall ladder, wedge Tier-1); `hooks.uninstall()` removes the legacy file; TEST24A-C.
- Cosmetic: `api/resolve.py:36` misindented return. **RESOLVED 2026-09-16 (v1.18.0):** retired by the serialized-RMW rewrite of `api/resolve.py` (byte-verified 2026-09-17: the return sits one indent level under its `if`).

## Verification

- `node usr/plugins/chat_shepherd/tests/suite_store.mjs` — shared-store regression suite (v1.21.0; 41 assertions, data-URL module harness — stubs `showConfirmDialog` + `toastFrontendSuccess` to exercise the guarded-nudge confirm flow, and covers the poll in-flight latch, the re-arm generation guard, the degraded-snapshot path and timeline pruning).
- `node usr/plugins/chat_shepherd/tests/suite_sidebar.mjs` — badge-injector DOM regression suite (v1.21.0; 31 assertions, no external deps — adds the bounded row-id walk and the reported-paint-failure cases).
- `node --check usr/plugins/chat_shepherd/webui/shepherd-sidebar.js` and `.../shepherd-store.js`.
- `python -m py_compile` over every plugin module.
- `GET/POST /api/plugins/chat_shepherd/status` returns live classified chats + counts + config snapshot.
- Sidebar shows per-chat icons; dashboard `webui/main.html` shows counts + history; manual Nudge increments the counter, Resolve resets it, Dismiss removes the entry.
- v1.20.4: a manual Nudge on an `error` row shows a danger confirmation carrying the backend's reason, and only then sends; the row keeps `status: error` afterwards and the timeline records a `manual_nudge_override` row. TEST39 + `suite_store.mjs` §5/§5b/§5c/§6 guard it.
- Settings → Plugins → Chat Shepherd card saves through `api/config.py` (clamped, known-key-only).
- v1.9.0: TEST11 covers the reload engine (prime/clean, live swap, compile-error backoff, exec-failure rollback, config gate, force); live check via the `hot_reload` block in `/status`.
- 2026-09-14 external review: full suite `ALL_TESTS_PASSED` on the Windows host with only the P6.2 bootstrap fix applied to a scratch copy (TEST1-TEST11). Off-Docker `pytest` collection is blocked until P6.2 lands; P6.1/P6.3/P6.4 verified against framework source (`helpers/api.py`, `webui/js/api.js`, `helpers/plugins.py`, `plugins/_goal/tools/goal.py`).

### v1.15.0 — Dashboard aggregates (2026-09-15, R4)

- New `helpers/aggregates.py` (pure compute, never raises): fixed 7-day window over the journal; stall episodes clustered per chat (`EPISODE_GAP_MINUTES = 90` — ladder retries inside the gap count as one stall), span-normalized stalls/day, nudges sent vs recovered; time-to-resume mean + median from `minutes_after_nudge` (`nudge_effective` = stall resume, `wedge_nudge_effective` = wedge recovery); wedge remediation success rate crediting the latest unconsumed attempt of the same chat within `WEDGE_RESOLVE_MINUTES = 120`, plus continues / soft-restart counts.
- Read-only integration: `/status` returns an `aggregates` block computed from the state journal mirror; no config keys, no state writes; hotreload watches the new module.
- Dashboard: 📈 aggregate badge (`aggregatesLabel()` in `shepherd-store.js`) beside the 🎯 / ⚡ badges, hidden while the window is empty.
- Tests: TEST16A-C (compute semantics incl. out-of-window and malformed entries, degenerate inputs, /status integration block); suite ALL_TESTS_PASSED on two clean runs. **Runtime note:** inside the A0 Docker container the suite must run with the framework runtime (`/opt/venv-a0/bin/python`) — TEST16C exercises the `/status` handler, whose `get_plugin_config` lazily imports framework `projects.py` → `pathspec`, which only exists in that runtime.
- Reconciliation: two concurrent sessions converged on R4 (coordination block in the roadmap); the adopted nested-schema module was kept and the retired flat-schema consumers (suite TEST16, store getter, stray main.html badge) were migrated.

### v1.18.0 — State RMW serialization (2026-09-16, P7)

The last open review race: `load_state()` -> mutate -> `save_state()` ran unlocked from the JobLoop tick and the API handlers; a manual action landing mid-tick could lose the tick's counter updates (last writer wins).

- **`state._state_lock`** (module RLock; hot-reload-preserved via the existing `*lock*` rule) + **`state.state_lock()`** context manager + **`state.state_transaction_apply(apply_fn)`**: the apply-fn mutates the locked snapshot; a durable change saves exactly once (through the v1.17.0 signature gate), mutation-free apply-fns stay write-free, and exceptions propagate without saving partial state.
- **Monitor**: `tick()` is now a thin wrapper that holds the state lock for its whole read-modify-write and delegates to `_tick_impl(cfg)` — a handler transaction arriving mid-tick blocks until the tick releases (TEST19E) instead of racing it.
- **Handlers**: `api/nudge.py`, `api/resolve.py`, `api/draft.py` run their counter/journal mutations through `state_transaction_apply` on a worker thread (`asyncio.to_thread`); `communicate()`/`AgentContext` access stays OUTSIDE the lock (audited cross-thread contract from v1.16.0); `draft.py` re-verifies the draft inside the transaction so a dismiss/send racing a re-queue acts on the fresh snapshot, and unknown-chat/no-change paths never write.
- Tests +5 (TEST19A save-once transaction, 19B write-free no-change, 19C exception atomicity, 19D handler txn blocks while the lock is held, 19E the whole tick holds the lock) = 70 markers `ALL_TESTS_PASSED` on two clean runs under `/opt/venv`.

### v1.17.0 — State save durability (2026-09-15, R4)

The monitor re-wrote the whole `state.json` on every tick even when nothing changed.

- **`save_state_interval_seconds`** (config, 0-180, default 120; 0 = legacy write-every-tick): quiet ticks skip the full-file rewrite until the interval elapses; the periodic flush keeps dashboard bookkeeping (throttle snapshot, `last_tick`) fresh well inside the 5-minute restart-detection gap (worst-case disk staleness 240s < 300s).
- **Content-signature gate** (new `state.state_signature()` / `state.save_due()` / `state.last_saved_signature()`): a save is forced immediately whenever the durable payload changes — chats (minus volatile per-chat `last_ticked`), drafts, adaptive overlay. Every meaningful transition (nudge counters, wedge bookkeeping, status changes) persists at once; only truly unchanged ticks skip the write. The signature is history-independent, so `load_state` and `save_state` compute consistent baselines; all gates fail safe to writing.
- **Observability**: `throttle.state_save_skipped` and `summary['state_save_skipped']` report the per-tick decision.
- Wiring: `config_defaults` (default + 0-180 clamp), `default_config.yaml`, `/status` snapshot, settings field + hint, suite TEST18A-18D (quiet skip, eventful force, interval-0 legacy, due save via counting `save_state` stub).

**Ownership:** `state.py` (signature + gate + load/save baseline), `monitor.py` (end-of-tick gate), `config_defaults.py`, `default_config.yaml`, `api/status.py`, `webui/config.html`, `tests/suite_monitor.py` (TEST18).

### v1.18.1 — Event-loop declog + fatal-stop guard (2026-09-16)

Live-instance symptoms: browser-side "CSRF token request timed out" warnings plus repeated "Infection check: TERMINATED" chat terminations. Root causes found in this plugin:

- **`/status` blocked the request event loop.** Async API handlers run on the main uvicorn loop; the Status handler (polled every `poll_seconds` per open tab) ran `load_state()` (state.json + full journal re-read/parse) and one bounded stat per tracked chat (`_has_chat_dir`, `_check_error_file` — up to 5s each on a degraded Docker 9p mount) inline. One slow stat stalled ALL HTTP handling, and the browser's CSRF fetch deadline is also 5s (`webui/js/api.js` `CSRF_TIMEOUT_MS`) — hence "CSRF token request timed out" whenever a poll landed on a degraded mount. Fix: the snapshot body moved to `Status._snapshot_sync`, executed via `asyncio.to_thread` (response shape unchanged); `api/draft.py`'s lock-free peek follows the same rule.
- **Per-poll stat storms removed.** New `monitor._chat_dir_ids()` lists `usr/chats` with ONE scandir (parent mtime+size keyed, 2s freshness TTL) and `_has_chat_dir` consults it (per-path bounded-stat fallback preserved); `_check_error_file` results are TTL-cached (3s); `state.read_journal()` gained an (mtime_ns, size)-keyed parse cache. Ticks and polls no longer spawn ~200 bounded stats each.
- **Terminate→nudge loop broken.** `_infection_check` terminates chats via `HandledException`, which the framework logs NOWHERE (no error log item, no error.txt — see `extensions/python/_functions/agent/Agent/handle_exception/end/_90_handle_critical_exception.py`), so shepherd classified those dead chats `stalled` and auto-nudged them back to life, reproducing the termination (and burning utility-model safety checks) until the nudge budget exhausted. New `monitor._last_log_fatal()` scans the log tail backwards until the first user/response item: `error` entries or `warning` items whose heading contains "terminated" now classify `STATUS_ERROR` (❌, human decision, no auto-nudge); the next user message or a fresh response clears it naturally.
- Tests +7 (TEST20A-20G): fatal error log, TERMINATED warning, plain warning non-fatal, response clears, tick does not nudge a fatally-stopped chat, dir-listing cache consistency, journal-cache append invalidation. Full suite `ALL_TESTS_PASSED` on the Windows host.
- **Pre-existing TEST15E failure fixed (test bug, not code):** the adaptive fixture `_journal_actions` timestamped fresh entries with `datetime.now()`, which on Windows is quantized (~15.6ms ticks) — a batch can land on the SAME clock tick as the adaptive `window_start`, and `window_rates`' deliberate `ts <= window_start → skip` boundary then dropped the whole batch (intermittent `wedge sent=0/effective=3 → insufficient_samples`). Fixture timestamps now carry a +1s shift so they sit strictly inside the window; production boundary semantics unchanged.

**Ownership:** `api/status.py` (to_thread split), `api/draft.py`, `helpers/monitor.py` (`_chat_dir_ids`, `_last_log_fatal`, classify gate, fs-probe caches), `helpers/state.py` (`_journal_read_cache`), `plugin.yaml`, `tests/suite_monitor.py` (TEST20).

### v1.18.2 — Durable terminate-suppression + budget-hold (2026-09-17)

Live-instance follow-up to v1.18.1: the terminate→nudge loop persisted because BOTH v1.18.1 guards were fragile. `_last_log_fatal()` scans only the last 5 log items and resets on any user/response item — a shepherd nudge injects a `user` log item and pushes the TERMINATED warning out of the window, re-arming the very nudges the guard exists to prevent. And the end-of-tick recovery loop reset `nudge_count` whenever a chat reached running/awaiting/paused, so every nudge→terminate cycle reset the budget: `max_auto_nudges` never actually capped anything (state journal showed nudge #1 "effective" → terminate → nudge #1 again, forever).

Three fixes in `helpers/monitor.py` (all inline-commented "v1.18.2 fix"):

- **Part 1 — in-flight safety check is never a stall/wedge.** `classify_chat()` returns `STATUS_RUNNING` ("Safety check in progress …") while `context.log.progress` starts with `Infection check:` (and is not "passed"). The checker's `gate()` freezes the log for the whole analysis (up to a main-model clarification loop); that freeze used to be misread as stalled/wedged and auto-nudged mid-check.
- **Part 2 — durable suppression stamp.** New `_scan_log_for_termination()` full-tail scan (TERMINATED-heading warnings and error items, newest→oldest) stamps `terminated_at` into the state entry — surviving restarts — and clears it on a fresh human turn. Shepherd's own nudges (`[chat_shepherd]`-prefixed user items) are TRANSPARENT to both this scan and `_last_log_fatal()` (they no longer count as a fresh human turn). While the stamp is younger than `TERMINATION_NUDGE_COOLDOWN_MIN` (30 min), `classify_chat()` returns ERROR ("nudges suppressed") and both the stalled-nudge and wedge queues are gated off.
- **Part 3 — budget hold across a terminate episode.** The recovery-loop `nudge_count` reset is skipped while a termination stamp is active, so a nudge→terminate cycle consumes the budget instead of resetting it.

Companion fix (same incident, other side): `plugins/_infection_check` v1.1.0 degenerate-verdict guard — the checker was terminating chats on bare 12-char `<terminate/>` verdicts from free-fallback utility endpoints; it now re-checks unreliable verdicts with the main model and requires its agreement for any terminate.

Live verification (2026-09-17 11:2x UTC, after the 11:20 restart): 0 auto-nudges in the journal since the fixes; 37 stamped chats all `error` status, 0 healthy chats stamped, 60 chats with cleared stamps; restart sweep found 0 interrupted chats. Suite: 12-case ad-hoc guard matrix + full suite `ALL_TESTS_PASSED` (incl. TEST20A-20G v1.18.1 contracts).

Durable coverage added 2026-09-17 (TEST21A-C in `tests/suite_monitor.py`): 21a — an in-flight infection check (`log.progress`) classifies RUNNING while the same frozen running chat without the gate wedges (part 1); 21b — the `terminated_at` stamp is persisted and nudges stay suppressed across ticks even when the TERMINATED warning is buried under shepherd-nudge user items (part 2); 21c — the nudge budget is NOT reset while a stamp is active while a clean paused chat still resets (part 3). FakeCtx gained a `progress` field for the gate. Two clean full-suite passes on this tree (80 markers, RC=0).

**Ownership:** `helpers/monitor.py` (classify gate, `_scan_log_for_termination`, `_termination_cooldown_active`, `TERMINATION_NUDGE_COOLDOWN_MIN`, stamp/clear in tick, queue gates, budget-hold reset); `tests/suite_monitor.py` (TEST21).

### v1.18.3 — Nudge tracked-chat gate (2026-09-17, P7)

- `api/nudge.py` no longer auto-creates state entries for untracked contexts: a manual nudge must pass the exact tick predicate (`monitor._has_chat_dir` = 8-alnum framework id pattern + persisted `usr/chats/<id>` transcript dir) and is rejected BEFORE `communicate()` and before `get_chat()` can auto-create the ghost entry. Old behavior: a nudge aimed at a script context (`verify-*`) wrote a `manual_nudge` history row + state entry that lingered until the next tick pruned them. Context lookup moved to a module-level `_get_context` seam (same test-patchable pattern as `api/draft.py`); the v1.18.0 serialized-RMW transaction and the communicate-outside-the-lock contract are unchanged.
- Tests: TEST22A (untracked id rejected, zero communicate, no state entry, no journal row) + TEST22B (tracked chat still nudges: communicate + nudge_count bump + `manual_nudge` journal row).

### v1.18.4 — read_journal chat_id filter (2026-09-17, P7)

- `helpers/state.py`: `read_journal(limit=200, chat_id=None)` filters per-chat AFTER the full parse, so per-chat reads trim the chat's own tail instead of the global 200-entry cap; the stat-keyed parse cache (`_journal_read_cache`) and the positional `read_journal(limit)` callers (state mirror, suite) are unchanged. `api/history.py` now calls the filtered read directly instead of filtering the global `load_state()` mirror — per-chat timelines keep this chat's older entries on busy instances where 200 newer global entries previously buried them.
- Tests: TEST23A (3 early chat-A entries buried under 205 chat-B entries — the per-chat read still returns all three newest-first while the global 10-tail stays chat-B-only) + TEST23B (the /history handler serves the buried entries).

### v1.18.5 — Nudge failure visibility (2026-09-17, P7 closeout)

- `helpers/monitor.py`: the private non-rotating `data/nudge_debug.log` (naive local timestamps, no pruning) is retired — `_nudge_context` logs failures through the shared `_debug_log` (`auto_nudge_fail` kind, UTC stamps, newest-7 daily rotation) AND journals an `auto_nudge_fail` history row (`chat_id`, `error`) so delivery failures are visible in the dashboard timeline, not only a side file. `_nudge_context` gained a `chat_id` keyword; all four auto-nudge call sites (goal gate, restart-resume, stall ladder, wedge Tier-1) pass it — previously a delivery failure left NO journal trace on any path. `hooks.uninstall()` removes the legacy file alongside the daily debug logs.
- Tests: TEST24A (forced UserMessage failure: False return, journaled row with chat_id + error, `auto_nudge_fail` line in the shared daily log, no legacy file created) + TEST24B (wedge Tier-1 with a raising communicate: no crash, `wedge_nudged` 0, failure row journaled) + TEST24C (uninstall removes a planted legacy file; pre-existing daily logs restored byte-identical).
- P7 bookkeeping folded in: sidebar bullet stamped RESOLVED (f7f6c54, work shipped 2026-09-16), cosmetic resolve.py bullet stamped RESOLVED (v1.18.0 rewrite, byte-verified), toasts bullet updated (dashboard scope done v1.17.1; config.html save-status line remains as save-button feedback).

### v1.19.0 — External-channel verification (2026-09-17, R3 closeout)

- `helpers/monitor.py`: `_dispatch_external` split into test-reusable seams — `_external_jobs()` (channel job building incl. the SSRF `webhook_skip` validation) and `_post_jobs()` (synchronous fan-out returning per-channel None=delivered / error-string results, keeping the exact `webhook_fail` / `telegram_fail` debug kinds); the framework leg of `_notify()` lifted verbatim into `_notify_framework()`. `_notify` / `_dispatch_external` remain thin wrappers — tick-path behavior and the TEST5/TEST6D contracts are unchanged.
- New `api/test_notify.py`: `POST /api/plugins/chat_shepherd/test_notify` `{channel: framework|webhook|telegram|all}` verifies the SAVED plugin config through the same code path the real fan-out uses (framework leg via `_notify_framework`; network legs via `_external_jobs` + `_post_jobs` on `asyncio.to_thread`, never the event loop). Unconfigured channels reject with an actionable error (single-channel) or land in `details` (all); SSRF-rejected URLs are reported instead of silently skipped; `telegram_bot_token` is redacted from returned error strings; every tested channel journals a `notify_test` row.
- WebUI (`config.html`): Test all channels / Test webhook / Test Telegram buttons under the alert-channels card, wired through `callJsonApi` via the page `api()` helper with a `notifyTesting` busy gate; results render in the existing save-status line (per the documented config.html inline-status residue).
- Tests +5 (TEST25A framework-leg success/failure independence; TEST25B `_external_jobs`/`_post_jobs` payload parity + non-2xx / raise / `webhook_skip` paths; TEST25C1-C3 endpoint all-legs + journal row, secret redaction, unconfigured/unknown-channel negatives) = 92 markers ALL_TESTS_PASSED on two clean runs under /opt/venv-a0. api/* handlers activate on plugin refresh/restart per the v1.9.0 caveat — run one refresh before the Test buttons go live on a running server.

### v1.19.1 — config.html toast migration (2026-09-18, UI contract closeout)

- `webui/config.html`: the inline save/test status line is retired per the plugin UI contract (`plugins/AGENTS.md`: no inline success/error boxes) — the last strict-contract UI residue. A new async `toast(msg, ok)` seam lazy-imports `/components/notifications/notification-store.js` (same pattern as the page `api()` helper): success → `toastFrontendSuccess`, failure → `toastFrontendError`, title 'Chat Shepherd'. The hosting context was verified up front: config.html embeds as an x-component fragment in the main WebUI settings document (`plugin-settings-store.js`), where the backend-first toast stack is mounted (`index.html`) and `globalThis.sendJsonData` exists (`index.js`) — so the framework toast path is reachable from the settings page. All 8 feedback call sites migrated (config fetch error, 4 test outcomes, 3 save outcomes); the two progress messages ('testing ...', 'saving ...') were dropped in favor of the existing `busy` / `notifyTesting` button gates. `lastMsg` / `lastOk` / `lastErr` state, the `<small>` status element, and the `.cs-msg` / `.cs-on` / `.cs-err` CSS removed (0 residue hits verified); v1.19.0 Test-button results now surface as toasts.
- Tests +2 (TEST26A residue-free + toast-seam structural asserts; TEST26B portable `node --check` gate over the extracted inline script, skipped cleanly when node is absent) = 94 markers ALL_TESTS_PASSED on two clean runs under /opt/venv-a0. Static WebUI file only — no api/* change, so no plugin refresh is needed.

### v1.20.0 — Live-audit fixes: cap churn, baseline race, secret redaction, settings contract (2026-09-24, P8)

Live-runtime audit of this instance's own data (state.json + history.jsonl + debug logs) surfaced one high-impact production bug and three lower-severity ones; all fixed with regression tests.

- **Cap churn storm (high)**: with more live contexts than `MAX_TRACKED_CHATS` (100), `cap_chats()` evicted the same live chats every tick (oldest `last_ticked` = the first-processed set) and the next tick re-created them — `nudge_count`/`last_nudge_at`/wedge bookkeeping was wiped per tick, so `nudge_cooldown_minutes` and the `max_auto_nudges` budget never engaged: one chat took **73 auto-nudges at a ~2-minute cadence** (journal evidence), the intervention bell could not fire for churned chats, every churn tick forced a state.json rewrite (defeating the v1.17.0 quiet-tick gate), and ~550–1300 `prune_state` rows/day evicted real history. Fix: `cap_chats(state, keep, protected)` is live-aware — **live contexts are never evicted**; `keep` bounds the dead (persisted-only) surplus, which is the growth the original P1 cap guarded. The tick passes `protected=set(live_contexts)` and journals dead pruning (`prune_state`) separately from dead-surplus eviction (`cap_evicted`); `summary` gains `cap_evicted`. Tests: TEST27A (120 live + 20 dead: live survive, dead prune, no eviction) and TEST27B (2 live + 105 dead-with-dirs: exactly the 5 oldest dead evicted).
- **Saved-baseline race (low)**: `/status` calls `state.load_state()` on worker threads every poll; load_state used to overwrite the shared `_last_saved_sig` — a save finishing mid-read clobbered the baseline (one suppressed or redundant save next tick). Fix: dedicated `_sig_lock` + `_save_serial`; `load_state()` snapshots the serial BEFORE the file read and adopts the loaded payload as baseline only when no save raced (all-or-nothing); `save_state()` / `last_saved_signature()` update/read the baseline under the lock (name ends in `lock` → hotreload-preserved). TEST30 (deterministic mid-read save via a `read_journal` patch).
- **Telegram/webhook secret redaction (low)**: `_post_jobs()` logged `repr(e)`; requests exceptions embed the full URL, so the Telegram bot token (and webhook credentials) could land in the debug log. New `monitor._redact_text()`; `_post_jobs` redacts each error against its own job URL before returning and logging. TEST28 (both channels, log + return value).
- **/history id gate (low)**: `api/history.py` validated only `len ≤ 64` and passed the raw id to `_chat_display_name` → `_read_chat_title`, which builds a `usr/chats/<id>/chat.json` path from the raw value; now enforces `CHAT_ID_PATTERN` (POST + auth + CSRF already gate the endpoint). TEST29 (traversal-shaped and overlong ids rejected; a valid id works).
- **Settings contract (convention)**: `webui/config.html` migrated to the framework plugin-settings contract (`plugins/AGENTS.md`) — values bind to `config.*` through the `$store.pluginSettingsPrototype` scope; the own save button / load component is gone (the settings modal's Save / Reset / scope selectors own persistence) and `hooks.save_plugin_config` now runs the shared `apply_overrides` sanitize pass, so int clamps / bool coercion / icon merges hold on EVERY save path, not only `/api/config`. The external-alert test buttons and the pictogram picker remain as nested custom widgets behind a `window.chatShepherdTestNotify` classic-script seam; toast feedback unchanged. TEST26A/B rewritten (settings-contract asserts + `node --check` over the single script block).
- **Minor**: the hotreload watch list gained `helpers/config_defaults.py` (edits previously needed a restart); stray leading-space comment lines in `constants.py` cleaned; `shepherd-store.js` gained an idempotent `_started` flag + a 2s self-heal `init()` fallback for the sidebar-extension/Alpine timing race (a missed handoff previously left the store silently unpolling).
- **Tracked-chat maintenance made visible (v1.20.0 closeout)**: the cap fix is only trustworthy if an operator can see it working, so the `pruned` / `cap_evicted` split also flows into the persisted `throttle` snapshot (the v1.12.0 dashboard channel) and out through `/status`; `shepherd-store.capLabel()` + a `main.html` badge render `🧹 N stale chats pruned`, appending `· M over cap evicted (live chats protected)` when the cap actually bites. `cap_evicted > 0` is now only reachable from dead surplus, so a sustained `> 0` on a busy instance is direct evidence that live contexts are being protected. TEST31 (120 live contexts: all survive, `throttle` carries both keys, `/status` passes them through).
- **Store submission artifacts**: added `README.md` (feature overview, the 8 statuses, config reference, optional `_goal` soft dependency, data/privacy table, removal + reinstall-preserves-state note, troubleshooting, dev/test instructions) and `index.yaml` (`title` / `description` / `github` / 5 tags from `TAGS.md`). The `description` deliberately contrasts with the passive `chat_status_lights` display plugin so reviewers can tell them apart. `index.yaml` ships pointing at the public repo `https://github.com/Olszalsik/a0-chat-shepherd`.
- Tests +6 new markers (TEST27A/B, 28, 29, 30, 31) + TEST26A/B rewritten = 100 markers; ALL_TESTS_PASSED on this tree (Windows framework runtime, two consecutive runs), py_compile green on all 22 plugin modules, `node --check` green on `shepherd-store.js`. Plugin version 1.20.0 (plugin.yaml + this file).

### v1.20.1 — Sidebar badge visibility after the v2.10 chat-row refactor (2026-09-26)

User report after updating Agent Zero: chat status icons were missing from the rows you glance at, and only appeared on chats revealed by expanding a parent. Root cause was the row-identity lookup in `webui/shepherd-sidebar.js`, not the status pipeline.

- **Row identity (the regression)**: `chatIdFromContainer()` read the chat id from the row's `<li>` Alpine scope and only accepted the keys `context` / `child`. Since the sidebar refactor (`chat-tree.html` as a shared row component, plus the `sidebar-chats-list-view` row-list extension point), a `_sidebar_folders` row is built as `li[data-folder-thread]` (x-for scope `{item, group, itemIndex}`) → `div.sidebar-folder-host[x-data="{get context(){return item}}"]` → `x-component` → `.chat-container`. None of the accepted keys exist on that `<li>`, so the resolver returned `null`, `paintRows` bailed at `if (!chatId) return`, and **no badge was ever created for top-level chats**. Nested children went through `chat-tree.html`'s own `x-for` (`child` scope) and still resolved — which is exactly why the icons looked present only after expanding a parent. Verified by replaying the old resolver against the new row shapes: folder row → `null`, child row → id, flat row → id.
- **Fix**: resolve in order — row attributes (`data-folder-thread` / `data-context-id` / `data-chat-id`) walking up from `.chat-container`; then `Alpine.$data(container)` (the row's *inherited* scope, which covers every list presentation) probing `context`, `child`, `item`, `task`; then the legacy `<li>` lookup; then `dataset.chatId`.
- **Placement**: the badge is now hosted by `.chat-list-button` and inserted before `.chat-list-action-btn`. It used to be appended to `.chat-container`, i.e. after the close/overflow buttons that core CSS shows only on hover/selection, so the icon sat in the hover-action column instead of beside the chat name.
- **Observer scope**: `.chats-config-list` is not part of the sidebar row-list extension contract, so any list view that does not reuse the class silently disabled badge injection. The observer now anchors on the core-owned `#chats-section`, falling back to `.chats-list-container`, then `.chats-config-list`; the 2.5 s poll re-scopes when that root is replaced.
- **Repaint correctness**: the badge is keyed on `data-cs-status` instead of the glyph, so two statuses sharing one pictogram still update colour, `title`, and `aria-label`; a badge in a stale host is dropped rather than stolen from another chat's row.
- **Idempotence**: `shepherd-sidebar.js` is a classic script inside the `sidebar-chats-list-start` HTML extension, and the component loader re-clones and re-executes it on every extension (re)load. A `window.__chatShepherdSidebar` guard now makes repeat loads a no-op that only calls `refresh()`, so the poll interval and MutationObserver can no longer stack.
- **Robustness**: `injectBadges` reschedules instead of silently dropping a re-entrant pass and swallows per-run DOM errors so a transient row shape cannot kill the poll loop; `pointer-events: none` keeps the row fully clickable; `role="img"` + `aria-label` added.
- **Tests**: new `tests/suite_sidebar.mjs` — a dependency-free DOM harness (no jsdom in this repo) that runs the real injector against the four row shapes Agent Zero actually renders, plus placement, repaint, idle/blank-icon clearing, observer anchoring, and repeat-load idempotence. 22 assertions, `node usr/plugins/chat_shepherd/tests/suite_sidebar.mjs` green. `node --check` green on both plugin scripts; the Python suite still reports ALL_TESTS_PASSED.
### v1.21.3 — the three open items closed, and one measured wrong (2026-10-05)

The three open items from v1.21.2. Two closed as expected; the third turned
out to have been diagnosed incorrectly, which was the most useful outcome of
the pass.

**1. P6.2 scratch-dir hygiene — CLOSED.** The suite now redirects
`monitor._CHAT_DIR_BASE` to `tmp/chat_shepherd_suite_chats` for the whole run
(one assignment, courtesy of the v1.21.2 `_chat_dir` seam) and clears the
listing / error-file / name caches on entry and exit, so nothing resolved
against the real base is reused. Fixture chats — including the `chat.json`
files the name resolver reads, which the suite previously lacked, meaning the
v1.21.0 LRU eviction had only ever run against a missing file — now live under
the scratch base. TEST45Y asserts the behavioural half: no `csTst*`/`nme45q*`
directory may exist under the real `usr/chats`, the base must still be
redirected, and the fixtures must resolve. Mutation-tested: dropping the
redirect, or making `make_chat_dirs` bypass the base, both fail the suite —
and the second one *leaked into the live tree during that battery*, which is
the residue TEST45Y exists to prevent (removed; it was empty).

**2. `TEST16C` — the caveat was STALE, closed.** v1.15.0 recorded that this
marker could only run under `/opt/venv-a0/bin/python` because `/status`'s lazy
`get_plugin_config` → `helpers/projects.py` → `pathspec` import exists only in
the container runtime. Verified directly instead of taken on faith: the project
venv has `pathspec` (the bare interpreter does not), the marker runs and
asserts green here, and the import is genuinely exercised —
`"pathspec" in sys.modules` is `True` after the call and the aggregates block is
computed from real journal rows. The whole suite is now verified on this host.
The container runtime remains the only place that can exercise real 9p mount
latency, which stays under *What is NOT certified*.

**3. The state-size item — I HAD DIAGNOSED IT WRONG, and the fix was better
than the plan.** The recorded plan was "split cold bookkeeping into a second
file". Profiling first showed that would have been wasted work: with 298
entries / 295 KB, the file read is 1.1 ms and the parse 2.5 ms, but
`load_state()` costs ~19 ms — and **50% of it is `state_signature`**, which
`load_state` computes only to adopt the saved baseline. That cost scales with
entry count and runs on *every* `/status` poll per open tab plus every
transaction; file size was never the bottleneck. So the fix targets the
signature instead:

- `load_state` keys a one-entry cache on a **sha256 of the file bytes it just
  read** and skips the recompute on an unchanged load: **19 ms → 6 ms** here.
  `state_transaction_apply` then baselines on `_last_loaded_sig` — the
  fingerprint of what the load read — instead of re-serializing the file.
- **The cache is keyed on CONTENT, not on `(mtime_ns, size)`** — and that is
  not a stylistic choice. My first attempt used the stat key, mirroring the
  journal cache, and TEST30 failed immediately: `state.json` is rewritten in
  whole on every save, and two payloads of identical length written inside the
  filesystem's timestamp granularity share a stat key. The cache then served a
  stale fingerprint, which does not slow anything down — it silently disarms
  the quiet-tick save gate. A digest of the bytes already read is an exact
  identity and still ~6× cheaper than the signature it avoids. TEST45AA
  constructs two same-length payloads on purpose; mutation-testing a stat-keyed
  cache now fails the suite with `clean load lost the baseline`.
- `_last_loaded_sig` is deliberately distinct from `_last_saved_sig`: the
  latter's adoption is *conditional* (skipped when a save raced the read or
  when the load repaired something), so it can describe different bytes. The
  first draft of this change read back the conditional one — the race in v1.20.0
  P8, re-introduced.

**Two mutation escapes, and why one of them stayed.** Of five cache mutations,
two escaped: baseline on the conditional save baseline, and recompute the
signature anyway. The first is now caught by a **structural** TEST45AA(b)
assertion (source-level, `ast`), because `state_transaction_apply` holds
`_state_lock` and calls `load_state()` itself — it re-establishes both values,
so no behavioural test can distinguish them; two attempts confirmed that, and
neutering the structural guard makes the race escape again, which is the
proof the assertion does the work. The second (recompute anyway) is
*correct, only slow*, so it needed a call-count assertion rather than a
correctness one.

**Verification.** 148 backend markers, 41 store, 35 sidebar assertions;
`py_compile` + `node --check` clean; AST undefined-name sweep clean.
Mutation coverage across the whole audit: **41**, each confirmed to fail the
suite.

### v1.21.2 — post-audit pass: one seam for the chat-dir base (2026-10-05)

A last consistency sweep while answering "is anything left?". `config_defaults.DEFAULTS`
and `default_config.yaml` were checked for drift (33/33 keys, zero value
differences) and `prune_to_explicit` re-verified for idempotence, unknown-key
preservation and secret retention — all clean.

One real latent inconsistency did turn up. `_CHAT_DIR_BASE` existed only for the
scandir in `_chat_dir_ids()`, while **four sites interpolated `'usr/chats/'`
inline** — and one of them was the fallback probe *inside* `_has_chat_dir`,
the exact predicate that decides whether a chat is supervised at all. If the
base and the listing ever disagreed, `_has_chat_dir` swallows the exception and
returns `False`, so a real chat stops being nudged with no visible signal. All
four now go through `monitor._chat_dir(chat_id)`, and TEST45X pins it
structurally with `ast` (docstrings mention the literal legitimately, so a text
check would either miss a real concatenation or exempt prose by hand).

Side effect: the chat-dir base is now redirectable in one place, which is what
the long-deferred **P6.2 scratch-dir hygiene** item needed. The suite still
creates real `usr/chats/csTstA01..C` dirs (they match `CHAT_ID_PATTERN`, so a
live tick can briefly track them if a run dies mid-test); redirecting
`_CHAT_DIR_BASE` to `tmp/` in the suite's `main()` would close that. Verified
that the redirect works end to end (predicate, title and display name all follow
it) — **not applied here**, because doing so is a test-layout decision, not a
production fix, and it belongs with whoever next touches the suite.

**Verification.** 147 backend markers, 41 store, 35 sidebar assertions;
`py_compile` + `node --check` clean; the AST undefined-name sweep clean. Three
mutations of the new seam each fail the suite (inline fallback probe, inline
title path, seam ignoring the base), so TEST45X is not vacuous. Cumulative
mutation coverage across the audit: **36**.

### v1.21.1 — audit cycles 2 and 3: boundary coercion, memo staleness, poll latch (2026-10-05)

The v1.21.0 remediation was followed by two further full audit+remediation
cycles over the *resulting* code, as the audit process requires. Cycle 2 used
three techniques the first cycle had not: an AST-based undefined-name sweep
over all 19 modules, a hostile-input boundary probe that feeds every endpoint
the shapes a JSON body can actually carry, and reference-counting to find
runtime-dead helpers. Cycle 3 re-reviewed every v1.21.0 and cycle-2 change
adversarially and probed the handlers end to end.

**Cycle 2 findings (6 fixed)**

1. **`int(float('inf'))` raises OverflowError, which the two-arm coercion
   handlers did not catch** (`state._as_int`, `monitor._as_int`). This was the
   same hole as A1 in a place the fix had not reached: on the LOAD path it
   meant `load_state()` itself raised, and `load_state` has NO exception guard,
   so one hand-edited non-finite counter reproduced exactly the whole-tick
   blindness the v1.21.0 repair exists to prevent. Now caught, with the guard
   kept deliberately minimal (TEST45S): a mutation that removed a
   post-hoc non-finite filter proved such a filter is unreachable, because
   `int()` already rejects every non-finite input.
2. **`read_journal` silently dropped entries for a non-positive limit.**
   `selected[-limit]` with a negative limit returns nothing for a short
   journal and everything-minus-five for a long one. No caller passes a
   non-positive limit today; the fix makes "no tail trimming" explicit instead
   of accidental (TEST45T).
3. **The sidebar repaint memo cached the CHAT ID, not just DOM handles.** My
   own O11 optimization: Alpine's keyed `x-for` REUSES elements across a key
   change and patches the bound data in place, so a cached id survives a
   re-key and keeps painting the old chat's status on a row now showing another
   one. The memo now holds only element handles and re-resolves the id every
   pass; TEST45 pins the re-key case. An O11-shaped fix that is only correct
   for a fresh page.
4. **`stopPolling()` cleared the in-flight latch**, so a re-arm happening while
   a slow snapshot was still awaiting could start a second concurrent poll —
   the exact thing the latch (v1.21.0 A11) exists to prevent. The latch belongs
   to the request lifecycle and is cleared by `fetchStatus`'s `finally`.
5. **`_log_updates` became runtime-dead** after the O5 snapshot refactor (the
   tick and `/status` read `view.updates` directly). Kept deliberately as the
   documented standalone equivalent, with a comment warning against "optimizing"
   the two call sites back to it, plus the live rationale that the tick header
   legitimately reads the log before the per-chat snapshot exists.
6. **`/history` was the one endpoint with no `isinstance(input, dict)` guard**
   (v1.20.7 added it to four handlers and missed this one) and its `limit`
   coercion had the same two-arm handler. Both closed (TEST45V).

**Cycle 3 findings (5 fixed)**

7. **`float(10**400)` raises OverflowError, and `float('inf')`/`float('nan')`
   SUCCEED** — in `monitor._as_float` and `adaptive._num`, both of which guard
   the tick header outside the per-chat guard. The raise is the v1.20.7 hole
   again; the success is worse than a raise, because an inf cooldown makes
   every chat instantly off-cooldown and an inf stall threshold makes every
   chat instantly stalled — a hand-edited value silently INVERTS the
   supervision policy instead of degrading. Both seams now catch OverflowError
   and reject non-finite results (TEST45W).
8. **`/status`'s throttle budget had its own inline `try/except` with the
   two-arm handler**, inside the block the v1.21.0 degrade guard covers — so
   the seam's own guard was firing on its unguarded neighbour and a
   non-finite persisted budget produced `degraded: true`. It now goes through
   `_to_int`, which also gained a `fallback` argument because this key must
   distinguish "unusable" from a legitimate 0 (0 = auto-nudging off; replacing
   it with the default would paint a false "capped" reading).
9. **`poll_seconds` had the same inline pattern and the same arm**, and an
   `or 5` I introduced in cycle 3's first cut collapsed a legitimate 0 to the
   default instead of clamping it up to the 2 s floor. Caught by the test
   written in the same commit; fixed with the same explicit-fallback
   approach.
10. **`_to_int` also guarded `poll_seconds` from a config value** that the
    framework merges without type coercion — a hand-edited
    `poll_seconds: Infinity` in `config.json` degraded every dashboard poll.

**Techniques worth repeating.** The AST undefined-name sweep (built because
v1.21.0 shipped a stale `tail` reference inside a `try/except` that swallowed
the NameError and silently disabled the durable termination stamp) reports zero
across all 19 modules. The hostile-input probe found three of the six cycle-2/3
findings that reading did not, because Python's `json.loads` accepts
`Infinity`/`NaN` and arbitrary-precision integers — shapes a hand-edited file
or a curious client can send but that never appear in a code review. Runtime
reference counting found the dead `_log_updates`.

**Verification.** 145 backend markers (up from 140), 41 store, 35 sidebar
assertions (up from 31). `py_compile` + `node --check` clean; the static
undefined-name sweep clean. **Mutation-tested: 33 mutations across the three
cycles, every one confirmed to fail the suite**; three gaps found this way were
closed (the resume drain had no coverage, the journal tail needed a
bytes-read proof, and `status poll coercion` escaped a battery because the test
for it did not exist yet). Every battery ends with a green restore run.

### v1.21.0 — audit remediation: 15 defects + 14 optimizations (2026-10-05)

A source-only technical audit (findings filed verbatim further down this file)
followed by remediation. Three defects were bugs in the *previous* fixes rather
than in new code, and two of the four implementation bugs below were caught by
this plugin's own suite while the fixes were being written — recorded here
because that is the argument for keeping the suite at this size.

**Defects closed**

1. **Persisted counters are coerced at the load boundary** (A1). v1.20.2 added
   `monitor._as_int` and claimed it made "any state counter" safe, but wired it
   into `classify_chat` only. Every other read was raw, and three of those sites
   run in the drains AFTER the per-chat loop: a string in `nudges_sent` or
   `wedge_nudges_sent` raised `TypeError` and killed the entire tick — no nudges,
   no prune, no cap, no throttle snapshot for any chat, traceable only in the
   rotated debug log. A string in `nudge_count` silently stopped that chat being
   nudged again while the dashboard still showed "nudge 3/3 used".
   `state.load_state` now repairs every counter of every surviving entry (next
   to the v1.20.2 non-dict repair), a repaired payload is deliberately NOT
   adopted as the saved baseline so exactly one write persists the repair, and
   all 14 remaining raw sites coerce locally so an in-process writer cannot hit
   the same hole. `/nudge` had the sharpest version of this: it raised AFTER
   `communicate()`, so the operator got a 500 for a nudge that went out.
2. **`/status` degrades instead of 500ing** (A2). Per-chat classification was
   guarded; the effectiveness / adaptive / throttle / payload blocks were not, so
   one malformed field froze every sidebar icon on stale data for the rest of the
   page session. `_snapshot_sync` is now a guarded wrapper over `_build_snapshot`
   and returns a valid `degraded: true` payload; the store toasts it once and
   keeps its last good data.
3. **Request fields are coerced, and `resolve` validates `action`** (A3).
   `helpers/api.py:79-83` normalizes a non-dict body, but dict *values* arrive
   raw: `{"chat_id": 123}` raised `AttributeError` on `.strip()` → bare 500.
   `nudge`/`resolve` now `str()` their fields like the other handlers do, and an
   unknown `resolve` action is rejected instead of silently clearing the budget.
4. **`adaptive.configured_values` cannot raise** (A4) — the v1.20.7 `_as_float`
   treatment never reached this module, so a hand-edited knob degraded the whole
   adaptive overlay to `adaptive_tag='error'` in the tick and deleted the
   `/status` reflection, both invisibly. Also drops an `or 10` that turned a
   legitimate configured `0` into 10.
5. **`/config` writes off the event loop** (A5). v1.20.5 offloaded both read
   seams and left the write inline; TEST41 only pinned the reads.
6. **`capped` means suppressed** (A6) — supervised mode with a zero budget queues
   editable drafts and suppresses nothing, yet reported a red "capped" badge.
7. **Dead `pre_drain_nudged` removed** (A7) — `summary['nudged']` only ever moves
   inside that drain, so the snapshot was always 0.
8. **Endpoints reach state through the module** (A8). `api/status.py`,
   `api/export.py` and `api/history.py` pinned `load_state`/`read_journal` with
   from-imports, which survive a `helpers/state.py` hot-reload — so a state
   patch silently did nothing for those endpoints, and `/status` could mix two
   generations of state code in one request. TEST41 and the new TEST45J pin this.
9. **`_notify` no longer reads the framework config directly** (A9) — the
   `cfg=None` fallback bypassed the v1.20.3 seam and would have silently dropped
   every external alert; TEST45K now asserts the seam is the only caller.
10. **A2-adjacent UI work**: the store's `timelines` map is pruned to the tracked
    chat set (A10, the `draftEdits` leak class), polling is a self-scheduling
    `setTimeout` chain with an in-flight latch and a generation counter so a slow
    snapshot can no longer stack requests or leave an orphan chain (A11), the
    settings modal reuses the store's payload instead of firing its own
    `/status` (A12), the sidebar row-id walk is bounded (A13) and a failing
    paint is reported to the console once a minute instead of vanishing (A14).
11. **The suite no longer writes into live production state** (A15). It
    redirected `STATE_FILE`/`JOURNAL_FILE` but not `_debug_log`, so every run
    appended test events to the shipped `debug-*.log` (confirmed on disk) and its
    newest-7 rotation could evict a real day. TEST24A/TEST44G now assert against
    the run's scratch sink, and TEST45R pins the redirect.

**Optimizations shipped**

12. **Journal reads are incremental** (O1) — the parse cache carries the byte
    offset past the last complete line, so an unchanged poll does no journal I/O
    at all and an appended row is read as a tail instead of re-parsing 379 KB per
    poll. Growth alone is not proof of an append (compaction rewrites the file
    in place and can end up *larger*), so a 512-byte head fingerprint must match
    too; TEST8C caught that, and TEST45G proves the path by BYTES READ, since a
    full re-parse returns identical entries and would pass a result-only
    assertion.
13. **The journal parse no longer holds `_journal_lock`** (O8) — the tick appends
    while holding `_state_lock`, so a slow parse stalled the tick and every API
    transaction behind it.
14. **One log snapshot per chat per pass** (O5). `_log_snapshot()` takes the
    agent's log lock once and returns a `_LogView` (length, updates, bounded
    tail, last type); all seven scanners and `classify_chat` read it, instead of
    up to 8 lock acquisitions and 5 tail copies per chat per tick and again per
    poll. TEST34 now asserts the new invariant (the slice lives in exactly one
    place) — a missing `log.updates` degrades to -1 instead of sinking the whole
    snapshot, which is how the first cut of this broke TEST1.
15. **`state_signature` computed once per save** (O3), **`state.json` serialized
    compact** (O4, ~40% fewer bytes per durable write), **`os.replace` retried
    briefly on lock contention** (a lost durable write is worse than 150 ms of
    waiting; found by TEST45H on Windows), **aggregates cached per journal stat
    key** (O2), **`/draft` reads state twice instead of three times** (O7 — the
    lock-free peek existed only for early returns the transaction now provides),
    **debug-log rotation once per UTC day** (O9), **`_NAME_CACHE` is a real LRU**
    (O10: a full flush at 120 entries against ~345 chats meant a burst of
    `chat.json` re-reads), redundant `get_chat` re-lookups dropped (O12), and
    hot-reload keys on `st_mtime_ns` (O13, a float mtime can miss an edit).
16. **Sidebar repaint memo** (O11) — `container -> {id, badge}` in a `WeakMap`,
    so a repaint resolves nothing it already resolved and skips rows whose status
    already matches.

**Not changed, on purpose (O6).** `MAX_TRACKED_CHATS = 100` bounds only the dead
surplus since v1.20.0's live-protection fix, so `state.json` scales with the
number of live contexts (298 entries / 295 KB on the audit host, 20 of them
already prunable). Evicting bookkeeping for a live chat would break its nudge
budget, so the size is reduced where it can be (compact serialization, the
signature gate, fewer needless writes) rather than by re-introducing a total cap.
This is now stated in *Known limitations* instead of being implied by the
constant.

**Verification.** `python usr/plugins/chat_shepherd/tests/suite_monitor.py`
ALL_TESTS_PASSED, including the new TEST45A–R block (18 markers). Store suite 41
assertions (was 26), sidebar suite 31 (was 23). **Mutation-tested:** 16 targeted
mutations of the new guards were each confirmed to FAIL the suite; four gaps
found that way were closed (the resume drain had no coverage, the journal tail
needed a bytes-read proof, and two of my own mutations were malformed and had
proved nothing). Every mutation run was followed by a green restore run.

### v1.20.7 — independent audit fixes: 10 findings (2026-09-28)

An off-DOX bug hunt (fresh reviewer, source-only) confirmed 10 real defects;
all are small and all are fixed here. No roadmap item was needed.

1. **Draft double-send race** (`api/draft.py`): two concurrent `send` calls
   for one draft id both passed the "find draft → remove → communicate"
   sequence on separate snapshots and the message was delivered twice. Send
   now CLAIMS the draft first in a single `state_transaction_apply`
   (find + `remove_draft` + return the claimed copy atomically); the second
   caller is rejected `not found` before any communicate. A failed send
   restores the claimed draft (best-effort transaction).
2. **Non-dict bodies crashed three endpoints** (`api/nudge.py`,
   `api/resolve.py`, `api/test_notify.py`): the framework passes the raw
   parsed JSON through, so a JSON array/string/number body raised
   `AttributeError` on `.get` → raw 500. All four write endpoints (incl.
   `/config`, guarded in v1.20.5) now coerce non-dict bodies to `{}` first.
3. **`/config` reported garbage as updated** (`api/config.py`):
   `updated: sorted(readable)` listed keys whose values had FAILED coercion
   (coerce_int falls back to the current value), so a "successful" save with
   `poll_seconds: "abc"` reported the key as updated while persisting
   nothing. The reply now diffs pre/post values and lists only keys that
   actually changed.
4. **Restart-resume burst starved the stalled queue** (`helpers/monitor.py`
   tick): the resume drain runs first and increments `nudges_this_tick`, and
   the stalled queue broke on that same counter — with
   `max_nudges_per_tick: 1`, a restart burst (resume budget ≥ 3) left every
   queued stalled chat unnudged for the tick and the dashboard showed a
   false "capped". The stalled queue now has its OWN counter
   (`stall_drained`); resume, stall, and wedge budgets are independent.
5. **`_post_json` followed redirects** (`helpers/monitor.py`): webhook
   delivery passed no `allow_redirects`, so a 307/308 could bounce a POST
   onto a host the SSRF guard had already rejected. `allow_redirects=False`.
6. **Rejected webhook URLs leaked into the debug log**
   (`helpers/monitor.py` `_external_jobs`): the `webhook_skip` path logged
   `repr(e)` of the SSRF validation error, which embeds the full URL
   (querystring token included), defeating `_redact_text` at every other
   site. The skip line is now redacted.
7. **Timing knobs crashed the tick on hand-edited config**
   (`helpers/monitor.py`): `classify_chat` and the tick header coerced
   `stall_minutes`, `nudge_cooldown_minutes`, `max_auto_nudges`,
   `max_nudges_per_tick`, `wedge_*` etc. with bare `int(...)`/`float(...)` —
   a hand-edited `null`/string config.json raised inside the per-chat guard
   and dropped that chat's supervision for the tick. All converted through
   new `_as_float` (and the existing `_as_int`).
8. **`/status` aggregates read a 200-entry mirror** (`api/status.py`):
   `aggregates.compute(state['history'])` covered only the
   `JOURNAL_READ_LIMIT` mirror `load_state` fills, so the advertised 7-day
   window undercounted on a busy instance. Now `read_journal(JOURNAL_KEEP)`
   — the full journal, mtime/size-cached, same source `api/export.py` uses.
9. **Hot-reload dropped plain Locks** (`helpers/hotreload.py`
   `_preserve_locks`): the isinstance check matched `_type(RLock)` only, so
   `state._sig_lock` (a plain `Lock`, the v1.20.0 save-baseline lock)
   silently changed identity on every reload, briefly splitting baseline
   bookkeeping across two locks. Both lock types are preserved now.
10. **Sidebar body-fallback observer repainted page-wide**
    (`webui/shepherd-sidebar.js`): until the chat root appeared, the
    MutationObserver on `document.body` called `scheduleInject()` on every
    page mutation while `observedEl` stayed `null`, so the poll's re-scope
    check never fired and the page-wide querySelectorAll + Alpine probes ran
    for the whole session. The fallback now only re-scopes; the poll picks
    the repaint back up and re-scopes on its own.

**Tests**: TEST44A–H (10 new markers, 122 total) — double-send claim +
restore-on-failure, non-dict bodies on all three endpoints, `/config`
`updated` truthfulness, the stall-budget/resume-budget split, the
redirect refusal (structural, TEST34-style), webhook-skip redaction, and
plain-Lock preservation. `ALL_TESTS_PASSED` in the container;
`suite_store.mjs` 26 + `suite_sidebar.mjs` 23 unchanged and green.

### v1.20.6 — config.json records only what the operator set (2026-09-27)

Closes the forward-compatibility item deliberately deferred in v1.20.5, at the
owner's direction (their own install is the only one that matters).

- **Every shipped default was being pinned into the user's file.** Both save
  paths persisted the FULLY MERGED config, so the first time anyone saved one
  setting, all 33 keys landed on disk — including 32 values identical to
  `DEFAULTS`. In `deep_merge_defaults` the file always wins over `DEFAULTS`, so
  those 32 stale copies would shadow any value a later release changed: a
  corrected default could never reach an existing install, silently, forever.
  This is also what v1.20.3's "config.json is fully merged 33/33" actually
  described — a side effect of saving the merged view, never a correctness
  feature. Both read paths (`api/config.py::_read_disk` and
  `monitor._load_config`) already apply `deep_merge_defaults` independently,
  so they agreed either way; the full file bought nothing.
- New pure `config_defaults.prune_to_explicit(merged)` reduces a merged config
  back to the operator's values: keep a known key only when it differs from
  `DEFAULTS`, keep **every** unknown key untouched (a key we do not understand
  may belong to another version — dropping it would destroy config), and for
  the `icons` dict keep only the sub-keys the user changed. It is idempotent.
  Applied in `api/config.py` **and** in `hooks.save_plugin_config`, because the
  hook is the path the *framework settings modal* takes, not just this
  endpoint.
- **TEST36 is inverted, not deleted.** It asserted the old contract
  (fully-merged, 33/33) and would have failed the moment a single key was
  pruned. It now asserts nothing on disk is pinned to a default, that the read
  path still yields all 33 regardless, that prune is lossless and idempotent,
  and that an unknown key survives. TEST37 (hookless read) is unchanged and
  still proves the runtime never depended on the file being complete.
- The owner's own `config.json` was 33/33 with every value equal to its
  default, so pruning reduced it to `{}`. Verified the read still returns all
  33 keys — nothing was lost, nothing was customised to begin with.

**Tests**: TEST42 (a save writes exactly the explicit keys and no default-valued
key; resetting a key to its default removes it from the file while the read
still returns the default; an unknown key survives) and TEST43 (the
`hooks.save_plugin_config` path prunes too, survives a `_read_persisted`
explosion, and never returns `None`). Mutation-tested; **one mutation escaped
twice** — the first TEST42/43 draft stubbed `plugins_helper.save_plugin_config`
and so never executed the hook, meaning deleting the hook's prune call passed
the entire suite. TEST43 now calls `hooks.save_plugin_config` directly. A
transient `PermissionError` seen once during mutation testing was my script
holding `hooks.py` open, not a product flake — four consecutive clean runs
afterwards. 112 markers.

### v1.20.5 — /config silent-drop fix, all endpoints off the event loop (2026-09-27)

Closes the one actionable follow-up recorded in v1.20.4's limitations list, and
fixes two defects found while doing it. Every claim below was reproduced
against the pre-fix code first.

- **A typo'd config key reported success and persisted nothing** (real,
  user-facing). `/config` split the body into `readable` (known keys) and
  discarded the rest, then answered `ok: True, success: True` with no mention
  of the drop. Reproduced: `{"totally_bogus_key": 1}` → `ok: True`,
  `success: True`, `error: None`, `updated: None`, and **nothing written**. An
  operator (or a third-party client) sending a misspelled field got a clean
  save confirmation for a no-op. Unknown keys are now collected into
  `rejected`; an all-unknown body is an explicit `ok/success: False` with the
  offending names in `error`, and a mixed body reports both `updated` and
  `rejected` so a partial save is never silent. TEST40.
- **Inconsistent error shape** (contract). The "body must be a JSON object"
  reply was the only path returning `{ok, error}` without `success`. It
  *behaved* — `config.html:54` gates on `r.success && r.ok` and `undefined` is
  falsy — so the inconsistency was invisible until you compared the three error
  paths side by side. Every path now carries both flags. Related: the old
  `overrides = input_data or {}` turned any *falsy* non-dict (`0`, `""`, `[]`)
  into `{}` and answered a malformed body as a legitimate pure read; the raw
  body is now validated before that coercion.
- **`/history` reported a malformed id as a missing one.** `if not chat_id or
  not CHAT_ID_PATTERN.match(chat_id)` returned `'chat_id required'` for both,
  so a caller sending `../../etc/passwd` was told to go look for a field it had
  actually supplied. Split into two accurate messages. The path-shaping
  rejection itself was already correct (v1.20.0 P8) and is unchanged.
- **All three endpoints now read off the event loop** (the improvement). v1.18.1
  moved `/status` and `/draft` onto `asyncio.to_thread`; `/config`, `/history`
  and `/export` were left reading files inline, so one slow 9p stat in a plugin
  request stalled every other request in the server. Each handler now delegates
  to a small sync `_build`/`_snapshot`/`_read_disk` body under
  `asyncio.to_thread`, mirroring the `_snapshot_sync` shape `api/status.py`
  already used. TEST41 asserts the read seam runs on a *different* thread than
  the caller, for every branch of `/config` (read AND save) plus `/history` and
  `/export`.

**A rejected optimization, recorded deliberately.** `/export` appeared to parse
the journal twice (`load_state()` builds a mirror, then `read_journal()` again),
and the first version of this work reused the mirror instead. That was wrong
twice over: `read_journal` is already cached by `(mtime_ns, size)`, so the
second call never re-parsed and the saving was zero; and `load_state()` fills
the mirror with `JOURNAL_READ_LIMIT` (200) while `/export` is a **full** export
that must use `JOURNAL_KEEP` (1000) — the reuse would have silently truncated
every export to 200 entries. The code comment in `_snapshot()` says so, and
TEST41 pins the constant so it cannot be reintroduced.

**Tests**: TEST40 (4 groups: all-unknown is an error and writes nothing; mixed
reports both sides; four malformed bodies all carry both flags; clean read/save
unchanged) and TEST41 (every read seam off the calling thread, including both
`/config` branches; export keeps `JOURNAL_KEEP`; a bad id is not called
missing). Mutation-tested: restoring the silent drop, either `/config` branch's
inline read, the 200-entry export reuse, and the old "required" message each
fail the suite. **One mutation initially escaped** — the first TEST41 only
covered `/config`'s read branch, so reverting the *save* branch's `to_thread`
passed; the test now exercises both branches. 109 markers, 26 store assertions,
23 sidebar assertions, core sidebar 6 passed.

### v1.20.4 — Confirmed, audited, non-destructive manual override (2026-09-27)

The auto-nudge ladder's refusal to touch a chat that ended on a real error or a security termination is **correct and unchanged** — Shepherd cannot know whether the underlying bug was fixed, and a nudge restarts the exact code path that just failed. The finding was about the *manual* path, which bypassed that judgement with no acknowledgement and no record.

Three defects in `api/nudge.py`, all reproduced before fixing:

- **Silent send, no warning.** The Nudge button sent immediately on a chat whose fatal stop the ladder itself considers un-nudgeable. The operator got no hint that this was the one case Shepherd refuses on its own.
- **The override erased the evidence.** `_apply` hardcoded `status='nudged'` and overwrote `last_classification`, so nudging an errored chat flipped the row to a healthy-looking `nudged` and replaced the reason it died. The signal an operator most needs *after* an override was the one thing the override destroyed. Reproduced directly: `{'status': 'error', ...}` → `{'status': 'nudged', 'last_classification': 'Manual nudge (attempt 1)'}`.
- **No distinct audit record.** An override was journaled as an ordinary `manual_nudge`, so the timeline could not distinguish "routinely nudged a stalled chat" from "a human deliberately restarted a broken one".

Contract now:

- `_evaluate_guards(ctx, chat_id)` reuses the *same* two predicates the ladder applies — `monitor._last_log_fatal()` and `monitor._termination_cooldown_active()` — so the two paths cannot drift. It runs on the nudge path too, including when forced, because a forced nudge must still know *which* guard it overrode.
- Guarded + no `force` → `{success: true, needs_confirmation: true, guard, warning}`. **Nothing is sent and no state is touched**, so the response is a confirmation request, not an error, and re-issuing with `force` is safe.
- Guarded + `force` → sends, journals `action='manual_nudge_override'` with the guard in `detail`, and **preserves the prior status**, annotating `last_classification` and stamping `manual_nudge_override_at` / `manual_nudge_override_guard`. The row stays `error` until the monitor re-classifies it on a real signal.
- Unguarded + `force` → an ordinary nudge (`overrode_guard: false`). `force` never invents an override.
- `force` is parsed by `_as_bool`, not `bool()`: a client sending the string `"false"` must not override a fatal-stop guard.
- WebUI: `nudge(chatId, force)` opens the framework's `showConfirmDialog` (type `danger`) carrying the backend's own warning text, and re-issues with `force: true` only on confirm. `nudgeHint(chat)` puts the same warning in the button's `title` on `status === 'error'` rows, so the behaviour is discoverable *before* the click.

New `state.read_chat_entry(chat_id)` reads one entry without `load_state()`'s journal read + cache; the guard needs only the durable `terminated_at` stamp, and the v1.18.1 event-loop work exists precisely because those reads used to block it.

**Tests**: TEST39 (backend, 5 cases: unguarded sends and flips status; guarded asks and sends/mutates nothing; forced delivers, journals an override, preserves `error`; `force` on a healthy chat stays ordinary; `force="false"` is not truthy) and `suite_store.mjs` §5/§5b/§5c/§6 (confirm dialog shown with the backend's reason, confirm re-issues with `force: true`, cancel sends nothing, ordinary nudges never force, the tool-tip keys only on real `STATUS_*` values). Verified by reverting each half: disabling the guard, retrying without force, and restoring `status='nudged'` in the override branch each fail the suite. `suite_monitor.py` 107 markers, `suite_store.mjs` 26 assertions.

### v1.20.3 — Row identity hook, hook-independent config, lock contract (2026-09-26)

Follow-up on the three items flagged at the end of the v1.20.2 audit. Two were defects worth fixing; the third was reviewed and deliberately left as-is, with the reasoning recorded in code.

- **Core row identity hook (fixes the root cause of v1.20.1)**. `webui/components/sidebar/chats/chat-tree.html` now renders `:data-context-id="context.id"` (plus `:data-parent-context-id`) on the same element that carries `.chat-container`, and `task-row.html` renders `:data-task-id="task.id"` on `.task-container`. v1.20.1 had to work around the missing hook by scraping Alpine's internal scope and probing four scope keys; that workaround is now the *fallback* path rather than the primary one, and a row resolves with no Alpine scope at all. A third-party row view that reuses the components inherits the hook for free; one that renders its own rows must publish an equivalent id. Core contracts live in `webui/components/sidebar/AGENTS.md` with a pytest guard in `tests/test_sidebar_row_actions.py::test_shared_rows_publish_their_model_id`; the injector gained a matching case in `tests/suite_sidebar.mjs` (now 23 assertions).
- **Config reads no longer depend on the hook (fixes the flagged hazard)**. `config.json` was 9 of 33 keys — safe only while the `get_plugin_config` hook in `hooks.py` stayed healthy, because every unwritten key otherwise fell back to a hard-coded `cfg.get(key, default)` that can drift from `config_defaults.DEFAULTS` (the exact hazard the hook was added to close). Two changes: (a) new `monitor._load_config()` re-applies `deep_merge_defaults()` locally, and the tick extension, `/status` and `/test_notify` all route through it, so a hook regression changes nothing; a *broken* framework read still yields a complete config from the shipped defaults; (b) the on-disk `config.json` is now fully merged (33/33, existing values preserved, verified idempotent), so the two paths cannot disagree even in principle. TEST36 (file completeness) and TEST37 (hookless read, override precedence, broken-read fallback) guard both halves. Dead `helpers.plugins` / `PLUGIN_NAME` imports removed from `api/status.py`, `api/nudge.py`, `api/test_notify.py`.
- **State lock across message delivery — reviewed, NOT changed**. The tick holds the state lock across `_nudge_context()` / `ctx.nudge()`, which looked inconsistent with `api/nudge.py` and `api/draft.py`, where `communicate()` deliberately runs outside the lock. Left as-is deliberately: delivery is non-blocking by the audited cross-thread contract (an alive task takes a plain `agent.intervention` attribute write; a dead one schedules onto the context's shared loop via DeferredTask), so the extra hold is microseconds; and the alternative — plan the sends, release the lock, deliver, then re-acquire it to apply the bookkeeping — splits one durable transition into two, so a crash in between would persist a tick claiming nudges that were never sent, and re-reading state in the second phase risks discarding the in-memory work the first phase built. That is a worse failure mode than a short lock hold. The reasoning now lives in the `tick()` docstring so the next reader does not mistake it for an oversight. TEST38 adds an end-to-end contention test: with the tick held inside delivery, a real `state_transaction_apply` handler thread must block, and afterwards neither the tick's delivery bookkeeping nor the manual action is lost. (The lock-hold invariant itself was already covered by TEST19D; removing the lock fails both.)
- **Tests**: 106 Python markers (TEST36-38 added) and `suite_sidebar.mjs` at 23 assertions. `suite_monitor.py` ALL_TESTS_PASSED, `suite_store.mjs` 12 assertions, `node --check` green on both plugin scripts, `py_compile` green on all plugin modules.

### v1.20.2 — Full-plugin audit: tick blast radius, shared-store lifecycle, log-copy cost (2026-09-26)

Whole-plugin read of every module (`helpers/*`, `api/*`, `webui/*`, the tick and both frontend suites). Five defects, each reproduced before fixing and covered by a new regression test.

- **One malformed state entry blinded the whole supervisor (high)**. `classify_chat` read `nudge_count` straight from `state.json` and compared it to an int: `"nudge_count": "3"` (hand-edit, legacy write, or a half-migrated file) raised `TypeError: '>=' not supported between instances of 'str' and 'int'`. The per-chat loop in `_tick_impl` had **no exception guard**, so the error escaped the entire tick — no nudges, no prune, no cap, no throttle snapshot, for *every* chat, with the only trace a `tick_fail` debug line. Reproduced directly against the pre-fix code. Three layers now: `monitor._as_int()` coerces any state counter without raising; `load_state()` drops non-dict chat entries at the load boundary (the shape that still breaks attribute access, and the one that also killed the unguarded nudge-budget reset loop); the per-chat loop body is wrapped so any remaining failure is caught, counted in `summary['chat_errors']`, and logged as `chat_tick_fail` instead of aborting the tick. The `repaired_entries` marker is transient and popped before persisting. TEST32 (both layers), TEST33.
- **Closing the dashboard froze the sidebar icons (high, WebUI)**. `webui/main.html` had `x-destroy="$store.chatShepherdStore.cleanup()"`, and `cleanup()` called `stopPolling()` on the store the **sidebar injector also depends on**. Open the dashboard, close it, and nothing ever re-polled `/status` again — the icons sat on stale data for the rest of the page session with no recovery short of a reload. `cleanup()` is now a documented no-op, the dashboard no longer tears the shared store down, and the sidebar's 2.5 s loop re-arms `startPolling()` if it ever sees `_started && !pollTimer`. Verified by reverting the fix: the new suite fails.
- **The "toast once" poll guard was dead code (medium, WebUI)**. `_fetchFailed` was declared `false`, assigned `false` on success, and read on failure — but never assigned `true`, so a wedged backend raised an error toast on *every* poll (every 5 s) even though the DOX claimed the opposite. It is now a proper latch, set after the check on both failure paths and cleared on recovery. TEST suite_store §1/§1b.
- **Every log scan copied the whole log under the agent's lock (medium, perf)**. `_last_log_type`, `_last_log_fatal`, `_scan_log_for_termination`, `_last_response_text` and `_last_entry_type` each did `list(context.log.logs)` **while holding `context.log._lock`** — copying every entry and blocking the agent's own log writer — and then inspected at most the last handful (one of them literally sliced `[-5:]` *after* the full copy). On a long-running chat this is 4 full copies per tick per chat, plus one more per `/status` poll per open tab. All five now slice a bounded `_LOG_TAIL = 50` under the lock; `_last_entry_type` reads the last item in place. Scans are unchanged for any log under 50 entries and still see a fatal stop pushed down by a few shepherd nudges. TEST34 guards the bounded slice structurally.
- **`/status` listed chats the shepherd refuses to touch (low, consistency)**. It enumerated *every* `AgentContext.all()` while `tick()` applied the real-chat predicate (8-alnum id + persisted `usr/chats/<id>` dir). Throwaway script contexts (`verify-*`, `ctx-hook-1`) therefore appeared in the dashboard and in the counts, and `/nudge` then refused them with "not a tracked chat" — the UI offered a row the backend rejected. `/status` now applies the same predicate, so the dashboard only shows what is actually supervised. TEST35.
- Also fixed in passing: `draftEdits` only ever grew, so a long dashboard session leaked one entry per draft (sent, dismissed, or aged out by the server-side `DRAFT_MAX`); `initDraftEdits()` now prunes entries for drafts that no longer exist while never clobbering an in-progress edit. TEST suite_store §4.
- **Tests**: +4 Python markers (TEST32-35) and a new `tests/suite_store.mjs` (12 assertions, dependency-free data-URL module harness like `_sidebar_folders`) covering the toast latch, the shared-poll contract, timer stacking and draft pruning. `suite_monitor.py` ALL_TESTS_PASSED, `suite_sidebar.mjs` 22 assertions, `suite_store.mjs` 12 assertions, `node --check` green on both plugin scripts, `py_compile` green on all plugin modules. The store suite was proven to fail against the pre-fix `cleanup()`. Plugin version 1.20.2 (plugin.yaml + this file).

### v1.16.0 — Supervised resume drafts (2026-09-15, R4)

- New opt-in `supervised_mode` (default off): `tick()` stops auto-sending and queues editable drafts for human review instead. `interrupted` chats (post-restart) queue a `restart_resume` draft; stalled + cooldown-eligible chats queue a `stall_nudge` draft with the classification reason embedded as `(situation: ...)`. Drafts never consume the nudge budget (the stalled-drain budget break is now supervised-aware, so `max_nudges_per_tick: 0` still queues drafts); with supervised mode off the behavior is unchanged (TEST17D).
- New `helpers/resume_draft.py` (pure, never raises): `draft_text(kind, reason)` builds the editable text, unknown kinds fall back to the shipped `RESUME_TEXT`; watched by hotreload.
- State layer: `drafts` list in state.json + per-chat `draft_dismissed_at`; `add_draft()` enforces one draft per chat, `DRAFT_MAX`=40 and the 30-minute dismiss requeue gate (`DRAFT_REQUEUE_MIN`); `remove_draft()`.
- New `api/draft.py`: `POST /api/plugins/chat_shepherd/draft` `{draft_id, action: send|dismiss, text?}` — send communicates the optionally edited text via the audited manual-nudge mechanics (`AgentContext.get` + `UserMessage`), bumps `nudge_count`, drops the draft, journals `draft_sent`; dismiss journals `draft_dismissed` and stamps the requeue gate. Context lookup goes through `_get_context` (test-patchable).
- WebUI: drafts review panel on the dashboard (textarea + Send/Dismiss per draft) with a drafts badge in `shepherd-store.js`; `supervised_mode` toggle in `config.html`; `/status` exposes `supervised_mode` + the `drafts` list.
- Tests +6 (TEST17A restart draft with nothing auto-sent, 17B stall draft, 17C drafts ignore a zero per-tick budget, 17D supervised-off legacy, 17E one-draft dedupe + requeue gate, 17F /draft send/dismiss/unknown-id) = 59 markers `ALL_TESTS_PASSED`; py_compile + `node --check` + framework-runtime imports green. api/* handlers activate on plugin refresh/restart per the v1.9.0 caveat.

## See also

- `plugin.yaml` — manifest; `default_config.yaml` — defaults incl. per-status `icons` map
- Framework references: `helpers/plugins.py` (`get_plugin_config` fallback + hooks), `helpers/api.py` (ApiHandler dispatch), `helpers/notification.py` (intervention notify)
## v1.17.1 (2026-09-15) — review pass: webui notifications + log retention

Full plugin audit (manifest/structure/patterns/security): PASS with 2 fixes shipped. Dashboard fetch/nudge/resolve/dismiss/draft/history failures previously rendered a persistent inline error div — replaced by the framework notification system (toastFrontendError), inline template + .shepherd-error css removed, store error field dropped (7 call sites, 16-point patch, node --check green). Poll-failure toasts guard on _fetchFailed so a wedged backend toasts once, not every 5s. helpers/monitor.py _debug_log now prunes daily logs beyond the newest 7 (probe-verified: 9→7 fixtures). hooks.py gains uninstall() removing debug logs only — state.json + config.json survive for reinstall continuity (live smoke: logs removed, STATE_PRESERVED). Advisories unchanged: LICENSE absent (community-only blocker), P7 read-modify-write race open. **(Both cleared in v1.18.0: LICENSE added in 47f0f76; RMW race fixed — reconciled 2026-09-18.)** Suite ALL_TESTS_PASSED (64 markers) on this tree; both modified modules import clean in the framework runtime.

## Technical audit 2026-10-04 — read-only review (no code changed)

Source-only audit of every module (`helpers/*`, `api/*`, `webui/*`, `hooks.py`, the
job_loop extension) against the framework contracts in `helpers/api.py`,
`helpers/plugins.py`, `webui/vendor/alpine/alpine.min.js` and the plugin's own
data files. **No code was modified** (the hot-reload watcher only watches
`helpers/*.py`, so nothing here can land in a live instance). Findings are
filed below for a future release; severities are impact-based, not effort-based.

Verification anchors used: framework input normalization
(`helpers/api.py:79-83` turns a non-dict JSON body into `{}` BEFORE `process`,
so the per-handler `isinstance(input, dict)` guards added in v1.20.7 are dead
code, while dict field VALUES are still passed through raw); `ApiHandler`
turns any handler exception into a plain-text 500 (`helpers/api.py:98-107`);
Alpine `x-for` clones only `content.firstElementChild`
(`webui/vendor/alpine/alpine.min.js`, x-for directive) — verified the
dashboard table keeps a single root, so the timeline panel is fine; live data
on this instance: 298 state entries / 295 KB `state.json`, 2779 journal lines /
379 KB, 345 chat dirs under `usr/chats`.

### Defects

- **A1 (medium-high) — state counters are only coerced inside `classify_chat`.**
  v1.20.2 added `monitor._as_int` and claimed it coerces "any state counter
  without raising", but it was wired into `classify_chat` only. Every other
  read of a persisted counter is raw: `monitor.py:631` and `:656`
  (`entry.get('nudge_count', 0) <= 0`), `:1247` / `:1319` (`nudge_count <
  max_nudges`), `:1353` / `:1399` / `:1428`, `:1354` / `:1400`
  (`nudges_sent ... + 1`), `:1441` / `:1477` (`wedge_nudges_sent ... + 1`),
  plus `api/nudge.py:139`, `api/draft.py:181` and the whole effectiveness block
  in `api/status.py:190-206`. A string counter in `state.json` (hand-edit,
  legacy write, half-migrated file) raises `TypeError` with two different
  blast radii:
  - inside the per-chat guard (`:631`, `:1247`, `:1319`, `:1267`): that one chat
    is silently unsupervised for the tick and again on every following tick —
    permanently, with no visible signal. `classify_chat` still reports
    `Stalled for Nm, nudge 3/3 used` (it coerces), so the dashboard shows a
    stalled chat with budget left while the drain refuses to queue it.
  - inside the drains (`:1354`, `:1400`, `:1441`, `:1477`): those run after the
    per-chat loop with NO guard, so `tick()` dies whole — no nudges, no prune,
    no cap, no throttle snapshot for ANY chat, with only a `tick_fail` line in
    the rotating debug log. This is precisely the failure v1.20.7 fixed for
    config knobs (`_as_float`) and v1.20.2 fixed for one state field; the class
    is still open.
  - in `api/nudge.py` the exception escapes `state_transaction_apply` AFTER
    `ctx.communicate()` already delivered the message: the operator gets a 500
    although the nudge went out, and no counter or journal row is written.
  - in `api/status.py` one bad counter 500s the whole dashboard/sidebar poll.
  Fix: coerce at the load boundary (`load_state` normalizes every counter
  field, the way it already drops non-dict entries), and use `_to_int` /
  `_as_int` at each read. Add a suite marker per counter field.
- **A2 (medium) — `/status` has no top-level guard.** `_snapshot_sync` wraps
  per-chat classification (`api/status.py:124-132`) but not the effectiveness,
  adaptive, throttle or return-building code, so any exception above becomes a
  500 for the sidebar icons and the dashboard. With A1 present this is a live
  path. Wrap the body (or each block) and degrade to a partial payload.
- **A3 (low-medium) — uncoerced `chat_id` in `/nudge` and `/resolve`.**
  `api/nudge.py:87-88` and `api/resolve.py:19-20` do
  `(body.get('chat_id') or '').strip()`; a JSON body with `{"chat_id": 123}`
  (or a numeric `text`/`action`) raises `AttributeError` → 500. `draft.py`,
  `history.py` and `test_notify.py` already use `str(...)`. `resolve.py` also
  does not validate `action`: anything other than `dismiss` silently resolves.
- **A4 (low-medium) — `adaptive.configured_values()` was missed by the v1.20.7
  coercion fix.** `helpers/adaptive.py:89-98` uses bare
  `float(cfg.get(k, 10) or 10)` on the three timing knobs. A hand-edited
  `"nudge_cooldown_minutes": "abc"` raises inside `evaluate()` (the tick catches
  it and silently degrades the whole overlay to `adaptive_tag='error'`) and
  inside `/status` (caught, and the entire `effectiveness.adaptive` reflection
  disappears). `or 10` also turns a legitimate `0` into `10`. Reuse
  `monitor._as_float` (or a local clone) instead.
- **A5 (low-medium) — `/config` writes on the event loop.** v1.20.5 offloaded
  both read seams, but `plugins_helper.save_plugin_config(...)` and
  `clear_plugin_cache(...)` still run inline (`api/config.py:111-119`). The
  save hook does `find_plugin_assets` + a JSON read + a write, so one slow 9p
  write stalls every request in the server — the exact failure TEST41 exists to
  prevent, pinned only for the read branches. Move the save into the same
  `asyncio.to_thread` body.
- **A6 (low) — `capped` misreports in supervised mode.** `helpers/monitor.py`
  `:1574-1583` ORs `max_nudges_per_tick <= 0` into `capped` unconditionally, so
  with `supervised_mode: true` and `max_nudges_per_tick: 0` — where the stall
  queue is drained into editable drafts and nothing is suppressed (v1.16.0 made
  supervised mode explicitly budget-free) — the dashboard shows a red
  "capped" badge. Gate the budget branch on `not supervised`.
- **A7 (low) — `pre_drain_nudged` is dead code.** `summary['nudged']` is only
  incremented at `:1406`, inside the stalled drain, so the snapshot taken at
  `:1371` is always `0` and `_stalled_delivered` is just `summary['nudged']`.
  Vestige of the pre-v1.20.7 shared budget; delete it or make it meaningful.
- **A8 (low-medium) — `api/*` pin pre-reload `state` functions while the rest
  of those files use module-attr access.** `api/status.py:10`,
  `api/export.py:10` and `api/history.py:8` do
  `from ...state import load_state, read_journal`, so after a
  `helpers/state.py` hot-reload those endpoints keep running the OLD function
  bodies while the tick runs the new ones — `api/status.py` mixes both
  generations in a single request (pinned `load_state()` at `:94`,
  `state_mod.read_journal()` at `:234`). Use `state_mod.load_state` /
  `state_mod.read_journal` so the v1.9.0 hot-reload contract holds for the
  endpoints too. This also makes the "api/* handlers keep their from-import
  bindings" limitation in *Known limitations* mostly stale: the remaining
  pinned names are only these three.
- **A9 (low, latent) — `_notify(cfg=None)` bypasses the config seam.**
  `helpers/monitor.py:615-619` calls `plugins.get_plugin_config` directly,
  against the v1.20.3 "no runtime path may call it directly" rule. Both current
  call sites pass `cfg=cfg`, so it is dead — but a future caller would get a
  partial config with no `webhook_url`/`telegram_bot_token` and silently lose
  all external alerting. Call `_load_config()` in the fallback.
- **A10 (low) — the store's `timelines` map grows without bound.**
  `webui/shepherd-store.js:254` caches a full `/history` payload per chat id
  and never prunes. This is the same leak class v1.20.2 fixed for
  `draftEdits` (`initDraftEdits`) and was missed for timelines.
- **A11 (low-medium) — `/status` polls can stack.** `fetchStatus` runs on
  `setInterval` with no in-flight guard, so a slow snapshot (journal read +
  aggregates + per-chat log scans) lets requests pile up. Use a
  self-scheduling `setTimeout` or an `_inFlight` latch.
- **A12 (low) — the settings modal issues a full `/status` snapshot on open.**
  `webui/config.html:306` calls the heaviest endpoint just to render two
  counts; on top of the dashboard poll that doubles the snapshot cost whenever
  the settings card mounts.
- **A13 (low-medium) — the sidebar row-id walk is unbounded.**
  `webui/shepherd-sidebar.js:149-153` climbs to `<html>` looking for
  `data-folder-thread` / `data-context-id` / `data-chat-id`. A third-party row
  view that publishes no id inside a wrapper that does will inherit the
  ANCESTOR's id and be painted with another chat's status. Core rows are safe
  (`sidebar/chats/chat-tree.html` publishes `:data-context-id` on the
  `.chat-container` element itself, so the walk stops on the first hop); bound
  the walk to the row boundary anyway.
- **A14 (low) — `injectBadges` swallows errors with no trace**
  (`webui/shepherd-sidebar.js:275-277`). A broken injector would be invisible,
  against the framework rule that a failed hook must be reported.
- **A15 (low) — the test suite writes into the live production data dir.**
  `tests/suite_monitor.py` redirects `state_mod.STATE_FILE` /
  `JOURNAL_FILE` (`:114-120`) but never `monitor._debug_log`, so every run
  injects test entries into `usr/plugins/chat_shepherd/data/debug-*.log` —
  confirmed in the shipped logs (`hot_reload reloaded cs_hr_test_mod`,
  `auto_nudge_fail ... cs_nudge_fail_probe`, `chat_tick_fail csTstA01`) — and
  its newest-7 rotation can push a real day out of the window. It also still
  creates real `usr/chats/csTstA01..C` directories (the known P6.2 deferral;
  they match `CHAT_ID_PATTERN`, so a live tick tracks them), and it
  `os.chdir(_REPO_ROOT)`s at import. Stub `_debug_log` in `main()` the way
  `_post_json` is stubbed.

### Optimization opportunities

- **O1 (high) — `read_journal` re-parses the whole journal after every
  append.** `helpers/state.py:320-373` keys its cache on
  `(st_mtime_ns, st_size)`, and every `append_journal` changes both, so the
  `/status` poll (every `poll_seconds`, per open tab) re-reads and re-parses
  the entire file — currently 379 KB / 2779 lines, up to `JOURNAL_MAX_BYTES`
  1 MB. Cache `(mtime_ns, size, byte_offset, parsed)` and parse only the
  appended tail (the per-line `try/except` already tolerates a torn last
  line). This is the single largest win in the plugin.
- **O2 (medium-high) — aggregates are recomputed per poll.**
  `api/status.py:233-235` runs `aggregates.compute(read_journal(JOURNAL_KEEP))`
  — up to 1000 entries parsed, filtered and sorted on every poll. Cache the
  block by the journal stat key; recompute only when it changes.
- **O3 (medium) — `state_signature` is computed 2-3x per save.**
  `state_transaction_apply` computes it before and after the apply-fn,
  `save_state` computes it a third time, and every `load_state` computes it
  once more. Each call is `json.dumps` of ~226 KB plus a sha256. Compute once
  per operation and pass it into `save_state(state, sig=...)`.
- **O4 (medium) — `save_state` serializes with `indent=2`.**
  `state.json` is 295 KB today; compact separators would cut roughly 40% of the
  bytes written on every durable change. This is a machine-managed file —
  `config.json` is the hand-edited one.
- **O5 (medium) — up to 8 log-lock acquisitions and 5 tail copies per chat per
  pass.** `_last_log_type` alone runs TWICE per chat per tick
  (`classify_chat` at `:799` and `update_chat` at `:1205`), plus
  `_last_log_fatal`, `_scan_log_for_termination`, `_last_response_text`,
  `_last_entry_type`, `_log_len`, `_log_updates` — each taking
  `context.log._lock` and slicing a 50-item tail, again for every `/status`
  poll. Take one snapshot per chat per pass (single lock, single slice) and
  pass it to the scanners; the v1.20.2 fix bounded the copy, it did not
  deduplicate it.
- **O6 (medium) — `state.json` is no longer size-bounded.**
  `MAX_TRACKED_CHATS = 100` bounds only the DEAD surplus after the v1.20.0
  live-protection fix, so the dict grows with the number of live contexts: this
  instance carries 298 entries / 295 KB (278 still have a chat dir, 20 are
  already prunable). Every poll pays the read + signature and every durable
  save pays a 295 KB rewrite. Options: lower the dead surplus cap, split cold
  bookkeeping out of the hot file, or store a compact per-chat record. At
  minimum, state in *Known limitations* that file size scales with live
  contexts, not with `MAX_TRACKED_CHATS`.
- **O7 (low-medium) — `/draft` reads state twice for nothing.**
  `api/draft.py:86` does a full `load_state()` peek before every action, then
  the transaction reads state again (`:150`, and a third time for the
  bookkeeping apply). The claim transaction already returns the draft with its
  `chat_id`, so the peek is redundant: drop it, or keep only a cheap 404 fast
  path via `state.read_chat_entry`.
- **O8 (medium) — the journal parse runs under `_journal_lock`.**
  `helpers/state.py:323-361` holds the lock across the whole file read and
  parse. The tick holds `_state_lock` while appending history, so a slow parse
  (or slow 9p read) stalls the tick and, through it, every API transaction.
  The file is append-only and the parse is idempotent — do it outside the lock.
- **O9 (low) — `_debug_log` globs the data dir on every call** and imports
  `glob`/`os` inside the function (`helpers/monitor.py:394-418`). Rotate at
  most once per day (the timestamp is already computed there) and hoist the
  imports.
- **O10 (low) — `_NAME_CACHE` flushes completely at 120 entries**
  (`helpers/monitor.py:465-466`). With 345 chats that means repeated full
  clears plus re-reads of `chat.json`. Evict the oldest key instead
  (`OrderedDict.move_to_end` + `popitem(last=False)`).
- **O11 (low) — the sidebar repaint re-resolves every row from scratch.**
  `paintRows` does a page-wide `querySelectorAll('.chat-container')` and then,
  per row, an ancestor walk, `Alpine.$data`, two `querySelector` calls in
  `anchorFor` and one `.cs-badge` lookup — with ~300 rows, every 2.5 s plus
  debounced mutation passes. Cache `container -> {chatId, host, badge}` in a
  `WeakMap` and skip rows whose `data-cs-status` already matches.
- **O12 (low) — redundant `state_mod.get_chat` re-lookups** at
  `helpers/monitor.py:1352`, `:1398` and `:1427` fetch the very dict the caller
  already holds (`entry`), purely to re-read the same object.
- **O13 (low) — hot-reload mtime uses `st_mtime` (float).**
  `helpers/hotreload.py:158` — use `st_mtime_ns` so two saves inside one
  filesystem mtime tick cannot be missed.
- **O14 (low, optional) — `/status` re-classifies every context per poll**
  although the tick classified them at most `job_loop`-interval ago. The
  freshness is deliberate; only the cheap statuses need recomputing, and the
  wedge clock could be served from the state entry.

### Accepted risks worth recording (no change proposed)

- `/config` returns `telegram_bot_token` in plaintext to any authenticated,
  CSRF-valid caller (needed because the settings modal binds `config.*`).
  `/status` correctly omits secrets; the asymmetry is deliberate but should be
  stated rather than discovered.
- `resolve action=dismiss` only deletes the state entry; a live chat is
  re-created by the next tick, so dismiss is transient for live contexts.
- The per-handler `isinstance(input, dict)` guards (v1.20.7) duplicate
  `helpers/api.py:79-83`; they are harmless but the v1.20.7 changelog entry
  overstates what the framework left to the plugin.
