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

class FakeTrack {
  constructor() { this.stopped = false; this.onended = null; }
  stop() { this.stopped = true; }
}

class FakeStream {
  constructor() { this.tracks = [new FakeTrack(), new FakeTrack()]; }
  getTracks() { return this.tracks; }
}

// behavior: "resolve" | "reject" | "throw"；记录每次的 constraints
function fakeMediaDevices(behavior) {
  return {
    calls: [],
    streams: [],
    getUserMedia(constraints) {
      this.calls.push(constraints);
      if (behavior === "throw") throw new Error("boom");
      if (behavior === "reject") return Promise.reject(new Error("denied"));
      const stream = new FakeStream();
      this.streams.push(stream);
      return Promise.resolve(stream);
    },
  };
}

function fakeUrl() {
  return {
    created: [],
    revoked: [],
    createObjectURL(blob) { this.created.push(blob); return `blob:test-${this.created.length}`; },
    revokeObjectURL(url) { this.revoked.push(url); },
  };
}

// 让 getUserMedia 的 then/catch 微任务跑完
const tick = () => new Promise((r) => setImmediate(r));

test("start: 创建循环无声 audio，并调用 play 持有媒体焦点", () => {
  const url = fakeUrl();
  const guard = createVoiceAudioFocusGuard({ AudioImpl: FakeAudio, URLImpl: url, BlobImpl: Blob, mediaDevices: null, volume: 0.002 });

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
  const guard = createVoiceAudioFocusGuard({ AudioImpl: FakeAudio, URLImpl: fakeUrl(), BlobImpl: Blob, mediaDevices: null });

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
  const guard = createVoiceAudioFocusGuard({ AudioImpl: FakeAudio, URLImpl: url, BlobImpl: Blob, mediaDevices: null });

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

test("micHold: 偏好持麦时申请与识别采集同形态的占位流，不创建 audio", async () => {
  const md = fakeMediaDevices("resolve");
  const guard = createVoiceAudioFocusGuard({
    AudioImpl: FakeAudio, URLImpl: fakeUrl(), BlobImpl: Blob,
    mediaDevices: md, preferMicHold: () => true,
  });

  guard.start();
  await tick();

  assert.strictEqual(md.calls.length, 1);
  // audio:true 与识别采集同形态（AEC 默认开）——实测这种采集才会让外部音乐暂停
  assert.deepStrictEqual(md.calls[0], { audio: true });
  assert.strictEqual(guard.getMicStream(), md.streams[0]);
  assert.strictEqual(guard.getAudio(), null);   // 持麦路径不走静音 audio

  guard.start();   // 已持有：不重复申请
  await tick();
  assert.strictEqual(md.calls.length, 1);
});

test("micHold: stop 归还麦克风轨道；等待期间 stop 则拿到后立即归还", async () => {
  const md = fakeMediaDevices("resolve");
  const guard = createVoiceAudioFocusGuard({
    AudioImpl: FakeAudio, URLImpl: fakeUrl(), BlobImpl: Blob,
    mediaDevices: md, preferMicHold: () => true,
  });

  guard.start();
  await tick();
  guard.stop();
  assert.strictEqual(guard.getMicStream(), null);
  assert.ok(md.streams[0].getTracks().every((t) => t.stopped));

  guard.start();           // 重新申请
  guard.stop();            // promise 还没 resolve 就 stop
  await tick();
  assert.strictEqual(guard.getMicStream(), null);
  assert.ok(md.streams[1].getTracks().every((t) => t.stopped));
});

test("micHold: 轨道被系统收回后，下次 start 重新申请", async () => {
  const md = fakeMediaDevices("resolve");
  const guard = createVoiceAudioFocusGuard({
    AudioImpl: FakeAudio, URLImpl: fakeUrl(), BlobImpl: Blob,
    mediaDevices: md, preferMicHold: () => true,
  });

  guard.start();
  await tick();
  md.streams[0].getTracks()[0].onended();   // 模拟系统侧收回
  assert.strictEqual(guard.getMicStream(), null);

  guard.start();
  await tick();
  assert.strictEqual(md.calls.length, 2);
  assert.strictEqual(guard.getMicStream(), md.streams[1]);
});

test("micHold: 申请失败退回静音 audio；getUserMedia 缺失直接走 audio", async () => {
  const md = fakeMediaDevices("reject");
  const guard = createVoiceAudioFocusGuard({
    AudioImpl: FakeAudio, URLImpl: fakeUrl(), BlobImpl: Blob,
    mediaDevices: md, preferMicHold: () => true,
  });
  guard.start();
  await tick();
  assert.strictEqual(guard.getMicStream(), null);
  assert.strictEqual(guard.getAudio().playCalls, 1);   // 回退到静音 audio

  const noMic = createVoiceAudioFocusGuard({
    AudioImpl: FakeAudio, URLImpl: fakeUrl(), BlobImpl: Blob,
    mediaDevices: null, preferMicHold: () => true,
  });
  noMic.start();
  assert.strictEqual(noMic.getAudio().playCalls, 1);
});

test("audioSession: 渐进增强——start 声明 play-and-record，stop 还原 auto", () => {
  const session = { type: "auto" };
  const guard = createVoiceAudioFocusGuard({
    AudioImpl: FakeAudio, URLImpl: fakeUrl(), BlobImpl: Blob,
    mediaDevices: null, audioSession: session,
  });

  guard.start();
  assert.strictEqual(session.type, "play-and-record");
  guard.stop();
  assert.strictEqual(session.type, "auto");
});

test("micHold: 偏好切回 audio（引擎切 webspeech）时归还占位麦", async () => {
  const md = fakeMediaDevices("resolve");
  let engine = "aliyun";
  const guard = createVoiceAudioFocusGuard({
    AudioImpl: FakeAudio, URLImpl: fakeUrl(), BlobImpl: Blob,
    mediaDevices: md, preferMicHold: () => engine === "aliyun",
  });

  guard.start();
  await tick();
  assert.strictEqual(guard.getMicStream(), md.streams[0]);

  engine = "webspeech";
  guard.start();
  assert.strictEqual(guard.getMicStream(), null);
  assert.ok(md.streams[0].getTracks().every((t) => t.stopped));
  assert.strictEqual(guard.getAudio().playCalls, 1);
});

test("换轨顺序: mic→audio 先起静音 audio 再归还占位麦（无焦点空窗）", async () => {
  const events = [];
  class OrderedAudio extends FakeAudio {
    play() { events.push("audio-play"); return super.play(); }
  }
  const md = fakeMediaDevices("resolve");
  let speaking = false;
  const guard = createVoiceAudioFocusGuard({
    AudioImpl: OrderedAudio, URLImpl: fakeUrl(), BlobImpl: Blob,
    mediaDevices: md, preferMicHold: () => !speaking,
  });

  guard.start();
  await tick();
  md.streams[0].getTracks().forEach((t) => {
    const orig = t.stop.bind(t);
    t.stop = () => { events.push("mic-stop"); orig(); };
  });

  speaking = true;   // 进入朗读：释放占位麦走媒体通路（防通信模式劫持路由）
  guard.start();
  assert.strictEqual(guard.getMicStream(), null);
  assert.strictEqual(events[0], "audio-play");   // 静音 audio 先占住媒体焦点
  assert.ok(events.slice(1).every((e) => e === "mic-stop"));
});

test("micHold: 等待期间偏好切走（进入朗读）→ 拿到麦立即归还且不打断静音 audio", async () => {
  const md = fakeMediaDevices("resolve");
  let speaking = false;
  const guard = createVoiceAudioFocusGuard({
    AudioImpl: FakeAudio, URLImpl: fakeUrl(), BlobImpl: Blob,
    mediaDevices: md, preferMicHold: () => !speaking,
  });

  guard.start();           // 占位麦申请中（promise 未 resolve）
  speaking = true;
  guard.start();           // 已切到 audio 策略
  await tick();            // 占位麦此刻才 resolve

  assert.strictEqual(guard.getMicStream(), null);
  assert.ok(md.streams[0].getTracks().every((t) => t.stopped));   // 立即归还
  assert.strictEqual(guard.getAudio().playCalls, 1);
  assert.strictEqual(guard.getAudio().pauseCalls, 0);   // 现行静音 audio 不被迟到的麦停掉
});

test("prime: 手势内解锁静音 audio——持麦策略下播一次立即暂停，且只解锁一次", async () => {
  const md = fakeMediaDevices("resolve");
  const guard = createVoiceAudioFocusGuard({
    AudioImpl: FakeAudio, URLImpl: fakeUrl(), BlobImpl: Blob,
    mediaDevices: md, preferMicHold: () => true,
  });

  guard.start();
  await tick();
  assert.strictEqual(guard.getMicStream(), md.streams[0]);

  guard.prime();
  const audio = guard.getAudio();
  assert.strictEqual(audio.playCalls, 1);
  await tick();
  assert.strictEqual(audio.pauseCalls, 1);   // 持麦策略下解锁完立即停，媒体面板不残留曲目

  guard.prime();   // 已解锁：no-op
  assert.strictEqual(audio.playCalls, 1);
});

test("prime: audio 策略下不打断正在播放的静音 audio", async () => {
  const guard = createVoiceAudioFocusGuard({
    AudioImpl: FakeAudio, URLImpl: fakeUrl(), BlobImpl: Blob,
    mediaDevices: null, preferMicHold: () => false,
  });

  guard.start();
  guard.prime();
  await tick();
  assert.strictEqual(guard.getAudio().pauseCalls, 0);   // 静音 audio 是现行策略，不能停
});

test("朗读周期: 聆听持麦 → 朗读放麦走媒体通路 → 回聆听重新持麦并停静音 audio", async () => {
  const md = fakeMediaDevices("resolve");
  let phase = "listening";
  const guard = createVoiceAudioFocusGuard({
    AudioImpl: FakeAudio, URLImpl: fakeUrl(), BlobImpl: Blob,
    mediaDevices: md, preferMicHold: () => phase !== "speaking",
  });

  guard.start();
  await tick();
  assert.strictEqual(guard.getMicStream(), md.streams[0]);

  phase = "speaking";
  guard.start();
  assert.strictEqual(guard.getMicStream(), null);       // 放麦：退出通信模式，TTS 走媒体通路
  assert.strictEqual(guard.getAudio().playCalls, 1);    // 静音 audio 顶住焦点

  phase = "listening";
  guard.start();
  await tick();
  assert.strictEqual(guard.getMicStream(), md.streams[1]);          // 重新持麦
  assert.strictEqual(guard.getAudio().pauseCalls, 1);               // 麦到手后静音 audio 停
});
