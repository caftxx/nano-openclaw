/* speech-gateway unified realtime TTS adapter.
 *
 * One WebSocket response accepts the stable text chunks already emitted by
 * the voice state machine and returns base64 PCM16LE audio deltas. Playback
 * remains owned by voice-speaker-fallback's shared PCM player.
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.createOpenAISpeaker = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function base64ToArrayBuffer(value) {
    if (!value) return new ArrayBuffer(0);
    var decode = typeof atob === "function"
      ? atob
      : function (text) { return Buffer.from(text, "base64").toString("binary"); };
    var binary = decode(value);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes.buffer;
  }

  function createOpenAISpeaker(opts) {
    opts = opts || {};
    var getUrl = opts.getUrl || function () { return ""; };
    var getConfig = opts.getConfig || function () { return {}; };
    var onAudio = opts.onAudio || function () {};
    var onCompleted = opts.onCompleted || function () {};
    var onError = opts.onError || function () {};
    var WS = opts.WebSocketImpl || (typeof WebSocket !== "undefined" ? WebSocket : null);

    var ws = null;
    var generation = 0;
    var state = "idle";
    var pendingTexts = [];
    var endRequested = false;
    var doneSent = false;
    var directive = null;
    var completed = false;

    function send(sock, payload) {
      sock.send(JSON.stringify(payload));
    }

    function fail(sock, name, message) {
      if (sock !== ws || completed) return;
      completed = true;
      onError(name, message || "speech-gateway realtime TTS failed");
      try { if (sock.readyState <= 1) sock.close(); } catch (_) {}
    }

    function flushText(sock) {
      if (sock !== ws || state !== "responding") return;
      try {
        for (var i = 0; i < pendingTexts.length; i++) {
          send(sock, { type: "speech.input_text.delta", delta: pendingTexts[i] });
        }
        pendingTexts = [];
        if (endRequested && !doneSent) {
          send(sock, { type: "speech.input_text.done" });
          doneSent = true;
        }
      } catch (err) {
        fail(sock, "ws", (err && err.message) || "发送实时合成文本失败");
      }
    }

    function begin(nextDirective) {
      if (ws || state === "connecting") return;
      directive = nextDirective || null;
      pendingTexts = [];
      endRequested = false;
      doneSent = false;
      completed = false;
      var cfg = getConfig() || {};
      var url = getUrl();
      if (!WS || !url || !cfg.model || !cfg.voice) {
        onError("config", "speech-gateway 实时 TTS 配置缺失");
        return;
      }
      var myGen = generation;
      var voice = (directive && (directive.voiceId || directive.voice)) || cfg.voice;
      var sep = url.indexOf("?") >= 0 ? "&" : "?";
      var socketUrl = url + sep + "model=" + encodeURIComponent(cfg.model)
        + "&voice=" + encodeURIComponent(voice);
      var sock;
      try { sock = new WS(socketUrl); }
      catch (err) {
        onError("ws", (err && err.message) || "WebSocket 创建失败");
        return;
      }
      if (myGen !== generation) { try { sock.close(); } catch (_) {} return; }
      ws = sock;
      state = "connecting";

      sock.onopen = function () { /* session.created drives negotiation */ };
      sock.onmessage = function (ev) {
        if (sock !== ws || typeof ev.data !== "string") return;
        var event;
        try { event = JSON.parse(ev.data); } catch (_) { return; }
        var kind = event && event.type;
        try {
          if (kind === "session.created") {
            state = "updating";
            send(sock, {
              type: "session.update",
              session: {
                type: "realtime",
                audio: { output: { model: cfg.model, voice: voice } },
              },
            });
          } else if (kind === "session.updated") {
            var output = (((event.session || {}).audio || {}).output || {});
            var actualRate = ((output.format || {}).rate || cfg.sampleRate);
            if (actualRate !== cfg.sampleRate) {
              fail(sock, "sample-rate", "采样率不匹配：期望 " + cfg.sampleRate + "，实际 " + actualRate);
              return;
            }
            state = "creating";
            send(sock, {
              type: "response.create",
              response: { output_modalities: ["audio"], input_text_stream: true },
            });
          } else if (kind === "response.created") {
            state = "responding";
            flushText(sock);
          } else if (kind === "response.output_audio.delta") {
            var audio = base64ToArrayBuffer(event.delta || "");
            if (audio.byteLength) onAudio(audio);
          } else if (kind === "response.done") {
            var response = event.response || {};
            if (response.status !== "completed") {
              var details = response.status_details || {};
              fail(sock, "response", String(details.error || "实时合成未完成"));
              return;
            }
            completed = true;
            ws = null;
            state = "idle";
            onCompleted();
            try { if (sock.readyState <= 1) sock.close(); } catch (_) {}
          } else if (kind === "error") {
            fail(sock, "gateway", String((event.error || {}).message || "实时合成失败"));
          }
        } catch (err) {
          fail(sock, "protocol", (err && err.message) || "实时合成协议错误");
        }
      };
      sock.onerror = function () {
        fail(sock, "ws", "speech-gateway WebSocket 错误");
      };
      sock.onclose = function () {
        if (sock !== ws) return;
        ws = null;
        state = "idle";
        if (!completed && myGen === generation) {
          completed = true;
          onError("ws", "speech-gateway WebSocket 提前断开");
        }
      };
    }

    function push(text, nextDirective) {
      if (!text) return;
      if (!ws && state !== "connecting") begin(nextDirective);
      if (state !== "responding" || !ws || ws.readyState !== 1) {
        pendingTexts.push(text);
        return;
      }
      try { send(ws, { type: "speech.input_text.delta", delta: text }); }
      catch (err) { fail(ws, "ws", (err && err.message) || "发送实时合成文本失败"); }
    }

    function end() {
      endRequested = true;
      if (!doneSent && state === "responding" && ws && ws.readyState === 1) {
        try {
          send(ws, { type: "speech.input_text.done" });
          doneSent = true;
        }
        catch (err) { fail(ws, "ws", (err && err.message) || "结束实时合成失败"); }
      }
    }

    function abort() {
      generation++;
      completed = true;
      pendingTexts = [];
      endRequested = false;
      doneSent = false;
      var sock = ws;
      ws = null;
      state = "idle";
      try {
        if (sock && sock.readyState === 1) send(sock, { type: "response.cancel" });
        if (sock && sock.readyState <= 1) sock.close();
      } catch (_) {}
    }

    function busy() { return state !== "idle" && !completed; }

    return { begin: begin, push: push, end: end, abort: abort, busy: busy, name: "openai-compatible" };
  }

  createOpenAISpeaker.base64ToArrayBuffer = base64ToArrayBuffer;
  return createOpenAISpeaker;
});
