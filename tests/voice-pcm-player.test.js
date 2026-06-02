/* 流式 PCM 播放器单测：纯函数 pcm16ToFloat32 的转换正确性，以及注入 FakeAudioContext
 * 验证 enqueue 排程 + onDrained 触发（Web Audio 本身无法在 node 跑，故注入桩）。 */
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
function makeFakeCtx(sources) {
  return class FakeAudioContext {
    constructor() {
      this.currentTime = 0;
      this.destination = {};
      this.closed = false;
    }
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
        connect() {},
        start(at) { this.startedAt = at; },
      };
      sources.push(src);
      return src;
    }
    close() { this.closed = true; }
  };
}

test("enqueue: 无缝排程 + 全部 onended 后触发 onDrained", () => {
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
  // 逐个结束 → 全部归零才 drain
  sources[0].onended();
  assert.strictEqual(drained, 0);
  sources[1].onended();
  assert.strictEqual(drained, 1);
  assert.strictEqual(player.isActive(), false);
});

test("stop: 幂等、复位、关 ctx，迟到 onended 不再触发 onDrained", () => {
  const sources = [];
  let drained = 0;
  const player = createPcmPlayer({
    sampleRate: 16000,
    AudioCtxImpl: makeFakeCtx(sources),
    onDrained: () => { drained++; },
  });
  player.enqueue(int16Buffer(new Array(1600).fill(0)));
  assert.doesNotThrow(() => { player.stop(); player.stop(); });
  // stop 后迟到的 onended 被 stopped 守卫拦截
  sources[0].onended();
  assert.strictEqual(drained, 0);
  assert.strictEqual(player.isActive(), false);
});

test("enqueue: 空 buffer 忽略，不建 source", () => {
  const sources = [];
  const player = createPcmPlayer({ sampleRate: 16000, AudioCtxImpl: makeFakeCtx(sources) });
  player.enqueue(new ArrayBuffer(0));
  player.enqueue(null);
  assert.strictEqual(sources.length, 0);
});
