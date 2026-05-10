const state = {
  token: localStorage.getItem("nanoOpenClawToken") || "",
  themePreference: localStorage.getItem("nanoOpenClawTheme") || "system",
  ws: null,
  reconnectDelay: 1200,
  sessions: [],
  currentSession: null,
  activeTurnId: null,
  activeTurnsBySession: new Map(),
  sessionByTurn: new Map(),
  assistantNode: null,
  approvals: new Map(),
  tools: new Map(),
  thinkingText: "",
  activityItems: [],
  activityNode: null,
  activityTurns: new Map(),
  currentActivityId: null,
  selectedActivityId: null,
  activityStartedAt: null,
  activityDurationMs: 0,
  activityTimer: null,
  thinkingActivityId: null,
  assistantName: "Assistant",
  userName: "User",
  _pendingTextLen: 0,
  _assistantRawText: "",
  _followBottom: true,
  openDrawer: null,
  thinkingLevel: "off",
  lastThinkingLevel: "low",
  runtime: {
    agentId: "",
    modelRef: "",
    imageModelRef: "",
    agentOptions: [],
    modelOptions: [],
    imageModelOptions: [],
    thinkingOptions: ["off", "minimal", "low", "medium", "high", "xhigh", "adaptive", "max"],
    openMenu: "",
  },
  attachments: [],
  submittedAttachmentIds: new Set(),
};

const $ = (id) => document.getElementById(id);
const MAX_ATTACHMENTS = 5;
const MAX_NON_IMAGE_BYTES = 10 * 1024 * 1024;
const MAX_TOTAL_ATTACHMENT_BYTES = 25 * 1024 * 1024;
const ALLOWED_ATTACHMENT_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "application/pdf",
]);
const THEME_LABELS = {
  system: "System",
  dark: "Dark",
  light: "Light",
};
const themeMedia = window.matchMedia("(prefers-color-scheme: dark)");

function resolvedTheme() {
  if (state.themePreference === "dark" || state.themePreference === "light") {
    return state.themePreference;
  }
  return themeMedia.matches ? "dark" : "light";
}

function applyThemePreference() {
  const preference = THEME_LABELS[state.themePreference] ? state.themePreference : "system";
  state.themePreference = preference;
  document.documentElement.dataset.theme = resolvedTheme();
  document.documentElement.dataset.themePreference = preference;
  renderAppearanceMenu();
}

function setThemePreference(preference) {
  state.themePreference = THEME_LABELS[preference] ? preference : "system";
  localStorage.setItem("nanoOpenClawTheme", state.themePreference);
  applyThemePreference();
}

function renderAppearanceMenu() {
  const btn = $("appearanceBtn");
  if (btn) {
    const label = THEME_LABELS[state.themePreference] || THEME_LABELS.system;
    btn.title = `Theme: ${label}`;
    btn.setAttribute("aria-label", `Theme settings, current: ${label}`);
  }
  document.querySelectorAll("[data-theme-choice]").forEach((option) => {
    option.setAttribute("aria-checked", String(option.dataset.themeChoice === state.themePreference));
  });
}

function closeAppearanceMenu() {
  const menu = $("appearanceOptions");
  const btn = $("appearanceBtn");
  if (!menu || !btn) return;
  menu.hidden = true;
  btn.setAttribute("aria-expanded", "false");
}

function authHeaders() {
  return state.token ? { Authorization: `Bearer ${state.token}` } : {};
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
      ...(options.headers || {}),
    },
  });
  if (res.status === 401) {
    showTokenDialog();
    throw new Error("unauthorized");
  }
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const qs = state.token ? `?token=${encodeURIComponent(state.token)}` : "";
  state.ws = new WebSocket(`${scheme}://${location.host}/ws${qs}`);
  state.ws.onopen = () => {
    state.reconnectDelay = 1200;
    addEvent("connected", "WebSocket ready", { type: "connected" });
  };
  state.ws.onmessage = (event) => handleEvent(JSON.parse(event.data));
  state.ws.onclose = () => {
    addEvent("disconnected", "WebSocket closed", { type: "disconnected" });
    const delay = state.reconnectDelay;
    state.reconnectDelay = Math.min(state.reconnectDelay * 1.6, 10000);
    setTimeout(connect, delay);
  };
}

function send(type, payload = {}) {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return false;
  state.ws.send(JSON.stringify({ type, ...payload }));
  return true;
}

function currentSessionId() {
  return state.currentSession?.session_id || null;
}

function activeTurnIdForCurrentSession() {
  const sessionId = currentSessionId();
  return sessionId ? state.activeTurnsBySession.get(sessionId) || null : null;
}

function setSessionActiveTurn(sessionId, turnId) {
  if (!sessionId) return;
  if (turnId) {
    state.activeTurnsBySession.set(sessionId, turnId);
    state.sessionByTurn.set(turnId, sessionId);
  } else {
    const previousTurnId = state.activeTurnsBySession.get(sessionId);
    if (previousTurnId) state.sessionByTurn.delete(previousTurnId);
    state.activeTurnsBySession.delete(sessionId);
  }
  state.activeTurnId = activeTurnIdForCurrentSession();
}

function syncSessionActiveTurn(session) {
  if (!session?.session_id) return;
  setSessionActiveTurn(session.session_id, session.active_turn_id || null);
}

function isCurrentSessionEvent(event) {
  return !event.session_id || event.session_id === currentSessionId();
}

function updateSendBtn() {
  const btn = $("sendBtn");
  state.activeTurnId = activeTurnIdForCurrentSession();
  if (state.activeTurnId) {
    btn.textContent = "";
    btn.title = "Cancel";
    btn.setAttribute("aria-label", "Cancel");
    btn.classList.add("stop-mode");
  } else {
    btn.textContent = "";
    btn.title = "Send";
    btn.setAttribute("aria-label", "Send");
    btn.classList.remove("stop-mode");
  }
}

