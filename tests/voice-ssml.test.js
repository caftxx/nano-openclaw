const test = require("node:test");
const assert = require("node:assert");
const ssml = require("../nano_openclaw/adapters/webui/static/voice-ssml.js");

test("isSsml only accepts a single speak root", () => {
  assert.strictEqual(ssml.isSsml("<speak>你好</speak>"), true);
  assert.strictEqual(ssml.isSsml("<emotion>你好</emotion>"), false);
  assert.strictEqual(ssml.isSsml("<speak>你好</speak><speak>再见</speak>"), false);
  assert.strictEqual(ssml.isSsml("<speak><emotion>坏</speak>"), false);
});

test("stripSsmlToText removes tags and decodes entities", () => {
  assert.strictEqual(
    ssml.stripSsmlToText("<speak><emotion category=\"happy\">你好 &amp; 再见</emotion><break time=\"300ms\"/></speak>"),
    "你好 & 再见",
  );
});

test("chunkSsmlForAliyun splits long emotion while preserving attributes", () => {
  const sentence = "今天天气很好，适合出门。";
  const text = sentence.repeat(80);
  const input = `<speak><emotion category="happy" intensity="1.2">${text}</emotion></speak>`;
  const chunks = ssml.chunkSsmlForAliyun(input, "rest");
  assert.ok(chunks.length > 1);
  for (const chunk of chunks) {
    assert.match(chunk, /^<speak>/);
    assert.match(chunk, /<\/speak>$/);
    assert.match(chunk, /<emotion category="happy" intensity="1.2">/);
    assert.ok(ssml.isSsml(chunk));
    assert.ok(ssml.stripSsmlToText(chunk).length <= ssml.REST_VISIBLE_CHAR_LIMIT);
  }
});

test("bad XML falls back to original text as one chunk", () => {
  const bad = "<speak><emotion>没闭合</speak>";
  assert.deepStrictEqual(ssml.chunkSsmlForAliyun(bad, "rest"), [bad]);
});

test("flowing chunks respect weighted run limit", () => {
  const input = `<speak><emotion category="neutral">${"长".repeat(6000)}</emotion></speak>`;
  const chunks = ssml.chunkSsmlForAliyun(input, "flowing");
  assert.ok(chunks.length > 1);
  for (const chunk of chunks) {
    assert.ok(ssml._weightedLen(ssml.stripSsmlToText(chunk)) <= ssml.FLOWING_RUN_WEIGHT_LIMIT);
  }
});
