/* 车机/Android 音频焦点保持：免提会话活跃时占住系统音频焦点，防外部音乐恢复混播。
 *
 * 部分车机浏览器在 getUserMedia 停麦后会把系统音频焦点还给上一个媒体 App，导致外部
 * 音乐恢复；随后浏览器 TTS/WebAudio 又不一定重新抢占焦点，出现助手语音和音乐混播。
 * guard 在用户手势内启动，贯穿「聆听 -> 思考 -> 朗读 -> 再聆听」整段语音会话，
 * 退出或暂停免提时停止。两种占焦点策略：
 *
 * - 占位麦克风流（主，preferMicHold() 为 true 时）：持续握住一条不消费音频的
 *   getUserMedia 流。实测车机上「语音输入时外部音乐必暂停」——采集是两类内核
 *   （Chrome / 百度 T7）都可靠的焦点通道；且不注册 Media Session，车机媒体面板
 *   不会出现一条循环曲目。只在页面自采音的引擎（aliyun）下用：webspeech 的识别
 *   采集在浏览器服务侧，页面持麦可能被 Android 并发采集限制静音掉识别。
 * - 无声 <audio>（兜底）：循环播放 6s 近静音 WAV（跨过 Chromium 5s 内容级媒体
 *   门槛）。副作用是会以一条 6s 曲目出现在媒体面板，且个别内核（百度 T7）不为
 *   网页 <audio> 申请系统焦点。
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.createVoiceAudioFocusGuard = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function writeAscii(view, offset, text) {
    for (var i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
  }

  function makeSilentWavBlob(BlobImpl, seconds, sampleRate) {
    // Chromium 系车机内核可能按 metadata duration 判定是否为「内容级」媒体；
    // 太短即便 loop 也可能只拿 transient/may-duck 焦点。默认 6s 保守跨过 5s 门槛。
    seconds = seconds || 6;
    sampleRate = sampleRate || 8000;
    var samples = Math.max(1, Math.floor(seconds * sampleRate));
    var header = 44;
    var buf = new ArrayBuffer(header + samples);
    var view = new DataView(buf);

    writeAscii(view, 0, "RIFF");
    view.setUint32(4, 36 + samples, true);
    writeAscii(view, 8, "WAVE");
    writeAscii(view, 12, "fmt ");
    view.setUint32(16, 16, true);      // PCM fmt chunk size
    view.setUint16(20, 1, true);       // PCM
    view.setUint16(22, 1, true);       // mono
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate, true); // byteRate: 8-bit mono
    view.setUint16(32, 1, true);       // blockAlign
    view.setUint16(34, 8, true);       // bitsPerSample
    writeAscii(view, 36, "data");
    view.setUint32(40, samples, true);
    // 近静音而非纯 128：避免个别平台把完全静音流优化掉；配合 volume=0.001 基本不可闻。
    for (var i = 0; i < samples; i++) view.setUint8(header + i, i % 2 ? 127 : 128);

    return new BlobImpl([buf], { type: "audio/wav" });
  }

  function createVoiceAudioFocusGuard(opts) {
    opts = opts || {};
    var AudioImpl = opts.AudioImpl || (typeof Audio !== "undefined" ? Audio : null);
    var URLImpl = opts.URLImpl || (typeof URL !== "undefined" ? URL : null);
    var BlobImpl = opts.BlobImpl || (typeof Blob !== "undefined" ? Blob : null);
    var mediaDevices = opts.mediaDevices !== undefined
      ? opts.mediaDevices
      : (typeof navigator !== "undefined" ? navigator.mediaDevices : null);
    // W3C Audio Session API：声明本页音频是「边放边录」的语音会话，由平台接管焦点仲裁。
    // 这是本问题的标准解，目前仅 Safari 16.4+ 实现（Chrome/Android 未实现，设了无害）。
    var audioSession = opts.audioSession !== undefined
      ? opts.audioSession
      : (typeof navigator !== "undefined" ? navigator.audioSession : null);
    var preferMicHold = opts.preferMicHold || function () { return false; };
    var log = opts.log || function () {};

    var audio = null;
    var objectUrl = "";
    var active = false;
    var micStream = null;
    var micPending = false;

    function micUsable() {
      return Boolean(mediaDevices && typeof mediaDevices.getUserMedia === "function");
    }

    function stopTracks(stream) {
      try {
        stream.getTracks().forEach(function (t) { try { t.stop(); } catch (_) {} });
      } catch (_) {}
    }

    // ── 占位麦克风流 ────────────────────────────────────────────────────────
    function startMicHold() {
      if (micStream || micPending) return;
      micPending = true;
      var p;
      try {
        // 与识别采集同形态（audio:true，AEC 默认开）：实测正是这种采集会让外部音乐暂停，
        // 占位流复用同一路径，别用关 AEC 的"轻量"配置偏离它。
        p = mediaDevices.getUserMedia({ audio: true });
      } catch (err) {
        micPending = false;
        log("mic-hold", (err && err.message) || "申请占位麦克风失败");
        startSilentAudio();
        return;
      }
      p.then(function (stream) {
        micPending = false;
        if (!active) { stopTracks(stream); return; }   // 等待期间已 stop：立即归还
        micStream = stream;
        // 系统侧收回轨道（设备抢占等）：清引用，下次 start() 重新申请。
        try {
          stream.getTracks().forEach(function (t) {
            t.onended = function () { if (micStream === stream) micStream = null; };
          });
        } catch (_) {}
        stopSilentAudio();   // 麦到手后不再需要静音 audio 兜底
      }, function (err) {
        micPending = false;
        log("mic-hold", (err && err.message) || "申请占位麦克风失败");
        if (active) startSilentAudio();   // 拿不到麦 → 退回静音 audio
      });
    }

    function stopMicHold() {
      if (!micStream) return;
      stopTracks(micStream);
      micStream = null;
    }

    // ── 无声 <audio> 兜底 ──────────────────────────────────────────────────
    function ensureAudio() {
      if (audio) return audio;
      if (!AudioImpl) { log("no-audio", "Audio 不可用"); return null; }
      try {
        audio = new AudioImpl();
      } catch (err) {
        audio = null;
        log("audio-create", (err && err.message) || "创建 Audio 失败");
        return null;
      }
      audio.loop = true;
      audio.preload = "auto";
      audio.playsInline = true;
      audio.muted = false;
      // 不设 0，避免部分平台把它当成无需音频焦点的静音媒体优化掉。
      audio.volume = opts.volume == null ? 0.001 : opts.volume;

      if (BlobImpl && URLImpl && typeof URLImpl.createObjectURL === "function") {
        try {
          objectUrl = URLImpl.createObjectURL(makeSilentWavBlob(BlobImpl, opts.seconds, opts.sampleRate));
          audio.src = objectUrl;
        } catch (err) {
          log("audio-src", (err && err.message) || "创建静音音频失败");
        }
      }
      return audio;
    }

    function startSilentAudio() {
      var a = ensureAudio();
      if (!a || !a.src || typeof a.play !== "function") return null;
      var p = null;
      try {
        p = a.play();
      } catch (err) {
        log("audio-play", (err && err.message) || "启动静音音频失败");
        return null;
      }
      if (p && typeof p.catch === "function") {
        p.catch(function (err) {
          if (active) log("audio-play", (err && err.message) || "启动静音音频失败");
        });
      }
      return p || null;
    }

    function stopSilentAudio() {
      if (!audio) return;
      try { if (typeof audio.pause === "function") audio.pause(); } catch (_) {}
      try { audio.currentTime = 0; } catch (_) {}
    }

    function setAudioSessionType(type) {
      if (!audioSession) return;
      try { audioSession.type = type; } catch (_) {}
    }

    // ── 对外生命周期 ────────────────────────────────────────────────────────
    function start() {
      active = true;
      setAudioSessionType("play-and-record");
      if (preferMicHold() && micUsable()) {
        startMicHold();   // 已持有/申请中则 no-op；静音 audio 在拿到麦后才停，避免换轨空窗
        return null;
      }
      stopMicHold();      // 偏好切回 audio（如引擎切到 webspeech）：先归还占位麦
      return startSilentAudio();
    }

    function stop() {
      active = false;
      setAudioSessionType("auto");   // 还给平台默认仲裁，外部音乐可恢复
      stopMicHold();
      stopSilentAudio();
    }

    function dispose() {
      stop();
      if (objectUrl && URLImpl && typeof URLImpl.revokeObjectURL === "function") {
        try { URLImpl.revokeObjectURL(objectUrl); } catch (_) {}
      }
      objectUrl = "";
      audio = null;
    }

    function isActive() {
      return active;
    }

    function getAudio() {
      return audio;
    }

    function getMicStream() {
      return micStream;
    }

    return { start: start, stop: stop, dispose: dispose, isActive: isActive, getAudio: getAudio, getMicStream: getMicStream };
  }

  createVoiceAudioFocusGuard.makeSilentWavBlob = makeSilentWavBlob;
  return createVoiceAudioFocusGuard;
});
