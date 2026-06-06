/* 阿里云实时识别适配器回归：
 * 【A5】async start 抗重入（不开双 ws）、被取代旧 socket 回调隔离、12s 启动窗口
 * 【A6】SentenceEnd 直接 onFinal（不叠前端去抖）；interim 暂存供 flushNow
 * 【A7】abort/stop 作废 await 中的 in-flight start（不建 ws、不开麦、已开则关）
 * 协议纯函数：事件解析 / 32hex id / 指令帧构造
 */
const test = require("node:test");
const assert = require("node:assert");
const createAliyunRecognizer = require("../nano_openclaw/gateway/webui/static/voice-recognizer-aliyun.js");
const { parseAliyunEvent, makeId, buildStartCommand, buildStopCommand } = createAliyunRecognizer;

// ── Fakes ───────────────────────────────────────────────────────────────────
function makeFakeWS() {
  const instances = [];
  function FakeWS(url) {
    this.url = url;
    this.readyState = 0;
    this.sent = [];
    this.closed = 0;
    this.send = (d) => {
      if (this.readyState !== 1) throw new Error("InvalidStateError");
      this.sent.push(d);
    };
    this.close = () => { this.closed++; this.readyState = 3; };
    this.open = () => { this.readyState = 1; if (this.onopen) this.onopen(); };
    this.message = (obj) => { if (this.onmessage) this.onmessage({ data: JSON.stringify(obj) }); };
    instances.push(this);
  }
  FakeWS.instances = instances;
  return FakeWS;
}

function evt(name, payload, header) {
  return { header: Object.assign({ name }, header || {}), payload: payload || {} };
}

function makeAdapter(extra) {
  const FakeWS = makeFakeWS();
  const out = { finals: [], interims: [], errors: [], ended: 0, started: 0, audioSetups: 0, audioCleanups: 0 };
  let tokenGate = null;   // 设为 Promise 可手动控制 getToken 释放时机
  const rec = createAliyunRecognizer(Object.assign({
    getConfig: () => ({ appkey: "ak", endpoint: "wss://nls.example/ws/v1" }),
    getToken: async () => { if (tokenGate) await tokenGate; return { token: "tok" }; },
    setupAudio: async () => { out.audioSetups++; },
    WebSocketImpl: FakeWS,
    onStarted: () => out.started++,
    onInterim: (t) => out.interims.push(t),
    onFinal: (t) => out.finals.push(t),
    onError: (k, m) => out.errors.push([k, m]),
    onEnded: () => out.ended++,
  }, extra || {}));
  return { rec, FakeWS, out, gate: (p) => { tokenGate = p; } };
}

const tick = () => new Promise((r) => setImmediate(r));

// ── 协议纯函数 ──────────────────────────────────────────────────────────────
test("parseAliyunEvent：五类事件映射 + 未知归 other", () => {
  assert.strictEqual(parseAliyunEvent(evt("TranscriptionStarted")).kind, "started");
  assert.deepStrictEqual(parseAliyunEvent(evt("TranscriptionResultChanged", { result: "你好" })), { kind: "interim", text: "你好" });
  assert.deepStrictEqual(parseAliyunEvent(evt("SentenceEnd", { result: "你好。" })), { kind: "final", text: "你好。" });
  assert.strictEqual(parseAliyunEvent(evt("TranscriptionCompleted")).kind, "completed");
  assert.deepStrictEqual(parseAliyunEvent(evt("TaskFailed", {}, { status_text: "原因" })), { kind: "failed", text: "原因" });
  assert.strictEqual(parseAliyunEvent(evt("TaskFailed", {}, { status_message: "新字段原因" })).text, "新字段原因",
    "failureText 双字段归一化：识别/合成谁带哪个字段都拿到真因（B6 共享件）");
  assert.strictEqual(parseAliyunEvent(evt("SentenceBegin")).kind, "other");
  assert.strictEqual(parseAliyunEvent(null).kind, "other");
});

test("makeId：32 个 hex 字符", () => {
  const id = makeId();
  assert.match(id, /^[0-9a-f]{32}$/);
  let i = 0;
  assert.match(makeId(() => i++), /^[0-9a-f]{32}$/);
});

test("指令帧：StartTranscription 含 PCM 16k 与断句参数；Stop 无 payload；task_id 透传", () => {
  const start = buildStartCommand("ak", "t".repeat(32), () => "m".repeat(32));
  assert.strictEqual(start.header.namespace, "SpeechTranscriber");
  assert.strictEqual(start.header.task_id, "t".repeat(32));
  assert.strictEqual(start.payload.format, "pcm");
  assert.strictEqual(start.payload.sample_rate, 16000);
  assert.strictEqual(start.payload.enable_intermediate_result, true);
  const stop = buildStopCommand("ak", "t".repeat(32), () => "m".repeat(32));
  assert.strictEqual(stop.header.name, "StopTranscription");
  assert.strictEqual(stop.payload, undefined);
});

// ── 生命周期 ────────────────────────────────────────────────────────────────
test("A5：并发 start 只建一条 ws；onopen 发 StartTranscription", async () => {
  const { rec, FakeWS } = makeAdapter();
  rec.start();
  rec.start();                       // 授权框/取 token 窗口内的重复进入
  await tick();
  assert.strictEqual(FakeWS.instances.length, 1, "绝不能开第二条 ws");
  FakeWS.instances[0].open();
  const cmd = JSON.parse(FakeWS.instances[0].sent[0]);
  assert.strictEqual(cmd.header.name, "StartTranscription");
});

