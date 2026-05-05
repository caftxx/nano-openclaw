const state = {
  token: localStorage.getItem("nanoOpenClawToken") || "",
  ws: null,
  reconnectDelay: 1200,
  sessions: [],
  currentSession: null,
  activeTurnId: null,
  assistantNode: null,
  approvals: new Map(),
  tools: new Map(),
  thinkingText: "",
  assistantName: "Assistant",
  userName: "User",
  _pendingTextLen: 0,
  _assistantRawText: "",
  _followBottom: true,
  openDrawer: null,
  thinkingLevel: "off",
  lastThinkingLevel: "low",
  attachments: [],
  clearAttachmentsOnAccept: false,
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

function updateSendBtn() {
  const btn = $("sendBtn");
  if (state.activeTurnId) {
    btn.textContent = "⬛";
    btn.title = "Cancel";
    btn.classList.add("stop-mode");
  } else {
    btn.textContent = "↑";
    btn.title = "Send";
    btn.classList.remove("stop-mode");
  }
}

function handleEvent(event) {
  switch (event.type) {
    case "state.updated":
      renderRuntime(event);
      break;
    case "session.updated":
      state.sessions = event.sessions || state.sessions;
      state.currentSession = event.session || state.currentSession;
      renderSessions();
      renderHistory();
      break;
    case "chat.accepted":
      // Remove pending slash-command block if a skill was routed to the agent loop
      document.querySelectorAll(".message.command.pending").forEach((el) => el.remove());
      state.activeTurnId = event.turn_id;
      updateSendBtn();
      state.tools.clear();
      state.thinkingText = "";
      state._pendingTextLen = 0;
      state._assistantRawText = "";
      document.querySelectorAll(".message.turn-event, .message.approval-card").forEach((el) => el.remove());
      appendMessage("user", formatAcceptedUserText(event));
      if (state.clearAttachmentsOnAccept) {
        state.attachments = [];
        state.clearAttachmentsOnAccept = false;
        renderAttachments();
      }
      state.assistantNode = appendMessage("assistant", "");
      scrollMessages(true);
      addEvent(event.type, "Turn accepted", event);
      break;
    case "text.delta":
      if (!state.assistantNode) state.assistantNode = appendMessage("assistant", "");
      state._assistantRawText += event.text;
      state.assistantNode.innerHTML = renderMarkdown(state._assistantRawText);
      state._pendingTextLen += event.text.length;
      scrollMessages();
      break;
    case "thinking.delta":
      state.thinkingText = compactTail(`${state.thinkingText}${event.text}`, 2000);
      break;
    case "thinking.done":
      addEvent(event.type, "", { ...event, full_text: state.thinkingText });
      break;
    case "tool.start":
      flushPendingText();
      state.tools.set(event.tool_use_id, { name: event.name, args: "", done: false });
      addEvent(event.type, `${event.name} started`, event);
      break;
    case "tool.delta": {
      const tool = state.tools.get(event.tool_use_id);
      if (tool) tool.args += event.partial_json;
      break;
    }
    case "tool.result": {
      const tool = state.tools.get(event.tool_use_id) || { name: event.name, args: "" };
      tool.done = true;
      tool.result = event.result;
      state.tools.set(event.tool_use_id, tool);
      const resultPreview = compactTail(String(event.result || ""), 100);
      addEvent(event.type, `${event.name} → ${resultPreview}`, { ...event, args: tool.args });
      break;
    }
    case "approval.requested":
      state.approvals.set(event.request_id, event);
      renderApprovals();
      addEvent(event.type, `${event.tool_name} requires approval`, event);
      break;
    case "approval.decided":
      state.approvals.delete(event.request_id);
      renderApprovals();
      addEvent(event.type, event.accepted ? "Approval recorded" : "Approval not found", event);
      break;
    case "turn.done":
      flushPendingText();
      state.activeTurnId = null;
      updateSendBtn();
      if (state.assistantNode) {
        if (!state._assistantRawText.trim()) {
          state.assistantNode.closest(".message")?.remove();
        } else {
          state.assistantNode.innerHTML = renderMarkdown(state._assistantRawText);
        }
        state.assistantNode = null;
      }
      state._assistantRawText = "";
      state.currentSession = event.session || state.currentSession;
      state.sessions = event.sessions || state.sessions;
      renderSessions();
      addEvent(event.type, "Turn done", event);
      break;
    case "turn.cancelled":
      state.activeTurnId = null;
      updateSendBtn();
      state.assistantNode = null;
      addEvent(event.type, "Turn cancelled", event);
      break;
    case "turn.error":
      state.activeTurnId = null;
      updateSendBtn();
      state.clearAttachmentsOnAccept = false;
      addEvent(event.type, event.message || "unknown error", event);
      break;
    case "session.error":
      state.sessions = event.sessions || state.sessions;
      renderSessions();
      addEvent(event.type, event.message || "session unavailable", event);
      break;
    case "compaction":
      addEvent(event.type, event.summary || "context compacted", event);
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
      if (event.type?.includes("status") || event.type?.includes("invoked") || event.type?.includes("memory")) {
        addEvent(event.type, summarizeEvent(event), event);
      }
  }
}

function renderRuntime(payload) {
  state.assistantName = payload.assistant_name || "Assistant";
  state.userName = payload.user_name || "User";
  state.thinkingLevel = payload.thinking_level || "off";
  if (state.thinkingLevel !== "off") state.lastThinkingLevel = state.thinkingLevel;
  $("modelLabel").textContent = payload.model || "model";
  if ($("workspaceLabel")) $("workspaceLabel").textContent = payload.workspace_dir || "";
  renderThinkingToggle();
  $("runtimeInfo").innerHTML = "";
  [
    ["Agent", payload.agent_id],
    ["Model", payload.model_ref || payload.model],
    ["Thinking", state.thinkingLevel],
    ["Assistant", state.assistantName],
    ["User", state.userName],
    ["Tools", (payload.tools || []).join(", ") || "none"],
    ["Workspace", payload.workspace_dir || ""],
  ].forEach(([k, v]) => {
    const row = document.createElement("div");
    row.innerHTML = `<strong>${escapeHtml(k)}</strong><br>${escapeHtml(String(v || ""))}`;
    $("runtimeInfo").appendChild(row);
  });
  if (state.currentSession && !state.activeTurnId) renderHistory();
}

function renderSessions() {
  const query = $("sessionSearch").value.toLowerCase();
  $("sessionList").innerHTML = "";
  state.sessions
    .filter((s) => !query || sessionMatches(s, query))
    .forEach((session) => {
      const btn = document.createElement("button");
      btn.className = `session-item ${state.currentSession?.session_id === session.session_id ? "active" : ""}`;
      btn.innerHTML = `<span class="session-id">${escapeHtml(session.title || session.session_id.slice(0, 8))}</span>
        <span class="session-preview">${escapeHtml(session.preview || session.session_id.slice(0, 8))}</span>
        <span class="session-meta">${session.message_count || 0} messages · ${escapeHtml(session.model || "")}</span>`;
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
  $("messages").innerHTML = "";
  if (!state.currentSession) {
    updateConversationEmptyState();
    return;
  }
  for (const msg of state.currentSession.history || []) {
    const text = extractText(msg).trim();
    if (text) appendMessage(msg.role, text);
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
  const SILENT_KINDS = new Set(["connected", "disconnected"]);
  if (SILENT_KINDS.has(kind)) return null;

  const el = document.createElement("article");
  el.className = "message turn-event";
  el.dataset.kind = kind;

  const timestamp = new Date().toLocaleTimeString([], { hour12: false });
  const details = payload || { type: kind, message: text };

  const detailsEl = document.createElement("details");
  detailsEl.className = "turn-event-details";
  detailsEl.innerHTML = `
    <summary class="turn-event-summary">
      <span class="turn-event-icon" aria-hidden="true">${eventIcon(kind)}</span>
      <span class="turn-event-label">${escapeHtml(labelEvent(kind))}</span>
      <span class="turn-event-body">${escapeHtml(String(text || ""))}</span>
      <span class="turn-event-time">${escapeHtml(timestamp)}</span>
    </summary>
    <pre class="turn-event-detail"></pre>`;
  detailsEl.querySelector(".turn-event-detail").textContent = JSON.stringify(details, null, 2);

  el.appendChild(detailsEl);
  $("messages").appendChild(el);
  scrollMessages();

  const allEvents = $("messages").querySelectorAll(".turn-event");
  if (allEvents.length > 150) allEvents[0].remove();

  return el;
}

function eventIcon(kind) {
  if (kind === "tool.start") return "⚙";
  if (kind === "tool.result") return "✓";
  if (kind === "turn.done") return "◎";
  if (kind === "turn.error") return "✗";
  if (kind === "turn.cancelled") return "⊘";
  if (kind === "thinking.done") return "💭";
  if (kind === "compaction") return "⌗";
  return "·";
}

function flushPendingText() {
  if (state._pendingTextLen > 0) {
    addEvent("text", `${state._pendingTextLen} chars`, { type: "text", chars: state._pendingTextLen });
    state._pendingTextLen = 0;
  }
}

function labelEvent(kind) {
  return kind;
}

function summarizeEvent(event) {
  if (event.skill_name) return event.skill_name;
  if (event.status) return event.status;
  if (event.message) return event.message;
  return JSON.stringify(event).slice(0, 120);
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
  const minHeight = 40;
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

async function buildAttachmentPayloads() {
  const payloads = [];
  for (const item of state.attachments) {
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
  if (state.activeTurnId) {
    event.preventDefault();
    send("turn.cancel", { turn_id: state.activeTurnId });
  }
};

const BUILTIN_COMMANDS = new Set([
  "help", "context", "compact", "clear", "save",
  "skills", "plugins", "hooks", "subagents", "active-memory", "dreaming",
]);

$("composer").onsubmit = async (event) => {
  event.preventDefault();
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
      return;
    }
    // Unknown slash command (skills etc.) → fall through to agent loop, same as CLI
  }

  if (!state.currentSession) return;
  let attachments = [];
  try {
    attachments = await buildAttachmentPayloads();
  } catch (error) {
    addEvent("attachment.error", String(error?.message || error), { type: "attachment.error" });
    return;
  }

  const didSend = send("chat.send", { session_id: state.currentSession.session_id, text, attachments });
  if (!didSend) return;
  $("prompt").value = "";
  state.clearAttachmentsOnAccept = state.attachments.length > 0;
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
  renderSessions();
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
$("closeSessionDrawerBtn").onclick = () => {
  if (isMobileViewport()) closeDrawers();
  else setDesktopSidebarCollapsed(true);
};
$("closeInspectorBtn").onclick = closeDrawers;
$("drawerBackdrop").onclick = closeDrawers;

window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.openDrawer) closeDrawers();
});

window.addEventListener("resize", () => {
  syncDrawerStateForViewport();
  resizePrompt();
});

syncDrawerStateForViewport();
connect();
$("prompt").focus();
resizePrompt();
