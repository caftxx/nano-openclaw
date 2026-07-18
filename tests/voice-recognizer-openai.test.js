const test = require("node:test");
const assert = require("node:assert");
const createOpenAIRecognizer = require("../nano_openclaw/adapters/webui/static/voice-recognizer-openai.js");

test("OpenAI realtime parser accumulates deltas and returns final transcript", () => {
  let parsed = createOpenAIRecognizer.parseRealtimeEvent({
    type: "conversation.item.input_audio_transcription.delta",
    delta: "你好",
  }, "");
  assert.deepStrictEqual(parsed, { kind: "interim", text: "你好" });
  parsed = createOpenAIRecognizer.parseRealtimeEvent({
    type: "conversation.item.input_audio_transcription.delta",
    delta: "世界",
  }, parsed.text);
  assert.deepStrictEqual(parsed, { kind: "interim", text: "你好世界" });
  assert.deepStrictEqual(createOpenAIRecognizer.parseRealtimeEvent({
    type: "conversation.item.input_audio_transcription.completed",
    transcript: " 你好世界。 ",
  }, parsed.text), { kind: "final", text: "你好世界。" });
});

test("OpenAI recognizer performs session handshake and forwards callbacks", async () => {
  const instances = [];
  class FakeWebSocket {
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      this.sent = [];
      instances.push(this);
    }
    send(value) { this.sent.push(JSON.parse(value)); }
    close() { this.readyState = 3; }
  }
  const calls = [];
  const recognizer = createOpenAIRecognizer({
    WebSocketImpl: FakeWebSocket,
    getUrl: () => "ws://nano/api/voice/realtime?token=secret",
    setupAudio: async () => {},
    onStarted: () => calls.push(["started"]),
    onInterim: (text) => calls.push(["interim", text]),
    onFinal: (text) => calls.push(["final", text]),
  });

  await recognizer.start();
  const socket = instances[0];
  assert.strictEqual(socket.url, "ws://nano/api/voice/realtime?token=secret");
  socket.onmessage({ data: JSON.stringify({ type: "session.created" }) });
  assert.strictEqual(socket.sent[0].type, "session.update");
  assert.strictEqual(socket.sent[0].session.audio.input.format.rate, 16000);
  socket.onmessage({ data: JSON.stringify({ type: "session.updated" }) });
  socket.onmessage({ data: JSON.stringify({
    type: "conversation.item.input_audio_transcription.delta", delta: "测试",
  }) });
  socket.onmessage({ data: JSON.stringify({
    type: "conversation.item.input_audio_transcription.completed", transcript: "测试完成。",
  }) });

  assert.deepStrictEqual(calls, [
    ["started"], ["interim", "测试"], ["final", "测试完成。"],
  ]);
  recognizer.stop();
  assert.strictEqual(recognizer.busy(), false);
});

test("OpenAI recognizer flushes the current partial exactly once", async () => {
  let socket;
  class FakeWebSocket {
    constructor() { socket = this; this.readyState = 1; }
    send() {}
    close() { this.readyState = 3; }
  }
  const finals = [];
  const recognizer = createOpenAIRecognizer({
    WebSocketImpl: FakeWebSocket,
    getUrl: () => "ws://nano/api/voice/realtime",
    setupAudio: async () => {},
    onFinal: (text) => finals.push(text),
  });
  await recognizer.start();
  socket.onmessage({ data: JSON.stringify({
    type: "conversation.item.input_audio_transcription.delta", delta: "还没断句",
  }) });
  assert.strictEqual(recognizer.flushNow(), "还没断句");
  assert.strictEqual(recognizer.flushNow(), "");
  assert.deepStrictEqual(finals, ["还没断句"]);
});
