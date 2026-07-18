const test = require("node:test");
const assert = require("node:assert");
const createVoiceRecognizerProvider = require("../nano_openclaw/adapters/webui/static/voice-recognizer-provider.js");
const createVoiceSpeakerProvider = require("../nano_openclaw/adapters/webui/static/voice-speaker-provider.js");

test("recognizer provider creates cloud and standby webspeech adapters", () => {
  const calls = [];
  const provider = createVoiceRecognizerProvider({
    createAliyunRecognizer: (opts) => { calls.push(["aliyun", opts]); return { name: "aliyun" }; },
    createOpenAIRecognizer: (opts) => { calls.push(["openai-compatible", opts]); return { name: "openai-compatible" }; },
    createWebspeechRecognizer: (opts) => { calls.push(["webspeech", opts]); return { name: "webspeech" }; },
    getAliyunConfig: () => ({ appkey: "ak" }),
    getToken: () => ({ token: "t" }),
    getOpenAIUrl: () => "ws://nano/api/voice/realtime",
  });

  provider.create("aliyun", { callbacks: { onFinal() {} } });
  provider.create("openai-compatible", { callbacks: { onFinal() {} } });
  provider.create("webspeech", { standby: true, callbacks: { onFinal() {} } });

  assert.strictEqual(calls[0][0], "aliyun");
  assert.deepStrictEqual(calls[0][1].getConfig(), { appkey: "ak" });
  assert.strictEqual(typeof calls[0][1].getToken, "function");
  assert.strictEqual(calls[1][0], "openai-compatible");
  assert.strictEqual(calls[1][1].getUrl(), "ws://nano/api/voice/realtime");
  assert.strictEqual(calls[2][0], "webspeech");
  assert.strictEqual(calls[2][1].baseSilenceMs, 800);
  assert.strictEqual(calls[2][1].maxSilenceMs, 1600);
});

test("speaker provider builds symmetric fallback levels and forwards player callbacks", () => {
  let capturedLevels = null;
  let capturedPlayerOpts = null;
  const provider = createVoiceSpeakerProvider({
    createFallbackSpeaker: (opts) => {
      capturedLevels = opts.levels.map((level) => level.name);
      const player = opts.createPlayer({
        onDrained() {},
        onAudible() {},
        onInterrupted() {},
        onError() {},
      });
      return { name: "fallback", player };
    },
    createLocalSpeaker: () => ({ name: "local" }),
    createFlowingSpeaker: () => ({ name: "flowing" }),
    createRestSpeaker: () => ({ name: "rest" }),
    createVoicePcmPlayer: (opts) => { capturedPlayerOpts = opts; return { name: "player" }; },
    getAliyunConfig: () => ({ voice: "v1", sampleRate: 16000 }),
    sampleRate: () => 16000,
    callbacks: { onAudible() {}, onDrained() {}, onFallback() {} },
  });

  const speaker = provider.build("aliyun-flowing");

  assert.deepStrictEqual(capturedLevels, ["aliyun-flowing", "aliyun-rest", "local"]);
  for (const key of ["onDrained", "onAudible", "onInterrupted", "onError"]) {
    assert.strictEqual(typeof capturedPlayerOpts[key], "function");
  }
  assert.strictEqual(capturedPlayerOpts.sampleRate, 16000);
  assert.strictEqual(speaker.name, "fallback");
});

test("speaker provider exposes speech-gateway REST level with local fallback", () => {
  let capturedLevels = null;
  const provider = createVoiceSpeakerProvider({
    createFallbackSpeaker: (opts) => {
      capturedLevels = opts.levels.map((level) => level.name);
      return { name: "fallback" };
    },
    createLocalSpeaker: () => ({ name: "local" }),
    createRestSpeaker: () => ({ name: "rest" }),
    createVoicePcmPlayer: () => ({ name: "player" }),
  });
  provider.build("openai-compatible");
  assert.deepStrictEqual(capturedLevels, ["openai-compatible", "local"]);
});
