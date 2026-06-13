/* 语音浮层 shell —— 命令解释器 + 端口接线，挂在聊天 WebUI（app.js）之上。
 *
 * 复用 app.js 的全局：ws 连接 / send / state / renderMarkdown / extractText /
 * formatAcceptedUserText / renderThinkingToggle / authHeaders。
 * app.js 在 handleEvent 末尾调用 window.VoiceMode.onEvent(event)，
 * 字幕/朗读/状态机和聊天流跑在同一份 ws + 同一个 session 上【E3】。
 *
 * 架构：voice-core.js 纯状态机产出 (state', commands)，本文件：
 *   1. 把 DOM 手势 / 识别回调 / 合成回调 / 聊天事件 / 可见性 / 计时器归一成事件 dispatch
 *   2. 逐条执行 command（开停麦、投合成、发消息、计时器、wakeLock…）
 *   3. 每次迁移后 diff 音频焦点档位（focusMode 纯函数【C3】）驱动 guard 换轨
 *   4. 调 voice-view.js 渲染
 *
 * 偏好与配置（不进核心 ctx）：
 *   - localStorage：识别引擎 / 输出引擎 / 阿里云音色 / 系统音色
 *   - /api/voice/config 异步拉取；configLoaded 前不把存储值误判降级【B7】
 *   - 合成链按所选输出引擎组装（FallbackSpeaker），降级上报驱动「生效引擎」UI【B8】
 */
