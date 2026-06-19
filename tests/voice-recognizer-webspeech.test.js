/* Web Speech 识别适配器回归：
 * 【A3】分片 final 去抖合并整句（动态静音分档），不被首 final 截断
 * 【A2】主动 stop 吞 onEnded、被替换旧对象回调忽略、重复 start 抗重入、start 抛错不吞
 * 【A1】denied 如实上报（前后台裁决在核心）
 */
const test = require("node:test");
const assert = require("node:assert");
const createWebspeechRecognizer = require("../nano_openclaw/adapters/webui/static/voice-recognizer-webspeech.js");

// ── Fakes ───────────────────────────────────────────────────────────────────
function fakeClock() {
  let pending = null, lastDelay = null, id = 0;
  return {
    setTimer(fn, delay) { pending = fn; lastDelay = delay; return ++id; },
    clearTimer() { pending = null; lastDelay = null; },
    fire() { const p = pending; pending = null; if (p) p(); },
    delay() { return lastDelay; },
    armed() { return pending !== null; },
  };
}

function makeFakeSR() {
  const instances = [];
  function FakeSR() {
    this.started = 0; this.aborted = 0;
    this.start = () => {
      if (FakeSR.throwOnStart) throw Object.assign(new Error("wrong state"), { name: "InvalidStateError" });
      this.started++;
    };
    this.abort = () => { this.aborted++; };
    instances.push(this);
  }
  FakeSR.instances = instances;
  return FakeSR;
}

function result(finals, interim) {
  // 构造 onresult 事件：finals 为已定片段数组，interim 为未定文本
  const results = finals.map((t) => Object.assign([{ transcript: t }], { isFinal: true }));
  if (interim) results.push(Object.assign([{ transcript: interim }], { isFinal: false }));
  return { resultIndex: 0, results };
}

function makeAdapter(extra) {
  const clock = fakeClock();
  const FakeSR = makeFakeSR();
  const out = { finals: [], interims: [], errors: [], ended: 0, started: 0 };
  const rec = createWebspeechRecognizer(Object.assign({
    SRImpl: FakeSR,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
    onStarted: () => out.started++,
    onInterim: (t) => out.interims.push(t),
    onFinal: (t) => out.finals.push(t),
    onError: (k, m) => out.errors.push([k, m]),
    onEnded: () => out.ended++,
  }, extra || {}));
  return { rec, clock, FakeSR, out, sr: () => FakeSR.instances[FakeSR.instances.length - 1] };
}

test("A3：分片 final 累积合并，静音到点才整句 flush（不被首 final 截断）", () => {
  const { rec, clock, out, sr } = makeAdapter();
  rec.start();
  sr().onstart();
  sr().onresult(result(["今天天气"], ""));
  assert.deepStrictEqual(out.finals, [], "首 final 不得立即发送");
  sr().onresult(result(["真不错"], ""));
  clock.fire();                                  // 持续静音到点
  assert.deepStrictEqual(out.finals, ["今天天气真不错"], "应合并全部分片");
});

test("A3：动态静音分档——短句 1600；interim 未收尾 +400；长句加码封顶 3200", () => {
  const { rec, clock, sr } = makeAdapter();
  rec.start();
  sr().onstart();
  sr().onresult(result(["短句"], ""));
  assert.strictEqual(clock.delay(), 1600, "短句 base 起步");
  sr().onresult(result([], "还在说"));
  assert.strictEqual(clock.delay(), 1600 + 400, "末次是未定 interim → +400");
  const long = "一二三四五六七八九十".repeat(8);   // 80 字
  sr().onresult(result([long], ""));
  assert.strictEqual(clock.delay(), Math.min(1600 + 300 + 400 + 300, 3200), "长句分档加码且封顶");
});

test("A3：任何语音活动重置静音计时器（说话中途的停顿不触发发送）", () => {
  const { rec, clock, out, sr } = makeAdapter();
  rec.start();
  sr().onstart();
  sr().onresult(result(["前半句"], ""));
  sr().onresult(result([], "后半"));             // interim 活动 → 重置计时器
  assert.ok(clock.armed());
  clock.fire();
  assert.deepStrictEqual(out.finals, ["前半句"], "自动 flush 只发已确认 buffer（interim 多半已 final 或作废）");
});

