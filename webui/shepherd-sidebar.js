/*
 * Chat Shepherd — sidebar status icon injector.
 *
 * Reads status from the shared Alpine store (chatShepherdStore) and paints a
 * badge on sidebar chat rows with a non-idle status. Icons are user-configurable
 * via plugin settings (config.icons); an empty icon means "no pictogram for
 * this state".
 *
 * Row resolution notes (Agent Zero v2.10+ sidebar refactor):
 *   - A chat row is rendered by the shared `sidebar/chats/chat-tree.html`
 *     component, so `.chat-container` is not always wrapped in the same <li>.
 *   - The default list binds the loop scope as `context`/`child`; the
 *     `_sidebar_folders` list view binds it as `item` and publishes
 *     `data-folder-thread` on the row.
 *   - Alpine's inherited scope is reachable from the row container itself via
 *     `Alpine.$data(container)`, which resolves every one of those shapes.
 *     Reading the <li> was the old behaviour and returned null for folder rows,
 *     so no badge was created for the top-level chats you actually glance at.
 */
(function () {
  "use strict";

  // Idempotence guard: this file is a classic script inside the
  // `sidebar-chats-list-start` HTML extension. The component loader clones and
  // re-executes extension scripts on every extension (re)load, which used to
  // stack a second poll interval and a second MutationObserver each time.
  if (window.__chatShepherdSidebar) {
    window.__chatShepherdSidebar.refresh();
    return;
  }

  var POLL_MS = 2500;
  var debounceTimer = null;
  var injecting = false;
  var obs = null;
  var observedEl = null;
  // v1.21.0 (O11): container -> {id, badge}. WeakMap so a re-keyed or removed
  // row is collected with its element; a plain object would leak one entry per
  // row ever rendered for the whole page session.
  var ROW_MEMO = typeof WeakMap === "function" ? new WeakMap() : null;

  function memoFor(container) {
    if (!ROW_MEMO) return null;
    var hit = ROW_MEMO.get(container);
    return hit || null;
  }

  function setMemo(container, chatId, badge) {
    if (!ROW_MEMO) return;
    ROW_MEMO.set(container, { id: chatId || "", badge: badge || null });
  }

  var DEFAULT_ICONS = {
    running: "🏃",
    stalled: "⚠️",
    nudged: "🔄",
    intervention: "🚨",
    blocked: "⛔",
    interrupted: "🔌",
    awaiting_user: "💬",
    error: "❌",
    paused: "⏸️",
    idle: "",
  };

  var LABELS = {
    running: "Running",
    stalled: "Stalled",
    nudged: "Nudged",
    intervention: "Needs attention",
    blocked: "Blocked",
    interrupted: "Interrupted",
    awaiting_user: "Awaiting you",
    error: "Error",
    paused: "Paused",
    idle: "Idle",
  };

  var DEFAULT_COLORS = {
    running: "#4caf50",
    stalled: "#ff9800",
    nudged: "#2196f3",
    intervention: "#f44336",
    blocked: "#d32f2f",
    interrupted: "#ff5722",
    awaiting_user: "#9c27b0",
    error: "#f44336",
    paused: "#607d8b",
    idle: "#757575",
  };

  // Alpine scope keys that can carry the row model on any sidebar list view.
  var SCOPE_KEYS = ["context", "child", "item", "task"];
  // Row attributes published by core/community list views.
  var ROW_ID_ATTRS = ["data-folder-thread", "data-context-id", "data-chat-id"];
  // v1.21.0 (A13): hard ceiling on the ancestor walk for a row id. Core rows
  // publish their id on the .chat-container element itself, so the walk is
  // one hop in practice; the ceiling only matters for third-party views.
  var ROW_WALK_MAX_HOPS = 6;
  // Badge host candidates, in preference order. The badge belongs next to the
  // chat name: appending it to `.chat-container` put it after the hover-only
  // action buttons, so the icon jumped sideways on every hover.
  var HOST_SELECTORS = [".chat-list-button", ".chat-container"];

  function store() {
    if (window.Alpine && Alpine.store) return Alpine.store("chatShepherdStore");
    return null;
  }

  // Icons are owned by the store (and ultimately by /status) so the sidebar can
  // no longer drift from the dashboard. The local map is a fallback for the
  // window before the store module has evaluated.
  function iconFor(s, status) {
    var custom = (s && s.data && s.data.config && s.data.config.icons) || {};
    if (Object.prototype.hasOwnProperty.call(custom, status)) {
      return String(custom[status] || "");
    }
    return DEFAULT_ICONS[status] || "";
  }

  function colorFor(s, status) {
    try {
      if (s && typeof s.statusColor === "function") {
        var fromStore = s.statusColor(status);
        if (fromStore) return fromStore;
      }
    } catch (e) {
      /* store not ready */
    }
    return DEFAULT_COLORS[status] || DEFAULT_COLORS.idle;
  }

  function scopeId(scope) {
    if (!scope) return null;
    for (var i = 0; i < SCOPE_KEYS.length; i++) {
      var row = scope[SCOPE_KEYS[i]];
      if (row && typeof row === "object" && row.id) return String(row.id);
    }
    return null;
  }

  function idFromAttributes(el) {
    if (!el || !el.getAttribute) return null;
    for (var i = 0; i < ROW_ID_ATTRS.length; i++) {
      var value = el.getAttribute(ROW_ID_ATTRS[i]);
      if (value) return String(value);
    }
    return null;
  }

  function alpineScope(el) {
    try {
      if (window.Alpine && typeof Alpine.$data === "function") return Alpine.$data(el);
    } catch (e) {
      /* detached / uninitialised node */
    }
    try {
      if (el && el._x_dataStack && el._x_dataStack[0]) return el._x_dataStack[0];
    } catch (e) {
      /* no scope */
    }
    return null;
  }

  // Resolve the chat id a row represents, tolerating every sidebar list view.
  function chatIdFromContainer(container) {
    if (!container) return null;

    // 1. Explicit row attributes published by the list view (most stable).
    //    v1.21.0 (A13): the walk is BOUNDED. It used to climb to <html>, so a
    //    row deep inside someone else's subtree could reach an ancestor
    //    `data-folder-thread` / `data-context-id` and be painted with another
    //    chat's status. Two guards now: a hard hop ceiling
    //    (ROW_WALK_MAX_HOPS) and a stop at any enclosing LI/UL/TABLE. Note the
    //    honest limit - when the row's OWN owner is the `li` (the
    //    _sidebar_folders shape) that `li` must still be readable, so the
    //    boundary is checked after its own attributes; a row that publishes no
    //    id and sits within the hop budget of an id-bearing wrapper is
    //    genuinely indistinguishable from a folder row in DOM terms. The
    //    ceiling bounds that exposure instead of making it unbounded.
    var node = container;
    var hops = 0;
    while (node && node.getAttribute && hops < ROW_WALK_MAX_HOPS) {
      var attr = idFromAttributes(node);
      if (attr) return attr;
      if (node.tagName === "LI" || node.tagName === "UL" || node.tagName === "TABLE") break;
      node = node.parentElement;
      hops++;
    }

    // 2. The row's own inherited Alpine scope. This is the fix for folder
    //    view, where `item` is bound on a wrapper x-data rather than the <li>.
    var own = scopeId(alpineScope(container));
    if (own) return own;

    // 3. Legacy <li> scope lookup (default list / nested child rows).
    var li = container.closest ? container.closest("li") : null;
    if (li) {
      var fromLi = scopeId(alpineScope(li));
      if (fromLi) return fromLi;
    }

    // 4. Last resort: a dataset id directly on the row.
    if (container.dataset && container.dataset.chatId) {
      return String(container.dataset.chatId);
    }
    return null;
  }

  // Where the badge lives inside a row: the label host, inserted before the
  // hover-only action buttons so the icon never shifts when they appear.
  function anchorFor(container) {
    var host = null;
    for (var i = 0; i < HOST_SELECTORS.length; i++) {
      host = container.querySelector(HOST_SELECTORS[i]);
      if (host) break;
    }
    if (!host) host = container;
    var actions = host.querySelector ? host.querySelector(".chat-list-action-btn") : null;
    if (actions && actions.parentNode === host) {
      return { host: host, before: actions };
    }
    return { host: host, before: null };
  }

  function placeBadge(badge, anchor) {
    if (anchor.before) {
      if (badge.parentNode !== anchor.host || badge.nextSibling !== anchor.before) {
        anchor.host.insertBefore(badge, anchor.before);
      }
      return;
    }
    if (badge.parentNode !== anchor.host || anchor.host.lastElementChild !== badge) {
      anchor.host.appendChild(badge);
    }
  }

  function applyBadge(badge, status, glyph, color, label) {
    // Track the status, not just the glyph: two states may share one pictogram,
    // and the old textContent-only comparison left a stale colour and tooltip.
    if (badge.getAttribute("data-cs-status") !== status) {
      badge.setAttribute("data-cs-status", status);
    }
    if (badge.textContent !== glyph) badge.textContent = glyph;
    if (badge.getAttribute("title") !== label) badge.title = label;
    if (badge.getAttribute("aria-label") !== label) badge.setAttribute("aria-label", label);
    if (badge.style.color !== color) badge.style.color = color;
  }

  function makeBadge() {
    var badge = document.createElement("span");
    badge.className = "cs-badge";
    badge.setAttribute("role", "img");
    // pointer-events:none keeps the whole row clickable and hoverable.
    badge.style.cssText =
      "margin-left:6px;flex:none;font-size:12px;line-height:1;pointer-events:none;";
    return badge;
  }

  function paintRows(s) {
    var chats = (s && s.data && s.data.chats) || [];
    var map = {};
    for (var i = 0; i < chats.length; i++) {
      var c = chats[i];
      if (c && c.chat_id && c.status && c.status !== "idle") {
        map[c.chat_id] = c.status;
      }
    }

    // v1.21.0 (O11): memoized DOM handles. A repaint used to redo ~4 queries per
    // row per pass - anchorFor (two querySelector calls) plus a
    // .cs-badge lookup - with ~300 rows every 2.5 s plus every debounced
    // mutation. The memo holds only the resolved ELEMENTS (host + badge),
    // never the chat id.
    //
    // The id is deliberately re-resolved every pass (one getAttribute on core
    // rows). Caching it looked free and is NOT: Alpine's keyed x-for REUSES
    // elements across a key change and patches the bound data in place, so a
    // cached id would survive a re-key and keep painting the old chat's status
    // on a row that now shows another one. Correctness of the id beats the
    // microseconds.
    var containers = document.querySelectorAll(".chat-container");
    for (var j = 0; j < containers.length; j++) {
      var container = containers[j];
      if (container.isConnected === false) continue;

      var chatId = chatIdFromContainer(container);
      if (!chatId) continue;

      var memo = memoFor(container);
      var status = map[chatId];
      if (!status) {
        if (memo && memo.badge && memo.badge.parentNode) memo.badge.remove();
        setMemo(container, chatId, null);
        continue;
      }

      var glyph = iconFor(s, status);
      // Configurable "no pictogram" for this state.
      if (!glyph) {
        if (memo && memo.badge && memo.badge.parentNode) memo.badge.remove();
        setMemo(container, chatId, null);
        continue;
      }

      var label = "Chat Shepherd: " + (LABELS[status] || status);
      var color = colorFor(s, status);
      var existing = memo && memo.badge;
      // Fast path: this row is already painted for exactly this status. The
      // container's own data-cs-status is the cross-check that a stale memo
      // can never hide a real change.
      if (existing && existing.parentNode
          && container.getAttribute("data-cs-status") === status
          && existing.textContent === glyph
          && existing.getAttribute("aria-label") === label) {
        if (existing.style.color !== color) existing.style.color = color;
        continue;
      }

      var anchor = anchorFor(container);
      // Alpine may have re-keyed the row; never steal another chat's badge.
      if (existing && !anchor.host.contains(existing)) existing = null;
      var badge = existing || makeBadge();
      applyBadge(badge, status, glyph, color, label);
      if (!existing) placeBadge(badge, anchor);
      setMemo(container, chatId, badge);
    }
  }

  function injectBadges() {
    if (injecting) {
      // Never drop a pass silently; re-run on the next frame.
      scheduleInject();
      return;
    }
    injecting = true;
    try {
      paintRows(store());
    } catch (e) {
      // v1.21.0 (A14): report, do not disappear. This catch exists so a
      // transient DOM shape cannot kill the poll loop - but swallowing the
      // error outright meant a genuinely broken injector (a renamed helper, a
      // null row model) showed up as "the icons just stopped updating" with
      // nothing in the console. Rate-limited to one line per minute so a
      // persistent failure cannot flood the console every 2.5 s.
      reportOnce("paintRows failed: " + ((e && e.stack) || e));
    } finally {
      injecting = false;
    }
  }

  // v1.21.0 (A14): one console line per distinct failure per minute.
  var lastReportAt = 0;
  function reportOnce(text) {
    try {
      var now = Date.now();
      if (now - lastReportAt < 60000) return;
      lastReportAt = now;
      console.error("[chat_shepherd] " + text);
    } catch (e) {
      /* console unavailable */
    }
  }

  function scheduleInject() {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(function () {
      debounceTimer = null;
      requestAnimationFrame(injectBadges);
    }, 50);
  }

  // Observe the core-owned chats section so every list presentation (default
  // list, _sidebar_folders, any future row-list extension) is covered. The old
  // `.chats-config-list` lookup depended on a class that is not part of the
  // row-list extension contract, so an alternative view silently lost badges.
  function findChatRoot() {
    return (
      document.querySelector("#chats-section") ||
      document.querySelector(".chats-list-container") ||
      document.querySelector(".chats-config-list")
    );
  }

  function attachObserver() {
    if (obs) obs.disconnect();
    var target = findChatRoot();
    observedEl = target;
    if (target) {
      obs = new MutationObserver(scheduleInject);
      obs.observe(target, { childList: true, subtree: true });
    } else {
      // Temporary body fallback: its ONLY job is to notice the sidebar
      // appearing, then re-scope. v1.20.7: it no longer repaints on every
      // page mutation (the 2.5 s poll already repaints) — on a busy page
      // the old debounced scheduleInject per mutation ran querySelectorAll
      // + Alpine scope probes page-wide for the whole session, because
      // observedEl stayed null and the poll's re-scope check never fired.
      obs = new MutationObserver(function () {
        if (findChatRoot()) {
          attachObserver();
          return;
        }
      });
      observedEl = document.body;
      obs.observe(document.body, { childList: true, subtree: true });
    }
    scheduleInject();
  }

  setInterval(function () {
    var s = store();
    // v1.20.2: the shared store is page-scoped, but re-arm the poll if any
    // surface ever stopped it - otherwise the icons would silently freeze on
    // stale data with no way to recover short of a reload.
    if (s && s._started && !s.pollTimer && typeof s.startPolling === "function") {
      s.startPolling();
    }
    if (s && s.data) scheduleInject();
    // A sidebar re-render replaced the observed root -> re-scope the observer.
    if (observedEl && !document.contains(observedEl)) attachObserver();
    // v1.20.7: still on the body fallback (no chat root yet) — re-scope as
    // soon as one appears, even without a fallback observer callback.
    if (observedEl === document.body && findChatRoot()) attachObserver();
  }, POLL_MS);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", attachObserver);
  } else {
    attachObserver();
  }

  window.__chatShepherdSidebar = {
    refresh: scheduleInject,
    // Exposed for the offline regression suite in tests/suite_sidebar.mjs.
    _test: {
      chatIdFromContainer: chatIdFromContainer,
      anchorFor: anchorFor,
      injectBadges: injectBadges,
      attachObserver: attachObserver,
      observedTarget: function () { return observedEl; },
    },
  };
})();
