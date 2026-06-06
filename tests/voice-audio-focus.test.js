/* 音频焦点 guard 回归：
 * 【C1】4s 近静音保持音持瞬态焦点（<5s 防车机面板出进度条曲目；近静音非纯静音；volume 0.001 非 0）
 * 【C3】刻意没有持麦档（AEC 采集的假"通话"周期会劫持音量键/路由并触发车机唤醒已暂停音乐）
 * prime 手势解锁 autoplay / released 还焦点 / refresh 重申 / dispose 释放
 */
const test = require("node:test");
const assert = require("node:assert");
const createVoiceAudioFocusGuard = require("../nano_openclaw/gateway/webui/static/voice-audio-focus.js");
const { makeSilentWavBlob } = createVoiceAudioFocusGuard;

// ── Fakes ───────────────────────────────────────────────────────────────────
function FakeBlob(parts, opts) { this.parts = parts; this.type = opts && opts.type; }
const FakeURL = {
  created: 0, revoked: 0,
  createObjectURL() { this.created++; return "blob:fake"; },
  revokeObjectURL() { this.revoked++; },
};

function makeFakeAudio(events) {
  function FakeAudio() {
    this.loop = false; this.src = ""; this.volume = 1; this.paused = true;
    this.play = () => { this.paused = false; events.push("audio-play"); return Promise.resolve(); };
    this.pause = () => { this.paused = true; events.push("audio-pause"); };
    FakeAudio.last = this;
  }
  return FakeAudio;
}

function makeGuard(events, extra) {
  const audioSession = { type: "auto" };
  const guard = createVoiceAudioFocusGuard(Object.assign({
    AudioImpl: makeFakeAudio(events),
    URLImpl: FakeURL,
    BlobImpl: FakeBlob,
    audioSession,
    log: () => {},
  }, extra || {}));
  return { guard, audioSession };
}

const tick = () => new Promise((r) => setImmediate(r));

test("C1：保持音 WAV 默认 4s——压在 Chromium 5s 内容级门槛下（车机面板不出进度条曲目），近静音非纯静音", () => {
  const captured = [];
  function CapBlob(parts, opts) { captured.push(parts[0]); FakeBlob.call(this, parts, opts); }
  makeSilentWavBlob(CapBlob);
  const buf = captured[0];
  assert.strictEqual(buf.byteLength, 44 + 4 * 8000, "44 字节头 + 4s×8kHz 数据（必须 <5s）");
  const view = new DataView(buf);
  assert.notStrictEqual(view.getUint8(44), view.getUint8(45), "样本应交替（近静音），防被平台优化掉");
});

test("silent-audio：循环播放保持音 + volume 0.001 + audioSession 声明 play-and-record", async () => {
  const events = [];
  const { guard, audioSession } = makeGuard(events);
  guard.setMode("silent-audio");
  await tick();
  assert.ok(events.includes("audio-play"));
  assert.strictEqual(audioSession.type, "play-and-record");
  const a = guard.getAudio();
  assert.strictEqual(a.loop, true);
  assert.ok(Math.abs(a.volume - 0.001) < 1e-9, "volume 0.001 而非 0（防被当无声流优化掉）");
});

test("C3：guard 不提供持麦档——接口面上没有任何 getUserMedia 依赖", () => {
  const events = [];
  const { guard } = makeGuard(events);
  assert.strictEqual(guard.getMicStream, undefined, "占位麦策略已删（假通话周期会唤醒已暂停的音乐）");
  guard.setMode("silent-audio");
  assert.strictEqual(guard.getMode(), "silent-audio");
});

test("released：停保持音、audioSession 还原 auto（外部音乐可恢复仲裁）", async () => {
  const events = [];
  const { guard, audioSession } = makeGuard(events);
  guard.setMode("silent-audio");
  await tick();
  guard.setMode("released");
  assert.ok(events.includes("audio-pause"));
  assert.strictEqual(audioSession.type, "auto");
});

test("prime：手势内 play 一次解锁 autoplay；当前模式非 silent-audio 则解锁后立即暂停", async () => {
  const events = [];
  const { guard } = makeGuard(events);
  guard.prime();                       // mode 仍是 released
  await tick();
  assert.ok(events.includes("audio-play"), "手势内应 play 一次完成解锁");
  assert.ok(events.includes("audio-pause"), "未占焦点时解锁完立即暂停，不残留播放");
});

test("refresh：重申当前模式（回前台后保持音可能被系统打断）", async () => {
  const events = [];
  const { guard } = makeGuard(events);
  guard.setMode("silent-audio");
  await tick();
  events.length = 0;
  guard.refresh();
  assert.ok(events.includes("audio-play"), "refresh 应重新 play 当前模式的保持音");
});

test("dispose：释放 objectURL，audio 元素丢弃，primed 复位", async () => {
  const events = [];
  const before = FakeURL.revoked;
  const { guard } = makeGuard(events);
  guard.setMode("silent-audio");
  guard.dispose();
  assert.strictEqual(FakeURL.revoked, before + 1);
  assert.strictEqual(guard.getAudio(), null);
});
