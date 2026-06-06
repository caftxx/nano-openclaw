/* 流式 PCM 播放器 —— Web Audio 无缝排队播放 Int16 单声道小端 PCM。
 *
 * 云端合成引擎（流式 / RESTful 代理）只投二进制 PCM 帧与生命周期事件，本播放器
 * 负责解码排程。与协议完全解耦，node --test 可测（AudioContext 注入）。
 *
 * 排队：维护 nextStartTime 游标，每帧建一段 AudioBuffer 在
 * max(ctx.currentTime, nextStartTime) 起播，游标前移 duration → 帧间无缝。
 *
 * 历史坑位（语义必须保持）：
 *  - 【B1】drain 判定 gate 在 markEnded()（上游 SynthesisCompleted / 文本流收尾）之后：
 *    流中途的帧间空隙会让在播 source 数瞬间归零，不 gate 会误判「读完」提前开麦，
 *    外放被麦克风回采。
 *  - 【B2】ctx 长生命周期可复用：移动端 Chrome 要求 AudioContext 在用户手势内
 *    创建/resume，否则停在 suspended——不出声且 source 永不 onended → 永不 drain →
 *    状态机卡死。上层必须在手势里调 unlock()；stop() 只停在播音源**不关 ctx**
 *    （重建出的 ctx 是 suspended，丢失解锁态）；离开语音模式才 dispose()。
 *    stop() 用 generation 自增作废迟到 onended，复用时旧回调不污染计数。
 *  - 【B4】HTTP 分块边界可能切在 Int16 样本中间：跨帧保留奇数尾字节（carry），
 *    保证送进解码的永远是 2 字节对齐 buffer，否则整段字节错位 1 字节 → 刺耳蜂鸣。
 *  - 【D1】锁屏/系统回收可能把 ctx 变 closed：closed ctx 无法 resume/排程，必须
 *    丢弃重建，且调度游标/在播计数/carry 一并复位——否则新 ctx 继承陈旧
 *    nextStartTime（长静音），孤儿 source 不 onended → 永不 drain。
 *
 * UMD：node --test 取 createVoicePcmPlayer.pcm16ToFloat32 做纯函数单测，
 * 浏览器挂 window.createVoicePcmPlayer。
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.createVoicePcmPlayer = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // Int16 小端 PCM（ArrayBuffer）→ Float32Array [-1,1]：负/0x8000、正/0x7fff，
  // 保证 -32768→-1、32767→~1、0→0。
  function pcm16ToFloat32(arrayBuffer) {
    var view = new DataView(arrayBuffer);
    var n = (arrayBuffer.byteLength / 2) | 0;
    var out = new Float32Array(n);
    for (var i = 0; i < n; i++) {
      var v = view.getInt16(i * 2, true);
      out[i] = v < 0 ? v / 0x8000 : v / 0x7fff;
    }
    return out;
  }

  // 工厂：opts = { sampleRate, onDrained, onAudible, onInterrupted, onError,
  //               AudioCtxImpl, setTimer, clearTimer }
  //   onAudible：本轮**真正排程成功首个音源**时回调一次——「字节到达」不等于「出过声」，
  //   ctx 建不起来/source 起不来时字节进来也是无声的，上层（回退链）据此判断零发声重投【B5】。
  //   onInterrupted：closed-ctx 解卡专用（区别于正常读完的 onDrained）——此时合成引擎
  //   可能仍在向重建后的 ctx 流尾部帧，上层须先掐断引擎再推进状态机，否则 mic 重开时
  //   尾音还在出声（自回声）。未提供时退化为 onDrained。
  function createVoicePcmPlayer(opts) {
    opts = opts || {};
    var sampleRate = opts.sampleRate || 16000;
    var onDrained = opts.onDrained || function () {};
    var onAudible = opts.onAudible || function () {};
    var onInterrupted = opts.onInterrupted || function () { onDrained(); };
    var onError = opts.onError || function () {};
    var setTimer = opts.setTimer || setTimeout;
    var clearTimer = opts.clearTimer || clearTimeout;

    var ctx = null;
    var nextStartTime = 0;   // 下一帧应起播的 ctx 时间线位置
    var outstanding = 0;     // 已排程仍未结束的 source 数
    var ended = false;       // markEnded() 后置位，开启 drain 判定【B1】
    var generation = 0;      // stop() 自增；onended 比对作废迟到回调【B2】
    var liveSources = [];    // 在播 source（stop 时统一 stop+disconnect）
    var carry = null;        // 上帧遗留的奇数尾字节【B4】
    var drainTimer = null;   // 输出尾延迟补偿计时器（见 maybeDrained）
    var audibleFired = false; // 本轮是否已上报 onAudible（stop/重建复位）

    function getImpl() {
      if (opts.AudioCtxImpl) return opts.AudioCtxImpl;
      if (typeof window !== "undefined") return window.AudioContext || window.webkitAudioContext;
      return null;
    }

    // 复位调度状态（不触发 onDrained：ended 同步复位，挂起的尾延迟补偿一并撤销）。
    function resetSchedule() {
      generation++;
      ended = false;
      outstanding = 0;
      nextStartTime = 0;
      carry = null;
      liveSources = [];
      audibleFired = false;
      if (drainTimer != null) { clearTimer(drainTimer); drainTimer = null; }
    }

    // 懒建 ctx；closed 则丢弃重建并复位调度状态【D1】；建失败 onError 不抛。
    // 重建时若有在途播放（outstanding>0 / 已 markEnded 待 drain）：孤儿 source 的
    // onended 永不再来，复位后必须补一发解卡信号——否则锁屏把 ctx 杀成 closed 的
    // 场景里，状态机永远停在「朗读中」（speaking 没有别的出口）。走 onInterrupted
    // 而非 onDrained：turn.done 已到但 SynthesisCompleted 未到的窗口里，引擎仍在向
    // 重建后的 ctx 流尾部帧，上层须先掐断引擎再续听。
    function ensureCtx() {
      if (ctx && ctx.state === "closed") {
        var hadFlight = outstanding > 0 || ended || drainTimer != null;
        ctx = null;
        resetSchedule();
        if (hadFlight) onInterrupted();
      }
      if (ctx) return ctx;
      var Impl = getImpl();
      if (!Impl) { onError("no-audio-context", "AudioContext 不可用"); return null; }
      try { ctx = new Impl(); }
      catch (err) {
        ctx = null;
        onError("audio-context", (err && err.message) || "创建 AudioContext 失败");
        return null;
      }
      return ctx;
    }

    // 用户手势内调用：建 ctx 并 resume【B2】。
    function unlock() {
      var c = ensureCtx();
      if (c && c.state === "suspended") { try { c.resume(); } catch (_) {} }
    }

    // 输出尾延迟补偿：source.onended 是「渲染完」不是「扬声器放完」——WebAudio 的
    // 渲染游标领先于物理输出，蓝牙/车机（A2DP）链路 outputLatency 可达 0.5~1s。
    // 立即 drain 会让状态机提前进入续听、识别麦（AEC）一开通信模式切换音频路由，
    // 把还在输出管线里的最后几个字直接杀掉（实测：每句话尾巴丢几个字，蓝牙越久越明显）。
    // 这里按 ctx.outputLatency + baseLatency + 150ms 余量延迟上报 drain；内核不支持
    // outputLatency（老内核）时保守按 300ms 估算。
    function drainDelayMs() {
      var out = 0.3, base = 0;
      try {
        if (ctx && typeof ctx.outputLatency === "number") out = ctx.outputLatency;
        if (ctx && typeof ctx.baseLatency === "number") base = ctx.baseLatency;
      } catch (_) {}
      var ms = (out + base) * 1000 + 150;
      return Math.max(150, Math.min(2500, ms));
    }
    function maybeDrained() {
      if (!(ended && outstanding === 0)) return;
      if (drainTimer != null) return;   // 已在补偿等待中
      var myGen = generation;
      drainTimer = setTimer(function () {
        drainTimer = null;
        if (myGen !== generation) return;            // stop()/重建已作废
        if (ended && outstanding === 0) onDrained(); // 等待期间又来了音频则不 drain
      }, drainDelayMs());
    }

    // 上游音频流收尾（SynthesisCompleted / 队列排空）：开启 drain 判定；
    // 若此刻已无在播音频，立即 drain。
    function markEnded() {
      ended = true;
      maybeDrained();
    }

    function enqueue(arrayBuffer) {
      if (!arrayBuffer || !arrayBuffer.byteLength) return;
      var c = ensureCtx();
      if (!c) return;
      // 防御性 resume：非手势路径建出 / 被系统挂起的 ctx，不 resume 则 source 永不播。
      if (c.state === "suspended") { try { c.resume(); } catch (_) {} }

      // 【B4】2 字节对齐：拼上 carry，偶数部分解码，奇数尾字节留作下帧 carry。
      var incoming = new Uint8Array(arrayBuffer);
      var total = (carry ? 1 : 0) + incoming.length;
      var usable = total - (total % 2);
      var nextCarry = (total % 2) ? new Uint8Array([incoming[incoming.length - 1]]) : null;
      if (usable < 2) { carry = nextCarry; return; }
      var aligned = new Uint8Array(usable);
      var off = 0;
      if (carry) { aligned[0] = carry[0]; off = 1; }
      aligned.set(incoming.subarray(0, usable - off), off);
      carry = nextCarry;

      var floats;
      try { floats = pcm16ToFloat32(aligned.buffer); }
      catch (err) { onError("decode", (err && err.message) || "PCM 解码失败"); return; }
      if (!floats.length) return;

      var buffer;
      try {
        buffer = c.createBuffer(1, floats.length, sampleRate);
        buffer.getChannelData(0).set(floats);
      } catch (err) { onError("buffer", (err && err.message) || "创建 AudioBuffer 失败"); return; }

      var src;
      try {
        src = c.createBufferSource();
        src.buffer = buffer;
        src.connect(c.destination);
      } catch (err) { onError("source", (err && err.message) || "创建播放源失败"); return; }

      var myGen = generation;
      var startAt = Math.max(c.currentTime, nextStartTime);
      outstanding++;
      liveSources.push(src);
      src.onended = function () {
        var idx = liveSources.indexOf(src);
        if (idx >= 0) liveSources.splice(idx, 1);
        if (myGen !== generation) return;   // stop() 后的迟到回调：作废
        outstanding--;
        maybeDrained();
      };
      try { src.start(startAt); }
      catch (err) {
        var j = liveSources.indexOf(src);
        if (j >= 0) liveSources.splice(j, 1);
        outstanding--;                       // start 失败不会有 onended，回滚
        onError("start", (err && err.message) || "启动播放失败");
        return;
      }
      nextStartTime = startAt + buffer.duration;
      // 首个音源真正排程成功才算「出过声」【B5】——字节到达不算。
      if (!audibleFired) { audibleFired = true; onAudible(); }
    }

    // 停止当前播报：断所有在播 source、作废迟到 onended、复位 drain 状态。
    // 保留 ctx 不关【B2】。幂等。
    function stop() {
      var sources = liveSources;
      resetSchedule();
      for (var i = 0; i < sources.length; i++) {
        try { sources[i].stop(); } catch (_) {}
        try { sources[i].disconnect(); } catch (_) {}
      }
    }

    // 离开语音模式：停掉在播音源并关闭 ctx（之后再用需重新 unlock）。
    function dispose() {
      stop();
      try { if (ctx) ctx.close(); } catch (_) {}
      ctx = null;
    }

    function isActive() { return outstanding > 0; }

    return { enqueue: enqueue, stop: stop, unlock: unlock, markEnded: markEnded, isActive: isActive, dispose: dispose };
  }

  createVoicePcmPlayer.pcm16ToFloat32 = pcm16ToFloat32;
  return createVoicePcmPlayer;
});
