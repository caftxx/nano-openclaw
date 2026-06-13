/* 阿里云流式合成引擎回归：
 * 【B6】TaskFailed 优先读 header.status_message（status_text 仅回退）
 * 协议帧构造 / Started 前攒文本与补发 StopSynthesis / abort 作废 in-flight begin
 */
const test = require("node:test");
const assert = require("node:assert");
const createFlowingSpeaker = require("../nano_openclaw/gateway/webui/static/voice-speaker-flowing.js");
const { parseTtsEvent, buildStartSynthesis, buildRunSynthesis, buildStopSynthesis } = createFlowingSpeaker;

function makeFakeWS() {
  const instances = [];
  function FakeWS(url) {
    this.url = url; this.readyState = 0; this.sent = []; this.closed = 0;
    this.send = (d) => { if (this.readyState !== 1) throw new Error("InvalidStateError"); this.sent.push(d); };
    this.close = () => { this.closed++; this.readyState = 3; };
    this.open = () => { this.readyState = 1; if (this.onopen) this.onopen(); };
    this.message = (obj) => { if (this.onmessage) this.onmessage({ data: JSON.stringify(obj) }); };
    this.binary = (buf) => { if (this.onmessage) this.onmessage({ data: buf }); };
    instances.push(this);
  }
  FakeWS.instances = instances;
  return FakeWS;
}

function evt(name, header) { return { header: Object.assign({ name }, header || {}) }; }

function makeSpeaker(extra) {
  const FakeWS = makeFakeWS();
  const out = { audio: [], completed: 0, errors: [] };
  let tokenGate = null;
  const sp = createFlowingSpeaker(Object.assign({
    getConfig: () => ({ appkey: "ak", endpoint: "wss://nls.example/ws/v1", voice: "xiaoxian", sampleRate: 16000 }),
    getToken: async () => { if (tokenGate) await tokenGate; return { token: "tok" }; },
    WebSocketImpl: FakeWS,
    onAudio: (b) => out.audio.push(b),
    onCompleted: () => out.completed++,
    onError: (k, m) => out.errors.push([k, m]),
  }, extra || {}));
  return { sp, FakeWS, out, gate: (p) => { tokenGate = p; } };
}

const tick = () => new Promise((r) => setImmediate(r));
const sentNames = (ws) => ws.sent.map((s) => JSON.parse(s).header.name);

test("B6：TaskFailed 优先读 status_message；无则回退 status_text；都无兜底", () => {
  assert.strictEqual(parseTtsEvent(evt("TaskFailed", { status_message: "真实原因" })).text, "真实原因");
  assert.strictEqual(parseTtsEvent(evt("TaskFailed", { status_text: "旧字段" })).text, "旧字段");
  assert.strictEqual(parseTtsEvent(evt("TaskFailed")).text, "task failed");
  assert.strictEqual(parseTtsEvent(evt("SynthesisStarted")).kind, "started");
  assert.strictEqual(parseTtsEvent(evt("SynthesisCompleted")).kind, "completed");
  assert.strictEqual(parseTtsEvent(evt("SentenceBegin")).kind, "other");
});

test("指令帧：StartSynthesis 含音色/采样率/pcm；Run 带文本；Stop 无 payload", () => {
  const start = buildStartSynthesis("ak", "t".repeat(32), { voice: "v1", sampleRate: 16000 }, () => "m".repeat(32));
  assert.strictEqual(start.header.namespace, "FlowingSpeechSynthesizer");
  assert.strictEqual(start.payload.voice, "v1");
  assert.strictEqual(start.payload.format, "pcm");
  const run = buildRunSynthesis("ak", "t".repeat(32), "你好", () => "m".repeat(32));
  assert.strictEqual(run.payload.text, "你好");
  const stop = buildStopSynthesis("ak", "t".repeat(32), () => "m".repeat(32));
  assert.strictEqual(stop.header.name, "StopSynthesis");
  assert.strictEqual(stop.payload, undefined);
});