function handleEvent(event) {
  switch (event.type) {
    case "state.updated":
      renderRuntime(event);
      break;
    case "session.updated":
      const previousSessionId = state.currentSession?.session_id || null;
      state.sessions = event.sessions || state.sessions;
      state.currentSession = event.session || state.currentSession;
      syncSessionActiveTurn(state.currentSession);
      renderSessions();
      updateSendBtn();
      if (previousSessionId !== (state.currentSession?.session_id || null) || !$("messages").children.length) {
        renderHistory();
      }
      break;
    case "chat.accepted":
      // Remove pending slash-command block if a skill was routed to the agent loop
      setSessionActiveTurn(event.session_id, event.turn_id);
      renderSessions();
      updateSendBtn();
      if (!isCurrentSessionEvent(event)) break;
      document.querySelectorAll(".message.command.pending").forEach((el) => el.remove());
      state.tools.clear();
      state.thinkingText = "";
      resetActivity();
      state.activityStartedAt = Date.now();
      state._pendingTextLen = 0;
      state._assistantRawText = "";
      document.querySelectorAll(".message.turn-event, .message.approval-card").forEach((el) => el.remove());
      appendMessage("user", formatAcceptedUserText(event));
      state.activityNode = appendActivitySummary();
      if (state.submittedAttachmentIds.size) {
        state.attachments = state.attachments.filter((item) => !state.submittedAttachmentIds.has(item.id));
        state.submittedAttachmentIds.clear();
        validateAttachmentSet();
        renderAttachments();
      }
      state.assistantNode = appendMessage("assistant", "");
      scrollMessages(true);
      break;
    case "text.delta":
      if (!isCurrentSessionEvent(event)) break;
      if (!state.assistantNode) state.assistantNode = appendMessage("assistant", "");
      state._assistantRawText += event.text;
      state.assistantNode.innerHTML = renderMarkdown(state._assistantRawText);
      state._pendingTextLen += event.text.length;
      scrollMessages();
      break;
    case "thinking.delta":
      if (!isCurrentSessionEvent(event)) break;
      state.thinkingText = compactTail(`${state.thinkingText}${event.text}`, 2000);
      upsertThinkingActivity(event);
      break;
    case "thinking.done":
      if (!isCurrentSessionEvent(event)) break;
      upsertThinkingActivity({ ...event, done: true });
      break;
    case "tool.start":
      if (!isCurrentSessionEvent(event)) break;
      flushPendingText();
      state.tools.set(event.tool_use_id, { name: event.name, args: "", done: false });
      addActivity(event.type, `${event.name} started`, event);
      break;
    case "tool.delta": {
      if (!isCurrentSessionEvent(event)) break;
      const tool = state.tools.get(event.tool_use_id);
      if (tool) tool.args += event.partial_json;
      break;
    }
    case "tool.result": {
      if (!isCurrentSessionEvent(event)) break;
      const tool = state.tools.get(event.tool_use_id) || { name: event.name, args: "" };
      tool.done = true;
      tool.result = event.result;
      state.tools.set(event.tool_use_id, tool);
      const resultPreview = compactTail(String(event.result || ""), 100);
      addActivity(event.type, `${event.name} -> ${resultPreview}`, { ...event, args: tool.args });
      break;
    }
    case "approval.requested":
      if (!isCurrentSessionEvent(event)) break;
      state.approvals.set(event.request_id, event);
      renderApprovals();
      addActivity(event.type, `${event.tool_name} requires approval`, event);
      break;
    case "approval.decided":
      state.approvals.delete(event.request_id);
      renderApprovals();
      if (isCurrentSessionEvent(event)) addActivity(event.type, event.accepted ? "Approval recorded" : "Approval not found", event);
      break;
    case "turn.done":
      setSessionActiveTurn(event.session_id, null);
      if (isCurrentSessionEvent(event)) {
        flushPendingText();
        finishActivity();
        state.currentSession = event.session || state.currentSession;
        if (state.currentSession) state.currentSession.active_turn_id = null;
        state.assistantNode = null;
        state._assistantRawText = "";
        renderHistory();
        updateSendBtn();
      }
      state.sessions = event.sessions || state.sessions;
      renderSessions();
      break;
    case "turn.cancelled":
      setSessionActiveTurn(event.session_id || state.sessionByTurn.get(event.turn_id), null);
      renderSessions();
      if (isCurrentSessionEvent(event)) {
        finishActivity();
        updateSendBtn();
        state.assistantNode = null;
        state.submittedAttachmentIds.clear();
        addActivity(event.type, "Turn cancelled", event);
      }
      break;
    case "turn.error":
      if (event.session_id) setSessionActiveTurn(event.session_id, null);
      renderSessions();
      if (isCurrentSessionEvent(event)) {
        finishActivity();
        updateSendBtn();
        state.submittedAttachmentIds.clear();
        addActivity(event.type, event.message || "unknown error", event);
      }
      break;
    case "session.error":
      state.sessions = event.sessions || state.sessions;
      renderSessions();
      addActivity(event.type, event.message || "session unavailable", event);
      break;
    case "compaction":
      addActivity(event.type, event.summary || "context compacted", event);
      break;
    case "command.result": {
      const pending = document.querySelector(".message.command.pending");
      if (pending && pending.dataset.command === event.command) {
        pending.classList.remove("pending");
        pending.querySelector(".bubble").innerHTML = renderMarkdown(event.text || "");
        scrollMessages(true);
      } else {
        appendSlashResult(event.command, event.text || "");
      }
      break;
    }
    default:
      if (event.type === "subagent.event" || event.type?.includes("status") || event.type?.includes("invoked") || event.type?.includes("memory")) {
        addActivity(event.type, summarizeEvent(event), event);
      }
  }
}

function renderRuntime(payload) {
  state.assistantName = payload.assistant_name || "Assistant";
  state.userName = payload.user_name || "User";
  state.thinkingLevel = payload.thinking_level || "off";
  if (state.thinkingLevel !== "off") state.lastThinkingLevel = state.thinkingLevel;
  state.runtime.agentId = payload.agent_id || "";
  state.runtime.modelRef = payload.model_ref || payload.model || "";
  state.runtime.imageModelRef = payload.image_model_ref || "";
  state.runtime.agentOptions = payload.agent_options || [];
  state.runtime.modelOptions = payload.model_options || [];
  state.runtime.imageModelOptions = payload.image_model_options || [];
  state.runtime.thinkingOptions = payload.thinking_options || state.runtime.thinkingOptions;
  if ($("workspaceLabel")) $("workspaceLabel").textContent = payload.workspace_dir || "";
  renderThinkingToggle();
  renderRuntimeEditor(payload);
  if (state.currentSession && !state.activeTurnId) renderHistory();
}

