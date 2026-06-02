/* 阿里云流式语音合成引擎单测：事件 JSON → started/completed/failed 映射、
 * StartSynthesis/RunSynthesis/StopSynthesis 指令帧构造、32hex id，以及用 FakeWS
 * 覆盖工厂的抗重入 / socket 隔离 / pending 攒帧 / 二进制帧投递（与 voice-asr-aliyun.test.js 同思路）。 */
const test = require("node:test");
const assert = require("node:assert");
const createAliyunSynthesizer = require("../nano_openclaw/gateway/webui/static/voice-tts-aliyun.js");

const { parseTtsEvent, makeId, buildStartSynthesis, buildRunSynthesis, buildStopSynthesis } = createAliyunSynthesizer;

function evt(name, extraHeader) {
  return { header: Object.assign({ name }, extraHeader || {}) };
}

// 仿浏览器 WebSocket：记录 send，暴露 emitOpen/emitMessage 手动驱动回调。
function makeFakeWSClass(created) {
  return class FakeWS {
    constructor(url) {
      this.url = url;
      this.readyState = 1;   // 直接置 OPEN，测试用 emitOpen 显式触发 onopen
      this.sent = [];
      this.closed = false;
      this.binaryType = "";
      this.onopen = null;
      this.onmessage = null;
      this.onerror = null;
      this.onclose = null;
      created.push(this);
    }
    send(data) { this.sent.push(data); }
    close() { this.closed = true; this.readyState = 3; }
    emitOpen() { if (this.onopen) this.onopen(); }
    emitMessage(data) { if (this.onmessage) this.onmessage({ data }); }
  };
}

function sentNames(sock) {
  return sock.sent
    .filter((s) => typeof s === "string")
    .map((s) => { try { return JSON.parse(s).header.name; } catch (_) { return null; } });
}

// ── 纯解析 ────────────────────────────────────────────────────────────────
test("parseTtsEvent: SynthesisStarted → started", () => {
  assert.deepStrictEqual(parseTtsEvent(evt("SynthesisStarted")), { kind: "started", text: "" });
});

test("parseTtsEvent: SynthesisCompleted → completed", () => {
  assert.deepStrictEqual(parseTtsEvent(evt("SynthesisCompleted")), { kind: "completed", text: "" });
});

test("parseTtsEvent: TaskFailed → failed(读 status_message)", () => {
  // 阿里云合成事件失败原因在 header.status_message，优先取它。
  assert.deepStrictEqual(
    parseTtsEvent(evt("TaskFailed", { status_message: "Meta: UNAUTHENTICATED ..." })),
    { kind: "failed", text: "Meta: UNAUTHENTICATED ..." }
  );
});

test("parseTtsEvent: TaskFailed 无 status_message 时回退 status_text(向后兼容)", () => {
  assert.deepStrictEqual(
    parseTtsEvent(evt("TaskFailed", { status_text: "auth failed" })),
    { kind: "failed", text: "auth failed" }
  );
});

test("parseTtsEvent: 句级时间戳 / 未知 name → other", () => {
  assert.strictEqual(parseTtsEvent(evt("SentenceBegin")).kind, "other");
  assert.strictEqual(parseTtsEvent(evt("SentenceEnd")).kind, "other");
  assert.strictEqual(parseTtsEvent(evt("WhatIsThis")).kind, "other");
});

test("parseTtsEvent: 缺字段/空对象不抛、归为 other", () => {
  assert.strictEqual(parseTtsEvent({}).kind, "other");
  assert.strictEqual(parseTtsEvent(undefined).kind, "other");
});

// ── 指令帧构造 ──────────────────────────────────────────────────────────────
test("buildStartSynthesis: header/payload 符合 FlowingSpeechSynthesizer 协议", () => {
  const cmd = buildStartSynthesis("APPKEY", "task-id-32", { voice: "xiaoyun", sampleRate: 16000 }, () => "msg-id-32");
  assert.strictEqual(cmd.header.appkey, "APPKEY");
  assert.strictEqual(cmd.header.task_id, "task-id-32");
  assert.strictEqual(cmd.header.message_id, "msg-id-32");
  assert.strictEqual(cmd.header.namespace, "FlowingSpeechSynthesizer");
  assert.strictEqual(cmd.header.name, "StartSynthesis");
  assert.strictEqual(cmd.payload.voice, "xiaoyun");
  assert.strictEqual(cmd.payload.format, "pcm");
  assert.strictEqual(cmd.payload.sample_rate, 16000);
  assert.strictEqual(cmd.payload.volume, 50);
  assert.strictEqual(cmd.payload.speech_rate, 0);
  assert.strictEqual(cmd.payload.pitch_rate, 0);
});

test("buildRunSynthesis: name=RunSynthesis，payload 带 text", () => {
  const cmd = buildRunSynthesis("APPKEY", "task-id-32", "你好世界", () => "msg-2");
  assert.strictEqual(cmd.header.name, "RunSynthesis");
  assert.strictEqual(cmd.header.namespace, "FlowingSpeechSynthesizer");
  assert.strictEqual(cmd.header.task_id, "task-id-32");
  assert.strictEqual(cmd.header.message_id, "msg-2");
  assert.strictEqual(cmd.payload.text, "你好世界");
});

test("buildStopSynthesis: name=StopSynthesis，无 payload，沿用同一 task_id", () => {
  const cmd = buildStopSynthesis("APPKEY", "task-id-32", () => "msg-3");
  assert.strictEqual(cmd.header.name, "StopSynthesis");
  assert.strictEqual(cmd.header.task_id, "task-id-32");
  assert.strictEqual(cmd.header.message_id, "msg-3");
  assert.strictEqual(cmd.payload, undefined);
});

