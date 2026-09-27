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

// Track live intervals so "no stacked timers" is observable. A real
// setInterval keeps the Node process alive, so every block stops polling.
const intervals = { created: 0, live: new Set() };
const realSetInterval = globalThis.setInterval;
const realClearInterval = globalThis.clearInterval;
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

// The store is an ES module, so load it as one. Each load gets a unique URL
// so `import` does not hand back a cached module with stale closures.
let loadSeq = 0;
async function loadStore(overrides = {}) {
  const toasts = [];
  const apiCalls = [];
  globalThis.__storeTest = {
    toasts,
    apiCalls,
    api: overrides.api || (() => ({ success: true })),
  };
  const header = `
    const toasts = globalThis.__storeTest.toasts;
    const apiCalls = globalThis.__storeTest.apiCalls;
    const createStore = (_name, model) => model;
    const callJsonApi = async (url) => {
      apiCalls.push(url);
      return globalThis.__storeTest.api(url);
    };
    const toastFrontendError = (message, title) => { toasts.push({ message, title }); };
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
  return { store: globalThis.__storeModule.store, toasts, apiCalls };
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

console.log(`Shared store checks passed (${checks} assertions).`);