function renderRuntimeEditor(payload) {
  const root = $("runtimeInfo");
  root.innerHTML = "";
  root.appendChild(runtimeSelectRow({
    label: "Agent",
    value: state.runtime.agentId,
    options: state.runtime.agentOptions.map((agent) => ({
      value: agent.id,
      label: agent.name && agent.name !== agent.id ? `${agent.name} (${agent.id})` : agent.id,
    })),
    onChange: (value) => send("runtime.set", { agent_id: value }),
  }));
  root.appendChild(runtimeSelectRow({
    label: "Model",
    value: state.runtime.modelRef,
    options: state.runtime.modelOptions.map((model) => ({
      value: model.ref,
      label: runtimeModelLabel(model),
    })),
    onChange: (value) => send("runtime.set", { model_ref: value }),
  }));
  root.appendChild(runtimeSelectRow({
    label: "ImageModel",
    value: state.runtime.imageModelRef,
    options: state.runtime.imageModelOptions.map((model) => ({
      value: model.ref,
      label: runtimeModelLabel(model),
    })),
    allowUnknown: false,
    onChange: (value) => send("runtime.set", { image_model_ref: value }),
  }));
  root.appendChild(runtimeSelectRow({
    label: "Thinking",
    value: state.thinkingLevel || "low",
    options: state.runtime.thinkingOptions.map((level) => ({ value: level, label: level })),
    onChange: (value) => {
      state.thinkingLevel = value;
      if (value !== "off") state.lastThinkingLevel = value;
      renderThinkingToggle();
      send("runtime.set", { thinking_level: value });
    },
  }));

  [
    ["Assistant", state.assistantName],
    ["User", state.userName],
    ["Tools", (payload.tools || []).join(", ") || "none"],
    ["Workspace", payload.workspace_dir || ""],
  ].forEach(([k, v]) => {
    const row = document.createElement("div");
    row.className = "runtime-static-row";
    row.innerHTML = `<strong>${escapeHtml(k)}</strong><br>${escapeHtml(String(v || ""))}`;
    root.appendChild(row);
  });
}

function runtimeModelLabel(model) {
  const icon = capabilityIcon(model.input || []);
  if (!model.ref) return `${icon} ${model.name || "Native Vision"}`.trim();
  const text = model.name && model.name !== model.ref ? `${model.name} (${model.ref})` : model.ref;
  return `${icon} ${text}`.trim();
}

function capabilityIcon(input) {
  const modes = new Set(input || []);
  if (modes.has("image")) return "👁️";
  if (modes.has("text")) return "💬";
  return "◦";
}

function runtimeSelectRow({ label, value, options, onChange, allowUnknown = true }) {
  const selected = options.find((option) => option.value === value) || (allowUnknown ? { value, label: value } : options[0]);
  const menuId = `runtime-${label.toLowerCase()}-${Math.random().toString(16).slice(2)}`;
  const row = document.createElement("div");
  row.className = "runtime-field";
  row.innerHTML = `
    <span class="runtime-field-label">${escapeHtml(label)}</span>
    <div class="runtime-combobox">
      <button type="button" class="runtime-select-trigger" aria-haspopup="listbox" aria-expanded="false" aria-controls="${menuId}">
        <span>${escapeHtml(selected.label || "")}</span>
        <span class="runtime-select-chevron" aria-hidden="true">⌄</span>
      </button>
      <div id="${menuId}" class="runtime-select-menu" role="listbox" hidden></div>
    </div>`;

  const trigger = row.querySelector(".runtime-select-trigger");
  const menu = row.querySelector(".runtime-select-menu");
  const choices = allowUnknown && selected?.value && !options.some((option) => option.value === selected.value)
    ? [selected, ...options]
    : options;

  for (const option of choices) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "runtime-select-option";
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", String(option.value === value));
    item.dataset.value = option.value;
    item.innerHTML = `<span>${escapeHtml(option.label)}</span>`;
    item.onclick = () => {
      trigger.querySelector("span").textContent = option.label;
      closeRuntimeMenus();
      onChange(option.value);
    };
    menu.appendChild(item);
  }

  trigger.onclick = (event) => {
    event.stopPropagation();
    const isOpen = !menu.hidden;
    closeRuntimeMenus();
    if (!isOpen) {
      menu.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      state.runtime.openMenu = menuId;
    }
  };
  return row;
}

function closeRuntimeMenus() {
  document.querySelectorAll(".runtime-select-menu").forEach((menu) => {
    menu.hidden = true;
  });
  document.querySelectorAll(".runtime-select-trigger").forEach((trigger) => {
    trigger.setAttribute("aria-expanded", "false");
  });
  state.runtime.openMenu = "";
}

function renderSessions() {
  const query = $("sessionSearch").value.toLowerCase();
  $("sessionList").innerHTML = "";
  state.sessions
    .filter((s) => !query || sessionMatches(s, query))
    .forEach((session) => {
      const isRunning = Boolean(state.activeTurnsBySession.get(session.session_id) || session.active_turn_id);
      const btn = document.createElement("button");
      btn.className = `session-item ${state.currentSession?.session_id === session.session_id ? "active" : ""} ${isRunning ? "is-running" : ""}`;
      const startDate = session.created_at ? new Date(session.created_at * 1000).toLocaleDateString() : "";
      btn.innerHTML = `<span class="session-id">${escapeHtml(session.title || session.session_id.slice(0, 8))}</span>
        <span class="session-preview">${escapeHtml(session.preview || session.session_id.slice(0, 8))}</span>
        <span class="session-meta">
          <span class="session-meta-text">${session.message_count || 0} messages · ${escapeHtml(session.model || "")}${startDate ? ` · ${startDate}` : ""}</span>
          ${isRunning ? `<span class="session-spinner" title="Processing" aria-label="Processing"></span>` : ""}
        </span>`;
      btn.onclick = () => {
        send("session.select", { session_id: session.session_id });
        if (isMobileViewport()) closeDrawers();
      };
      $("sessionList").appendChild(btn);
    });
}

function sessionMatches(session, query) {
  return [
    session.session_id,
    session.title,
    session.preview,
    session.search_text,
    session.model,
  ].some((value) => String(value || "").toLowerCase().includes(query));
}

function renderHistory() {
  syncSessionActiveTurn(state.currentSession);
  updateSendBtn();
  state.assistantNode = null;
  state._assistantRawText = "";
  if (!activeTurnIdForCurrentSession()) resetActivity();
  $("messages").innerHTML = "";
  if (!state.currentSession) {
    updateConversationEmptyState();
    return;
  }
  const activitiesByIndex = groupActivitiesByInsertIndex(state.currentSession.activities || []);
  (state.currentSession.history || []).forEach((msg, index) => {
    const text = extractText(msg).trim();
    if (text) appendMessage(msg.role, text);
    for (const activity of activitiesByIndex.get(index) || []) {
      appendHistoricalActivitySummary(activity);
    }
  });
  for (const activity of activitiesByIndex.get(-1) || []) {
    appendHistoricalActivitySummary(activity);
  }
  updateConversationEmptyState();
  scrollMessages(true);
}

