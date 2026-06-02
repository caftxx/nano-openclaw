/* 阿里云实时语音识别引擎 —— 浏览器经 WebSocket 直连阿里云 NLS 网关。
 *
 * 作为浏览器内置 Web Speech API（SpeechRecognition）的可选替代：后端 /api/voice/config
 * 报告 available 且环境支持 getUserMedia + AudioWorklet 时，voice-mode.js 用本引擎，
 * 否则回退 Web Speech。引擎只负责「音频采集 → 阿里云协议 → interim/final 回调」，
 * 不碰 UI/去抖/状态机——那些复用 voice-mode.js 现有逻辑，把回调接到 v.acc.feed 即可。
 *
 * 阿里云协议（SpeechTranscriber namespace）：
 *   - 连 wss://...?token=<临时Token>
 *   - 发 StartTranscription（Text frame，JSON）→ 收 TranscriptionStarted 后才能送音频
 *   - 音频为 Binary frame（PCM 16k/16bit/单声道，每帧约 3200B，由 worklet 切好）
 *   - 收事件（Text frame）：SentenceBegin / TranscriptionResultChanged（中间结果）/
 *     SentenceEnd（一句结束）/ TranscriptionCompleted（StopTranscription 后）
 *   - message_id 每条消息重新随机 32 hex；task_id 整个会话保持一致 32 hex。
 *
 * UMD 导出：既能被 node --test require（单测 parseAliyunEvent / makeId 等纯函数），
 * 也在浏览器挂 window.createAliyunRecognizer。
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.createAliyunRecognizer = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var HEX = "0123456789abcdef";
  // 生成 32 个 hex 字符的 id（message_id / task_id 用）。浏览器优先用 crypto 随机，
  // 没有则退回 Math.random（id 只需唯一、非安全敏感）。
  function makeId(randomFn) {
    var out = "";
    if (randomFn) {
      for (var i = 0; i < 32; i++) out += HEX[randomFn() & 15];
      return out;
    }
    var cryptoObj = typeof crypto !== "undefined" ? crypto : null;
    if (cryptoObj && cryptoObj.getRandomValues) {
      var buf = new Uint8Array(16);
      cryptoObj.getRandomValues(buf);
      for (var j = 0; j < 16; j++) out += HEX[buf[j] >> 4] + HEX[buf[j] & 15];
      return out;
    }
    for (var k = 0; k < 32; k++) out += HEX[(Math.random() * 16) | 0];
    return out;
  }

  // 纯解析：把一条阿里云事件 JSON 映射为 {kind, text}，供单测与回调分发共用。
  // kind: 'started' | 'interim' | 'final' | 'completed' | 'failed' | 'other'
  function parseAliyunEvent(obj) {
    var header = (obj && obj.header) || {};
    var name = header.name;
    var payload = (obj && obj.payload) || {};
    switch (name) {
      case "TranscriptionStarted":
        return { kind: "started", text: "" };
      case "TranscriptionResultChanged":
        return { kind: "interim", text: payload.result || "" };
      case "SentenceEnd":
        return { kind: "final", text: payload.result || "" };
      case "TranscriptionCompleted":
        return { kind: "completed", text: "" };
      case "TaskFailed":
        return { kind: "failed", text: header.status_text || "task failed" };
      default:
        return { kind: "other", text: "" };
    }
  }

  // 构造 StartTranscription 指令帧（Text frame 的 JSON），task_id 由会话传入保持一致。
  function buildStartCommand(appkey, taskId, makeMsgId) {
    return {
      header: {
        appkey: appkey,
        message_id: (makeMsgId || makeId)(),
        task_id: taskId,
        namespace: "SpeechTranscriber",
        name: "StartTranscription",
      },
      payload: {
        format: "pcm",
        sample_rate: 16000,
        enable_intermediate_result: true,
        enable_punctuation_prediction: true,
        enable_inverse_text_normalization: true,
      },
    };
  }

  function buildStopCommand(appkey, taskId, makeMsgId) {
    return {
      header: {
        appkey: appkey,
        message_id: (makeMsgId || makeId)(),
        task_id: taskId,
        namespace: "SpeechTranscriber",
        name: "StopTranscription",
      },
    };
  }

  // 浏览器侧工厂：opts = { getConfig, getToken, onStart, onInterim, onFinal, onError, onEnd,
  //                       WebSocketImpl?, setupAudio? }
  // WebSocketImpl/setupAudio 仅为测试注入点（默认走全局 WebSocket / 真实音频建立），不改线上行为。
  function createAliyunRecognizer(opts) {
    opts = opts || {};
    var getConfig = opts.getConfig;   // () -> {appkey, endpoint}
    var getToken = opts.getToken;     // async () -> {token, ...}
    var onStart = opts.onStart || function () {};
    var onInterim = opts.onInterim || function () {};
    var onFinal = opts.onFinal || function () {};
    var onError = opts.onError || function () {};
    var onEnd = opts.onEnd || function () {};
    var WebSocketImpl = opts.WebSocketImpl || (typeof WebSocket !== "undefined" ? WebSocket : null);

    var ws = null;
    var starting = false;     // 进入 start() 到 new WebSocket 成功这段中间态（抗重入）
    var audioCtx = null;
    var workletNode = null;
    var micStream = null;
    var taskId = "";
    var started = false;      // 是否已收到 TranscriptionStarted（可发音频）
    var stopping = false;     // 主动停止中，忽略后续事件/避免重复清理
    var pendingFrames = [];   // TranscriptionStarted 之前先攒下音频帧

    function cleanupAudio() {
      try { if (workletNode) workletNode.disconnect(); } catch (_) {}
      try {
        if (micStream) micStream.getTracks().forEach(function (t) { t.stop(); });
      } catch (_) {}
      try { if (audioCtx) audioCtx.close(); } catch (_) {}
      workletNode = null;
      micStream = null;
      audioCtx = null;
    }

    function finish() {
      starting = false;
      cleanupAudio();
      try { if (ws && ws.readyState <= 1) ws.close(); } catch (_) {}
      ws = null;
      pendingFrames = [];
      onEnd();
    }

    function fail(name, msg) {
      starting = false;   // 建立阶段抛错也要复位重入守卫，否则之后再也起不来
      if (stopping) return;
      stopping = true;
      onError(name, msg);
      finish();
    }

    function sendAudio(buf) {
      if (!ws || ws.readyState !== 1) return;
      if (!started) { pendingFrames.push(buf); return; }   // 还没 Started，先攒着
      try { ws.send(buf); } catch (_) {}
    }

    function flushPending() {
      if (!ws || ws.readyState !== 1) return;
      for (var i = 0; i < pendingFrames.length; i++) {
        try { ws.send(pendingFrames[i]); } catch (_) {}
      }
      pendingFrames = [];
    }

    // sock：本条消息所属的连接。已被新连接取代的旧 socket 回调一律忽略，避免污染当前会话。
    function onWsMessage(sock, ev) {
      if (sock !== ws) return;
      if (typeof ev.data !== "string") return;   // 识别只下发 Text frame
      var obj;
      try { obj = JSON.parse(ev.data); } catch (_) { return; }
      var parsed = parseAliyunEvent(obj);
      switch (parsed.kind) {
        case "started":
          started = true;
          flushPending();
          onStart();
          break;
        case "interim":
          if (parsed.text) onInterim(parsed.text);
          break;
        case "final":
          if (parsed.text) onFinal(parsed.text);
          break;
        case "completed":
          // StopTranscription 后的收尾——主动停止时由 abort() 走 finish，这里不重复。
          break;
        case "failed":
          fail("aliyun-task-failed", parsed.text);
          break;
      }
    }

    async function setupAudioReal() {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      var Ctx = window.AudioContext || window.webkitAudioContext;
      audioCtx = new Ctx();
      // worklet 模块由 JS 内部按 URL 加载（不在 html 引）；与本文件同目录。
      await audioCtx.audioWorklet.addModule("/static/voice-pcm-worklet.js");
      var source = audioCtx.createMediaStreamSource(micStream);
      workletNode = new AudioWorkletNode(audioCtx, "pcm-downsampler", {
        processorOptions: { targetRate: 16000, frameBytes: 3200 },
      });
      workletNode.port.onmessage = function (e) { sendAudio(e.data); };
      source.connect(workletNode);
      // 不接 destination：避免把麦克风回放到扬声器形成回环。
    }
    var setupAudio = opts.setupAudio || setupAudioReal;

    async function start() {
      // 实例级抗重入：已有活动连接（ws）或正在建立（starting）时，绝不开第二条 ws。
      // start() 是 async（中间 await getToken + setupAudio 的 getUserMedia 授权框可能数秒），
      // 这段窗口里被重复进入会让旧/新 socket 互相覆盖、污染 → 这里直接忽略本次调用。
      if (ws || starting) return;
      starting = true;
      stopping = false;
      started = false;
      pendingFrames = [];
      var cfg, tok;
      try {
        cfg = getConfig ? getConfig() : null;
        tok = getToken ? await getToken() : null;
      } catch (err) {
        fail("token", (err && err.message) || "获取 Token 失败");
        return;
      }
      if (!cfg || !cfg.appkey || !cfg.endpoint || !tok || !tok.token) {
        fail("config", "阿里云配置或 Token 缺失");
        return;
      }
      try {
        await setupAudio();
      } catch (err) {
        // getUserMedia 被拒 / AudioWorklet 不支持等
        fail("mic", (err && err.name) || (err && err.message) || "麦克风初始化失败");
        return;
      }
      taskId = makeId();
      var sep = cfg.endpoint.indexOf("?") >= 0 ? "&" : "?";
      var url = cfg.endpoint + sep + "token=" + encodeURIComponent(tok.token);
      var sock;
      try {
        sock = new WebSocketImpl(url);
      } catch (err) {
        fail("ws", (err && err.message) || "WebSocket 创建失败");
        return;
      }
      ws = sock;
      starting = false;   // ws 已就位，之后以 sock===ws 作为「这条连接仍是当前连接」的判据
      sock.binaryType = "arraybuffer";
      // 回调全部闭包捕获本条 sock，首行 sock!==ws 守卫：被取代的旧连接回调一律不动作。
      sock.onopen = function () {
        if (sock !== ws) return;
        if (sock.readyState !== 1) return;
        try { sock.send(JSON.stringify(buildStartCommand(cfg.appkey, taskId))); }
        catch (err) { fail("ws", "发送 StartTranscription 失败"); }
      };
      sock.onmessage = function (ev) { onWsMessage(sock, ev); };
      sock.onerror = function () { if (sock !== ws) return; fail("ws", "WebSocket 错误"); };
      sock.onclose = function () {
        if (sock !== ws) return;
        // 非主动停止时的意外断开：上报并收尾（onEnd 让状态机回到聆听/空闲）。
        if (stopping) return;
        stopping = true;
        cleanupAudio();
        ws = null;
        onEnd();
      };
    }

    // 主动结束：尽量优雅发 StopTranscription，再关闭 ws + 停麦 + 关 worklet。
    function abort() {
      if (stopping) return;
      stopping = true;
      var cfg = null;
      try { cfg = getConfig ? getConfig() : null; } catch (_) {}
      if (ws && ws.readyState === 1 && cfg && taskId) {
        try { ws.send(JSON.stringify(buildStopCommand(cfg.appkey, taskId))); } catch (_) {}
      }
      finish();
    }

    return { start: start, abort: abort };
  }

  // 暴露纯函数以便单测（require 后从导出对象取）。
  createAliyunRecognizer.parseAliyunEvent = parseAliyunEvent;
  createAliyunRecognizer.makeId = makeId;
  createAliyunRecognizer.buildStartCommand = buildStartCommand;
  createAliyunRecognizer.buildStopCommand = buildStopCommand;
  return createAliyunRecognizer;
});
