/* 唤醒提示音 —— Web Audio 合成一声短「叮」（双音 880→1320Hz，各 ~0.11s，指数衰减）。
 *
 * 为什么不再用 <audio> 解码 WAV：旧实现喂 8bit/8kHz WAV 给 <audio> 解码，Chrome
 * 能放，但部分国产内核（百度 T7 / 小米自带浏览器）的精简音频解码栈对 8bit PCM
 * 或 8kHz 低采样率解码失败/静默——叮听不到。改用 OscillatorNode 纯合成，不经任何
 * 容器/编码解码，凡支持 (webkit)AudioContext 的内核都出声。
 *
 * 为什么不经 voice-pcm-player：提示音与 TTS 播放链路语义无关，独立 AudioContext
 * 不污染播放器的 drain gate / onAudible（零发声判定）。
 *
 * autoplay：唤醒发生在非手势上下文（识别回调）。AudioContext 在移动端首次须在用户
 * 手势内创建并 resume()→running，否则其后 schedule 的 oscillator 不发声。故 prime()
 * 在手势内建 ctx + resume（与 voice-pcm-player 的 ctx-in-gesture 同款套路）。
 *
 * 时长 ~0.22s（<5s）：Chromium 只拿瞬态焦点，不建媒体会话（车机面板无条目）。
 *
 * UMD：AudioContext 可注入，node --test 可测。
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.createVoiceChime = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // 双音「叮」各 SEG 秒，指数衰减包络（清脆不刺耳）。唤醒升调（880→1320，"醒"）／
  // 回落待机降调（1320→880，"睡"）——同段提示音两个方向，一耳朵区分醒/睡。
  var TONES_WAKE = [880, 1320];
  var TONES_SLEEP = [1320, 880];
  var SEG = 0.11;     // 每音时长（秒）
  var PEAK = 0.7;     // 包络峰值（相对满刻度），再乘 opts.volume

  function createVoiceChime(opts) {
    opts = opts || {};
    var CtxImpl = opts.AudioContextImpl ||
      (typeof window !== "undefined" ? (window.AudioContext || window.webkitAudioContext) : null);
    var log = opts.log || function () {};

    var ctx = null;
    var primed = false;

    function ensureCtx() {
      if (ctx) return ctx;
      if (!CtxImpl) { log("chime-create", "AudioContext 不可用"); return null; }
      try {
        ctx = new CtxImpl();
      } catch (err) {
        ctx = null;
        log("chime-create", (err && err.message) || "创建 AudioContext 失败");
      }
      return ctx;
    }

    function resumeIfNeeded(c) {
      if (c && c.state === "suspended" && typeof c.resume === "function") {
        try {
          var p = c.resume();
          if (p && typeof p.catch === "function") p.catch(function () {});
        } catch (_) {}
      }
    }

    // 用户手势内调用：创建 + resume AudioContext 解锁 autoplay（不发声）。
    function prime() {
      if (primed) return;
      var c = ensureCtx();
      if (!c) return;
      resumeIfNeeded(c);
      primed = true;
    }

    // 调度一声「叮」（非手势上下文，靠 prime 过的 running ctx）。
    // variant: "sleep" → 降调（回落待机），其余 → 升调（唤醒）。
    function play(variant) {
      var c = ensureCtx();
      if (!c) return;
      resumeIfNeeded(c);   // ctx 可能被系统挂起，best-effort 续上
      try {
        var vol = opts.volume == null ? 0.5 : opts.volume;
        var tones = variant === "sleep" ? TONES_SLEEP : TONES_WAKE;
        var t0 = c.currentTime;
        for (var i = 0; i < tones.length; i++) {
          var start = t0 + i * SEG;
          var osc = c.createOscillator();
          var gain = c.createGain();
          osc.frequency.setValueAtTime(tones[i], start);
          // 指数衰减包络：峰值 → 近零（exponentialRamp 不能落到 0，用 1e-4 兜底）。
          gain.gain.setValueAtTime(vol * PEAK, start);
          gain.gain.exponentialRampToValueAtTime(0.0001, start + SEG);
          osc.connect(gain);
          gain.connect(c.destination);
          osc.start(start);
          osc.stop(start + SEG);
        }
      } catch (err) {
        log("chime-play", (err && err.message) || "播放提示音失败");
      }
    }

    function dispose() {
      if (ctx && typeof ctx.close === "function") {
        try { ctx.close(); } catch (_) {}
      }
      ctx = null;
      primed = false;
    }

    return {
      prime: prime, play: play, dispose: dispose,
      getContext: function () { return ctx; },
    };
  }

  return createVoiceChime;
});
