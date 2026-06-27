/* Web Voice AI podcast mode.
 *
 * Owns the configuration dialog, podcast event rendering, one-shot user
 * interjections, and per-utterance voice playback. Normal hands-free voice
 * mode remains in voice-shell.js.
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory({});
  else root.PodcastMode = factory(root);
})(typeof self !== "undefined" ? self : this, function (root) {
  "use strict";

  var ROLES = [
    "自动",
    "作家",
    "脱口秀工作者",
    "相声演员",
    "IT后台研发工程师",
    "IT前端研发工程师",
    "AI Agent研发工程师",
    "云计算架构师",
    "高性能网络协议设计师",
    "硬件工程师",
  ];
  var MODE_KEY = "nanoPodcastMode";
  var RUN_KEY = "nanoPodcastRunId";
  var AGENTS_KEY = "nanoGroupAgents";
  var TOPIC_KEY = "nanoGroupTopic";
  var MAX_GROUP_AGENTS = 9;
  var CLOUD_TTS_MAX_CONCURRENCY = 2;
  var CLOUD_TTS_MAX_ATTEMPTS = 3;
  var MEMBER_COLORS = ["#0f9f8f", "#2d7ff9", "#d49300", "#8b5cf6", "#e0527d", "#3ca65c", "#d76035", "#6475e8"];
  var MEMBER_EMOJIS = ["🦊", "🐼", "🐯", "🐧", "✍️", "🧑‍💻", "🧠", "🛠️"];
  var ROLE_EMOJIS = {
    "作家": "✍️",
    "脱口秀工作者": "🎭",
    "相声演员": "🎙️",
    "IT后台研发工程师": "🧑‍💻",
    "IT前端研发工程师": "🎨",
    "AI Agent研发工程师": "🧠",
    "云计算架构师": "☁️",
    "高性能网络协议设计师": "🌐",
    "硬件工程师": "🛠️",
  };

  var podcast = {
    runId: "",
    active: false,
    starting: false,
    agents: [],
    nextAgentId: 1,
    activeSpeakerKey: "",
    activeSpeakerMode: "",
    playingSpeakerKey: "",
    lastTopic: "",
    removedAgentIds: [],
    removedSpeakerRoles: [],
    utterances: new Map(),
    currentSpeaker: null,
    currentSpeakerResolve: null,
    currentPcmPlayer: null,
    currentPcmResolve: null,
    currentPlaySeq: 0,
    inputRecognizer: null,
    topicRecognizer: null,
    synthJobs: new Map(),
    speechJobVersions: new Map(),
    synthEngines: new Set(),
    skippedSeqs: new Set(),
    nextSeq: 1,
    nextPlaySeq: 1,
    generation: 0,
    pendingInputText: "",
    memberMenuId: "",
    editingAgentId: "",
    editorMode: "",
    editorDraft: null,
    modelOptions: null,
    playPumpActive: false,
    playbackGeneration: 0,
    voiceCfg: null,
    cloudTtsActive: 0,
    cloudTtsQueue: [],
    mode: false,
    capturingInput: false,
    capturingTopic: false,
    topicCaptureArmed: false,
    playbackStopped: false,
    playbackPausedForInput: false,
    prioritySpeechActive: false,
    replayCurrentPlayback: false,
    generationDone: false,
  };

  function $(id) { return root.document && root.document.getElementById(id); }
  function escapeHtml(value) {
    return String(value || "").replace(/[&<>"']/g, function (ch) {
      return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[ch];
    });
  }
  function localGet(key) {
    try { return root.localStorage.getItem(key) || ""; } catch (_) { return ""; }
  }
  function sessionGet(key) {
    try { return root.sessionStorage.getItem(key) || ""; } catch (_) { return ""; }
  }
  function sessionSet(key, value) {
    try { root.sessionStorage.setItem(key, String(value || "")); } catch (_) {}
  }
  function sessionRemove(key) {
    try { root.sessionStorage.removeItem(key); } catch (_) {}
  }
  function sessionGetJson(key, fallback) {
    try {
      var raw = root.sessionStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch (_) {
      return fallback;
    }
  }
  function safeAgentRole(role) {
    return ROLES.indexOf(role) >= 0 ? role : "自动";
  }
  function displayRole(agent) {
    return agent && agent.assignedRole ? agent.assignedRole : safeAgentRole(agent && agent.role);
  }
  function agentEmoji(agent, index) {
    var role = displayRole(agent);
    return ROLE_EMOJIS[role] || agent.emoji || MEMBER_EMOJIS[index % MEMBER_EMOJIS.length];
  }
  function agentName(agent) {
    var role = displayRole(agent);
    if (role === "自动") return "成员";
    return role
      .replace(/^IT/, "")
      .replace(/研发工程师$/, "")
      .replace(/工程师$/, "")
      .replace(/设计师$/, "")
      .replace(/架构师$/, "架构");
  }
  function normalizeAgents(raw) {
    var list = Array.isArray(raw) ? raw : [];
    var maxId = 0;
    var normalized = list.map(function (agent, index) {
      var id = String(agent && agent.id || ("agent-" + (index + 1)));
      var n = Number(id.replace(/^agent-/, ""));
      if (Number.isFinite(n)) maxId = Math.max(maxId, n);
      return {
        id: id,
        role: safeAgentRole(agent && agent.role),
        assignedRole: agent && agent.assignedRole || "",
        voiceId: agent && agent.voiceId || "",
        voiceLabel: agent && agent.voiceLabel || "",
        modelRef: agent && (agent.modelRef || agent.model_ref) || "",
        modelLabel: agent && (agent.modelLabel || agent.model_label) || "",
        emoji: agent && agent.emoji || MEMBER_EMOJIS[index % MEMBER_EMOJIS.length],
        colorIndex: Number.isFinite(Number(agent && agent.colorIndex)) ? Number(agent.colorIndex) : index,
      };
    });
    podcast.nextAgentId = Math.max(podcast.nextAgentId, maxId + 1, normalized.length + 1);
    return normalized;
  }
  function savePodcastState() {
    if (podcast.mode || podcast.runId || podcast.topicCaptureArmed) {
      sessionSet(MODE_KEY, "1");
      if (podcast.runId) sessionSet(RUN_KEY, podcast.runId);
      else sessionRemove(RUN_KEY);
      sessionSet(AGENTS_KEY, JSON.stringify(podcast.agents || []));
      if (podcast.lastTopic) sessionSet(TOPIC_KEY, podcast.lastTopic);
      else sessionRemove(TOPIC_KEY);
      return;
    }
    sessionRemove(MODE_KEY);
    sessionRemove(RUN_KEY);
    sessionRemove(AGENTS_KEY);
    sessionRemove(TOPIC_KEY);
  }
  function restorePodcastSurface() {
    if (!podcast.agents.length) podcast.agents = normalizeAgents(sessionGetJson(AGENTS_KEY, []));
    if (!podcast.lastTopic) podcast.lastTopic = sessionGet(TOPIC_KEY);
    if (!sessionGet(MODE_KEY) && !podcast.mode && !podcast.runId && !podcast.agents.length) {
      renderParticipants();
      return;
    }
    var savedRunId = sessionGet(RUN_KEY);
    if (!podcast.runId && savedRunId) {
      podcast.runId = savedRunId;
      podcast.playbackStopped = false;
      podcast.generationDone = false;
    }
    setPodcastMode(podcast.agents.length > 0 || Boolean(podcast.runId));
    if (podcast.runId) {
      setActive(true);
      if (!podcast.capturingInput && !podcast.capturingTopic) {
        setStatus("群聊进行中，点屏幕空白处可插话。");
        setVoiceStatus("群聊已恢复，等待后续内容...");
      }
    } else if (podcast.agents.length) {
      podcast.topicCaptureArmed = true;
      setActive(false);
      setStatus("点击屏幕空白处，说出群聊话题。");
      setVoiceStatus("群聊待启动，点击屏幕说出讨论话题。");
    }
    renderAgents();
    renderParticipants();
    savePodcastState();
  }
  function optionLabel(select) {
    if (!select || select.selectedIndex < 0) return "";
    var opt = select.options[select.selectedIndex];
    return opt ? String(opt.textContent || "").replace(/^🗣\s*/, "").trim() : "";
  }
  function selectedOut() {
    var outSelect = $("voiceTtsVoice");
    if (outSelect && outSelect.value) return outSelect.value;
    var pref = localGet("nanoTtsMode");
    if (pref) return pref;
    var cfg = podcast.voiceCfg;
    return cfg && cfg.available && cfg.tts && cfg.tts.enabled ? "aliyun-flowing" : "local";
  }
  function voiceLabelFromConfig(voiceId) {
    var voices = podcast.voiceCfg && podcast.voiceCfg.tts && podcast.voiceCfg.tts.voices || [];
    for (var i = 0; i < voices.length; i++) {
      if (voices[i].value === voiceId) return voices[i].label || voiceId;
    }
    return voiceId || "";
  }
  function currentHostVoice() {
    var out = selectedOut();
    var select = $("voiceVoice");
    var value = select ? select.value : "";
    var label = optionLabel(select);
    if (out !== "local") {
      value = value || localGet("nanoAliyunVoice") || (podcast.voiceCfg && podcast.voiceCfg.tts && podcast.voiceCfg.tts.voice) || "xiaoxian";
      label = label || voiceLabelFromConfig(value) || value;
    } else {
      value = value || localGet("nanoVoiceURI") || "";
      label = label || (value ? value : "系统默认");
    }
    return { id: value, label: label };
  }
  function updateHostPreview() {
    var el = $("podcastHostVoice");
    if (!el) return;
    var host = currentHostVoice();
    el.textContent = "跟随当前音色 · " + (host.label || host.id || "系统默认");
  }
  function currentSessionId() {
    return typeof state !== "undefined" && state.currentSession ? state.currentSession.session_id : "";
  }
  function authHeadersSafe() {
    return typeof root.authHeaders === "function" ? root.authHeaders() : {};
  }
  async function apiSafe(path, body) {
    if (typeof root.api === "function") {
      return await root.api(path, { method: "POST", body: JSON.stringify(body || {}) });
    }
    var res = await fetch(path, {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, authHeadersSafe()),
      body: JSON.stringify(body || {}),
    });
    if (!res.ok) throw new Error(String(res.status));
    return await res.json();
  }
  async function apiGetSafe(path) {
    if (typeof root.api === "function") return await root.api(path);
    var res = await fetch(path, { headers: authHeadersSafe() });
    if (!res.ok) throw new Error(String(res.status));
    return await res.json();
  }
  async function loadVoiceConfig() {
    if (podcast.voiceCfg) return podcast.voiceCfg;
    try {
      if (typeof root.api === "function") podcast.voiceCfg = await root.api("/api/voice/config");
      else {
        var res = await fetch("/api/voice/config", { headers: authHeadersSafe() });
        podcast.voiceCfg = res.ok ? await res.json() : {};
      }
    } catch (_) {
      podcast.voiceCfg = {};
    }
    return podcast.voiceCfg;
  }
  async function loadModelOptions() {
    if (podcast.modelOptions) return podcast.modelOptions;
    try {
      var payload = await apiGetSafe("/api/models");
      podcast.modelOptions = Array.isArray(payload && payload.models) ? payload.models : [];
    } catch (_) {
      podcast.modelOptions = [];
    }
    return podcast.modelOptions;
  }

  function isGroupMode() {
    return podcast.agents.length > 0 || podcast.mode || Boolean(podcast.runId);
  }
  function systemParticipantName() {
    return podcast.agents.length > 0 || podcast.mode || podcast.runId ? "主持人" : "Assistant";
  }
  function participantColor(index) {
    return MEMBER_COLORS[index % MEMBER_COLORS.length];
  }
  function systemParticipants() {
    return [
      { id: "me", kind: "user", name: "我", emoji: "🙂", color: "#2d7ff9", removable: false },
      { id: "host", kind: "host", name: systemParticipantName(), emoji: isGroupMode() ? "🎙️" : "🤖", color: "#0f9f8f", removable: false },
    ];
  }
  function agentParticipants() {
    var list = [];
    podcast.agents.slice(0, MAX_GROUP_AGENTS).forEach(function (agent, index) {
      list.push({
        id: agent.id,
        kind: "agent",
        name: agentName(agent),
        role: displayRole(agent),
        emoji: agentEmoji(agent, index),
        color: participantColor((agent.colorIndex || index) + 2),
        removable: true,
      });
    });
    if (!podcast.starting && podcast.agents.length < MAX_GROUP_AGENTS) {
      list.push({ id: "add", kind: "add", name: "添加", emoji: "+", color: "#6b7280", removable: false });
    }
    return list;
  }
  function speakerKeyFromRole(role, phase) {
    if (phase === "opening" || phase === "summary" || phase === "interjection" || role === "主持人") return "host";
    var eventAgentId = arguments.length > 2 ? arguments[2] : "";
    if (eventAgentId) return String(eventAgentId);
    var value = String(role || "");
    for (var i = 0; i < podcast.agents.length; i++) {
      var agent = podcast.agents[i];
      if (displayRole(agent) === value || agent.role === value || agent.id === value) return agent.id;
    }
    return value ? "role:" + value : "";
  }
  function isRemovedSpeakerEvent(event) {
    if (!event || event.phase !== "speaker") return false;
    var agentId = String(event.agent_id || event.agentId || "");
    if (agentId) return podcast.removedAgentIds.indexOf(agentId) >= 0;
    var role = String(event.role || "");
    return Boolean(role && podcast.removedSpeakerRoles.indexOf(role) >= 0);
  }
  function isRemovedSpeakerKey(key) {
    key = String(key || "");
    if (!key) return false;
    if (podcast.removedAgentIds.indexOf(key) >= 0) return true;
    if (key.indexOf("role:") === 0) return podcast.removedSpeakerRoles.indexOf(key.slice(5)) >= 0;
    return false;
  }
  function rememberRemovedAgent(agent) {
    if (!agent) return;
    if (agent.id && podcast.removedAgentIds.indexOf(agent.id) < 0) podcast.removedAgentIds.push(agent.id);
    [displayRole(agent), agent.role, agent.assignedRole].forEach(function (role) {
      role = String(role || "");
      if (role && role !== "自动" && podcast.removedSpeakerRoles.indexOf(role) < 0) podcast.removedSpeakerRoles.push(role);
    });
  }
  function dropRemovedSpeakerWork() {
    podcast.utterances.forEach(function (entry, utteranceId) {
      if (!entry || !isRemovedSpeakerKey(entry.speakerKey)) return;
      if (entry.node && entry.node.remove) entry.node.remove();
      if (entry.seq) skipSpeechSeq(entry.seq);
      podcast.utterances.delete(utteranceId);
    });
  }
  function setActiveSpeaker(key, mode) {
    podcast.activeSpeakerKey = key || "";
    podcast.activeSpeakerMode = mode || "";
    renderParticipants();
  }
  function setPlayingSpeaker(key) {
    podcast.playingSpeakerKey = key || "";
    renderParticipants();
  }
  function renderMemberButton(rootEl, member, options) {
    if (!rootEl || !root.document) return;
    options = options || {};
    var item = root.document.createElement("div");
    item.className = "group-participant" + (member.kind === "add" ? " group-add" : "");
    if (options.stage) item.className += " group-stage-participant";
    var isPlaying = podcast.playingSpeakerKey === member.id;
    var isGenerating = !isPlaying && podcast.activeSpeakerKey === member.id;
    if (isPlaying) item.classList.add("is-speaking");
    else if (isGenerating) item.classList.add("is-generating");
    item.style.setProperty("--member-color", member.color);
    item.dataset.memberId = member.id;
    var btn = root.document.createElement("button");
    btn.type = "button";
    btn.className = "group-member-hit";
    btn.setAttribute("aria-label", member.kind === "add" ? "添加群成员" : member.name);
    btn.innerHTML = '<span class="group-avatar"><span class="group-emoji">' + escapeHtml(member.emoji) + '</span>'
      + (isPlaying ? '<span class="group-speaker">🔊</span>' : "")
      + '</span>';
    btn.onclick = function (event) {
      event.stopPropagation();
      if (btn.blur) btn.blur();
      if (member.kind === "add") {
        openAddAgentEditor();
        return;
      }
      if (member.kind !== "agent") {
        closeMemberMenu();
        return;
      }
      openMemberMenu(member, item);
    };
    item.appendChild(btn);
    if (member.kind === "agent") {
      var remove = root.document.createElement("button");
      remove.type = "button";
      remove.className = "group-member-remove";
      remove.setAttribute("aria-label", "踢出群聊");
      remove.onclick = function (event) {
        event.stopPropagation();
        closeMemberMenu();
        removeGroupAgent(member.id);
      };
      item.appendChild(remove);
    }
    rootEl.appendChild(item);
  }
  function renderParticipants() {
    var rootEl = $("groupParticipants");
    var gridEl = $("groupAgentGrid");
    if (!root.document) return;
    if (rootEl) {
      rootEl.innerHTML = "";
      systemParticipants().forEach(function (member) {
        renderMemberButton(rootEl, member);
      });
    }
    if (!gridEl) return;
    gridEl.innerHTML = "";
    var gridMembers = agentParticipants().slice(0, MAX_GROUP_AGENTS);
    gridMembers.forEach(function (member) {
      renderMemberButton(gridEl, member, { stage: true });
    });
    for (var i = gridMembers.length; i < MAX_GROUP_AGENTS; i++) {
      var empty = root.document.createElement("div");
      empty.className = "group-participant group-stage-participant group-empty";
      empty.setAttribute("aria-hidden", "true");
      empty.innerHTML = '<span class="group-avatar"></span>';
      gridEl.appendChild(empty);
    }
  }
  function closeMemberMenu() {
    var menu = $("groupMemberMenu");
    if (!menu) return;
    podcast.memberMenuId = "";
    menu.hidden = true;
    menu.innerHTML = "";
  }
  function findAgent(agentId) {
    for (var i = 0; i < podcast.agents.length; i++) {
      if (podcast.agents[i].id === agentId) return podcast.agents[i];
    }
    return null;
  }
  function voiceForSpeaker(speakerKey, event) {
    var agent = findAgent(speakerKey);
    if (agent && agent.voiceId) {
      return {
        id: agent.voiceId,
        label: agent.voiceLabel || agent.voiceId,
      };
    }
    return {
      id: event && event.voice_id || "xiaoxian",
      label: event && (event.voice_label || event.voice_id) || "",
    };
  }
  function openMemberMenu(member, anchor) {
    if (!member || member.kind === "add" || !root.document) return;
    var menu = $("groupMemberMenu");
    if (!menu) return;
    if (!menu.hidden && podcast.memberMenuId === member.id) {
      closeMemberMenu();
      var hit = anchor && anchor.querySelector && anchor.querySelector(".group-member-hit");
      if (hit && hit.blur) hit.blur();
      return;
    }
    closeMemberMenu();
    podcast.memberMenuId = member.id;
    var title = root.document.createElement("button");
    title.type = "button";
    title.disabled = true;
    title.textContent = member.name;
    menu.appendChild(title);
    if (member.kind === "agent") {
      var role = root.document.createElement("button");
      role.type = "button";
      role.textContent = "修改身份";
      role.onclick = function () {
        closeMemberMenu();
        openAgentEditor(member.id, "role");
      };
      menu.appendChild(role);
      var voice = root.document.createElement("button");
      voice.type = "button";
      voice.textContent = "更换音色";
      voice.onclick = function () {
        closeMemberMenu();
        openAgentEditor(member.id, "voice");
      };
      menu.appendChild(voice);
      var model = root.document.createElement("button");
      model.type = "button";
      model.textContent = "更换模型";
      model.onclick = function () {
        closeMemberMenu();
        openAgentEditor(member.id, "model");
      };
      menu.appendChild(model);
    }
    var rect = anchor.getBoundingClientRect();
    menu.style.left = Math.min(rect.left, root.innerWidth ? root.innerWidth - 170 : rect.left) + "px";
    menu.style.top = (rect.bottom + 8) + "px";
    menu.hidden = false;
  }
  function setAgentEditor(open) {
    var dlg = $("agentEditorDialog");
    if (dlg) dlg.hidden = !open;
    if (!open) {
      podcast.editingAgentId = "";
      podcast.editorMode = "";
      podcast.editorDraft = null;
    }
  }
  function agentDraft(agent) {
    return {
      role: safeAgentRole(agent && agent.role || "自动"),
      voiceId: agent && agent.voiceId || "",
      voiceLabel: agent && agent.voiceLabel || "",
      modelRef: agent && agent.modelRef || "",
      modelLabel: agent && agent.modelLabel || "",
    };
  }
  function voiceOptions() {
    var voices = podcast.voiceCfg && podcast.voiceCfg.tts && podcast.voiceCfg.tts.voices || [];
    return Array.isArray(voices) ? voices : [];
  }
  function selectAppend(select, value, label) {
    var opt = root.document.createElement("option");
    opt.value = value || "";
    opt.textContent = label || value || "";
    select.appendChild(opt);
  }
  function selectHasValue(select, value) {
    for (var i = 0; i < select.options.length; i++) {
      if (select.options[i].value === value) return true;
    }
    return false;
  }
  function updateAgentEditorHeader() {
    var title = $("agentEditorTitle");
    var avatar = $("agentEditorAvatar");
    var draft = podcast.editorDraft || agentDraft(null);
    var role = safeAgentRole(draft.role);
    if (title) title.textContent = podcast.editorMode === "add" ? "添加角色" : (role === "自动" ? "成员设置" : role);
    if (avatar) avatar.textContent = podcast.editorMode === "add" && role === "自动" ? "+" : (ROLE_EMOJIS[role] || "🧠");
  }
  function fillAgentEditor() {
    var roleSelect = $("agentEditorRole");
    var voiceSelect = $("agentEditorVoice");
    var modelSelect = $("agentEditorModel");
    var draft = podcast.editorDraft || agentDraft(null);
    if (!roleSelect || !voiceSelect || !modelSelect) return;
    updateAgentEditorHeader();
    roleSelect.innerHTML = "";
    ROLES.forEach(function (role) { selectAppend(roleSelect, role, role); });
    roleSelect.value = draft.role || "自动";
    voiceSelect.innerHTML = "";
    selectAppend(voiceSelect, "", "自动分配音色");
    voiceOptions().forEach(function (voice) {
      var value = voice && (voice.value || voice.id) || "";
      if (!value) return;
      selectAppend(voiceSelect, value, voice.label || value);
    });
    if (draft.voiceId && !selectHasValue(voiceSelect, draft.voiceId)) {
      selectAppend(voiceSelect, draft.voiceId, draft.voiceLabel || draft.voiceId);
    }
    voiceSelect.value = draft.voiceId || "";
    modelSelect.innerHTML = "";
    selectAppend(modelSelect, "", "自动分配模型");
    (podcast.modelOptions || []).forEach(function (model) {
      var ref = model && (model.ref || model.value || model.id) || "";
      if (!ref) return;
      selectAppend(modelSelect, ref, model.name || model.label || ref);
    });
    if (draft.modelRef && !selectHasValue(modelSelect, draft.modelRef)) {
      selectAppend(modelSelect, draft.modelRef, draft.modelLabel || draft.modelRef);
    }
    modelSelect.value = draft.modelRef || "";
  }
  async function openAgentEditor(agentId, focusField) {
    var agent = findAgent(agentId);
    if (!agent) return;
    podcast.editingAgentId = agentId;
    podcast.editorMode = "edit";
    podcast.editorDraft = agentDraft(agent);
    setAgentEditor(true);
    await loadVoiceConfig();
    await loadModelOptions();
    fillAgentEditor();
    var target = focusField === "voice" ? $("agentEditorVoice") : focusField === "model" ? $("agentEditorModel") : $("agentEditorRole");
    if (target && target.focus) target.focus();
  }
  async function openAddAgentEditor(focusField) {
    if (podcast.starting) return;
    if (podcast.agents.length >= MAX_GROUP_AGENTS) {
      setStatus("群聊最多支持 9 个角色。");
      setVoiceStatus("群聊最多支持 9 个角色。");
      updatePodcastControl();
      return;
    }
    closeMemberMenu();
    podcast.editingAgentId = "";
    podcast.editorMode = "add";
    podcast.editorDraft = agentDraft(null);
    setAgentEditor(true);
    await loadVoiceConfig();
    await loadModelOptions();
    fillAgentEditor();
    var target = focusField === "voice" ? $("agentEditorVoice") : focusField === "model" ? $("agentEditorModel") : $("agentEditorRole");
    if (target && target.focus) target.focus();
  }
  function updateEditorDraft(field, value, label) {
    var draft = podcast.editorDraft;
    if (!draft) return;
    if (field === "role") {
      draft.role = safeAgentRole(value || "自动");
    } else if (field === "voice") {
      draft.voiceId = value || "";
      draft.voiceLabel = value ? (label || value) : "";
    } else if (field === "model") {
      draft.modelRef = value || "";
      draft.modelLabel = value ? (label || value) : "";
    }
    updateAgentEditorHeader();
  }
  function agentUpdatePayload(agent) {
    return {
      id: agent.id,
      role: agent.role || "自动",
      voice_id: agent.voiceId || "",
      voice_label: agent.voiceLabel || "",
      model_ref: agent.modelRef || "",
      model_label: agent.modelLabel || "",
    };
  }
  function applyAssignedAgent(agentId, assigned) {
    var agent = findAgent(agentId);
    if (!agent || !assigned) return;
    agent.assignedRole = assigned.role || "";
    agent.voiceId = assigned.voice_id || "";
    agent.voiceLabel = assigned.voice_label || "";
    agent.modelRef = assigned.model_ref || "";
    agent.modelLabel = assigned.model_label || "";
  }
  function agentFromDraft(draft) {
    draft = typeof draft === "string" ? { role: draft } : (draft || {});
    var id = draft.id || ("agent-" + podcast.nextAgentId++);
    var index = podcast.agents.length;
    return {
      id: id,
      role: safeAgentRole(draft.role || "自动"),
      assignedRole: draft.assignedRole || "",
      voiceId: draft.voiceId || draft.voice_id || "",
      voiceLabel: draft.voiceLabel || draft.voice_label || "",
      modelRef: draft.modelRef || draft.model_ref || "",
      modelLabel: draft.modelLabel || draft.model_label || "",
      emoji: draft.emoji || MEMBER_EMOJIS[index % MEMBER_EMOJIS.length],
      colorIndex: Number.isFinite(Number(draft.colorIndex)) ? Number(draft.colorIndex) : index,
    };
  }
  function agentFromAssigned(assigned) {
    return agentFromDraft({
      id: assigned && assigned.id,
      role: assigned && (assigned.requested_role || assigned.role),
      assignedRole: assigned && assigned.role || "",
      voice_id: assigned && assigned.voice_id,
      voice_label: assigned && assigned.voice_label,
      model_ref: assigned && assigned.model_ref,
      model_label: assigned && assigned.model_label,
    });
  }
  function appendAssignedAgent(assigned) {
    if (!assigned || !assigned.id) return null;
    var existing = findAgent(assigned.id);
    if (existing) {
      applyAssignedAgent(assigned.id, assigned);
      return existing;
    }
    var agent = agentFromAssigned(assigned);
    podcast.agents.push(agent);
    normalizeAgents(podcast.agents);
    return agent;
  }
  function agentEditChange(agent, draft) {
    return {
      role: agent.role !== safeAgentRole(draft.role),
      voice: (agent.voiceId || "") !== (draft.voiceId || ""),
      model: (agent.modelRef || "") !== (draft.modelRef || ""),
    };
  }
  async function confirmAgentEditor() {
    var draft = podcast.editorDraft;
    if (!draft) return;
    if (podcast.editorMode === "add") {
      if (podcast.runId) {
        await addGroupAgentDuringRun(draft);
      } else {
        addGroupAgent(draft);
      }
      setAgentEditor(false);
      return;
    }
    var agent = findAgent(podcast.editingAgentId);
    if (!agent) return;
    var changes = agentEditChange(agent, draft);
    var changed = changes.role || changes.voice || changes.model;
    var voiceOnly = changes.voice && !changes.role && !changes.model;
    if (agent.role !== draft.role) agent.assignedRole = "";
    agent.role = safeAgentRole(draft.role);
    agent.voiceId = draft.voiceId || "";
    agent.voiceLabel = draft.voiceLabel || "";
    agent.modelRef = draft.modelRef || "";
    agent.modelLabel = draft.modelLabel || "";
    renderAgents();
    renderParticipants();
    savePodcastState();
    setAgentEditor(false);
    if (!changed || !podcast.runId) return;
    if (voiceOnly) {
      setStatus("角色音色已更新，正在替换该角色语音...");
      setVoiceStatus("正在重新生成该角色语音，不影响其他成员。");
    } else {
      setStatus("角色配置已更新，正在重新生成相关内容...");
      setVoiceStatus("角色配置已更新，正在重新生成内容和语音...");
    }
    try {
      var payload = await apiSafe("/api/voice/podcast/update_agent", {
        run_id: podcast.runId,
        agent: agentUpdatePayload(agent),
      });
      if (payload && payload.ok === false) throw new Error(payload.reason || "update failed");
      if (payload && payload.agent) applyAssignedAgent(agent.id, payload.agent);
      if (payload && payload.content_changed === false) {
        refreshAgentSpeechVoice(agent.id, agent.voiceId || "", agent.voiceLabel || "");
        setVoiceStatus("该角色语音已切换到新音色。");
      } else {
        resetForAgentConfigChange(payload && payload.generation);
      }
      renderAgents();
      renderParticipants();
      savePodcastState();
    } catch (err) {
      setStatus("角色已在本机更新，后端重新生成失败：" + (err && err.message || err));
    }
  }
  function addGroupAgent(config) {
    if (podcast.agents.length >= MAX_GROUP_AGENTS) {
      setStatus("群聊最多支持 9 个角色。");
      setVoiceStatus("群聊最多支持 9 个角色。");
      updatePodcastControl();
      return;
    }
    var agent = agentFromDraft(config);
    podcast.agents.push(agent);
    setPodcastMode(true);
    podcast.topicCaptureArmed = true;
    setStatus("已添加群成员，可开始群聊。");
    setVoiceStatus("群聊待启动，点击屏幕说出讨论话题。");
    renderAgents();
    renderParticipants();
    savePodcastState();
    return agent;
  }
  async function addGroupAgentDuringRun(config) {
    if (podcast.agents.length >= MAX_GROUP_AGENTS) {
      setStatus("群聊最多支持 9 个角色。");
      setVoiceStatus("群聊最多支持 9 个角色。");
      updatePodcastControl();
      return;
    }
    var agent = agentFromDraft(config);
    setStatus("正在添加群成员...");
    setVoiceStatus("新成员会从下一轮开始参与。");
    try {
      var payload = await apiSafe("/api/voice/podcast/add_agent", {
        run_id: podcast.runId,
        agent: agentUpdatePayload(agent),
      });
      if (payload && payload.ok === false) throw new Error(payload.reason || "add failed");
      appendAssignedAgent(payload && payload.agent || agentUpdatePayload(agent));
      renderAgents();
      renderParticipants();
      savePodcastState();
      setStatus("已添加群成员，下一轮开始参与。");
      setVoiceStatus("已添加群成员，下一轮开始参与。");
    } catch (err) {
      setStatus("添加群成员失败：" + (err && err.message || err));
      setVoiceStatus("添加群成员失败。");
    }
  }
  async function removeGroupAgent(agentId) {
    var removedAgent = null;
    podcast.agents.forEach(function (agent) {
      if (agent.id === agentId) removedAgent = agent;
    });
    var before = podcast.agents.length;
    podcast.agents = podcast.agents.filter(function (agent) { return agent.id !== agentId; });
    if (podcast.agents.length === before) return;
    if (podcast.editingAgentId === agentId) setAgentEditor(false);
    var wasRunning = Boolean(podcast.runId);
    rememberRemovedAgent(removedAgent);
    dropRemovedSpeakerWork();
    if (isRemovedSpeakerKey(podcast.activeSpeakerKey) || isRemovedSpeakerKey(podcast.playingSpeakerKey)) {
      stopCurrentPlayback(false);
      setActiveSpeaker("", "");
      setPlayingSpeaker("");
    }
    if (!podcast.agents.length) {
      if (wasRunning) await stopPodcast({ keepGroup: false });
      exitGroupChat();
      return;
    }
    setPodcastMode(true);
    podcast.topicCaptureArmed = true;
    renderAgents();
    renderParticipants();
    savePodcastState();
    if (wasRunning) {
      var runId = podcast.runId;
      try {
        await apiSafe("/api/voice/podcast/remove_agent", { run_id: runId, agent_id: agentId });
        setStatus("已踢出群成员，当前话题继续。");
        setVoiceStatus("已踢出群成员，其他成员继续讨论。");
      } catch (err) {
        setStatus("踢出已在本机生效，通知后端失败：" + (err && err.message || err));
      }
    }
  }
  function exitGroupChat() {
    podcast.agents = [];
    podcast.topicCaptureArmed = false;
    podcast.activeSpeakerKey = "";
    podcast.activeSpeakerMode = "";
    podcast.playingSpeakerKey = "";
    setAgentEditor(false);
    setDialog(false);
    setPodcastMode(false);
    setStatus("");
    setVoiceStatus("点击麦克风，开始连续语音对话");
    renderAgents();
    renderParticipants();
    savePodcastState();
  }

  function renderAgents() {
    var rootEl = $("podcastAgents");
    if (!rootEl) return;
    rootEl.innerHTML = "";
    if (!podcast.agents.length) {
      var empty = root.document.createElement("div");
      empty.className = "podcast-status";
      empty.textContent = "还没有普通群成员。";
      rootEl.appendChild(empty);
      return;
    }
    podcast.agents.forEach(function (agent, index) {
      var row = root.document.createElement("div");
      row.className = "podcast-agent-row";
      var select = root.document.createElement("select");
      select.setAttribute("aria-label", "群成员身份");
      ROLES.forEach(function (role) {
        var opt = root.document.createElement("option");
        opt.value = role;
        opt.textContent = role;
        select.appendChild(opt);
      });
      select.value = agent.role || "自动";
      select.disabled = Boolean(podcast.runId || podcast.starting);
      select.onchange = function () {
        if (podcast.runId || podcast.starting) {
          select.value = agent.role || "自动";
          setStatus("群聊进行中请点击角色头像修改身份。");
          return;
        }
        podcast.agents[index].role = select.value;
        podcast.agents[index].assignedRole = "";
        renderParticipants();
        savePodcastState();
      };
      var remove = root.document.createElement("button");
      remove.type = "button";
      remove.className = "podcast-agent-remove";
      remove.setAttribute("aria-label", "踢出群聊");
      remove.textContent = "×";
      remove.onclick = function () {
        removeGroupAgent(agent.id);
      };
      row.appendChild(select);
      row.appendChild(remove);
      rootEl.appendChild(row);
    });
  }

  function setDialog(open) {
    var dlg = $("podcastDialog");
    if (dlg) dlg.hidden = !open;
    if (open) {
      if (!podcast.agents.length) addGroupAgent();
      setPodcastMode(true);
      podcast.topicCaptureArmed = true;
      renderAgents();
      renderParticipants();
      updateHostPreview();
      loadVoiceConfig().then(updateHostPreview).catch(function () {});
      setStatus("可输入话题，或直接点击开始群聊。");
      setVoiceStatus("群聊待启动，配置后可点击屏幕说出讨论话题。");
    }
  }
  function setStatus(text) {
    var el = $("podcastStatus");
    if (el) el.textContent = text || "";
  }
  function setVoiceStatus(text) {
    var el = $("voiceStatus");
    if (el) el.textContent = text || "";
    var podcastStatus = $("podcastModeStatus");
    if (podcastStatus) podcastStatus.textContent = text || "";
  }
  function setPodcastMode(on) {
    if (on) suspendNormalVoiceMode();
    podcast.mode = Boolean(on);
    var overlay = $("voiceOverlay");
    if (overlay) overlay.classList.toggle("podcast-mode", podcast.mode);
    var stage = $("podcastStage");
    if (stage) stage.hidden = !podcast.mode;
    var btn = $("voicePodcastBtn");
    if (btn) {
      btn.classList.toggle("is-active", podcast.mode);
      btn.setAttribute("aria-pressed", podcast.mode ? "true" : "false");
      btn.setAttribute("aria-label", podcast.mode ? "切换到语音" : "切换到群聊");
      btn.title = podcast.mode ? "切换到语音" : "切换到群聊";
    }
    updatePodcastControl();
    renderParticipants();
    savePodcastState();
  }
  function suspendNormalVoiceMode() {
    if (root.VoiceMode && typeof root.VoiceMode.suspendForPodcast === "function") {
      try { root.VoiceMode.suspendForPodcast(); } catch (_) {}
    } else if (root.VoiceMode && typeof root.VoiceMode.close === "function") {
      try { root.VoiceMode.close(); } catch (_) {}
    }
    var overlay = $("voiceOverlay");
    if (overlay) overlay.hidden = false;
    if (root.document && root.document.body) root.document.body.classList.add("voice-open");
  }
  function setPodcastControl(cls, icon, label) {
    var circle = $("podcastCircle");
    var iconEl = $("podcastCircleIcon");
    var labelEl = $("podcastCircleLabel");
    if (circle) circle.className = "podcast-action-chip " + (cls || "idle");
    if (iconEl) iconEl.textContent = icon || "◎";
    if (labelEl) labelEl.textContent = label || "点击说话题";
  }
  function updatePodcastControl() {
    var circle = $("podcastCircle");
    if (circle) circle.disabled = Boolean(podcast.starting);
    if (podcast.capturingTopic) {
      setPodcastControl("listening", "◉", "正在听话题");
      return;
    }
    if (podcast.capturingInput) {
      setPodcastControl("listening", "◉", "正在听插话");
      return;
    }
    if (podcast.starting) {
      setPodcastControl("generating", "◌", "启动中");
      return;
    }
    if (podcast.runId) {
      if (podcast.playPumpActive || podcast.currentSpeaker || podcast.currentPcmResolve) {
        setPodcastControl("speaking", "✦", "点击插话");
      } else {
        setPodcastControl("generating", "◆", "点击插话");
      }
      return;
    }
    setPodcastControl("idle", "◎", "点击说话题");
  }
  function phaseLabel(event) {
    if (event.phase === "opening") return "主持人开场";
    if (event.phase === "interjection") return "主持人回应插话";
    if (event.phase === "summary") return "第 " + event.round + " 轮主持人总结";
    if (event.phase === "speaker") return "第 " + event.round + " 轮 " + (event.role || "成员") + " 发言";
    return event.role || "群聊";
  }
  function finalUtteranceText(eventText, entry) {
    var value = String(eventText || "").trim();
    if (value) return value;
    return String(entry && entry.text || "").trim();
  }
  function setActive(active) {
    podcast.active = Boolean(active);
    var btn = $("voicePodcastBtn");
    var start = $("podcastStartBtn");
    var stop = $("podcastStopBtn");
    var stageStop = $("podcastStageStopBtn");
    var add = $("podcastAddAgentBtn");
    if (start) start.disabled = podcast.active || podcast.starting || podcast.capturingTopic;
    if (stop) stop.disabled = !podcast.active;
    if (stageStop) stageStop.disabled = !podcast.runId;
    if (add) add.disabled = podcast.agents.length >= MAX_GROUP_AGENTS || Boolean(podcast.starting);
    updatePodcastControl();
  }

  function explicitTopicValue() {
    var el = $("podcastTopic");
    return el ? el.value.trim() : "";
  }

  function topicValue(topicOverride) {
    var value = String(topicOverride || "").trim();
    if (value) return value;
    value = explicitTopicValue();
    if (value) return value;
    var prompt = $("prompt");
    if (prompt && prompt.value.trim()) return prompt.value.trim();
    var hist = typeof state !== "undefined" && state.currentSession && state.currentSession.history || [];
    for (var i = hist.length - 1; i >= 0; i--) {
      var msg = hist[i];
      if (msg.role === "user" && typeof root.extractText === "function") {
        value = root.extractText(msg).trim();
        if (value) return value;
      }
    }
    return "自由讨论";
  }

  async function startPodcast(topicOverride) {
    if (podcast.active || podcast.starting) return;
    if (!podcast.agents.length) addGroupAgent();
    podcast.starting = true;
    podcast.topicCaptureArmed = false;
    setPodcastMode(true);
    setActive(false);
    setStatus("正在启动...");
    setVoiceStatus("群聊正在启动...");
    try {
      var roundsEl = $("podcastRounds");
      await loadVoiceConfig();
      var hostVoice = currentHostVoice();
      podcast.lastTopic = topicValue(topicOverride);
      podcast.removedAgentIds = [];
      podcast.removedSpeakerRoles = [];
      var payload = await apiSafe("/api/voice/podcast/start", {
        session_id: currentSessionId(),
        topic: podcast.lastTopic,
        rounds: Number(roundsEl && roundsEl.value || 20),
        host_voice_id: hostVoice.id,
        host_voice_label: hostVoice.label,
        agents: podcast.agents.map(function (agent) {
          return {
            id: agent.id,
            role: agent.role || "自动",
            voice_id: agent.voiceId || "",
            voice_label: agent.voiceLabel || "",
            model_ref: agent.modelRef || "",
            model_label: agent.modelLabel || "",
          };
        }),
      });
      podcast.runId = payload.run_id || "";
      podcast.playbackStopped = false;
      podcast.generationDone = false;
      savePodcastState();
      resetPlaybackState();
      primePlayback();
      renderAssignments(payload);
      setStatus("群聊进行中，点屏幕空白处可插话。");
      setVoiceStatus("群聊已启动，正在生成主持人开场...");
      setDialog(false);
      setPodcastMode(true);
      setActive(true);
    } catch (err) {
      setStatus("启动失败：" + (err && err.message || err));
      podcast.topicCaptureArmed = true;
    } finally {
      podcast.starting = false;
      setActive(Boolean(podcast.runId));
    }
  }

  async function stopPodcast(options) {
    if (!podcast.runId) return;
    options = options || {};
    var keepGroup = options.keepGroup !== false && podcast.agents.length > 0;
    var runId = podcast.runId;
    podcast.runId = "";
    podcast.playbackStopped = true;
    podcast.playbackPausedForInput = false;
    podcast.prioritySpeechActive = false;
    podcast.generationDone = true;
    podcast.topicCaptureArmed = keepGroup;
    setActiveSpeaker("", "");
    savePodcastState();
    setPodcastMode(true);
    setActive(false);
    stopInterjectionCapture();
    stopTopicCapture();
    invalidatePlaybackWork();
    stopSpeech();
    setStatus("正在停止...");
    setVoiceStatus("群聊正在停止...");
    try { await apiSafe("/api/voice/podcast/stop", { run_id: runId }); } catch (_) {}
    if (keepGroup) {
      setStatus("群聊已停止，可换话题后重新开始。");
      setVoiceStatus("群聊已停止。");
    }
  }

  function renderAssignments(payload) {
    var el = $("podcastAssignedVoices");
    if (!payload) return;
    var agents = payload.agents || [];
    var host = payload.host || {};
    var byId = {};
    agents.forEach(function (a) { if (a && a.id) byId[a.id] = a; });
    podcast.agents.forEach(function (agent) {
      var assigned = byId[agent.id];
      if (!assigned) return;
      agent.assignedRole = assigned.role || "";
      agent.voiceId = assigned.voice_id || "";
      agent.voiceLabel = assigned.voice_label || "";
      agent.modelRef = assigned.model_ref || "";
      agent.modelLabel = assigned.model_label || "";
    });
    renderAgents();
    renderParticipants();
    savePodcastState();
    if (!el) return;
    el.hidden = false;
    el.innerHTML = "主持人：" + escapeHtml(host.voice_label || host.voice_id || "当前音色") + "<br>" + agents.map(function (a) {
      var model = a.model_label || a.model_ref || "";
      return escapeHtml(a.role + "：" + (a.voice_label || a.voice_id || "") + (model ? " · " + model : ""));
    }).join("<br>");
  }

  function addBubble(role, text, speaker) {
    var captions = $("voiceCaptions");
    if (!captions) return null;
    var div = document.createElement("div");
    div.className = "vbubble " + (role === "you" ? "you" : "ai");
    if (speaker) {
      div.innerHTML = "<strong>" + escapeHtml(speaker) + "</strong><br><span></span>";
      div.querySelector("span").textContent = text || "";
    } else {
      div.textContent = text || "";
    }
    captions.appendChild(div);
    captions.scrollTop = captions.scrollHeight;
    return div;
  }

  function updateBubble(entry, text) {
    if (!entry || !entry.node) return;
    var span = entry.node.querySelector("span");
    if (span) span.textContent = text || "";
    else entry.node.textContent = text || "";
    var captions = $("voiceCaptions");
    if (captions) captions.scrollTop = captions.scrollHeight;
  }
  function updateBubbleSpeaker(entry, speaker) {
    if (!entry || !entry.node) return;
    var strong = entry.node.querySelector("strong");
    if (strong) strong.textContent = speaker || "";
  }
  function removeAgentBubbles() {
    var captions = $("voiceCaptions");
    if (!captions) return;
    var nodes = captions.querySelectorAll(".vbubble.ai");
    nodes.forEach(function (node) { node.remove(); });
  }

  function resetForUserInput(generation) {
    if (Number.isFinite(Number(generation))) podcast.generation = Number(generation);
    invalidatePlaybackWork();
    stopSpeech();
    setActiveSpeaker("me", "speaking");
    podcast.utterances.clear();
    podcast.synthJobs.clear();
    podcast.speechJobVersions.clear();
    podcast.skippedSeqs.clear();
    podcast.nextPlaySeq = 0;
    podcast.playPumpActive = false;
    podcast.playbackPausedForInput = false;
    podcast.prioritySpeechActive = false;
    podcast.replayCurrentPlayback = false;
    podcast.generationDone = false;
    removeAgentBubbles();
    setVoiceStatus("已收到你的插话，正在重新生成群聊内容...");
    updatePodcastControl();
  }

  function resetForAgentConfigChange(generation) {
    if (Number.isFinite(Number(generation))) podcast.generation = Number(generation);
    invalidatePlaybackWork();
    stopSpeech();
    podcast.utterances.clear();
    podcast.synthJobs.clear();
    podcast.speechJobVersions.clear();
    podcast.skippedSeqs.clear();
    podcast.nextPlaySeq = 0;
    podcast.playPumpActive = false;
    podcast.playbackPausedForInput = false;
    podcast.prioritySpeechActive = false;
    podcast.replayCurrentPlayback = false;
    podcast.generationDone = false;
    removeAgentBubbles();
    setVoiceStatus("角色配置已更新，正在重新生成内容和语音...");
    updatePodcastControl();
  }

  function eventGeneration(event) {
    return event && event.generation != null ? Number(event.generation) || 0 : podcast.generation;
  }

  function isCurrentGenerationEvent(event) {
    return event && event.generation != null ? eventGeneration(event) === podcast.generation : true;
  }

  function eventSequence(event) {
    return Number(event && event.sequence) || 0;
  }

  function skipEventSpeechSeq(event) {
    var seq = eventSequence(event);
    if (seq) skipSpeechSeq(seq);
  }

  function onEvent(event) {
    if (!event || typeof event.type !== "string" || !event.type.startsWith("podcast.")) return;
    if (event.run_id && podcast.runId && event.run_id !== podcast.runId) return;
    if (event.type === "podcast.started") {
      setPodcastMode(true);
      podcast.generation = Number(event.generation) || 0;
      podcast.runId = event.run_id || podcast.runId;
      podcast.playbackStopped = false;
      podcast.generationDone = false;
      savePodcastState();
      resetPlaybackState();
      primePlayback();
      renderAssignments(event);
      setActive(true);
      setVoiceStatus("群聊已启动，正在生成主持人开场...");
      updatePodcastControl();
      return;
    }
    if (event.type === "podcast.input.accepted") {
      resetForUserInput(eventGeneration(event));
      if (event.text && event.text !== podcast.pendingInputText) addBubble("you", event.text || "");
      podcast.pendingInputText = "";
      setVoiceStatus("已收到你的插话，主持人准备回应...");
      return;
    }
    if (event.type === "podcast.agent.updated") {
      if (event.agent_id && event.agent) applyAssignedAgent(event.agent_id, event.agent);
      if (event.content_changed === false) {
        refreshAgentSpeechVoice(event.agent_id || "", event.agent && event.agent.voice_id || "", event.agent && event.agent.voice_label || "");
      } else {
        resetForAgentConfigChange(eventGeneration(event));
      }
      renderAgents();
      renderParticipants();
      savePodcastState();
      return;
    }
    if (event.type === "podcast.agent.added") {
      appendAssignedAgent(event.agent);
      renderAgents();
      renderParticipants();
      savePodcastState();
      setVoiceStatus("新群成员已加入，下一轮开始参与。");
      return;
    }
    if (!isCurrentGenerationEvent(event)) return;
    if (event.type === "podcast.round.started") {
      setVoiceStatus("第 " + event.round + " 轮开始，" + (event.speaker_count || 1) + " 位成员并行生成中...");
      return;
    }
    if (event.type === "podcast.research.started") {
      if (isRemovedSpeakerEvent(event)) return;
      setVoiceStatus("第 " + event.round + " 轮 " + (event.role || "成员") + " 首次深度 research 中...");
      return;
    }
    if (event.type === "podcast.research.done") {
      if (isRemovedSpeakerEvent(event)) return;
      setVoiceStatus("第 " + event.round + " 轮 " + (event.role || "成员") + " research 完成，正在生成观点...");
      return;
    }
    if (event.type === "podcast.agent.removed") {
      if (event.agent_id && podcast.removedAgentIds.indexOf(event.agent_id) < 0) podcast.removedAgentIds.push(event.agent_id);
      setVoiceStatus("群成员已踢出，其他成员继续讨论。");
      return;
    }
    if (event.type === "podcast.utterance.skipped") {
      skipEventSpeechSeq(event);
      return;
    }
    if (event.type === "podcast.utterance.started") {
      if (isRemovedSpeakerEvent(event)) {
        skipEventSpeechSeq(event);
        return;
      }
      var speaker = event.role + " · " + (event.voice_label || event.voice_id || "");
      var seq = Number(event.sequence) || podcast.nextSeq++;
      if (podcast.nextPlaySeq <= 0) podcast.nextPlaySeq = seq;
      if (seq >= podcast.nextSeq) podcast.nextSeq = seq + 1;
      var speakerKey = speakerKeyFromRole(event.role, event.phase, event.agent_id || event.agentId || "");
      var startVoice = voiceForSpeaker(speakerKey, event);
      speaker = event.role + " · " + (startVoice.label || startVoice.id || "");
      setActiveSpeaker(speakerKey, "generating");
      podcast.utterances.set(event.utterance_id, {
        seq: seq,
        text: "",
        node: addBubble("ai", "", speaker),
        speakerKey: speakerKey,
        label: phaseLabel(event),
        speaker: speaker,
        voiceId: startVoice.id || "",
        voiceLabel: startVoice.label || "",
        finalText: "",
      });
      setVoiceStatus(phaseLabel(event) + "生成中...");
      updatePodcastControl();
      return;
    }
    if (event.type === "podcast.text.delta") {
      if (isRemovedSpeakerEvent(event)) {
        skipEventSpeechSeq(event);
        return;
      }
      var entry = podcast.utterances.get(event.utterance_id);
      if (!entry) return;
      entry.text += event.text || "";
      updateBubble(entry, entry.text);
      return;
    }
    if (event.type === "podcast.utterance.done") {
      if (isRemovedSpeakerEvent(event)) {
        skipEventSpeechSeq(event);
        return;
      }
      var done = podcast.utterances.get(event.utterance_id);
      var finalText = finalUtteranceText(event.text, done);
      if (done) {
        done.finalText = finalText;
        done.voiceId = event.voice_id || done.voiceId || "";
        done.voiceLabel = event.voice_label || done.voiceLabel || event.voice_id || "";
        done.label = phaseLabel(event);
        updateBubble(done, finalText);
      }
      var doneSeq = done ? done.seq : (Number(event.sequence) || podcast.nextSeq++);
      if (doneSeq >= podcast.nextSeq) podcast.nextSeq = doneSeq + 1;
      var doneSpeakerKey = done && done.speakerKey || speakerKeyFromRole(event.role, event.phase, event.agent_id || event.agentId || "");
      var doneVoice = voiceForSpeaker(doneSpeakerKey, event);
      if (event.phase === "interjection") {
        skipSpeechSeq(doneSeq);
        enqueuePrioritySpeech(finalText, doneVoice.id || "xiaoxian", phaseLabel(event), doneSpeakerKey || "host");
        setVoiceStatus(phaseLabel(event) + "已生成，正在优先准备语音...");
        return;
      }
      if (done) {
        done.voiceId = doneVoice.id || done.voiceId || "";
        done.voiceLabel = doneVoice.label || done.voiceLabel || done.voiceId || "";
      }
      enqueueSpeech(doneSeq, finalText, doneVoice.id || "xiaoxian", phaseLabel(event), doneSpeakerKey);
      setVoiceStatus(phaseLabel(event) + "已生成，正在准备语音...");
      updatePodcastControl();
      return;
    }
    if (event.type === "podcast.round.done") {
      setVoiceStatus("第 " + event.round + " 轮内容已生成，继续生成后续轮次...");
      return;
    }
    if (event.type === "podcast.done") {
      podcast.runId = "";
      podcast.generationDone = true;
      savePodcastState();
      setActive(false);
      setStatus("群聊内容已生成完成。");
      setVoiceStatus("群聊内容已生成完成，继续播放剩余语音...");
      pumpPlayback();
      updatePodcastControl();
      return;
    }
    if (event.type === "podcast.stopped") {
      podcast.runId = "";
      podcast.generationDone = true;
      podcast.playbackStopped = true;
      podcast.playbackPausedForInput = false;
      podcast.prioritySpeechActive = false;
      podcast.topicCaptureArmed = podcast.agents.length > 0;
      setActiveSpeaker("", "");
      setPlayingSpeaker("");
      invalidatePlaybackWork();
      stopSpeech();
      savePodcastState();
      setActive(false);
      stopInterjectionCapture();
      setStatus("群聊已停止，可换话题后重新开始。");
      setVoiceStatus("群聊已停止。");
      updatePodcastControl();
      return;
    }
    if (event.type === "podcast.error") {
      podcast.runId = "";
      podcast.generationDone = true;
      podcast.playbackStopped = true;
      setActiveSpeaker("", "");
      setPlayingSpeaker("");
      invalidatePlaybackWork();
      stopSpeech();
      savePodcastState();
      setActive(false);
      setStatus("群聊出错：" + (event.message || "未知错误"));
      setVoiceStatus("群聊出错：" + (event.message || "未知错误"));
      setPodcastControl("error", "!", "出错");
    }
  }

  function stopSpeech() {
    stopCurrentPlayback(false);
    podcast.synthEngines.forEach(function (engine) {
      try { engine.abort && engine.abort(); } catch (_) {}
    });
    podcast.synthEngines.clear();
    setActiveSpeaker("", "");
    setPlayingSpeaker("");
    updatePodcastControl();
  }

  function stalePlaybackError() {
    return new Error("stale podcast playback generation");
  }

  function isCurrentPlaybackGeneration(generation) {
    return generation === podcast.playbackGeneration;
  }

  function isStalePlaybackError(err) {
    return String(err && err.message || err || "").indexOf("stale podcast playback generation") >= 0;
  }

  function invalidatePlaybackWork() {
    podcast.playbackGeneration++;
    var queued = podcast.cloudTtsQueue.splice(0);
    queued.forEach(function (entry) {
      try { entry.reject(stalePlaybackError()); } catch (_) {}
    });
  }

  function stopCurrentPlayback(replayCurrent) {
    var hadCurrent = Boolean(podcast.currentSpeaker || podcast.currentSpeakerResolve || podcast.currentPcmResolve);
    if (podcast.currentSpeaker) {
      try { podcast.currentSpeaker.abort(); } catch (_) {}
      podcast.currentSpeaker = null;
    }
    if (podcast.currentSpeakerResolve) {
      try { podcast.currentSpeakerResolve(); } catch (_) {}
      podcast.currentSpeakerResolve = null;
    }
    if (podcast.currentPcmPlayer) {
      try { podcast.currentPcmPlayer.stop(); } catch (_) {}
    }
    if (podcast.currentPcmResolve) {
      try { podcast.currentPcmResolve(); } catch (_) {}
      podcast.currentPcmResolve = null;
    }
    if (hadCurrent) setPlayingSpeaker("");
    if (hadCurrent && replayCurrent) podcast.replayCurrentPlayback = true;
    updatePodcastControl();
  }

  function resetPlaybackState() {
    invalidatePlaybackWork();
    stopSpeech();
    podcast.synthJobs.clear();
    podcast.speechJobVersions.clear();
    podcast.skippedSeqs.clear();
    podcast.nextSeq = 1;
    podcast.nextPlaySeq = 1;
    podcast.currentPlaySeq = 0;
    podcast.playPumpActive = false;
    podcast.playbackPausedForInput = false;
    podcast.prioritySpeechActive = false;
    podcast.replayCurrentPlayback = false;
    setActiveSpeaker("", "");
    setPlayingSpeaker("");
  }

  function primePlayback() {
    try {
      ensurePcmPlayer().unlock();
    } catch (_) {}
  }

  function makeSpeechJob(seq, text, voiceId, label, speakerKey) {
    var version = Number(podcast.speechJobVersions.get(seq) || 0) + 1;
    podcast.speechJobVersions.set(seq, version);
    var generation = podcast.playbackGeneration;
    return synthSpeech(text, voiceId, generation).then(function (prepared) {
      if (!isCurrentPlaybackGeneration(generation) || podcast.speechJobVersions.get(seq) !== version) throw stalePlaybackError();
      prepared.label = label || ("第 " + seq + " 段");
      prepared.speakerKey = speakerKey || "";
      prepared.playbackGeneration = generation;
      return prepared;
    }).catch(function (err) {
      if (isStalePlaybackError(err) || !isCurrentPlaybackGeneration(generation) || podcast.speechJobVersions.get(seq) !== version) {
        return { kind: "stale", playbackGeneration: generation };
      }
      console.warn("[podcast] synth failed; fallback to local", err);
      return { kind: "local", text: text, voiceId: voiceId, label: label || ("第 " + seq + " 段"), speakerKey: speakerKey || "", playbackGeneration: generation };
    });
  }

  function enqueueSpeech(seq, text, voiceId, label, speakerKey) {
    text = String(text || "").trim();
    if (!text) {
      skipSpeechSeq(seq);
      return;
    }
    setVoiceStatus((label || ("第 " + seq + " 段")) + "语音合成中...");
    if (podcast.nextPlaySeq <= 0) podcast.nextPlaySeq = seq;
    podcast.synthJobs.set(seq, makeSpeechJob(seq, text, voiceId, label, speakerKey));
    pumpPlayback();
  }

  function refreshAgentSpeechVoice(agentId, voiceId, voiceLabel) {
    agentId = String(agentId || "");
    if (!agentId) return;
    var refreshed = 0;
    podcast.utterances.forEach(function (entry) {
      if (!entry || entry.speakerKey !== agentId || !entry.seq || !entry.finalText) return;
      entry.voiceId = voiceId || entry.voiceId || "xiaoxian";
      entry.voiceLabel = voiceLabel || entry.voiceLabel || entry.voiceId;
      entry.speaker = entry.speaker ? entry.speaker.replace(/ · .*/, " · " + (entry.voiceLabel || entry.voiceId)) : "";
      updateBubbleSpeaker(entry, entry.speaker);
      if (entry.seq < podcast.nextPlaySeq && entry.seq !== podcast.currentPlaySeq) return;
      podcast.synthJobs.set(entry.seq, makeSpeechJob(entry.seq, entry.finalText, entry.voiceId, entry.label, entry.speakerKey));
      refreshed++;
    });
    if (podcast.playingSpeakerKey === agentId) {
      stopCurrentPlayback(true);
    }
    if (refreshed) {
      setVoiceStatus("已重新生成该角色的待播放语音。");
      pumpPlayback();
    }
  }

  async function enqueuePrioritySpeech(text, voiceId, label, speakerKey) {
    text = String(text || "").trim();
    if (!text) {
      podcast.playbackPausedForInput = false;
      pumpPlayback();
      return;
    }
    podcast.prioritySpeechActive = true;
    var generation = podcast.playbackGeneration;
    updatePodcastControl();
    try {
      setVoiceStatus((label || "主持人回应") + "语音合成中...");
      var prepared = await synthSpeech(text, voiceId, generation).catch(function (err) {
        if (isStalePlaybackError(err) || !isCurrentPlaybackGeneration(generation)) return { kind: "stale" };
        console.warn("[podcast] priority synth failed; fallback to local", err);
        return { kind: "local", text: text, voiceId: voiceId };
      });
      if (!isCurrentPlaybackGeneration(generation) || prepared.kind === "stale") return;
      prepared.label = label || "主持人回应";
      prepared.speakerKey = speakerKey || "host";
      if (isRemovedSpeakerKey(prepared.speakerKey)) return;
      if (podcast.playbackStopped) return;
      stopCurrentPlayback(false);
      setVoiceStatus(prepared.label + "正在发言...");
      setPlayingSpeaker(prepared.speakerKey);
      updatePodcastControl();
      await playPrepared(prepared);
    } finally {
      setPlayingSpeaker("");
      podcast.prioritySpeechActive = false;
      podcast.playbackPausedForInput = false;
      updatePodcastControl();
      if (!podcast.playbackStopped) pumpPlayback();
    }
  }

  function skipSpeechSeq(seq) {
    if (!seq) return;
    podcast.skippedSeqs.add(seq);
    pumpPlayback();
  }

  function advanceSkippedSeqs() {
    while (podcast.skippedSeqs.has(podcast.nextPlaySeq)) {
      podcast.skippedSeqs.delete(podcast.nextPlaySeq);
      podcast.nextPlaySeq++;
    }
  }

  async function pumpPlayback() {
    if (podcast.playPumpActive) return;
    if (podcast.playbackPausedForInput || podcast.prioritySpeechActive) return;
    var generation = podcast.playbackGeneration;
    podcast.playPumpActive = true;
    updatePodcastControl();
    try {
      while (!podcast.playbackStopped && isCurrentPlaybackGeneration(generation)) {
        if (podcast.playbackPausedForInput || podcast.prioritySpeechActive) break;
        advanceSkippedSeqs();
        var job = podcast.synthJobs.get(podcast.nextPlaySeq);
        if (!job) {
          if (!podcast.generationDone && podcast.runId) setVoiceStatus("等待下一段内容生成...");
          break;
        }
        var prepared = await job;
        if (!isCurrentPlaybackGeneration(generation) || prepared.kind === "stale") break;
        if (podcast.playbackStopped) break;
        if (isRemovedSpeakerKey(prepared.speakerKey)) {
          podcast.synthJobs.delete(podcast.nextPlaySeq);
          podcast.nextPlaySeq++;
          continue;
        }
        setVoiceStatus((prepared.label || ("第 " + podcast.nextPlaySeq + " 段")) + "正在发言...");
        podcast.currentPlaySeq = podcast.nextPlaySeq;
        setPlayingSpeaker(prepared.speakerKey || "");
        updatePodcastControl();
        await playPrepared(prepared);
        if (podcast.currentPlaySeq === podcast.nextPlaySeq) podcast.currentPlaySeq = 0;
        if (podcast.playingSpeakerKey === (prepared.speakerKey || "")) setPlayingSpeaker("");
        if (podcast.replayCurrentPlayback) {
          podcast.replayCurrentPlayback = false;
          break;
        }
        podcast.synthJobs.delete(podcast.nextPlaySeq);
        podcast.nextPlaySeq++;
      }
    } finally {
      if (!isCurrentPlaybackGeneration(generation)) return;
      podcast.playPumpActive = false;
      advanceSkippedSeqs();
      if (!podcast.playbackStopped && podcast.synthJobs.has(podcast.nextPlaySeq)) pumpPlayback();
      else if (!podcast.playbackStopped && podcast.generationDone && !podcast.synthJobs.size) {
        setPlayingSpeaker("");
        setVoiceStatus("群聊播放完成。");
      }
      updatePodcastControl();
    }
  }

  async function synthSpeech(text, voiceId, playbackGeneration) {
    await loadVoiceConfig();
    if (!isCurrentPlaybackGeneration(playbackGeneration)) throw stalePlaybackError();
    var out = selectedOut();
    if (out === "aliyun-flowing" && aliyunTtsUsable()) {
      try { return await synthFlowing(text, voiceId, playbackGeneration); }
      catch (err) { console.warn("[podcast] flowing synth failed", err); }
    }
    if (!isCurrentPlaybackGeneration(playbackGeneration)) throw stalePlaybackError();
    if (out !== "local" && aliyunTtsUsable()) {
      try { return await synthRest(text, voiceId, playbackGeneration); }
      catch (err2) { console.warn("[podcast] rest synth failed", err2); }
    }
    if (!isCurrentPlaybackGeneration(playbackGeneration)) throw stalePlaybackError();
    return { kind: "local", text: text, voiceId: voiceId };
  }

  function aliyunTtsUsable() {
    var cfg = podcast.voiceCfg || {};
    return Boolean(cfg.available && cfg.appkey && cfg.endpoint && cfg.tts && cfg.tts.enabled);
  }

  function synthFlowing(text, voiceId, playbackGeneration) {
    if (typeof root.createFlowingSpeaker !== "function") {
      return Promise.reject(new Error("flowing speaker unavailable"));
    }
    return synthCloudAudioWithRetry("aliyun-flowing", playbackGeneration, function () {
      return collectCloudAudio(function (callbacks) {
        return root.createFlowingSpeaker({
          getConfig: function () {
            var cfg = podcast.voiceCfg || {};
            return {
              appkey: cfg.appkey,
              endpoint: cfg.endpoint,
              voice: voiceId,
              sampleRate: cfg.tts && cfg.tts.sample_rate || 16000,
            };
          },
          getToken: function () {
            return typeof root.api === "function" ? root.api("/api/voice/token") : fetch("/api/voice/token", { headers: authHeadersSafe() }).then(function (r) { return r.json(); });
          },
          onAudio: callbacks.onAudio,
          onCompleted: callbacks.onCompleted,
          onError: callbacks.onError,
        });
      }, text, voiceId, "aliyun-flowing");
    });
  }

  function synthRest(text, voiceId, playbackGeneration) {
    if (typeof root.createRestSpeaker !== "function") {
      return Promise.reject(new Error("rest speaker unavailable"));
    }
    return synthCloudAudioWithRetry("aliyun-rest", playbackGeneration, function () {
      return collectCloudAudio(function (callbacks) {
        return root.createRestSpeaker({
          url: "/api/talk/speak",
          headers: authHeadersSafe(),
          getConfig: function () {
            var cfg = podcast.voiceCfg || {};
            return { voice: voiceId, sampleRate: cfg.tts && cfg.tts.sample_rate || 16000 };
          },
          onAudio: callbacks.onAudio,
          onCompleted: callbacks.onCompleted,
          onError: callbacks.onError,
        });
      }, text, voiceId, "aliyun-rest");
    });
  }

  async function synthCloudAudioWithRetry(engineName, playbackGeneration, task) {
    var lastErr = null;
    for (var attempt = 1; attempt <= CLOUD_TTS_MAX_ATTEMPTS; attempt++) {
      try {
        if (!isCurrentPlaybackGeneration(playbackGeneration)) throw stalePlaybackError();
        return await withCloudTtsSlot(task, playbackGeneration);
      } catch (err) {
        lastErr = err;
        if (isStalePlaybackError(err) || !isCurrentPlaybackGeneration(playbackGeneration)) throw stalePlaybackError();
        if (attempt >= CLOUD_TTS_MAX_ATTEMPTS || !isRetryableCloudTtsError(err)) throw err;
        console.warn("[podcast] " + engineName + " synth retry " + attempt + "/" + CLOUD_TTS_MAX_ATTEMPTS, err);
        await sleep(cloudTtsRetryDelayMs(attempt));
      }
    }
    throw lastErr || new Error(engineName + " failed");
  }

  function withCloudTtsSlot(task, playbackGeneration) {
    return new Promise(function (resolve, reject) {
      function run() {
        if (!isCurrentPlaybackGeneration(playbackGeneration)) {
          reject(stalePlaybackError());
          return;
        }
        podcast.cloudTtsActive++;
        Promise.resolve()
          .then(task)
          .then(resolve, reject)
          .finally(function () {
            podcast.cloudTtsActive = Math.max(0, podcast.cloudTtsActive - 1);
            drainCloudTtsQueue();
          });
      }
      if (podcast.cloudTtsActive < CLOUD_TTS_MAX_CONCURRENCY) run();
      else podcast.cloudTtsQueue.push({ run: run, reject: reject, playbackGeneration: playbackGeneration });
    });
  }

  function drainCloudTtsQueue() {
    while (podcast.cloudTtsActive < CLOUD_TTS_MAX_CONCURRENCY && podcast.cloudTtsQueue.length) {
      var next = podcast.cloudTtsQueue.shift();
      if (next) next.run();
    }
  }

  function cloudTtsRetryDelayMs(attempt) {
    return Math.min(4000, 600 * Math.pow(2, Math.max(0, Number(attempt) - 1)));
  }

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function isRetryableCloudTtsError(err) {
    var text = String(err && (err.message || err.name || err) || "").toLowerCase();
    if (!text) return true;
    if (text.indexOf("unavailable") >= 0 || text.indexOf("不可用") >= 0) return false;
    if (text.indexOf("config") >= 0 || text.indexOf("配置") >= 0 || text.indexOf("token 缺失") >= 0) return false;
    return Boolean(
      text.indexOf("timeout") >= 0
        || text.indexOf("429") >= 0
        || /^http 5\d\d/.test(text)
        || text.indexOf("limit") >= 0
        || text.indexOf("thrott") >= 0
        || text.indexOf("quota") >= 0
        || text.indexOf("qps") >= 0
        || text.indexOf("busy") >= 0
        || text.indexOf("rate") >= 0
        || text.indexOf("too many") >= 0
        || text.indexOf("concurrent") >= 0
        || text.indexOf("websocket 错误") >= 0
        || text.indexOf("请求失败") >= 0
        || text.indexOf("超时") >= 0
        || text.indexOf("并发") >= 0
        || text.indexOf("限流") >= 0
        || text.indexOf("频率") >= 0
    );
  }

  function collectCloudAudio(createEngine, text, voiceId, engineName) {
    return new Promise(function (resolve, reject) {
      var settled = false;
      var chunks = [];
      var engine = null;
      var timeout = setTimeout(function () {
        finish(null, new Error(engineName + " timeout"));
      }, synthTimeoutMs(text));
      function finish(value, err) {
        if (settled) return;
        settled = true;
        clearTimeout(timeout);
        if (engine) podcast.synthEngines.delete(engine);
        if (err) {
          try { if (engine) engine.abort && engine.abort(); } catch (_) {}
          reject(err);
        } else {
          resolve(value);
        }
      }
      engine = createEngine({
        onAudio: function (buf) {
          var copy = copyAudioBuffer(buf);
          if (copy) chunks.push(copy);
        },
        onCompleted: function () {
          finish({
            kind: "pcm",
            chunks: chunks,
            sampleRate: podcast.voiceCfg && podcast.voiceCfg.tts && podcast.voiceCfg.tts.sample_rate || 16000,
            voiceId: voiceId,
            engine: engineName,
          });
        },
        onError: function (name, msg) {
          finish(null, new Error(name + ": " + (msg || "")));
        },
      });
      podcast.synthEngines.add(engine);
      try {
        engine.begin({ voiceId: voiceId });
        engine.push(text, { voiceId: voiceId });
        engine.end();
      } catch (err) {
        finish(null, err);
      }
    });
  }

  function synthTimeoutMs(text) {
    return Math.min(30000, Math.max(12000, String(text || "").length * 250));
  }

  function copyAudioBuffer(buf) {
    if (!buf) return null;
    if (buf instanceof ArrayBuffer) return buf.slice(0);
    if (ArrayBuffer.isView(buf)) {
      return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
    }
    return null;
  }

  function playPrepared(prepared) {
    if (!prepared) return Promise.resolve();
    if (prepared.kind === "pcm") return playPcm(prepared);
    return playLocal(prepared.text || "", prepared.voiceId);
  }

  function ensurePcmPlayer() {
    if (podcast.currentPcmPlayer) return podcast.currentPcmPlayer;
    if (typeof root.createVoicePcmPlayer !== "function") {
      throw new Error("PCM player unavailable");
    }
    podcast.currentPcmPlayer = root.createVoicePcmPlayer({
      sampleRate: podcast.voiceCfg && podcast.voiceCfg.tts && podcast.voiceCfg.tts.sample_rate || 16000,
      onDrained: function () {
        var resolve = podcast.currentPcmResolve;
        podcast.currentPcmResolve = null;
        if (resolve) resolve();
      },
      onInterrupted: function () {
        var resolve = podcast.currentPcmResolve;
        podcast.currentPcmResolve = null;
        if (resolve) resolve();
      },
      onError: function (name, msg) { console.warn("[podcast] pcm", name, msg); },
    });
    return podcast.currentPcmPlayer;
  }

  function playPcm(prepared) {
    return new Promise(function (resolve) {
      var player = ensurePcmPlayer();
      podcast.currentPcmResolve = resolve;
      try {
        player.stop();
        player.unlock();
        var chunks = prepared.chunks || [];
        if (!chunks.length) {
          podcast.currentPcmResolve = null;
          resolve();
          return;
        }
        for (var i = 0; i < chunks.length; i++) player.enqueue(chunks[i]);
        player.markEnded();
      } catch (err) {
        console.warn("[podcast] pcm play failed", err);
        podcast.currentPcmResolve = null;
        resolve();
      }
      setTimeout(function () {
        if (podcast.currentPcmResolve === resolve) {
          podcast.currentPcmResolve = null;
          resolve();
        }
      }, playbackTimeoutMs(prepared));
    });
  }

  function playbackTimeoutMs(prepared) {
    var chunks = prepared && prepared.chunks || [];
    var bytes = 0;
    for (var i = 0; i < chunks.length; i++) {
      bytes += chunks[i] && chunks[i].byteLength || 0;
    }
    var sampleRate = Number(prepared && prepared.sampleRate) || Number(podcast.voiceCfg && podcast.voiceCfg.tts && podcast.voiceCfg.tts.sample_rate) || 16000;
    var durationMs = sampleRate > 0 ? bytes / 2 / sampleRate * 1000 : 0;
    return Math.max(10000, Math.ceil(durationMs + 8000));
  }

  function playLocal(text, voiceId) {
    return new Promise(function (resolve) {
      if (typeof root.createLocalSpeaker !== "function") {
        resolve();
        return;
      }
      var settled = false;
      function done() {
        if (settled) return;
        settled = true;
        if (podcast.currentSpeaker === speaker) podcast.currentSpeaker = null;
        if (podcast.currentSpeakerResolve === done) podcast.currentSpeakerResolve = null;
        resolve();
      }
      var speaker = root.createLocalSpeaker({
        getVoice: function () { return selectedSystemVoice(voiceId); },
        onCompleted: done,
        onError: done,
      });
      podcast.currentSpeaker = speaker;
      podcast.currentSpeakerResolve = done;
      try {
        speaker.begin();
        speaker.push(text, { voiceId: voiceId });
        speaker.end();
      } catch (_) {
        done();
      }
      setTimeout(done, Math.max(5000, text.length * 260));
    });
  }

  function selectedSystemVoice(voiceURI) {
    if (!voiceURI || !root.speechSynthesis) return null;
    var voices = [];
    try { voices = root.speechSynthesis.getVoices() || []; } catch (_) {}
    for (var i = 0; i < voices.length; i++) {
      if (voices[i].voiceURI === voiceURI) return voices[i];
    }
    return null;
  }

  function shouldIgnoreOverlayTap(target) {
    if (!target || typeof target.closest !== "function") return false;
    return Boolean(
      target.closest(".voice-stage-head, .voice-footer, .podcast-dialog, .podcast-stage-stop, .podcast-action-chip, .group-member-menu, .group-participant, .group-agent-grid")
      || target.closest(".voice-circle")
    );
  }

  function startPodcastFromUi() {
    if (!podcast.agents.length) addGroupAgent();
    if (explicitTopicValue()) {
      startPodcast();
      return;
    }
    startTopicCapture();
  }

  function startTopicCapture() {
    if (podcast.runId || podcast.starting || podcast.capturingTopic) return;
    var SR = root.SpeechRecognition || root.webkitSpeechRecognition;
    if (!SR) {
      setDialog(true);
      setStatus("当前浏览器不支持语音输入，请手动输入话题。");
      setVoiceStatus("当前浏览器不支持语音话题输入。");
      return;
    }
    podcast.topicCaptureArmed = true;
    podcast.capturingTopic = true;
    setPodcastMode(true);
    setActive(false);
    setDialog(false);
    setStatus("请说出群聊要讨论的话题...");
    setVoiceStatus("正在收听群聊话题，说完后会自动开始。");
    var rec = new SR();
    podcast.topicRecognizer = rec;
    var submitted = false;
    rec.lang = "zh-CN";
    rec.interimResults = false;
    rec.continuous = false;
    rec.onresult = function (event) {
      var text = "";
      for (var i = 0; i < event.results.length; i++) {
        text += event.results[i][0] && event.results[i][0].transcript || "";
      }
      text = text.trim();
      submitted = Boolean(text);
      stopTopicCapture();
      if (!text) {
        podcast.topicCaptureArmed = true;
        setStatus("没有听清话题，点击屏幕可重试。");
        setVoiceStatus("没有听清话题，点击屏幕可重新说。");
        return;
      }
      var topic = $("podcastTopic");
      if (topic) topic.value = text;
      addBubble("you", "话题：" + text);
      startPodcast(text);
    };
    rec.onerror = function () {
      stopTopicCapture();
      podcast.topicCaptureArmed = true;
      setStatus("话题识别失败，点击屏幕可重试。");
      setVoiceStatus("话题识别失败，点击屏幕可重新说。");
      updatePodcastControl();
    };
    rec.onend = function () {
      if (podcast.topicRecognizer === rec) podcast.topicRecognizer = null;
      podcast.capturingTopic = false;
      setActive(Boolean(podcast.runId));
      updatePodcastControl();
      if (!submitted && !podcast.runId && !podcast.starting) {
        podcast.topicCaptureArmed = true;
        setStatus("点击屏幕空白处，说出群聊话题。");
        setVoiceStatus("群聊待启动，点击屏幕说出讨论话题。");
      }
    };
    try { rec.start(); } catch (_) {
      stopTopicCapture();
      podcast.topicCaptureArmed = true;
      setStatus("话题识别启动失败，点击屏幕可重试。");
      setVoiceStatus("话题识别启动失败。");
      updatePodcastControl();
    }
  }

  function stopTopicCapture() {
    var rec = podcast.topicRecognizer;
    podcast.topicRecognizer = null;
    podcast.capturingTopic = false;
    setActive(Boolean(podcast.runId));
    updatePodcastControl();
    if (!rec) return;
    try { rec.onresult = null; rec.onerror = null; } catch (_) {}
    try { rec.stop(); } catch (_) {}
    try { rec.abort && rec.abort(); } catch (_) {}
  }

  function startInterjectionCapture() {
    if (!podcast.runId || podcast.capturingInput) return;
    var SR = root.SpeechRecognition || root.webkitSpeechRecognition;
    if (!SR) {
      setStatus("当前浏览器不支持本地语音插话。");
      return;
    }
    podcast.playbackPausedForInput = true;
    stopCurrentPlayback(true);
    podcast.capturingInput = true;
    setPodcastMode(true);
    updatePodcastControl();
    setStatus("请说出你的观点或问题...");
    setVoiceStatus("正在收听你的插话，说完后会自动关闭麦克风...");
    var rec = new SR();
    podcast.inputRecognizer = rec;
    var submitted = false;
    rec.lang = "zh-CN";
    rec.interimResults = false;
    rec.continuous = false;
    rec.onresult = function (event) {
      var text = "";
      for (var i = 0; i < event.results.length; i++) {
        text += event.results[i][0] && event.results[i][0].transcript || "";
      }
      submitted = true;
      stopInterjectionCapture();
      submitInterjection(text.trim());
    };
    rec.onerror = function () {
      stopInterjectionCapture();
      podcast.playbackPausedForInput = false;
      setStatus("插话识别失败。");
      setVoiceStatus("插话识别失败，继续播放群聊。");
      pumpPlayback();
    };
    rec.onend = function () {
      if (podcast.inputRecognizer === rec) podcast.inputRecognizer = null;
      podcast.capturingInput = false;
      if (!submitted) {
        podcast.playbackPausedForInput = false;
        pumpPlayback();
      }
      if (podcast.runId) setStatus("群聊进行中，点屏幕空白处可插话。");
      updatePodcastControl();
    };
    try { rec.start(); } catch (_) {
      stopInterjectionCapture();
      podcast.playbackPausedForInput = false;
      pumpPlayback();
    }
  }

  function stopInterjectionCapture() {
    var rec = podcast.inputRecognizer;
    podcast.inputRecognizer = null;
    podcast.capturingInput = false;
    updatePodcastControl();
    if (!rec) return;
    try { rec.onresult = null; rec.onerror = null; } catch (_) {}
    try { rec.stop(); } catch (_) {}
    try { rec.abort && rec.abort(); } catch (_) {}
  }

  async function submitInterjection(text) {
    if (!text || !podcast.runId) {
      podcast.playbackPausedForInput = false;
      pumpPlayback();
      return;
    }
    podcast.pendingInputText = text;
    resetForUserInput(podcast.generation + 1);
    addBubble("you", text);
    try {
      await apiSafe("/api/voice/podcast/input", { run_id: podcast.runId, text: text });
    } catch (err) {
      podcast.playbackPausedForInput = false;
      setStatus("插话发送失败：" + (err && err.message || err));
      setVoiceStatus("插话发送失败，继续播放群聊。");
      pumpPlayback();
    }
  }

  function init() {
    var btn = $("voicePodcastBtn");
    if (btn) btn.onclick = function () {
      if (podcast.mode) {
        if (podcast.runId || podcast.starting || podcast.capturingTopic || podcast.capturingInput) {
          setStatus("群聊正在进行中，请先停止后再切回语音。");
          setVoiceStatus("群聊进行中，停止后可切回语音。");
          return;
        }
        podcast.topicCaptureArmed = false;
        stopTopicCapture();
        setDialog(false);
        setPodcastMode(false);
        setVoiceStatus("点击麦克风，开始连续语音对话");
        return;
      }
      setPodcastMode(true);
      setDialog(true);
    };
    var close = $("podcastCloseBtn");
    if (close) close.onclick = function () { setDialog(false); };
    var dlg = $("podcastDialog");
    if (dlg) dlg.addEventListener("click", function (event) {
      if (event.target === dlg) setDialog(false);
    });
    var agentDlg = $("agentEditorDialog");
    if (agentDlg) agentDlg.addEventListener("click", function (event) {
      if (event.target === agentDlg) setAgentEditor(false);
    });
    var agentClose = $("agentEditorCloseBtn");
    if (agentClose) agentClose.onclick = function () { setAgentEditor(false); };
    var agentCancel = $("agentEditorCancelBtn");
    if (agentCancel) agentCancel.onclick = function () { setAgentEditor(false); };
    var agentDone = $("agentEditorDoneBtn");
    if (agentDone) agentDone.onclick = confirmAgentEditor;
    var agentRole = $("agentEditorRole");
    if (agentRole) agentRole.onchange = function () {
      updateEditorDraft("role", agentRole.value, optionLabel(agentRole));
    };
    var agentVoice = $("agentEditorVoice");
    if (agentVoice) agentVoice.onchange = function () {
      updateEditorDraft("voice", agentVoice.value, optionLabel(agentVoice));
    };
    var agentModel = $("agentEditorModel");
    if (agentModel) agentModel.onchange = function () {
      updateEditorDraft("model", agentModel.value, optionLabel(agentModel));
    };
    var add = $("podcastAddAgentBtn");
    if (add) add.onclick = function () {
      openAddAgentEditor();
    };
    var voiceAdd = $("voiceAddRoleBtn");
    if (voiceAdd) voiceAdd.onclick = function (event) {
      event.stopPropagation();
      openAddAgentEditor();
    };
    var start = $("podcastStartBtn");
    if (start) start.onclick = startPodcastFromUi;
    var stop = $("podcastStopBtn");
    if (stop) stop.onclick = stopPodcast;
    var stageStop = $("podcastStageStopBtn");
    if (stageStop) stageStop.onclick = function (event) {
      event.stopPropagation();
      stopPodcast();
    };
    var exit = $("voiceExitBtn");
    if (exit) exit.addEventListener("click", function (event) {
      if (podcast.runId || podcast.starting || podcast.capturingTopic || podcast.capturingInput) {
        event.preventDefault();
        event.stopImmediatePropagation();
        setStatus("群聊进行中，请先点击停止。");
        setVoiceStatus("群聊进行中，请先点击停止。");
        return;
      }
      if (podcast.agents.length) exitGroupChat();
    }, true);
    var podcastCircle = $("podcastCircle");
    if (podcastCircle) podcastCircle.addEventListener("click", function (event) {
      if (!podcast.mode) return;
      event.stopPropagation();
      event.preventDefault();
      if (podcast.runId) startInterjectionCapture();
      else if (explicitTopicValue()) startPodcast();
      else startTopicCapture();
    }, true);
    var overlay = $("voiceOverlay");
    if (overlay) overlay.addEventListener("click", function (event) {
      if (!podcast.mode && !podcast.runId && !podcast.topicCaptureArmed) return;
      if (shouldIgnoreOverlayTap(event.target)) return;
      event.stopPropagation();
      event.preventDefault();
      if (podcast.runId) startInterjectionCapture();
      else if (explicitTopicValue()) startPodcast();
      else startTopicCapture();
    }, true);
    function restoreAndResumePodcast() {
      restorePodcastSurface();
      if (podcast.mode || podcast.runId) {
        primePlayback();
        if (!podcast.playbackStopped) pumpPlayback();
      }
    }
    renderAgents();
    renderParticipants();
    restorePodcastSurface();
    if (root.document) {
      root.document.addEventListener("visibilitychange", function () {
        if (root.document.visibilityState === "visible") restoreAndResumePodcast();
        else savePodcastState();
      });
    }
    if (root.window) {
      root.window.addEventListener("pagehide", savePodcastState);
      root.window.addEventListener("pageshow", restoreAndResumePodcast);
      root.window.addEventListener("focus", restoreAndResumePodcast);
    } else if (root.addEventListener) {
      root.addEventListener("pagehide", savePodcastState);
      root.addEventListener("pageshow", restoreAndResumePodcast);
      root.addEventListener("focus", restoreAndResumePodcast);
    }
    if (root.document) {
      root.document.addEventListener("click", function (event) {
        if (!event.target.closest || !event.target.closest(".group-member-menu, .group-participant")) closeMemberMenu();
      });
    }
  }

  if (root.document) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
    else init();
  }

  return {
    onEvent: onEvent,
    _helpers: {
      roles: ROLES,
      selectedOut: selectedOut,
      escapeHtml: escapeHtml,
      playbackTimeoutMs: playbackTimeoutMs,
      synthTimeoutMs: synthTimeoutMs,
      cloudTtsRetryDelayMs: cloudTtsRetryDelayMs,
      isRetryableCloudTtsError: isRetryableCloudTtsError,
      finalUtteranceText: finalUtteranceText,
      shouldIgnoreOverlayTap: shouldIgnoreOverlayTap,
    },
  };
});
