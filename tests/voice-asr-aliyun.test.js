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

// ── 重入 / socket 隔离回归（前端竞态导致阿里云"完全用不了"的根因）─────────────
// 假 WebSocket：记录每个实例与其 send 调用，可手动触发 onopen（不连真实网络/音频）。
class FakeWS {
  constructor(url) {
    FakeWS.instances.push(this);
    this.url = url;
    this.readyState = 0;            // CONNECTING
    this.sent = [];
    this.closed = false;
    this.onopen = null; this.onmessage = null; this.onerror = null; this.onclose = null;
  }
  send(data) {
    if (this.readyState !== 1) throw new Error("InvalidStateError");
    this.sent.push(data);
  }
  close() { this.closed = true; this.readyState = 3; }
  open() { this.readyState = 1; if (this.onopen) this.onopen(); }   // 测试手动触发握手完成
}

function makeRecognizer(extra) {
  FakeWS.instances = [];
  const opts = Object.assign({
    getConfig: () => ({ appkey: "APPKEY", endpoint: "wss://nls.example/ws" }),
    getToken: async () => ({ token: "TOK" }),
    setupAudio: async () => {},     // 跳过真实音频建立
    WebSocketImpl: FakeWS,
  }, extra || {});
  return createAliyunRecognizer(opts);
}

// start() 是 async（await getToken + setupAudio），用 setImmediate 把所有挂起的 microtask 推进完。
const flush = () => new Promise((r) => setImmediate(r));

test("重入守卫：同一实例连续两次 start() 只建一条 WebSocket", async () => {
  const rec = makeRecognizer();
  const p1 = rec.start();
  const p2 = rec.start();          // 第二次在第一次 await 窗口内进来：应被守卫忽略
  await Promise.all([p1, p2]);
  await flush();
  assert.strictEqual(FakeWS.instances.length, 1, "并发 start 只应创建一条 ws");
});

test("socket 隔离：被取代的旧 socket 触发 onopen 也不 send、不抛、不污染当前连接", async () => {
  // 用一个慢 getToken 卡住第一次 start：拿到旧 ws 引用后再放行，制造"旧 socket 还在 CONNECTING"。
  let release;
  const gate = new Promise((r) => { release = r; });
  let firstCall = true;
  const rec = makeRecognizer({
    getToken: async () => { if (firstCall) { firstCall = false; await gate; } return { token: "TOK" }; },
  });

  const p1 = rec.start();          // 卡在 getToken
  await flush();
  assert.strictEqual(FakeWS.instances.length, 0, "getToken 未返回前不应建 ws");

  release();                       // 放行第一次 start → 建第一条 ws
  await p1; await flush();
  assert.strictEqual(FakeWS.instances.length, 1);
  const sock1 = FakeWS.instances[0];

  // 手动把 sock1 从「当前连接」位置挤掉：模拟它被新连接取代（abort/重建后又来旧 onopen）。
  // 直接对它 close 让 finish 把内部 ws 置空 —— 之后 sock1 已非当前 ws。
  rec.abort();
  // sock1 仍可能收到迟到的 onopen（CONNECTING→OPEN 的握手在 abort 之后才完成）。
  sock1.readyState = 1;
  assert.doesNotThrow(() => { if (sock1.onopen) sock1.onopen(); }, "旧 socket onopen 不应抛");
  assert.strictEqual(sock1.sent.length, 0, "已被取代的旧 socket 不应 send StartTranscription");
});

test("abort 取消 await 中的 in-flight start：不建 ws、不开麦", async () => {
  // 用 gated getToken 把 start() 卡在 await；在放行前 abort()，放行后断言全程没 new ws、没开麦。
  let release;
  const gate = new Promise((r) => { release = r; });
  let setupCalled = false;
  const rec = makeRecognizer({
    getToken: async () => { await gate; return { token: "TOK" }; },
    setupAudio: async () => { setupCalled = true; },
  });

  const p = rec.start();           // 卡在 getToken
  await flush();
  assert.strictEqual(FakeWS.instances.length, 0, "getToken 未返回前不应建 ws");

  rec.abort();                     // 中止：in-flight start 应被 generation 令牌作废
  release();                       // 放行 getToken，让 start() 续跑到 return
  await p; await flush();
  assert.strictEqual(FakeWS.instances.length, 0, "abort 后 in-flight start 不应再建 ws");
  assert.strictEqual(setupCalled, false, "abort 后 in-flight start 不应开麦");
});
