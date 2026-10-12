/*
 * Chat Shepherd — offline regression suite for webui/shepherd-store.js.
 *
 * The store is SHARED by the sidebar badge injector and the dashboard modal,
 * which is where its bugs lived:
 *
 *   - the "toast once" poll-failure guard was dead code (_fetchFailed was
 *     only ever assigned false, so a wedged backend toasted every 5s);
 *   - the dashboard's x-destroy called cleanup(), which stopped the shared
 *     poll and froze the sidebar icons on stale data for the page session;
 *   - startPolling() could stack timers;
 *   - draftEdits only ever grew.
 *
 * Run: node usr/plugins/chat_shepherd/tests/suite_store.mjs
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInThisContext } from "node:vm";

const SOURCE = await readFile(
  new URL("../webui/shepherd-store.js", import.meta.url),
  "utf8",
);

let checks = 0;
function ok(condition, message) { assert.ok(condition, message); checks += 1; }

// Track live timers so "no stacked timers" is observable. v1.21.0 replaced
// setInterval with a self-scheduling setTimeout chain plus an in-flight latch
// (a slow snapshot must not queue another poll behind itself), so both timer
// kinds are shimmed here. Real timers would keep the Node process alive, so
// every block stops polling.
const intervals = { created: 0, live: new Set() };
const realSetInterval = globalThis.setInterval;
const realClearInterval = globalThis.clearInterval;
const realSetTimeout = globalThis.setTimeout;
const realClearTimeout = globalThis.clearTimeout;
globalThis.setInterval = (fn, ms) => {
  const t = realSetInterval(fn, ms);
  intervals.created += 1;
  intervals.live.add(t);
  return t;
};
globalThis.clearInterval = (t) => {
  intervals.live.delete(t);
  return realClearInterval(t);
};
globalThis.setTimeout = (fn, ms) => {
  const t = realSetTimeout(fn, ms);
  intervals.created += 1;
  intervals.live.add(t);
  return t;
};
globalThis.clearTimeout = (t) => {
  intervals.live.delete(t);
  return realClearTimeout(t);
};

// The store is an ES module, so load it as one. Each load gets a unique URL
// so `import` does not hand back a cached module with stale closures.
let loadSeq = 0;
async function loadStore(overrides = {}) {
  const toasts = [];
  const apiCalls = [];
  const dialogs = [];
  globalThis.__storeTest = {
    toasts,
    apiCalls,
    dialogs,
    api: overrides.api || (() => ({ success: true })),
  };
  const header = `
    const toasts = globalThis.__storeTest.toasts;
    const apiCalls = globalThis.__storeTest.apiCalls;
    const dialogs = globalThis.__storeTest.dialogs;
    const createStore = (_name, model) => model;
    const callJsonApi = async (url, body) => {
      apiCalls.push([url, body]);
      return globalThis.__storeTest.api(url, body);
    };
    const toastFrontendError = (message, title) => { toasts.push({ kind: 'error', message, title }); };
    const toastFrontendSuccess = (message, title) => { toasts.push({ kind: 'success', message, title }); };
    const showConfirmDialog = async (options) => {
      dialogs.push(options);
      return globalThis.__storeTest.confirmAnswer !== false;
    };
  `;
  // Strip the framework imports (unresolvable in a data: URL) and the ESM
  // export keywords; the stubs above stand in for them.
  const body = SOURCE
    .split("\n")
    .filter((line) => !/^\s*import\s/.test(line))
    .join("\n")
    .replace(/^export /gm, "");
  const url = "data:text/javascript;base64," + Buffer.from(
    header + body
      // exports were stripped above; re-publish what the suite needs
      + "\nglobalThis.__storeModule = { store: store, DEFAULT_ICONS: DEFAULT_ICONS };\n"
      + "//#" + (++loadSeq),
  ).toString("base64");
  await import(url);
  return {
    store: globalThis.__storeModule.store,
    toasts,
    apiCalls,
    dialogs,
    setConfirmAnswer: (v) => { globalThis.__storeTest.confirmAnswer = v; },
  };
}

const OK_RESP = () => ({
  success: true, chats: [], counts: {}, config: { poll_seconds: 5 }, history: [],
});

// 1. Poll failures toast exactly once, then stay quiet until recovery.
{
  const { store, toasts } = await loadStore({
    api: () => ({ success: false, error: "boom" }),
  });
  for (let i = 0; i < 5; i++) await store.fetchStatus();
  ok(toasts.length === 1, "five failed polls must raise exactly one toast, got " + toasts.length);
  ok(toasts[0].message === "boom", String(toasts[0].message));
  ok(store._fetchFailed === true, "the latch must be set after a failure");
  store.stopPolling();
}

// 1b. A recovered poll clears the latch, so a NEW outage reports again.
{
  let fail = false;
  const { store, toasts } = await loadStore({
    api: () => (fail ? { success: false, error: "again" } : OK_RESP()),
  });
  await store.fetchStatus();
  ok(store._fetchFailed === false, "a successful poll must clear the latch");
  fail = true;
  await store.fetchStatus();
  await store.fetchStatus();
  await store.fetchStatus();
  ok(toasts.length === 1, "a fresh outage must report exactly once, got " + toasts.length);
  store.stopPolling();
}

// 2. cleanup() must NOT stop the shared poll (the sidebar-freeze regression).
{
  const { store } = await loadStore({ api: OK_RESP });
  store.startPolling();
  assert.ok(store.pollTimer, "precondition: polling is running");
  store.cleanup();
  ok(store.pollTimer, "cleanup() must not stop the shared poll - the sidebar needs it");
  store.stopPolling();
  ok(store.pollTimer === null, "stopPolling() still stops it explicitly");
}

// 3. startPolling() is idempotent - repeated onOpen() must not stack timers.
{
  const { store } = await loadStore({ api: OK_RESP });
  const before = intervals.live.size;
  store.onOpen();
  assert.equal(intervals.live.size, before + 1, "precondition: onOpen started one poll");
  store.onOpen();
  store.onOpen();
  ok(intervals.live.size === before + 1,
    "repeated onOpen() must not stack poll timers, live=" + intervals.live.size);
  store.stopPolling();
  ok(intervals.live.size === before, "stopPolling leaves no live timer behind");
}

// 4. draftEdits keeps live edits and prunes dead ones.
{
  const resp = () => ({
    success: true, chats: [], counts: {}, config: { poll_seconds: 5 }, history: [],
    drafts: [{ id: "keep1", text: "server text" }, { id: "keep2", text: "server text 2" }],
  });
  const { store } = await loadStore({ api: resp });
  await store.fetchStatus();
  store.draftEdits.keep1 = "my edited text";     // user edit must survive
  store.draftEdits.gone1 = "stale";              // draft no longer exists
  store.draftEdits.gone2 = "stale";
  assert.equal(Object.keys(store.draftEdits).length, 4, "precondition: two live + two stale edits");
  await store.fetchStatus();
  ok(store.draftEdits.keep1 === "my edited text", "an in-progress edit must never be clobbered");
  ok(store.draftEdits.keep2 === "server text 2", "new drafts are seeded from the server text");
  ok(!("gone1" in store.draftEdits) && !("gone2" in store.draftEdits),
    "edits for removed drafts must be pruned");
  store.stopPolling();
}

// 5. A guarded nudge asks before it restarts an errored chat (v1.20.4).
{
  const responses = [
    { success: true, needs_confirmation: true, guard: "fatal_stop",
      warning: "This chat stopped on an error or a security termination." },
    { success: true, overrode_guard: true, nudge_count: 1 },
  ];
  let i = 0;
  const { store, apiCalls, dialogs } = await loadStore({ api: () => responses[i++] });
  await store.nudge("chat0001");
  ok(dialogs.length === 1, "a guarded nudge must ask for confirmation");
  ok(dialogs[0].type === "danger", "the dialog is a danger confirmation");
  ok(/stopped on an error/.test(dialogs[0].message),
    "the dialog must show the backend's reason");
  const nudges = apiCalls.filter(([url]) => url.endsWith("/nudge"));
  ok(nudges.length === 2, "confirming re-issues the request with force");
  ok(nudges[0][1].force === false, "the first attempt must not force");
  ok(nudges[1][1].force === true, "the confirmed attempt must force");
  store.stopPolling();
}

// 5b. Cancelling the confirmation sends nothing.
{
  const responses = [
    { success: true, needs_confirmation: true, guard: "fatal_stop", warning: "boom" },
  ];
  let i = 0;
  const { store, apiCalls, dialogs, setConfirmAnswer } = await loadStore({
    api: () => responses[i++],
  });
  setConfirmAnswer(false);
  await store.nudge("chat0001");
  ok(dialogs.length === 1, "the confirmation was shown");
  const nudges = apiCalls.filter(([url]) => url.endsWith("/nudge"));
  ok(nudges.length === 1, "cancelling must NOT re-issue a forced nudge");
  store.stopPolling();
}

// 5c. An ordinary nudge never opens a dialog and never forces.
{
  const { store, apiCalls, dialogs } = await loadStore({
    api: () => ({ success: true, nudge_count: 1 }),
  });
  await store.nudge("chat0001");
  ok(dialogs.length === 0, "a healthy chat nudges without a confirmation");
  const nudges = apiCalls.filter(([url]) => url.endsWith("/nudge"));
  ok(nudges.length === 1, "exactly one request");
  ok(nudges[0][1].force === false, "and it is not forced");
  store.stopPolling();
}

// 6. The nudge tool-tip advertises the confirmation on errored rows only.
{
  const { store } = await loadStore();
  ok(/ask for confirmation/.test(store.nudgeHint({ status: "error" })),
    "an errored chat warns before the click");
  ok(!/ask for confirmation/.test(store.nudgeHint({ status: "stalled" })),
    "a merely stalled chat does not warn");
  ok(!/ask for confirmation/.test(store.nudgeHint(null)),
    "a missing chat does not warn");
  store.stopPolling();
}

// 7. Overlapping polls are dropped (v1.21.0 A11). setInterval used to fire on
//    schedule regardless of whether the previous snapshot had landed, so a slow
//    backend queued concurrent /status requests - each contending for the state
//    lock - and the sidebar icons showed a snapshot several generations old.
{
  const pending = [];
  const slow = () => new Promise((r) => { pending.push(r); });
  const { store, apiCalls } = await loadStore({ api: slow });
  const first = store.fetchStatus();
  ok(store._inFlight === true, "a poll in progress must latch _inFlight");
  const second = store.fetchStatus();
  await second;
  ok(apiCalls.length === 1,
    "a second poll while one is in flight must be dropped, saw " + apiCalls.length);
  pending[0](OK_RESP());
  await first;
  ok(store._inFlight === false, "the latch must clear when the poll lands");
  const third = store.fetchStatus();
  pending[pending.length - 1](OK_RESP());
  await third;
  ok(apiCalls.length === 2, "polling resumes once the latch is clear");
  store.stopPolling();
}

// 8. Re-arming from inside a poll must not leave an orphan chain (A11).
//    applyPollFromConfig() calls startPolling() when poll_seconds changes,
//    which happens WHILE fetchStatus() is awaiting: without the generation
//    guard the in-flight chain would reschedule itself on top of the fresh one.
{
  let answers = [
    { success: true, chats: [], counts: {}, config: { poll_seconds: 5 }, history: [] },
    { success: true, chats: [], counts: {}, config: { poll_seconds: 9 }, history: [] },
  ];
  const { store } = await loadStore({ api: () => answers.shift() || OK_RESP() });
  store.startPolling();
  const before = intervals.live.size;
  await store.fetchStatus();
  await store.fetchStatus();
  ok(store.pollInterval === 9000, "the poll interval follows the config");
  ok(intervals.live.size === before,
    "a poll_seconds change must leave exactly one live timer, live=" + intervals.live.size);
  store.stopPolling();
}

// 9. A degraded snapshot is surfaced once and never blanks the dashboard (A2).
//    The backend now answers with a valid payload instead of a 500, so the
//    store must tell the operator once rather than silently keeping stale data.
{
  let degraded = true;
  const { store, toasts } = await loadStore({
    api: () => (degraded
      ? { success: true, degraded: true, error: "division by zero", chats: [], counts: {}, config: { poll_seconds: 5 }, history: [] }
      : OK_RESP()),
  });
  await store.fetchStatus();
  await store.fetchStatus();
  ok(toasts.length === 1, "a degraded snapshot must toast once, got " + toasts.length);
  ok(/degraded/i.test(toasts[0].message), String(toasts[0].message));
  ok(store.data === null, "a degraded payload must not overwrite good state with blanks");
  degraded = false;
  await store.fetchStatus();
  ok(store._fetchFailed === false, "recovery clears the latch");
  store.stopPolling();
}

// 10. timelines is pruned to the tracked chat set (A10) - the draftEdits leak
//     class, missed for the per-chat timeline payloads.
{
  // NB: read the outer `ids` at CALL time - passing it as a parameter would
  // capture the first array and the reassignment below would be invisible.
  const withChats = () => () => ({
    success: true, chats: ids.map((id) => ({ chat_id: id, status: "stalled" })),
    counts: {}, config: { poll_seconds: 5 }, history: [],
  });
  let ids = ["chat0001", "chat0002", "chat0003"];
  const { store, apiCalls } = await loadStore({ api: withChats() });
  store.timelines = { chat0001: { entries: [] }, chat0002: { entries: [] }, chat0003: { entries: [] } };
  await store.fetchStatus();
  await store.toggleTimeline("chat0001");
  ok(store.timelines.chat0001, "the opened timeline is cached");
  ok(Object.keys(store.timelines).length === 3,
    "tracked chats keep their cache, got " + Object.keys(store.timelines).length);
  ids = ["chat0001"];
  await store.fetchStatus();
  store.timelineOpen = "";            // close, so the next toggle re-opens
  await store.toggleTimeline("chat0001");
  const keys = Object.keys(store.timelines);
  ok(!("chat0002" in store.timelines) && !("chat0003" in store.timelines),
    "timelines for untracked chats must be dropped, left: " + keys.join(","));
  ok("chat0001" in store.timelines, "the chat being viewed must survive the prune");
  ok(apiCalls.some(([url]) => url.endsWith("/history")), "the timeline still loads history");
  store.stopPolling();
}

console.log(`Shared store checks passed (${checks} assertions).`);