test("Started 前 push 的文本先攒队列，Started 后按序补发", async () => {
  const { sp, FakeWS } = makeSpeaker();
  sp.begin();
  sp.push("第一句。");
  sp.push("第二句。");
  await tick();
  const ws = FakeWS.instances[0];
  ws.open();
  assert.deepStrictEqual(sentNames(ws), ["StartSynthesis"], "Started 前不得发文本");
  ws.message(evt("SynthesisStarted"));
  assert.deepStrictEqual(sentNames(ws), ["StartSynthesis", "RunSynthesis", "RunSynthesis"]);
  assert.deepStrictEqual(ws.sent.slice(1).map((s) => JSON.parse(s).payload.text), ["第一句。", "第二句。"]);
});

test("Talk directive：首段音色覆盖 StartSynthesis voice", async () => {
  const { sp, FakeWS } = makeSpeaker();
  sp.begin({ voiceId: "longxiaochun" });
  sp.push("第一句。", { voiceId: "longxiaochun" });
  await tick();
  const ws = FakeWS.instances[0];
  ws.open();
  const start = JSON.parse(ws.sent[0]);
  assert.strictEqual(start.payload.voice, "longxiaochun");
});

test("Started 前调 end()：Started 后补发 StopSynthesis（缓存文本不丢）", async () => {
  const { sp, FakeWS } = makeSpeaker();
  sp.begin();
  sp.push("唯一一句。");
  sp.end();                            // turn.done 先到
  await tick();
  const ws = FakeWS.instances[0];
  ws.open();
  ws.message(evt("SynthesisStarted"));
  assert.deepStrictEqual(sentNames(ws), ["StartSynthesis", "RunSynthesis", "StopSynthesis"]);
});

test("Binary frame → onAudio；SynthesisCompleted → onCompleted 并关 ws", async () => {
  const { sp, FakeWS, out } = makeSpeaker();
  sp.begin();
  sp.push("hi");
  await tick();
  const ws = FakeWS.instances[0];
  ws.open();
  ws.message(evt("SynthesisStarted"));
  const pcm = new ArrayBuffer(4);
  ws.binary(pcm);
  assert.strictEqual(out.audio[0], pcm);
  ws.message(evt("SynthesisCompleted"));
  assert.strictEqual(out.completed, 1);
  assert.ok(ws.closed >= 1);
});

test("B6：TaskFailed → onError 携带 status_message 真实原因", async () => {
  const { sp, FakeWS, out } = makeSpeaker();
  sp.begin();
  await tick();
  const ws = FakeWS.instances[0];
  ws.open();
  ws.message(evt("TaskFailed", { status_message: "appkey 未开通商用版" }));
  assert.deepStrictEqual(out.errors, [["aliyun-task-failed", "appkey 未开通商用版"]]);
});

test("abort 作废 getToken await 中的 in-flight begin：不建 ws、不报错", async () => {
  const { sp, FakeWS, out, gate } = makeSpeaker();
  let release;
  gate(new Promise((r) => { release = r; }));
  sp.begin();
  sp.abort();
  release();
  await tick();
  assert.strictEqual(FakeWS.instances.length, 0);
  assert.deepStrictEqual(out.errors, []);
});

test("抗重入：begin 进行中重复 begin/push 不建第二条 ws", async () => {
  const { sp, FakeWS } = makeSpeaker();
  sp.begin();
  sp.begin();
  sp.push("x");   // push 在 starting 中不得再触发 begin
  await tick();
  assert.strictEqual(FakeWS.instances.length, 1);
});

test("abort 后被取代旧 socket 的迟到回调一律忽略", async () => {
  const { sp, FakeWS, out } = makeSpeaker();
  sp.begin();
  await tick();
  const old = FakeWS.instances[0];
  sp.abort();
  old.readyState = 1;
  if (old.onopen) old.onopen();
  if (old.onmessage) old.message(evt("TaskFailed", { status_message: "迟到失败" }));
  assert.deepStrictEqual(out.errors, [], "旧 socket 事件不得污染");
});
