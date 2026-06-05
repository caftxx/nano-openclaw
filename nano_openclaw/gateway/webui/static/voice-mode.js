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
    aliyunTts: null,          // 阿里云流式合成引擎实例（云端合成 + 未回退时按需创建）
    restfulTts: null,         // 阿里云 RESTful 代理合成引擎实例（ttsRestfulFallback 后按需创建）
    ttsRestfulFallback: false, // 本会话内流式已证实不可用 → 改走 RESTful 代理合成（不每轮重试流式）
    pcmPlayer: null,          // 流式 PCM 播放器（持有 Web Audio，合成音频投这里播放）
    ttsBegun: false,          // 本轮是否已对合成引擎调过 begin()（每个 turn 重置）
    outMode: "",              // 语音输出引擎："local"=浏览器 | "aliyun-rest"=RESTful 代理 | "aliyun-flowing"=流式；独立于识别引擎
    aliyunVoice: "",          // 阿里云音色 value（outMode 为阿里云任一时生效；右上角「音色」下拉选）
    ttsFallback: false,       // 阿里云合成本会话内致命失败 → 回退本地 synth，直到用户手动换音色重试
    ttsTurnAudio: false,      // 本轮阿里云是否真正出过声（首帧音频到达才置位）；零发声失败时据它回退浏览器补读
    ttsConfigLoaded: false,   // /api/voice/config 是否已确定（成功/失败/断网都算）；未确定前不把存储音色误判为无效降级
    replaySpeechOnVisible: false, // 锁屏/后台期间 TTS 不可靠；回前台后重播本轮回复
    foregroundRecovery: false, // 曾切到非 visible；下次回前台做一次 Chrome 音频/识别恢复
    audioFocusGuard: null,     // 车机/Android 音频焦点保持：免提活跃时用无声 audio 防外部音乐恢复
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

  // ── 右上角「音色」(TTS voice/timbre) ───────────────────────────────────────
  // 右上角下拉是「音色」选择器，内容跟随底部「语音输出」引擎变化（见 buildTimbreOptions）：
  //   - 输出=本地 → 列系统/浏览器提供的声音（getVoices，优先中文）
  //   - 输出=阿里云/阿里云流式 → 列阿里云音色目录
  const VOICE_KEY = "nanoVoiceURI";
  const ENGINE_KEY = "nanoVoiceEngine";
  function allVoices() { try { return synth.getVoices() || []; } catch (_) { return []; } }
  // 本地模式音色：系统/浏览器声音（getVoices）；优先列中文声音，没有则全列。
  function buildSystemVoiceOptions() {
    if (!elVoice) return;
    // 先清空并放「系统默认」——即便声音还没加载，也要把可能残留的阿里云音色目录清掉，
    // 保证切到「本地」输出后右上角音色立刻同步成系统声音（不显示上一个通道的音色）。
    elVoice.innerHTML = "";
    const def = document.createElement("option");
    def.value = "";
    def.textContent = "🗣 系统默认";
    elVoice.appendChild(def);
    const voices = allVoices();
    if (!voices.length) { elVoice.value = ""; return; }   // 声音未加载：先只显「系统默认」，voiceschanged 后重建
    const zh = voices.filter((vo) => /^zh/i.test(vo.lang));
    const list = zh.length ? zh : voices;
    for (const vo of list) {
      const o = document.createElement("option");
      o.value = vo.voiceURI;
      o.textContent = `🗣 ${vo.name}`;
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
  function resumeSpeechOutput() {
    try { if (synth && typeof synth.resume === "function") synth.resume(); } catch (_) {}
    if (v.pcmPlayer) { try { v.pcmPlayer.unlock(); } catch (_) {} }
    holdAudioFocus();
  }
  function ensureAudioFocusGuard() {
    if (v.audioFocusGuard) return v.audioFocusGuard;
    const make = window.createVoiceAudioFocusGuard
      || (typeof createVoiceAudioFocusGuard !== "undefined" ? createVoiceAudioFocusGuard : null);
    if (!make) return null;
    v.audioFocusGuard = make({
      log: (name, msg) => console.warn("[voice] audio-focus", name, msg),
      // aliyun 引擎页面自采音，可叠一条占位麦克风流（同 App 内并发采集无冲突）；
      // webspeech 的识别采集在浏览器服务侧，页面持麦可能被 Android 并发采集限制
      // 静音掉识别，退回无声 audio 兜底。
      preferMicHold: () => v.engine === "aliyun",
    });
    return v.audioFocusGuard;
  }
  function holdAudioFocus() {
    // 浮层开着就持有（不要求 v.active）：打开 voice 页面外部音乐即应暂停，
    // 否则聆听会把音乐采进识别；暂停免提也不放音乐回来，✕ 退出才释放。
    if (!v.open) return;
    const guard = ensureAudioFocusGuard();
    if (guard) { try { guard.start(); } catch (_) {} }
  }
  function releaseAudioFocus(dispose) {
    const guard = v.audioFocusGuard;
    if (!guard) return;
    try { dispose ? guard.dispose() : guard.stop(); } catch (_) {}
    if (dispose) v.audioFocusGuard = null;
  }
  function deferSpeechReplay() {
    if (!(v.assistantText || "").trim()) return;
    v.replaySpeechOnVisible = true;
    // speechSynthesis 在锁屏时会取消/挂起已排队 utterance；这些文本虽已计入
    // spokenLen，但不一定真的出声。回前台从本轮开头重播，避免解锁后完全无声。
    v.spokenLen = 0;
  }
  function recoverLocalSpeechAfterForeground() {
    if (useCloudTts()) return;
    let synthBusy = false;
    try { synthBusy = Boolean(synth && (synth.speaking || synth.pending || synth.paused)); } catch (_) {}
    if (v.speaking || v.pendingSpeak.length || v.phase === "speaking" || synthBusy) {
      deferSpeechReplay();
      stopAllSpeech();
    } else {
      // Chrome Android 解锁后 speechSynthesis 可能处在 paused/stuck queue；
      // 空 cancel + resume 能清掉这类无声队列，不影响后续新 utterance。
      try { synth.cancel(); } catch (_) {}
    }
    resumeSpeechOutput();
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
        releaseAudioFocus(false);
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
          releaseAudioFocus(false);
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
        o.textContent = ok ? "🎤 阿里云" : "🎤 阿里云（未配置）";
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
    // 无论成功/失败/断网，到这里配置状态都已确定 → 之后 buildOutModeOptions 才允许据此降级输出引擎。
    v.ttsConfigLoaded = true;
    applyEngineChoice();
    resolveDefaultOutMode();      // 尚无输出引擎偏好时按可用性定默认（阿里云流式 / 本地）
    resolveDefaultAliyunVoice();  // 尚无阿里云音色偏好时定后端默认音色
    buildOutModeOptions();        // 配置到位后渲染输出引擎下拉 + 右上角音色（含禁用态）
  }

  // ── 语音输出（TTS 合成引擎 + 音色）──────────────────────────────────────────
  // 输出独立于识别引擎。底部「语音输出」下拉选引擎（本地 / 阿里云[RESTful] / 阿里云流式），
  // 右上角「音色」下拉随之列出对应音色：本地=系统声音，阿里云任一=阿里云音色目录。
  // 回退链：流式 →（运行失败）RESTful →（再失败）浏览器本地。
  const OUTMODE_KEY = "nanoTtsMode";          // 持久化「语音输出」引擎选择
  const ALIYUN_VOICE_KEY = "nanoAliyunVoice"; // 持久化阿里云音色选择
  // 语音输出引擎选项（底部右侧下拉）；🔊 前缀标识「输出」。
  const OUT_MODES = [
    { value: "local", label: "本地" },
    { value: "aliyun-rest", label: "阿里云" },
    { value: "aliyun-flowing", label: "阿里云流式" },
  ];
  // 阿里云 TTS 在浏览器侧是否具备运行条件：依赖 Web Audio（aliyunEnvOk 已含 AudioContext）
  // + 后端报告 tts.enabled + appkey/endpoint 齐全。与识别引擎选择无关。
  function aliyunTtsUsable() {
    return Boolean(
      aliyunEnvOk && v.voiceCfg && v.voiceCfg.available
      && v.voiceCfg.tts && v.voiceCfg.tts.enabled
      && v.voiceCfg.appkey && v.voiceCfg.endpoint
    );
  }
  // 当前是否选了阿里云任一输出引擎（rest / flowing）。指用户的「选择」，不含运行时回退。
  function aliyunOutSelected() {
    return v.outMode === "aliyun-rest" || v.outMode === "aliyun-flowing";
  }
  // 「当前实际生效」的输出引擎（区别于用户选择 v.outMode）：运行时回退会让生效引擎不同于所选。
  //   - 选本地 → local
  //   - 云端已彻底退本地（ttsFallback）→ local
  //   - 选阿里云流式但已回退 RESTful（ttsRestfulFallback）→ aliyun-rest
  //   - 否则 → 所选 v.outMode
  // 底部「语音输出」下拉与右上角「音色」列表都以此为准，回退后 UI 反映真实正在用的引擎。
  function effectiveOutMode() {
    if (!aliyunOutSelected()) return v.outMode || "local";
    if (v.ttsFallback) return "local";
    if (v.outMode === "aliyun-flowing" && v.ttsRestfulFallback) return "aliyun-rest";
    return v.outMode;
  }
  // 生效引擎是否为阿里云（rest / flowing）。
  function effectiveAliyun() {
    const e = effectiveOutMode();
    return e === "aliyun-rest" || e === "aliyun-flowing";
  }
  // 把底部「语音输出」下拉的显示值同步为「当前生效引擎」（回退后调用，让 UI 不骗人）。
  // 不改 v.outMode（用户的选择/偏好保持不变，下次会话仍从所选引擎起试）。
  function syncOutModeSelect() {
    if (!elTtsVoice) return;
    const eff = effectiveOutMode();
    const wantAliyun = (eff === "aliyun-rest" || eff === "aliyun-flowing");
    elTtsVoice.value = (wantAliyun && !aliyunTtsUsable()) ? "local" : eff;
  }
  // 回退发生后同步整组输出 UI：下拉显示 + 右上角音色列表都跟生效引擎走。
  function syncOutputUiToEffective() {
    syncOutModeSelect();
    buildTimbreOptions();
  }
  // 当前是否应当用阿里云云端合成朗读（而非浏览器本地）：可用 + 未致命回退本地 + 选了阿里云输出引擎。
  // 不区分流式/RESTful——两条云端路径共用此判定（停麦、turn.done 收尾等语义）。
  function useCloudTts() {
    return aliyunTtsUsable() && !v.ttsFallback && aliyunOutSelected();
  }
  // 取当前应使用的云端合成引擎实例：
  //   - 输出=阿里云(RESTful) → 恒用 RESTful；
  //   - 输出=阿里云流式 → 默认流式，运行中失败回退后（ttsRestfulFallback）转 RESTful。
  // begin/push/end 统一经此。
  function currentCloudTts() {
    if (v.outMode === "aliyun-rest") return ensureRestfulTts();
    return v.ttsRestfulFallback ? ensureRestfulTts() : ensureTts();
  }
  // 尚无输出引擎偏好（v.outMode 为空串）时定默认：阿里云可用 → 阿里云流式，否则本地。
  // 已有偏好（用户选过 / localStorage 有值）则不动。
  function resolveDefaultOutMode() {
    if (v.outMode) return;
    v.outMode = aliyunTtsUsable() ? "aliyun-flowing" : "local";
  }
  // 尚无阿里云音色偏好时定默认为后端默认音色。
  function resolveDefaultAliyunVoice() {
    if (v.aliyunVoice) return;
    v.aliyunVoice = (v.voiceCfg && v.voiceCfg.tts && v.voiceCfg.tts.voice) || "";
  }
  // 渲染底部「语音输出」下拉：本地 / 阿里云 / 阿里云流式；阿里云未配置时禁用并标注。
  function buildOutModeOptions() {
    if (!elTtsVoice) return;
    const usable = aliyunTtsUsable();
    elTtsVoice.innerHTML = "";
    for (const m of OUT_MODES) {
      const o = document.createElement("option");
      o.value = m.value;
      const isAliyun = m.value !== "local";
      o.disabled = isAliyun && !usable;
      o.textContent = `🔊 ${m.label}${(isAliyun && !usable) ? "（未配置）" : ""}`;
      elTtsVoice.appendChild(o);
    }
    // config 已确定且选了阿里云但真不可用 → 降级本地；config 未到位前不误判（一旦改了回不来）。
    if (v.ttsConfigLoaded && aliyunOutSelected() && !usable) v.outMode = "local";
    // 显示「当前生效引擎」（回退后反映实际正在用的；想用阿里云但不可用→视觉显本地），不改 v.outMode。
    syncOutModeSelect();
    buildTimbreOptions();   // 生效引擎决定右上角音色列表
  }
  // 渲染右上角「音色」下拉：跟随「当前生效引擎」——本地列系统声音，阿里云任一列音色目录。
  function buildTimbreOptions() {
    if (effectiveAliyun() && aliyunTtsUsable()) buildAliyunTimbreOptions();
    else buildSystemVoiceOptions();
  }
  // 阿里云音色目录（右上角，outMode 为阿里云时）。
  function buildAliyunTimbreOptions() {
    if (!elVoice) return;
    const voices = (v.voiceCfg && v.voiceCfg.tts && v.voiceCfg.tts.voices) || [];
    elVoice.innerHTML = "";
    for (const vo of voices) {
      const o = document.createElement("option");
      o.value = vo.value;
      o.textContent = `🗣 ${vo.label}`;
      elVoice.appendChild(o);
    }
    const inList = (val) => Array.from(elVoice.options).some((o) => o.value === val);
    // config 到位后，选中音色不在目录（未设 / 被下线）→ 取后端默认音色，再不行取首项。
    if (v.ttsConfigLoaded && voices.length && !inList(v.aliyunVoice)) {
      const def = (v.voiceCfg && v.voiceCfg.tts && v.voiceCfg.tts.voice) || voices[0].value;
      v.aliyunVoice = inList(def) ? def : voices[0].value;
    }
    elVoice.value = inList(v.aliyunVoice) ? v.aliyunVoice : (voices[0] ? voices[0].value : "");
  }
  // 阿里云合成失败回退本地时，把真实原因显示到提示横幅（手机上看不到 console）。
  // 多为 appkey 未开通「流式文本语音合成（商用版）」之类的服务端 TaskFailed。
  function showTtsFallbackNotice(msg) {
    if (!elUnsupported) return;
    elUnsupported.textContent = `阿里云语音合成失败，已回退本地音色。原因：${msg || "未知"}（换音色可重试）`;
    elUnsupported.hidden = false;
  }
  function clearTtsFallbackNotice() {
    // 只清「TTS 回退提示」；不要影响 openOverlay 设置的 HTTPS/不支持硬提示——
    // 那些场景圆按钮已禁用、不会进合成流程，故此处仅在横幅当前是 TTS 提示态时隐藏。
    if (elUnsupported && !elUnsupported.dataset.hardBlock) elUnsupported.hidden = true;
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
        voice: v.aliyunVoice,
        sampleRate: (v.voiceCfg && v.voiceCfg.tts && v.voiceCfg.tts.sample_rate) || 16000,
      }),
      getToken: fetchVoiceToken,
      onAudio: (buf) => { v.ttsTurnAudio = true; ensurePlayer(); if (v.pcmPlayer) v.pcmPlayer.enqueue(buf); },
      onStart: () => { v.speaking = true; setPhase("speaking"); stopRecognition(); clearTtsFallbackNotice(); },
      onComplete: () => {
        // SynthesisCompleted：音频全部下发完，告知播放器可在播完后 drain → 续听。
        if (v.pcmPlayer) v.pcmPlayer.markEnded();
        else { v.speaking = false; onTtsDrained(); }   // 没产生任何音频：无 drain 可等，直接续听
      },
      onError: (name, msg) => {
        console.warn("[voice] tts(flowing)", name, msg);
        // 流式致命失败 → 本会话改走 RESTful 代理合成（不再每轮先试流式失败一次）。
        // 注意：这里只置 ttsRestfulFallback（转 RESTful），不置 ttsFallback（还没退到本地）。
        // 用户改「语音输出」引擎或换音色会重置二者再试流式。仅在「阿里云流式」输出时才会走到这里
        // （RESTful 输出恒用 RESTful，不经流式引擎）。
        v.ttsRestfulFallback = true;
        clearTtsFallbackNotice();   // 转 RESTful 不弹「已回退本地」横幅（尚未退到本地）
        syncOutputUiToEffective();  // 底部「语音输出」下拉同步成「阿里云」（生效引擎已变 RESTful）
        // 本轮零发声（多为 StartSynthesis 即 TaskFailed）：把"已算作已读"但没发声的文本退回，
        // 改用 RESTful 引擎重投本轮已累积文本。turn.done 可能先于 TaskFailed 到达，此时
        // turnOpen 已被清掉、speakThisTurn 已置 false，但 spokenLen 仍记录着投给流式却未出声的文本。
        const shouldReplayZeroAudio = !v.ttsTurnAudio && v.active
          && (v.speakThisTurn || !v.turnOpen)
          && v.spokenLen > 0 && (v.assistantText || "").trim();
        if (shouldReplayZeroAudio) {
          if (v.pcmPlayer) { try { v.pcmPlayer.stop(); } catch (_) {} }
          v.speaking = false;          // 让后续投递能启动（onError 时 v.speaking 仍为 true）
          v.spokenLen = 0;             // 退回到本轮开头
          v.ttsBegun = false;          // 让下次 enqueueSpeak 重新 begin 到 RESTful 引擎
          speakReadyChunks(!v.turnOpen); // 经 currentCloudTts() 自动选到 RESTful 重投
          // turn 已结束时不会再有 turn.done 触发 end()：这里补发结束信号，让 RESTful 引擎
          // 在队列排空后 onComplete → pcmPlayer.markEnded() → drain 续听，避免卡死在「朗读中」。
          if (!v.turnOpen && v.ttsBegun) {
            const tts = currentCloudTts();
            if (tts) { try { tts.end(); } catch (_) {} }
          }
          return;
        }
        // 本轮别卡在 speaking，按「读完」恢复续听（播放器若有在播则等其 drain）。
        if (v.pcmPlayer) v.pcmPlayer.markEnded();
        else { v.speaking = false; onTtsDrained(); }
      },
    });
    return v.aliyunTts;
  }

  // 懒建阿里云 RESTful 代理合成引擎：onAudio/onStart/onComplete 接线与 ensureTts() 相同；
  // onError 是回退链末端 → 退到浏览器本地 synth（置 ttsFallback + 弹横幅 + 零发声补读）。
  function ensureRestfulTts() {
    if (v.restfulTts) return v.restfulTts;
    const make = window.createRestfulSynthesizer
      || (typeof createRestfulSynthesizer !== "undefined" ? createRestfulSynthesizer : null);
    if (!make) return null;
    v.restfulTts = make({
      url: "/api/voice/tts",
      headers: authHeaders(),   // 鉴权 header 本会话稳定，建引擎时取快照即可
      getConfig: () => ({
        voice: v.aliyunVoice,
        sampleRate: (v.voiceCfg && v.voiceCfg.tts && v.voiceCfg.tts.sample_rate) || 16000,
      }),
      onAudio: (buf) => { v.ttsTurnAudio = true; ensurePlayer(); if (v.pcmPlayer) v.pcmPlayer.enqueue(buf); },
      onStart: () => { v.speaking = true; setPhase("speaking"); stopRecognition(); clearTtsFallbackNotice(); },
      onComplete: () => {
        if (v.pcmPlayer) v.pcmPlayer.markEnded();
        else { v.speaking = false; onTtsDrained(); }
      },
      onError: (name, msg) => {
        console.warn("[voice] tts(restful)", name, msg);
        // RESTful 也失败 → 回退链末端：退到浏览器本地 synth，直到用户改输出引擎/换音色重试。
        v.ttsFallback = true;
        showTtsFallbackNotice(name + ": " + (msg || ""));
        syncOutputUiToEffective();  // 底部「语音输出」下拉同步成「本地」+ 右上角音色切系统声音（生效引擎已退本地）
        const shouldReplayZeroAudio = !v.ttsTurnAudio && v.active
          && (v.speakThisTurn || !v.turnOpen)
          && v.spokenLen > 0 && (v.assistantText || "").trim();
        if (shouldReplayZeroAudio) {
          if (v.pcmPlayer) { try { v.pcmPlayer.stop(); } catch (_) {} }
          v.speaking = false;
          v.spokenLen = 0;
          speakReadyChunks(!v.turnOpen);   // ttsFallback 已置位 → useCloudTts() 假 → 走浏览器补读
          return;
        }
        if (v.pcmPlayer) v.pcmPlayer.markEnded();
        else { v.speaking = false; onTtsDrained(); }
      },
    });
    return v.restfulTts;
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
    holdAudioFocus();                       // 用户手势内抢住媒体焦点，覆盖停麦→TTS 首帧之间的空窗
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
      if (!t) { setPhase("listening", "还没识别到内容，请继续说话"); return; }
      v.aliyunInterim = "";
      stopRecognition();
      sendVoiceText(t);
      return;
    }
    // Web Speech：仍走累积器，flushNow 内部 onFlush → stopRecognition + sendVoiceText。
    if (!v.acc || !v.acc.flushNow()) {
      setPhase("listening", "还没识别到内容，请继续说话");
      return;
    }
  }

  function sendVoiceText(text) {
    if (!text) { resumeListeningIfActive(); return; }
    holdAudioFocus();   // 停麦发送后到回复首段朗读前，仍保持媒体焦点，避免外部播放器恢复
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
    // 选了阿里云音色：走云端合成（流式或 RESTful 回退，由 currentCloudTts 决定）。首段开一条
    // 流，后续 push 续投。currentCloudTts 拿不到引擎（脚本没加载好）时回退 webspeech，别把朗读丢了。
    if (useCloudTts()) {
      const tts = currentCloudTts();
      if (tts) {
        holdAudioFocus();
        if (!v.ttsBegun) { v.ttsBegun = true; tts.begin(); }
        tts.push(text);
        v.speaking = true;
        setPhase("speaking");
        stopRecognition();   // 朗读时停麦，防回环
        return;
      }
    }
    v.pendingSpeak.push(text);
    holdAudioFocus();
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
    holdAudioFocus();
    const u = new SpeechSynthesisUtterance(next);
    applyVoice(u);
    u.rate = 1.05;
    u.onend = () => { v.speaking = false; drainSpeak(); };
    u.onerror = () => { v.speaking = false; drainSpeak(); };
    resumeSpeechOutput();
    try { synth.speak(u); } catch (_) { v.speaking = false; drainSpeak(); }
  }
  // 短播报（如出错时读「出错了」）一律走浏览器 synth：单句、无需流式，避免为一句话
  // 也起一条阿里云 ws；与选中的合成音色无关。
  function speakOnce(text, done) {
    stopRecognition();
    v.speaking = true;
    setPhase("speaking");
    holdAudioFocus();
    const u = new SpeechSynthesisUtterance(text);
    applyVoice(u);
    u.onend = () => { v.speaking = false; if (done) done(); };
    u.onerror = () => { v.speaking = false; if (done) done(); };
    resumeSpeechOutput();
    try { synth.speak(u); } catch (_) { v.speaking = false; if (done) done(); }
  }
  function stopAllSpeech() {
    // 先拆阿里云合成链路（中止 ws + 停播放器 + 重置 begin 标志），再走浏览器 synth。
    // 播放器 stop() 只停当前在播音源、保留并复用 ctx：ctx 在用户手势里已 unlock，置空重建
    // 会丢失解锁状态（移动端重建出的 ctx 是 suspended）→ 不出声 + source 不 onended → 卡死。
    if (v.aliyunTts) { try { v.aliyunTts.abort(); } catch (_) {} }
    if (v.restfulTts) { try { v.restfulTts.abort(); } catch (_) {} }
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
        v.replaySpeechOnVisible = false;
        v.ttsBegun = false;   // 新 turn：合成引擎尚未 begin，首段 enqueueSpeak 时再开一条流
        v.ttsTurnAudio = false;   // 新 turn：本轮阿里云尚未出声（首帧音频到达才置位）
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
        if (v.active && v.speakThisTurn) {
          if (document.visibilityState === "visible") speakReadyChunks(false);
          else deferSpeechReplay();
        }
        break;
      case "turn.done":
        setAiBubble(v.captionAiNode, v.assistantText);
        v.captionAiNode = null;
        clearTurnState(event.turn_id, true);
        if (v.active && v.speakThisTurn) {
          if (document.visibilityState === "visible") speakReadyChunks(true);
          else deferSpeechReplay();
        }
        if (document.visibilityState === "visible") v.replaySpeechOnVisible = false;
        v.speakThisTurn = false;
        if (document.visibilityState !== "visible") break;
        if (useCloudTts()) {
          // 云端合成：文本已全部投完 → end() 收尾（流式发 StopSynthesis / RESTful 标记结束）；
          // 续听交给播放器 drain → onTtsDrained。
          if (v.ttsBegun) { const tts = currentCloudTts(); if (tts) { try { tts.end(); } catch (_) {} } }
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
    holdAudioFocus();   // 打开浮层即占焦点停外部音乐（占位麦不需要手势，权限已授过即生效）
    buildThinkOptions(state.runtime && state.runtime.thinkingOptions);
    reflectThinking(state.thinkingLevel);
    buildOutModeOptions();    // 底部「语音输出」下拉 + 右上角「音色」（配置到位则含阿里云项与音色目录）
    seedCaptionsFromHistory();

    if (!secureOk) {
      elUnsupported.innerHTML = "当前通过 <b>HTTP</b> 访问，手机浏览器会禁用麦克风（即使在设置里允许也无效）。请改用 <b>HTTPS</b> 地址访问。";
      elUnsupported.hidden = false;
      elUnsupported.dataset.hardBlock = "1";   // 硬阻断提示：圆按钮禁用，不让 clearTtsFallbackNotice 误清
      setPhase("error", "需要 HTTPS 才能使用麦克风");
      if (elCircle) elCircle.disabled = true;
      return;
    }
    // 阿里云引擎不依赖浏览器 SpeechRecognition，只需 getUserMedia+AudioWorklet（aliyunEnvOk
    // 已在选引擎时校验）；只有 webspeech 引擎才要求 SR 存在。
    if (v.engine !== "aliyun" && !SR) {
      elUnsupported.textContent = "当前浏览器不支持语音识别，请用 Android Chrome 打开。";
      elUnsupported.hidden = false;
      elUnsupported.dataset.hardBlock = "1";   // 硬阻断提示：圆按钮禁用，不让 clearTtsFallbackNotice 误清
      setPhase("error", "浏览器不支持语音识别");
      if (elCircle) elCircle.disabled = true;
      return;
    }
    elUnsupported.hidden = true;
    delete elUnsupported.dataset.hardBlock;   // 进入正常分支：清掉硬阻断标记，使 TTS 回退提示可被 clear
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
    v.replaySpeechOnVisible = false;
    v.foregroundRecovery = false;
    v.ttsFallback = false;          // 关浮层视为会话结束：清回退标志，下次重新从所选引擎起试
    v.ttsRestfulFallback = false;
    stopAllSpeech();
    releaseAudioFocus(true);
    v.restfulTts = null;            // 丢弃 RESTful 引擎实例（其 AbortController 无须显式释放）
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

    // 输出引擎 / 阿里云音色 / 系统音色偏好：先读持久化（""=尚无偏好，留给 selectRecognitionEngine
    // 拿到配置后定默认）。三者都要在 selectRecognitionEngine 之前读好——它会据此渲染两个下拉。
    try { v.outMode = localStorage.getItem(OUTMODE_KEY) || ""; } catch (_) {}
    try { v.aliyunVoice = localStorage.getItem(ALIYUN_VOICE_KEY) || ""; } catch (_) {}
    try { v.voiceURI = localStorage.getItem(VOICE_KEY) || ""; } catch (_) {}

    // 识别引擎选路：后端 /api/voice/config 报告阿里云可用 + 浏览器支持 getUserMedia/AudioWorklet
    // → 用阿里云实时识别；否则回退浏览器内置 Web Speech（保留全部现有降级路径）。
    // 不阻塞 init：配置异步拉取，拿到再切引擎、定输出引擎/音色默认并重渲两个下拉（见 selectRecognitionEngine 末尾）。
    selectRecognitionEngine();

    // 底部右「语音输出」引擎切换：持久化；重置回退标志（重新从所选引擎起试）；重渲右上角音色；正在播报先停。
    if (elTtsVoice) elTtsVoice.onchange = () => {
      v.outMode = elTtsVoice.value;
      try { localStorage.setItem(OUTMODE_KEY, v.outMode); } catch (_) {}
      v.ttsFallback = false;             // 重新尝试所选引擎
      v.ttsRestfulFallback = false;      // 流式回退标志也清掉
      clearTtsFallbackNotice();
      buildTimbreOptions();              // 输出引擎变了 → 右上角「音色」选项随之切换
      if (v.speaking) stopAllSpeech();   // 正在播报中切换：立即停，下轮生效
    };

    // 引擎手动切换：持久化用户选择，停掉旧引擎当前识别、丢弃两种实例，重算 v.engine；
    // 正在免提聆听时用新引擎立即重开，否则下次点麦生效。
    if (elEngine) elEngine.onchange = () => {
      try { localStorage.setItem(ENGINE_KEY, elEngine.value); } catch (_) {}
      const wasListening = v.active && v.wantListen;   // 切换前捕获，stop 会清掉 wantListen
      if (recogBusy()) stopRecognition();              // 用旧 engine 停掉当前识别
      v.aliyun = null; v.recog = null;                 // 两种实例都丢弃，避免切换后残留
      applyEngineChoice();                             // 重算 v.engine + 刷新下拉
      holdAudioFocus();                                // 焦点 guard 按新引擎换轨（持麦 ↔ 静音 audio）
      if (wasListening && document.visibilityState === "visible") startRecognition();
    };

    // 初次渲染两个下拉（config 未到位前：输出下拉禁用阿里云项、音色列系统声音）；
    // 系统声音异步加载，voiceschanged 时按当前输出引擎重建（本地→系统声音；阿里云→音色目录）。
    buildOutModeOptions();
    if (synth && "onvoiceschanged" in synth) synth.onvoiceschanged = buildTimbreOptions;
    // 右上角「音色」切换：阿里云输出时存阿里云音色（清回退、正在播报先停）；本地输出时存系统声音并试听一句。
    if (elVoice) elVoice.onchange = () => {
      if (effectiveAliyun()) {   // 按「当前生效引擎」判定，回退到本地后选的就是系统声音
        v.aliyunVoice = elVoice.value;
        try { localStorage.setItem(ALIYUN_VOICE_KEY, v.aliyunVoice); } catch (_) {}
        v.ttsFallback = false;
        v.ttsRestfulFallback = false;
        clearTtsFallbackNotice();
        if (v.speaking) stopAllSpeech();   // 换音色立即停，下轮生效
      } else {
        v.voiceURI = elVoice.value;
        try { localStorage.setItem(VOICE_KEY, v.voiceURI); } catch (_) {}
        // 选完试听一句（仅在空闲、没在听/读真实对话时，避免被麦克风回采或打断回复）
        if (!v.speaking && v.phase === "idle") {
          const u = new SpeechSynthesisUtterance("你好，我是你的语音助手");
          applyVoice(u); u.rate = 1.05;
          try { synth.cancel(); synth.speak(u); } catch (_) {}
        }
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
        // 不释放音频焦点：浮层开着就维持静音环境，✕ 退出才把声音还给外部音乐
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
      holdAudioFocus();
      if (v.pcmPlayer) { try { v.pcmPlayer.unlock(); } catch (_) {} }
      const resolve = window.resolveTapAction || resolveTapAction;
      switch (resolve(v.phase, v.speaking)) {
        case "interrupt":
          // 播报中若后端仍在生成，本次点击应取消整轮交互；若后端已经 done，
          // 则没有可取消的 turn，只停止本地剩余播报。
          if (hasOpenTurn()) cancelCurrentTurn();
          else interruptSpeechForTurn();
          break;
        case "cancel": cancelCurrentTurn(); break;
        case "flush": flushPendingNow(); break;
      }
    });

    // 切走/锁屏：小米/Android Chrome 在锁屏期间可能仍能语音交互，真正脆弱的是
    // 解锁回到前台这一瞬间。后台不主动拆链路；回前台后清本地 TTS 卡住队列，并延迟
    // 重建 recognizer，避开 Chrome 麦克风/语音服务刚恢复时的坏状态。
    document.addEventListener("visibilitychange", () => {
      if (!v.open) return;
      // 回前台先重占焦点（即使免提未激活）：后台期间占位麦可能被系统收回（track onended 已清引用）
      if (document.visibilityState === "visible") holdAudioFocus();
      if (!v.active) return;
      if (document.visibilityState !== "visible") {
        releaseWakeLock();
        v.foregroundRecovery = true;
        return;
      }
      const needsRecovery = v.foregroundRecovery;
      v.foregroundRecovery = false;
      requestWakeLock();
      if (needsRecovery) recoverLocalSpeechAfterForeground();
      else resumeSpeechOutput();
      if (hasOpenTurn()) {                                  // 正在等回复，别插队
        setPhase("thinking", "等待当前回复结束…");
        return;
      }
      if (v.replaySpeechOnVisible && (v.assistantText || "").trim()) {
        v.replaySpeechOnVisible = false;
        // 锁屏期间阿里云那条合成流可能仍开着、且前台已 push 过部分文本（ttsBegun 仍为 true）。
        // 不先拆掉就从 spokenLen=0 重投全文，会把已合成的句子再读一遍，并复用陈旧的播放器
        // 调度游标。stopAllSpeech 中止旧流 + stop() 复位播放器 + 清 ttsBegun，让下面的 replay
        // 以 begin() 重开一条干净的流，从本轮开头完整重播一次。
        stopAllSpeech();
        v.speakThisTurn = true;
        v.spokenLen = 0;
        speakReadyChunks(true);
        v.speakThisTurn = false;
        if (useCloudTts()) {
          if (v.ttsBegun) { const tts = currentCloudTts(); if (tts) { try { tts.end(); } catch (_) {} } }
          else resumeListeningIfActive();
        }
        return;
      }
      if (needsRecovery) {
        // 延迟重建 recognizer：先丢弃锁屏期间可能已卡死的旧实例（复用它再 start() 会抛
        // InvalidStateError），并清运行态，等 ~1.2s 让 Chrome 麦克风/语音服务恢复就绪后，
        // 由 scheduleResumeListening → startRecognition → _doStart 建一个干净的新实例开麦。
        stopRecognition();
        v.recog = null; v.aliyun = null;
        v.recognizing = false; v.recogStarting = false; v.aliyunRunning = false;
        scheduleResumeListening(1200);
      } else {
        scheduleRecognizerReset();                        // 等旧 recognizer onend 后干净重建（带 800ms 兜底）
      }
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