function appendMessage(role, text) {
  $("conversationRoot")?.classList.remove("is-empty");
  const wrap = document.createElement("article");
  wrap.className = `message ${role}`;
  wrap.innerHTML = `<div class="role">${escapeHtml(displayRole(role))}</div><div class="bubble"></div>`;
  const bubble = wrap.querySelector(".bubble");
  if (role === "assistant") {
    bubble.innerHTML = text ? renderMarkdown(text) : "";
  } else {
    bubble.textContent = text;
  }
  $("messages").appendChild(wrap);
  scrollMessages();
  return bubble;
}

function appendSlashResult(command, text) {
  $("conversationRoot")?.classList.remove("is-empty");
  const wrap = document.createElement("article");
  wrap.className = "message command";
  wrap.dataset.command = command;
  wrap.innerHTML = `<div class="role">${escapeHtml(command)}</div><div class="bubble"></div>`;
  const bubble = wrap.querySelector(".bubble");
  bubble.innerHTML = text ? renderMarkdown(text) : "";
  $("messages").appendChild(wrap);
  scrollMessages(true);
  return wrap;
}

function updateConversationEmptyState() {
  const hasMessages = $("messages").children.length > 0;
  $("conversationRoot")?.classList.toggle("is-empty", !hasMessages);
}

function displayRole(role) {
  if (role === "user") return state.userName || "User";
  if (role === "assistant") return state.assistantName || "Assistant";
  return role;
}

function renderApprovals() {
  document.querySelectorAll(".message.approval-card").forEach((card) => {
    const rid = card.dataset.requestId;
    if (!state.approvals.has(rid) && !card.classList.contains("is-decided")) {
      card.classList.add("is-decided");
      const decision = card.dataset.decision || "decided";
      card.querySelector(".approval-card-status").textContent =
        decision === "allow-once" ? "✓ Approved" : "✗ Denied";
      setTimeout(() => card.remove(), 1500);
    }
  });

  for (const approval of state.approvals.values()) {
    const existing = $("messages").querySelector(
      `.approval-card[data-request-id="${CSS.escape(approval.request_id)}"]`
    );
    if (existing) continue;

    const el = document.createElement("article");
    el.className = "message approval-card";
    el.dataset.requestId = approval.request_id;
    el.innerHTML = `
      <div class="approval-card-inner">
        <div class="approval-card-header">
          <span class="approval-card-icon" aria-hidden="true">⚠</span>
          <strong class="approval-card-name">${escapeHtml(approval.tool_name)}</strong>
          <span class="approval-card-status"></span>
        </div>
        <div class="approval-card-reason">${escapeHtml(approval.reason || "")}</div>
        <pre class="approval-card-args">${escapeHtml(JSON.stringify(approval.tool_args || {}, null, 2))}</pre>
        <div class="approval-actions">
          <button data-decision="allow-once">Approve</button>
          <button data-decision="deny">Deny</button>
        </div>
      </div>`;

    el.querySelectorAll("button[data-decision]").forEach((btn) => {
      btn.onclick = () => {
        el.dataset.decision = btn.dataset.decision;
        el.querySelectorAll("button").forEach((b) => (b.disabled = true));
        send("approval.decide", {
          request_id: approval.request_id,
          decision: btn.dataset.decision,
        });
      };
    });

    $("messages").appendChild(el);
    scrollMessages(true);
  }
}

function addEvent(kind, text, payload = null) {
  return addActivity(kind, text, payload);
}

function resetActivity() {
  state.activityItems = [];
  state.activityNode = null;
  state.currentActivityId = null;
  state.selectedActivityId = null;
  state.activityStartedAt = null;
  state.activityDurationMs = 0;
  state.thinkingActivityId = null;
  if (state.activityTimer) {
    clearInterval(state.activityTimer);
    state.activityTimer = null;
  }
  renderActivity();
}

function appendActivitySummary() {
  $("conversationRoot")?.classList.remove("is-empty");
  const id = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const wrap = document.createElement("article");
  wrap.className = "message activity-summary is-empty";
  wrap.dataset.activityId = id;
  wrap.innerHTML = `
    <button type="button" class="activity-pill" aria-label="Open activity">
      <span class="activity-pill-label">Thought</span>
      <span class="activity-pill-meta"></span>
      <span class="activity-pill-chevron" aria-hidden="true">›</span>
    </button>`;
  state.currentActivityId = id;
  state.selectedActivityId = id;
  state.activityTurns.set(id, {
    id,
    node: wrap,
    items: state.activityItems,
    startedAt: state.activityStartedAt,
    durationMs: state.activityDurationMs,
    thinkingActivityId: state.thinkingActivityId,
  });
  wrap.querySelector("button").onclick = () => {
    selectActivity(id);
    openDrawer("inspector");
  };
  $("messages").appendChild(wrap);
  ensureActivityTimer();
  renderActivitySummary();
  return wrap;
}

function appendHistoricalActivitySummary(record) {
  const items = replayActivityItems(record.payloads || []);
  const id = record.turn_id || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const wrap = document.createElement("article");
  wrap.className = "message activity-summary";
  wrap.dataset.activityId = id;
  wrap.innerHTML = `
    <button type="button" class="activity-pill" aria-label="Open activity">
      <span class="activity-pill-label">Thought</span>
      <span class="activity-pill-meta"></span>
      <span class="activity-pill-chevron" aria-hidden="true">›</span>
    </button>`;
  const activity = {
    id,
    node: wrap,
    items,
    startedAt: null,
    durationMs: Number(record.duration_ms || 0),
    thinkingActivityId: null,
  };
  state.activityTurns.set(id, activity);
  wrap.querySelector("button").onclick = () => {
    selectActivity(id);
    openDrawer("inspector");
  };
  $("messages").appendChild(wrap);
  renderActivitySummaryFor(activity);
  return wrap;
}

function ensureActivityTimer() {
  if (state.activityTimer) return;
  state.activityTimer = setInterval(() => {
    if (!state.activeTurnId || !state.activityStartedAt) return;
    state.activityDurationMs = Date.now() - state.activityStartedAt;
    syncCurrentActivityRecord();
    renderActivitySummary();
    renderActivityHeader();
  }, 1000);
}

function finishActivity() {
  if (state.activityStartedAt) {
    state.activityDurationMs = Date.now() - state.activityStartedAt;
  }
  syncCurrentActivityRecord();
  if (state.activityTimer) {
    clearInterval(state.activityTimer);
    state.activityTimer = null;
  }
  renderActivitySummary();
  renderActivityHeader();
}

