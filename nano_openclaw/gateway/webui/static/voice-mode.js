/* nano-openclaw 语音模式 —— 挂在聊天 WebUI（app.js）之上的全屏免提层。
 *
 * 复用 app.js 的全局：ws 连接 / send / state(session·runtime·thinking) /
 * renderMarkdown / extractText / formatAcceptedUserText / renderThinkingToggle。
 * 本模块只负责：全屏浮层 UI、语音识别(输入)、语音合成(输出)、连续免提状态机。
 *
 * 链路：SpeechRecognition 转文字 -> send("chat.send") -> 收 text.delta 累积
 *       -> speechSynthesis 朗读 -> turn.done 后回到聆听。
 *
 * app.js 在 handleEvent 末尾调用 window.VoiceMode.onEvent(event) 把事件喂进来，
 * 所以字幕/朗读/状态机和聊天流跑在同一份 ws + 同一个 session 上。
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const synth = window.speechSynthesis;

  // secure context：手机非 HTTPS（且非 localhost）下浏览器直接禁麦克风
  const isLocalHost = /^(localhost|127\.|0\.0\.0\.0$|\[?::1\]?$)/.test(location.hostname);
  const secureOk = window.isSecureContext || isLocalHost;

  const LEVEL_LABELS = {
    off: "关", minimal: "极简", low: "低", medium: "中",
    high: "高", xhigh: "超高", adaptive: "自适应", max: "最大",
  };
  const SENTENCE_END = /[。！？!?；;\n]/;

  const v = {
    open: false,
    active: false,            // 免提循环是否开启
    phase: "idle",
    recog: null,
    assistantText: "",        // 当前 turn 累积的回复（供 TTS）
    spokenLen: 0,             // 已送朗读的字符数
    pendingSpeak: [],
    speaking: false,
    recognizing: false,       // recognizer 是否真的在跑（onstart→true / onend→false）
    recogStarting: false,     // 已调 start() 但 onstart 还没回的中间态——堵 onstart 前的二次 start 竞态
    pendingReset: false,      // visibilitychange 触发的"等旧 recognizer onend 后再重建"挂起标志
    wantListen: false,        // 是否处于"应当聆听"意图：驱动 onend 续听、并防止抢跑重复 start
    turnOpen: false,          // 当前回复是否还在流式进行
    speakThisTurn: false,     // 本轮是否允许继续播报
    captionAiNode: null,      // 当前 turn 的 AI 字幕节点
    thinkOptionsKey: "",
    voiceURI: "",             // 选中的 TTS 声音（空 = 系统默认）
    acc: null,                // 整句累积器（静音去抖合并分片 final），init() 里创建
  };

  let elOverlay, elCircle, elEmoji, elLabel, elStatus, elCaptions, elThink, elUnsupported, elVoice;
  function grab() {
    elOverlay = $("voiceOverlay");
    elCircle = $("voiceCircle");
    elEmoji = elCircle && elCircle.querySelector(".voice-emoji");
    elLabel = elCircle && elCircle.querySelector(".voice-circle-label");
    elStatus = $("voiceStatus");
    elCaptions = $("voiceCaptions");
    elThink = $("voiceThinkLevel");
    elUnsupported = $("voiceUnsupported");
    elVoice = $("voiceVoice");
  }

  // ── 状态展示 ──────────────────────────────────────────────────────────────
  const PHASE_UI = {
    idle:      { cls: "off",       emoji: "🎙️", label: "点击开始", status: "点击麦克风，开始连续语音对话" },
    listening: { cls: "listening", emoji: "👂", label: "正在聆听…", status: "请说话，停顿后自动发送" },
    thinking:  { cls: "thinking",  emoji: "🤔", label: "思考中…",   status: "已发送，等待回复…" },
    speaking:  { cls: "speaking",  emoji: "🔊", label: "朗读中…",   status: "点屏幕任意处可打断" },
    error:     { cls: "error",     emoji: "⚠️", label: "出错",      status: "" },
  };
  function setPhase(phase, statusOverride) {
    v.phase = phase;
    const ui = PHASE_UI[phase] || PHASE_UI.idle;
    if (elCircle) elCircle.className = `voice-circle ${ui.cls}`;
    if (elEmoji) elEmoji.textContent = ui.emoji;
    if (elLabel) elLabel.textContent = ui.label;
    if (elStatus) elStatus.textContent = statusOverride != null ? statusOverride : ui.status;
  }

  // ── 字幕（镜像会话，用 app.js 的 renderMarkdown 保持渲染一致）──────────────
  function addBubble(role, text, interim) {
    const div = document.createElement("div");
    div.className = `vbubble ${role === "you" ? "you" : "ai"}${interim ? " interim" : ""}`;
    if (role === "ai" && typeof renderMarkdown === "function" && text) div.innerHTML = renderMarkdown(text);
    else div.textContent = text || "";
    elCaptions.appendChild(div);
    elCaptions.scrollTop = elCaptions.scrollHeight;
    return div;
  }
  function setAiBubble(node, text) {
    if (!node) return;
    if (typeof renderMarkdown === "function") node.innerHTML = renderMarkdown(text || "");
    else node.textContent = text || "";
    elCaptions.scrollTop = elCaptions.scrollHeight;
  }
  function seedCaptionsFromHistory() {
    elCaptions.innerHTML = "";
    v.captionAiNode = null;
    const hist = (state.currentSession && state.currentSession.history) || [];
    for (const msg of hist.slice(-8)) {
      const text = (typeof extractText === "function" ? extractText(msg) : "").trim();
      if (text) addBubble(msg.role === "user" ? "you" : "ai", text);
    }
  }

  // ── 思考等级下拉（跟随后端，仅用户操作时下发）──────────────────────────────
  function buildThinkOptions(levels) {
    if (!elThink || !Array.isArray(levels) || !levels.length) return;
    const key = levels.join("\n");
    if (key === v.thinkOptionsKey) return;
    elThink.innerHTML = "";
    for (const lv of levels) {
      const o = document.createElement("option");
      o.value = lv;
      o.textContent = `🧠 ${LEVEL_LABELS[lv] || lv}`;
      elThink.appendChild(o);
    }
    v.thinkOptionsKey = key;
  }
  function reflectThinking(level) {
    if (typeof level !== "string" || !elThink) return;
    elThink.value = level;
    elThink.classList.toggle("on", level !== "off");
  }

  // ── 播报声音（TTS voice）──────────────────────────────────────────────────
  // 声音由系统/浏览器提供（getVoices）；优先列中文声音，没有则全列。
  const VOICE_KEY = "nanoVoiceURI";
  function allVoices() { try { return synth.getVoices() || []; } catch (_) { return []; } }
  function buildVoiceOptions() {
    if (!elVoice) return;
    const voices = allVoices();
    if (!voices.length) return;        // 还没加载好，等 voiceschanged 再来
    const zh = voices.filter((vo) => /^zh/i.test(vo.lang));
    const list = zh.length ? zh : voices;
    elVoice.innerHTML = "";
    const def = document.createElement("option");
    def.value = "";
    def.textContent = "🔊 系统默认";
    elVoice.appendChild(def);
    for (const vo of list) {
      const o = document.createElement("option");
      o.value = vo.voiceURI;
      o.textContent = `🔊 ${vo.name}`;
      elVoice.appendChild(o);
    }
    // 恢复已选（不在列表则回退默认）
    elVoice.value = list.some((x) => x.voiceURI === v.voiceURI) ? v.voiceURI : "";
    v.voiceURI = elVoice.value;
  }
  function getSelectedVoice() {
    if (!v.voiceURI) return null;
    return allVoices().find((x) => x.voiceURI === v.voiceURI) || null;
  }
  function applyVoice(u) {
    const vo = getSelectedVoice();
    if (vo) { u.voice = vo; u.lang = vo.lang; }
    else u.lang = "zh-CN";
  }

  // ── 语音识别 ──────────────────────────────────────────────────────────────
  function buildRecognizer() {
    const r = new SR();
    r.lang = "zh-CN";
    r.continuous = true;
    r.interimResults = true;
    r.onstart = () => { if (r === v.recog) { v.recogStarting = false; v.recognizing = true; } };
    r.onresult = (e) => {
      let interim = "", finalText = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const res = e.results[i];
        if (res.isFinal) finalText += res[0].transcript;
        else interim += res[0].transcript;
      }
      // 不再"拿到首个 final 即停麦发送"（会把长句在停顿处截断）：累积分片 final，
      // 任何语音活动重置静音计时器，持续静音后由 acc.onFlush 统一停麦+发送。
      if (v.acc) {
        const shown = v.acc.feed(finalText, interim);
        if (shown) setPhase("listening", `识别中：${shown}`);
      } else {
        // 累积器不可用（voice-utterance.js 没加载成功）：退回旧的"final 即停麦发送"行为，
        // 否则识别得到文本却永远发不出去。长句可能被截断，但能用 > 不能用。
        if (interim) setPhase("listening", `识别中：${interim}`);
        if (finalText.trim()) { stopRecognition(); sendVoiceText(finalText.trim()); }
      }
    };
    r.onerror = (e) => {
      if (e.error === "not-allowed" || e.error === "service-not-allowed") {
        // 切到后台/锁屏时浏览器会临时拒麦——别据此关掉免提，回前台能恢复。
        // 只有在前台被真正拒绝才报错并停。
        if (document.visibilityState !== "visible") return;
        setPhase("error", secureOk ? "麦克风权限被拒绝，请在浏览器设置中允许" : "需要 HTTPS 才能使用麦克风（当前是 HTTP）");
        v.active = false;
        v.speakThisTurn = false;
        // 立即清聆听意图/运行态，别等 onend——权限被拒正是浏览器最易丢 onend 的脆弱边角。
        v.wantListen = false;
        v.recognizing = false;
        v.recogStarting = false;
        releaseWakeLock();
      }
    };
    r.onend = () => {
      if (r !== v.recog) return;   // 已被 visibilitychange 重建替换的旧对象，其回调一律忽略
      v.recognizing = false;
      v.recogStarting = false;
      if (v.pendingReset) { finishRecognizerReset(); return; }   // 旧对象已干净结束 → 在此重建并开麦
      // 仅在前台、且仍处于聆听意图时续听：
      //   - 静音超时自然结束 → wantListen 仍为 true → 接力重开（续命）
      //   - 主动 stop/abort（朗读/发送/暂停）→ wantListen 已置 false → 不重启
      // 后台续听会触发 not-allowed，反而把免提弄死，故要求 visible。
      if (v.active && v.wantListen && document.visibilityState === "visible") _doStart();
    };
    return r;
  }
  function _doStart() {
    if (!SR) return;
    if (!v.recog) v.recog = buildRecognizer();
    v.recogStarting = true;     // 进入"已 start、待 onstart"中间态
    try { v.recog.start(); setPhase("listening"); }
    catch (err) {
      v.recogStarting = false;
      // InvalidStateError / 权限临界 / recognizer 未完全停 等都会落到这里。
      // 旧实现空吞导致 UI 仍显示"聆听"但实际没在识别——这里至少暴露出来。
      console.warn("[voice] recog.start failed:", err && err.name, err && err.message);
    }
  }
  function startRecognition() {
    if (!SR) return;
    clearResumeTimer();
    v.wantListen = true;
    // 上一段识别还在跑或正在启动（start 已发但 onstart 未回 / abort 的 onend 未回）时别抢跑：
    // 对同一对象重复 start 会抛 InvalidStateError → UI 假"聆听"。交给 onstart/onend 接力。
    if (v.recognizing || v.recogStarting) return;
    _doStart();
  }
  function stopRecognition() {
    clearResumeTimer();
    v.wantListen = false;
    // 主动停麦（朗读/暂停/切后台/发送等）时清掉未完成的半句，避免误发。
    // onFlush 内部会调本函数，但那时 buffer 已被 flush 清空，reset 无副作用。
    if (v.acc) v.acc.reset();
    // abort() 立即终止并丢弃挂起结果，比 stop()（要等末尾 final、异步收尾更久）更干净，
    // 能压缩"上一段还没真正结束就要重开"的竞态窗口。
    if (v.recog) { try { v.recog.abort(); } catch (_) {} }
  }
  let resumeTimer = null;
  function clearResumeTimer() {
    if (resumeTimer) { clearTimeout(resumeTimer); resumeTimer = null; }
  }
  // TTS 读完后延迟一小段再开麦：speechSynthesis 的 onend 只代表"播报结束"，
  // 不代表声学环境已安静——手机外放的尾音/支架反射仍会被麦克风回采、污染识别。
  function scheduleResumeListening(delay) {
    clearResumeTimer();
    resumeTimer = setTimeout(() => { resumeTimer = null; resumeListeningIfActive(); }, delay);
  }
  // 切前后台回来：旧 recognizer 可能已卡死。优先等它 onend 后再"干净重建"（零跨对象
  // 竞态——避免新对象在浏览器语音服务还没拆完时就 start）；onend 迟迟不来（卡死正是
  // 本场景主因）则超时强制丢弃重建作兜底。
  let resetTimer = null;
  function scheduleRecognizerReset() {
    const old = v.recog;
    stopRecognition();             // abort 旧对象，wantListen=false
    if (!old) { finishRecognizerReset(); return; }   // 没有旧对象可等，直接重建
    v.pendingReset = true;
    if (resetTimer) clearTimeout(resetTimer);
    resetTimer = setTimeout(() => { if (v.pendingReset) finishRecognizerReset(); }, 800);
  }
  function finishRecognizerReset() {
    if (resetTimer) { clearTimeout(resetTimer); resetTimer = null; }
    v.pendingReset = false;
    v.recog = null;                // 丢弃旧对象，下次 _doStart 会建新的
    v.recognizing = false;
    v.recogStarting = false;
    if (document.visibilityState === "visible" && v.open && v.active && !v.speaking && v.phase !== "thinking") {
      startRecognition();
    }
  }
  function hasOpenTurn() {
    return v.turnOpen || Boolean(state.activeTurnId || (state.currentSession && state.currentSession.active_turn_id));
  }
  function resumeListeningIfActive() {
    if (v.active && !v.speaking && hasOpenTurn()) setPhase("thinking", "等待当前回复结束…");
    else if (v.active && !v.speaking) startRecognition();
    else if (!v.active) setPhase("idle");
  }

  // ── 屏幕常亮（Wake Lock）─────────────────────────────────────────────────
  // 免提期间保持屏幕常亮，挡住"无操作自动锁屏"——开车场景的主要杀手。
  // 注意：手动息屏 / 切到别的 App 仍无解（浏览器禁止后台采集麦克风）。
  let wakeLock = null;
  async function requestWakeLock() {
    if (!("wakeLock" in navigator)) return;
    try {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeLock.addEventListener("release", () => { wakeLock = null; });
    } catch (_) { wakeLock = null; }   // 被拒/不支持，忽略
  }
  function releaseWakeLock() {
    try { if (wakeLock) wakeLock.release(); } catch (_) {}
    wakeLock = null;
  }

  function startLoop() {
    v.active = true;
    try { synth.cancel(); } catch (_) {}   // 用户手势内先解锁 TTS
    requestWakeLock();                     // 进免提即保持屏幕常亮
    if (hasOpenTurn()) {
      setPhase("thinking", "等待当前回复结束…");
      return;
    }
    startRecognition();
  }

  function sendVoiceText(text) {
    if (!text) { resumeListeningIfActive(); return; }
    const sid = state.currentSession && state.currentSession.session_id;
    if (!sid) {
      v.active = false;
      releaseWakeLock();
      setPhase("error", "没有可用会话");
      return;
    }
    // 用户气泡由 chat.accepted 事件统一加（与服务器接收文本一致）
    // response_style:"voice" → 后端给本轮 system prompt 追加口语化指令
    if (!send("chat.send", { session_id: sid, text, attachments: [], response_style: "voice" })) {
      v.active = false;
      releaseWakeLock();
      setPhase("error", "未连接到服务器");
      return;
    }
    setPhase("thinking");
  }

  // ── 语音合成（边收边读）──────────────────────────────────────────────────
  function speakReadyChunks(flush) {
    const rest = v.assistantText.slice(v.spokenLen);
    if (!rest) return;
    if (flush) {
      const tail = rest.trim();
      if (tail) enqueueSpeak(tail);
      v.spokenLen = v.assistantText.length;
      return;
    }
    let lastEnd = -1;
    for (let i = rest.length - 1; i >= 0; i--) {
      if (SENTENCE_END.test(rest[i])) { lastEnd = i; break; }
    }
    if (lastEnd >= 0) {
      const chunk = rest.slice(0, lastEnd + 1).trim();
      if (chunk) enqueueSpeak(chunk);
      v.spokenLen += lastEnd + 1;
    }
  }
  function enqueueSpeak(text) { v.pendingSpeak.push(text); drainSpeak(); }
  function drainSpeak() {
    if (v.speaking) return;
    const next = v.pendingSpeak.shift();
    if (next == null) {
      if (v.turnOpen) {
        if (v.active && v.speakThisTurn) setPhase("thinking", "正在接收回复…");
      } else {
        // 本轮已读完（turn.done 后队列排空）→ 冷却 ~500ms 再开麦，避开外放尾音被回采
        scheduleResumeListening(500);
      }
      return;
    }
    v.speaking = true;
    setPhase("speaking");
    stopRecognition();   // 朗读时停麦，防回环
    const u = new SpeechSynthesisUtterance(next);
    applyVoice(u);
    u.rate = 1.05;
    u.onend = () => { v.speaking = false; drainSpeak(); };
    u.onerror = () => { v.speaking = false; drainSpeak(); };
    try { synth.speak(u); } catch (_) { v.speaking = false; drainSpeak(); }
  }
  function speakOnce(text, done) {
    stopRecognition();
    v.speaking = true;
    setPhase("speaking");
    const u = new SpeechSynthesisUtterance(text);
    applyVoice(u);
    u.onend = () => { v.speaking = false; if (done) done(); };
    u.onerror = () => { v.speaking = false; if (done) done(); };
    try { synth.speak(u); } catch (_) { v.speaking = false; if (done) done(); }
  }
  function stopAllSpeech() {
    v.pendingSpeak = [];
    v.speaking = false;
    try { synth.cancel(); } catch (_) {}
  }
  function interruptSpeechForTurn() {
    v.speakThisTurn = false;
    stopAllSpeech();
    if (v.turnOpen) {
      if (v.active) setPhase("thinking", "已停止朗读，等待回复结束…");
      else setPhase("idle", "已暂停，点击麦克风继续");
      return;
    }
    resumeListeningIfActive();
  }

  // ── 来自 app.js handleEvent 的事件 ────────────────────────────────────────
  function onEvent(event) {
    // thinking 下拉始终跟随后端（即使浮层没开，下次开时也是对的）
    if (event.type === "state.updated") {
      buildThinkOptions(state.runtime && state.runtime.thinkingOptions);
      reflectThinking(event.thinking_level != null ? event.thinking_level : state.thinkingLevel);
    }
    if (!v.open) return;
    // 只认当前 session 的 turn 事件
    if (event.session_id && state.currentSession && event.session_id !== state.currentSession.session_id) return;

    switch (event.type) {
      case "chat.accepted":
        stopAllSpeech();
        v.assistantText = ""; v.spokenLen = 0;
        v.turnOpen = true;
        v.speakThisTurn = v.active;
        addBubble("you", typeof formatAcceptedUserText === "function" ? formatAcceptedUserText(event) : (event.text || ""));
        v.captionAiNode = addBubble("ai", "");
        break;
      case "text.delta":
        v.assistantText += event.text || "";
        setAiBubble(v.captionAiNode, v.assistantText);
        if (v.active && v.speakThisTurn) speakReadyChunks(false);
        break;
      case "turn.done":
        setAiBubble(v.captionAiNode, v.assistantText);
        v.captionAiNode = null;
        v.turnOpen = false;
        if (v.active && v.speakThisTurn) speakReadyChunks(true);
        v.speakThisTurn = false;
        // 队列空且没在读：若本轮确实出过声（spokenLen>0），尾音可能仍在 → 走冷却再开麦；
        // 本轮一个字没读则立即开麦。（flush 若还有尾巴入队，drainSpeak 会接管、读完在那边冷却。）
        if (v.pendingSpeak.length === 0 && !v.speaking) {
          if (v.spokenLen > 0) scheduleResumeListening(500);
          else resumeListeningIfActive();
        }
        break;
      case "turn.error":
        v.captionAiNode = null;
        v.turnOpen = false;
        v.speakThisTurn = false;
        stopAllSpeech();
        addBubble("ai", `⚠️ 出错：${event.message || "未知错误"}`);
        if (v.active) speakOnce("出错了", () => resumeListeningIfActive());
        else resumeListeningIfActive();
        break;
      case "turn.cancelled":
        v.captionAiNode = null;
        v.turnOpen = false;
        v.speakThisTurn = false;
        stopAllSpeech();
        resumeListeningIfActive();
        break;
    }
  }

  // ── open / close ──────────────────────────────────────────────────────────
  function openOverlay(startListening) {
    if (!elOverlay) grab();
    if (!elOverlay) return;
    if (v.open) {
      if (startListening && !v.active && !(elCircle && elCircle.disabled)) startLoop();
      return;
    }
    v.open = true;
    elOverlay.hidden = false;
    document.body.classList.add("voice-open");
    buildThinkOptions(state.runtime && state.runtime.thinkingOptions);
    reflectThinking(state.thinkingLevel);
    buildVoiceOptions();
    seedCaptionsFromHistory();

    if (!secureOk) {
      elUnsupported.innerHTML = "当前通过 <b>HTTP</b> 访问，手机浏览器会禁用麦克风（即使在设置里允许也无效）。请改用 <b>HTTPS</b> 地址访问。";
      elUnsupported.hidden = false;
      setPhase("error", "需要 HTTPS 才能使用麦克风");
      if (elCircle) elCircle.disabled = true;
      return;
    }
    if (!SR) {
      elUnsupported.textContent = "当前浏览器不支持语音识别，请用 Android Chrome 打开。";
      elUnsupported.hidden = false;
      setPhase("error", "浏览器不支持语音识别");
      if (elCircle) elCircle.disabled = true;
      return;
    }
    elUnsupported.hidden = true;
    if (elCircle) elCircle.disabled = false;
    if (startListening) startLoop();
    else setPhase("idle");
  }
  function closeOverlay() {
    if (!v.open) return;
    v.open = false;
    v.active = false;
    v.turnOpen = false;
    v.speakThisTurn = false;
    v.captionAiNode = null;
    v.assistantText = "";
    v.spokenLen = 0;
    stopAllSpeech();
    stopRecognition();
    releaseWakeLock();
    if (elOverlay) elOverlay.hidden = true;
    document.body.classList.remove("voice-open");
    setPhase("idle");
  }

  // ── 绑定 ──────────────────────────────────────────────────────────────────
  function init() {
    grab();
    // 整句累积器：分片 final 按静音去抖合并，等待时间按累积文本长度 + interim 活动自动调整。
    const makeAcc = window.createUtteranceAccumulator || (typeof createUtteranceAccumulator !== "undefined" ? createUtteranceAccumulator : null);
    if (makeAcc) {
      v.acc = makeAcc({
        onFlush: (text) => { stopRecognition(); sendVoiceText(text); },
      });
    }
    const micBtn = $("voiceMicBtn");
    if (micBtn) micBtn.onclick = () => openOverlay(true);   // 单击进全屏免提，无其它手势

    // 播报声音：读持久化偏好、建选项；声音是异步加载的，加载完再重建一次
    try { v.voiceURI = localStorage.getItem(VOICE_KEY) || ""; } catch (_) {}
    buildVoiceOptions();
    if (synth && "onvoiceschanged" in synth) synth.onvoiceschanged = buildVoiceOptions;
    if (elVoice) elVoice.onchange = () => {
      v.voiceURI = elVoice.value;
      try { localStorage.setItem(VOICE_KEY, v.voiceURI); } catch (_) {}
      // 选完试听一句（仅在空闲、没在听/读真实对话时，避免被麦克风回采或打断回复）
      if (!v.speaking && v.phase === "idle") {
        const u = new SpeechSynthesisUtterance("你好，我是你的语音助手");
        applyVoice(u); u.rate = 1.05;
        try { synth.cancel(); synth.speak(u); } catch (_) {}
      }
    };

    if (elCircle) elCircle.onclick = () => {
      if (elCircle.disabled) return;
      if (!v.active) startLoop();
      else {
        v.active = false;
        v.speakThisTurn = false;
        stopAllSpeech();
        stopRecognition();
        releaseWakeLock();
        setPhase("idle", "已暂停，点击麦克风继续");
      }
    };

    const exitTop = $("voiceExitBtn");   // 左上角 ✕ 退出（底部不再放退出按钮）
    if (exitTop) exitTop.onclick = closeOverlay;

    const stopBtn = $("voiceStopSpeak");
    if (stopBtn) stopBtn.onclick = interruptSpeechForTurn;

    if (elThink) elThink.onchange = () => {
      const lvl = elThink.value;
      state.thinkingLevel = lvl;
      if (lvl !== "off") state.lastThinkingLevel = lvl;
      reflectThinking(lvl);
      send("thinking.set", { level: lvl });
      if (typeof renderThinkingToggle === "function") renderThinkingToggle();  // 同步聊天页的开关
    };

    // 朗读时点字幕区以外（圆下方空白）打断
    if (elOverlay) elOverlay.addEventListener("click", (e) => {
      if (e.target.closest(".voice-circle, .voice-footer, .voice-stage-head, .voice-captions")) return;
      if (v.speaking || v.phase === "speaking") interruptSpeechForTurn();
    });

    // 切走/锁屏再回到前台：wakeLock 被系统释放、识别已 abort 且常卡在坏状态
    // （旧 recognizer 再 start() 会抛 InvalidStateError）。所以重申请常亮，并
    // 丢掉旧 recognizer、建一个干净的重新起；留一点延迟等前台权限就绪。
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState !== "visible" || !v.open || !v.active) return;
      requestWakeLock();
      if (v.speaking || v.phase === "thinking") return;   // 正在读/等回复，别插队
      scheduleRecognizerReset();                          // 等旧 recognizer onend 后干净重建（带 800ms 兜底）
    });

    // 深链：/voice 直接进语音态（未自动聆听，需用户点圆——浏览器要求手势）
    if (location.pathname === "/voice" || location.pathname === "/voice/") {
      setTimeout(() => openOverlay(false), 300);
    }
  }

  window.VoiceMode = { onEvent, open: () => openOverlay(true), close: closeOverlay };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