(() => {
  "use strict";

  const core = window.VoiceCore;
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const synth = window.speechSynthesis;

  // secure context：手机非 HTTPS（且非 localhost）下浏览器直接禁麦克风
  const isLocalHost = /^(localhost|127\.|0\.0\.0\.0$|\[?::1\]?$)/.test(location.hostname);
  const secureOk = window.isSecureContext || isLocalHost;
  // 阿里云引擎硬依赖：getUserMedia + AudioWorklet（worklet 在音频线程降采样转 PCM）
  const aliyunEnvOk = Boolean(
    navigator.mediaDevices && navigator.mediaDevices.getUserMedia
    && window.AudioContext && window.AudioWorklet
  );

  const ENGINE_KEY = "nanoVoiceEngine";
  const OUTMODE_KEY = "nanoTtsMode";
  const ALIYUN_VOICE_KEY = "nanoAliyunVoice";
  const VOICE_KEY = "nanoVoiceURI";

  // ── shell 持有的全部可变状态 ──────────────────────────────────────────────
  let model = core.createInitialModel();
  let view = null;
  let recognizer = null;        // 当前识别适配器（按 desiredEngineName 懒建）
  let recognizerStandby = false; // 当前实例是否为待机模式（待唤醒短去抖本地引擎）
  let chime = null;             // 唤醒提示音
  let warnedWakeNoSr = false;
  let speaker = null;           // FallbackSpeaker 合成链（按 selectedOut 组装）
  let localHelper = null;       // 独立本地合成器：speakOnce / 音色试听 / 锁屏卡死检测
  let guard = null;             // 音频焦点 guard
  let lastFocusMode = "released";
  let wakeLock = null;
  const timers = {};            // tag -> timeout id
  const timerArmedAt = {};      // tag -> 最近一次armed时刻（wakeIdle 节流用）

  const prefs = { engine: "", outMode: "", aliyunVoice: "", voiceURI: "" };
  let voiceCfg = null;
  let configLoaded = false;     //【B7】
  let effectiveOut = "";        // 合成链当前生效级（降级上报驱动）【B8】
  let fallbackNotice = "";
  let aliyunAsrBlockedByBluetooth = false; // Chrome 只给蓝牙电话麦时，本轮退本地 ASR 保 TTS 出声

  function store(key, val) { try { localStorage.setItem(key, val); } catch (_) {} }
  function load(key) { try { return localStorage.getItem(key) || ""; } catch (_) { return ""; } }

  // ── 能力判定 ──────────────────────────────────────────────────────────────
  function aliyunUsable() {
    return Boolean(aliyunEnvOk && voiceCfg && voiceCfg.available
      && voiceCfg.provider === "aliyun" && voiceCfg.appkey && voiceCfg.endpoint);
  }
  function aliyunTtsUsable() {
    return Boolean(aliyunEnvOk && voiceCfg && voiceCfg.available
      && voiceCfg.tts && voiceCfg.tts.enabled && voiceCfg.appkey && voiceCfg.endpoint);
  }
  // 识别引擎选路：显式选本地恒为 webspeech；否则阿里云可用则阿里云。
  function resolvedEngine() {
    if (prefs.engine === "webspeech") return "webspeech";
    return aliyunUsable() ? "aliyun" : "webspeech";
  }
  // 待唤醒模式【W1】：config 配了 wakeWord 且本机支持 Web Speech 才启用
  // （待机引擎固定本地——免费听关键词，阿里云待机持续计费不可取）。
  function wakeKeyword() {
    const kw = (voiceCfg && voiceCfg.wake_word) || "";
    if (kw && !SR) {
      if (!warnedWakeNoSr) { warnedWakeNoSr = true; console.warn("[voice] wakeWord 已配置但本机不支持 Web Speech，待唤醒模式禁用"); }
      return "";
    }
    return kw;
  }
  function inStandby() { return core.inStandby(model.ctx); }   // 判定单源在 core
  // startMic 实际使用的引擎：待机本地听关键词，唤醒后切回所选引擎【W2】。
  function desiredEngineName() { return inStandby() ? "webspeech" : resolvedEngine(); }
  function activeEngineName() {
    if (aliyunAsrBlockedByBluetooth && SR && resolvedEngine() === "aliyun") return "webspeech";
    return desiredEngineName();
  }
  // 输出引擎：用户偏好优先；无偏好且 config 已到 → 阿里云可用默认流式，否则本地。
  function selectedOut() {
    if (prefs.outMode) return prefs.outMode;
    if (!configLoaded) return "local";   // config 未到位前临时显示，不写回偏好【B7】
    return aliyunTtsUsable() ? "aliyun-flowing" : "local";
  }
  function currentAliyunVoice() {
    if (prefs.aliyunVoice) return prefs.aliyunVoice;
    return (voiceCfg && voiceCfg.tts && voiceCfg.tts.voice) || "";
  }
  function ttsSampleRate() {
    return (voiceCfg && voiceCfg.tts && voiceCfg.tts.sample_rate) || 16000;
  }

  // 走 app.js 的 api()：401 会统一弹 token 重输框（与 WebUI 其余部分行为一致），
  // 抛错由各引擎 onError("token") 接住。
  function fetchVoiceToken() {
    return api("/api/voice/token");
  }

  // ── 端口：识别 ───────────────────────────────────────────────────────────
  function ensureRecognizer() {
    // 引擎或待机/对话模式不匹配 → 丢弃重建（唤醒/回落时的引擎切换走这里）【W2/W4】
    const standby = inStandby();
    if (recognizer && (recognizer.name !== activeEngineName() || recognizerStandby !== standby)) {
      dropRecognizer();
    }
    if (recognizer) return recognizer;
    recognizerStandby = standby;
    const cbs = {
      onStarted: () => dispatch({ type: "MIC_STARTED" }),
      onInterim: (text) => dispatch({ type: "MIC_INTERIM", text }),
      onFinal: (text) => dispatch({ type: "MIC_FINAL", text }),
      onError: (kind, msg) => {
        if (kind !== "denied") console.warn("[voice] mic error:", kind, msg);
        if (kind === "bluetooth-hfp" && SR) {
          aliyunAsrBlockedByBluetooth = true;
          markControlsDirty();
        }
        dispatch({ type: "MIC_ERROR", kind });
      },
      onEnded: () => dispatch({ type: "MIC_ENDED" }),
      log: (k, m) => console.warn("[voice] recog", k, m),
    };
    if (activeEngineName() === "aliyun") {
      recognizer = window.createAliyunRecognizer(Object.assign({
        getConfig: () => ({ appkey: voiceCfg && voiceCfg.appkey, endpoint: voiceCfg && voiceCfg.endpoint }),
        getToken: fetchVoiceToken,
      }, cbs));
    } else if (standby) {
      // 待机模式差异参数（非双源）：待机语句只有唤醒词/唤醒词+短指令，短去抖快速 flush；
      // 正式对话仍用适配器默认（base1600/max3200，单一来源在 voice-recognizer-webspeech.js）
      recognizer = window.createWebspeechRecognizer(Object.assign({
        baseSilenceMs: 800, maxSilenceMs: 1600,
      }, cbs));
    } else {
      recognizer = window.createWebspeechRecognizer(cbs);
    }
    return recognizer;
  }
  function dropRecognizer() {
    if (recognizer) { try { recognizer.stop(); } catch (_) {} }
    recognizer = null;
  }

  // ── 端口：合成链 ─────────────────────────────────────────────────────────
  function ensureLocalHelper() {
    if (localHelper) return localHelper;
    localHelper = window.createLocalSpeaker({ getVoice: getSelectedSystemVoice });
    return localHelper;
  }
  function getSelectedSystemVoice() {
    if (!prefs.voiceURI || !synth) return null;
    let voices = [];
    try { voices = synth.getVoices() || []; } catch (_) {}
    return voices.find((v) => v.voiceURI === prefs.voiceURI) || null;
  }

  function buildSpeaker() {
    if (speaker) { try { speaker.dispose(); } catch (_) {} }
    fallbackNotice = "";
    markControlsDirty();   // 生效引擎/回退状态变化 → 下拉需重渲
    const out = selectedOut();
    effectiveOut = out;
    const localLevel = {
      name: "local", usesPlayer: false,
      create: (cb) => window.createLocalSpeaker({
        getVoice: getSelectedSystemVoice,
        onAudible: cb.onAudible, onCompleted: cb.onCompleted, onError: cb.onError,
      }),
    };
    const flowingLevel = {
      name: "aliyun-flowing", usesPlayer: true,
      create: (cb) => window.createFlowingSpeaker({
        getConfig: () => ({
          appkey: voiceCfg && voiceCfg.appkey,
          endpoint: voiceCfg && voiceCfg.endpoint,
          voice: currentAliyunVoice(),
          sampleRate: ttsSampleRate(),
        }),
        getToken: fetchVoiceToken,
        onAudio: cb.onAudio, onCompleted: cb.onCompleted, onError: cb.onError,
      }),
    };
    const restLevel = {
      name: "aliyun-rest", usesPlayer: true,
      create: (cb) => window.createRestSpeaker({
        url: "/api/voice/tts",
        headers: authHeaders(),
        getConfig: () => ({ voice: currentAliyunVoice(), sampleRate: ttsSampleRate() }),
        onAudio: cb.onAudio, onCompleted: cb.onCompleted, onError: cb.onError,
      }),
    };
    // 回退链【B3】：流式→RESTful→本地；RESTful 起→本地；本地恒本地。
    const levels = out === "aliyun-flowing" ? [flowingLevel, restLevel, localLevel]
      : out === "aliyun-rest" ? [restLevel, localLevel]
      : [localLevel];
    speaker = window.createFallbackSpeaker({
      levels,
      // cb 全量展开转发（onDrained/onAudible/onInterrupted/onError，键名与播放器
      // 选项 1:1）——不逐键枚举：曾因按过期契约只转发两个键，把零发声判定
      // （onAudible）和解卡先掐引擎（onInterrupted）在生产路径上整段丢成死代码。
      createPlayer: (cb) => window.createVoicePcmPlayer(Object.assign({
        sampleRate: ttsSampleRate(),
      }, cb)),
      onAudible: () => dispatch({ type: "SPEAK_AUDIBLE" }),
      onDrained: () => dispatch({ type: "SPEAK_DRAINED" }),
      onFallback: (levelName, reason) => {
        effectiveOut = levelName;
        // 降到本地才弹横幅（手机上看不到 console）【B6】；流式→RESTful 静默换轨
        fallbackNotice = levelName === "local"
          ? `阿里云语音合成失败，已回退本地音色。原因：${reason}（换音色可重试）` : "";
        markControlsDirty();
        renderAll();
      },
      log: (k, m) => console.warn("[voice]", k, m),
    });
    return speaker;
  }
  function ensureSpeaker() { return speaker || buildSpeaker(); }

  // ── 端口：音频焦点 / wakeLock ────────────────────────────────────────────
  function ensureChime() {
    if (chime) return chime;
    chime = window.createVoiceChime({
      volume: 0.9,   // 提示音独立 AudioContext，音量与 TTS 朗读分开调大（默认 0.5 偏小）
      log: (name, msg) => console.warn("[voice]", name, msg),
    });
    return chime;
  }

  function ensureGuard() {
    if (guard) return guard;
    guard = window.createVoiceAudioFocusGuard({
      log: (name, msg) => console.warn("[voice] audio-focus", name, msg),
    });
    return guard;
  }
  // 每次迁移后 diff 换轨【C3】：浮层开着持静音保持音瞬态焦点，closed/error 释放。
  function syncFocus() {
    const mode = core.focusMode(model.state);
    if (mode === lastFocusMode) return;
    lastFocusMode = mode;
    if (mode === "released" && !guard) return;
    try { ensureGuard().setMode(mode); } catch (_) {}
  }

  async function requestWakeLock() {
    if (!("wakeLock" in navigator)) return;
    try {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeLock.addEventListener("release", () => { wakeLock = null; });
    } catch (_) { wakeLock = null; }
  }
  function releaseWakeLock() {
    try { if (wakeLock) wakeLock.release(); } catch (_) {}
    wakeLock = null;
  }

  // ── 命令解释器 ───────────────────────────────────────────────────────────
  function exec(cmd) {
    switch (cmd.type) {
      case "startMic": {
        const rec = ensureRecognizer();
        if (!rec.busy()) rec.start();
        break;
      }
      case "stopMic":
        if (recognizer) recognizer.stop();
        break;
      case "rebuildMic": {
        // 丢弃可能卡死的实例建新的【A4】（aliyun 的 rebuild 内部即 stop+start）
        const rec = ensureRecognizer();
        rec.rebuild();
        break;
      }
      case "flushMic": {
        const rec = recognizer;
        const sent = rec ? rec.flushNow() : "";
        if (!sent) dispatch({ type: "FLUSH_EMPTY" });
        break;
      }
      case "armTimer": {
        // wakeIdle 在说话期间每个 interim 都会重置——节流到 2s 一次，避免高频
        // clearTimeout/setTimeout 空转（回落语义只需 20s±2s 精度）。
        if (cmd.tag === "wakeIdle" && timers.wakeIdle
            && Date.now() - (timerArmedAt.wakeIdle || 0) < 2000) break;
        const ms = cmd.ms != null ? cmd.ms : ensureRecognizer().startTimeoutMs;   //【A5】
        if (timers[cmd.tag]) clearTimeout(timers[cmd.tag]);
        timerArmedAt[cmd.tag] = Date.now();
        timers[cmd.tag] = setTimeout(() => {
          timers[cmd.tag] = null;
          dispatch({ type: "TIMEOUT", tag: cmd.tag });
        }, ms);
        break;
      }
      case "clearTimer":
        if (timers[cmd.tag]) { clearTimeout(timers[cmd.tag]); timers[cmd.tag] = null; }
        break;
      case "chatSend": {
        const sid = state.currentSession && state.currentSession.session_id;
        if (!sid) { dispatch({ type: "SEND_FAILED", message: "没有可用会话" }); break; }
        // response_style:"voice" → 后端给本轮 system prompt 追加口语化指令
        if (!send("chat.send", { session_id: sid, text: cmd.text, attachments: [], response_style: "voice" })) {
          dispatch({ type: "SEND_FAILED", message: "未连接到服务器" });
        }
        break;
      }
      case "cancelTurn":
        if (!cmd.turnId) break;
        if (!send("turn.cancel", { turn_id: cmd.turnId })) {
          dispatch({ type: "SEND_FAILED", message: "未连接到服务器，无法停止当前回复" });
        }
        break;
      case "speakerBegin":
        ensureSpeaker().begin();
        break;
      case "speak":
        ensureSpeaker().push(cmd.text);
        break;
      case "speakerEnd":
        if (speaker) speaker.end();
        break;
      case "stopSpeech":
        if (speaker) speaker.abort();
        if (localHelper) { try { localHelper.abort(); } catch (_) {} }   // speakOnce 残留
        try { if (synth) synth.cancel(); } catch (_) {}
        break;
      case "speakOnce":
        ensureLocalHelper().sayOnce(cmd.text, () => dispatch({ type: "SPEAK_DRAINED" }));
        break;
      case "chime":
        ensureChime().play(cmd.variant);   // 唤醒升调／回落待机降调(variant:"sleep")；非手势靠 primeAudio 解锁
        break;
      case "wakeLock":
        if (cmd.on) requestWakeLock();
        else releaseWakeLock();
        break;
      case "primeAudio":
        // 用户手势内：解锁静音 audio 的 autoplay【C3】+ 播放器 ctx【B2】+ 提示音 + 清 synth 队列
        try { ensureGuard().prime(); } catch (_) {}
        try { ensureChime().prime(); } catch (_) {}
        if (speaker) { try { speaker.unlock(); } catch (_) {} }
        else if (selectedOut() !== "local") { try { ensureSpeaker().unlock(); } catch (_) {} }
        try { if (synth) synth.cancel(); } catch (_) {}
        break;
      case "recoverSpeechOutput":
        // 回前台：重申焦点（后台期间保持音可能被系统打断）+ 播放器 unlock + 清本地 synth 坏队列
        if (guard) { try { guard.refresh(); } catch (_) {} }
        if (speaker) { try { speaker.unlock(); } catch (_) {} }
        try { if (synth && typeof synth.resume === "function") synth.resume(); } catch (_) {}
        break;
      case "teardown":
        // ✕ 退出：释放一切（焦点还给外部音乐、播放器 ctx 关闭、回退记忆复位）
        if (speaker) { try { speaker.dispose(); } catch (_) {} speaker = null; }
        if (guard) { try { guard.dispose(); } catch (_) {} guard = null; }
        if (chime) { try { chime.dispose(); } catch (_) {} chime = null; }
        lastFocusMode = "released";
        dropRecognizer();
        effectiveOut = "";
        fallbackNotice = "";
        markControlsDirty();
        break;
      default:
        console.warn("[voice] unknown command:", cmd.type);
    }
  }

  // ── dispatch：事件队列 + 迁移 + 命令执行 + 焦点 diff + 渲染 ───────────────
  let processing = false;
  const eventQueue = [];
  function dispatch(event) {
    eventQueue.push(event);
    if (processing) return;
    processing = true;
    while (eventQueue.length) {
      const ev = eventQueue.shift();
      const r = core.transition(model, ev);
      model = { state: r.state, ctx: r.ctx };
      for (const c of r.commands) exec(c);
      syncFocus();
    }
    processing = false;
    renderAll();
  }

  // ── 渲染 ─────────────────────────────────────────────────────────────────
  // 控件区（四个下拉）只在配置/偏好/降级/系统声音变化时重建（controlsDirty 显式标脏）；
  // 不能每个 text.delta 都全量重建 <option> 列表——长回复会重建几百次，且正打开的
  // <select> 会被打闪/收起。
  let controlsDirty = true;
  function markControlsDirty() { controlsDirty = true; }
  function controlsState() {
    let systemVoices = [];
    try { systemVoices = (synth && synth.getVoices()) || []; } catch (_) {}
    return {
      resolvedEngine: resolvedEngine(),
      srSupported: Boolean(SR),
      aliyunUsable: aliyunUsable(),
      aliyunTtsUsable: aliyunTtsUsable(),
      selectedOut: selectedOut(),
      effectiveOut: effectiveOut || selectedOut(),
      aliyunVoice: currentAliyunVoice(),
      voiceURI: prefs.voiceURI,
      ttsVoices: (voiceCfg && voiceCfg.tts && voiceCfg.tts.voices) || [],
      systemVoices,
    };
  }
  function renderAll() {
    if (!view) return;
    view.render(model, { fallbackNotice });
    if (controlsDirty) {
      controlsDirty = false;
      view.renderControls(controlsState());
    }
  }

  // ── 打开/关闭 ────────────────────────────────────────────────────────────
  function externalTurnOpen() {
    return Boolean(state.activeTurnId || (state.currentSession && state.currentSession.active_turn_id));
  }
  function computeHardBlock() {
    if (!secureOk) return "https";
    if (resolvedEngine() !== "aliyun" && !SR) return "no-sr";
    return null;
  }
  function openOverlay(autoStart) {
    if (model.state === "closed" && view) {
      view.seedCaptions(
        (state.currentSession && state.currentSession.history) || [],
        typeof extractText === "function" ? extractText : null
      );
    }
    dispatch({
      type: "OPEN",
      autoStart: Boolean(autoStart),
      hardBlock: computeHardBlock(),
      externalTurnOpen: externalTurnOpen(),
      hidden: document.visibilityState !== "visible",
      wakeKeyword: wakeKeyword(),
    });
  }
  function closeOverlay() { dispatch({ type: "CLOSE" }); }

  // ── 来自 app.js handleEvent 的聊天事件【E3】──────────────────────────────
  function onEvent(event) {
    // thinking 下拉始终跟随后端（即使浮层没开，下次开时也是对的）
    if (event.type === "state.updated" && view) {
      view.buildThinkOptions(state.runtime && state.runtime.thinkingOptions);
      view.reflectThinking(event.thinking_level != null ? event.thinking_level : state.thinkingLevel);
    }
    if (model.state === "closed") return;
    // 只认当前 session 的 turn 事件
    if (event.session_id && state.currentSession && event.session_id !== state.currentSession.session_id) return;

    switch (event.type) {
      case "chat.accepted":
        if (view) {
          view.addUserBubble(typeof formatAcceptedUserText === "function" ? formatAcceptedUserText(event) : (event.text || ""));
          view.startAiBubble();
        }
        dispatch({ type: "CHAT_ACCEPTED", turnId: event.turn_id || "" });
        break;
      case "text.delta":
        dispatch({ type: "TEXT_DELTA", text: event.text || "" });
        break;
      case "turn.done":
        dispatch({ type: "TURN_DONE", turnId: event.turn_id || "" });
        if (view) view.finishAiBubble(model.ctx.turn ? model.ctx.turn.text : null);
        break;
      case "turn.error":
        if (view) view.addAiError(event.message);
        dispatch({ type: "TURN_ERROR", message: event.message || "" });
        break;
      case "turn.cancelled":
        if (view) view.finishAiBubble(null);
        dispatch({ type: "TURN_CANCELLED", turnId: event.turn_id || "" });
        break;
    }
  }

  // ── 配置拉取（异步，不阻塞 init）─────────────────────────────────────────
  async function loadVoiceConfig() {
    if (aliyunEnvOk) {
      // api() 统一处理 401（弹 token 框）；断网/旧后端静默保持 null，阿里云视为不可用
      try { voiceCfg = await api("/api/voice/config"); } catch (_) {}
    }
    configLoaded = true;   // 成功/失败/断网都算确定【B7】
    // 存储的阿里云音色不在目录（未设/已下线）→ 此刻才允许降级到后端默认【B7】
    const voices = (voiceCfg && voiceCfg.tts && voiceCfg.tts.voices) || [];
    if (prefs.aliyunVoice && voices.length && !voices.some((v) => v.value === prefs.aliyunVoice)) {
      prefs.aliyunVoice = "";
    }
    // 选了阿里云输出但确实不可用 → 落回本地（一旦 config 确定才动偏好）
    if (prefs.outMode && prefs.outMode !== "local" && !aliyunTtsUsable()) {
      prefs.outMode = "local";
      store(OUTMODE_KEY, "local");
    }
    if (speaker) buildSpeaker();   // 已建过链（不太可能这么早）：按新配置重组
    // 引擎选路可能随配置变化（webspeech→aliyun）：丢弃空闲的旧实例，下次开麦用新引擎
    if (recognizer && recognizer.name !== activeEngineName() && !recognizer.busy()) dropRecognizer();
    markControlsDirty();
    renderAll();
  }

  // ── 绑定 ─────────────────────────────────────────────────────────────────
  function init() {
    view = window.createVoiceView({
      renderMarkdown: typeof renderMarkdown === "function" ? renderMarkdown : null,
    });

    prefs.engine = load(ENGINE_KEY);
    prefs.outMode = load(OUTMODE_KEY);
    prefs.aliyunVoice = load(ALIYUN_VOICE_KEY);
    prefs.voiceURI = load(VOICE_KEY);

    loadVoiceConfig();

    const els = view.els;
    if (els.micBtn) els.micBtn.onclick = () => openOverlay(true);
    if (els.exit) els.exit.onclick = closeOverlay;
    if (els.circle) els.circle.onclick = () => {
      if (els.circle.disabled) return;
      dispatch({ type: "TOGGLE", externalTurnOpen: externalTurnOpen(), wakeKeyword: wakeKeyword() });
    };

    // 点圆/字幕/底栏以外的空白区：手势意图由状态机路由（flush/cancel/interrupt），
    // 手势解锁（primeAudio）也由核心作为命令发出，与 OPEN/TOGGLE 同一通道。
    if (els.overlay) els.overlay.addEventListener("click", (e) => {
      if (e.target.closest(".voice-circle, .voice-footer, .voice-stage-head, .voice-captions")) return;
      dispatch({ type: "TAP", externalTurnOpen: externalTurnOpen(), externalTurnId: state.activeTurnId || "" });
    });

    if (els.think) els.think.onchange = () => {
      const lvl = els.think.value;
      state.thinkingLevel = lvl;
      if (lvl !== "off") state.lastThinkingLevel = lvl;
      view.reflectThinking(lvl);
      send("thinking.set", { level: lvl });
      if (typeof renderThinkingToggle === "function") renderThinkingToggle();
    };

    // 识别引擎切换：持久化；正在聆听时用新引擎接力重开（MIC_ENDED 走核心续听路径）
    if (els.engine) els.engine.onchange = () => {
      prefs.engine = els.engine.value;
      store(ENGINE_KEY, prefs.engine);
      aliyunAsrBlockedByBluetooth = false;
      const listening = model.state === "starting" || model.state === "capturing";
      dropRecognizer();              // 丢弃旧引擎实例（stop 不触发 onEnded）
      markControlsDirty();
      if (listening) dispatch({ type: "MIC_ENDED" });   // 核心据此用新引擎立即重开
      renderAll();
    };

    // 输出引擎切换：持久化；重建链（复位回退记忆【B3】）；正在播立即停（SPEAKER_RESET）
    if (els.outMode) els.outMode.onchange = () => {
      prefs.outMode = els.outMode.value;
      store(OUTMODE_KEY, prefs.outMode);
      buildSpeaker();
      dispatch({ type: "SPEAKER_RESET" });
    };

    // 音色切换：按当前生效引擎判定存阿里云音色还是系统声音【B8】
    if (els.timbre) els.timbre.onchange = () => {
      const eff = effectiveOut || selectedOut();
      if (eff !== "local" && aliyunTtsUsable()) {
        prefs.aliyunVoice = els.timbre.value;
        store(ALIYUN_VOICE_KEY, prefs.aliyunVoice);
        buildSpeaker();              // 换音色重试所选引擎（复位回退记忆）
        dispatch({ type: "SPEAKER_RESET" });
      } else {
        prefs.voiceURI = els.timbre.value;
        store(VOICE_KEY, prefs.voiceURI);
        // 空闲试听一句（不打断真实对话）
        if (model.state === "paused") ensureLocalHelper().sayOnce("你好，我是你的语音助手");
        markControlsDirty();
        renderAll();
      }
    };

    // 系统声音异步加载：voiceschanged 后重渲音色下拉
    if (synth && "onvoiceschanged" in synth) synth.onvoiceschanged = () => { markControlsDirty(); renderAll(); };

    // 可见性：后台不拆链路，回前台统一恢复【A1/D1/D2】
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        // 本地 synth 锁屏会静默丢/挂起队列；卡着即触发全文重播
        const speechBusy = Boolean(
          (effectiveOut || selectedOut()) === "local" && speaker && speaker.busy()
        );
        dispatch({ type: "VISIBLE", speechBusy });
      } else {
        dispatch({ type: "HIDDEN" });
      }
    });

    // 深链：/voice 直接进语音态（不自动聆听——浏览器要求手势才能开麦）
    if (location.pathname === "/voice" || location.pathname === "/voice/") {
      setTimeout(() => openOverlay(false), 300);
    }

    renderAll();
  }

  window.VoiceMode = { onEvent, open: () => openOverlay(true), close: closeOverlay };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