function addActivity(kind, text, payload = null) {
  const SILENT_KINDS = new Set(["connected", "disconnected"]);
  if (SILENT_KINDS.has(kind)) return null;

  const timestamp = new Date().toLocaleTimeString([], { hour12: false });
  const details = payload || { type: kind, message: text };
  const item = {
    id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
    kind,
    label: labelEvent(kind),
    icon: eventIcon(kind),
    text: String(text || ""),
    timestamp,
    details,
  };
  state.activityItems.push(item);
  if (state.activityItems.length > 150) state.activityItems.shift();
  if (!state.activityNode && state.activeTurnId) state.activityNode = appendActivitySummary();
  syncCurrentActivityRecord();
  renderActivitySummary();
  renderActivity();
  return item;
}

function upsertThinkingActivity(event) {
  const text = state.thinkingText ? compactTail(state.thinkingText, 180) : "Thinking";
  const details = { ...event, type: event.type || "thinking.delta", full_text: state.thinkingText };
  const existing = state.activityItems.find((item) => item.id === state.thinkingActivityId);
  if (existing) {
    existing.text = text;
    existing.details = details;
    renderActivitySummary();
    renderActivity();
    if (event.done) {
      state.thinkingActivityId = null;
      state.thinkingText = "";
    }
    return existing;
  }
  const item = addActivity("thinking.done", text, details);
  if (event.done) {
    state.thinkingActivityId = null;
    state.thinkingText = "";
  } else {
    state.thinkingActivityId = item?.id || null;
  }
  return item;
}

function eventIcon(kind) {
  if (kind === "tool.start") return "⚙";
  if (kind === "tool.result") return "✓";
  if (kind === "subagent.status") return "◈";
  if (kind === "subagent.event") return "◈";
  if (kind === "turn.done") return "◎";
  if (kind === "turn.error") return "!";
  if (kind === "turn.cancelled") return "⊘";
  if (kind === "thinking.done") return "◌";
  if (kind === "compaction") return "⌗";
  if (kind.includes("skill")) return "◇";
  if (kind.includes("memory")) return "✦";
  return "·";
}

function flushPendingText() {
  state._pendingTextLen = 0;
}

function labelEvent(kind) {
  if (kind === "thinking.done") return "Thinking";
  if (kind === "tool.start") return "Tool call";
  if (kind === "tool.result") return "Tool result";
  if (kind === "subagent.status") return "Subagent";
  if (kind === "subagent.event") return "Subagent";
  if (kind === "approval.requested") return "Approval";
  if (kind === "approval.decided") return "Approval";
  if (kind === "turn.done") return "Done";
  if (kind === "turn.error") return "Error";
  if (kind === "turn.cancelled") return "Cancelled";
  if (kind === "text") return "Response";
  if (kind.includes("skill")) return "Skill";
  if (kind.includes("memory")) return "Memory";
  return kind;
}

function renderActivitySummary() {
  const activity = getCurrentActivity();
  renderActivitySummaryFor(activity || null);
}

function renderActivitySummaryFor(activity) {
  const node = activity?.node || state.activityNode;
  if (!node) return;
  const count = (activity?.items || state.activityItems).length;
  node.classList.toggle("is-empty", count === 0);
  const seconds = Math.max(0, Math.round(((activity?.durationMs ?? state.activityDurationMs) || 0) / 1000));
  const label = node.querySelector(".activity-pill-label");
  const meta = node.querySelector(".activity-pill-meta");
  label.textContent = seconds > 0 ? `Thought for ${seconds}s` : "Thought";
  meta.textContent = count > 0 ? `${count} step${count === 1 ? "" : "s"}` : "";
}

function renderActivityHeader() {
  const title = $("activityTitle");
  const meta = $("activityMeta");
  if (!title || !meta) return;
  const activity = getSelectedActivity();
  const seconds = Math.max(0, Math.round(((activity?.durationMs ?? state.activityDurationMs) || 0) / 1000));
  title.textContent = "Activity";
  meta.textContent = seconds > 0 ? `· ${seconds}s` : "";
}

function renderActivity() {
  renderActivityHeader();
  const list = $("activityList");
  if (!list) return;
  const activity = getSelectedActivity();
  const items = activity?.items || state.activityItems;
  list.innerHTML = "";
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "activity-empty";
    empty.textContent = "No activity";
    list.appendChild(empty);
    return;
  }
  for (const item of items) {
    const entry = document.createElement("details");
    entry.className = `activity-item ${item.kind.replace(/[^a-z0-9_-]/gi, "-")}`;
    entry.innerHTML = `
      <summary class="activity-item-summary">
        <span class="activity-item-icon" aria-hidden="true">${escapeHtml(item.icon)}</span>
        <span class="activity-item-main">
          <span class="activity-item-label">${escapeHtml(item.label)}</span>
          <span class="activity-item-text">${escapeHtml(item.text)}</span>
        </span>
        <span class="activity-item-time">${escapeHtml(item.timestamp)}</span>
      </summary>
      <pre class="activity-item-detail"></pre>`;
    entry.querySelector(".activity-item-detail").textContent = JSON.stringify(item.details, null, 2);
    list.appendChild(entry);
  }
}

function getCurrentActivity() {
  return state.currentActivityId ? state.activityTurns.get(state.currentActivityId) : null;
}

function getSelectedActivity() {
  return state.selectedActivityId ? state.activityTurns.get(state.selectedActivityId) : getCurrentActivity();
}

function syncCurrentActivityRecord() {
  const activity = getCurrentActivity();
  if (!activity) return;
  activity.items = state.activityItems;
  activity.node = state.activityNode || activity.node;
  activity.startedAt = state.activityStartedAt;
  activity.durationMs = state.activityDurationMs;
  activity.thinkingActivityId = state.thinkingActivityId;
}

function selectActivity(id) {
  if (!state.activityTurns.has(id)) return;
  state.selectedActivityId = id;
  renderActivity();
}

function groupActivitiesByInsertIndex(activities) {
  const result = new Map();
  const historyLength = (state.currentSession?.history || []).length;
  for (const activity of activities) {
    const rawIndex = Number(activity.insert_after_index);
    const index = Number.isInteger(rawIndex) && rawIndex >= 0 && rawIndex < historyLength ? rawIndex : -1;
    if (!result.has(index)) result.set(index, []);
    result.get(index).push(activity);
  }
  return result;
}

