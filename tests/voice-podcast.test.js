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

test("podcast cloud synthesis timeout is capped", () => {
  assert.strictEqual(PodcastMode._helpers.synthTimeoutMs("短句"), 12000);
  assert.strictEqual(PodcastMode._helpers.synthTimeoutMs("x".repeat(1000)), 30000);
});

function pcmBuffer(samples) {
  const bytes = new ArrayBuffer(samples.length * 2);
  const view = new DataView(bytes);
  samples.forEach((sample, index) => view.setInt16(index * 2, sample, true));
  return bytes;
}

function pcmRms(buf) {
  const view = new DataView(buf);
  let sum = 0;
  for (let i = 0; i < view.byteLength / 2; i++) {
    const value = view.getInt16(i * 2, true) / 32768;
    sum += value * value;
  }
  return Math.sqrt(sum / (view.byteLength / 2));
}

function pcmPeak(buf) {
  const view = new DataView(buf);
  let peak = 0;
  for (let i = 0; i < view.byteLength / 2; i++) {
    peak = Math.max(peak, Math.abs(view.getInt16(i * 2, true)) / 32768);
  }
  return peak;
}

test("podcast normalizes quiet and loud PCM voices toward common loudness", () => {
  const quiet = pcmBuffer(Array.from({ length: 400 }, (_, i) => (i % 2 ? -1200 : 1200)));
  const loud = pcmBuffer(Array.from({ length: 400 }, (_, i) => (i % 2 ? -12000 : 12000)));

  const quietOut = PodcastMode._helpers.normalizePcmChunks([quiet])[0];
  const loudOut = PodcastMode._helpers.normalizePcmChunks([loud])[0];

  assert.ok(pcmRms(quietOut) > pcmRms(quiet), "quiet voice should be amplified");
  assert.ok(pcmRms(loudOut) < pcmRms(loud), "loud voice should be attenuated");
  assert.ok(Math.abs(pcmRms(quietOut) - pcmRms(loudOut)) < 0.015);
  assert.ok(pcmPeak(quietOut) <= 0.95);
  assert.ok(pcmPeak(loudOut) <= 0.95);
});

test("podcast retries transient Aliyun TTS failures", () => {
  assert.strictEqual(PodcastMode._helpers.cloudTtsRetryDelayMs(1), 600);
  assert.strictEqual(PodcastMode._helpers.cloudTtsRetryDelayMs(3), 2400);
  assert.strictEqual(PodcastMode._helpers.cloudTtsRetryDelayMs(20), 4000);
  assert.strictEqual(PodcastMode._helpers.isRetryableCloudTtsError(new Error("HTTP 429")), true);
  assert.strictEqual(PodcastMode._helpers.isRetryableCloudTtsError(new Error("并发请求超限")), true);
  assert.strictEqual(PodcastMode._helpers.isRetryableCloudTtsError(new Error("HTTP 503")), true);
  assert.strictEqual(PodcastMode._helpers.isRetryableCloudTtsError(new Error("阿里云配置或 Token 缺失")), false);
  assert.strictEqual(PodcastMode._helpers.isRetryableCloudTtsError(new Error("flowing speaker unavailable")), false);
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
