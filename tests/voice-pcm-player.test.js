/* 流式 PCM 播放器回归：
 * 【B1】drain gate 在 markEnded 之后（帧间空隙不误判读完）
 * 【B2】stop 不关 ctx、generation 作废迟到 onended
 * 【B4】奇数尾字节跨帧 carry 对齐（HTTP 分块切在 Int16 中间 → 蜂鸣）
 * 【D1】closed ctx 丢弃重建时复位调度游标/在播计数/carry
 */
const test = require("node:test");
const assert = require("node:assert");
const createVoicePcmPlayer = require("../nano_openclaw/gateway/webui/static/voice-pcm-player.js");
const { pcm16ToFloat32 } = createVoicePcmPlayer;

// ── Fake Web Audio ──────────────────────────────────────────────────────────
function makeFakeCtx() {
  const ctx = {
    state: "running",
    currentTime: 0,
    destination: {},
    resumed: 0,
    closed: 0,
    sources: [],
    buffers: [],
    resume() { this.resumed++; if (this.state === "suspended") this.state = "running"; },
    close() { this.closed++; this.state = "closed"; },
    createBuffer(channels, length, rate) {
      const data = new Float32Array(length);
      const buf = { duration: length / rate, getChannelData: () => data, _data: data };
      this.buffers.push(buf);
      return buf;
    },
    createBufferSource() {
      const src = {
        buffer: null, started: null, stopped: 0, disconnected: 0, onended: null,
        connect() {},
        start(at) { this.started = at; },
        stop() { this.stopped++; },
        disconnect() { this.disconnected++; },
      };
      this.sources.push(src);
      return src;
    },
  };
  return ctx;
}

// 假时钟：drain 尾延迟补偿是单一计时器（同时只有一个 pending）
function fakeClock() {
  let pending = null, lastDelay = null, id = 0;
  return {
    setTimer(fn, delay) { pending = fn; lastDelay = delay; return ++id; },
    clearTimer() { pending = null; lastDelay = null; },
    fire() { const p = pending; pending = null; if (p) p(); },
    armed() { return pending !== null; },
    delay() { return lastDelay; },
  };
}

function makePlayer(ctx, hooks) {
  hooks = hooks || {};
  let drained = 0, audible = 0;
  const errors = [];
  const clock = fakeClock();
  const player = createVoicePcmPlayer({
    sampleRate: 16000,
    AudioCtxImpl: function () { return hooks.nextCtx ? hooks.nextCtx() : ctx; },
    onDrained: () => drained++,
    onAudible: () => audible++,
    onError: (name, msg) => errors.push([name, msg]),
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });
  return { player, drainedCount: () => drained, audibleCount: () => audible, errors, clock };
}

// Int16 小端样本数组 → ArrayBuffer
function pcmBuf(samples) {
  const buf = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buf);
  samples.forEach((s, i) => view.setInt16(i * 2, s, true));
  return buf;
}

test("pcm16ToFloat32：-32768→-1、32767→~1、0→0", () => {
  const out = pcm16ToFloat32(pcmBuf([-32768, 32767, 0]));
  assert.strictEqual(out[0], -1);
  assert.ok(Math.abs(out[1] - 1) < 1e-4);
  assert.strictEqual(out[2], 0);
});

test("B4：奇数尾字节跨帧 carry——两帧拼接后样本完整且对齐", () => {
  const ctx = makeFakeCtx();
  const { player } = makePlayer(ctx);
  // 完整流是 2 个样本（4 字节），切成 3+1 字节两帧（边界在样本中间）
  const whole = new Uint8Array(pcmBuf([1000, -2000]));
  player.enqueue(whole.slice(0, 3).buffer);
  player.enqueue(whole.slice(3).buffer);
  // 第一帧只解出 1 个对齐样本，第二帧 carry+1 字节解出第 2 个样本
  assert.strictEqual(ctx.buffers.length, 2);
  const all = [...ctx.buffers[0]._data, ...ctx.buffers[1]._data];
  const expect = pcm16ToFloat32(pcmBuf([1000, -2000]));
  assert.deepStrictEqual(all.map((x) => x.toFixed(6)), [...expect].map((x) => x.toFixed(6)));
});

