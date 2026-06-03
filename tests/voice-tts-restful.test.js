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

test("流式 reader：r.value 为带 byteOffset 的子视图时，onAudio 收到的字节是该视图实际字节", async () => {
  // 模拟真实 reader：r.value 是指向更大底层 buffer 的子视图。若直接传 r.value.buffer，
  // 会带上视图外的字节（错位/垃圾）；修复后应 slice 出恰好该视图的字节。
  const larger = new Uint8Array([0, 1, 2, 3, 100, 101, 102, 103, 104, 105, 200, 201]);
  const sub = new Uint8Array(larger.buffer, 4, 6);   // 实际视图字节 = [100,101,102,103,104,105]
  const received = [];
  const fetchImpl = async () => ({
    ok: true,
    status: 200,
    body: {
      getReader() {
        let done = false;
        return {
          async read() {
            if (done) return { done: true, value: undefined };
            done = true;
            return { done: false, value: sub };
          },
        };
      },
    },
    async text() { return ""; },
  });
  const tts = createRestfulSynthesizer({
    getConfig: () => ({ voice: "x", sampleRate: 16000 }),
    onAudio: (buf) => received.push(Array.from(new Uint8Array(buf))),
    fetchImpl,
  });
  tts.begin();
  tts.push("hi");
  tts.end();
  await flush(15);
  assert.strictEqual(received.length, 1);
  // 必须恰为视图的 6 个字节，而非整块 12 字节底层 buffer。
  assert.deepStrictEqual(received[0], [100, 101, 102, 103, 104, 105]);
});

// ── 契约：begin+push 后必须 end() 才会 onComplete（缺 end 即卡死，对应 voice-mode Bug 1）─
test("契约：begin+push 后不 end → 不 onComplete；补 end → onComplete", async () => {
  let completes = 0;
  const tts = createRestfulSynthesizer({
    getConfig: () => ({ voice: "x", sampleRate: 16000 }),
    onComplete: () => { completes++; },
    fetchImpl: async () => okResp([1]),
  });
  tts.begin();
  tts.push("x");
  await flush(15);
  // 未 end()：endRequested 永远 false → onComplete 不触发（上层若不补 end 就会卡在朗读中）。
  assert.strictEqual(completes, 0, "未 end 不应 onComplete");
  tts.end();
  await flush(15);
  assert.strictEqual(completes, 1, "补 end 后应 onComplete 收尾");
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
