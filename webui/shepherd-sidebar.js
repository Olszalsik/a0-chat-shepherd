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

  var DEFAULT_ICONS = {
    running: "🏃",
    stalled: "⚠️",
    nudged: "🔄",
    intervention: "🚨",
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
    var node = container;
    while (node && node.getAttribute) {
      var attr = idFromAttributes(node);
      if (attr) return attr;
      node = node.parentElement;
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

    var containers = document.querySelectorAll(".chat-container");
    for (var j = 0; j < containers.length; j++) {
      var container = containers[j];
      if (container.isConnected === false) continue;

      var chatId = chatIdFromContainer(container);
      if (!chatId) continue;

      var status = map[chatId];
      var existing = container.querySelector(".cs-badge");
      if (!status) {
        if (existing) existing.remove();
        continue;
      }

      var glyph = iconFor(s, status);
      // Configurable "no pictogram" for this state.
      if (!glyph) {
        if (existing) existing.remove();
        continue;
      }

      var anchor = anchorFor(container);
      // Alpine may have re-keyed the row; never steal another chat's badge.
      if (existing && !anchor.host.contains(existing)) existing = null;
      var badge = existing || makeBadge();
      applyBadge(badge, status, glyph, colorFor(s, status),
        "Chat Shepherd: " + (LABELS[status] || status));
      if (!existing) placeBadge(badge, anchor);
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
      /* a transient DOM shape must not kill the poll loop */
    } finally {
      injecting = false;
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
      // Temporary body fallback: its only job is to notice the sidebar
      // appearing, then re-scope.
      obs = new MutationObserver(function () {
        if (findChatRoot()) {
          attachObserver();
          return;
        }
        scheduleInject();
      });
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