function replayActivityItems(payloads) {
  const items = [];
  let thinkingText = "";
  let thinkingActivityId = null;

  const pushItem = (kind, text, payload) => {
    const item = {
      id: `${payload.turn_id || "history"}-${items.length}`,
      kind,
      label: labelEvent(kind),
      icon: eventIcon(kind),
      text: String(text || ""),
      timestamp: "",
      details: payload || { type: kind, message: text },
    };
    items.push(item);
    return item;
  };

  for (const payload of payloads || []) {
    const kind = payload.type || "event";
    if (kind === "thinking.delta") {
      thinkingText = compactTail(`${thinkingText}${payload.text || ""}`, 2000);
      const text = thinkingText ? compactTail(thinkingText, 180) : "Thinking";
      const details = { ...payload, full_text: thinkingText };
      const existing = items.find((item) => item.id === thinkingActivityId);
      if (existing) {
        existing.text = text;
        existing.details = details;
      } else {
        const item = pushItem("thinking.done", text, details);
        thinkingActivityId = item.id;
      }
    } else if (kind === "thinking.done") {
      const text = thinkingText ? compactTail(thinkingText, 180) : "Thinking";
      const details = { ...payload, done: true, full_text: thinkingText };
      const existing = items.find((item) => item.id === thinkingActivityId);
      if (existing) {
        existing.text = text;
        existing.details = details;
      } else {
        pushItem("thinking.done", text, details);
      }
      thinkingActivityId = null;
      thinkingText = "";
    } else if (kind === "tool.start") {
      pushItem(kind, `${payload.name} started`, payload);
    } else if (kind === "tool.result") {
      const resultPreview = compactTail(String(payload.result || ""), 100);
      pushItem(kind, `${payload.name} -> ${resultPreview}`, payload);
    } else if (kind === "approval.requested") {
      pushItem(kind, `${payload.tool_name} requires approval`, payload);
    } else if (kind === "turn.done") {
      if (items.length) pushItem(kind, "Turn done", payload);
    } else if (kind === "compaction") {
      pushItem(kind, payload.summary || "context compacted", payload);
    } else if (kind === "subagent.event" || kind?.includes("status") || kind?.includes("invoked") || kind?.includes("memory")) {
      pushItem(kind, summarizeEvent(payload), payload);
    }
  }
  return items;
}

function summarizeEvent(event) {
  if (event.type === "subagent.status") return summarizeSubagentEvent(event);
  if (event.type === "subagent.event") return summarizeSubagentNestedEvent(event);
  if (event.skill_name) return event.skill_name;
  if (event.status) return event.status;
  if (event.message) return event.message;
  return JSON.stringify(event).slice(0, 120);
}

function summarizeSubagentEvent(event) {
  const task = compactTail(event.label || event.task || event.run_id || "subagent", 80);
  const status = event.status || "updated";
  if (status === "spawned") return `Spawned: ${task}`;
  if (status === "progress") {
    const activity = event.current_activity ? ` · ${event.current_activity}` : "";
    return `Running: ${task}${activity}`;
  }
  if (status === "killed") return `Killed: ${task}`;
  const elapsed = formatElapsed(event.elapsed_ms);
  const result = event.error_message || event.result_text || "";
  const preview = result ? ` · ${compactTail(result, 120)}` : "";
  return `${capitalize(status)}: ${task}${elapsed ? ` (${elapsed})` : ""}${preview}`;
}

function summarizeSubagentNestedEvent(event) {
  const child = event.event || {};
  const prefix = subagentPrefix(event);
  if (child.type === "thinking.done") return `${prefix}Thinking done${child.redacted ? " · redacted" : ""}`;
  if (child.type === "tool.result") {
    const resultPreview = compactTail(String(child.result || ""), 120);
    return `${prefix}Tool result · ${child.name || "tool"}${resultPreview ? ` -> ${resultPreview}` : ""}`;
  }
  if (child.type === "skill.invoked") return `${prefix}Skill · ${child.skill_name || child.skill_path || "skill"}`;
  return `${prefix}${child.type || "event"}`;
}

function subagentPrefix(event) {
  const label = compactTail(event.label || event.task || "subagent", 48);
  const run = event.run_id ? `#${String(event.run_id).slice(0, 8)}` : "";
  return `[${label}${run ? ` ${run}` : ""}] `;
}

function formatElapsed(ms) {
  const value = Number(ms);
  if (!Number.isFinite(value) || value <= 0) return "";
  const seconds = Math.round(value / 1000);
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function capitalize(text) {
  const value = String(text || "");
  return value ? `${value[0].toUpperCase()}${value.slice(1)}` : "";
}

function compactTail(text, limit) {
  const compact = text.replace(/\s+/g, " ").trim();
  if (compact.length <= limit) return compact;
  return `...${compact.slice(-limit)}`;
}

function extractText(msg) {
  return (msg.content || [])
    .filter((b) => b && b.type === "text")
    .map((b) => b.text || "")
    .join("\n");
}

let _programmaticScroll = false;

function scrollMessages(force = false) {
  const el = $("messages");
  if (force) {
    state._followBottom = true;
    _programmaticScroll = true;
    el.scrollTop = el.scrollHeight;
  } else if (state._followBottom) {
    _programmaticScroll = true;
    el.scrollTop = el.scrollHeight;
  }
}

function renderMarkdown(text) {
  if (typeof marked === "undefined") {
    return escapeHtml(text).replace(/\n/g, "<br>");
  }
  return marked.parse(text, { breaks: true, gfm: true });
}

function escapeHtml(value) {
  return value.replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  }[ch]));
}

function showTokenDialog() {
  $("tokenDialog").classList.remove("hidden");
}

function formatAcceptedUserText(event) {
  const text = String(event.user_text || "").trim();
  const attachments = event.attachments || [];
  if (!attachments.length) return text;
  const names = attachments.map((item) => `${item.name} (${formatBytes(item.size || 0)})`).join(", ");
  return text ? `${text}\n\nAttached: ${names}` : `Attached: ${names}`;
}

function isMobileViewport() {
  return window.innerWidth <= 720;
}

function isTouchViewport() {
  return window.matchMedia("(pointer: coarse)").matches || isMobileViewport();
}

function resizePrompt() {
  const prompt = $("prompt");
  const minHeight = Number.parseFloat(getComputedStyle(prompt).minHeight) || 40;
  const maxHeight = isMobileViewport() ? 132 : 208;
  prompt.style.height = `${minHeight}px`;
  const nextHeight = Math.min(Math.max(prompt.scrollHeight, minHeight), maxHeight);
  prompt.style.height = `${nextHeight}px`;
  prompt.style.overflowY = prompt.scrollHeight > maxHeight ? "auto" : "hidden";
}

