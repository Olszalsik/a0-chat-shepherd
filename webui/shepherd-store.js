import { createStore } from "/js/AlpineStore.js";

const API = "/api/plugins/chat_shepherd";

// Fallback icons; the server config (icons) overrides these per status.
// Empty string = no pictogram shown for that state.
export const DEFAULT_ICONS = {
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

const COLORS = {
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

async function callApi(path, body) {
  const resp = await fetch(API + path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const txt = await resp.text();
  try {
    return JSON.parse(txt);
  } catch (e) {
    return { success: false, ok: false, error: txt };
  }
}

export const store = createStore("chatShepherdStore", {
  data: null,
  error: "",
  pollTimer: null,
  pollInterval: 5000,

  init() {
    this.onOpen();
  },

  onOpen() {
    this.fetchStatus();
    this.startPolling();
  },

  cleanup() {
    this.stopPolling();
  },

  startPolling() {
    this.stopPolling();
    this.pollTimer = setInterval(() => this.fetchStatus(), this.pollInterval);
  },

  stopPolling() {
    if (this.pollTimer) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  },

  async fetchStatus() {
    try {
      const json = await callApi("/status");
      if (json && json.success) {
        this.data = json;
        this.error = "";
      } else {
        this.error = (json && json.error) || "Status request failed";
      }
    } catch (e) {
      this.error = String(e);
    }
  },

  async nudge(chatId) {
    try {
      const json = await callApi("/nudge", { chat_id: chatId });
      if (!json || !json.success) this.error = (json && json.error) || "Nudge failed";
      this.fetchStatus();
    } catch (e) {
      this.error = String(e);
    }
  },

  async resolve(chatId) {
    try {
      const json = await callApi("/resolve", { chat_id: chatId, action: "resolve" });
      if (!json || !json.success) this.error = (json && json.error) || "Resolve failed";
      this.fetchStatus();
    } catch (e) {
      this.error = String(e);
    }
  },

  async dismiss(chatId) {
    try {
      const json = await callApi("/resolve", { chat_id: chatId, action: "dismiss" });
      if (!json || !json.success) this.error = (json && json.error) || "Dismiss failed";
      this.fetchStatus();
    } catch (e) {
      this.error = String(e);
    }
  },

  effectLabel() {
  const e = (this.data && this.data.effectiveness) || null;
  if (!e || !e.sent) return '';
  return '🎯 ' + e.hit_rate + '% nudge hit-rate (' + e.effective + '/' + e.sent + ')';
  },
  
  iconFor(status) {
    const custom = (this.data && this.data.config && this.data.config.icons) || {};
    const fallback = DEFAULT_ICONS[status];
    const ch = custom[status] !== undefined && custom[status] !== null ? custom[status] : fallback;
    return ch || "";
  },

  statusIcon(status) {
    return this.iconFor(status);
  },

  statusColor(status) {
    return COLORS[status] || "#757575";
  },

  formatTime(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      return d.toLocaleTimeString();
    } catch (e) {
      return iso;
    }
  },
});
