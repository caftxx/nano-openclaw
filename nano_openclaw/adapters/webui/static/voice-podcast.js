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

  var podcast = {
    runId: "",
    active: false,
    starting: false,
    agents: [{ role: "自动" }, { role: "自动" }],
    utterances: new Map(),
    currentSpeaker: null,
    currentSpeakerResolve: null,
    currentPcmPlayer: null,
    currentPcmResolve: null,
    inputRecognizer: null,
    synthJobs: new Map(),
    synthEngines: new Set(),
    skippedSeqs: new Set(),
    nextSeq: 1,
    nextPlaySeq: 1,
    playPumpActive: false,
    voiceCfg: null,
    capturingInput: false,
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

  function renderAgents() {
    var rootEl = $("podcastAgents");
    if (!rootEl) return;
    rootEl.innerHTML = "";
    podcast.agents.forEach(function (agent, index) {
      var row = document.createElement("div");
      row.className = "podcast-agent-row";
      var select = document.createElement("select");
      select.setAttribute("aria-label", "Agent身份");
      ROLES.forEach(function (role) {
        var opt = document.createElement("option");
        opt.value = role;
        opt.textContent = role;
        select.appendChild(opt);
      });
      select.value = agent.role || "自动";
      select.onchange = function () { podcast.agents[index].role = select.value; };
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "podcast-agent-remove";
      remove.setAttribute("aria-label", "删除Agent");
      remove.textContent = "×";
      remove.onclick = function () {
        podcast.agents.splice(index, 1);
        if (!podcast.agents.length) podcast.agents.push({ role: "自动" });
        renderAgents();
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
      renderAgents();
      updateHostPreview();
      loadVoiceConfig().then(updateHostPreview).catch(function () {});
    }
  }
  function setStatus(text) {
    var el = $("podcastStatus");
    if (el) el.textContent = text || "";
  }
  function setVoiceStatus(text) {
    var el = $("voiceStatus");
    if (el) el.textContent = text || "";
  }
  function phaseLabel(event) {
    if (event.phase === "opening") return "主持人开场";
    if (event.phase === "interjection") return "主持人回应插话";
    if (event.phase === "summary") return "第 " + event.round + " 轮主持人总结";
    if (event.phase === "speaker") return "第 " + event.round + " 轮 " + (event.role || "Agent") + " 发言";
    return event.role || "播客";
  }
  function setActive(active) {
    podcast.active = Boolean(active);
    var btn = $("voicePodcastBtn");
    var start = $("podcastStartBtn");
    var stop = $("podcastStopBtn");
    if (btn) btn.classList.toggle("is-active", podcast.active);
    if (start) start.disabled = podcast.active || podcast.starting;
    if (stop) stop.disabled = !podcast.active;
  }

  function topicValue() {
    var el = $("podcastTopic");
    var value = el ? el.value.trim() : "";
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

  async function startPodcast() {
    if (podcast.active || podcast.starting) return;
    podcast.starting = true;
    setActive(false);
    setStatus("正在启动...");
    setVoiceStatus("AI播客正在启动...");
    try {
      var roundsEl = $("podcastRounds");
      await loadVoiceConfig();
      var hostVoice = currentHostVoice();
      var payload = await apiSafe("/api/voice/podcast/start", {
        session_id: currentSessionId(),
        topic: topicValue(),
        rounds: Number(roundsEl && roundsEl.value || 20),
        host_voice_id: hostVoice.id,
        host_voice_label: hostVoice.label,
        agents: podcast.agents.map(function (agent, index) {
          return { id: "agent-" + (index + 1), role: agent.role || "自动" };
        }),
      });
      podcast.runId = payload.run_id || "";
      podcast.playbackStopped = false;
      podcast.generationDone = false;
      resetPlaybackState();
      primePlayback();
      renderAssignments(payload);
      setStatus("播客进行中，点屏幕空白处可插话。");
      setVoiceStatus("AI播客已启动，正在生成主持人开场...");
      setDialog(false);
      setActive(true);
    } catch (err) {
      setStatus("启动失败：" + (err && err.message || err));
    } finally {
      podcast.starting = false;
      setActive(Boolean(podcast.runId));
    }
  }

  async function stopPodcast() {
    if (!podcast.runId) return;
    var runId = podcast.runId;
    podcast.runId = "";
    podcast.playbackStopped = true;
    podcast.playbackPausedForInput = false;
    podcast.prioritySpeechActive = false;
    podcast.generationDone = true;
    setActive(false);
    stopInterjectionCapture();
    stopSpeech();
    setStatus("正在停止...");
    setVoiceStatus("AI播客正在停止...");
    try { await apiSafe("/api/voice/podcast/stop", { run_id: runId }); } catch (_) {}
  }

  function renderAssignments(payload) {
    var el = $("podcastAssignedVoices");
    if (!el || !payload) return;
    var agents = payload.agents || [];
    var host = payload.host || {};
    el.hidden = false;
    el.innerHTML = "主持人：" + escapeHtml(host.voice_label || host.voice_id || "当前音色") + "<br>" + agents.map(function (a) {
      return escapeHtml(a.role + "：" + (a.voice_label || a.voice_id || ""));
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

  function onEvent(event) {
    if (!event || typeof event.type !== "string" || !event.type.startsWith("podcast.")) return;
    if (event.run_id && podcast.runId && event.run_id !== podcast.runId) return;
    if (event.type === "podcast.started") {
      podcast.runId = event.run_id || podcast.runId;
      podcast.playbackStopped = false;
      podcast.generationDone = false;
      resetPlaybackState();
      primePlayback();
      renderAssignments(event);
      setActive(true);
      setVoiceStatus("AI播客已启动，正在生成主持人开场...");
      return;
    }
    if (event.type === "podcast.input.accepted") {
      addBubble("you", event.text || "");
      setVoiceStatus("已收到你的插话，主持人准备回应...");
      return;
    }
    if (event.type === "podcast.round.started") {
      setVoiceStatus("第 " + event.round + " 轮开始，" + (event.speaker_count || 1) + " 个 Agent 并行生成中...");
      return;
    }
    if (event.type === "podcast.research.started") {
      setVoiceStatus("第 " + event.round + " 轮 " + (event.role || "Agent") + " 首次深度 research 中...");
      return;
    }
    if (event.type === "podcast.research.done") {
      setVoiceStatus("第 " + event.round + " 轮 " + (event.role || "Agent") + " research 完成，正在生成观点...");
      return;
    }
    if (event.type === "podcast.utterance.started") {
      var speaker = event.role + " · " + (event.voice_label || event.voice_id || "");
      var seq = Number(event.sequence) || podcast.nextSeq++;
      if (seq >= podcast.nextSeq) podcast.nextSeq = seq + 1;
      podcast.utterances.set(event.utterance_id, {
        seq: seq,
        text: "",
        node: addBubble("ai", "", speaker),
      });
      setVoiceStatus(phaseLabel(event) + "生成中...");
      return;
    }
    if (event.type === "podcast.text.delta") {
      var entry = podcast.utterances.get(event.utterance_id);
      if (!entry) return;
      entry.text += event.text || "";
      updateBubble(entry, entry.text);
      return;
    }
    if (event.type === "podcast.utterance.done") {
      var done = podcast.utterances.get(event.utterance_id);
      if (done) updateBubble(done, event.text || done.text);
      var doneSeq = done ? done.seq : (Number(event.sequence) || podcast.nextSeq++);
      if (doneSeq >= podcast.nextSeq) podcast.nextSeq = doneSeq + 1;
      if (event.phase === "interjection") {
        skipSpeechSeq(doneSeq);
        enqueuePrioritySpeech(event.text || "", event.voice_id || "xiaoxian", phaseLabel(event));
        setVoiceStatus(phaseLabel(event) + "已生成，正在优先准备语音...");
        return;
      }
      enqueueSpeech(doneSeq, event.text || "", event.voice_id || "xiaoxian", phaseLabel(event));
      setVoiceStatus(phaseLabel(event) + "已生成，正在准备语音...");
      return;
    }
    if (event.type === "podcast.round.done") {
      setVoiceStatus("第 " + event.round + " 轮内容已生成，继续生成后续轮次...");
      return;
    }
    if (event.type === "podcast.done") {
      podcast.runId = "";
      podcast.generationDone = true;
      setActive(false);
      setStatus("播客内容已生成完成。");
      setVoiceStatus("AI播客内容已生成完成，继续播放剩余语音...");
      pumpPlayback();
      return;
    }
    if (event.type === "podcast.stopped") {
      podcast.runId = "";
      podcast.generationDone = true;
      podcast.playbackStopped = true;
      podcast.playbackPausedForInput = false;
      podcast.prioritySpeechActive = false;
      setActive(false);
      stopInterjectionCapture();
      setStatus("播客已停止。");
      setVoiceStatus("AI播客已停止。");
      return;
    }
    if (event.type === "podcast.error") {
      podcast.runId = "";
      podcast.generationDone = true;
      setActive(false);
      setStatus("播客出错：" + (event.message || "未知错误"));
      setVoiceStatus("AI播客出错：" + (event.message || "未知错误"));
    }
  }

  function stopSpeech() {
    stopCurrentPlayback(false);
    podcast.synthEngines.forEach(function (engine) {
      try { engine.abort && engine.abort(); } catch (_) {}
    });
    podcast.synthEngines.clear();
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
    if (hadCurrent && replayCurrent) podcast.replayCurrentPlayback = true;
  }

  function resetPlaybackState() {
    stopSpeech();
    podcast.synthJobs.clear();
    podcast.skippedSeqs.clear();
    podcast.nextSeq = 1;
    podcast.nextPlaySeq = 1;
    podcast.playPumpActive = false;
    podcast.playbackPausedForInput = false;
    podcast.prioritySpeechActive = false;
    podcast.replayCurrentPlayback = false;
  }

  function primePlayback() {
    try {
      ensurePcmPlayer().unlock();
    } catch (_) {}
  }

  function enqueueSpeech(seq, text, voiceId, label) {
    text = String(text || "").trim();
    if (!text) {
      skipSpeechSeq(seq);
      return;
    }
    setVoiceStatus((label || ("第 " + seq + " 段")) + "语音合成中...");
    var job = synthSpeech(text, voiceId).then(function (prepared) {
      prepared.label = label || ("第 " + seq + " 段");
      return prepared;
    }).catch(function (err) {
      console.warn("[podcast] synth failed; fallback to local", err);
      return { kind: "local", text: text, voiceId: voiceId, label: label || ("第 " + seq + " 段") };
    });
    podcast.synthJobs.set(seq, job);
    pumpPlayback();
  }

  async function enqueuePrioritySpeech(text, voiceId, label) {
    text = String(text || "").trim();
    if (!text) {
      podcast.playbackPausedForInput = false;
      pumpPlayback();
      return;
    }
    podcast.prioritySpeechActive = true;
    try {
      setVoiceStatus((label || "主持人回应") + "语音合成中...");
      var prepared = await synthSpeech(text, voiceId).catch(function (err) {
        console.warn("[podcast] priority synth failed; fallback to local", err);
        return { kind: "local", text: text, voiceId: voiceId };
      });
      prepared.label = label || "主持人回应";
      if (podcast.playbackStopped) return;
      stopCurrentPlayback(false);
      setVoiceStatus(prepared.label + "播放中...");
      await playPrepared(prepared);
    } finally {
      podcast.prioritySpeechActive = false;
      podcast.playbackPausedForInput = false;
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
    podcast.playPumpActive = true;
    try {
      while (!podcast.playbackStopped) {
        if (podcast.playbackPausedForInput || podcast.prioritySpeechActive) break;
        advanceSkippedSeqs();
        var job = podcast.synthJobs.get(podcast.nextPlaySeq);
        if (!job) {
          if (!podcast.generationDone && podcast.runId) setVoiceStatus("等待下一段内容生成...");
          break;
        }
        var prepared = await job;
        if (podcast.playbackStopped) break;
        setVoiceStatus((prepared.label || ("第 " + podcast.nextPlaySeq + " 段")) + "播放中...");
        await playPrepared(prepared);
        if (podcast.replayCurrentPlayback) {
          podcast.replayCurrentPlayback = false;
          break;
        }
        podcast.synthJobs.delete(podcast.nextPlaySeq);
        podcast.nextPlaySeq++;
      }
    } finally {
      podcast.playPumpActive = false;
      advanceSkippedSeqs();
      if (!podcast.playbackStopped && podcast.synthJobs.has(podcast.nextPlaySeq)) pumpPlayback();
      else if (!podcast.playbackStopped && podcast.generationDone && !podcast.synthJobs.size) setVoiceStatus("AI播客播放完成。");
    }
  }

  async function synthSpeech(text, voiceId) {
    await loadVoiceConfig();
    var out = selectedOut();
    if (out === "aliyun-flowing" && aliyunTtsUsable()) {
      try { return await synthFlowing(text, voiceId); }
      catch (err) { console.warn("[podcast] flowing synth failed", err); }
    }
    if (out !== "local" && aliyunTtsUsable()) {
      try { return await synthRest(text, voiceId); }
      catch (err2) { console.warn("[podcast] rest synth failed", err2); }
    }
    return { kind: "local", text: text, voiceId: voiceId };
  }

  function aliyunTtsUsable() {
    var cfg = podcast.voiceCfg || {};
    return Boolean(cfg.available && cfg.appkey && cfg.endpoint && cfg.tts && cfg.tts.enabled);
  }

  function synthFlowing(text, voiceId) {
    if (typeof root.createFlowingSpeaker !== "function") {
      return Promise.reject(new Error("flowing speaker unavailable"));
    }
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
  }

  function synthRest(text, voiceId) {
    if (typeof root.createRestSpeaker !== "function") {
      return Promise.reject(new Error("rest speaker unavailable"));
    }
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
  }

  function collectCloudAudio(createEngine, text, voiceId, engineName) {
    return new Promise(function (resolve, reject) {
      var settled = false;
      var chunks = [];
      var engine = null;
      var timeout = setTimeout(function () {
        finish(null, new Error(engineName + " timeout"));
      }, Math.max(15000, text.length * 900));
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
      setVoiceStatus("插话识别失败，继续播放播客。");
      pumpPlayback();
    };
    rec.onend = function () {
      if (podcast.inputRecognizer === rec) podcast.inputRecognizer = null;
      podcast.capturingInput = false;
      if (!submitted) {
        podcast.playbackPausedForInput = false;
        pumpPlayback();
      }
      if (podcast.runId) setStatus("播客进行中，点屏幕空白处可插话。");
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
    try {
      await apiSafe("/api/voice/podcast/input", { run_id: podcast.runId, text: text });
    } catch (err) {
      podcast.playbackPausedForInput = false;
      setStatus("插话发送失败：" + (err && err.message || err));
      setVoiceStatus("插话发送失败，继续播放播客。");
      pumpPlayback();
    }
  }

  function init() {
    var btn = $("voicePodcastBtn");
    if (!btn) return;
    btn.onclick = function () { setDialog(true); };
    var close = $("podcastCloseBtn");
    if (close) close.onclick = function () { setDialog(false); };
    var dlg = $("podcastDialog");
    if (dlg) dlg.addEventListener("click", function (event) {
      if (event.target === dlg) setDialog(false);
    });
    var add = $("podcastAddAgentBtn");
    if (add) add.onclick = function () {
      podcast.agents.push({ role: "自动" });
      renderAgents();
    };
    var start = $("podcastStartBtn");
    if (start) start.onclick = startPodcast;
    var stop = $("podcastStopBtn");
    if (stop) stop.onclick = stopPodcast;
    var overlay = $("voiceOverlay");
    if (overlay) overlay.addEventListener("click", function (event) {
      if (!podcast.runId) return;
      if (event.target.closest(".voice-stage-head, .voice-footer, .voice-captions, .voice-circle, .podcast-dialog")) return;
      event.stopPropagation();
      event.preventDefault();
      startInterjectionCapture();
    }, true);
    renderAgents();
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
    },
  };
});