function renderAttachments() {
  const list = $("attachmentList");
  list.innerHTML = "";
  list.hidden = state.attachments.length === 0;
  for (const item of state.attachments) {
    const chip = document.createElement("div");
    chip.className = `attachment-chip ${item.error ? "is-error" : ""}`;
    chip.title = item.error || item.file.name;
    chip.innerHTML = `<span class="attachment-name">${escapeHtml(item.file.name)}</span>
      <span class="attachment-size">${escapeHtml(item.error || formatBytes(item.file.size))}</span>
      <button type="button" class="attachment-remove" aria-label="Remove ${escapeHtml(item.file.name)}">×</button>`;
    chip.querySelector("button").onclick = () => {
      state.attachments = state.attachments.filter((candidate) => candidate.id !== item.id);
      renderAttachments();
    };
    list.appendChild(chip);
  }
}

function addAttachmentFiles(files) {
  for (const file of files) {
    const id = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const next = { id, file, error: validateAttachment(file) };
    if (state.attachments.length >= MAX_ATTACHMENTS) {
      next.error = `最多 ${MAX_ATTACHMENTS} 个文件`;
    }
    state.attachments.push(next);
  }
  validateAttachmentSet();
  renderAttachments();
}

function validateAttachment(file) {
  if (!ALLOWED_ATTACHMENT_TYPES.has(file.type)) {
    return "不支持的类型";
  }
  if (!file.type.startsWith("image/") && file.size > MAX_NON_IMAGE_BYTES) {
    return `超过 ${formatBytes(MAX_NON_IMAGE_BYTES)}`;
  }
  if (file.size <= 0) {
    return "空文件";
  }
  return "";
}

function validateAttachmentSet() {
  const total = state.attachments.reduce((sum, item) => sum + item.file.size, 0);
  for (const item of state.attachments) {
    const ownError = validateAttachment(item.file);
    item.error = ownError;
    if (!item.error && total > MAX_TOTAL_ATTACHMENT_BYTES) {
      item.error = `总大小超过 ${formatBytes(MAX_TOTAL_ATTACHMENT_BYTES)}`;
    }
  }
}

function hasAttachmentErrors() {
  return state.attachments.some((item) => item.error);
}

async function buildAttachmentPayloads(items = state.attachments) {
  const payloads = [];
  for (const item of items) {
    const data = await fileToBase64(item.file);
    payloads.push({
      name: item.file.name,
      mime: item.file.type || "application/octet-stream",
      size: item.file.size,
      data,
    });
  }
  return payloads;
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const value = String(reader.result || "");
      resolve(value.includes(",") ? value.split(",", 2)[1] : value);
    };
    reader.onerror = () => reject(reader.error || new Error("failed to read file"));
    reader.readAsDataURL(file);
  });
}

function formatBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
}

function openDrawer(type) {
  if (!isMobileViewport()) {
    if (type === "sessions") {
      setDesktopSidebarCollapsed(!document.body.classList.contains("sidebar-collapsed"));
      return;
    }
    const inspectorDrawer = $("inspectorDrawer");
    const backdrop = $("drawerBackdrop");
    if (type !== "inspector") {
      syncDrawerStateForViewport();
      return;
    }
    const isOpen = inspectorDrawer.classList.contains("is-open");
    state.openDrawer = isOpen ? null : "inspector";
    inspectorDrawer.classList.toggle("is-open", !isOpen);
    inspectorDrawer.setAttribute("aria-hidden", String(isOpen));
    backdrop.hidden = isOpen;
    backdrop.classList.toggle("is-visible", !isOpen);
    return;
  }
  const sessionDrawer = $("sessionDrawer");
  const inspectorDrawer = $("inspectorDrawer");
  const backdrop = $("drawerBackdrop");
  const target = type === "inspector" ? inspectorDrawer : sessionDrawer;

  state.openDrawer = type;
  sessionDrawer.classList.toggle("is-open", type === "sessions");
  inspectorDrawer.classList.toggle("is-open", type === "inspector");
  sessionDrawer.setAttribute("aria-hidden", String(type !== "sessions"));
  inspectorDrawer.setAttribute("aria-hidden", String(type !== "inspector"));
  backdrop.hidden = false;
  requestAnimationFrame(() => backdrop.classList.add("is-visible"));
  document.body.classList.add("drawer-open");
  target.querySelector("input, button")?.focus();
}

function closeDrawers() {
  const sessionDrawer = $("sessionDrawer");
  const inspectorDrawer = $("inspectorDrawer");
  const backdrop = $("drawerBackdrop");

  state.openDrawer = null;
  sessionDrawer.classList.remove("is-open");
  inspectorDrawer.classList.remove("is-open");
  sessionDrawer.setAttribute("aria-hidden", String(isMobileViewport()));
  inspectorDrawer.setAttribute("aria-hidden", String(isMobileViewport()));
  backdrop.classList.remove("is-visible");
  document.body.classList.remove("drawer-open");
  window.setTimeout(() => {
    if (!state.openDrawer) backdrop.hidden = true;
  }, 180);
}

function setDesktopSidebarCollapsed(collapsed) {
  if (isMobileViewport()) return;
  document.body.classList.toggle("sidebar-collapsed", collapsed);
  $("sessionDrawer").setAttribute("aria-hidden", String(collapsed));
  $("openSessionDrawerBtn").setAttribute("aria-expanded", String(!collapsed));
}

function syncDrawerStateForViewport() {
  const sessionDrawer = $("sessionDrawer");
  const inspectorDrawer = $("inspectorDrawer");
  const backdrop = $("drawerBackdrop");

  if (isMobileViewport()) {
    document.body.classList.remove("sidebar-collapsed");
    sessionDrawer.setAttribute("aria-hidden", String(state.openDrawer !== "sessions"));
    inspectorDrawer.setAttribute("aria-hidden", String(state.openDrawer !== "inspector"));
    return;
  }

  state.openDrawer = null;
  sessionDrawer.classList.remove("is-open");
  inspectorDrawer.classList.remove("is-open");
  const sidebarCollapsed = document.body.classList.contains("sidebar-collapsed");
  sessionDrawer.setAttribute("aria-hidden", String(sidebarCollapsed));
  $("openSessionDrawerBtn").setAttribute("aria-expanded", String(!sidebarCollapsed));
  inspectorDrawer.setAttribute("aria-hidden", "true");
  backdrop.classList.remove("is-visible");
  backdrop.hidden = true;
  document.body.classList.remove("drawer-open");
}

