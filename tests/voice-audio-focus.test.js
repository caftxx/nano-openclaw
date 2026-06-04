"use strict";

const test = require("node:test");
const assert = require("node:assert");
const createVoiceAudioFocusGuard = require("../nano_openclaw/gateway/webui/static/voice-audio-focus.js");

class FakeAudio {
  constructor() {
    this.loop = false;
    this.preload = "";
    this.playsInline = false;
    this.muted = true;
    this.volume = 1;
    this.src = "";
    this.currentTime = 10;
    this.playCalls = 0;
    this.pauseCalls = 0;
  }
  play() { this.playCalls++; return Promise.resolve(); }
  pause() { this.pauseCalls++; }
}

function fakeUrl() {
  return {
    created: [],
    revoked: [],
    createObjectURL(blob) { this.created.push(blob); return `blob:test-${this.created.length}`; },
    revokeObjectURL(url) { this.revoked.push(url); },
  };
}

test("start: 创建循环无声 audio，并调用 play 持有媒体焦点", () => {
  const url = fakeUrl();
  const guard = createVoiceAudioFocusGuard({ AudioImpl: FakeAudio, URLImpl: url, BlobImpl: Blob, volume: 0.002 });

  guard.start();
  const audio = guard.getAudio();

  assert.strictEqual(guard.isActive(), true);
  assert.strictEqual(audio.loop, true);
  assert.strictEqual(audio.preload, "auto");
  assert.strictEqual(audio.playsInline, true);
  assert.strictEqual(audio.muted, false);
  assert.strictEqual(audio.volume, 0.002);
  assert.strictEqual(audio.src, "blob:test-1");
  assert.strictEqual(audio.playCalls, 1);
  assert.strictEqual(url.created.length, 1);
});

test("stop: 暂停但保留 audio，便于下一次用户手势复用", () => {
  const guard = createVoiceAudioFocusGuard({ AudioImpl: FakeAudio, URLImpl: fakeUrl(), BlobImpl: Blob });

  guard.start();
  const audio = guard.getAudio();
  guard.stop();

  assert.strictEqual(guard.isActive(), false);
  assert.strictEqual(audio.pauseCalls, 1);
  assert.strictEqual(audio.currentTime, 0);
  assert.strictEqual(guard.getAudio(), audio);
});

test("dispose: 停止并释放 object URL", () => {
  const url = fakeUrl();
  const guard = createVoiceAudioFocusGuard({ AudioImpl: FakeAudio, URLImpl: url, BlobImpl: Blob });

  guard.start();
  guard.dispose();

  assert.strictEqual(guard.isActive(), false);
  assert.strictEqual(guard.getAudio(), null);
  assert.deepStrictEqual(url.revoked, ["blob:test-1"]);
});

test("makeSilentWavBlob: 生成可播放的近静音 PCM WAV 头", async () => {
  const blob = createVoiceAudioFocusGuard.makeSilentWavBlob(Blob, 0.01, 8000);
  const buf = await blob.arrayBuffer();
  const bytes = new Uint8Array(buf);
  const text = (from, to) => String.fromCharCode(...bytes.slice(from, to));

  assert.strictEqual(blob.type, "audio/wav");
  assert.strictEqual(text(0, 4), "RIFF");
  assert.strictEqual(text(8, 12), "WAVE");
  assert.strictEqual(text(36, 40), "data");
  assert.strictEqual(bytes[44], 128);
  assert.strictEqual(bytes[45], 127);
});

test("makeSilentWavBlob: 默认时长跨过车机内容级媒体焦点门槛", async () => {
  const blob = createVoiceAudioFocusGuard.makeSilentWavBlob(Blob);
  const buf = await blob.arrayBuffer();
  const view = new DataView(buf);

  assert.strictEqual(buf.byteLength, 44 + 6 * 8000);
  assert.strictEqual(view.getUint32(40, true), 6 * 8000);
});
