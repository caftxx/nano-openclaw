/* 流式 PCM 播放器单测：纯函数 pcm16ToFloat32 的转换正确性，以及注入 FakeAudioContext
 * 验证 enqueue 排程 + onDrained 触发、unlock 解锁、stop 后复用、generation 作废迟到回调
 * （Web Audio 本身无法在 node 跑，故注入桩）。 */
const test = require("node:test");
const assert = require("node:assert");
const createPcmPlayer = require("../nano_openclaw/gateway/webui/static/voice-pcm-player.js");

const { pcm16ToFloat32 } = createPcmPlayer;

// 用已知 Int16 值构造小端 ArrayBuffer。
function int16Buffer(values) {
  const ab = new ArrayBuffer(values.length * 2);
  const view = new DataView(ab);
  values.forEach((v, i) => view.setInt16(i * 2, v, true));
  return ab;
}

test("pcm16ToFloat32: 0 / 32767 / -32768 转换值正确", () => {
  const floats = pcm16ToFloat32(int16Buffer([0, 32767, -32768]));
  assert.strictEqual(floats.length, 3);
  assert.strictEqual(floats[0], 0);
  assert.ok(Math.abs(floats[1] - 1.0) < 1e-4, `期望 ~1.0，实际 ${floats[1]}`);
  assert.strictEqual(floats[2], -1.0);   // -32768 / 0x8000 = -1 精确
});

test("pcm16ToFloat32: 空 buffer → 空数组", () => {
  const floats = pcm16ToFloat32(new ArrayBuffer(0));
  assert.strictEqual(floats.length, 0);
});

// ── FakeAudioContext：记录建 buffer/source、start 时间、可手动触发 onended ──
// 支持 state（默认 "running"，可由测试设为 "suspended"）+ resume()（计数）。
// source 增加 stop()/disconnect()（stop() 用到）。
function makeFakeCtx(sources, opts) {
  opts = opts || {};
  return class FakeAudioContext {
    constructor() {
      this.currentTime = 0;
      this.destination = {};
      this.closed = false;
      this.state = opts.initialState || "running";
      this.resumeCalls = 0;
      if (opts.onCreate) opts.onCreate(this);
    }
    resume() { this.resumeCalls++; this.state = "running"; return Promise.resolve(); }
    createBuffer(channels, length, sampleRate) {
      const data = new Float32Array(length);
      return {
        duration: length / sampleRate,
        getChannelData: () => data,
      };
    }
    createBufferSource() {
      const src = {
        buffer: null,
        onended: null,
        startedAt: null,
        stopped: false,
        disconnected: false,
        connect() {},
        start(at) { this.startedAt = at; },
        stop() { this.stopped = true; },
        disconnect() { this.disconnected = true; },
      };
      sources.push(src);
      return src;
    }
    close() { this.closed = true; }
  };
}

test("enqueue: 无缝排程 + markEnded 且全部 onended 后触发 onDrained", () => {
  const sources = [];
  let drained = 0;
  const player = createPcmPlayer({
    sampleRate: 16000,
    AudioCtxImpl: makeFakeCtx(sources),
    onDrained: () => { drained++; },
  });
  // 两帧各 1600 采样（=0.1s @16k）
  player.enqueue(int16Buffer(new Array(1600).fill(0)));
  player.enqueue(int16Buffer(new Array(1600).fill(0)));
  assert.strictEqual(sources.length, 2);
  assert.strictEqual(sources[0].startedAt, 0);
  // 第二帧应接在第一帧之后（0.1s 处）
  assert.ok(Math.abs(sources[1].startedAt - 0.1) < 1e-6, `期望 0.1，实际 ${sources[1].startedAt}`);
  assert.strictEqual(player.isActive(), true);
  // 未 markEnded 前，即便逐个结束、outstanding 归零也不 drain（流中途空隙不误判）
  sources[0].onended();
  assert.strictEqual(drained, 0);
  sources[1].onended();
  assert.strictEqual(drained, 0);
  assert.strictEqual(player.isActive(), false);
  // markEnded（SynthesisCompleted）后、outstanding 已 0 → 立即 drain 一次
  player.markEnded();
  assert.strictEqual(drained, 1);
});

test("gating: markEnded 在 outstanding>0 时不 drain，待 onended 后 drain 一次", () => {
  const sources = [];
  let drained = 0;
  const player = createPcmPlayer({
    sampleRate: 16000,
    AudioCtxImpl: makeFakeCtx(sources),
    onDrained: () => { drained++; },
  });
  player.enqueue(int16Buffer(new Array(1600).fill(0)));
  assert.strictEqual(sources.length, 1);
  // 音频还在播（outstanding=1）就收到 SynthesisCompleted → 不 drain，等播完
  player.markEnded();
  assert.strictEqual(drained, 0);
  assert.strictEqual(player.isActive(), true);
  // source 播完 → drain 触发恰好一次
  sources[0].onended();
  assert.strictEqual(drained, 1);
  assert.strictEqual(player.isActive(), false);
});

test("gating: stop 复位 ended，迟到 onended 不致误触发 drain", () => {
  const sources = [];
  let drained = 0;
  const player = createPcmPlayer({
    sampleRate: 16000,
    AudioCtxImpl: makeFakeCtx(sources),
    onDrained: () => { drained++; },
  });
  player.enqueue(int16Buffer(new Array(1600).fill(0)));
  player.markEnded();              // 置 ended（outstanding=1，不 drain）
  assert.strictEqual(drained, 0);
  player.stop();                   // 复位 ended、outstanding；generation 自增作废迟到回调
  assert.strictEqual(drained, 0);
  // stop 后那条旧 source 的迟到 onended：generation 不匹配 → 不触发 drain
  sources[0].onended();
  assert.strictEqual(drained, 0);
});

