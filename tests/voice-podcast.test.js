const test = require("node:test");
const assert = require("node:assert");
const PodcastMode = require("../nano_openclaw/adapters/webui/static/voice-podcast.js");

test("podcast helper exposes configured roles", () => {
  const roles = PodcastMode._helpers.roles;
  assert.ok(roles.includes("自动"));
  assert.ok(roles.includes("AI Agent研发工程师"));
  assert.ok(roles.includes("高性能网络协议设计师"));
  assert.ok(roles.includes("硬件工程师"));
});

test("podcast escapeHtml escapes speaker labels", () => {
  assert.strictEqual(PodcastMode._helpers.escapeHtml("<主持&人>"), "&lt;主持&amp;人&gt;");
});

test("podcast pcm playback timeout follows audio duration", () => {
  const oneSecond = new ArrayBuffer(16000 * 2);
  const elevenSeconds = new ArrayBuffer(16000 * 2 * 11);
  assert.strictEqual(
    PodcastMode._helpers.playbackTimeoutMs({ chunks: [oneSecond], sampleRate: 16000 }),
    10000,
  );
  assert.ok(
    PodcastMode._helpers.playbackTimeoutMs({ chunks: [elevenSeconds], sampleRate: 16000 }) >= 19000,
  );
});

test("podcast done text falls back to streamed deltas", () => {
  assert.strictEqual(
    PodcastMode._helpers.finalUtteranceText("", { text: "已经流式生成的观点" }),
    "已经流式生成的观点",
  );
  assert.strictEqual(
    PodcastMode._helpers.finalUtteranceText("最终观点", { text: "临时观点" }),
    "最终观点",
  );
});