test("B4：单字节帧只入 carry 不出声；凑齐后补出完整样本", () => {
  const ctx = makeFakeCtx();
  const { player } = makePlayer(ctx);
  const whole = new Uint8Array(pcmBuf([12345]));
  player.enqueue(whole.slice(0, 1).buffer);
  assert.strictEqual(ctx.buffers.length, 0, "不足一个样本不应排程");
  player.enqueue(whole.slice(1).buffer);
  assert.strictEqual(ctx.buffers.length, 1);
  assert.ok(Math.abs(ctx.buffers[0]._data[0] - pcm16ToFloat32(pcmBuf([12345]))[0]) < 1e-6);
});

test("B1：帧间空隙 outstanding 归零不 drain；markEnded 后最后一帧结束 + 尾延迟补偿到点才 drain", () => {
  const ctx = makeFakeCtx();
  const { player, drainedCount, clock } = makePlayer(ctx);
  player.enqueue(pcmBuf([1, 2, 3, 4]));
  ctx.sources[0].onended();                       // 流中途：在播归零
  assert.ok(!clock.armed(), "未 markEnded 连补偿计时器都不该起（防提前开麦回采）");
  player.enqueue(pcmBuf([5, 6]));
  player.markEnded();                             // SynthesisCompleted
  assert.strictEqual(drainedCount(), 0, "还有在播 source，等它结束");
  ctx.sources[1].onended();
  assert.strictEqual(drainedCount(), 0, "渲染完≠扬声器放完：先等输出尾延迟补偿");
  clock.fire();
  assert.strictEqual(drainedCount(), 1, "补偿到点 → drain 一次");
});

test("B1：markEnded 时已无在播音频 → 起补偿计时器，到点 drain", () => {
  const ctx = makeFakeCtx();
  const { player, drainedCount, clock } = makePlayer(ctx);
  player.markEnded();
  assert.strictEqual(drainedCount(), 0);
  clock.fire();
  assert.strictEqual(drainedCount(), 1);
});

test("尾延迟补偿时长：按 ctx.outputLatency+baseLatency+150ms 计；不支持时保守 300ms+150ms", () => {
  const ctx = makeFakeCtx();
  ctx.outputLatency = 0.8;                        // 蓝牙/车机 A2DP 链路
  ctx.baseLatency = 0.05;
  const a = makePlayer(ctx);
  a.player.enqueue(pcmBuf([1, 2]));               // 先建 ctx（延迟从 ctx 读取）
  ctx.sources[0].onended();
  a.player.markEnded();
  assert.ok(Math.abs(a.clock.delay() - 1000) < 1e-6, `0.85s 链路延迟 + 150ms 余量 ≈ 1000ms（实际 ${a.clock.delay()}）`);

  const ctx2 = makeFakeCtx();                     // 无 outputLatency（老内核）
  const b = makePlayer(ctx2);
  b.player.enqueue(pcmBuf([1, 2]));               // 先建 ctx
  ctx2.sources[0].onended();
  b.player.markEnded();
  assert.strictEqual(b.clock.delay(), 300 + 150, "内核不支持时保守按 300ms 估算");
});

test("补偿等待期间 stop()：撤销计时器不 drain；等待期间又来音频也不 drain", () => {
  const ctx = makeFakeCtx();
  const a = makePlayer(ctx);
  a.player.markEnded();
  assert.ok(a.clock.armed());
  a.player.stop();
  a.clock.fire();                                 // 已被 clearTimer 清掉，fire 无效
  assert.strictEqual(a.drainedCount(), 0, "stop 后不得补一发 drain");

  const ctx2 = makeFakeCtx();
  const b = makePlayer(ctx2);
  b.player.markEnded();
  b.player.stop();                                // 复位 ended
  b.player.enqueue(pcmBuf([1, 2]));               // 新一轮音频
  b.clock.fire();
  assert.strictEqual(b.drainedCount(), 0, "新音频在播时残留计时器不得误 drain");
});