test("makeId: 32 个 hex 字符", () => {
  const id = makeId(() => 10);
  assert.strictEqual(id.length, 32);
  assert.ok(/^[0-9a-f]{32}$/.test(id));
  assert.strictEqual(id, "a".repeat(32));
});

// ── 工厂行为（FakeWS 驱动）─────────────────────────────────────────────────
function makeSynth(created, overrides) {
  const FakeWS = makeFakeWSClass(created);
  return createAliyunSynthesizer(Object.assign({
    getConfig: () => ({ appkey: "APPKEY", endpoint: "wss://gw/ws/v1", voice: "xiaoyun", sampleRate: 16000 }),
    getToken: async () => ({ token: "TOK" }),
    WebSocketImpl: FakeWS,
  }, overrides || {}));
}

test("抗重入：连续两次 begin() 只建一条 ws", async () => {
  const created = [];
  const synth = makeSynth(created);
  synth.begin();
  synth.begin();
  // begin 是 async（await getToken）；等微任务排空
  await new Promise((r) => setTimeout(r, 0));
  assert.strictEqual(created.length, 1);
});

test("socket 隔离：被 abort 取代的旧 socket 迟到 onopen 不 send、不抛", async () => {
  const created = [];
  const synth = makeSynth(created);
  synth.begin();
  await new Promise((r) => setTimeout(r, 0));
  const sock = created[0];
  synth.abort();   // 摘掉 ws 引用
  // 迟到的 onopen：sock !== ws 守卫应直接 return，不发 StartSynthesis
  assert.doesNotThrow(() => sock.emitOpen());
  assert.strictEqual(sentNames(sock).length, 0);
});

test("push 早于 SynthesisStarted：先入队，收到 SynthesisStarted 后才 flush RunSynthesis", async () => {
  const created = [];
  const synth = makeSynth(created);
  synth.begin();
  await new Promise((r) => setTimeout(r, 0));
  const sock = created[0];
  sock.emitOpen();   // 发 StartSynthesis
  synth.push("第一段");   // 还没 started → 入队，不应直接发 RunSynthesis
  assert.deepStrictEqual(sentNames(sock), ["StartSynthesis"]);
  sock.emitMessage(JSON.stringify(evt("SynthesisStarted")));   // → flush
  assert.deepStrictEqual(sentNames(sock), ["StartSynthesis", "RunSynthesis"]);
  // started 后再 push 直接发
  synth.push("第二段");
  assert.deepStrictEqual(sentNames(sock), ["StartSynthesis", "RunSynthesis", "RunSynthesis"]);
});

test("end() 早于 started：started 后补发 StopSynthesis", async () => {
  const created = [];
  const synth = makeSynth(created);
  synth.begin();
  await new Promise((r) => setTimeout(r, 0));
  const sock = created[0];
  sock.emitOpen();
  synth.push("一段话");
  synth.end();   // 还没 started → 置 endRequested
  assert.deepStrictEqual(sentNames(sock), ["StartSynthesis"]);
  sock.emitMessage(JSON.stringify(evt("SynthesisStarted")));   // flush + 补发 Stop
  assert.deepStrictEqual(sentNames(sock), ["StartSynthesis", "RunSynthesis", "StopSynthesis"]);
});

test("二进制帧 → onAudio 被调用", async () => {
  const created = [];
  const audios = [];
  const synth = makeSynth(created, { onAudio: (buf) => audios.push(buf) });
  synth.begin();
  await new Promise((r) => setTimeout(r, 0));
  const sock = created[0];
  sock.emitOpen();
  sock.emitMessage(JSON.stringify(evt("SynthesisStarted")));
  const ab = new ArrayBuffer(8);
  sock.emitMessage(ab);   // 非字符串 → onAudio
  assert.strictEqual(audios.length, 1);
  assert.strictEqual(audios[0], ab);
});

test("SynthesisCompleted → onComplete 调用并关 ws", async () => {
  const created = [];
  let completed = 0;
  const synth = makeSynth(created, { onComplete: () => { completed++; } });
  synth.begin();
  await new Promise((r) => setTimeout(r, 0));
  const sock = created[0];
  sock.emitOpen();
  sock.emitMessage(JSON.stringify(evt("SynthesisStarted")));
  sock.emitMessage(JSON.stringify(evt("SynthesisCompleted")));
  assert.strictEqual(completed, 1);
  assert.strictEqual(sock.closed, true);
});

test("TaskFailed → onError(aliyun-task-failed) 并关 ws", async () => {
  const created = [];
  const errors = [];
  const synth = makeSynth(created, { onError: (n, m) => errors.push([n, m]) });
  synth.begin();
  await new Promise((r) => setTimeout(r, 0));
  const sock = created[0];
  sock.emitOpen();
  sock.emitMessage(JSON.stringify(evt("TaskFailed", { status_message: "boom" })));
  assert.strictEqual(errors.length, 1);
  assert.strictEqual(errors[0][0], "aliyun-task-failed");
  assert.strictEqual(errors[0][1], "boom");
  assert.strictEqual(sock.closed, true);
});

test("push 在未 begin 时自动 begin（建一条 ws）", async () => {
  const created = [];
  const synth = makeSynth(created);
  synth.push("自动开播");
  await new Promise((r) => setTimeout(r, 0));
  assert.strictEqual(created.length, 1);
});

test("abort 幂等：重复调用不抛", async () => {
  const created = [];
  const synth = makeSynth(created);
  synth.begin();
  await new Promise((r) => setTimeout(r, 0));
  assert.doesNotThrow(() => { synth.abort(); synth.abort(); });
});
