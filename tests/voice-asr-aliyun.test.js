/* 阿里云实时识别引擎的纯逻辑单测：事件 JSON → interim/final/started/completed 映射，
 * 以及 32hex id 生成。浏览器侧的 ws/getUserMedia/AudioWorklet 无法在 node 里测，
 * 故只覆盖可纯函数化的协议解析与 id 生成（与 voice-utterance.test.js 同思路）。 */
const test = require("node:test");
const assert = require("node:assert");
const createAliyunRecognizer = require("../nano_openclaw/gateway/webui/static/voice-asr-aliyun.js");

const { parseAliyunEvent, makeId, buildStartCommand, buildStopCommand } = createAliyunRecognizer;

function evt(name, payload, extraHeader) {
  return { header: Object.assign({ name }, extraHeader || {}), payload: payload || {} };
}

test("parseAliyunEvent: TranscriptionStarted → started", () => {
  assert.deepStrictEqual(parseAliyunEvent(evt("TranscriptionStarted")), { kind: "started", text: "" });
});

test("parseAliyunEvent: TranscriptionResultChanged → interim(取 payload.result)", () => {
  assert.deepStrictEqual(
    parseAliyunEvent(evt("TranscriptionResultChanged", { result: "今天天气" })),
    { kind: "interim", text: "今天天气" }
  );
});

test("parseAliyunEvent: SentenceEnd → final(取 payload.result)", () => {
  assert.deepStrictEqual(
    parseAliyunEvent(evt("SentenceEnd", { result: "今天天气不错。" })),
    { kind: "final", text: "今天天气不错。" }
  );
});

test("parseAliyunEvent: TranscriptionCompleted → completed", () => {
  assert.deepStrictEqual(parseAliyunEvent(evt("TranscriptionCompleted")), { kind: "completed", text: "" });
});

test("parseAliyunEvent: TaskFailed → failed(带 status_text)", () => {
  assert.deepStrictEqual(
    parseAliyunEvent(evt("TaskFailed", {}, { status_text: "auth failed" })),
    { kind: "failed", text: "auth failed" }
  );
});

test("parseAliyunEvent: SentenceBegin / 未知 name → other", () => {
  assert.strictEqual(parseAliyunEvent(evt("SentenceBegin")).kind, "other");
  assert.strictEqual(parseAliyunEvent(evt("WhatIsThis")).kind, "other");
});

test("parseAliyunEvent: 缺字段/空对象不抛、归为 other 或空文本", () => {
  assert.strictEqual(parseAliyunEvent({}).kind, "other");
  assert.deepStrictEqual(parseAliyunEvent(evt("TranscriptionResultChanged")), { kind: "interim", text: "" });
});

test("makeId: 32 个 hex 字符", () => {
  // 注入确定随机源验证长度 + 字符集（避免依赖 crypto/Math.random）
  const id = makeId(() => 10);   // 10 & 15 = 10 → 'a'
  assert.strictEqual(id.length, 32);
  assert.ok(/^[0-9a-f]{32}$/.test(id));
  assert.strictEqual(id, "a".repeat(32));
});

test("makeId: 默认随机源也产出 32 hex（多次基本不重复）", () => {
  const a = makeId();
  const b = makeId();
  assert.ok(/^[0-9a-f]{32}$/.test(a));
  assert.ok(/^[0-9a-f]{32}$/.test(b));
  assert.notStrictEqual(a, b);
});

test("buildStartCommand: header/payload 符合 SpeechTranscriber 协议", () => {
  const cmd = buildStartCommand("APPKEY", "task-id-32", () => "msg-id-32");
  assert.strictEqual(cmd.header.appkey, "APPKEY");
  assert.strictEqual(cmd.header.task_id, "task-id-32");
  assert.strictEqual(cmd.header.message_id, "msg-id-32");
  assert.strictEqual(cmd.header.namespace, "SpeechTranscriber");
  assert.strictEqual(cmd.header.name, "StartTranscription");
  assert.strictEqual(cmd.payload.format, "pcm");
  assert.strictEqual(cmd.payload.sample_rate, 16000);
  assert.strictEqual(cmd.payload.enable_intermediate_result, true);
});

test("buildStopCommand: name=StopTranscription，沿用同一 task_id", () => {
  const cmd = buildStopCommand("APPKEY", "task-id-32", () => "msg-2");
  assert.strictEqual(cmd.header.name, "StopTranscription");
  assert.strictEqual(cmd.header.task_id, "task-id-32");
  assert.strictEqual(cmd.header.message_id, "msg-2");
});
