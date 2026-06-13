const test = require("node:test");
const assert = require("node:assert");
const createFallbackSpeaker = require("../nano_openclaw/gateway/webui/static/voice-speaker-fallback.js");

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
