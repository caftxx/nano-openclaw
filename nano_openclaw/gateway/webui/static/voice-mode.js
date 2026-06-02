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
  // 阿里云慢启动看门狗窗口：要覆盖 getUserMedia 授权框（首次可能数秒）+ 拉 token + ws 握手 +
  // 等 TranscriptionStarted。默认 1500ms 会在授权框还没点完时就误判卡死、强杀 CONNECTING 的 ws。
  const ALIYUN_START_TIMEOUT_MS = 12000;

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
    currentTurnId: "",        // 当前语音轮的 turn_id；clearTurnState 据它判定本轮是否已结束
    cancelRequested: false,   // 已向后端发出取消的标记（随轮次重置；思考中点屏触发 turn.cancel 后置位，避免重复刷请求）
    speakThisTurn: false,     // 本轮是否允许继续播报
    captionAiNode: null,      // 当前 turn 的 AI 字幕节点
    thinkOptionsKey: "",
    voiceURI: "",             // 选中的 TTS 声音（空 = 系统默认）
    acc: null,                // 整句累积器（静音去抖合并分片 final），init() 里创建
    engine: "webspeech",      // 识别引擎："webspeech"(浏览器内置) | "aliyun"(阿里云实时识别)
    voiceCfg: null,           // /api/voice/config 结果（available/provider/appkey/endpoint）
    aliyun: null,             // 阿里云 recognizer 实例（engine==="aliyun" 时按需创建）
    aliyunRunning: false,     // 阿里云引擎当前是否在采集（替代 SR 的 recognizing/recogStarting）
    aliyunInterim: "",        // 阿里云当前未定 interim 文本：SentenceEnd 直接发，点屏立即发送时也发它
    aliyunTts: null,          // 阿里云流式合成引擎实例（useAliyunTts() 时按需创建）
    pcmPlayer: null,          // 流式 PCM 播放器（持有 Web Audio，合成音频投这里播放）
    ttsBegun: false,          // 本轮是否已对合成引擎调过 begin()（每个 turn 重置）
    ttsChoice: "",            // 选中的合成音色 value（"local"=浏览器；其余=阿里云音色），独立于识别引擎
    ttsFallback: false,       // 阿里云合成本会话内致命失败 → 回退本地 synth，直到用户手动换音色重试
  };

  // getUserMedia + AudioWorklet 是阿里云引擎的硬依赖（worklet 在音频线程里降采样转 PCM）。
  // 任一不支持就退回 Web Speech——别让选了阿里云的环境反而比内置还差。
  const aliyunEnvOk = Boolean(
    navigator.mediaDevices && navigator.mediaDevices.getUserMedia
    && window.AudioContext && window.AudioWorklet
  );

  let elOverlay, elCircle, elEmoji, elLabel, elStatus, elCaptions, elThink, elUnsupported, elVoice, elEngine, elTtsVoice;
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
    elEngine = $("voiceEngine");
    elTtsVoice = $("voiceTtsVoice");
  }

  // ── 状态展示 ──────────────────────────────────────────────────────────────
  const PHASE_UI = {
    idle:      { cls: "off",       emoji: "🎙️", label: "点击开始", status: "点击麦克风，开始连续语音对话" },
    listening: { cls: "listening", emoji: "👂", label: "正在聆听…", status: "请说话，停顿后自动发送 · 说完点屏幕立即发送" },
    thinking:  { cls: "thinking",  emoji: "🤔", label: "思考中…",   status: "已发送，等待回复… · 点屏幕停止" },
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
  const ENGINE_KEY = "nanoVoiceEngine";
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
    r.onstart = () => { if (r === v.recog) { v.recogStarting = false; v.recognizing = true; if (watchdog) watchdog.confirmed(); } };
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
  // ── 阿里云实时识别引擎 ──────────────────────────────────────────────────────
  // 不走前端去抖累积器：阿里云自带 max_sentence_silence 断句，SentenceEnd（→ onFinal）就是一句
  // 完整的话，再叠一层静音去抖只会让发送变慢。所以 onFinal 直接停麦 + 发送；onInterim 仅更新
  // 字幕并记到 v.aliyunInterim，供"点屏立即发送"取用。（Web Speech 路径仍用累积器，见 buildRecognizer。）
  function buildAliyunRecognizer() {
    const make = window.createAliyunRecognizer
      || (typeof createAliyunRecognizer !== "undefined" ? createAliyunRecognizer : null);
    if (!make) return null;
    return make({
      getConfig: () => ({ appkey: v.voiceCfg && v.voiceCfg.appkey, endpoint: v.voiceCfg && v.voiceCfg.endpoint }),
      getToken: fetchVoiceToken,
      onStart: () => { v.aliyunRunning = true; if (watchdog) watchdog.confirmed(); setPhase("listening"); },
      onInterim: (text) => {
        // 未定结果：只更新字幕并暂存，等 SentenceEnd 或点屏才发送。
        v.aliyunInterim = text || "";
        if (text) setPhase("listening", `识别中：${text}`);
      },
      onFinal: (text) => {
        // SentenceEnd：阿里云已断好句，直接停麦发送，不等去抖。
        v.aliyunInterim = "";
        const t = (text || "").trim();
        if (t) { stopRecognition(); sendVoiceText(t); }
      },
      onError: (name, msg) => {
        v.aliyunRunning = false;
        v.recogStarting = false;
        // 麦克风被拒：与 SR 路径一致——后台不报错（回前台可恢复），前台才停。
        if (name === "mic") {
          if (document.visibilityState !== "visible") return;
          setPhase("error", "麦克风权限被拒绝，请在浏览器设置中允许");
          v.active = false; v.speakThisTurn = false; v.wantListen = false;
          releaseWakeLock();
          return;
        }
        // token/config/ws 等：提示但不强制退出免提，交由续听/看门狗自愈重试。
        console.warn("[voice] aliyun error:", name, msg);
      },
      onEnd: () => {
        v.aliyunRunning = false;
        v.recogStarting = false;
        // 与 SR onend 同义：仍在聆听意图且前台 → 续听重开。
        if (v.active && v.wantListen && document.visibilityState === "visible") _doStart();
      },
    });
  }
  // 用户引擎偏好持久化：""(无偏好) | "webspeech" | "aliyun"。
  function storedEnginePref() {
    try { return localStorage.getItem(ENGINE_KEY) || ""; } catch (_) { return ""; }
  }
  // 阿里云当前是否真的可用：浏览器环境支持其硬依赖 + 后端已配置好 appkey/endpoint。
  function aliyunUsable() {
    return Boolean(
      aliyunEnvOk && v.voiceCfg && v.voiceCfg.available
      && v.voiceCfg.provider === "aliyun" && v.voiceCfg.appkey && v.voiceCfg.endpoint
    );
  }
  // 综合用户偏好 + 可用性算出 v.engine（用户显式选优先，否则走自动默认），并刷新下拉 UI。
  function applyEngineChoice() {
    const resolve = window.resolveVoiceEngine || resolveVoiceEngine;
    v.engine = resolve(storedEnginePref(), aliyunUsable());
    syncEngineSelect();
  }
  // 把下拉控件的选中值与禁用态同步到当前能力：阿里云未配置 / 本地不支持时禁用对应项。
  function syncEngineSelect() {
    if (!elEngine) return;
    elEngine.value = v.engine;
    const opts = elEngine.options;
    for (let i = 0; i < opts.length; i++) {
      const o = opts[i];
      if (o.value === "aliyun") {
        const ok = aliyunUsable();
        o.disabled = !ok;
        o.textContent = ok ? "☁️ 阿里云" : "☁️ 阿里云（未配置）";
      } else if (o.value === "webspeech") {
        o.disabled = !SR;
        o.textContent = SR ? "🎤 本地" : "🎤 本地（不支持）";
      }
    }
  }

  // 拉 /api/voice/config 探测阿里云配置，存到 v.voiceCfg，最后无论如何都重算引擎选择
  // （含刷新下拉禁用态）。仅 aliyunEnvOk 时才发请求；失败（断网/旧后端无此端点）静默忽略。
  async function selectRecognitionEngine() {
    if (aliyunEnvOk) {
      try {
        const res = await fetch("/api/voice/config", { headers: authHeaders() });
        if (res.ok) v.voiceCfg = await res.json();
      } catch (_) { /* 静默忽略：保持 v.voiceCfg=null，阿里云视为不可用 */ }
    }
    applyEngineChoice();
    resolveDefaultTtsChoice();   // 尚无音色偏好时按可用性定默认（阿里云默认音色 / 本地）
    buildTtsVoiceOptions();      // 配置到位后渲染音色下拉（含阿里云音色项 + 禁用态）
  }

  // ── 阿里云流式语音合成（TTS）──────────────────────────────────────────────
  // 朗读输出独立于识别引擎：用户在底部「音色」下拉选「本地」走浏览器 speechSynthesis，
  // 选某个阿里云音色则经 voice-tts-aliyun.js 流式合成、voice-pcm-player.js 无缝播放。
  const TTS_VOICE_KEY = "nanoTtsVoice";
  // 阿里云 TTS 在浏览器侧是否具备运行条件：依赖 Web Audio（aliyunEnvOk 已含 AudioContext）
  // + 后端报告 tts.enabled + appkey/endpoint 齐全。与识别引擎选择无关。
  function aliyunTtsUsable() {
    return Boolean(
      aliyunEnvOk && v.voiceCfg && v.voiceCfg.available
      && v.voiceCfg.tts && v.voiceCfg.tts.enabled
      && v.voiceCfg.appkey && v.voiceCfg.endpoint
    );
  }
  // 当前是否应当用阿里云合成朗读：可用 + 选了非「本地」音色。
  function useAliyunTts() {
    return aliyunTtsUsable() && !v.ttsFallback && Boolean(v.ttsChoice) && v.ttsChoice !== "local";
  }
  // 尚无音色偏好（v.ttsChoice 为空串）时定默认：阿里云 TTS 可用 → 后端默认音色，否则「本地」。
  // 已有偏好（用户选过 / localStorage 有值）则不动。
  function resolveDefaultTtsChoice() {
    if (v.ttsChoice) return;
    v.ttsChoice = aliyunTtsUsable() ? ((v.voiceCfg.tts && v.voiceCfg.tts.voice) || "local") : "local";
  }
  // 渲染底部「音色」下拉：首项「🔊 本地」(value=local) + 阿里云中文音色目录。
  // 阿里云不可用时只列「本地」；当前选中 v.ttsChoice 不在列表则回退 local。
  function buildTtsVoiceOptions() {
    if (!elTtsVoice) return;
    elTtsVoice.innerHTML = "";
    const local = document.createElement("option");
    local.value = "local";
    local.textContent = "🔊 本地";
    elTtsVoice.appendChild(local);
    const voices = (v.voiceCfg && v.voiceCfg.tts && v.voiceCfg.tts.voices) || [];
    const usable = aliyunTtsUsable();
    if (usable) {
      for (const vo of voices) {
        const o = document.createElement("option");
        o.value = vo.value;
        o.textContent = `🗣 ${vo.label}`;
        elTtsVoice.appendChild(o);
      }
    }
    // 选中态：当前 ttsChoice 在新选项里就保留；非空但不在列表（音色被禁用/下线）回退 local。
    // 空串（尚无偏好、配置未到位）不强写，留给 resolveDefaultTtsChoice 定默认，UI 先显「本地」。
    const valid = v.ttsChoice && Array.from(elTtsVoice.options).some((o) => o.value === v.ttsChoice);
    if (v.ttsChoice && !valid) v.ttsChoice = "local";
    elTtsVoice.value = v.ttsChoice || "local";
    updateBrowserVoiceVisibility();   // 同步右上角浏览器音色下拉的可见性
  }
  // 右上角浏览器音色下拉只在「本地合成」时有意义：选了生效的阿里云音色就隐藏，避免与底部「音色」下拉重复。
  function updateBrowserVoiceVisibility() {
    if (elVoice) elVoice.hidden = useAliyunTts();
  }
  // 合成音频播完（播放器 drain）后的续听时序——复刻 webspeech 读完续听逻辑：
  // turn 仍在流式 → 显示 thinking 等回复；否则冷却 ~500ms 再开麦（避外放尾音回采）。
  function onTtsDrained() {
    if (v.turnOpen) {
      if (v.active && v.speakThisTurn) setPhase("thinking", "正在接收回复…");
    } else {
      scheduleResumeListening(500);
    }
  }
  // 懒建流式 PCM 播放器：合成音频帧 enqueue 进来无缝播放，drain 时回到续听。
  function ensurePlayer() {
    if (v.pcmPlayer) return v.pcmPlayer;
    const make = window.createPcmPlayer
      || (typeof createPcmPlayer !== "undefined" ? createPcmPlayer : null);
    if (!make) return null;
    const sr = (v.voiceCfg && v.voiceCfg.tts && v.voiceCfg.tts.sample_rate) || 16000;
    v.pcmPlayer = make({
      sampleRate: sr,
      onDrained: () => { v.speaking = false; onTtsDrained(); },
      onError: (m) => console.warn("[voice] pcm", m),
    });
    return v.pcmPlayer;
  }
  // 懒建阿里云合成引擎：投递文本 → 收 PCM 帧入播放器 + 生命周期事件接状态机。
  function ensureTts() {
    if (v.aliyunTts) return v.aliyunTts;
    const make = window.createAliyunSynthesizer
      || (typeof createAliyunSynthesizer !== "undefined" ? createAliyunSynthesizer : null);
    if (!make) return null;
    v.aliyunTts = make({
      getConfig: () => ({
        appkey: v.voiceCfg && v.voiceCfg.appkey,
        endpoint: v.voiceCfg && v.voiceCfg.endpoint,
        voice: v.ttsChoice,
        sampleRate: (v.voiceCfg && v.voiceCfg.tts && v.voiceCfg.tts.sample_rate) || 16000,
      }),
      getToken: fetchVoiceToken,
      onAudio: (buf) => { ensurePlayer(); if (v.pcmPlayer) v.pcmPlayer.enqueue(buf); },
      onStart: () => { v.speaking = true; setPhase("speaking"); stopRecognition(); },
      onComplete: () => {
        // SynthesisCompleted：音频全部下发完，告知播放器可在播完后 drain → 续听。
        if (v.pcmPlayer) v.pcmPlayer.markEnded();
        else { v.speaking = false; onTtsDrained(); }   // 没产生任何音频：无 drain 可等，直接续听
      },
      onError: (name, msg) => {
        console.warn("[voice] tts", name, msg);
        // 本会话内回退本地 synth：阿里云合成致命失败大概率会复发，别每轮都卡一次。
        // 用户手动换音色（elTtsVoice.onchange）会重置 ttsFallback 再试阿里云。
        v.ttsFallback = true;
        updateBrowserVoiceVisibility();   // 回退本地后应重新显示右上角浏览器音色下拉
        // 本轮别卡在 speaking，按「读完」恢复续听（播放器若有在播则等其 drain）。
        if (v.pcmPlayer) v.pcmPlayer.markEnded();
        else { v.speaking = false; onTtsDrained(); }
      },
    });
    return v.aliyunTts;
  }

  // 带 Bearer 取临时 token（命中后端缓存）。失败抛错由引擎 onError("token") 接住。
  async function fetchVoiceToken() {
    const res = await fetch("/api/voice/token", { headers: authHeaders() });
    if (!res.ok) throw new Error(`token ${res.status}`);
    return res.json();
  }

  function _doStartAliyun() {
    // 抗重入：续听重连 / 看门狗重建可能在上一次启动还没确认时再次进来 → 别叠两条 ws。
    // forceRecognizerRebuild 调本函数前已把 recogStarting/aliyunRunning 置 false，不会被此守卫挡住。
    if (v.recogStarting || v.aliyunRunning) return;
    if (!v.aliyun) v.aliyun = buildAliyunRecognizer();
    if (!v.aliyun) return;
    v.recogStarting = true;
    // 阿里云慢启动专用更长兜底窗口（含授权框+网络+握手）；SR 路径仍用默认。
    if (watchdog) watchdog.arm(ALIYUN_START_TIMEOUT_MS);
    setPhase("listening");
    // start 是 async（取 token + 开麦 + 连 ws）；onStart/onError/onEnd 回调里翻转运行态。
    Promise.resolve(v.aliyun.start()).catch((err) => {
      v.recogStarting = false;
      console.warn("[voice] aliyun start failed:", err && err.message);
    });
  }

  function _doStart() {
    if (v.engine === "aliyun") { _doStartAliyun(); return; }
    if (!SR) return;
    if (!v.recog) v.recog = buildRecognizer();
    v.recogStarting = true;     // 进入"已 start、待 onstart"中间态
    if (watchdog) watchdog.arm();   // 兜底：onstart 没在限期内确认（卡死/start 抛错）→ 强制重建重开
    try { v.recog.start(); setPhase("listening"); }
    catch (err) {
      v.recogStarting = false;
      // InvalidStateError / 权限临界 / recognizer 未完全停 等都会落到这里。
      // 旧实现空吞导致 UI 仍显示"聆听"但实际没在识别——这里至少暴露出来。
      // watchdog 已挂起：到点会强制重建重开，不让 start() 抛错把免提卡死。
      console.warn("[voice] recog.start failed:", err && err.name, err && err.message);
    }
  }
  // 当前引擎是否处于"在跑/正在启动"——两种引擎的运行态字段不同，统一在此判断。
  function recogBusy() {
    if (v.engine === "aliyun") return v.aliyunRunning || v.recogStarting;
    return v.recognizing || v.recogStarting;
  }
  function startRecognition() {
    if (v.engine === "webspeech" && !SR) return;
    clearResumeTimer();
    v.wantListen = true;
    // 上一段识别还在跑或正在启动时别抢跑（SR 重复 start 抛 InvalidStateError；阿里云会建第二条 ws）。
    // 交给 onstart/onend(SR) 或 onStart/onEnd(阿里云) 接力；arm 看门狗兜底卡死场景。
    if (recogBusy()) { if (watchdog) watchdog.arm(); return; }
    _doStart();
  }
  function stopRecognition() {
    clearResumeTimer();
    if (watchdog) watchdog.clear();   // 主动停麦：撤销聆听兜底，避免误重建
    v.wantListen = false;
    // 主动停麦（朗读/暂停/切后台/发送等）时清掉未完成的半句，避免误发。
    // onFlush 内部会调本函数，但那时 buffer 已被 flush 清空，reset 无副作用。
    if (v.acc) v.acc.reset();
    if (v.engine === "aliyun") {
      v.aliyunInterim = "";   // 清未定半句，避免残留 interim 被点屏误发（与上面 acc.reset 同理）
      // abort 关 ws + 停麦 + 关 worklet；onEnd 里若 wantListen 仍为 true 会续听，但这里已置 false。
      if (v.aliyun) { try { v.aliyun.abort(); } catch (_) {} }
      v.aliyunRunning = false;
      v.recogStarting = false;
      return;
    }
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
    // 阿里云引擎：abort() 已彻底拆掉 ws/麦克风/worklet，没有"等旧对象 onend"的跨对象竞态，
    // 直接 stop + 重建即可（每次 start 都新建 ws/AudioContext）。
    if (v.engine === "aliyun") {
      stopRecognition();
      v.aliyun = null;             // 丢弃旧实例，下次 _doStart 建新的
      finishRecognizerReset();
      return;
    }
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
    v.aliyunRunning = false;
    if (document.visibilityState === "visible" && v.open && v.active && !v.speaking && v.phase !== "thinking") {
      startRecognition();
    }
  }
  // ── 聆听看门狗：纯前台卡死的自愈兜底 ──────────────────────────────────────
  // 基于"意图 + 真实运行态"判断是否应当聆听——刻意不看 v.phase（卡死时 phase 文案不可信，
  // 比如停在"已停止朗读，等待回复结束…"但 turn 其实早已结束）。
  let watchdog = null;
  function shouldListen() {
    return v.open && v.active && v.wantListen && !v.speaking
      && !v.pendingReset && !hasOpenTurn()
      && document.visibilityState === "visible";
  }
  // 丢弃卡死的 recognizer，建一个干净的重新起——看门狗到点时调用，绕过那个可能永不来的 onend。
  function forceRecognizerRebuild() {
    if (resetTimer) { clearTimeout(resetTimer); resetTimer = null; }
    v.pendingReset = false;
    if (v.engine === "aliyun") {
      // 先摘除再 abort：abort 内部会同步触发 onEnd，若此时 v.aliyun 还指向旧实例，
      // onEnd 的续听分支会递归复用它再 start() → 与下方 _doStart 叠成双启动。
      const old = v.aliyun;
      v.aliyun = null;             // 下次 _doStart 建新的
      if (old) { try { old.abort(); } catch (_) {} }
    } else if (v.recog) {
      try { v.recog.abort(); } catch (_) {}
    }
    v.recog = null;                // 下次 _doStart 会建新的
    v.recognizing = false;
    v.recogStarting = false;
    v.aliyunRunning = false;
    if (shouldListen()) _doStart();
  }

  function hasOpenTurn() {
    return v.turnOpen || Boolean(state.activeTurnId || (state.currentSession && state.currentSession.active_turn_id));
  }
  function clearTurnState(turnId, keepSpeech) {
    if (!turnId || !v.currentTurnId || turnId === v.currentTurnId) v.currentTurnId = "";
    v.cancelRequested = false;
    v.turnOpen = false;
    if (!keepSpeech) v.speakThisTurn = false;
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
    // 用户手势内建好并 resume 阿里云 TTS 的 AudioContext：移动端 Chrome 只认手势内的
    // resume，否则首帧音频到达时才懒建会停在 suspended（不出声 + source 不 onended 卡死）。
    if (aliyunTtsUsable()) { ensurePlayer(); if (v.pcmPlayer) { try { v.pcmPlayer.unlock(); } catch (_) {} } }
    requestWakeLock();                     // 进免提即保持屏幕常亮
    if (hasOpenTurn()) {
      setPhase("thinking", "等待当前回复结束…");
      return;
    }
    startRecognition();
  }

  // 手动"立即发送"：用户说完点屏，不等静音去抖。无累积文本则什么都不做（避免点空屏发空消息）。
  function flushPendingNow() {
    if (!v.active || v.speaking) return;   // 阿里云路径不依赖 acc，故不再因 !v.acc 直接 return
    if (v.engine === "aliyun") {
      // 阿里云无累积器：发当前未定 interim（SentenceEnd 已会自己发，这里只兜未断句的尾巴）。
      const t = (v.aliyunInterim || "").trim();
      if (!t) return;
      v.aliyunInterim = "";
      stopRecognition();
      sendVoiceText(t);
      return;
    }
    // Web Speech：仍走累积器，flushNow 内部 onFlush → stopRecognition + sendVoiceText。
    if (!v.acc || !v.acc.flushNow()) return;   // 没有任何待发文本：忽略这次点击
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
  function enqueueSpeak(text) {
    // 选了阿里云音色：走流式合成（首段开一条流，后续 push 续投）。ensureTts 拿不到引擎
    // （脚本没加载好）时回退 webspeech，别把朗读丢了。
    if (useAliyunTts()) {
      const tts = ensureTts();
      if (tts) {
        if (!v.ttsBegun) { v.ttsBegun = true; tts.begin(); }
        tts.push(text);
        v.speaking = true;
        setPhase("speaking");
        stopRecognition();   // 朗读时停麦，防回环
        return;
      }
    }
    v.pendingSpeak.push(text);
    drainSpeak();
  }
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
  // 短播报（如出错时读「出错了」）一律走浏览器 synth：单句、无需流式，避免为一句话
  // 也起一条阿里云 ws；与选中的合成音色无关。
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
    // 先拆阿里云合成链路（中止 ws + 停播放器 + 重置 begin 标志），再走浏览器 synth。
    // 播放器 stop() 只停当前在播音源、保留并复用 ctx：ctx 在用户手势里已 unlock，置空重建
    // 会丢失解锁状态（移动端重建出的 ctx 是 suspended）→ 不出声 + source 不 onended → 卡死。
    if (v.aliyunTts) { try { v.aliyunTts.abort(); } catch (_) {} }
    if (v.pcmPlayer) { try { v.pcmPlayer.stop(); } catch (_) {} }
    v.ttsBegun = false;
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

  // 思考中（已发送、等后端回复）点屏：向后端发 turn.cancel 取消当前回复。
  // 取代被移除的底部「停止」按钮；已发过取消则只更新提示，避免重复刷请求。
  function cancelCurrentTurn() {
    const turnId = state.activeTurnId
      || (state.currentSession && state.currentSession.active_turn_id)
      || v.currentTurnId || "";
    stopAllSpeech();              // 顺带停掉本轮可能已在播的朗读（含阿里云流式）
    v.speakThisTurn = false;
    if (v.cancelRequested) { setPhase("thinking", "正在停止当前回复…"); return; }
    if (!turnId) { interruptSpeechForTurn(); return; }   // 没有可取消的 turn：退回打断本地播报
    if (!send("turn.cancel", { turn_id: turnId })) {
      setPhase("error", "未连接到服务器，无法停止当前回复");
      return;
    }
    v.cancelRequested = true;
    setPhase("thinking", "正在停止当前回复…");
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
        v.ttsBegun = false;   // 新 turn：合成引擎尚未 begin，首段 enqueueSpeak 时再开一条流
        v.turnOpen = true;
        v.currentTurnId = event.turn_id || "";
        v.cancelRequested = false;
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
        clearTurnState(event.turn_id, true);
        if (v.active && v.speakThisTurn) speakReadyChunks(true);
        v.speakThisTurn = false;
        if (useAliyunTts()) {
          // 阿里云：文本已全部投完 → 发 StopSynthesis 收尾；续听交给播放器 drain → onTtsDrained。
          if (v.ttsBegun && v.aliyunTts) { try { v.aliyunTts.end(); } catch (_) {} }
          else resumeListeningIfActive();   // 本轮一字未合成：无 drain 可等，直接续听兜底
        } else if (v.pendingSpeak.length === 0 && !v.speaking) {
          // webspeech：队列空且没在读 → 出过声走冷却避尾音，否则立即开麦。
          if (v.spokenLen > 0) scheduleResumeListening(500);
          else resumeListeningIfActive();
        }
        break;
      case "turn.error":
        v.captionAiNode = null;
        clearTurnState(event.turn_id);
        stopAllSpeech();
        addBubble("ai", `⚠️ 出错：${event.message || "未知错误"}`);
        if (v.active) speakOnce("出错了", () => resumeListeningIfActive());
        else resumeListeningIfActive();
        break;
      case "turn.cancelled":
        v.captionAiNode = null;
        clearTurnState(event.turn_id);
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
    buildTtsVoiceOptions();   // 合成音色下拉（配置已到位则含阿里云音色，否则仅「本地」）
    seedCaptionsFromHistory();

    if (!secureOk) {
      elUnsupported.innerHTML = "当前通过 <b>HTTP</b> 访问，手机浏览器会禁用麦克风（即使在设置里允许也无效）。请改用 <b>HTTPS</b> 地址访问。";
      elUnsupported.hidden = false;
      setPhase("error", "需要 HTTPS 才能使用麦克风");
      if (elCircle) elCircle.disabled = true;
      return;
    }
    // 阿里云引擎不依赖浏览器 SpeechRecognition，只需 getUserMedia+AudioWorklet（aliyunEnvOk
    // 已在选引擎时校验）；只有 webspeech 引擎才要求 SR 存在。
    if (v.engine !== "aliyun" && !SR) {
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
    v.currentTurnId = "";
    v.cancelRequested = false;
    v.speakThisTurn = false;
    v.captionAiNode = null;
    v.assistantText = "";
    v.spokenLen = 0;
    stopAllSpeech();
    // 离开语音模式：释放播放器 AudioContext（stopAllSpeech 只 stop 不关 ctx，复用而已）。
    if (v.pcmPlayer) { try { v.pcmPlayer.dispose(); } catch (_) {} v.pcmPlayer = null; }
    stopRecognition();
    releaseWakeLock();
    if (elOverlay) elOverlay.hidden = true;
    document.body.classList.remove("voice-open");
    setPhase("idle");
  }

  // ── 绑定 ──────────────────────────────────────────────────────────────────
  function init() {
    grab();
    // 聆听看门狗：recognizer 卡死（onend 不回 / start 抛错）时，纯前台也能自愈重建，
    // 不再只能靠刷新页面或切前后台恢复。
    const makeWatchdog = window.createListenWatchdog || (typeof createListenWatchdog !== "undefined" ? createListenWatchdog : null);
    if (makeWatchdog) watchdog = makeWatchdog({ shouldListen, onTimeout: forceRecognizerRebuild });
    // 整句累积器：分片 final 按静音去抖合并，等待时间按累积文本长度 + interim 活动自动调整。
    const makeAcc = window.createUtteranceAccumulator || (typeof createUtteranceAccumulator !== "undefined" ? createUtteranceAccumulator : null);
    if (makeAcc) {
      v.acc = makeAcc({
        // 手机免提/开车时不应依赖点屏立即发送：宁可多等一点，也别把自然停顿误判成说完。
        // 短句 1.6s；长句、末尾仍有 interim 时按累积器分档延长，最高 3.2s。
        baseSilenceMs: 1600,
        maxSilenceMs: 3200,
        onFlush: (text) => { stopRecognition(); sendVoiceText(text); },
      });
    }
    const micBtn = $("voiceMicBtn");
    if (micBtn) micBtn.onclick = () => openOverlay(true);   // 单击进全屏免提，无其它手势

    // 合成音色偏好：读持久化（""=尚无偏好，留给 selectRecognitionEngine 拿到配置后定默认）。
    try { v.ttsChoice = localStorage.getItem(TTS_VOICE_KEY) || ""; } catch (_) {}

    // 识别引擎选路：后端 /api/voice/config 报告阿里云可用 + 浏览器支持 getUserMedia/AudioWorklet
    // → 用阿里云实时识别；否则回退浏览器内置 Web Speech（保留全部现有降级路径）。
    // 不阻塞 init：配置异步拉取，拿到再切引擎；用户在配置返回前点开浮层时仍按默认 webspeech，
    // 配置到位后下次进入即生效（getUserMedia 需用户手势，反正要等点圆才真正开麦）。
    // 拿到配置后还会定合成音色默认值并渲染「音色」下拉（见 selectRecognitionEngine 末尾）。
    selectRecognitionEngine();

    // 合成音色切换：持久化选择；切换前若正在播报先停掉（避免旧引擎/旧音色继续读）。
    if (elTtsVoice) elTtsVoice.onchange = () => {
      v.ttsChoice = elTtsVoice.value;
      try { localStorage.setItem(TTS_VOICE_KEY, v.ttsChoice); } catch (_) {}
      v.ttsFallback = false;             // 用户手动换音色：清除回退标志，重新尝试阿里云合成
      updateBrowserVoiceVisibility();    // 换回阿里云音色 → 隐藏；切回本地 → 显示
      if (v.speaking) stopAllSpeech();   // 正在播报中切换：立即停，新音色下轮生效
    };

    // 引擎手动切换：持久化用户选择，停掉旧引擎当前识别、丢弃两种实例，重算 v.engine；
    // 正在免提聆听时用新引擎立即重开，否则下次点麦生效。
    if (elEngine) elEngine.onchange = () => {
      try { localStorage.setItem(ENGINE_KEY, elEngine.value); } catch (_) {}
      const wasListening = v.active && v.wantListen;   // 切换前捕获，stop 会清掉 wantListen
      if (recogBusy()) stopRecognition();              // 用旧 engine 停掉当前识别
      v.aliyun = null; v.recog = null;                 // 两种实例都丢弃，避免切换后残留
      applyEngineChoice();                             // 重算 v.engine + 刷新下拉
      if (wasListening && document.visibilityState === "visible") startRecognition();
    };

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

    if (elThink) elThink.onchange = () => {
      const lvl = elThink.value;
      state.thinkingLevel = lvl;
      if (lvl !== "off") state.lastThinkingLevel = lvl;
      reflectThinking(lvl);
      send("thinking.set", { level: lvl });
      if (typeof renderThinkingToggle === "function") renderThinkingToggle();  // 同步聊天页的开关
    };

    // 点圆/字幕/底栏以外的空白区（圆周围）：手势意图由纯函数 resolveTapAction 解析。
    //   朗读中 → 打断本地播报；思考中 → 取消后端回复；聆听中 → 立即发送（不等去抖）。
    if (elOverlay) elOverlay.addEventListener("click", (e) => {
      if (e.target.closest(".voice-circle, .voice-footer, .voice-stage-head, .voice-captions")) return;
      // 点屏是用户手势：顺手解锁播放器 ctx，兜底首次未在点麦手势里解锁的情况。
      if (v.pcmPlayer) { try { v.pcmPlayer.unlock(); } catch (_) {} }
      const resolve = window.resolveTapAction || resolveTapAction;
      switch (resolve(v.phase, v.speaking)) {
        case "interrupt": interruptSpeechForTurn(); break;
        case "cancel": cancelCurrentTurn(); break;
        case "flush": flushPendingNow(); break;
      }
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
