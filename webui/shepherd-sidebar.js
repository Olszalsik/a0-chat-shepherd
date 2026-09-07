/*
 * Chat Shepherd — sidebar status icon injector.
 * Reads status from the shared Alpine store (chatShepherdStore)
 * and paints a badge on sidebar chat rows with a non-idle status.
 * Icons are user-configurable via plugin settings (config.icons);
 * an empty icon means "no pictogram for this state".
 */
(function () {
  "use strict";

  var POLL_MS = 2500;
  var debounceTimer = null;
  var injecting = false;

  var DEFAULT_ICONS = {
    running: "🏃",
    stalled: "⚠️",
    nudged: "🔄",
    intervention: "🚨",
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
    awaiting_user: "Awaiting you",
    error: "Error",
    paused: "Paused",
    idle: "Idle",
  };

  var COLORS = {
    running: "#4caf50",
    stalled: "#ff9800",
    nudged: "#2196f3",
    intervention: "#f44336",
    awaiting_user: "#9c27b0",
    error: "#f44336",
    paused: "#607d8b",
    idle: "#757575",
  };

  function store() {
    if (window.Alpine && Alpine.store) return Alpine.store("chatShepherdStore");
    return null;
  }

  function iconFor(s, status) {
    var custom = (s && s.data && s.data.config && s.data.config.icons) || {};
    if (Object.prototype.hasOwnProperty.call(custom, status)) {
      return String(custom[status] || "");
    }
    return DEFAULT_ICONS[status] || "";
  }

  setInterval(function () {
    var s = store();
    if (s && s.data) scheduleInject();
  }, POLL_MS);

  function scheduleInject() {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(function () {
      requestAnimationFrame(injectBadges);
    }, 50);
  }

  function chatIdFromContainer(container) {
    var li = container.closest("li");
    if (li) {
      try {
        if (window.Alpine && Alpine.$data) {
          var d = Alpine.$data(li);
          if (d) {
            if (d.context && d.context.id) return d.context.id;
            if (d.child && d.child.id) return d.child.id;
          }
        }
      } catch (e) {}
      if (li._x_dataStack && li._x_dataStack[0]) {
        var stack = li._x_dataStack[0];
        if (stack.context && stack.context.id) return stack.context.id;
        if (stack.child && stack.child.id) return stack.child.id;
      }
    }
    if (container.dataset && container.dataset.chatId) return container.dataset.chatId;
    return null;
  }

  function injectBadges() {
    if (injecting) return;
    injecting = true;
    try {
      var s = store();
      var chats = (s && s.data && s.data.chats) || [];
      var map = {};
      chats.forEach(function (c) {
        if (c && c.chat_id && c.status && c.status !== "idle") {
          map[c.chat_id] = c.status;
        }
      });

      var containers = document.querySelectorAll(".chat-container");
      containers.forEach(function (container) {
        var chatId = chatIdFromContainer(container);
        if (!chatId) return;
        var status = map[chatId];
        var existing = container.querySelector(".cs-badge");

        if (!status) {
          if (existing) existing.remove();
          return;
        }

        var ch = iconFor(s, status);
        var color = COLORS[status] || "#757575";

        // Configurable "no pictogram" for this state.
        if (!ch) {
          if (existing) existing.remove();
          return;
        }

        var label = "Chat Shepherd: " + (LABELS[status] || status);
        if (existing) {
          if (existing.textContent !== ch) {
            existing.textContent = ch;
            existing.style.color = color;
            existing.title = label;
          }
          return;
        }

        var badge = document.createElement("span");
        badge.className = "cs-badge";
        badge.textContent = ch;
        badge.title = label;
        badge.style.cssText =
          "margin-left:6px;flex:none;font-size:12px;line-height:1;pointer-events:auto;" +
          "color:" + color + ";";
        container.appendChild(badge);
      });
    } finally {
      injecting = false;
    }
  }

  function startObserver() {
    var target = document.querySelector(".chats-config-list") || document.body;
    var obs = new MutationObserver(scheduleInject);
    obs.observe(target, { childList: true, subtree: true });
    scheduleInject();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", startObserver);
  } else {
    startObserver();
  }

  window.__chatShepherdSidebarRefresh = scheduleInject;
})();