test("flushNow：点屏立即发送要带上未定 interim（最后几个字常还没 final）", () => {
  const { rec, out, sr } = makeAdapter();
  rec.start();
  sr().onstart();
  sr().onresult(result(["导航去"], "公司"));
  const sent = rec.flushNow();
  assert.strictEqual(sent, "导航去公司");
  assert.deepStrictEqual(out.finals, ["导航去公司"]);
  assert.strictEqual(out.ended, 0, "flushNow 的主动停麦不触发 onEnded");
});

test("flushNow 无内容：返回空串、不发送、不停麦", () => {
  const { rec, out, sr } = makeAdapter();
  rec.start();
  sr().onstart();
  assert.strictEqual(rec.flushNow(), "");
  assert.deepStrictEqual(out.finals, []);
});

test("A2：stop 清半句（不误发）且不触发 onEnded；自然 onend 才触发", () => {
  const { rec, clock, out, sr } = makeAdapter();
  rec.start();
  const r1 = sr();
  r1.onstart();
  r1.onresult(result(["半句"], ""));
  rec.stop();
  assert.ok(r1.aborted >= 1, "应 abort 立即终止");
  assert.strictEqual(out.ended, 0, "主动停止不上报 onEnded");
  clock.fire();
  assert.deepStrictEqual(out.finals, [], "停麦后残留计时器不得把半句发出去");
  rec.start();
  const r2 = sr();
  r2.onstart();
  r2.onend();                                    // 自然静音超时结束
  assert.strictEqual(out.ended, 1, "非主动结束应上报，核心续听接力");
});

test("A8：自然 onend 后续听必建新 SR 实例（不复用——复用会让 Chrome 识别随次数退化）", () => {
  const { rec, sr, FakeSR } = makeAdapter();
  rec.start();
  const r1 = sr();
  r1.onstart();
  assert.strictEqual(FakeSR.instances.length, 1);
  r1.onend();                                    // 自然静音超时结束（续听接力最高频路径）
  rec.start();                                   // 核心据 MIC_ENDED 续听重开
  assert.strictEqual(FakeSR.instances.length, 2, "onend 后必须建新实例，绝不复用已 end 的对象");
  const r2 = sr();
  assert.notStrictEqual(r2, r1);
  assert.strictEqual(r1.started, 1, "旧实例不得被二次 start");
  assert.strictEqual(r2.started, 1, "新实例被 start");
});

test("A2：被 rebuild 替换的旧对象回调一律忽略", () => {
  const { rec, out, sr, FakeSR } = makeAdapter();
  rec.start();
  const old = sr();
  old.onstart();
  rec.rebuild();
  assert.strictEqual(FakeSR.instances.length, 2, "rebuild 应建新对象");
  old.onend();                                   // 旧对象迟到回调
  old.onresult(result(["旧对象残留"], ""));
  assert.strictEqual(out.ended, 0, "旧对象 onend 不得上报");
  assert.deepStrictEqual(out.interims, [], "旧对象结果不得污染");
});

test("A2：重复 start 抗重入（starting/running 期间不二次 start）", () => {
  const { rec, sr } = makeAdapter();
  rec.start();
  rec.start();                                   // onstart 未回时重复调
  assert.strictEqual(sr().started, 1);
  sr().onstart();
  rec.start();                                   // running 中重复调
  assert.strictEqual(sr().started, 1);
});

test("A2：start() 抛错不吞——log 暴露，starting 复位（核心超时重建可再起）", () => {
  const logs = [];
  const { rec, FakeSR } = makeAdapter({ log: (k, m) => logs.push([k, m]) });
  FakeSR.throwOnStart = true;                    // 新建实例的 start 即抛 InvalidStateError
  rec.start();
  assert.ok(logs.some(([k]) => k === "start-failed"), "失败原因应被 log 暴露");
  assert.strictEqual(rec.busy(), false, "starting 应复位，不卡死");
  FakeSR.throwOnStart = false;
  rec.rebuild();                                 // 超时重建路径：丢弃旧对象后可正常再起
  assert.strictEqual(rec.busy(), true);
});

test("A1：denied 如实上报并立即清运行态（不等脆弱的 onend）", () => {
  const { rec, out, sr } = makeAdapter();
  rec.start();
  sr().onstart();
  sr().onerror({ error: "not-allowed" });
  assert.deepStrictEqual(out.errors[0][0], "denied");
  assert.strictEqual(rec.busy(), false);
});

test("startTimeoutMs 默认 1500（webspeech 启动窗口）", () => {
  const { rec } = makeAdapter();
  assert.strictEqual(rec.startTimeoutMs, 1500);
});
