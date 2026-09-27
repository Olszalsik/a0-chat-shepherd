/*
 * Chat Shepherd — offline regression suite for webui/shepherd-sidebar.js.
 *
 * Guards the sidebar row-resolution contract that broke when Agent Zero moved
 * chat rows into the shared `chat-tree.html` component and let plugins
 * (notably `_sidebar_folders`) render alternative list views:
 *
 *   - default flat list rows (Alpine loop scope `context` on the <li>)
 *   - folder-view rows (wrapper x-data `context`, x-for scope `item`, and the
 *     published `data-folder-thread` attribute)
 *   - nested child rows (`child` scope)
 *   - badge placement next to the chat name, ahead of the hover-only actions
 *   - status changes that keep the same pictogram still repaint colour/tooltip
 *   - idle rows and blank icon settings clear the badge
 *   - re-running the extension script does not stack timers or observers
 *
 * Run: node usr/plugins/chat_shepherd/tests/suite_sidebar.mjs
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { runInThisContext } from "node:vm";

const SOURCE = await readFile(
  new URL("../webui/shepherd-sidebar.js", import.meta.url),
  "utf8",
);

/* ------------------------------------------------------------------ *
 * Minimal DOM: only the selectors and node APIs the injector uses.    *
 * ------------------------------------------------------------------ */

function matchOne(el, selector) {
  if (!selector) return false;
  if (selector.startsWith("#")) return el.getAttribute("id") === selector.slice(1);
  if (selector.startsWith(".")) return el.classList.contains(selector.slice(1));
  if (selector.startsWith("[")) return el.getAttribute(selector.slice(1, -1)) !== null;
  return el.tagName === selector.toUpperCase();
}

class ClassList {
  constructor(el) { this.el = el; }
  get _list() { return (this.el.getAttribute("class") || "").split(/\s+/).filter(Boolean); }
  contains(name) { return this._list.includes(name); }
}

class Text {
  constructor(text) { this.nodeType = 3; this.data = String(text); this.parentNode = null; }
  get textContent() { return this.data; }
  set textContent(v) { this.data = String(v); }
}

