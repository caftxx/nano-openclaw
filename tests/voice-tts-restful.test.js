/* 阿里云 RESTful 代理合成引擎单测：splitForTts 纯函数 + 注入 fake fetch 跑串行泵，
 * 覆盖按序投递音频、end→onComplete、失败→onError、abort 取消（与 voice-tts-aliyun.test.js 同思路）。 */
const test = require("node:test");
const assert = require("node:assert");
const createRestfulSynthesizer = require("../nano_openclaw/gateway/webui/static/voice-tts-restful.js");

const { splitForTts } = createRestfulSynthesizer;

// 让微任务队列排空：泵是 async/await 串行，断言前要 await 几轮。
function flush(n) {
  let p = Promise.resolve();
  for (let i = 0; i < (n || 8); i++) p = p.then(() => {});
  return p;
}

// 构造一个返回整段 arrayBuffer 的假 Response。
function okResp(bytes) {
  return {
    ok: true,
    status: 200,
    async arrayBuffer() { return new Uint8Array(bytes).buffer; },
    async text() { return ""; },
  };
}

// ── splitForTts 纯函数 ─────────────────────────────────────────────────────
test("splitForTts: 短文本不切", () => {
  assert.deepStrictEqual(splitForTts("你好"), ["你好"]);
});

test("splitForTts: 空文本返回空数组", () => {
  assert.deepStrictEqual(splitForTts(""), []);
});

test("splitForTts: >300 字符被切成多段且每段 ≤300", () => {
  // 用句号穿插，确保既能在标点切也能硬切。
  const long = ("一二三四五六七八九十".repeat(40) + "。").repeat(3);   // 远超 300
  const parts = splitForTts(long);
  assert.ok(parts.length > 1, "应被切成多段");
  for (const p of parts) assert.ok(p.length <= 300, `每段 ≤300，实际 ${p.length}`);
  assert.strictEqual(parts.join(""), long, "拼接应还原原文");
});

test("splitForTts: 无标点超长文本硬切到 ≤300", () => {
  const long = "字".repeat(750);
  const parts = splitForTts(long);
  for (const p of parts) assert.ok(p.length <= 300);
  assert.strictEqual(parts.join(""), long);
});

// ── 按序投递音频 + end → onComplete ─────────────────────────────────────────
test("push 多段 → onAudio 按序触发；end → onComplete 一次", async () => {
  const calls = [];
  let completes = 0, starts = 0;
  const fetchImpl = async (url, opts) => {
    const body = JSON.parse(opts.body);
    // 用文本首字符当音频内容标记，便于断言顺序。
    return okResp([body.text.charCodeAt(0)]);
  };
  const tts = createRestfulSynthesizer({
    url: "/api/voice/tts",
    getConfig: () => ({ voice: "xiaoyun", sampleRate: 16000 }),
    onAudio: (buf) => calls.push(new Uint8Array(buf)[0]),
    onStart: () => { starts++; },
    onComplete: () => { completes++; },
    onError: (n, m) => { throw new Error("unexpected onError " + n + " " + m); },
    fetchImpl,
  });
  tts.begin();
  tts.push("A段");
  tts.push("B段");
  tts.push("C段");
  tts.end();
  await flush(20);
  assert.deepStrictEqual(calls, ["A".charCodeAt(0), "B".charCodeAt(0), "C".charCodeAt(0)], "音频按文本顺序投递");
  assert.strictEqual(starts, 1, "onStart 只触发一次");
  assert.strictEqual(completes, 1, "onComplete 只触发一次");
});

test("end 在队列已空闲时立即 onComplete", async () => {
  let completes = 0;
  const tts = createRestfulSynthesizer({
    getConfig: () => ({ voice: "x", sampleRate: 16000 }),
    onComplete: () => { completes++; },
    fetchImpl: async () => okResp([1]),
  });
  tts.begin();
  tts.end();
  await flush(5);
  assert.strictEqual(completes, 1);
});

