/* 阿里云流式语音合成引擎 —— 浏览器经 WebSocket 直连阿里云 NLS 网关。
 *
 * 作为浏览器 speechSynthesis 的可选朗读输出：后端 /api/voice/config 报告
 * tts.enabled 且选中阿里云引擎时，voice-mode.js 用本引擎，否则回退 speechSynthesis。
 * 引擎只负责「协议 + 投递音频字节与生命周期事件」，不碰 Web Audio——播放交给
 * voice-mode.js 持有的 voice-pcm-player.js（保持本文件 node 可测、与 ASR 同结构）。
 *
 * 阿里云协议（FlowingSpeechSynthesizer namespace，标准音色 / CosyVoice 大模型音色通用）：
 *   - 连同一网关 wss://...?token=<临时Token>（与识别共用 Token，靠 namespace 区分）
 *   - 发 StartSynthesis（Text frame，JSON）→ 收 SynthesisStarted 后才能发文本
 *   - RunSynthesis（Text frame）：流式可多次发，payload {text}
 *   - StopSynthesis（Text frame）：文本流结束后必须发，否则缓存文本丢失
 *   - 下行音频为 Binary frame：同一完整音频被分帧；pcm 格式无文件头，直接把每帧
 *     Int16 小端 PCM 追加进流式播放器
 *   - 下行事件（Text frame）：SynthesisStarted / SynthesisCompleted（StopSynthesis 后到达，
 *     表示音频全部下发完）/ TaskFailed；SentenceBegin/Synthesis/End 为字级时间戳事件，本实现不需要，忽略
 *   - message_id 每条消息重新随机 32 hex；task_id 整个会话保持一致 32 hex。
 *
 * UMD 导出：既能被 node --test require（单测 parseTtsEvent / makeId / buildXxx 纯函数），
 * 也在浏览器挂 window.createAliyunSynthesizer。
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.createAliyunSynthesizer = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var HEX = "0123456789abcdef";
  // 生成 32 个 hex 字符的 id（message_id / task_id 用）。与 ASR 同实现，文件自包含。
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

  // 纯解析：把一条阿里云合成事件 JSON 映射为 {kind, text}，供单测与回调分发共用。
  // kind: 'started' | 'completed' | 'failed' | 'other'（句级时间戳事件归 other 忽略）
  function parseTtsEvent(obj) {
    var header = (obj && obj.header) || {};
    var name = header.name;
    switch (name) {
      case "SynthesisStarted":
        return { kind: "started", text: "" };
      case "SynthesisCompleted":
        return { kind: "completed", text: "" };
      case "TaskFailed":
        // 阿里云合成事件里失败原因在 header.status_message（不是 ASR 的 status_text），
        // 优先读它；老/异常报文无 status_message 时回退 status_text，最后兜底文案。
        return { kind: "failed", text: header.status_message || header.status_text || "task failed" };
      default:
        return { kind: "other", text: "" };
    }
  }

  // 构造 StartSynthesis 指令帧（Text frame 的 JSON），task_id 由会话传入保持一致。
  // opts = { voice, sampleRate }
  function buildStartSynthesis(appkey, taskId, opts, makeMsgId) {
    opts = opts || {};
    return {
      header: {
        appkey: appkey,
        message_id: (makeMsgId || makeId)(),
        task_id: taskId,
        namespace: "FlowingSpeechSynthesizer",
        name: "StartSynthesis",
      },
      payload: {
        voice: opts.voice,
        format: "pcm",
        sample_rate: opts.sampleRate,
        volume: 50,
        speech_rate: 0,
        pitch_rate: 0,
      },
    };
  }

  // 构造 RunSynthesis 指令帧：流式投递一段文本。
  function buildRunSynthesis(appkey, taskId, text, makeMsgId) {
    return {
      header: {
        appkey: appkey,
        message_id: (makeMsgId || makeId)(),
        task_id: taskId,
        namespace: "FlowingSpeechSynthesizer",
        name: "RunSynthesis",
      },
      payload: { text: text },
    };
  }

  // 构造 StopSynthesis 指令帧：文本流结束信号，无 payload。
  function buildStopSynthesis(appkey, taskId, makeMsgId) {
    return {
      header: {
        appkey: appkey,
        message_id: (makeMsgId || makeId)(),
        task_id: taskId,
        namespace: "FlowingSpeechSynthesizer",
        name: "StopSynthesis",
      },
    };
  }

  // 浏览器侧工厂：
  //   opts = { getConfig, getToken, onAudio, onStart, onComplete, onError, WebSocketImpl }
  function createAliyunSynthesizer(opts) {
    opts = opts || {};
    var getConfig = opts.getConfig;   // () -> {appkey, endpoint, voice, sampleRate}
    var getToken = opts.getToken;     // async () -> {token, ...}
    var onAudio = opts.onAudio || function () {};
    var onStart = opts.onStart || function () {};
    var onComplete = opts.onComplete || function () {};
    var onError = opts.onError || function () {};
    var WS = opts.WebSocketImpl || (typeof WebSocket !== "undefined" ? WebSocket : null);

    var ws = null;
    var starting = false;       // 已发起 begin() 但 ws 还没建好的中间态——堵二次 begin 竞态
    var started = false;        // 是否已收到 SynthesisStarted（可发 RunSynthesis）
    var stopping = false;       // 主动 abort 中，忽略后续事件
    var taskId = "";
    var appkey = "";
    var pendingTexts = [];      // SynthesisStarted 之前先攒下待合成文本
    var endRequested = false;   // started 之前调过 end()：等 started 后补发 StopSynthesis

    function reset() {
      ws = null;
      starting = false;
      started = false;
      taskId = "";
      pendingTexts = [];
      endRequested = false;
    }

    // 发 StopSynthesis（结束文本流），仅在确有 ws + taskId 时。
    function sendStop() {
      if (!ws || ws.readyState !== 1) return;
      try { ws.send(JSON.stringify(buildStopSynthesis(appkey, taskId))); } catch (_) {}
    }

    // started 后把攒下的文本逐条 RunSynthesis 发出。
    function flushPending() {
      if (!ws || ws.readyState !== 1) return;
      for (var i = 0; i < pendingTexts.length; i++) {
        try { ws.send(JSON.stringify(buildRunSynthesis(appkey, taskId, pendingTexts[i]))); } catch (_) {}
      }
      pendingTexts = [];
    }

    async function begin() {
      if (ws || starting) return;   // 抗重入：已在跑/启动中就不再建第二条 ws
      starting = true;
      started = false;
      stopping = false;
      endRequested = false;
      pendingTexts = [];
      var cfg, tok;
      try {
        cfg = getConfig ? getConfig() : null;
        tok = getToken ? await getToken() : null;
      } catch (err) {
        starting = false;
        onError("token", (err && err.message) || "获取 Token 失败");
        return;
      }
      if (!cfg || !cfg.appkey || !cfg.endpoint || !tok || !tok.token) {
        starting = false;
        onError("config", "阿里云配置或 Token 缺失");
        return;
      }
      if (!WS) {
        starting = false;
        onError("ws", "WebSocket 不可用");
        return;
      }
      appkey = cfg.appkey;
      taskId = makeId();
      var sep = cfg.endpoint.indexOf("?") >= 0 ? "&" : "?";
      var url = cfg.endpoint + sep + "token=" + encodeURIComponent(tok.token);
      var sock;
      try {
        sock = new WS(url);
      } catch (err) {
        starting = false;
        onError("ws", (err && err.message) || "WebSocket 创建失败");
        return;
      }
      ws = sock;
      starting = false;
      try { sock.binaryType = "arraybuffer"; } catch (_) {}

      sock.onopen = function () {
        if (sock !== ws) return;   // 已被 abort 取代的旧 socket，迟到回调一律忽略
        try {
          sock.send(JSON.stringify(
            buildStartSynthesis(appkey, taskId, { voice: cfg.voice, sampleRate: cfg.sampleRate })
          ));
        } catch (err) {
          onError("ws", "发送 StartSynthesis 失败");
        }
      };
      sock.onmessage = function (ev) {
        if (sock !== ws) return;
        // 二进制帧 = 音频 PCM；直接投给上层播放器。
        if (typeof ev.data !== "string") { onAudio(ev.data); return; }
        var obj;
        try { obj = JSON.parse(ev.data); } catch (_) { return; }
        var parsed = parseTtsEvent(obj);
        switch (parsed.kind) {
          case "started":
            started = true;
            flushPending();
            if (endRequested) sendStop();   // 文本已全部投完且早调过 end()：补发结束信号
            onStart();
            break;
          case "completed":
            // 音频已全部下发完；优雅关 ws，播放收尾由播放器 drain 触发。
            onComplete();
            try { if (ws && ws.readyState <= 1) ws.close(); } catch (_) {}
            reset();
            break;
          case "failed":
            onError("aliyun-task-failed", parsed.text);
            try { if (ws && ws.readyState <= 1) ws.close(); } catch (_) {}
            reset();
            break;
        }
      };
      sock.onerror = function () {
        if (sock !== ws) return;
        onError("ws", "WebSocket 错误");
      };
      sock.onclose = function () {
        if (sock !== ws) return;
        // 意外断开（非主动 abort/completed）：收尾复位，让状态机能重启。
        reset();
      };
    }

    // 投递一段待合成文本。空文本忽略；未建连则自动 begin；未 started 先入队。
    function push(text) {
      if (!text) return;
      if (!ws && !starting) { begin(); }
      if (!started || !ws || ws.readyState !== 1) {
        pendingTexts.push(text);
        return;
      }
      try { ws.send(JSON.stringify(buildRunSynthesis(appkey, taskId, text))); } catch (_) {}
    }

    // 结束文本流：已 started 直接发 StopSynthesis，否则置位等 started 后补发。
    function end() {
      if (started && ws && ws.readyState === 1) { sendStop(); return; }
      endRequested = true;
    }

    // 主动中止：关 ws + 清队列，幂等可重复调不抛。被取代的旧 socket 迟到回调靠
    // 各处 sock!==ws 守卫拦截，故这里无需逐一摘 handler。
    function abort() {
      stopping = true;
      var sock = ws;
      ws = null;   // 先摘引用，使任何迟到回调 sock!==ws 直接 return
      try { if (sock && sock.readyState <= 1) sock.close(); } catch (_) {}
      reset();
      stopping = false;
    }

    return { begin: begin, push: push, end: end, abort: abort };
  }

  // 暴露纯函数以便单测（require 后从导出对象取）。
  createAliyunSynthesizer.parseTtsEvent = parseTtsEvent;
  createAliyunSynthesizer.makeId = makeId;
  createAliyunSynthesizer.buildStartSynthesis = buildStartSynthesis;
  createAliyunSynthesizer.buildRunSynthesis = buildRunSynthesis;
  createAliyunSynthesizer.buildStopSynthesis = buildStopSynthesis;
  return createAliyunSynthesizer;
});
