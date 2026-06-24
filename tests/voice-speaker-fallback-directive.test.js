const test = require("node:test");
const assert = require("node:assert");
const createFallbackSpeaker = require("../nano_openclaw/adapters/webui/static/voice-speaker-fallback.js");
const ssml = require("../nano_openclaw/adapters/webui/static/voice-ssml.js");

test("parseTalkDirective strips first-line JSON and normalizes known keys", () => {
  const parsed = createFallbackSpeaker.parseTalkDirective(
    '{"voice":"longxiaochun","speed":1.1,"rate":190}\n\n你好。',
  );
  assert.deepStrictEqual(parsed.directive, {
    voiceId: "longxiaochun",
    speed: 1.1,
    rateWpm: 190,
  });
  assert.strictEqual(parsed.text, "你好。");
});

test("fallback speaker delays begin until first push so directive reaches engine", () => {
  const calls = [];
  const sp = createFallbackSpeaker({
    levels: [{
      name: "fake",
      usesPlayer: false,
      create: () => ({
        begin: (directive) => calls.push(["begin", directive]),
        push: (text, directive) => calls.push(["push", text, directive]),
        end: () => calls.push(["end"]),
        abort: () => {},
      }),
    }],
  });
  sp.begin();
  sp.push('{"voice":"v1","speed":1.2}\n\n第一句。');
  sp.end();
  assert.deepStrictEqual(calls, [
    ["begin", { voiceId: "v1", speed: 1.2 }],
    ["push", "第一句。", { voiceId: "v1", speed: 1.2 }],
    ["end"],
  ]);
});

test("SSML：REST 层收到多个完整 speak chunk，本地层剥离标签", () => {
  const restCalls = [];
  const localCalls = [];
  const sp = createFallbackSpeaker({
    levels: [
      {
        name: "aliyun-rest",
        usesPlayer: true,
        create: () => ({
          begin: () => restCalls.push(["begin"]),
          push: (text) => restCalls.push(["push", text]),
          end: () => restCalls.push(["end"]),
          abort: () => {},
        }),
      },
      {
        name: "local",
        usesPlayer: false,
        create: () => ({
          begin: () => localCalls.push(["begin"]),
          push: (text) => localCalls.push(["push", text]),
          end: () => localCalls.push(["end"]),
          abort: () => {},
        }),
      },
    ],
    createPlayer: () => ({ enqueue() {}, markEnded() {}, stop() {}, isActive: () => false }),
    ssml,
  });
  const xml = `<speak><emotion category="happy" intensity="1.0">${"你好。".repeat(160)}</emotion></speak>`;
  sp.begin();
  sp.push(xml);
  sp.end();
  const chunks = restCalls.filter((c) => c[0] === "push").map((c) => c[1]);
  assert.ok(chunks.length > 1);
  for (const chunk of chunks) {
    assert.ok(ssml.isSsml(chunk));
    assert.ok(ssml.stripSsmlToText(chunk).length <= ssml.REST_VISIBLE_CHAR_LIMIT);
  }

  const local = createFallbackSpeaker({
    levels: [{
      name: "local",
      usesPlayer: false,
      create: () => ({
        begin: () => localCalls.push(["local-begin"]),
        push: (text) => localCalls.push(["local-push", text]),
        end: () => localCalls.push(["local-end"]),
        abort: () => {},
      }),
    }],
    ssml,
  });
  local.begin();
  local.push("<speak><emotion category=\"happy\">你好 &amp; 再见</emotion></speak>");
  local.end();
  assert.deepStrictEqual(localCalls.slice(-3), [
    ["local-begin"],
    ["local-push", "你好 & 再见"],
    ["local-end"],
  ]);
});
