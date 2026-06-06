/* 音频焦点 guard —— 语音浮层期间持一条静音保持音的瞬态焦点（Chrome / Android）。
 *
 * 设计要点（历史坑位语义）：
 *  - 【C1】保持音是 4s 近静音 WAV 循环：时长刻意压在 Chromium 5s「内容级媒体」门槛
 *    之下——内容级会创建媒体会话，在车机媒体面板显示一条进度条曲目；<5s 只拿
 *    GAIN_TRANSIENT_MAY_DUCK 瞬态焦点、无面板条目。瞬态焦点对实际用法足够：用户
 *    进语音页前音乐已暂停，没人发 GAIN 它就不会醒；正在播的音乐只被压低不强行暂停。
 *    近静音而非纯静音 + volume 0.001 而非 0，避免被平台当无声流优化掉。
 *  - 【C3】刻意**没有持麦档**：曾用占位麦（AEC 采集）压外部音乐，但 AEC 采集让
 *    Android 进通信模式 = 系统层面"来电话"——音量键变通话音量、TTS 走通话通路
 *    从手机出声到不了车机；释放麦时"通话结束"，车机/蓝牙栈按通话结束的标准行为
 *    自动恢复媒体播放（AVRCP PLAY），把用户手动暂停的音乐在朗读开始瞬间叫醒。
 *  - 保持音的 autoplay 解锁：焦点换挡可能发生在非手势上下文（如 TTS 首帧到达时），
 *    从未在手势内 play 过的元素会被 autoplay 策略拦下 → 须在用户手势内 prime()。
 *
 * 渐进增强 navigator.audioSession（W3C Audio Session API，目前仅 Safari 16.4+）：
 * 占焦点时声明 play-and-record（页面同时有识别采集），释放时还原 auto。
 *
 * 由 shell 以两档模式驱动（focusMode 纯函数派生，随状态机迁移 diff 换轨）：
 *   setMode("silent-audio") 浮层开着：循环保持音持瞬态焦点
 *   setMode("released")     退出语音：还焦点
 *
 * UMD：依赖全部可注入（Audio/URL/Blob/audioSession），node --test 可测。
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory(require("./voice-wav.js"));
  else root.createVoiceAudioFocusGuard = factory(root.VoiceWav);
})(typeof self !== "undefined" ? self : this, function (wav) {
  "use strict";

  // 生成 4s 近静音 WAV（8bit 单声道 8kHz）。时长 <5s（见头注释 C1）；
  // 交替 127/128 而非纯 128：个别平台会把完全静音流优化掉。
  function makeSilentWavBlob(BlobImpl, seconds, sampleRate) {
    seconds = seconds || 4;
    sampleRate = sampleRate || 8000;
    var n = Math.max(1, Math.floor(seconds * sampleRate));
    var samples = new Uint8Array(n);
    for (var i = 0; i < n; i++) samples[i] = i % 2 ? 127 : 128;
    return wav.makeWavBlob(BlobImpl, sampleRate, samples);
  }

  function createVoiceAudioFocusGuard(opts) {
    opts = opts || {};
    var AudioImpl = opts.AudioImpl || (typeof Audio !== "undefined" ? Audio : null);
    var URLImpl = opts.URLImpl || (typeof URL !== "undefined" ? URL : null);
    var BlobImpl = opts.BlobImpl || (typeof Blob !== "undefined" ? Blob : null);
    var audioSession = opts.audioSession !== undefined
      ? opts.audioSession
      : (typeof navigator !== "undefined" ? navigator.audioSession : null);
    var log = opts.log || function () {};

    var mode = "released";
    var audio = null;
    var objectUrl = "";
    var primed = false;   // 保持音是否已在手势内成功 play 过（autoplay 解锁）

    function setAudioSessionType(type) {
      if (!audioSession) return;
      try { audioSession.type = type; } catch (_) {}
    }

    function ensureAudio() {
      if (audio) return audio;
      if (!AudioImpl) { log("no-audio", "Audio 不可用"); return null; }
      try { audio = new AudioImpl(); }
      catch (err) { audio = null; log("audio-create", (err && err.message) || "创建 Audio 失败"); return null; }
      audio.loop = true;
      audio.preload = "auto";
      audio.playsInline = true;
      audio.muted = false;
      audio.volume = opts.volume == null ? 0.001 : opts.volume;   // 不设 0，防被当无声流优化掉【C1】
      if (BlobImpl && URLImpl && typeof URLImpl.createObjectURL === "function") {
        try {
          objectUrl = URLImpl.createObjectURL(makeSilentWavBlob(BlobImpl, opts.seconds, opts.sampleRate));
          audio.src = objectUrl;
        } catch (err) { log("audio-src", (err && err.message) || "创建保持音频失败"); }
      }
      return audio;
    }

    function startSilentAudio() {
      var a = ensureAudio();
      if (!a || !a.src || typeof a.play !== "function") return null;
      var p = null;
      try { p = a.play(); }
      catch (err) { log("audio-play", (err && err.message) || "启动保持音失败"); return null; }
      if (p && typeof p.catch === "function") {
        p.catch(function (err) {
          if (mode !== "released") log("audio-play", (err && err.message) || "启动保持音失败");
        });
      }
      return p || null;
    }

    function stopSilentAudio() {
      if (!audio) return;
      try { if (typeof audio.pause === "function") audio.pause(); } catch (_) {}
      try { audio.currentTime = 0; } catch (_) {}
    }

    // 两档模式驱动。幂等。
    function setMode(next) {
      mode = next;
      if (next === "released") {
        setAudioSessionType("auto");   // 还平台默认仲裁
        stopSilentAudio();
        return;
      }
      setAudioSessionType("play-and-record");
      startSilentAudio();
    }

    // 回前台等时机重申当前模式（后台期间播放可能被系统打断）。
    function refresh() { setMode(mode); }

    // 用户手势内调用：把保持音成功 play 一次，解锁后续非手势 play()。
    // 解锁后若当前模式并非占焦点，立即暂停，媒体侧不残留播放。
    function prime() {
      if (primed) return;
      var p = startSilentAudio();
      if (!p || typeof p.then !== "function") {
        primed = true;                 // play() 不返回 promise（老内核）：保守按已解锁处理
        if (mode !== "silent-audio") stopSilentAudio();
        return;
      }
      p.then(function () {
        primed = true;
        if (mode !== "silent-audio") stopSilentAudio();
      }, function () { /* 被 autoplay 拦下：保持未解锁，下次手势再试 */ });
    }

    function dispose() {
      setMode("released");
      if (objectUrl && URLImpl && typeof URLImpl.revokeObjectURL === "function") {
        try { URLImpl.revokeObjectURL(objectUrl); } catch (_) {}
      }
      objectUrl = "";
      audio = null;
      primed = false;                  // 元素已丢弃，重建后需重新手势解锁
    }

    return {
      setMode: setMode, refresh: refresh, prime: prime, dispose: dispose,
      getMode: function () { return mode; },
      getAudio: function () { return audio; },
    };
  }

  createVoiceAudioFocusGuard.makeSilentWavBlob = makeSilentWavBlob;
  return createVoiceAudioFocusGuard;
});