function renderThinkingToggle() {
  const btn = $("thinkingToggle");
  if (!btn) return;
  const enabled = state.thinkingLevel !== "off";
  btn.classList.toggle("is-on", enabled);
  btn.setAttribute("aria-pressed", String(enabled));
  btn.title = enabled ? `Thinking: ${state.thinkingLevel}` : "Thinking: off";
}

$("sendBtn").onclick = (event) => {
  updateSendBtn();
  if (state.activeTurnId) {
    event.preventDefault();
    send("turn.cancel", { turn_id: state.activeTurnId });
  }
};

// Mirror of gateway/slash.py::_HANDLERS so the WebUI front-end routes the
// same set of commands through ``command.run`` instead of leaking them to
// chat.send (where the agent would treat ``/model`` as a regular message
// and likely fire the ``switch_model`` tool). Keep in sync with
// ``_HANDLERS`` and ``HELP_TEXT`` in nano_openclaw/gateway/slash.py.
const BUILTIN_COMMANDS = new Set([
  // banner / lifecycle
  "help", "quit", "exit", "q", "save",
  // session lifecycle
  "clear", "new", "sessions", "session",
  // context
  "context", "compact",
  // introspection
  "tools", "skills", "plugins", "hooks", "subagents",
  // memory
  "active-memory", "dreaming",
  // daemon-introspection
  "health", "channels", "runtime",
  // models
  "models", "model",
  // runtime tuning
  "thinking",
  // gateway lifecycle
  "restart",
]);

$("composer").onsubmit = async (event) => {
  event.preventDefault();
  if (activeTurnIdForCurrentSession()) {
    send("turn.cancel", { turn_id: activeTurnIdForCurrentSession() });
    return;
  }
  const text = $("prompt").value.trim();
  if (!text && !state.attachments.length) return;
  if (hasAttachmentErrors()) {
    addEvent("attachment.error", "Fix attachment errors before sending", {
      type: "attachment.error",
      attachments: state.attachments.map((item) => ({ name: item.file.name, error: item.error })),
    });
    return;
  }

  if (text.startsWith("/") && !state.attachments.length) {
    const verb = text.slice(1).split(/\s+/)[0].toLowerCase();
    if (BUILTIN_COMMANDS.has(verb)) {
      const msgEl = appendSlashResult(text, "…");
      msgEl.classList.add("pending");
      send("command.run", {
        command: text,
        session_id: state.currentSession?.session_id ?? null,
      });
      $("prompt").value = "";
      resizePrompt();
      return;
    }
    // Unknown slash command (skills etc.) → fall through to agent loop, same as CLI
  }

  if (!state.currentSession) return;
  const submittedAttachments = [...state.attachments];
  const submittedAttachmentIds = new Set(submittedAttachments.map((item) => item.id));
  let attachments = [];
  try {
    attachments = await buildAttachmentPayloads(submittedAttachments);
  } catch (error) {
    addEvent("attachment.error", String(error?.message || error), { type: "attachment.error" });
    return;
  }

  const didSend = send("chat.send", { session_id: state.currentSession.session_id, text, attachments });
  if (!didSend) return;
  $("prompt").value = "";
  state.submittedAttachmentIds = submittedAttachmentIds;
  resizePrompt();
};

$("prompt").onkeydown = (event) => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing || isTouchViewport()) return;
  event.preventDefault();
  $("composer").requestSubmit();
};

$("newSessionNavBtn").onclick = async () => {
  const data = await api("/api/sessions", { method: "POST", body: "{}" });
  state.sessions = data.sessions;
  state.currentSession = data.session;
  syncSessionActiveTurn(state.currentSession);
  renderSessions();
  updateSendBtn();
  renderHistory();
  if (isMobileViewport()) closeDrawers();
};

$("sessionSearch").oninput = renderSessions;
$("prompt").oninput = resizePrompt;
$("attachmentInput").onchange = (event) => {
  addAttachmentFiles(Array.from(event.target.files || []));
  event.target.value = "";
};
document.querySelector(".composer-tool").onclick = () => $("attachmentInput").click();
$("thinkingToggle").onclick = () => {
  const nextLevel = state.thinkingLevel === "off" ? state.lastThinkingLevel : "off";
  state.thinkingLevel = nextLevel;
  renderThinkingToggle();
  send("thinking.set", { level: nextLevel });
};

$("tokenSave").onclick = () => {
  state.token = $("tokenInput").value.trim();
  localStorage.setItem("nanoOpenClawToken", state.token);
  $("tokenDialog").classList.add("hidden");
  connect();
  $("prompt").focus();
};

$("messages").addEventListener("scroll", () => {
  if (_programmaticScroll) {
    _programmaticScroll = false;
    return;
  }
  const el = $("messages");
  state._followBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
});

$("openSessionDrawerBtn").onclick = () => openDrawer("sessions");
$("openInspectorBtn").onclick = () => openDrawer("inspector");
$("openInspectorDesktopBtn").onclick = () => openDrawer("inspector");
$("appearanceBtn").onclick = (event) => {
  event.stopPropagation();
  const menu = $("appearanceOptions");
  const expanded = $("appearanceBtn").getAttribute("aria-expanded") === "true";
  menu.hidden = expanded;
  $("appearanceBtn").setAttribute("aria-expanded", String(!expanded));
};
$("appearanceOptions").querySelectorAll("[data-theme-choice]").forEach((option) => {
  option.onclick = () => {
    setThemePreference(option.dataset.themeChoice);
    closeAppearanceMenu();
  };
});
$("closeSessionDrawerBtn").onclick = () => {
  if (isMobileViewport()) closeDrawers();
  else setDesktopSidebarCollapsed(true);
};
$("closeInspectorBtn").onclick = closeDrawers;
$("drawerBackdrop").onclick = closeDrawers;

window.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeRuntimeMenus();
  if (event.key === "Escape") closeAppearanceMenu();
  if (event.key === "Escape" && state.openDrawer) closeDrawers();
});

document.addEventListener("click", (event) => {
  if (!event.target.closest(".appearance-menu")) closeAppearanceMenu();
  if (!event.target.closest(".runtime-combobox")) closeRuntimeMenus();
});

window.addEventListener("resize", () => {
  syncDrawerStateForViewport();
  resizePrompt();
});

themeMedia.addEventListener("change", () => {
  if (state.themePreference === "system") applyThemePreference();
});

applyThemePreference();
syncDrawerStateForViewport();
connect();
$("prompt").focus();
resizePrompt();