test("B2：stop 不关 ctx；迟到 onended 被 generation 作废，不触发 drain", () => {
  const ctx = makeFakeCtx();
  const { player, drainedCount, clock } = makePlayer(ctx);
  player.enqueue(pcmBuf([1, 2]));
  const src = ctx.sources[0];
  player.stop();
  assert.strictEqual(ctx.closed, 0, "stop 不得关闭 ctx（会丢手势解锁态）");
  assert.ok(src.stopped >= 1, "在播 source 应被停掉");
  player.markEnded();
  src.onended();                                  // stop 前排程的迟到回调
  clock.fire();
  assert.strictEqual(drainedCount(), 1, "迟到 onended 不改计数：markEnded 时已归零，补偿到点 drain 仅一次");
});

test("无缝排队：游标按 duration 前移，第二帧接在第一帧之后", () => {
  const ctx = makeFakeCtx();
  const { player } = makePlayer(ctx);
  player.enqueue(pcmBuf(new Array(1600).fill(0)));   // 100ms
  player.enqueue(pcmBuf(new Array(1600).fill(0)));
  assert.strictEqual(ctx.sources[0].started, 0);
  assert.ok(Math.abs(ctx.sources[1].started - 0.1) < 1e-9, "第二帧应从 0.1s 起播");
});

test("D1：closed ctx 丢弃重建——在途播放先补一发 drain 解卡，新 ctx 从零时间线起播", () => {
  const ctx1 = makeFakeCtx();
  const ctx2 = makeFakeCtx();
  let calls = 0;
  const { player, drainedCount, clock } = makePlayer(null, { nextCtx: () => (++calls === 1 ? ctx1 : ctx2) });
  player.enqueue(pcmBuf(new Array(1600).fill(0)));
  ctx1.state = "closed";                           // 锁屏/系统回收
  player.enqueue(pcmBuf(new Array(160).fill(0)));
  assert.strictEqual(drainedCount(), 1, "在途播放（outstanding>0）被 closed 杀死：孤儿 onended 永不来，必须补 drain 解卡");
  assert.strictEqual(ctx2.sources.length, 1, "应在新 ctx 上排程");
  assert.strictEqual(ctx2.sources[0].started, 0, "新 ctx 不得继承陈旧 nextStartTime（长静音）");
  player.markEnded();
  ctx2.sources[0].onended();
  clock.fire();
  assert.strictEqual(drainedCount(), 2, "新一轮播放正常 drain");
});

test("D1/解卡：speaking 期间 ctx 被 OS 关闭 → unlock()（回前台恢复路径）触发补 drain，不卡『朗读中』", () => {
  const ctx1 = makeFakeCtx();
  const ctx2 = makeFakeCtx();
  let calls = 0;
  const { player, drainedCount } = makePlayer(null, { nextCtx: () => (++calls === 1 ? ctx1 : ctx2) });
  player.enqueue(pcmBuf([1, 2]));
  player.markEnded();                              // 字节投完，等播放器 drain
  ctx1.state = "closed";                           // 锁屏把 ctx 杀成 closed：onended 永不来
  player.unlock();                                 // VISIBLE → recoverSpeechOutput → unlock
  assert.strictEqual(drainedCount(), 1, "speaking 没有别的出口，closed 重建必须补 drain");
});

