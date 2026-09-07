/*
 * Chat Shepherd — sidebar status icon injector.
 * Reads status from the shared Alpine store (chatShepherdStore)
 * and paints a badge on sidebar chat rows with a non-idle status.
 */
(function () {
  "use strict";

  var POLL_MS = 10000;
  var debounceTimer = null;
  var injecting = false;

  var ICONS = {
    running: { ch: "✅", color: "#4caf50", label: "Running" },
    stalled: { ch: "⚠️", color: "#ff9800", label: "Stalled" },
    nudged: { ch: "🔄", color: "#2196f3", label: "Nudged" },
    intervention: { ch: "🚨", color: "#f44336", label: "Needs attention" },
    awaiting_user: { ch: "💬", color: "#9c27b0", label: "Awaiting you" },
    error: { ch: "❌", color: "#f44336", label: "Error" },
    paused: { ch: "⏸️", color: "#607d8b", label: "Paused" }
  };

  function store() {
    if (window.Alpine && Alpine.store) return Alpine.store("chatShepherdStore");
    return null;
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
      if (li.__x_for_context && li.__x_for_context.id) return li.__x_for_context.id;
      if (li._x_dataStack && li._x_dataStack[0]) {
        var stack = li._x_dataStack[0];
        if (stack.context && stack.context.id) return stack.context.id;
        if (stack.id) return stack.id;
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

        var meta = ICONS[status];
        if (!meta) return;

        if (existing) {
          if (existing.textContent !== meta.ch) {
            existing.textContent = meta.ch;
            existing.style.color = meta.color;
            existing.title = "Chat Shepherd: " + meta.label;
          }
          return;
        }

        var badge = document.createElement("span");
        badge.className = "cs-badge";
        badge.textContent = meta.ch;
        badge.title = "Chat Shepherd: " + meta.label;
        badge.style.cssText =
          "margin-left:6px;flex:none;font-size:12px;line-height:1;pointer-events:auto;" +
          "color:" + meta.color + ";";
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
