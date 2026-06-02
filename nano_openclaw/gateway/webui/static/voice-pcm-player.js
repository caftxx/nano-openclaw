/* 流式 PCM 播放器 —— Web Audio API 无缝排队播放 Int16 单声道小端 PCM。
 *
 * 配合阿里云流式语音合成（voice-tts-aliyun.js）：合成引擎只投递二进制 PCM 帧与
 * 生命周期事件，由本播放器把每帧 Int16 PCM 解为 Float32、排进 AudioContext 时间线
 * 连续播放。播放器与协议解耦，保持 node 可测（纯函数 pcm16ToFloat32 单独导出）。
 *
 * 排队策略：维护 nextStartTime 游标，每帧建一段 AudioBuffer，在
 * max(ctx.currentTime, nextStartTime) 起播并把游标前移 buffer.duration，做到帧间无缝。
 * 记录在播 source 数；drain 判定 gate 在「流已结束」之后——只有上层显式
 * markEnded()（对应 SynthesisCompleted）后、outstanding 归 0 才触发 onDrained()。
 * 这样可避免流式合成中途的帧间空隙（首帧前延迟、句间停顿、网络抖动让在播 source
 * 瞬间归零）被误判为「读完」而提前续听，造成外放被麦克风回采。
 *
 * AudioContext 生命周期（关键）：ctx 长生命周期、可复用，stop() 只停当前在播音源、
 * **不关 ctx**；离开语音模式时才用 dispose() 关闭释放。移动端 Chrome 要求 AudioContext
 * 在「用户手势」内创建/resume，否则会停在 suspended——既不出声，排程的 source 也永远
 * 不会 onended → outstanding 不归零 → 永不 drain → 卡死在「思考中」。所以上层必须在
 * 用户手势（点麦/点屏）里调 unlock() 把 ctx 建好并 resume；本播放器在 enqueue 里也做
 * 防御性 resume 兜底。stop() 用 generation 自增作废迟到的 onended，避免复用时旧回调污染。
 *
 * UMD 导出：node --test 取 createPcmPlayer.pcm16ToFloat32 做纯函数单测，
 * 浏览器挂 window.createPcmPlayer。
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.createPcmPlayer = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // 把一段 Int16 小端 PCM（ArrayBuffer）转为 Float32Array [-1,1]。
  // 负值 / 0x8000、正值 / 0x7fff，保证 -32768→-1、32767→~1、0→0。
  function pcm16ToFloat32(arrayBuffer) {
    var view = new DataView(arrayBuffer);
    var n = (arrayBuffer.byteLength / 2) | 0;
    var out = new Float32Array(n);
    for (var i = 0; i < n; i++) {
      var v = view.getInt16(i * 2, true);   // little-endian
      out[i] = v < 0 ? v / 0x8000 : v / 0x7fff;
    }
    return out;
  }

  // 工厂：opts = { sampleRate, onDrained, onError, AudioCtxImpl }
  function createPcmPlayer(opts) {
    opts = opts || {};
    var sampleRate = opts.sampleRate || 16000;
    var onDrained = opts.onDrained || function () {};
    var onError = opts.onError || function () {};

    var ctx = null;
    var nextStartTime = 0;     // 下一帧应起播的 ctx 时间线位置（游标）
    var outstanding = 0;       // 已排程仍未结束的 source 数
    var ended = false;         // markEnded()（SynthesisCompleted）后置位，开启 drain 判定
    var generation = 0;        // stop() 自增；onended 比对自身 generation 作废迟到回调
    var liveSources = [];      // 当前在播的 source 引用（stop 时统一 stop+disconnect）

    function getAudioCtxImpl() {
      if (opts.AudioCtxImpl) return opts.AudioCtxImpl;
      if (typeof window !== "undefined") return window.AudioContext || window.webkitAudioContext;
      return null;
    }

    // 懒建 AudioContext；建失败走 onError 并返回 null（不抛）。ctx 一旦建好就长期复用，
    // stop() 不再销毁它（销毁会丢失手势内 unlock 的解锁状态 → 移动端 suspended 卡死）。
    function ensureCtx() {
      if (ctx) return ctx;
      var Impl = getAudioCtxImpl();
      if (!Impl) { onError("no-audio-context", "AudioContext 不可用"); return null; }
      try {
        ctx = new Impl();
      } catch (err) {
        ctx = null;
        onError("audio-context", (err && err.message) || "创建 AudioContext 失败");
        return null;
      }
      return ctx;
    }

    // 在用户手势内调用：建好 ctx 并 resume。移动端 Chrome 只认手势内的 resume，
    // 否则 ctx 停在 suspended，音频不出声且 source 不会 onended → 状态机卡死。
    function unlock() {
      var c = ensureCtx();
      if (c && c.state === "suspended") {
        try { c.resume(); } catch (_) {}
      }
    }

    function removeFromLive(src) {
      var idx = liveSources.indexOf(src);
      if (idx >= 0) liveSources.splice(idx, 1);
    }

    function maybeDrained() {
      // 队列里没有显式 pending 缓存（每帧即建即排程）。但 drain 需等 markEnded
      // （SynthesisCompleted）后才触发：流式合成中途的帧间空隙会让 outstanding
      // 瞬间归零，若不 gate 在 ended 之后会被误判为「读完」而提前续听。
      // stop() 会把 ended 复位为 false，故 stop 后不会误触发。
      if (ended && outstanding === 0) onDrained();
    }

    // 上层收到 SynthesisCompleted（音频全部下发完）时调用，开启 drain 判定。
    // 若此刻 outstanding 已为 0（音频已播完），立即触发 onDrained。
    function markEnded() {
      ended = true;
      maybeDrained();
    }

    function enqueue(arrayBuffer) {
      if (!arrayBuffer || !arrayBuffer.byteLength) return;
      var c = ensureCtx();
      if (!c) return;
      // 防御性：ctx 可能在非手势路径建出、或被系统挂起 → 尝试 resume，否则 source 永不播。
      if (c.state === "suspended") { try { c.resume(); } catch (_) {} }
      var floats;
      try {
        floats = pcm16ToFloat32(arrayBuffer);
      } catch (err) {
        onError("decode", (err && err.message) || "PCM 解码失败");
        return;
      }
      if (!floats.length) return;
      var buffer;
      try {
        buffer = c.createBuffer(1, floats.length, sampleRate);
        buffer.getChannelData(0).set(floats);
      } catch (err) {
        onError("buffer", (err && err.message) || "创建 AudioBuffer 失败");
        return;
      }
      var src;
      try {
        src = c.createBufferSource();
        src.buffer = buffer;
        src.connect(c.destination);
      } catch (err) {
        onError("source", (err && err.message) || "创建播放源失败");
        return;
      }
      var myGen = generation;
      var startAt = Math.max(c.currentTime, nextStartTime);
      outstanding++;
      liveSources.push(src);
      src.onended = function () {
        removeFromLive(src);
        if (myGen !== generation) return;   // stop() 后的迟到回调：作废，不动 outstanding
        outstanding--;
        maybeDrained();
      };
      try {
        src.start(startAt);
      } catch (err) {
        removeFromLive(src);
        outstanding--;   // 回滚：start 失败不会有 onended
        onError("start", (err && err.message) || "启动播放失败");
        return;
      }
      nextStartTime = startAt + buffer.duration;
    }

    // 停止当前播报：作废迟到 onended、断所有在播 source、复位 drain 状态。
    // **保留 ctx 不关**（手势内已 unlock，复用避免重建丢失解锁状态）。幂等，可重复调不抛。
    function stop() {
      generation++;      // 既有 source 的 onended 比对失败 → 作废
      ended = false;
      outstanding = 0;
      nextStartTime = 0;
      for (var i = 0; i < liveSources.length; i++) {
        var src = liveSources[i];
        try { src.stop(); } catch (_) {}
        try { src.disconnect(); } catch (_) {}
      }
      liveSources = [];
    }

    // 离开语音模式时释放：停掉在播音源并关闭 ctx。之后再用会按需重建（但需重新 unlock）。
    function dispose() {
      stop();
      try { if (ctx) ctx.close(); } catch (_) {}
      ctx = null;
    }

    function isActive() {
      return outstanding > 0;
    }

    return { enqueue: enqueue, stop: stop, unlock: unlock, markEnded: markEnded, isActive: isActive, dispose: dispose };
  }

  // 暴露纯函数以便单测。
  createPcmPlayer.pcm16ToFloat32 = pcm16ToFloat32;
  return createPcmPlayer;
});
