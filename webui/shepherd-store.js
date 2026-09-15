import { createStore } from "/js/AlpineStore.js";
import { callJsonApi } from "/js/api.js";
import { toastFrontendError } from "/components/notifications/notification-store.js";

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
 // P6.3: framework callJsonApi - CSRF token, Origin and credentials handled centrally.
 try {
 return await callJsonApi("/api/plugins/chat_shepherd" + path, body || {});
 } catch (e) {
 return { success: false, ok: false, error: String((e && e.message) || e) };
 }
}

export const store = createStore("chatShepherdStore", {
  data: null,
  _fetchFailed: false,
  statusFilter: '',
  timelineOpen: '',
  timelines: {},
  draftEdits: {},
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
      this._fetchFailed = false;
      this.initDraftEdits();
      this.applyPollFromConfig();
      } else {
        if (!this._fetchFailed) toastFrontendError((json && json.error) || "Status request failed", "Chat Shepherd");
      }
    } catch (e) {
      if (!this._fetchFailed) toastFrontendError(String((e && e.message) || e), "Chat Shepherd");
    }
  },

  async nudge(chatId) {
    try {
      const json = await callApi("/nudge", { chat_id: chatId });
      if (!json || !json.success) toastFrontendError((json && json.error) || "Nudge failed", "Chat Shepherd");
      this.fetchStatus();
    } catch (e) {
      toastFrontendError(String((e && e.message) || e), "Chat Shepherd");
    }
  },

  async resolve(chatId) {
    try {
      const json = await callApi("/resolve", { chat_id: chatId, action: "resolve" });
      if (!json || !json.success) toastFrontendError((json && json.error) || "Resolve failed", "Chat Shepherd");
      this.fetchStatus();
    } catch (e) {
      toastFrontendError(String((e && e.message) || e), "Chat Shepherd");
    }
  },

  async dismiss(chatId) {
    try {
      const json = await callApi("/resolve", { chat_id: chatId, action: "dismiss" });
      if (!json || !json.success) toastFrontendError((json && json.error) || "Dismiss failed", "Chat Shepherd");
      this.fetchStatus();
    } catch (e) {
      toastFrontendError(String((e && e.message) || e), "Chat Shepherd");
    }
  },

  initDraftEdits() {
      const ds = (this.data && this.data.drafts) || [];
      for (const d of ds) {
          if (!(d.id in this.draftEdits)) this.draftEdits[d.id] = d.text || '';
      }
  },

  async draftSend(draftId) {
      try {
          const text = (this.draftEdits[draftId] || '').trim();
          const json = await callApi("/draft", { draft_id: draftId, action: 'send', text });
          if (json && json.success) delete this.draftEdits[draftId];
          else toastFrontendError((json && json.error) || 'Draft send failed', "Chat Shepherd");
          this.fetchStatus();
      } catch (e) {
          toastFrontendError(String((e && e.message) || e), "Chat Shepherd");
      }
  },

  async draftDismiss(draftId) {
      try {
          const json = await callApi("/draft", { draft_id: draftId, action: 'dismiss' });
          if (json && json.success) delete this.draftEdits[draftId];
          else toastFrontendError((json && json.error) || 'Draft dismiss failed', "Chat Shepherd");
          this.fetchStatus();
      } catch (e) {
          toastFrontendError(String((e && e.message) || e), "Chat Shepherd");
      }
  },

  applyPollFromConfig() {
    const cfg = (this.data && this.data.config) || {};
    let secs = Number(cfg.poll_seconds);
    if (!Number.isFinite(secs)) secs = 5;
    secs = Math.max(2, Math.min(120, secs));
    const ms = secs * 1000;
    if (ms !== this.pollInterval) {
      this.pollInterval = ms;
      this.startPolling();
    }
  },

  filteredChats() {
    const chats = (this.data && this.data.chats) || [];
    if (!this.statusFilter) return chats;
    return chats.filter((c) => c.status === this.statusFilter);
  },

  nameFor(chatId) {
    const chats = (this.data && this.data.chats) || [];
    const hit = chats.find((c) => c.chat_id === chatId);
    return (hit && (hit.name || hit.chat_id)) || chatId;
  },

  timelineFor(chatId) {
    const t = (this.timelines && this.timelines[chatId]) || null;
    return (t && t.history) || [];
  },

  timelineName(chatId) {
    const t = (this.timelines && this.timelines[chatId]) || null;
    return (t && t.name) || this.nameFor(chatId);
  },

  async toggleTimeline(chatId) {
    if (this.timelineOpen === chatId) {
      this.timelineOpen = '';
      return;
    }
    try {
      const json = await callApi('/history', { chat_id: chatId, limit: 30 });
      if (json && json.success) {
        this.timelines[chatId] = json;
        this.timelineOpen = chatId;
      } else {
        toastFrontendError((json && json.error) || 'History failed', "Chat Shepherd");
      }
    } catch (e) {
      toastFrontendError(String((e && e.message) || e), "Chat Shepherd");
    }
  },

  effectLabel() {
  const e = (this.data && this.data.effectiveness) || null;
  if (!e || !e.sent) return '';
  return '🎯 ' + e.hit_rate + '% nudge hit-rate (' + e.effective + '/' + e.sent + ')';
  },
  
  throttleLabel() {
   const t = (this.data && this.data.throttle) || null;
   if (!t || typeof t.nudges_this_tick === 'undefined') return '';
   const base = '⚡ ' + t.nudges_this_tick + '/' + t.budget + ' nudges this tick';
   return t.capped ? base + ' — capped' : base;
   },
  throttleCapped() {
   const t = (this.data && this.data.throttle) || null;
   return !!(t && t.capped);
   },

  aggregatesLabel() {
  const a = (this.data && this.data.aggregates) || null;
  if (!a || !a.events_in_window) return '';
  const s = a.stalls || {}, r = (a.resume && a.resume.stall) || {}, w = a.wedge || {};
  let label = '📈 ' + (s.episodes || 0) + ' stalls/7d';
  if (r.mean_minutes !== null && r.mean_minutes !== undefined) {
      label += ' · ' + r.mean_minutes + 'min avg resume';
  }
  if (w.success_rate_pct !== null && w.success_rate_pct !== undefined) {
      label += ' · ' + w.success_rate_pct + '% wedge fixes';
  }
  return label;
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