// ── 流式 reader 路径 ────────────────────────────────────────────────────────
test("resp.body.getReader 流式读：每块都 onAudio", async () => {
  const chunks = [];
  const fetchImpl = async () => ({
    ok: true,
    status: 200,
    body: {
      getReader() {
        let i = 0;
        const data = [new Uint8Array([10]), new Uint8Array([20])];
        return {
          async read() {
            if (i < data.length) return { done: false, value: data[i++] };
            return { done: true, value: undefined };
          },
        };
      },
    },
    async text() { return ""; },
  });
  const tts = createRestfulSynthesizer({
    getConfig: () => ({ voice: "x", sampleRate: 16000 }),
    onAudio: (buf) => chunks.push(new Uint8Array(buf)[0]),
    fetchImpl,
  });
  tts.begin();
  tts.push("hi");
  tts.end();
  await flush(15);
  assert.deepStrictEqual(chunks, [10, 20]);
});

// ── 失败：resp.ok=false → onError，不 onComplete ────────────────────────────
test("resp.ok=false → onError(restful)，不触发 onComplete", async () => {
  let completes = 0;
  const errors = [];
  const fetchImpl = async () => ({ ok: false, status: 502, async text() { return "bad gateway"; } });
  const tts = createRestfulSynthesizer({
    getConfig: () => ({ voice: "x", sampleRate: 16000 }),
    onComplete: () => { completes++; },
    onError: (name, msg) => errors.push([name, msg]),
    fetchImpl,
  });
  tts.begin();
  tts.push("x");
  tts.end();
  await flush(15);
  assert.strictEqual(errors.length, 1);
  assert.strictEqual(errors[0][0], "restful");
  assert.ok(/bad gateway/.test(errors[0][1]));
  assert.strictEqual(completes, 0, "失败不应 onComplete");
});

test("fetch reject → onError(restful)，不触发 onComplete", async () => {
  let completes = 0;
  const errors = [];
  const fetchImpl = async () => { throw new Error("network down"); };
  const tts = createRestfulSynthesizer({
    getConfig: () => ({ voice: "x", sampleRate: 16000 }),
    onComplete: () => { completes++; },
    onError: (name, msg) => errors.push([name, msg]),
    fetchImpl,
  });
  tts.begin();
  tts.push("x");
  tts.end();
  await flush(15);
  assert.strictEqual(errors.length, 1);
  assert.strictEqual(errors[0][0], "restful");
  assert.strictEqual(completes, 0);
});

// ── abort：取消在途 + 后续不再 onAudio/onComplete ───────────────────────────
test("abort 取消在途请求：不再 onAudio / onComplete", async () => {
  const calls = [];
  let completes = 0;
  let releaseFetch;
  const gate = new Promise((res) => { releaseFetch = res; });
  const fetchImpl = async (url, opts) => {
    await gate;   // 卡住第一段请求，模拟在途
    return okResp([99]);
  };
  const tts = createRestfulSynthesizer({
    getConfig: () => ({ voice: "x", sampleRate: 16000 }),
    onAudio: (buf) => calls.push(new Uint8Array(buf)[0]),
    onComplete: () => { completes++; },
    onError: () => {},
    fetchImpl,
  });
  tts.begin();
  tts.push("first");
  tts.push("second");
  tts.end();
  await flush(3);
  tts.abort();          // 在途请求未返回时 abort
  releaseFetch();       // 放行那个在途请求
  await flush(15);
  assert.deepStrictEqual(calls, [], "abort 后迟到的请求不应 onAudio");
  assert.strictEqual(completes, 0, "abort 后不应 onComplete");
});

test("abort 幂等：重复调用不抛", () => {
  const tts = createRestfulSynthesizer({
    getConfig: () => ({ voice: "x", sampleRate: 16000 }),
    fetchImpl: async () => okResp([1]),
  });
  tts.begin();
  assert.doesNotThrow(() => { tts.abort(); tts.abort(); });
});
