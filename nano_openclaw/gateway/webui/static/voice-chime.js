/* 唤醒提示音 —— 合成一声短「叮」（双音正弦 ~0.22s），<audio> 元素播放。
 *
 * 为什么不经 voice-pcm-player：提示音与合成播放链路语义无关，走播放器会污染
 * drain gate / onAudible（零发声判定）。独立 <audio> + 预生成 WAV blob 最轻。
 * 时长 <5s → Chromium 只拿瞬态焦点，不创建媒体会话（车机面板无条目）。
 *
 * autoplay：唤醒发生在非手势上下文（识别回调），从未在手势内 play 过的元素会被
 * 拦下 → 须在用户手势内 prime()（静音 play 一次解锁，与 focus guard 同款套路）。
 *
 * UMD：依赖全部可注入（Audio/URL/Blob），node --test 可测。
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.createVoiceChime = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // 双音「叮」：880Hz → 1320Hz 各 ~0.11s，指数衰减包络，8bit 单声道 8kHz。
  function makeChimeWavBlob(BlobImpl, sampleRate) {
    sampleRate = sampleRate || 8000;
    var seg = Math.floor(sampleRate * 0.11);
    var samples = seg * 2;
    var buf = new ArrayBuffer(44 + samples);
    var view = new DataView(buf);
    function ascii(off, text) { for (var i = 0; i < text.length; i++) view.setUint8(off + i, text.charCodeAt(i)); }
    ascii(0, "RIFF");
    view.setUint32(4, 36 + samples, true);
    ascii(8, "WAVE");
    ascii(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate, true);
    view.setUint16(32, 1, true);
    view.setUint16(34, 8, true);
    ascii(36, "data");
    view.setUint32(40, samples, true);
    var freqs = [880, 1320];
    for (var i = 0; i < samples; i++) {
      var f = freqs[(i / seg) | 0];
      var t = (i % seg) / sampleRate;
      var env = Math.exp(-t * 18);   // 指数衰减：清脆不刺耳
      var v = Math.sin(2 * Math.PI * f * t) * env * 90;   // 90/127 ≈ 0.7 FS 峰值
      view.setUint8(44 + i, 128 + Math.round(v));
    }
    return new BlobImpl([buf], { type: "audio/wav" });
  }

  function createVoiceChime(opts) {
    opts = opts || {};
    var AudioImpl = opts.AudioImpl || (typeof Audio !== "undefined" ? Audio : null);
    var URLImpl = opts.URLImpl || (typeof URL !== "undefined" ? URL : null);
    var BlobImpl = opts.BlobImpl || (typeof Blob !== "undefined" ? Blob : null);
    var log = opts.log || function () {};

    var audio = null;
    var objectUrl = "";
    var primed = false;

    function ensureAudio() {
      if (audio) return audio;
      if (!AudioImpl || !URLImpl || !BlobImpl) return null;
      try {
        audio = new AudioImpl();
        objectUrl = URLImpl.createObjectURL(makeChimeWavBlob(BlobImpl));
        audio.src = objectUrl;
        audio.preload = "auto";
        audio.playsInline = true;
      } catch (err) {
        audio = null;
        log("chime-create", (err && err.message) || "创建提示音失败");
      }
      return audio;
    }

    // 用户手势内调用：静音 play 一次解锁 autoplay，立即暂停（听不到）。
    function prime() {
      if (primed) return;
      var a = ensureAudio();
      if (!a) return;
      a.volume = 0;
      var p = null;
      try { p = a.play(); } catch (_) { return; }
      var done = function () {
        primed = true;
        try { a.pause(); a.currentTime = 0; } catch (_) {}
      };
      if (p && typeof p.then === "function") p.then(done, function () { /* 拦下：下次手势再试 */ });
      else done();
    }

    // 播放提示音（唤醒命中时，非手势上下文——靠 prime 过的解锁状态）。
    function play() {
      var a = ensureAudio();
      if (!a) return;
      try {
        a.volume = opts.volume == null ? 0.5 : opts.volume;
        a.currentTime = 0;
        var p = a.play();
        if (p && typeof p.catch === "function") {
          p.catch(function (err) { log("chime-play", (err && err.message) || "播放提示音失败"); });
        }
      } catch (err) { log("chime-play", (err && err.message) || "播放提示音失败"); }
    }

    function dispose() {
      try { if (audio) audio.pause(); } catch (_) {}
      if (objectUrl && URLImpl && typeof URLImpl.revokeObjectURL === "function") {
        try { URLImpl.revokeObjectURL(objectUrl); } catch (_) {}
      }
      objectUrl = "";
      audio = null;
      primed = false;
    }

    return {
      prime: prime, play: play, dispose: dispose,
      getAudio: function () { return audio; },
    };
  }

  createVoiceChime.makeChimeWavBlob = makeChimeWavBlob;
  return createVoiceChime;
});
