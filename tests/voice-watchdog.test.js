/* 回归：免提语音卡死无法恢复 —— SpeechRecognition 偶发卡死（abort 的 onend 永不回 →
 * recognizing 卡 true；或 start() 抛 InvalidStateError 被吞），让免提停在
 * "思考中/已停止朗读，等待回复结束…"且无人再驱动，纯前台只能刷新页面恢复。
 * 这里验证看门狗：想听却没真正在听时，到点强制回调 onTimeout（调用方重建重开）；
 * onstart 确认在听后撤销兜底；不该听时（在读/等回复/暂停）不插手。 */
const test = require("node:test");
const assert = require("node:assert");
const createListenWatchdog = require("../nano_openclaw/gateway/webui/static/voice-watchdog.js");

// 假时钟：看门狗是单一兜底计时器（arm 时 clear 旧的、set 新的），同时只有一个 pending。
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

test("想听却没真正在听：到点强制 onTimeout（重建重开）", () => {
  const clock = fakeClock();
  let rebuilt = 0;
  const wd = createListenWatchdog({
    shouldListen: () => true,            // 仍应当聆听
    onTimeout: () => rebuilt++,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });
  wd.arm();
  assert.ok(clock.armed(), "arm 后应有兜底计时器");
  assert.strictEqual(rebuilt, 0, "未到点不应重建");
  clock.fire();
  assert.strictEqual(rebuilt, 1, "到点且仍应听 → 强制重建一次");
  assert.ok(!clock.armed(), "fire 后计时器应已解除");
});

test("onstart 确认在听：confirmed() 撤销兜底，不再重建", () => {
  const clock = fakeClock();
  let rebuilt = 0;
  const wd = createListenWatchdog({
    shouldListen: () => true,
    onTimeout: () => rebuilt++,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });
  wd.arm();
  wd.confirmed();                        // 真的开始识别了
  assert.ok(!clock.armed(), "confirmed 应撤销兜底");
  clock.fire();                          // 即使误触发也不应重建（pending 已清）
  assert.strictEqual(rebuilt, 0);
});

test("到点时已不该听（在读/等回复/暂停/切后台）→ 不插手", () => {
  const clock = fakeClock();
  let rebuilt = 0;
  let listen = true;
  const wd = createListenWatchdog({
    shouldListen: () => listen,
    onTimeout: () => rebuilt++,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });
  wd.arm();
  listen = false;                        // 期间切到朗读 / 等回复
  clock.fire();
  assert.strictEqual(rebuilt, 0, "不该听时不强制重建，避免打断朗读/抢跑");
});

test("stopRecognition 等主动停麦：clear() 撤销兜底", () => {
  const clock = fakeClock();
  let rebuilt = 0;
  const wd = createListenWatchdog({
    shouldListen: () => true,
    onTimeout: () => rebuilt++,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });
  wd.arm();
  wd.clear();
  assert.ok(!clock.armed(), "clear 应撤销兜底");
  clock.fire();
  assert.strictEqual(rebuilt, 0);
});

test("重复 arm：撤销旧计时器、只保留一个 pending（不会多次重建）", () => {
  const clock = fakeClock();
  let rebuilt = 0;
  const wd = createListenWatchdog({
    shouldListen: () => true,
    onTimeout: () => rebuilt++,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });
  wd.arm();
  wd.arm();                              // resume 路径可能反复进入中间态
  assert.ok(clock.armed());
  clock.fire();
  assert.strictEqual(rebuilt, 1, "同时只有一个 pending → 仅重建一次");
});

test("默认超时 1500ms（纯前台卡死的自愈窗口）", () => {
  const clock = fakeClock();
  const wd = createListenWatchdog({
    shouldListen: () => true,
    onTimeout: () => {},
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });
  wd.arm();
  assert.strictEqual(clock.delay(), 1500);
});

test("arm(ms)：传入超时覆盖默认（阿里云慢启动需更长兜底窗口）", () => {
  const clock = fakeClock();
  const wd = createListenWatchdog({
    shouldListen: () => true,
    onTimeout: () => {},
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  });
  wd.arm(12000);
  assert.strictEqual(clock.delay(), 12000, "传入 ms 应覆盖默认 timeoutMs");
  wd.arm();   // 不传参回落默认
  assert.strictEqual(clock.delay(), 1500, "不传参仍用默认 1500ms");
});
