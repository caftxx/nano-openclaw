/* nano-openclaw 语音端 —— 开车免提语音交互
 *
 * 链路：
 *   webkitSpeechRecognition 转文字
 *     -> ws.send("chat.send", {session_id, text})   (复用现有 /ws 协议)
 *     -> 收 text.delta 累积 -> turn.done
 *     -> speechSynthesis 朗读回复
 *
 * 状态机（连续免提）：
 *   IDLE --点击--> LISTENING --说完一句(isFinal)--> THINKING
 *   THINKING --turn.done--> SPEAKING --朗读完onend--> LISTENING (循环)
 *
 * 关键防回环：进入 THINKING/SPEAKING 时停止识别，朗读结束后才重新 start()，
 * 否则 TTS 读的话会被麦克风再次识别，造成自问自答死循环。
 */

(() => {
  "use strict";

  // ── DOM ────────────────────────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);
  const micBtn = $("mic");
  const micLabel = $("micLabel");
  const statusEl = $("status");
  const connEl = $("conn");
  const captionsEl = $("captions");
  const unsupportedEl = $("unsupported");
  const thinkLevelEl = $("thinkLevel");

  // 语音端忽略 thinking.* 事件，所以开思考不会被 TTS 读出来，只是让模型多想一会儿。

  // ── 能力检测 ─────────────────────────────────────────────────────────────
  // secure context 检测先行：手机浏览器在非 HTTPS（且非 localhost）下会直接
  // 禁用麦克风 / 语音识别，报 not-allowed —— 看起来像"权限被拒绝"，实则是协议
  // 问题。必须在这里拦下来，否则用户去浏览器设置里怎么开都没用。
  const isLocalHost = /^(localhost|127\.|0\.0\.0\.0$|\[?::1\]?$)/.test(location.hostname);
  const secureOk = window.isSecureContext || isLocalHost;
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const synth = window.speechSynthesis;

  if (!secureOk) {
    unsupportedEl.innerHTML =
      "当前通过 <b>HTTP</b> 访问，手机浏览器会禁用麦克风（即使在设置里允许也无效）。<br>" +
      "请改用 <b>HTTPS</b> 地址访问：用隧道（cloudflared / ngrok）或让网关启用 TLS（<code>--tls-cert/--tls-key</code>）。";
    unsupportedEl.classList.remove("hidden");
    micBtn.disabled = true;
    statusEl.textContent = "需要 HTTPS 才能使用麦克风";
  } else if (!SR) {
    unsupportedEl.classList.remove("hidden");
    micBtn.disabled = true;
    statusEl.textContent = "浏览器不支持语音识别";
  }

  // ── 全局状态 ─────────────────────────────────────────────────────────────
  const state = {
    phase: "idle",            // idle | listening | thinking | speaking
    active: false,            // 用户是否开启了免提循环（点过麦克风）
    ws: null,
    wsReady: false,
    reconnectDelay: 1200,
    token: new URLSearchParams(location.search).get("token") || "",
    sessionId: null,
    recog: null,
    assistantText: "",        // 当前 turn 累积的回复文本
    spokenLen: 0,             // 已经送去朗读的字符数（边收边读用）
    interimNode: null,        // 临时（未定稿）识别字幕节点
    pendingSpeak: [],         // 朗读队列
    speaking: false,
    lastUserText: "",
    thinkingLevel: "off",     // 占位；连接后由后端 state.updated 回填真实值
  };

  // ── 工具：状态展示 ───────────────────────────────────────────────────────
  const PHASE_UI = {
    idle:      { cls: "off",       emoji: "🎙️", label: "点击开始", status: "点击麦克风，开始连续语音对话" },
    listening: { cls: "listening", emoji: "👂", label: "正在聆听…", status: "请说话，停顿后自动发送" },
    thinking:  { cls: "thinking",  emoji: "🤔", label: "思考中…",   status: "已发送，等待回复…" },
    speaking:  { cls: "speaking",  emoji: "🔊", label: "朗读中…",   status: "点屏幕任意处可打断" },
    error:     { cls: "error",     emoji: "⚠️", label: "出错",      status: "" },
  };

  function setPhase(phase, statusOverride) {
    state.phase = phase;
    const ui = PHASE_UI[phase] || PHASE_UI.idle;
    micBtn.className = ui.cls;
    micBtn.querySelector(".emoji").textContent = ui.emoji;
    micLabel.textContent = ui.label;
    statusEl.textContent = statusOverride != null ? statusOverride : ui.status;
  }

  function setConn(ok, text) {
    connEl.textContent = text;
    connEl.className = ok ? "ok" : "bad";
  }

  // ── 字幕 ─────────────────────────────────────────────────────────────────
  function addBubble(role, text, interim) {
    const div = document.createElement("div");
    div.className = `bubble ${role === "you" ? "you" : "ai"}${interim ? " interim" : ""}`;
    div.textContent = text;
    captionsEl.appendChild(div);
    captionsEl.scrollTop = captionsEl.scrollHeight;
    return div;
  }

  function updateBubble(node, text) {
    if (!node) return;
    node.textContent = text;
    captionsEl.scrollTop = captionsEl.scrollHeight;
  }

  // ── WebSocket（复用现有 /ws 协议）──────────────────────────────────────────
  function connect() {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const qs = state.token ? `?token=${encodeURIComponent(state.token)}` : "";
    let ws;
    try {
      ws = new WebSocket(`${scheme}://${location.host}/ws${qs}`);
    } catch (e) {
      setConn(false, "连接失败");
      return;
    }
    state.ws = ws;

    ws.onopen = () => {
      state.wsReady = true;
      state.reconnectDelay = 1200;
      setConn(true, "已连接");
      // 不再强写 thinking —— 跟随后端：连接建立后服务器会推 state.updated，
      // 里面带真实的 thinking_level，下面 handleEvent 据此回填下拉框。
    };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      handleEvent(msg);
    };
    ws.onclose = () => {
      state.wsReady = false;
      setConn(false, "重连中…");
      const delay = state.reconnectDelay;
      state.reconnectDelay = Math.min(delay * 1.6, 10000);
      setTimeout(connect, delay);
    };
    ws.onerror = () => { /* onclose 会接手重连 */ };
  }

  function wsSend(type, payload = {}) {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return false;
    state.ws.send(JSON.stringify({ type, ...payload }));
    return true;
  }

  // 只关心当前 session 的事件
  function isMine(event) {
    return !event.session_id || event.session_id === state.sessionId;
  }

  function handleEvent(event) {
    switch (event.type) {
      case "state.updated":
        // 后端广播真实运行时（含 thinking_level）。语音端跟随它，不覆盖。
        // 任何前端改了 thinking，这里都会被推到并回填下拉框。
        buildThinkOptions(event.thinking_options);
        reflectThinking(event.thinking_level);
        break;

      case "session.updated":
        // 服务器在连接建立后自动 create 一个 session 并推下来
        if (event.session && event.session.session_id) {
          state.sessionId = event.session.session_id;
        }
        break;

      case "chat.accepted":
        if (!isMine(event)) break;
        state.assistantText = "";
        state.spokenLen = 0;
        break;

      case "text.delta":
        if (!isMine(event)) break;
        state.assistantText += event.text || "";
        renderAssistantInterim();
        speakReadyChunks(false);   // 边收边读：遇到句末标点就读出来，降低延迟
        break;

      case "turn.done":
        if (!isMine(event)) break;
        if (event.session && event.session.session_id) state.sessionId = event.session.session_id;
        finalizeAssistant();
        speakReadyChunks(true);    // 把剩余尾巴读完
        // 若整段为空（没文字回复），直接回到聆听
        if (!state.assistantText.trim() && state.pendingSpeak.length === 0 && !state.speaking) {
          resumeListeningIfActive();
        }
        break;

      case "turn.error":
        if (!isMine(event)) break;
        finalizeAssistant();
        const m = event.message || "未知错误";
        addBubble("ai", `⚠️ 出错：${m}`);
        speak(`出错了：${m}`, () => resumeListeningIfActive());
        break;

      case "turn.cancelled":
        if (!isMine(event)) break;
        resumeListeningIfActive();
        break;

      // 其余事件（tool.start/result、thinking.* 等）语音端忽略
      default:
        break;
    }
  }

  // ── 助手回复字幕 ─────────────────────────────────────────────────────────
  let _assistantNode = null;
  function renderAssistantInterim() {
    if (!_assistantNode) _assistantNode = addBubble("ai", "");
    updateBubble(_assistantNode, state.assistantText);
  }
  function finalizeAssistant() {
    if (_assistantNode) updateBubble(_assistantNode, state.assistantText);
    _assistantNode = null;
  }

  // ── 发送 ─────────────────────────────────────────────────────────────────
  function sendText(text) {
    text = (text || "").trim();
    if (!text) { resumeListeningIfActive(); return; }
    state.lastUserText = text;
    addBubble("you", text);
    if (!wsSend("chat.send", { session_id: state.sessionId, text })) {
      addBubble("ai", "⚠️ 未连接到服务器，消息未发送");
      resumeListeningIfActive();
      return;
    }
    setPhase("thinking");
  }

  // ── 语音识别 ─────────────────────────────────────────────────────────────
  function buildRecognizer() {
    const r = new SR();
    r.lang = "zh-CN";
    r.continuous = true;       // 连续模式
    r.interimResults = true;   // 要中间结果（实时字幕 + 静音判定）

    r.onresult = (e) => {
      let interim = "";
      let finalText = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const res = e.results[i];
        if (res.isFinal) finalText += res[0].transcript;
        else interim += res[0].transcript;
      }
      // 实时字幕
      if (interim) {
        if (!state.interimNode) state.interimNode = addBubble("you", "", true);
        updateBubble(state.interimNode, interim);
      }
      // 一句定稿 -> 停止识别并发送
      if (finalText.trim()) {
        if (state.interimNode) { state.interimNode.remove(); state.interimNode = null; }
        stopRecognition();       // 关键：发送前先停麦，进入 thinking
        sendText(finalText);
      }
    };

    r.onerror = (e) => {
      // no-speech / aborted 属正常，忽略；其它错误提示
      if (e.error === "not-allowed" || e.error === "service-not-allowed") {
        // 非安全上下文（HTTP）下浏览器同样抛 not-allowed —— 优先提示真正原因
        const hint = secureOk
          ? "麦克风权限被拒绝，请在浏览器设置中允许"
          : "需要 HTTPS 才能使用麦克风（当前是 HTTP）";
        setPhase("error", hint);
        state.active = false;
      } else if (e.error === "no-speech") {
        // 长时间没说话，continuous 下浏览器可能 end，onend 里会重启
      }
    };

    r.onend = () => {
      // continuous 模式下浏览器可能自动结束；只有处于 listening 且仍 active 时才自动重启
      if (state.active && state.phase === "listening") {
        try { r.start(); } catch (_) { /* 已在运行 */ }
      }
    };
    return r;
  }

  function startRecognition() {
    if (!SR) return;
    if (!state.recog) state.recog = buildRecognizer();
    setPhase("listening");
    if (state.interimNode) { state.interimNode.remove(); state.interimNode = null; }
    try { state.recog.start(); } catch (_) { /* 已在运行，忽略 */ }
  }

  function stopRecognition() {
    if (state.recog) {
      try { state.recog.stop(); } catch (_) {}
    }
  }

  function resumeListeningIfActive() {
    if (state.active && !state.speaking) {
      startRecognition();
    } else if (!state.active) {
      setPhase("idle");
    }
  }

  // ── 语音合成（TTS）───────────────────────────────────────────────────────
  // 边收边读：从 assistantText 中切出「以句末标点结尾」的完整片段去朗读，
  // 减少等到整段结束才开口的延迟。flush=true 时把剩余文本也读掉。
  const SENTENCE_END = /[。！？!?；;\n]/;
  function speakReadyChunks(flush) {
    let rest = state.assistantText.slice(state.spokenLen);
    if (!rest) return;
    if (flush) {
      const tail = rest.trim();
      if (tail) enqueueSpeak(tail);
      state.spokenLen = state.assistantText.length;
      return;
    }
    // 找到最后一个句末标点，把之前的部分整体送读
    let lastEnd = -1;
    for (let i = rest.length - 1; i >= 0; i--) {
      if (SENTENCE_END.test(rest[i])) { lastEnd = i; break; }
    }
    if (lastEnd >= 0) {
      const chunk = rest.slice(0, lastEnd + 1).trim();
      if (chunk) enqueueSpeak(chunk);
      state.spokenLen += lastEnd + 1;
    }
  }

  function enqueueSpeak(text) {
    state.pendingSpeak.push(text);
    drainSpeak();
  }

  function drainSpeak() {
    if (state.speaking) return;
    const next = state.pendingSpeak.shift();
    if (next == null) {
      // 队列空：若本轮已结束且不再 thinking，则回到聆听
      if (state.phase !== "thinking") resumeListeningIfActive();
      return;
    }
    state.speaking = true;
    setPhase("speaking");
    stopRecognition();   // 朗读时务必停麦，防回环
    const u = new SpeechSynthesisUtterance(next);
    u.lang = "zh-CN";
    u.rate = 1.05;
    u.onend = () => { state.speaking = false; drainSpeak(); };
    u.onerror = () => { state.speaking = false; drainSpeak(); };
    try { synth.speak(u); }
    catch (_) { state.speaking = false; drainSpeak(); }
  }

  // 一次性朗读（错误提示等），带回调
  function speak(text, done) {
    stopRecognition();
    state.speaking = true;
    setPhase("speaking");
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "zh-CN";
    u.onend = () => { state.speaking = false; if (done) done(); };
    u.onerror = () => { state.speaking = false; if (done) done(); };
    synth.speak(u);
  }

  function stopAllSpeech() {
    state.pendingSpeak = [];
    state.speaking = false;
    try { synth.cancel(); } catch (_) {}
  }

  // ── 交互 ─────────────────────────────────────────────────────────────────
  micBtn.onclick = () => {
    if (!SR) return;
    if (!state.active) {
      // 开启免提循环。注意：start() 必须由用户手势触发（浏览器策略）
      state.active = true;
      // 朗读一个空串以「解锁」TTS（部分浏览器要求用户手势内首次 speak）
      try { synth.cancel(); } catch (_) {}
      startRecognition();
    } else {
      // 再次点击 = 暂停整个循环
      state.active = false;
      stopAllSpeech();
      stopRecognition();
      setPhase("idle", "已暂停，点击麦克风继续");
    }
  };

  // 朗读时点击屏幕（main 区域）打断
  document.querySelector("main").addEventListener("click", (e) => {
    if (e.target === micBtn || micBtn.contains(e.target)) return;
    if (state.speaking || state.phase === "speaking") {
      stopAllSpeech();
      resumeListeningIfActive();
    }
  });

  // ── 思考等级（跟随后端）──────────────────────────────────────────────────
  // 语义：语音端不强制写 thinking。它跟随后端的全局 runtime —— 进页面/重连
  // 由 state.updated 回填，下拉框只在用户手动选时才 push thinking.set。
  // thinking 仍是全局设置（webui / TUI / 微信 共享同一个 runtime）。
  const LEVEL_LABELS = {
    off: "关", minimal: "极简", low: "低", medium: "中",
    high: "高", xhigh: "超高", adaptive: "自适应", max: "最大",
  };
  let _optionsBuilt = false;

  // 用后端给的 thinking_options 动态生成下拉项（后端加等级会自动跟上）。
  // 只建一次，避免在用户拨弄下拉框时被 state.updated 重建打断。
  function buildThinkOptions(levels) {
    if (!thinkLevelEl || _optionsBuilt || !Array.isArray(levels) || !levels.length) return;
    thinkLevelEl.innerHTML = "";
    for (const lv of levels) {
      const opt = document.createElement("option");
      opt.value = lv;
      opt.textContent = `🧠 ${LEVEL_LABELS[lv] || lv}`;
      thinkLevelEl.appendChild(opt);
    }
    _optionsBuilt = true;
  }

  // 把后端真实等级回填到 state + 下拉框（不回写后端，纯展示同步）。
  function reflectThinking(level) {
    if (typeof level !== "string") return;
    state.thinkingLevel = level;
    if (thinkLevelEl) {
      thinkLevelEl.value = level;
      thinkLevelEl.classList.toggle("on", level !== "off");
    }
  }

  if (thinkLevelEl) {
    thinkLevelEl.onchange = () => {
      const level = thinkLevelEl.value;
      state.thinkingLevel = level;
      thinkLevelEl.classList.toggle("on", level !== "off");
      wsSend("thinking.set", { level });   // 仅用户主动选择时写后端
    };
  }

  $("stopSpeak").onclick = () => {
    stopAllSpeech();
    resumeListeningIfActive();
  };

  $("newSession").onclick = async () => {
    stopAllSpeech();
    stopRecognition();
    try {
      const res = await fetch("/api/sessions", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}),
        },
        body: "{}",
      });
      if (res.ok) {
        const data = await res.json();
        if (data.session && data.session.session_id) {
          state.sessionId = data.session.session_id;
          // 通知服务器把当前 ws 绑定到新 session
          wsSend("session.select", { session_id: state.sessionId });
        }
      }
    } catch (_) {}
    captionsEl.innerHTML = "";
    if (state.active) startRecognition(); else setPhase("idle");
  };

  // 防止页面在后台被语音锁屏：保持唤醒（可选，失败无碍）
  async function keepAwake() {
    try {
      if ("wakeLock" in navigator) await navigator.wakeLock.request("screen");
    } catch (_) {}
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") keepAwake();
  });

  // ── 启动 ─────────────────────────────────────────────────────────────────
  setPhase("idle");
  connect();
  keepAwake();
})();