test("D1/解卡：提供 onInterrupted 时解卡走它而非 onDrained（正常读完仍走 onDrained）", () => {
  const ctx1 = makeFakeCtx();
  const ctx2 = makeFakeCtx();
  let calls = 0, drained = 0, interrupted = 0;
  const clock = fakeClock();
  const player = createVoicePcmPlayer({
    sampleRate: 16000,
    AudioCtxImpl: function () { return ++calls === 1 ? ctx1 : ctx2; },   // 需可 new
    onDrained: () => drained++,
    onInterrupted: () => interrupted++,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });
  player.enqueue(pcmBuf([1, 2]));
  ctx1.state = "closed";
  player.unlock();                                 // 解卡路径
  assert.strictEqual(interrupted, 1, "解卡≠正常读完：上层要先掐引擎");
  assert.strictEqual(drained, 0);
  player.enqueue(pcmBuf([3, 4]));                  // 新一轮正常播放
  player.markEnded();
  ctx2.sources[0].onended();
  clock.fire();
  assert.strictEqual(drained, 1, "正常读完仍走 onDrained");
  assert.strictEqual(interrupted, 1);
});

test("D1/解卡：空闲时 ctx closed 重建不补 drain（无在途播放不得误推进状态机）", () => {
  const ctx1 = makeFakeCtx();
  const ctx2 = makeFakeCtx();
  let calls = 0;
  const { player, drainedCount } = makePlayer(null, { nextCtx: () => (++calls === 1 ? ctx1 : ctx2) });
  player.unlock();                                 // 建出 ctx1
  ctx1.state = "closed";
  player.unlock();                                 // 重建：此刻无 outstanding/ended
  assert.strictEqual(drainedCount(), 0);
});

test("B5：onAudible 仅在首个音源真正排程成功时上报一次——字节到达不算出过声", () => {
  const ctx = makeFakeCtx();
  const { player, audibleCount } = makePlayer(ctx);
  assert.strictEqual(audibleCount(), 0);
  player.enqueue(pcmBuf([1, 2]));
  assert.strictEqual(audibleCount(), 1, "首个 source.start 成功 → 出过声");
  player.enqueue(pcmBuf([3, 4]));
  assert.strictEqual(audibleCount(), 1, "本轮只报一次");
  player.stop();
  player.enqueue(pcmBuf([5, 6]));
  assert.strictEqual(audibleCount(), 2, "stop 复位后新一轮重新上报");
});

test("B5：source.start 抛错 → 不上报 onAudible（无声不算出过声），上报 onError", () => {
  const ctx = makeFakeCtx();
  const origCreate = ctx.createBufferSource.bind(ctx);
  ctx.createBufferSource = () => {
    const src = origCreate();
    src.start = () => { throw new Error("InvalidStateError"); };
    return src;
  };
  const { player, audibleCount, errors } = makePlayer(ctx);
  player.enqueue(pcmBuf([1, 2]));
  assert.strictEqual(audibleCount(), 0, "起不来的音源不算出过声（零发声重投的依据）");
  assert.ok(errors.some(([n]) => n === "start"));
});

test("B2：unlock 在 suspended ctx 上 resume；enqueue 防御性 resume", () => {
  const ctx = makeFakeCtx();
  ctx.state = "suspended";
  const { player } = makePlayer(ctx);
  player.unlock();
  assert.ok(ctx.resumed >= 1);
  ctx.state = "suspended";
  player.enqueue(pcmBuf([1, 2]));
  assert.ok(ctx.resumed >= 2, "enqueue 遇 suspended 也应尝试 resume");
});

test("dispose：停在播音源并关闭 ctx", () => {
  const ctx = makeFakeCtx();
  const { player } = makePlayer(ctx);
  player.enqueue(pcmBuf([1, 2]));
  player.dispose();
  assert.strictEqual(ctx.closed, 1);
});

test("isActive：有在播 source 为真，结束/停止后为假", () => {
  const ctx = makeFakeCtx();
  const { player } = makePlayer(ctx);
  assert.strictEqual(player.isActive(), false);
  player.enqueue(pcmBuf([1, 2]));
  assert.strictEqual(player.isActive(), true);
  ctx.sources[0].onended();
  assert.strictEqual(player.isActive(), false);
});
