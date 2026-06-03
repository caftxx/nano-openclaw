/* 阿里云 RESTful 语音合成引擎 —— 经后端代理 /api/voice/tts（POST JSON）合成。
 *
 * 作为流式合成（voice-tts-aliyun.js）不可用/失败时的自动备选：流式仅商用版可用、
 * 不支持试用版，未开通账号每轮首句会 TaskFailed；RESTful 是标准「语音合成」产品，
 * 试用版亦可用。回退链：流式 →（不可用/失败）RESTful 代理 →（再失败）浏览器本地。
 *
 * 为何经后端代理：阿里云 RESTful 文档明确「不支持纯 JavaScript 直接调用」——CORS
 * 跨域 + 泄露 appkey 风险。故浏览器只 POST 文本到本端 /api/voice/tts，由后端带
 * 临时 Token + appkey 调阿里云，把 PCM 音频字节回流。浏览器永不接触 AK/SK/appkey。
 *
 * 为何串行：RESTful 一次请求合一段音频、整段返回（或流式分块），多段之间必须按文本
 * 顺序串行播放，否则音频会乱序。内部 FIFO 队列 + 单跑泵保证顺序。音频字节投给上层
 * voice-pcm-player.js 播放（与流式引擎同结构，保持本文件 node 可测）。
 *
 * 接口与流式引擎一致：{begin, push, end, abort}，voice-mode.js 通过 currentCloudTts()
 * 在两者间统一切换。
 *
 * UMD 导出：既能被 node --test require（单测 splitForTts 纯函数 + 注入 fetch 跑泵），
 * 也在浏览器挂 window.createRestfulSynthesizer。
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.createRestfulSynthesizer = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var MAX_LEN = 300;   // 阿里云 RESTful 单次文本上限 300 字符，超出会被截断。
  var SENTENCE_END = /[。！？!?；;\n]/;

  // 把文本切成每段 ≤MAX_LEN 的片段：先在句末标点处切，单段仍超长再硬切到 ≤MAX_LEN。
  function splitForTts(text) {
    if (!text) return [];
    if (text.length <= MAX_LEN) return [text];
    var out = [];
    var buf = "";
    for (var i = 0; i < text.length; i++) {
      buf += text[i];
      if (SENTENCE_END.test(text[i]) && buf.length >= MAX_LEN) {
        out.push(buf);
        buf = "";
      } else if (buf.length >= MAX_LEN) {
        // 没遇到句末标点但已到上限：硬切，避免超过 300 被服务端截断。
        out.push(buf);
        buf = "";
      }
    }
    if (buf) out.push(buf);
    return out;
  }

  // 浏览器侧工厂：
  //   opts = { url, headers, getConfig, onAudio, onStart, onComplete, onError, fetchImpl }
  function createRestfulSynthesizer(opts) {
    opts = opts || {};
    var url = opts.url || "/api/voice/tts";
    var headers = opts.headers || {};
    var getConfig = opts.getConfig;   // () -> {voice, sampleRate}
    var onAudio = opts.onAudio || function () {};
    var onStart = opts.onStart || function () {};
    var onComplete = opts.onComplete || function () {};
    var onError = opts.onError || function () {};
    var fetchImpl = opts.fetchImpl || (typeof fetch !== "undefined" ? fetch : null);

    var queue = [];
    var pumping = false;
    var started = false;        // 是否已触发过 onStart（首段开播时一次）
    var completed = false;       // 是否已触发过 onComplete（仅一次）
    var endRequested = false;    // 调过 end()：队列排空后触发 onComplete
    var aborted = false;         // abort() 后忽略一切迟到回调
    var controller = null;       // AbortController：取消在途请求
    var generation = 0;          // abort 自增，作废 in-flight 的 then/catch

    function reset() {
      queue = [];
      pumping = false;
      started = false;
      completed = false;
      endRequested = false;
      aborted = false;
      controller = (typeof AbortController !== "undefined") ? new AbortController() : null;
    }

    function maybeComplete() {
      if (endRequested && queue.length === 0 && !pumping && !completed && !aborted) {
        completed = true;
        onComplete();
      }
    }

    async function pump() {
      if (pumping) return;
      pumping = true;
      var myGen = generation;
      while (queue.length > 0 && !aborted && myGen === generation) {
        var text = queue.shift();
        var cfg = getConfig ? getConfig() : {};
        var body = {
          text: text,
          voice: cfg && cfg.voice,
          sample_rate: (cfg && cfg.sampleRate) || 16000,
        };
        var reqHeaders = {};
        for (var k in headers) { if (Object.prototype.hasOwnProperty.call(headers, k)) reqHeaders[k] = headers[k]; }
        reqHeaders["Content-Type"] = "application/json";
        var resp;
        try {
          resp = await fetchImpl(url, {
            method: "POST",
            headers: reqHeaders,
            body: JSON.stringify(body),
            signal: controller ? controller.signal : undefined,
          });
        } catch (err) {
          if (aborted || myGen !== generation) return;   // abort 取消的请求：静默退出
          pumping = false;
          var emsg = (err && err.message) || "请求失败";
          queue = [];
          onError("restful", emsg);
          return;
        }
        if (aborted || myGen !== generation) return;
        if (!resp || !resp.ok) {
          pumping = false;
          var reason = "HTTP " + (resp ? resp.status : "?");
          try { if (resp && resp.text) { var t = await resp.text(); if (t) reason = t.slice(0, 200); } } catch (_) {}
          if (aborted || myGen !== generation) return;
          queue = [];
          onError("restful", reason);
          return;
        }
        // 首段成功开播：触发一次 onStart。
        if (!started) { started = true; onStart(); }
        // 读音频：优先流式 reader 边收边投，否则整段 arrayBuffer。
        try {
          if (resp.body && typeof resp.body.getReader === "function") {
            var reader = resp.body.getReader();
            while (true) {
              var r = await reader.read();
              if (r.done) break;
              if (aborted || myGen !== generation) return;
              if (r.value) onAudio(r.value.buffer);
            }
          } else {
            var ab = await resp.arrayBuffer();
            if (aborted || myGen !== generation) return;
            onAudio(ab);
          }
        } catch (err2) {
          if (aborted || myGen !== generation) return;
          pumping = false;
          queue = [];
          onError("restful", (err2 && err2.message) || "读取音频失败");
          return;
        }
      }
      pumping = false;
      if (myGen !== generation) return;
      maybeComplete();
    }

    function begin() {
      reset();
    }

    function push(text) {
      if (!text || aborted) return;
      var parts = splitForTts(text);
      for (var i = 0; i < parts.length; i++) {
        if (parts[i]) queue.push(parts[i]);
      }
      if (!pumping) pump();
    }

    function end() {
      if (aborted) return;
      endRequested = true;
      maybeComplete();   // 泵已空闲且队列空：立即收尾
    }

    function abort() {
      generation++;
      aborted = true;
      queue = [];
      pumping = false;
      try { if (controller) controller.abort(); } catch (_) {}
    }

    // 构造时先建一个 controller，begin() 会重置。
    controller = (typeof AbortController !== "undefined") ? new AbortController() : null;

    return { begin: begin, push: push, end: end, abort: abort };
  }

  // 暴露纯函数以便单测。
  createRestfulSynthesizer.splitForTts = splitForTts;
  return createRestfulSynthesizer;
});
