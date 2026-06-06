/* 阿里云实时识别适配器 —— 浏览器经 WebSocket 直连阿里云 NLS 网关，包装为统一
 * Recognizer 端口（契约见 voice-recognizer-webspeech.js 头注释）。
 *
 * 阿里云协议（SpeechTranscriber namespace）：
 *   - 连 wss://...?token=<临时Token>（token 经 /api/voice/token 签发，浏览器不碰 AK/SK）
 *   - 发 StartTranscription（Text frame JSON）→ 收 TranscriptionStarted 后才能送音频
 *   - 音频 Binary frame：16k/16bit/单声道 PCM，每帧约 3200B（voice-pcm-worklet.js 切好）
 *   - 下行事件：TranscriptionResultChanged（中间结果）/ SentenceEnd（一句结束）/
 *     TranscriptionCompleted / TaskFailed
 *   - message_id 每条随机 32 hex；task_id 整个会话一致。
 *
 * 与 webspeech 适配器的关键差异【A6】：阿里云自带 max_sentence_silence 断句，
 * SentenceEnd 就是一句完整话 → 直接 onFinal，不叠前端去抖（再叠只会变慢）。
 * 未定 interim 记下来供 flushNow（点屏立即发送）取用。
 *
 * 历史坑位（语义必须保持）：
 *  - 【A5】start() 是 async（getToken + getUserMedia 授权框可能数秒）：
 *    实例级抗重入（ws||starting 时忽略）防双 ws；每条连接的回调闭包捕获局部 sock，
 *    首行 sock!==ws 守卫——被取代的旧 socket 迟到回调一律不动作；
 *    startTimeoutMs=12000：要覆盖授权框 + 拉 token + ws 握手 + 等 Started，
 *    1.5s 窗口会在授权框没点完时误判卡死、强杀 CONNECTING 的 ws。
 *  - 【A7】abort 要能作废 await 中的 in-flight start：generation 中止令牌，
 *    abort() 自增使每个 await 后及 new ws 前的校验失效；被中止视为用户主动行为
 *    （不 onError），已开麦阶段被中止要关麦。
 *  - 主动 stop/abort 不触发 onEnded（命令回执无意义）；意外断开才 onEnded，
 *    核心据此续听接力。
 *
 * UMD：WebSocketImpl / setupAudio 可注入；纯函数（parseAliyunEvent/makeId/
 * buildStartCommand/buildStopCommand）挂在工厂上供 node --test。
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory(require("./voice-nls.js"));
  else root.createAliyunRecognizer = factory(root.VoiceNls);
})(typeof self !== "undefined" ? self : this, function (nls) {
  "use strict";

  var makeId = nls.makeId;

  // 纯解析：一条阿里云事件 JSON → {kind, text}。
  // kind: 'started' | 'interim' | 'final' | 'completed' | 'failed' | 'other'
  function parseAliyunEvent(obj) {
    var header = (obj && obj.header) || {};
    var payload = (obj && obj.payload) || {};
    switch (header.name) {
      case "TranscriptionStarted": return { kind: "started", text: "" };
      case "TranscriptionResultChanged": return { kind: "interim", text: payload.result || "" };
      case "SentenceEnd": return { kind: "final", text: payload.result || "" };
      case "TranscriptionCompleted": return { kind: "completed", text: "" };
      case "TaskFailed": return { kind: "failed", text: nls.failureText(header) };
      default: return { kind: "other", text: "" };
    }
  }

  function buildStartCommand(appkey, taskId, makeMsgId) {
    return nls.envelope(appkey, taskId, "SpeechTranscriber", "StartTranscription", {
      format: "pcm",
      sample_rate: 16000,
      enable_intermediate_result: true,
      enable_punctuation_prediction: true,
      enable_inverse_text_normalization: true,
    }, makeMsgId);
  }

  function buildStopCommand(appkey, taskId, makeMsgId) {
    return nls.envelope(appkey, taskId, "SpeechTranscriber", "StopTranscription", undefined, makeMsgId);
  }

  // 工厂：opts = { getConfig, getToken, onStarted, onInterim, onFinal, onError, onEnded,
  //               WebSocketImpl?, setupAudio?, workletUrl? }
  function createAliyunRecognizer(opts) {
    opts = opts || {};
    var getConfig = opts.getConfig;     // () -> {appkey, endpoint}
    var getToken = opts.getToken;       // async () -> {token, ...}
    var onStarted = opts.onStarted || function () {};
    var onInterim = opts.onInterim || function () {};
    var onFinal = opts.onFinal || function () {};
    var onError = opts.onError || function () {};
    var onEnded = opts.onEnded || function () {};
    var WebSocketImpl = opts.WebSocketImpl || (typeof WebSocket !== "undefined" ? WebSocket : null);
    var workletUrl = opts.workletUrl || "/static/voice-pcm-worklet.js";

    var ws = null;
    var starting = false;      // start() 已进入、ws 未建好的中间态（抗重入【A5】）
    var generation = 0;        // 中止令牌【A7】
    var audioCtx = null;
    var workletNode = null;
    var micStream = null;
    var taskId = "";
    var started = false;       // 已收 TranscriptionStarted（可发音频）
    var deliberate = false;    // 主动 stop/abort 中：不触发 onEnded、忽略后续事件
    var pendingFrames = [];    // Started 之前先攒音频帧
    var lastInterim = "";      // 未定 interim（flushNow 用）

    function cleanupAudio() {
      try { if (workletNode) workletNode.disconnect(); } catch (_) {}
      try { if (micStream) micStream.getTracks().forEach(function (t) { t.stop(); }); } catch (_) {}
      try { if (audioCtx) audioCtx.close(); } catch (_) {}
      workletNode = null;
      micStream = null;
      audioCtx = null;
    }

    // 拆链路；fireEnded 决定是否上报（主动停止不报）。
    function teardown(fireEnded) {
      starting = false;
      started = false;
      lastInterim = "";
      pendingFrames = [];
      cleanupAudio();
      var sock = ws;
      ws = null;               // 先摘引用：迟到回调 sock!==ws 直接 return【A5】
      try { if (sock && sock.readyState <= 1) sock.close(); } catch (_) {}
      if (fireEnded) onEnded();
    }

    function fail(name, msg) {
      if (deliberate) return;
      deliberate = true;
      onError(name, msg);
      teardown(true);          // 失败也算非主动结束：核心可据此续听重试
      deliberate = false;
    }

    function sendAudio(buf) {
      if (!ws || ws.readyState !== 1) return;
      if (!started) { pendingFrames.push(buf); return; }
      try { ws.send(buf); } catch (_) {}
    }

    function flushPendingFrames() {
      if (!ws || ws.readyState !== 1) return;
      for (var i = 0; i < pendingFrames.length; i++) {
        try { ws.send(pendingFrames[i]); } catch (_) {}
      }
      pendingFrames = [];
    }

    async function setupAudioReal() {
      // 关 AEC（保留降噪/自动增益）：开 AEC 的采集走 VOICE_COMMUNICATION 源，Android
      // 进通信模式 = 系统层面"来电话"——音量键变通话音量；停麦时"通话结束"，车机/
      // 蓝牙栈会按通话结束的标准行为自动恢复媒体播放（AVRCP PLAY），把用户手动暂停
      // 的音乐叫醒。AEC 在本场景没有价值：朗读期间识别麦是关的，不存在回声路径。
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: true, autoGainControl: true },
      });
      var Ctx = window.AudioContext || window.webkitAudioContext;
      audioCtx = new Ctx();
      await audioCtx.audioWorklet.addModule(workletUrl);
      var source = audioCtx.createMediaStreamSource(micStream);
      workletNode = new AudioWorkletNode(audioCtx, "voice-pcm-downsampler", {
        processorOptions: { targetRate: 16000, frameBytes: 3200 },
      });
      workletNode.port.onmessage = function (e) { sendAudio(e.data); };
      source.connect(workletNode);
      // 不接 destination：避免麦克风回放到扬声器形成回环。
    }
    var setupAudio = opts.setupAudio || setupAudioReal;

    async function start() {
      if (ws || starting) return;   // 抗重入【A5】
      starting = true;
      deliberate = false;
      started = false;
      pendingFrames = [];
      lastInterim = "";
      var myGen = generation;       // 捕获代号；await 后变了说明已被 abort【A7】
      var cfg, tok;
      try {
        cfg = getConfig ? getConfig() : null;
        tok = getToken ? await getToken() : null;
      } catch (err) {
        if (myGen !== generation) return;   // 已被 abort：用户主动，非错误
        fail("token", (err && err.message) || "获取 Token 失败");
        return;
      }
      if (myGen !== generation) return;
      if (!cfg || !cfg.appkey || !cfg.endpoint || !tok || !tok.token) {
        fail("config", "阿里云配置或 Token 缺失");
        return;
      }
      try {
        await setupAudio();
      } catch (err) {
        if (myGen !== generation) { cleanupAudio(); return; }   // abort 发生在开麦期间：关麦退出
        fail("mic", (err && err.name) || (err && err.message) || "麦克风初始化失败");
        return;
      }
      if (myGen !== generation) { cleanupAudio(); return; }
      taskId = makeId();
      var sep = cfg.endpoint.indexOf("?") >= 0 ? "&" : "?";
      var url = cfg.endpoint + sep + "token=" + encodeURIComponent(tok.token);
      if (myGen !== generation) { cleanupAudio(); return; }   // new ws 前最后一道闸【A7】
      var sock;
      try { sock = new WebSocketImpl(url); }
      catch (err) {
        cleanupAudio();
        fail("ws", (err && err.message) || "WebSocket 创建失败");
        return;
      }
      ws = sock;
      starting = false;   // ws 已就位，此后以 sock===ws 判定连接归属【A5】
      sock.binaryType = "arraybuffer";
      sock.onopen = function () {
        if (sock !== ws) return;
        if (sock.readyState !== 1) return;
        try { sock.send(JSON.stringify(buildStartCommand(cfg.appkey, taskId))); }
        catch (_) { fail("ws", "发送 StartTranscription 失败"); }
      };
      sock.onmessage = function (ev) {
        if (sock !== ws) return;
        if (typeof ev.data !== "string") return;   // 识别只下发 Text frame
        var obj;
        try { obj = JSON.parse(ev.data); } catch (_) { return; }
        var parsed = parseAliyunEvent(obj);
        switch (parsed.kind) {
          case "started":
            started = true;
            flushPendingFrames();
            onStarted();
            break;
          case "interim":
            if (parsed.text) { lastInterim = parsed.text; onInterim(parsed.text); }
            break;
          case "final":
            // SentenceEnd 即整句【A6】：直接上报，去抖一概不叠。
            lastInterim = "";
            if (parsed.text) onFinal(parsed.text);
            break;
          case "failed":
            fail("aliyun-task-failed", parsed.text);
            break;
          // completed：主动停止后的收尾，主动路径已 teardown，不重复处理。
        }
      };
      sock.onerror = function () {
        if (sock !== ws) return;
        fail("ws", "WebSocket 错误");
      };
      sock.onclose = function () {
        if (sock !== ws) return;
        if (deliberate) return;
        deliberate = true;
        teardown(true);      // 意外断开：上报 onEnded，核心续听接力
        deliberate = false;
      };
    }

    // 主动停止：优雅发 StopTranscription 后整链拆掉。不触发 onEnded。幂等。
    function stop() {
      generation++;          // 作废 await 中的 in-flight start【A7】
      deliberate = true;
      var cfg = null;
      try { cfg = getConfig ? getConfig() : null; } catch (_) {}
      if (ws && ws.readyState === 1 && cfg && taskId) {
        try { ws.send(JSON.stringify(buildStopCommand(cfg.appkey, taskId))); } catch (_) {}
      }
      teardown(false);
      deliberate = false;
    }

    // 卡死自愈：拆掉重开（核心 starting 超时调用）。
    function rebuild() {
      stop();
      start();
    }

    // 点屏立即发送：发当前未定 interim（SentenceEnd 自己会发，这里兜未断句的尾巴）。
    function flushNow() {
      var text = (lastInterim || "").trim();
      lastInterim = "";
      if (text) {
        stop();
        onFinal(text);
      }
      return text;
    }

    function busy() { return Boolean(ws) || starting; }

    return {
      start: start, stop: stop, rebuild: rebuild, flushNow: flushNow, busy: busy,
      startTimeoutMs: opts.startTimeoutMs != null ? opts.startTimeoutMs : 12000,   //【A5】
      name: "aliyun",
    };
  }

  createAliyunRecognizer.parseAliyunEvent = parseAliyunEvent;
  createAliyunRecognizer.makeId = makeId;
  createAliyunRecognizer.buildStartCommand = buildStartCommand;
  createAliyunRecognizer.buildStopCommand = buildStopCommand;
  return createAliyunRecognizer;
});