test("stop: 幂等、复位、停断在播 source 且保留 ctx，迟到 onended 不触发 onDrained", () => {
  const sources = [];
  let drained = 0;
  let createdCtx = null;
  const player = createPcmPlayer({
    sampleRate: 16000,
    AudioCtxImpl: makeFakeCtx(sources, { onCreate: (c) => { createdCtx = c; } }),
    onDrained: () => { drained++; },
  });
  player.enqueue(int16Buffer(new Array(1600).fill(0)));
  assert.doesNotThrow(() => { player.stop(); player.stop(); });
  // stop 停断了在播 source
  assert.strictEqual(sources[0].stopped, true);
  assert.strictEqual(sources[0].disconnected, true);
  // stop 不关 ctx（保留复用）
  assert.strictEqual(createdCtx.closed, false);
  // 迟到 onended（generation 已变）→ 早退、不减 outstanding、不 drain
  sources[0].onended();
  assert.strictEqual(drained, 0);
  assert.strictEqual(player.isActive(), false);
});

test("unlock: suspended 时 resume，running 时不报错", () => {
  // suspended：unlock 应触发 resume
  {
    const sources = [];
    let createdCtx = null;
    const player = createPcmPlayer({
      sampleRate: 16000,
      AudioCtxImpl: makeFakeCtx(sources, { initialState: "suspended", onCreate: (c) => { createdCtx = c; } }),
    });
    player.unlock();
    assert.strictEqual(createdCtx.resumeCalls, 1);
    assert.strictEqual(createdCtx.state, "running");
  }
  // running：unlock 不报错、不调 resume
  {
    const sources = [];
    let createdCtx = null;
    const player = createPcmPlayer({
      sampleRate: 16000,
      AudioCtxImpl: makeFakeCtx(sources, { initialState: "running", onCreate: (c) => { createdCtx = c; } }),
    });
    assert.doesNotThrow(() => player.unlock());
    assert.strictEqual(createdCtx.resumeCalls, 0);
  }
});

test("enqueue: ctx 处于 suspended 时防御性 resume", () => {
  const sources = [];
  let createdCtx = null;
  const player = createPcmPlayer({
    sampleRate: 16000,
    AudioCtxImpl: makeFakeCtx(sources, { initialState: "suspended", onCreate: (c) => { createdCtx = c; } }),
  });
  player.enqueue(int16Buffer(new Array(1600).fill(0)));
  assert.strictEqual(createdCtx.resumeCalls, 1);
  assert.strictEqual(sources.length, 1);
});

test("可复用：stop 后再次 enqueue→onended→markEnded 能再次 drain，ctx 未关", () => {
  const sources = [];
  let drained = 0;
  let createdCtx = null;
  const player = createPcmPlayer({
    sampleRate: 16000,
    AudioCtxImpl: makeFakeCtx(sources, { onCreate: (c) => { createdCtx = c; } }),
    onDrained: () => { drained++; },
  });
  // 第一轮
  player.enqueue(int16Buffer(new Array(1600).fill(0)));
  sources[0].onended();
  player.markEnded();
  assert.strictEqual(drained, 1);
  // stop（保留 ctx）
  player.stop();
  assert.strictEqual(createdCtx.closed, false);
  // 第二轮：复用同一 ctx（FakeCtx 只建一次，sources 累积进同一数组）
  player.enqueue(int16Buffer(new Array(1600).fill(0)));
  assert.strictEqual(sources.length, 2);
  sources[1].onended();
  player.markEnded();
  assert.strictEqual(drained, 2);
});

test("generation: stop 后旧 source 的迟到 onended 不触发 drain、不让 outstanding 转负", () => {
  const sources = [];
  let drained = 0;
  const player = createPcmPlayer({
    sampleRate: 16000,
    AudioCtxImpl: makeFakeCtx(sources),
    onDrained: () => { drained++; },
  });
  player.enqueue(int16Buffer(new Array(1600).fill(0)));
  assert.strictEqual(player.isActive(), true);
  player.stop();                   // generation++，outstanding 复位 0
  assert.strictEqual(player.isActive(), false);
  // 旧 source 迟到 onended：generation 不匹配 → 早退，不动 outstanding、不 drain
  sources[0].onended();
  assert.strictEqual(player.isActive(), false);   // 没被减成负
  assert.strictEqual(drained, 0);                 // 迟到回调没触发 drain
});

test("dispose: stop 在播 source 并关闭 ctx", () => {
  const sources = [];
  let createdCtx = null;
  const player = createPcmPlayer({
    sampleRate: 16000,
    AudioCtxImpl: makeFakeCtx(sources, { onCreate: (c) => { createdCtx = c; } }),
  });
  player.enqueue(int16Buffer(new Array(1600).fill(0)));
  player.dispose();
  assert.strictEqual(sources[0].stopped, true);
  assert.strictEqual(createdCtx.closed, true);
});

test("enqueue: 空 buffer 忽略，不建 source", () => {
  const sources = [];
  const player = createPcmPlayer({ sampleRate: 16000, AudioCtxImpl: makeFakeCtx(sources) });
  player.enqueue(new ArrayBuffer(0));
  player.enqueue(null);
  assert.strictEqual(sources.length, 0);
});
