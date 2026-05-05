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
};

const $ = (id) => document.getElementById(id);

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
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  state.ws.send(JSON.stringify({ type, ...payload }));
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
      $("events").innerHTML = "";
      _eventsFollowBottom = true;
      appendMessage("user", event.user_text);
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
  $("modelLabel").textContent = payload.model || "model";
  $("workspaceLabel").textContent = payload.workspace_dir || "";
  $("runtimeInfo").innerHTML = "";
  [
    ["Agent", payload.agent_id],
    ["Model", payload.model_ref || payload.model],
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
      btn.onclick = () => send("session.select", { session_id: session.session_id });
      $("sessionList").appendChild(btn);
    });
  $("sessionTitle").textContent = state.currentSession
    ? (state.currentSession.title || state.currentSession.session_id.slice(0, 8))
    : "No session";
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
  if (!state.currentSession) return;
  for (const msg of state.currentSession.history || []) {
    const text = extractText(msg).trim();
    if (text) appendMessage(msg.role, text);
  }
  scrollMessages(true);
}

function appendMessage(role, text) {
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

function displayRole(role) {
  if (role === "user") return state.userName || "User";
  if (role === "assistant") return state.assistantName || "Assistant";
  return role;
}

function renderApprovals() {
  const root = $("approvals");
  root.innerHTML = "";
  if (!state.approvals.size) {
    root.className = "empty";
    root.textContent = "No pending approvals";
    return;
  }
  root.className = "";
  for (const approval of state.approvals.values()) {
    const el = document.createElement("div");
    el.className = "approval";
    el.innerHTML = `<strong>${escapeHtml(approval.tool_name)}</strong>
      <div>${escapeHtml(approval.reason || "")}</div>
      <pre>${escapeHtml(JSON.stringify(approval.tool_args || {}, null, 2))}</pre>
      <div class="approval-actions">
        <button data-decision="allow-once">Approve</button>
        <button data-decision="deny">Deny</button>
      </div>`;
    el.querySelectorAll("button").forEach((btn) => {
      btn.onclick = () => send("approval.decide", {
        request_id: approval.request_id,
        decision: btn.dataset.decision,
      });
    });
    root.appendChild(el);
  }
}

function addEvent(kind, text, payload = null) {
  const el = document.createElement("details");
  el.className = "event";
  const timestamp = new Date().toLocaleTimeString([], { hour12: false });
  const details = payload || { type: kind, message: text };
  el.innerHTML = `<summary>
      <span class="event-time">${escapeHtml(timestamp)}</span>
      <strong>${escapeHtml(labelEvent(kind))}</strong>
      <span class="event-body"></span>
    </summary>
    <pre class="event-detail"></pre>`;
  el.querySelector(".event-body").textContent = String(text || "");
  el.querySelector(".event-detail").textContent = JSON.stringify(details, null, 2);
  $("events").appendChild(el);
  while ($("events").children.length > 120) $("events").firstChild.remove();
  if (_eventsFollowBottom) {
    _eventsProgrammaticScroll = true;
    $("events").scrollTop = $("events").scrollHeight;
  }
  return el;
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
let _eventsFollowBottom = true;
let _eventsProgrammaticScroll = false;

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

function isTouchViewport() {
  return window.matchMedia("(pointer: coarse)").matches || window.innerWidth <= 720;
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

$("composer").onsubmit = (event) => {
  event.preventDefault();
  const text = $("prompt").value.trim();
  if (!text) return;
  $("prompt").value = "";

  if (text.startsWith("/")) {
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
  send("chat.send", { session_id: state.currentSession.session_id, text });
};

$("prompt").onkeydown = (event) => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing || isTouchViewport()) return;
  event.preventDefault();
  $("composer").requestSubmit();
};

$("newSessionBtn").onclick = async () => {
  const data = await api("/api/sessions", { method: "POST", body: "{}" });
  state.sessions = data.sessions;
  state.currentSession = data.session;
  renderSessions();
  renderHistory();
};

$("sessionSearch").oninput = renderSessions;

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

$("events").addEventListener("scroll", () => {
  if (_eventsProgrammaticScroll) {
    _eventsProgrammaticScroll = false;
    return;
  }
  const el = $("events");
  _eventsFollowBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
});

connect();
$("prompt").focus();
