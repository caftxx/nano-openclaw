/* 阿里云 RESTful 代理合成引擎回归：
 * 【B4】reader 子视图按 byteOffset 精确 slice（不取底层 buffer 视图外字节）
 * begin+push 必须 end 才 onCompleted（且仅一次）/ FIFO 串行泵 / abort 取消在途
 */
const test = require("node:test");
const assert = require("node:assert");
const createRestSpeaker = require("../nano_openclaw/gateway/webui/static/voice-speaker-rest.js");
const { splitForTts } = createRestSpeaker;

function okResponse(bytes) {
  return {
    ok: true,
    body: null,
    arrayBuffer: async () => bytes.buffer ? bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) : bytes,
  };
}

function streamResponse(chunks) {
  let i = 0;
  return {
    ok: true,
    body: {
      getReader: () => ({
        read: async () => (i < chunks.length ? { done: false, value: chunks[i++] } : { done: true }),
      }),
    },
  };
}

function makeSpeaker(fetchImpl, extra) {
  const out = { audio: [], completed: 0, errors: [] };
  const sp = createRestSpeaker(Object.assign({
    url: "/api/voice/tts",
    headers: { Authorization: "Bearer t" },
    getConfig: () => ({ voice: "xiaoxian", sampleRate: 16000 }),
    fetchImpl,
    onAudio: (b) => out.audio.push(new Uint8Array(b)),
    onCompleted: () => out.completed++,
    onError: (k, m) => out.errors.push([k, m]),
  }, extra || {}));
  return { sp, out };
}

const tick = () => new Promise((r) => setImmediate(r));

test("splitForTts：≤300 原样单段；>300 按上限切分且无丢字", () => {
  assert.deepStrictEqual(splitForTts("短文本。"), ["短文本。"]);
  assert.deepStrictEqual(splitForTts(""), []);
  const long = "字".repeat(750);
  const parts = splitForTts(long);
  assert.ok(parts.every((p) => p.length <= 300));
  assert.strictEqual(parts.join(""), long, "切分不得丢字");
});

test("FIFO 串行：多段按入队顺序逐个请求（音频不乱序）", async () => {
  const requested = [];
  let release = [];
  const fetchImpl = (url, init) => {
    requested.push(JSON.parse(init.body).text);
    return new Promise((r) => release.push(() => r(okResponse(new Uint8Array([requested.length])))));
  };
  const { sp, out } = makeSpeaker(fetchImpl);
  sp.begin();
  sp.push("一。");
  sp.push("二。");
  await tick();
  assert.deepStrictEqual(requested, ["一。"], "第二段必须等第一段完成");
  release[0]();
  await tick(); await tick();
  assert.deepStrictEqual(requested, ["一。", "二。"]);
  release[1]();
  await tick(); await tick();
  assert.deepStrictEqual([...out.audio.map((a) => a[0])], [1, 2]);
});

test("B4：reader 子视图按 byteOffset slice——不得取到底层 buffer 的视图外字节", async () => {
  // 构造底层 8 字节 buffer，子视图只覆盖中间 [2,5)
  const backing = new Uint8Array([9, 9, 1, 2, 3, 9, 9, 9]);
  const view = new Uint8Array(backing.buffer, 2, 3);
  const fetchImpl = async () => streamResponse([view]);
  const { sp, out } = makeSpeaker(fetchImpl);
  sp.begin();
  sp.push("x");
  await tick(); await tick(); await tick();
  assert.deepStrictEqual([...out.audio[0]], [1, 2, 3], "只投视图覆盖的字节");
});

test("契约：begin+push 不 end 永不 onCompleted；end 后队列排空 onCompleted 仅一次", async () => {
  const fetchImpl = async () => okResponse(new Uint8Array([1]));
  const { sp, out } = makeSpeaker(fetchImpl);
  sp.begin();
  sp.push("一。");
  await tick(); await tick(); await tick();
  assert.strictEqual(out.completed, 0, "上游没收尾（turn 未 done）不得 complete");
  sp.end();
  await tick();
  assert.strictEqual(out.completed, 1);
  sp.end();   // 重复 end 不得二次 complete
  assert.strictEqual(out.completed, 1);
});

test("end 先于 push 完成（泵仍在跑）：排空后才 onCompleted", async () => {
  let release;
  const fetchImpl = () => new Promise((r) => { release = () => r(okResponse(new Uint8Array([1]))); });
  const { sp, out } = makeSpeaker(fetchImpl);
  sp.begin();
  sp.push("一。");
  sp.end();                            // 在途请求未回就收尾
  await tick();
  assert.strictEqual(out.completed, 0);
  release();
  await tick(); await tick(); await tick();
  assert.strictEqual(out.completed, 1);
});

test("HTTP 非 2xx → onError 带原因；队列清空不再继续", async () => {
  const fetchImpl = async () => ({ ok: false, status: 403, text: async () => "Forbidden: tts disabled" });
  const { sp, out } = makeSpeaker(fetchImpl);
  sp.begin();
  sp.push("一。");
  sp.push("二。");
  await tick(); await tick(); await tick();
  assert.strictEqual(out.errors.length, 1);
  assert.match(out.errors[0][1], /Forbidden/);
});

test("abort：作废在途请求的迟到结果，不出声不报错不 complete", async () => {
  let release;
  const fetchImpl = () => new Promise((r) => { release = () => r(okResponse(new Uint8Array([1]))); });
  const { sp, out } = makeSpeaker(fetchImpl);
  sp.begin();
  sp.push("一。");
  sp.end();
  await tick();
  sp.abort();
  release();
  await tick(); await tick();
  assert.deepStrictEqual(out.audio, []);
  assert.strictEqual(out.completed, 0);
  assert.deepStrictEqual(out.errors, []);
});

test("请求体：携带文本/音色/采样率与鉴权 header", async () => {
  let captured;
  const fetchImpl = async (url, init) => { captured = { url, init }; return okResponse(new Uint8Array([1])); };
  const { sp } = makeSpeaker(fetchImpl);
  sp.begin();
  sp.push("你好。");
  await tick(); await tick(); await tick();
  assert.strictEqual(captured.url, "/api/voice/tts");
  const body = JSON.parse(captured.init.body);
  assert.deepStrictEqual(body, { text: "你好。", voice: "xiaoxian", sample_rate: 16000 });
  assert.strictEqual(captured.init.headers.Authorization, "Bearer t");
  assert.strictEqual(captured.init.headers["Content-Type"], "application/json");
});
