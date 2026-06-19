/* Talk RESTful 语音合成引擎 —— 经后端代理 /api/talk/speak（POST JSON）合成。
 *
 * 回退链第二级【B3】：流式仅商用版可用、不支持试用，未开通账号每轮首句 TaskFailed；
 * RESTful 是标准「语音合成」产品，试用版亦可用。
 *
 * 为何经后端代理：阿里云 RESTful 文档明确「不支持纯 JavaScript 直接调用」——CORS +
 * 泄露 appkey 风险。浏览器只 POST 文本到本端 /api/talk/speak，后端带临时 Token + appkey
 * 调阿里云回流 PCM。浏览器永不接触 AK/SK/appkey。
 *
 * 为何串行：RESTful 一次请求合一段音频，多段必须按文本顺序串行投放，否则音频乱序。
 * 内部 FIFO 队列 + 单跑泵保证顺序。
 *
 * 历史坑位（语义必须保持）：
 *  - 【B4】reader 的 r.value 是带 byteOffset 的子视图，.buffer 指向更大的底层 buffer，
 *    直接投会取到视图外字节 → 必须按 byteOffset 精确 slice。
 *  - end() 后队列排空才 onCompleted（begin+push 不 end 永不 complete——上游 turn.done
 *    才知道文本流结束）；onCompleted 仅一次。
 *  - abort：AbortController 取消在途请求 + generation 作废迟到 then/catch。
 *
 * UMD：fetchImpl 注入；splitForTts 纯函数挂工厂上供 node --test。
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.createRestSpeaker = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var MAX_LEN = 300;   // 阿里云 RESTful 单次文本上限 300 字符，超出被服务端截断。

  // 切成每段 ≤MAX_LEN：到达上限即切（送进来的本就是状态机按句末标点切好的整句，
  // 只有单句超 300 字才会走到这里，硬切兜底即可）。
  function splitForTts(text) {
    if (!text) return [];
    if (text.length <= MAX_LEN) return [text];
    var out = [];
    var buf = "";
    for (var i = 0; i < text.length; i++) {
      buf += text[i];
      if (buf.length >= MAX_LEN) {
        out.push(buf);
        buf = "";
      }
    }
    if (buf) out.push(buf);
    return out;
  }

  // 工厂：opts = { url, headers, getConfig, onAudio, onCompleted, onError, fetchImpl }
  //   getConfig() -> {voice, sampleRate}
  function createRestSpeaker(opts) {
    opts = opts || {};
    var url = opts.url || "/api/talk/speak";
    var headers = opts.headers || {};
    var getConfig = opts.getConfig;
    var onAudio = opts.onAudio || function () {};
    var onCompleted = opts.onCompleted || function () {};
    var onError = opts.onError || function () {};
    var fetchImpl = opts.fetchImpl || (typeof fetch !== "undefined" ? fetch : null);

    var queue = [];
    var pumping = false;
    var completed = false;
    var endRequested = false;
    var aborted = false;
    var generation = 0;
    var controller = (typeof AbortController !== "undefined") ? new AbortController() : null;

    function reset() {
      queue = [];
      pumping = false;
      completed = false;
      endRequested = false;
      aborted = false;
      controller = (typeof AbortController !== "undefined") ? new AbortController() : null;
    }

    function maybeComplete() {
      if (endRequested && queue.length === 0 && !pumping && !completed && !aborted) {
        completed = true;
        onCompleted();
      }
    }

    async function pump() {
      if (pumping) return;
      pumping = true;
      var myGen = generation;
      while (queue.length > 0 && !aborted && myGen === generation) {
        var item = queue.shift();
        var text = item && item.text;
        var directive = (item && item.directive) || {};
        var cfg = getConfig ? getConfig() : {};
        var body = {
          text: text,
          voiceId: directive.voiceId || directive.voice || (cfg && cfg.voice),
          sampleRate: (cfg && cfg.sampleRate) || 16000,
          speed: directive.speed,
          rateWpm: directive.rateWpm,
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
          queue = [];
          onError("restful", (err && err.message) || "请求失败");
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
        try {
          var contentType = "";
          try { contentType = (resp.headers && resp.headers.get && resp.headers.get("content-type")) || ""; } catch (_) {}
          if (contentType.indexOf("application/json") >= 0) {
            var data = await resp.json();
            var abJson = base64ToArrayBuffer(data && data.audioBase64);
            if (aborted || myGen !== generation) return;
            onAudio(abJson);
          } else if (resp.body && typeof resp.body.getReader === "function") {
            var reader = resp.body.getReader();
            while (true) {
              var r = await reader.read();
              if (r.done) break;
              if (aborted || myGen !== generation) return;
              if (r.value && r.value.byteLength) {
                // 按 byteOffset 精确 slice 子视图字节【B4】。
                var chunk = r.value;
                onAudio(chunk.buffer.slice(chunk.byteOffset, chunk.byteOffset + chunk.byteLength));
              }
            }
          } else {
            var ab;
            ab = await resp.arrayBuffer();
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

    function begin() { reset(); }

    function push(text, directive) {
      if (!text || aborted) return;
      var parts = splitForTts(text);
      for (var i = 0; i < parts.length; i++) {
        if (parts[i]) queue.push({ text: parts[i], directive: directive || null });
      }
      if (!pumping) pump();
    }

    function end() {
      if (aborted) return;
      endRequested = true;
      maybeComplete();   // 泵空闲且队列空：立即收尾
    }

    function abort() {
      generation++;
      aborted = true;
      queue = [];
      pumping = false;
      try { if (controller) controller.abort(); } catch (_) {}
    }

    return { begin: begin, push: push, end: end, abort: abort, name: "aliyun-rest" };
  }

  function base64ToArrayBuffer(b64) {
    if (!b64) return new ArrayBuffer(0);
    var decode = typeof atob === "function"
      ? atob
      : function (s) { return Buffer.from(s, "base64").toString("binary"); };
    var bin = decode(b64);
    var bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes.buffer;
  }

  createRestSpeaker.splitForTts = splitForTts;
  createRestSpeaker.base64ToArrayBuffer = base64ToArrayBuffer;
  return createRestSpeaker;
});
