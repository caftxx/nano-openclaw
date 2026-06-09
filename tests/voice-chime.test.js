/* 唤醒提示音：Web Audio 合成（双音、短、可闻）/ prime 手势内 resume 解锁 /
 * play 非手势调度 / dispose 关闭 ctx。注入 FakeAudioContext，纯本地可测。 */
const test = require("node:test");
const assert = require("node:assert");
const createVoiceChime = require("../nano_openclaw/gateway/webui/static/voice-chime.js");

function makeFakeCtx(events) {
  function FakeCtx() {
    this.state = "suspended";
    this.currentTime = 0;
    this.destination = { name: "dest" };
    this.resume = () => { this.state = "running"; events.push(["resume"]); return Promise.resolve(); };
    this.close = () => { this.state = "closed"; events.push(["close"]); return Promise.resolve(); };
    this.createOscillator = () => ({
      frequency: { setValueAtTime: (v, t) => events.push(["osc-freq", v, t]) },
      connect: () => {},
      start: (t) => events.push(["osc-start", t]),
      stop: (t) => events.push(["osc-stop", t]),
    });
    this.createGain = () => ({
      gain: {
        setValueAtTime: (v, t) => events.push(["gain-set", v, t]),
        exponentialRampToValueAtTime: (v, t) => events.push(["gain-ramp", v, t]),
      },
      connect: () => {},
    });
    FakeCtx.last = this;
  }
  return FakeCtx;
}

test("play：双音 880→1320Hz、总时长 <0.5s（瞬态焦点）、包络可闻", () => {
  const events = [];
  const chime = createVoiceChime({ AudioContextImpl: makeFakeCtx(events), volume: 0.5, log() {} });
  chime.play();
  const freqs = events.filter((e) => e[0] === "osc-freq").map((e) => e[1]);
  assert.deepStrictEqual(freqs, [880, 1320], "双音 880→1320");
  const starts = events.filter((e) => e[0] === "osc-start").map((e) => e[1]);
  const stops = events.filter((e) => e[0] === "osc-stop").map((e) => e[1]);
  assert.strictEqual(starts.length, 2, "两段 oscillator");
  assert.ok(Math.max(...stops) - Math.min(...starts) < 0.5, "总时长远短于 5s（无媒体会话）");
  const peaks = events.filter((e) => e[0] === "gain-set").map((e) => e[1]);
  assert.ok(peaks.length === 2 && peaks.every((p) => p > 0), "包络峰值可闻（>0）");
});

test("play('sleep')：回落待机降调 1320→880（与唤醒升调反向，区分醒/睡）", () => {
  const events = [];
  const chime = createVoiceChime({ AudioContextImpl: makeFakeCtx(events), volume: 0.5, log() {} });
  chime.play("sleep");
  const freqs = events.filter((e) => e[0] === "osc-freq").map((e) => e[1]);
  assert.deepStrictEqual(freqs, [1320, 880], "降调 1320→880");
});

test("prime：手势内创建并 resume 解锁 autoplay；幂等不重复 resume", () => {
  const events = [];
  const chime = createVoiceChime({ AudioContextImpl: makeFakeCtx(events), log() {} });
  chime.prime();
  assert.strictEqual(events.filter((e) => e[0] === "resume").length, 1, "prime 必须 resume 解锁");
  chime.prime(); // 已解锁：no-op
  assert.strictEqual(events.filter((e) => e[0] === "resume").length, 1, "prime 幂等");
});

test("play 不依赖 <audio> 解码：无 ctx 实现时静默不抛", () => {
  const chime = createVoiceChime({ AudioContextImpl: null, log() {} });
  assert.doesNotThrow(() => { chime.prime(); chime.play(); });
});

test("dispose：close AudioContext 并复位", () => {
  const events = [];
  const chime = createVoiceChime({ AudioContextImpl: makeFakeCtx(events), log() {} });
  chime.prime();
  assert.ok(chime.getContext(), "prime 后持有 ctx");
  chime.dispose();
  assert.ok(events.some((e) => e[0] === "close"), "dispose 必须 close ctx");
  assert.strictEqual(chime.getContext(), null, "复位");
});
