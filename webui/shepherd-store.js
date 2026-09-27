import { createStore } from "/js/AlpineStore.js";
import { callJsonApi } from "/js/api.js";
import { showConfirmDialog } from "/js/confirmDialog.js";
import { toastFrontendError, toastFrontendSuccess } from "/components/notifications/notification-store.js";

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
    // v1.20.0: idempotent start marker - the self-heal timer below (and
    // any repeat init) must not double-schedule polling.
    this._started = true;
    this.onOpen();
  },

  onOpen() {
    this.fetchStatus();
    this.startPolling();
  },

  cleanup() {
    // v1.20.2: deliberately a no-op. This store is SHARED with the sidebar
    // badge injector, so tearing the poll down when a surface unmounts froze
    // the sidebar icons on stale data for the rest of the page session (the
    // dashboard modal's x-destroy used to call this). Polling costs one small
    // GET every poll_seconds and the sidebar needs it anyway, so the poll is
    // page-scoped and only a full unload ends it. Consumers that genuinely
    // want it stopped can call stopPolling() directly.
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
        // v1.20.2: the "toast once, not every 5s" guard was dead code -
        // _fetchFailed was only ever assigned false, so a wedged backend
        // raised an error toast on every poll for the rest of the session.
        // Latch AFTER the check so the first failure still reports.
        if (!this._fetchFailed) toastFrontendError((json && json.error) || "Status request failed", "Chat Shepherd");
        this._fetchFailed = true;
      }
    } catch (e) {
      if (!this._fetchFailed) toastFrontendError(String((e && e.message) || e), "Chat Shepherd");
      this._fetchFailed = true;
    }
  },

  async nudge(chatId, force = false) {
    // v1.20.4: the backend refuses to silently restart a chat that died from a
    // real error / security termination - the same states the auto-nudge ladder
    // deliberately refuses. It answers with needs_confirmation instead of
    // sending, and the operator decides. Nothing has been sent or recorded at
    // that point, so re-issuing with force is safe.
    try {
      const json = await callApi("/nudge", { chat_id: chatId, force: force === true });
      if (json && json.needs_confirmation) {
        const confirmed = await showConfirmDialog({
          title: "Restart a chat that errored?",
          message: `${json.warning}<br><br>Only continue if you believe the underlying problem is fixed. Chat Shepherd still will not auto-nudge this chat while the suppression is active.`,
          confirmText: "Nudge anyway",
          cancelText: "Cancel",
          type: "danger",
        });
        if (!confirmed) return;
        return this.nudge(chatId, true);
      }
      if (!json || !json.success) {
        toastFrontendError((json && json.error) || "Nudge failed", "Chat Shepherd");
      } else if (json.overrode_guard) {
        toastFrontendSuccess("Nudge sent (guard overridden)", "Chat Shepherd");
      }
      this.fetchStatus();
    } catch (e) {
      toastFrontendError(String((e && e.message) || e), "Chat Shepherd");
    }
  },

  // v1.20.4: tool-tip explaining the confirmation a guarded nudge will ask
  // for, so the behaviour is discoverable before the click, not after it.
  // Keys on the real helpers/constants.py STATUS_* values - 'error' is what
  // classify_chat assigns to both a fatal stop and a security termination.
  nudgeHint(chat) {
    if (!chat) return "";
    if (String(chat.status || "") === "error") {
      return "This chat ended on an error or a security termination. "
        + "Chat Shepherd will not auto-nudge it, and a manual nudge will ask "
        + "for confirmation first.";
    }
    return "Send the nudge message to this chat.";
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
      const live = new Set();
      for (const d of ds) {
          live.add(d.id);
          if (!(d.id in this.draftEdits)) this.draftEdits[d.id] = d.text || '';
      }
      // v1.20.2: drop edits for drafts that no longer exist (sent, dismissed,
      // or aged out by the server-side DRAFT_MAX cap). The map only ever grew
      // before, so a long dashboard session leaked one entry per draft.
      for (const id of Object.keys(this.draftEdits)) {
          if (!live.has(id)) delete this.draftEdits[id];
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

  // v1.20.0: tracked-chats maintenance. Routine dead-chat pruning and
  // dead-surplus cap eviction are shown separately. cap_evicted is only
  // non-zero when dead entries outnumber the cap, which - since live
  // contexts are never evicted - is the visible proof that live chats are
  // protected. Before v1.20.0 this churn was invisible and instead showed
  // up as a constant prune_state count in the journal.
  capLabel() {
   const t = (this.data && this.data.throttle) || null;
   if (!t) return '';
   const pruned = t.pruned || 0;
   const evicted = t.cap_evicted || 0;
   if (!pruned && !evicted) return '';
   let label = '🧹 ' + pruned + ' stale chat' + (pruned === 1 ? '' : 's') + ' pruned';
   if (evicted) {
     label += ' · ' + evicted + ' over cap evicted (live chats protected)';
   }
   return label;
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

// v1.20.0: self-heal for the sidebar extension handoff. shepherd-init.html
// wires init() through x-init guarded by store presence; if Alpine
// evaluated that binding before this module registered the store (module
// script vs Alpine.start timing), the store would never start polling and
// the sidebar would stay icon-less. Idempotent: init() no-ops once started.
setTimeout(function () {
  try {
    var s = window.Alpine && Alpine.store ? Alpine.store('chatShepherdStore') : null;
    if (s && !s._started) s.init();
  } catch (e) { /* store lifecycle is best-effort here */ }
}, 2000);
