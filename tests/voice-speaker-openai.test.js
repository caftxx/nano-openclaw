const test = require("node:test");
const assert = require("node:assert");
const createOpenAISpeaker = require("../nano_openclaw/adapters/webui/static/voice-speaker-openai.js");

class FakeWebSocket {
  static instances = [];

  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.sent = [];
    FakeWebSocket.instances.push(this);
  }

  open() {
    this.readyState = 1;
    if (this.onopen) this.onopen();
  }

  receive(event) {
    if (this.onmessage) this.onmessage({ data: JSON.stringify(event) });
  }

  send(payload) { this.sent.push(JSON.parse(payload)); }

  close() {
    this.readyState = 3;
    if (this.onclose) this.onclose();
  }
}

function fixture() {
  FakeWebSocket.instances = [];
  const out = { audio: [], completed: 0, errors: [] };
  const speaker = createOpenAISpeaker({
    getUrl: () => "ws://nano/api/voice/realtime?token=secret",
    getConfig: () => ({ model: "fun-cosyvoice3-0.5b", voice: "nano", sampleRate: 24000 }),
    WebSocketImpl: FakeWebSocket,
    onAudio: (audio) => out.audio.push([...new Uint8Array(audio)]),
    onCompleted: () => out.completed++,
    onError: (name, message) => out.errors.push([name, message]),
  });
  return { speaker, out };
}

test("unified realtime TTS negotiates, streams text, and emits PCM deltas", () => {
  const { speaker, out } = fixture();
  speaker.begin();
  speaker.push("你好，");
  speaker.push("世界。");
  speaker.end();

  const ws = FakeWebSocket.instances[0];
  assert.strictEqual(ws.url, "ws://nano/api/voice/realtime?token=secret&model=fun-cosyvoice3-0.5b&voice=nano");
  ws.open();
  ws.receive({ type: "session.created" });
  assert.strictEqual(ws.sent[0].type, "session.update");
  assert.strictEqual(ws.sent[0].session.type, "realtime");
  ws.receive({
    type: "session.updated",
    session: { audio: { output: { format: { rate: 24000 } } } },
  });
  assert.strictEqual(ws.sent[1].type, "response.create");
  ws.receive({ type: "response.created" });
  speaker.end();
  assert.deepStrictEqual(ws.sent.slice(2), [
    { type: "speech.input_text.delta", delta: "你好，" },
    { type: "speech.input_text.delta", delta: "世界。" },
    { type: "speech.input_text.done" },
  ]);

  ws.receive({ type: "response.output_audio.delta", delta: Buffer.from([1, 2, 3]).toString("base64") });
  ws.receive({ type: "response.output_audio.done" });
  ws.receive({ type: "response.done", response: { status: "completed" } });
  assert.deepStrictEqual(out.audio, [[1, 2, 3]]);
  assert.strictEqual(out.completed, 1);
  assert.deepStrictEqual(out.errors, []);
});

test("realtime TTS validates output sample rate", () => {
  const { speaker, out } = fixture();
  speaker.begin();
  const ws = FakeWebSocket.instances[0];
  ws.open();
  ws.receive({ type: "session.created" });
  ws.receive({
    type: "session.updated",
    session: { audio: { output: { format: { rate: 16000 } } } },
  });
  assert.strictEqual(out.errors.length, 1);
  assert.strictEqual(out.errors[0][0], "sample-rate");
  assert.match(out.errors[0][1], /24000.*16000/);
});

test("abort closes realtime TTS without reporting a fallback error", () => {
  const { speaker, out } = fixture();
  speaker.begin();
  const ws = FakeWebSocket.instances[0];
  ws.open();
  speaker.abort();
  assert.strictEqual(ws.readyState, 3);
  assert.deepStrictEqual(out.errors, []);
  assert.strictEqual(out.completed, 0);
});