class Element {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.nodeType = 1;
    this.attributes = new Map();
    this.childNodes = [];
    this.parentNode = null;
    this.style = { cssText: "" };
    this.classList = new ClassList(this);
    this.dataset = new Proxy({}, {
      get: (_t, key) => this.getAttribute(
        "data-" + String(key).replace(/[A-Z]/g, (c) => "-" + c.toLowerCase()),
      ),
    });
  }
  get parentElement() {
    return this.parentNode && this.parentNode.nodeType === 1 ? this.parentNode : null;
  }
  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  get lastElementChild() { const k = this.children; return k[k.length - 1] || null; }
  get nextSibling() {
    if (!this.parentNode) return null;
    const i = this.parentNode.childNodes.indexOf(this);
    return this.parentNode.childNodes[i + 1] || null;
  }
  get isConnected() { return isConnected(this); }
  get className() { return this.getAttribute("class") || ""; }
  set className(v) { this.setAttribute("class", v); }
  get title() { return this.getAttribute("title") || ""; }
  set title(v) { this.setAttribute("title", v); }
  get textContent() { return this.childNodes.map((n) => n.textContent).join(""); }
  set textContent(v) {
    this.childNodes = [];
    if (v !== "") this.appendChild(new Text(v));
  }
  getAttribute(name) { const v = this.attributes.get(name); return v === undefined ? null : v; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  appendChild(node) {
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this; this.childNodes.push(node); return node;
  }
  insertBefore(node, ref) {
    if (!ref) return this.appendChild(node);
    if (node.parentNode) node.parentNode.removeChild(node);
    const i = this.childNodes.indexOf(ref);
    node.parentNode = this;
    this.childNodes.splice(i < 0 ? this.childNodes.length : i, 0, node);
    return node;
  }
  removeChild(node) {
    const i = this.childNodes.indexOf(node);
    if (i >= 0) this.childNodes.splice(i, 1);
    node.parentNode = null; return node;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  contains(node) {
    for (let n = node; n; n = n.parentNode) if (n === this) return true;
    return false;
  }
  matches(selector) { return selector.split(",").some((s) => matchOne(this, s.trim())); }
  closest(selector) {
    for (let n = this; n && n.nodeType === 1; n = n.parentNode) {
      if (n.matches(selector)) return n;
    }
    return null;
  }
  querySelectorAll(selector) {
    const out = [];
    const walk = (node) => {
      for (const child of node.children) {
        if (child.matches(selector)) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

function isConnected(node) {
  for (let n = node; n; n = n.parentNode) if (n === globalThis.document) return true;
  return false;
}

/* ---------------------------------------------------------------- *
 * Build a sidebar row exactly as Agent Zero renders it today.       *
 * ---------------------------------------------------------------- */

function buildRow({ scopeOwner, wrapperScope = null, attrs = {}, hasChildren = false }) {
  // <li> possibly carrying the x-for scope and the list-view attributes.
  const li = new Element("li");
  li.setAttribute("class", "chat-tree-item");
  for (const [k, v] of Object.entries(attrs)) li.setAttribute(k, v);
  if (scopeOwner) li._x_dataStack = [scopeOwner];

  // Optional wrapper x-data="{ get context() { return item; } }", used by
  // the _sidebar_folders list view.
  const scopeHost = new Element("div");
  scopeHost.setAttribute("class", "sidebar-folder-host");
  if (wrapperScope) scopeHost._x_dataStack = [wrapperScope];

  const host = new Element("x-component");
  const row = new Element("div");
  row.setAttribute("class", "sidebar-row-content");

  const container = new Element("div");
  container.setAttribute("class", "chat-container");

  if (hasChildren) {
    const expand = new Element("button");
    expand.setAttribute("class", "chat-expand-btn is-visible");
    container.appendChild(expand);
  }

  const label = new Element("div");
  label.setAttribute("class", "chat-list-button");
  const ball = new Element("span");
  ball.setAttribute("class", "project-color-ball");
  const name = new Element("span");
  name.setAttribute("class", "chat-name");
  name.textContent = "chat name";
  label.appendChild(ball);
  label.appendChild(name);

  const closeBtn = new Element("button");
  closeBtn.setAttribute("class", "btn-icon-action chat-list-action-btn");
  const moreBtn = new Element("button");
  moreBtn.setAttribute("class", "btn-icon-action chat-list-action-btn");

  container.appendChild(label);
  container.appendChild(closeBtn);
  container.appendChild(moreBtn);

  row.appendChild(container);
  host.appendChild(row);
  scopeHost.appendChild(host);
  li.appendChild(scopeHost);
  return { li, container, label, closeBtn, moreBtn };
}

function badgeIn(row) { return row.container.querySelector(".cs-badge"); }

function statusData(pairs, icons = {}) {
  return {
    chats: pairs.map(([chat_id, status]) => ({ chat_id, status })),
    config: { icons },
  };
}

function installEnv({ data } = {}) {
  const document = new Element("html");
  document.readyState = "complete";
  const body = new Element("body");
  document.appendChild(body);

  const chatsSection = new Element("div");
  chatsSection.setAttribute("id", "chats-section");
  body.appendChild(chatsSection);

  document.querySelector = (sel) => document.querySelectorAll(sel)[0] || null;
  document.createElement = (tag) => new Element(tag);
  document.addEventListener = () => {};
  document.contains = (node) => isConnected(node);

  const counts = { observers: 0, intervals: 0 };
  class FakeObserver {
    constructor() { counts.observers += 1; }
    observe() {}
    disconnect() {}
  }

  const store = {
    data,
    statusColor: (s) => (
      { running: "#4caf50", stalled: "#ff9800", error: "#f44336" }[s] || "#757575"
    ),
  };

  globalThis.document = document;
  globalThis.window = globalThis;
  globalThis.Alpine = {
    store: (name) => (name === "chatShepherdStore" ? store : undefined),
    $data(el) {
      for (let n = el; n && n.nodeType === 1; n = n.parentNode) {
        if (n._x_dataStack) {
          return n._x_dataStack.reduce((acc, scope) => Object.assign(acc, scope), {});
        }
      }
      return undefined;
    },
  };
  globalThis.MutationObserver = FakeObserver;
  globalThis.requestAnimationFrame = (fn) => fn();
  globalThis.setTimeout = (fn) => { fn(); return 0; };
  globalThis.clearTimeout = () => {};
  globalThis.setInterval = () => { counts.intervals += 1; return counts.intervals; };

  return { document, body, chatsSection, counts, store };
}

// `keepState` mirrors the component loader re-executing the extension script
// without resetting globals; the default gives each scenario a fresh injector.
function load(keepState = false) {
  if (!keepState) delete globalThis.__chatShepherdSidebar;
  runInThisContext(SOURCE, { filename: "shepherd-sidebar.js" });
  return globalThis.window.__chatShepherdSidebar;
}

let checks = 0;
function ok(condition, message) { assert.ok(condition, message); checks += 1; }

/* ---------------------------------------------------------------- *
 * 1. Folder view: the regression. Top-level rows must get a badge.  *
 * ---------------------------------------------------------------- */

{
  const env = installEnv({ data: statusData([["chat0001", "running"]]) });
  const row = buildRow({
    scopeOwner: { item: { id: "chat0001", name: "Alpha" }, group: {}, itemIndex: 0 },
    wrapperScope: { context: { id: "chat0001" } },
    attrs: { "data-folder-thread": "chat0001" },
  });
  env.chatsSection.appendChild(row.li);
  load()._test.injectBadges();
  const badge = badgeIn(row);
  ok(badge, "folder-view top-level row receives a status badge");
  ok(badge.textContent === "🏃", "folder-view badge shows the configured pictogram");
  ok(badge.getAttribute("data-cs-status") === "running", "badge records its status");
}

// 2. Folder view with no convenience attribute: Alpine scope only.
{
  const env = installEnv({ data: statusData([["chat0002", "stalled"]]) });
  const row = buildRow({
    scopeOwner: { item: { id: "chat0002" }, group: {}, itemIndex: 0 },
    wrapperScope: { context: { id: "chat0002" } },
  });
  env.chatsSection.appendChild(row.li);
  load()._test.injectBadges();
  ok(badgeIn(row), "folder-view row resolves through the wrapper Alpine scope");
}

// 2b. Folder view where only the <li> carries the row model.
{
  const env = installEnv({ data: statusData([["chat000b", "stalled"]]) });
  const row = buildRow({ scopeOwner: { item: { id: "chat000b" } } });
  env.chatsSection.appendChild(row.li);
  load()._test.injectBadges();
  ok(badgeIn(row), "a row whose <li> binds `item` resolves through the scope path");
}

// 3. Default flat list row (x-for scope `context` on the <li>).
{
  const env = installEnv({ data: statusData([["chat0003", "error"]]) });
  const row = buildRow({ scopeOwner: { context: { id: "chat0003" } } });
  env.chatsSection.appendChild(row.li);
  load()._test.injectBadges();
  ok(badgeIn(row), "default flat list row still resolves");
}

// 4. Nested child row (x-for scope `child`).
{
  const env = installEnv({ data: statusData([["chat0004", "awaiting_user"]]) });
  const row = buildRow({ scopeOwner: { child: { id: "chat0004" } } });
  env.chatsSection.appendChild(row.li);
  load()._test.injectBadges();
  ok(badgeIn(row), "nested child row resolves");
}

// 5. Placement: the badge sits with the name, ahead of the hover actions.
{
  const env = installEnv({ data: statusData([["chat0005", "running"]]) });
  const row = buildRow({
    scopeOwner: { item: { id: "chat0005" } },
    attrs: { "data-folder-thread": "chat0005" },
    hasChildren: true,
  });
  env.chatsSection.appendChild(row.li);
  load()._test.injectBadges();
  const badge = badgeIn(row);
  ok(badge.parentNode === row.label, "badge is hosted by .chat-list-button");
  ok(
    row.label.children[row.label.children.length - 1] === badge,
    "badge is the last child of the label host, right after the chat name",
  );
  const order = row.container.children;
  ok(
    order.indexOf(row.label) < order.indexOf(row.closeBtn),
    "the label host (and its badge) stays ahead of the hover-only action buttons",
  );
}
// 6. Status change that keeps the same pictogram still repaints.
{
  const env = installEnv({
    data: statusData([["chat0006", "running"]], { running: "●", error: "●" }),
  });
  const row = buildRow({
    scopeOwner: { item: { id: "chat0006" } },
    attrs: { "data-folder-thread": "chat0006" },
  });
  env.chatsSection.appendChild(row.li);
  const api = load();
  api._test.injectBadges();
  const badge = badgeIn(row);
  assert.equal(badge.textContent, "●");
  assert.equal(badge.style.color, "#4caf50");
  checks += 2;

  // Same glyph, different status -> colour and tooltip must follow.
  env.store.data = statusData([["chat0006", "error"]], { running: "●", error: "●" });
  api._test.injectBadges();
  assert.equal(badgeIn(row), badge, "the badge node is reused instead of recreated");
  assert.equal(badge.style.color, "#f44336", "colour follows the status, not the glyph");
  assert.equal(badge.getAttribute("title"), "Chat Shepherd: Error", "tooltip follows the status");
  checks += 3;
}

// 7. Idle rows and a blank icon setting both clear the badge.
{
  const env = installEnv({ data: statusData([["chat0007", "intervention"]]) });
  const row = buildRow({
    scopeOwner: { item: { id: "chat0007" } },
    attrs: { "data-folder-thread": "chat0007" },
  });
  env.chatsSection.appendChild(row.li);
  const api = load();
  api._test.injectBadges();
  ok(badgeIn(row), "badge present while the chat needs attention");

  env.store.data = statusData([["chat0007", "idle"]]);
  api._test.injectBadges();
  ok(!badgeIn(row), "badge is removed once the chat goes idle");

  env.store.data = statusData([["chat0007", "intervention"]], { intervention: "" });
  api._test.injectBadges();
  ok(!badgeIn(row), "an explicitly blank icon paints no badge");
}
// 8. Idempotence: the extension loader re-runs the script on every reload.
{
  const env = installEnv({ data: statusData([["chat0008", "running"]]) });
  const row = buildRow({
    scopeOwner: { item: { id: "chat0008" } },
    attrs: { "data-folder-thread": "chat0008" },
  });
  env.chatsSection.appendChild(row.li);
  load();
  const afterFirst = { ...env.counts };
  load(true);
  load(true);
  assert.equal(env.counts.intervals, afterFirst.intervals,
    "repeat loads do not stack poll intervals");
  assert.equal(env.counts.observers, afterFirst.observers,
    "repeat loads do not stack observers");
  ok(badgeIn(row), "row keeps its badge across repeated extension loads");
  checks += 2;
}

// 9. The observer anchors on the core chats section, not a list-view class.
{
  const env = installEnv({ data: statusData([["chat0009", "running"]]) });
  // Deliberately no .chats-config-list: a list view need not reuse the class.
  const row = buildRow({ scopeOwner: { item: { id: "chat0009" } } });
  env.chatsSection.appendChild(row.li);
  const api = load();
  api._test.attachObserver();
  assert.equal(
    api._test.observedTarget(),
    env.chatsSection,
    "the observer anchors on #chats-section so any list view is covered",
  );
  checks += 1;
}

console.log(`Sidebar badge injector checks passed (${checks} assertions).`);
