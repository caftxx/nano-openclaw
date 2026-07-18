/* OpenAI Realtime-compatible ASR adapter.
 *
 * The browser records 16 kHz PCM16 mono, but connects only to nano's
 * /api/voice/realtime WebSocket. nano owns the upstream speech-gateway URL
 * and Bearer token and relays protocol events in both directions.
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.createOpenAIRecognizer = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function arrayBufferToBase64(buf) {
    var bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
    if (typeof Buffer !== "undefined") return Buffer.from(bytes).toString("base64");
    var binary = "";
    var step = 0x8000;
    for (var i = 0; i < bytes.length; i += step) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
    }
    return btoa(binary);
  }

  function parseRealtimeEvent(event, accumulated) {
    event = event || {};
    accumulated = accumulated || "";
    switch (event.type) {
      case "session.created": return { kind: "created", text: accumulated };
      case "session.updated": return { kind: "started", text: accumulated };
      case "conversation.item.input_audio_transcription.delta":
        return { kind: "interim", text: accumulated + String(event.delta || "") };
      case "conversation.item.input_audio_transcription.completed":
        return { kind: "final", text: String(event.transcript || "").trim() };
      case "error":
        return { kind: "failed", text: String((event.error && event.error.message) || "实时语音识别失败") };
      default: return { kind: "other", text: accumulated };
    }
  }

  function createOpenAIRecognizer(opts) {
    opts = opts || {};
    var getUrl = opts.getUrl || function () { return "/api/voice/realtime"; };
    var onStarted = opts.onStarted || function () {};
    var onInterim = opts.onInterim || function () {};
    var onFinal = opts.onFinal || function () {};
    var onError = opts.onError || function () {};
    var onEnded = opts.onEnded || function () {};
    var WebSocketImpl = opts.WebSocketImpl || (typeof WebSocket !== "undefined" ? WebSocket : null);
    var workletUrl = opts.workletUrl || "/static/voice-pcm-worklet.js";

    var ws = null;
    var starting = false;
    var generation = 0;
    var deliberate = false;
    var ready = false;
    var pendingFrames = [];
    var lastInterim = "";
    var audioCtx = null;
    var workletNode = null;
    var sourceNode = null;
    var micStream = null;

    function cleanupAudio() {
      try { if (workletNode) workletNode.disconnect(); } catch (_) {}
      try { if (sourceNode) sourceNode.disconnect(); } catch (_) {}
      try { if (micStream) micStream.getTracks().forEach(function (track) { track.stop(); }); } catch (_) {}
      try { if (audioCtx) audioCtx.close(); } catch (_) {}
      workletNode = null;
      sourceNode = null;
      micStream = null;
      audioCtx = null;
    }

    function teardown(fireEnded) {
      starting = false;
      ready = false;
      pendingFrames = [];
      lastInterim = "";
      cleanupAudio();
      var sock = ws;
      ws = null;
      try { if (sock && sock.readyState <= 1) sock.close(); } catch (_) {}
      if (fireEnded) onEnded();
    }

    function fail(kind, message) {
      if (deliberate) return;
      deliberate = true;
      onError(kind, message);
      teardown(true);
      deliberate = false;
    }

    function sendJson(message) {
      if (!ws || ws.readyState !== 1) return false;
      try { ws.send(JSON.stringify(message)); return true; } catch (_) { return false; }
    }

    function sendAudio(buf) {
      if (!ws || ws.readyState !== 1 || !ready) {
        pendingFrames.push(buf);
        return;
      }
      sendJson({ type: "input_audio_buffer.append", audio: arrayBufferToBase64(buf) });
    }

    function flushPending() {
      var frames = pendingFrames;
      pendingFrames = [];
      for (var i = 0; i < frames.length; i++) sendAudio(frames[i]);
    }

    async function setupAudioReal() {
      var mediaDevices = opts.mediaDevices || (typeof navigator !== "undefined" ? navigator.mediaDevices : null);
      if (!mediaDevices || !mediaDevices.getUserMedia) throw new Error("浏览器不支持麦克风采集");
      micStream = await mediaDevices.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: true, autoGainControl: true },
      });
      var Ctx = root.AudioContext || root.webkitAudioContext;
      audioCtx = new Ctx();
      await audioCtx.audioWorklet.addModule(workletUrl);
      sourceNode = audioCtx.createMediaStreamSource(micStream);
      workletNode = new AudioWorkletNode(audioCtx, "voice-pcm-downsampler", {
        processorOptions: { targetRate: 16000, frameBytes: 3200 },
      });
      workletNode.port.onmessage = function (event) { sendAudio(event.data); };
      sourceNode.connect(workletNode);
    }
    var setupAudio = opts.setupAudio || setupAudioReal;

    async function start() {
      if (ws || starting) return;
      if (!WebSocketImpl) { fail("ws", "浏览器不支持 WebSocket"); return; }
      starting = true;
      deliberate = false;
      ready = false;
      pendingFrames = [];
      lastInterim = "";
      var myGeneration = generation;
      try {
        await setupAudio();
      } catch (error) {
        if (myGeneration !== generation) { cleanupAudio(); return; }
        fail("mic", (error && (error.name || error.message)) || "麦克风初始化失败");
        return;
      }
      if (myGeneration !== generation) { cleanupAudio(); return; }
      var sock;
      try { sock = new WebSocketImpl(getUrl()); }
      catch (error2) {
        cleanupAudio();
        fail("ws", (error2 && error2.message) || "WebSocket 创建失败");
        return;
      }
      ws = sock;
      starting = false;
      sock.onmessage = function (message) {
        if (sock !== ws || typeof message.data !== "string") return;
        var event;
        try { event = JSON.parse(message.data); } catch (_) { return; }
        var parsed = parseRealtimeEvent(event, lastInterim);
        if (parsed.kind === "created") {
          sendJson({
            type: "session.update",
            session: { audio: { input: {
              format: { type: "audio/pcm", rate: 16000 },
              turn_detection: { type: "server_vad", silence_duration_ms: 600, prefix_padding_ms: 300 },
            } } },
          });
        } else if (parsed.kind === "started") {
          ready = true;
          flushPending();
          onStarted();
        } else if (parsed.kind === "interim") {
          lastInterim = parsed.text;
          if (lastInterim) onInterim(lastInterim);
        } else if (parsed.kind === "final") {
          lastInterim = "";
          if (parsed.text) onFinal(parsed.text);
        } else if (parsed.kind === "failed") {
          fail("realtime", parsed.text);
        }
      };
      sock.onerror = function () {
        if (sock === ws) fail("ws", "speech-gateway WebSocket 错误");
      };
      sock.onclose = function () {
        if (sock !== ws || deliberate) return;
        deliberate = true;
        teardown(true);
        deliberate = false;
      };
    }

    function stop() {
      generation++;
      deliberate = true;
      teardown(false);
      deliberate = false;
    }

    function rebuild() { stop(); start(); }

    function flushNow() {
      var text = String(lastInterim || "").trim();
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
      startTimeoutMs: opts.startTimeoutMs != null ? opts.startTimeoutMs : 12000,
      name: "openai-compatible",
    };
  }

  createOpenAIRecognizer.parseRealtimeEvent = parseRealtimeEvent;
  createOpenAIRecognizer.arrayBufferToBase64 = arrayBufferToBase64;
  return createOpenAIRecognizer;
});
