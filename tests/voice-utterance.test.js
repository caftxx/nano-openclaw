/* 回归：长句被中途截断 —— SpeechRecognition 在停顿处吐分片 final，
 * "拿到首个 final 即停麦发送"会丢后半句。这里验证累积器按静音去抖合并整句。 */
const test = require("node:test");
const assert = require("node:assert");
const createUtteranceAccumulator = require("../nano_openclaw/gateway/webui/static/voice-utterance.js");

// 假时钟：去抖是单一 pending 计时器（arm 时 clear 旧的、set 新的），同时只有一个 pending。
function fakeClock() {
  let pending = null, lastDelay = null, id = 0;
  return {
    setTimer(fn, delay) { pending = fn; lastDelay = delay; return ++id; },
    clearTimer() { pending = null; lastDelay = null; },
    fire() { const p = pending; pending = null; if (p) p(); },
    armed() { return pending !== null; },
    delay() { return lastDelay; },
  };
}

test("多个分片 final（停顿 < 阈值）合并成整句，长句不被截断", () => {
  const clock = fakeClock();
  const flushed = [];
  const acc = createUtteranceAccumulator({
    silenceMs: 1200,
    onFlush: (t) => flushed.push(t),
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });

  acc.feed("今天天气不错，", "");
  assert.strictEqual(flushed.length, 0, "停顿内不应 flush");
  assert.ok(clock.armed(), "应有静音计时器待触发");

  acc.feed("我们出去走走吧，", "");
  assert.strictEqual(flushed.length, 0);

  acc.feed("顺便买点东西。", "");
  assert.strictEqual(flushed.length, 0);

  clock.fire();   // 静音流逝
  assert.deepStrictEqual(flushed, ["今天天气不错，我们出去走走吧，顺便买点东西。"]);
  assert.ok(!clock.armed(), "flush 后计时器应已解除");
});

test("单条短 final fire 后恰好一次 flush", () => {
  const clock = fakeClock();
  const flushed = [];
  const acc = createUtteranceAccumulator({
    onFlush: (t) => flushed.push(t),
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });
  acc.feed("你好", "");
  assert.strictEqual(flushed.length, 0);
  clock.fire();
  assert.deepStrictEqual(flushed, ["你好"]);
});

test("计时器未 fire 前 reset() 不 flush（朗读/暂停时丢弃半句）", () => {
  const clock = fakeClock();
  const flushed = [];
  const acc = createUtteranceAccumulator({
    onFlush: (t) => flushed.push(t),
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });
  acc.feed("还没说完", "");
  assert.ok(clock.armed());
  acc.reset();
  assert.ok(!clock.armed(), "reset 应解除计时器");
  assert.strictEqual(flushed.length, 0, "reset 后即使 fire 也不应发出半句");
  clock.fire();   // 即使误触发也不应发（pending 已被 clear，这里仅防御性确认无残留）
  assert.strictEqual(flushed.length, 0);
});

test("仅 interim、无 final → fire 后不产生 flush（空文本）", () => {
  const clock = fakeClock();
  const flushed = [];
  const acc = createUtteranceAccumulator({
    onFlush: (t) => flushed.push(t),
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });
  acc.feed("", "正在说话");
  assert.ok(clock.armed(), "interim 也应重置静音计时器");
  clock.fire();
  assert.strictEqual(flushed.length, 0, "无 final 时不应发出文本");
});

test("feed 返回值正确反映 buffer+interim 实时展示文本", () => {
  const clock = fakeClock();
  const acc = createUtteranceAccumulator({
    onFlush: () => {},
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });
  assert.strictEqual(acc.feed("已确认部分。", "未确认尾巴"), "已确认部分。未确认尾巴");
  assert.strictEqual(acc.feed("", "新尾巴"), "已确认部分。新尾巴", "buffer 应保留、interim 替换");
  assert.strictEqual(acc.pending(), "已确认部分。", "pending 只含已确认 buffer，不含 interim");
});

// 公式：base 1200；长度档 +300(>=20)/+400(>=40)/+300(>=80)；interim +400；封顶 2600。
// 用 "字".repeat(n) 让累积长度一目了然，改系数时对照算式即可。
function delayFor(finalText, interim) {
  const clock = fakeClock();
  const acc = createUtteranceAccumulator({
    onFlush: () => {},
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });
  acc.feed(finalText, interim || "");
  return clock.delay();
}

test("静音等待时间按累积文本长度自动调整（不依赖标点）", () => {
  assert.strictEqual(delayFor("打开空调"), 1200, "短句=base，不再被无标点惩罚拖慢");
  assert.strictEqual(delayFor("字".repeat(25)), 1500, "1200 + 300(>=20)");
  assert.strictEqual(delayFor("字".repeat(45)), 1900, "1200 + 300 + 400(>=40)");
  assert.strictEqual(delayFor("字".repeat(85)), 2200, "1200 + 300 + 400 + 300(>=80)");
});

test("末次仍是未定 interim → 识别没收尾，再多等一档；但受上限保护", () => {
  assert.strictEqual(delayFor("字".repeat(4), "还在说"), 1600, "1200 + interim 400（合计 7 字，未到长度档）");
  assert.strictEqual(delayFor("字".repeat(45), "继续说"), 2300, "1200 + 300 + 400 + interim 400（合计 48 字）");
  assert.strictEqual(delayFor("字".repeat(85), "继续说"), 2600, "1200+300+400+300+400=2600 命中上限");
});
