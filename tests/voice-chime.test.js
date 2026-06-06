/* 唤醒提示音：WAV 生成（短、可闻、双音）/ prime 手势解锁 / play 非手势播放 / dispose */
const test = require("node:test");
const assert = require("node:assert");
const createVoiceChime = require("../nano_openclaw/gateway/webui/static/voice-chime.js");
const { makeChimeWavBlob } = createVoiceChime;

function FakeBlob(parts, opts) { this.parts = parts; this.type = opts && opts.type; }
const FakeURL = {
  revoked: 0,
  createObjectURL() { return "blob:chime"; },
  revokeObjectURL() { this.revoked++; },
};
function makeFakeAudio(events) {
  function FakeAudio() {
    this.src = ""; this.volume = 1; this.currentTime = 0;
    this.play = () => { events.push(["play", this.volume]); return Promise.resolve(); };
    this.pause = () => { events.push(["pause"]); };
    FakeAudio.last = this;
  }
  return FakeAudio;
}
const tick = () => new Promise((r) => setImmediate(r));

test("WAV：~0.22s 双音（远短于 5s 媒体会话门槛），峰值可闻（远超近静音 ±1）", () => {
  const captured = [];
  function CapBlob(parts, opts) { captured.push(parts[0]); FakeBlob.call(this, parts, opts); }
  makeChimeWavBlob(CapBlob);
  const buf = captured[0];
  const samples = buf.byteLength - 44;
  assert.ok(samples / 8000 < 0.5, "必须远短于 5s（瞬态焦点、无媒体会话）");
  const view = new DataView(buf);
  let maxDev = 0;
  for (let i = 44; i < buf.byteLength; i++) maxDev = Math.max(maxDev, Math.abs(view.getUint8(i) - 128));
  assert.ok(maxDev >= 60, `提示音必须可闻（峰值 ±${maxDev}）`);
});

test("prime：手势内静音 play 一次解锁后立即暂停（听不到）；play：恢复音量从头播", async () => {
  const events = [];
  const chime = createVoiceChime({
    AudioImpl: makeFakeAudio(events), URLImpl: FakeURL, BlobImpl: FakeBlob, log: () => {},
  });
  chime.prime();
  await tick();
  assert.deepStrictEqual(events[0], ["play", 0], "prime 必须静音（volume 0）");
  assert.deepStrictEqual(events[1], ["pause"], "解锁完立即暂停");
  events.length = 0;
  chime.play();
  assert.strictEqual(events[0][0], "play");
  assert.ok(events[0][1] > 0, "正式播放恢复音量");
  assert.strictEqual(chime.getAudio().currentTime, 0, "从头播");
});

test("prime 幂等；dispose 释放 objectURL 并复位", async () => {
  const events = [];
  const before = FakeURL.revoked;
  const chime = createVoiceChime({
    AudioImpl: makeFakeAudio(events), URLImpl: FakeURL, BlobImpl: FakeBlob, log: () => {},
  });
  chime.prime();
  await tick();
  chime.prime();   // 已解锁：no-op
  await tick();
  assert.strictEqual(events.filter((e) => e[0] === "play").length, 1);
  chime.dispose();
  assert.strictEqual(FakeURL.revoked, before + 1);
  assert.strictEqual(chime.getAudio(), null);
});
