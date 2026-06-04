/* 车机/Android 音频焦点保持：免提会话活跃时播放一条无声、循环的 <audio>。
 *
 * 部分车机浏览器在 getUserMedia 停麦后会把系统音频焦点还给上一个媒体 App，导致外部
 * 音乐恢复；随后浏览器 TTS/WebAudio 又不一定重新抢占焦点，出现助手语音和音乐混播。
 * 这个 guard 在用户手势内启动，贯穿「聆听 -> 思考 -> 朗读 -> 再聆听」整段语音会话，
 * 退出或暂停免提时停止，减少停麦和 TTS 首帧之间的焦点空窗。
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
    var log = opts.log || function () {};

    var audio = null;
    var objectUrl = "";
    var active = false;

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

    function start() {
      active = true;
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

    function stop() {
      active = false;
      if (!audio) return;
      try { if (typeof audio.pause === "function") audio.pause(); } catch (_) {}
      try { audio.currentTime = 0; } catch (_) {}
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

    return { start: start, stop: stop, dispose: dispose, isActive: isActive, getAudio: getAudio };
  }

  createVoiceAudioFocusGuard.makeSilentWavBlob = makeSilentWavBlob;
  return createVoiceAudioFocusGuard;
});