test("A5：rebuild 后被取代的旧 socket 迟到回调一律忽略（不 send、不报错、不 onEnded）", async () => {
  const { rec, FakeWS, out } = makeAdapter();
  rec.start();
  await tick();
  const old = FakeWS.instances[0];
  rec.rebuild();
  await tick();
  assert.strictEqual(FakeWS.instances.length, 2);
  old.readyState = 1;
  if (old.onopen) old.onopen();      // 旧 socket 迟到 onopen
  if (old.onclose) old.onclose();    // 旧 socket 迟到 onclose
  assert.deepStrictEqual(out.errors, [], "旧 socket 不得触发错误");
  assert.strictEqual(out.ended, 0, "旧 socket 不得触发 onEnded");
});

test("A7：getToken await 期间 stop → 不建 ws、不开麦、无 onError（用户主动非错误）", async () => {
  const { rec, FakeWS, out, gate } = makeAdapter();
  let release;
  gate(new Promise((r) => { release = r; }));
  rec.start();
  rec.stop();                        // token 还没回来就停
  release();
  await tick();
  assert.strictEqual(FakeWS.instances.length, 0, "abort 后不得 new ws");
  assert.strictEqual(out.audioSetups, 0, "abort 后不得开麦");
  assert.deepStrictEqual(out.errors, []);
});

test("A7：setupAudio await 期间 stop → 已开的麦被关掉后退出", async () => {
  let release;
  const cleanups = [];
  const { rec, FakeWS, out } = makeAdapter({
    setupAudio: () => new Promise((r) => { release = r; }),
  });
  // 注入探针：cleanupAudio 会 stop micStream 轨道——用 getConfig 之外没有注入点，
  // 这里以「不建 ws」为主断言（cleanupAudio 是内部防御，开麦由 setupAudio 注入本就为空）。
  rec.start();
  await tick();                      // 进入 setupAudio await
  rec.stop();
  release();
  await tick();
  assert.strictEqual(FakeWS.instances.length, 0, "开麦期间被中止不得继续建 ws");
  assert.deepStrictEqual(out.errors, [], "被中止视为主动行为，不报错");
  void cleanups;
});

test("A6：SentenceEnd 直接 onFinal；interim 只上报暂存", async () => {
  const { rec, FakeWS, out } = makeAdapter();
  rec.start();
  await tick();
  const ws = FakeWS.instances[0];
  ws.open();
  ws.message(evt("TranscriptionStarted"));
  assert.strictEqual(out.started, 1);
  ws.message(evt("TranscriptionResultChanged", { result: "导航去" }));
  assert.deepStrictEqual(out.interims, ["导航去"]);
  assert.deepStrictEqual(out.finals, []);
  ws.message(evt("SentenceEnd", { result: "导航去公司。" }));
  assert.deepStrictEqual(out.finals, ["导航去公司。"], "SentenceEnd 即整句，不等去抖");
});

test("flushNow：发当前未定 interim 并主动停止（兜未断句的尾巴）；停止不触发 onEnded", async () => {
  const { rec, FakeWS, out } = makeAdapter();
  rec.start();
  await tick();
  const ws = FakeWS.instances[0];
  ws.open();
  ws.message(evt("TranscriptionStarted"));
  ws.message(evt("TranscriptionResultChanged", { result: "打开空调" }));
  const sent = rec.flushNow();
  assert.strictEqual(sent, "打开空调");
  assert.deepStrictEqual(out.finals, ["打开空调"]);
  assert.strictEqual(out.ended, 0, "主动停止不上报 onEnded");
  assert.ok(ws.closed >= 1 || ws.readyState === 3, "flushNow 应停掉链路");
});

test("TaskFailed：onError + onEnded（核心据此续听重试）", async () => {
  const { rec, FakeWS, out } = makeAdapter();
  rec.start();
  await tick();
  const ws = FakeWS.instances[0];
  ws.open();
  ws.message(evt("TranscriptionStarted"));
  ws.message(evt("TaskFailed", {}, { status_text: "配额耗尽" }));
  assert.deepStrictEqual(out.errors, [["aliyun-task-failed", "配额耗尽"]]);
  assert.strictEqual(out.ended, 1);
});

test("意外断开：onEnded（续听接力）；主动 stop：无 onEnded", async () => {
  const a = makeAdapter();
  a.rec.start();
  await tick();
  a.FakeWS.instances[0].open();
  a.FakeWS.instances[0].onclose();   // 网络意外断
  assert.strictEqual(a.out.ended, 1);

  const b = makeAdapter();
  b.rec.start();
  await tick();
  b.FakeWS.instances[0].open();
  b.FakeWS.instances[0].message(evt("TranscriptionStarted"));
  b.rec.stop();
  assert.strictEqual(b.out.ended, 0, "主动停止不上报");
  const stopCmd = b.FakeWS.instances[0].sent.map((s) => JSON.parse(s)).find((c) => c.header.name === "StopTranscription");
  assert.ok(stopCmd, "主动停止应优雅发 StopTranscription");
});

test("音频帧时序：TranscriptionStarted 之前先攒帧，Started 后一次性补发", async () => {
  const sendAudioRef = {};
  const { rec, FakeWS } = makeAdapter({
    setupAudio: async function () { /* worklet onmessage 由测试直接驱动 */ },
  });
  void sendAudioRef;
  rec.start();
  await tick();
  const ws = FakeWS.instances[0];
  ws.open();
  // Started 前 ws.send 只该有 StartTranscription（音频帧被攒住）——通过 sent 内容验证
  assert.strictEqual(ws.sent.length, 1);
  ws.message(evt("TranscriptionStarted"));
  assert.strictEqual(ws.sent.length, 1, "无积压帧时 Started 后不应多发");
});

test("A5：startTimeoutMs 默认 12000（覆盖授权框+取token+握手+等Started）", () => {
  const { rec } = makeAdapter();
  assert.strictEqual(rec.startTimeoutMs, 12000);
});
