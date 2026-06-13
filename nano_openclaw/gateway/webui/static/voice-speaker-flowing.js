/* 阿里云流式语音合成引擎 —— 浏览器经 WebSocket 直连阿里云 NLS 网关。
 *
 * 回退链（voice-speaker-fallback.js 组装）的首选级。仅商用版可开通、不支持试用【B3】，
 * 失败由组合器降级到 RESTful 代理。本引擎只负责「协议 + 投递音频字节与生命周期事件」，
 * 不碰 Web Audio——播放交给组合器持有的 voice-pcm-player.js。
 *
 * 阿里云协议（FlowingSpeechSynthesizer namespace，标准音色 / CosyVoice 通用）：
 *   - 连同一 NLS 网关 wss://...?token=<临时Token>（与识别共用 Token，namespace 区分）
 *   - 发 StartSynthesis → 收 SynthesisStarted 后才能发文本
 *   - RunSynthesis：流式可多次投 payload {text}
 *   - StopSynthesis：文本流结束后必须发，否则缓存文本丢失
 *   - 下行 Binary frame = PCM 音频分帧（无文件头）；Text frame = 生命周期事件
 *     SynthesisStarted / SynthesisCompleted（音频全部下发完）/ TaskFailed；
 *     SentenceBegin/Synthesis/End 为字级时间戳事件，忽略。
 *
 * 历史坑位（语义必须保持）：
 *  - 【B6】TaskFailed 的失败原因在 header.status_message（不是 ASR 的 status_text）——
 *    抄 ASR 读错字段会把真实原因丢成 "task failed"。优先 status_message，
 *    回退 status_text，最后兜底文案。
 *  - 抗重入 / generation 中止令牌 / sock!==ws 隔离：与识别引擎同款（async begin 的
 *    await 窗口同样会被重复进入、被 abort 横插）。
 *  - started 前 push 的文本先攒队列、started 前调过 end() 要在 started 后补发
 *    StopSynthesis——首段文本往往先于 SynthesisStarted 到达。
 *
 * UMD：WebSocketImpl 可注入；纯函数挂工厂上供 node --test。
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory(require("./voice-nls.js"));
  else root.createFlowingSpeaker = factory(root.VoiceNls);
})(typeof self !== "undefined" ? self : this, function (nls) {
  "use strict";

  var makeId = nls.makeId;

  // 纯解析：合成事件 JSON → {kind, text}。kind: 'started'|'completed'|'failed'|'other'
  function parseTtsEvent(obj) {
    var header = (obj && obj.header) || {};
    switch (header.name) {
      case "SynthesisStarted": return { kind: "started", text: "" };
      case "SynthesisCompleted": return { kind: "completed", text: "" };
      case "TaskFailed": return { kind: "failed", text: nls.failureText(header) };   //【B6】
      default: return { kind: "other", text: "" };
    }
  }

  function buildStartSynthesis(appkey, taskId, opts, makeMsgId) {
    opts = opts || {};
    return nls.envelope(appkey, taskId, "FlowingSpeechSynthesizer", "StartSynthesis", {
      voice: opts.voice,
      format: "pcm",
      sample_rate: opts.sampleRate,
      volume: 50,
      speech_rate: 0,
      pitch_rate: 0,
    }, makeMsgId);
  }

  function buildRunSynthesis(appkey, taskId, text, makeMsgId) {
    return nls.envelope(appkey, taskId, "FlowingSpeechSynthesizer", "RunSynthesis", { text: text }, makeMsgId);
  }

  function buildStopSynthesis(appkey, taskId, makeMsgId) {
    return nls.envelope(appkey, taskId, "FlowingSpeechSynthesizer", "StopSynthesis", undefined, makeMsgId);
  }

  // 工厂：opts = { getConfig, getToken, onAudio, onCompleted, onError, WebSocketImpl }
  //   getConfig() -> {appkey, endpoint, voice, sampleRate}
  function createFlowingSpeaker(opts) {
    opts = opts || {};
    var getConfig = opts.getConfig;
    var getToken = opts.getToken;
    var onAudio = opts.onAudio || function () {};
    var onCompleted = opts.onCompleted || function () {};
    var onError = opts.onError || function () {};
    var WS = opts.WebSocketImpl || (typeof WebSocket !== "undefined" ? WebSocket : null);

    var ws = null;
    var starting = false;
    var generation = 0;
    var started = false;        // 已收 SynthesisStarted（可发 RunSynthesis）
    var taskId = "";
    var appkey = "";
    var pendingTexts = [];      // Started 前先攒文本
    var endRequested = false;   // Started 前调过 end()：Started 后补发 StopSynthesis
    var directive = null;

    function reset() {
      ws = null;
      starting = false;
      started = false;
      taskId = "";
      pendingTexts = [];
      endRequested = false;
    }

    function sendStop() {
      if (!ws || ws.readyState !== 1) return;
      try { ws.send(JSON.stringify(buildStopSynthesis(appkey, taskId))); } catch (_) {}
    }

    function flushPendingTexts() {
      if (!ws || ws.readyState !== 1) return;
      for (var i = 0; i < pendingTexts.length; i++) {
        try { ws.send(JSON.stringify(buildRunSynthesis(appkey, taskId, pendingTexts[i]))); } catch (_) {}
      }
      pendingTexts = [];
    }

    async function begin(nextDirective) {
      if (ws || starting) return;   // 抗重入
      directive = nextDirective || null;
      starting = true;
      started = false;
      endRequested = false;
      pendingTexts = [];
      var myGen = generation;
      var cfg, tok;
      try {
        cfg = getConfig ? getConfig() : null;
        tok = getToken ? await getToken() : null;
      } catch (err) {
        if (myGen !== generation) return;   // 已被 abort：主动行为非错误
        starting = false;
        onError("token", (err && err.message) || "获取 Token 失败");
        return;
      }
      if (myGen !== generation) return;
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
      if (myGen !== generation) return;   // new ws 前最后一道闸
      var sock;
      try { sock = new WS(url); }
      catch (err) {
        starting = false;
        onError("ws", (err && err.message) || "WebSocket 创建失败");
        return;
      }
      ws = sock;
      starting = false;
      try { sock.binaryType = "arraybuffer"; } catch (_) {}

      sock.onopen = function () {
        if (sock !== ws) return;
        try {
          sock.send(JSON.stringify(
            buildStartSynthesis(appkey, taskId, {
              voice: (directive && (directive.voiceId || directive.voice)) || cfg.voice,
              sampleRate: cfg.sampleRate,
            })
          ));
        } catch (_) { onError("ws", "发送 StartSynthesis 失败"); }
      };
      sock.onmessage = function (ev) {
        if (sock !== ws) return;
        if (typeof ev.data !== "string") { onAudio(ev.data); return; }   // Binary = PCM
        var obj;
        try { obj = JSON.parse(ev.data); } catch (_) { return; }
        var parsed = parseTtsEvent(obj);
        switch (parsed.kind) {
          case "started":
            started = true;
            flushPendingTexts();
            if (endRequested) sendStop();   // 文本已投完且早调过 end()：补发结束信号
            break;
          case "completed":
            onCompleted();                  // 音频全部下发完；播放收尾由播放器 drain 触发
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
        reset();   // 意外断开：复位以便下轮能重启
      };
    }

    // 投递一段文本。未建连自动 begin；未 started 先入队。
    function push(text, nextDirective) {
      if (!text) return;
      if (!ws && !starting) begin(nextDirective);
      if (!started || !ws || ws.readyState !== 1) {
        pendingTexts.push(text);
        return;
      }
      try { ws.send(JSON.stringify(buildRunSynthesis(appkey, taskId, text))); } catch (_) {}
    }

    // 结束文本流：已 started 直接发 StopSynthesis，否则置位等 started 补发。
    function end() {
      if (started && ws && ws.readyState === 1) { sendStop(); return; }
      endRequested = true;
    }

    // 主动中止：作废 in-flight begin、关 ws、清队列。幂等。
    function abort() {
      generation++;
      var sock = ws;
      ws = null;   // 先摘引用：迟到回调 sock!==ws 直接 return
      try { if (sock && sock.readyState <= 1) sock.close(); } catch (_) {}
      reset();
    }

    return { begin: begin, push: push, end: end, abort: abort, name: "aliyun-flowing" };
  }

  createFlowingSpeaker.parseTtsEvent = parseTtsEvent;
  createFlowingSpeaker.makeId = makeId;
  createFlowingSpeaker.buildStartSynthesis = buildStartSynthesis;
  createFlowingSpeaker.buildRunSynthesis = buildRunSynthesis;
  createFlowingSpeaker.buildStopSynthesis = buildStopSynthesis;
  return createFlowingSpeaker;
});
