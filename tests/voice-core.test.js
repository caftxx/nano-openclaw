/* 语音核心状态机 —— 历史坑位的事件序列回放回归（编号对应重写前的 bugfix commit）：
 * 【A1】后台丢弃识别器，回前台恢复            【A4】纯前台卡死由 starting 超时自愈
 * 【C3】焦点派生：全程无持麦档（防假通话周期）  【D1】回前台延迟重建识别
 * 【D2】锁屏内容回前台全文重播一次且只一次     【D3】读完 500ms 冷却防尾音回采
 * 【E1】句读完 turn 未 done 不提前开麦         【E2】思考中点屏取消 + 防重复刷
 * 全部用 transition 纯函数回放，断言 (state', commands)。
 */
const test = require("node:test");
const assert = require("node:assert");
const VoiceCore = require("../nano_openclaw/adapters/webui/static/voice-core.js");
const { createInitialModel, transition, focusMode, matchWake } = VoiceCore;

// ── 回放工具 ────────────────────────────────────────────────────────────────
function replay(events, model) {
  model = model || createInitialModel();
  let lastCmds = [];
  for (const ev of events) {
    const r = transition(model, ev);
    model = { state: r.state, ctx: r.ctx };
    lastCmds = r.commands;
  }
  return { model, cmds: lastCmds };
}
const types = (cmds) => cmds.map((c) => c.type);
const has = (cmds, type) => cmds.some((c) => c.type === type);
const get = (cmds, type) => cmds.find((c) => c.type === type);

// 到达 capturing 的标准开场
const BOOT = [{ type: "OPEN", autoStart: true }, { type: "MIC_STARTED" }];

// ── 基本生命周期 ────────────────────────────────────────────────────────────
test("OPEN(autoStart) → starting[startMic+armTimer(start)]；MIC_STARTED → capturing 撤看门狗", () => {
  let r = replay([{ type: "OPEN", autoStart: true }]);
  assert.strictEqual(r.model.state, "starting");
  assert.ok(has(r.cmds, "startMic"));
  const t = get(r.cmds, "armTimer");
  assert.strictEqual(t.tag, "start");
  assert.strictEqual(t.ms, null, "启动窗口由 shell 按引擎 startTimeoutMs 填【A5】");
  r = replay(BOOT);
  assert.strictEqual(r.model.state, "capturing");
  assert.ok(has(r.cmds, "clearTimer"));
});

test("OPEN 硬阻断（HTTP）→ error，不开麦", () => {
  const r = replay([{ type: "OPEN", autoStart: true, hardBlock: "https" }]);
  assert.strictEqual(r.model.state, "error");
  assert.ok(!has(r.cmds, "startMic"));
});

test("OPEN 时已有进行中的 turn → thinking 等待，不抢麦", () => {
  const r = replay([{ type: "OPEN", autoStart: true, externalTurnOpen: true }]);
  assert.strictEqual(r.model.state, "thinking");
  assert.ok(!has(r.cmds, "startMic"));
});

test("TOGGLE 暂停：停麦停播留焦点；再 TOGGLE 恢复聆听", () => {
  let r = replay([...BOOT, { type: "TOGGLE" }]);
  assert.strictEqual(r.model.state, "paused");
  assert.ok(has(r.cmds, "stopMic") && has(r.cmds, "stopSpeech"));
  assert.ok(!has(r.cmds, "teardown"), "暂停不释放焦点（✕ 退出才放音乐回来）");
  r = replay([{ type: "TOGGLE" }], r.model);
  assert.strictEqual(r.model.state, "starting");
  assert.ok(has(r.cmds, "primeAudio"), "手势内应顺带解锁静音 audio/播放器");
});

test("CLOSE：全量清理 + teardown", () => {
  const r = replay([...BOOT, { type: "CLOSE" }]);
  assert.strictEqual(r.model.state, "closed");
  assert.ok(has(r.cmds, "teardown") && has(r.cmds, "stopMic") && has(r.cmds, "stopSpeech"));
});

// ── 一轮完整对话（happy path，覆盖 E1/D3）──────────────────────────────────
test("完整一轮：说话→发送→流式分句朗读→done 读尾→drain→冷却 500ms→续听", () => {
  let r = replay([...BOOT, { type: "MIC_FINAL", text: "今天天气怎么样" }]);
  assert.strictEqual(r.model.state, "thinking");
  assert.ok(has(r.cmds, "stopMic"));
  assert.strictEqual(get(r.cmds, "chatSend").text, "今天天气怎么样");

  r = replay([{ type: "CHAT_ACCEPTED", turnId: "t1" }], r.model);
  assert.strictEqual(r.model.state, "thinking");
  assert.ok(has(r.cmds, "stopSpeech"), "新 turn 清旧播报");

  r = replay([{ type: "TEXT_DELTA", text: "今天晴。气温" }], r.model);
  assert.strictEqual(r.model.state, "speaking");
  assert.deepStrictEqual(types(r.cmds), ["speakerBegin", "speak"], "首段懒开流再投");
  assert.strictEqual(get(r.cmds, "speak").text, "今天晴。");

  r = replay([{ type: "TEXT_DELTA", text: "25度" }], r.model);
  assert.ok(!has(r.cmds, "speak"), "没到句末标点不投");

  r = replay([{ type: "TURN_DONE", turnId: "t1" }], r.model);
  assert.strictEqual(r.model.state, "speaking");
  assert.strictEqual(get(r.cmds, "speak").text, "气温25度", "done 时把尾巴读掉");
  assert.ok(has(r.cmds, "speakerEnd"), "文本流收尾信号【B4 上游侧】");

  r = replay([{ type: "SPEAK_AUDIBLE" }, { type: "SPEAK_DRAINED" }], r.model);
  assert.strictEqual(r.model.state, "cooldown");
  assert.strictEqual(get(r.cmds, "armTimer").ms, 500, "出过声 → 冷却 500ms 防尾音回采【D3】");

  r = replay([{ type: "TIMEOUT", tag: "cooldown" }], r.model);
  assert.strictEqual(r.model.state, "starting");
  assert.ok(has(r.cmds, "startMic"));
});

test("E1：句读完但 turn 未 done → SPEAK_DRAINED 回 thinking 等 delta，不提前开麦", () => {
  const r = replay([
    ...BOOT,
    { type: "MIC_FINAL", text: "讲个故事" },
    { type: "CHAT_ACCEPTED", turnId: "t1" },
    { type: "TEXT_DELTA", text: "从前有座山。" },
    { type: "SPEAK_DRAINED" },                      // 第一句读完，流还开着
  ]);
  assert.strictEqual(r.model.state, "thinking");
  assert.ok(!has(r.cmds, "startMic"), "turn 未结束绝不开麦（防把后续 TTS 采进识别）");
});

test("整轮无可读文本（空回复）：不冷却直接续听", () => {
  const r = replay([
    ...BOOT,
    { type: "MIC_FINAL", text: "嗯" },
    { type: "CHAT_ACCEPTED", turnId: "t1" },
    { type: "TURN_DONE", turnId: "t1" },
  ]);
  assert.strictEqual(r.model.state, "starting");
  assert.ok(has(r.cmds, "startMic"));
  assert.ok(!has(r.cmds, "speakerEnd"), "没投过合成不该发收尾");
});

// ── A 组：识别生命周期 ──────────────────────────────────────────────────────
test("A4：starting 超时 → rebuildMic 强制重建并重新armed（纯前台卡死自愈）", () => {
  const r = replay([{ type: "OPEN", autoStart: true }, { type: "TIMEOUT", tag: "start" }]);
  assert.strictEqual(r.model.state, "starting");
  assert.deepStrictEqual(types(r.cmds), ["rebuildMic", "armTimer"]);
});

test("续听接力：MIC_ENDED（自然静音超时）→ 前台立即重开", () => {
  const r = replay([...BOOT, { type: "MIC_ENDED" }]);
  assert.strictEqual(r.model.state, "starting");
  assert.ok(has(r.cmds, "startMic"));
});

test("A1：后台丢弃识别器且不重开——hidden 时 MIC_ERROR(denied) 忽略、MIC_ENDED 不重启", () => {
  let r = replay([...BOOT, { type: "HIDDEN" }]);
  assert.strictEqual(r.model.state, "capturing");
  assert.ok(has(r.cmds, "dropMic"), "进后台先丢弃识别器，避免 Chrome 识别服务半死");
  assert.ok(r.cmds.some((c) => c.type === "clearTimer" && c.tag === "start"));
  r = replay([{ type: "MIC_ERROR", kind: "denied" }], r.model);
  assert.strictEqual(r.model.state, "capturing", "后台拒麦是暂时的，不进 error");
  r = replay([{ type: "MIC_ENDED" }], r.model);
  assert.strictEqual(r.model.state, "starting");
  assert.ok(!has(r.cmds, "startMic"), "后台不开新麦（会再触发 not-allowed 弄死免提）");
});

test("前台拒麦才是真拒绝 → error + 收回 wakeLock", () => {
  const r = replay([...BOOT, { type: "MIC_ERROR", kind: "denied" }]);
  assert.strictEqual(r.model.state, "error");
  assert.strictEqual(get(r.cmds, "wakeLock").on, false);
});

test("A1/D1：回前台恢复——丢弃旧识别实例，延迟 1.2s 重建开麦", () => {
  let r = replay([...BOOT, { type: "HIDDEN" }, { type: "VISIBLE" }]);
  assert.strictEqual(r.model.state, "cooldown");
  assert.ok(has(r.cmds, "dropMic"), "丢弃锁屏期间可能卡死的旧识别器 wrapper");
  assert.strictEqual(get(r.cmds, "armTimer").ms, 1200, "等浏览器麦克风/语音服务恢复");
  assert.ok(has(r.cmds, "recoverSpeechOutput"));
  r = replay([{ type: "TIMEOUT", tag: "cooldown" }], r.model);
  assert.strictEqual(r.model.state, "starting");
  assert.ok(has(r.cmds, "startMic"));
});

test("MIC_FINAL 空文本：原地继续聆听不发送", () => {
  const r = replay([...BOOT, { type: "MIC_FINAL", text: "  " }]);
  assert.strictEqual(r.model.state, "capturing");
  assert.ok(!has(r.cmds, "chatSend"));
});

test("MIC_INTERIM / FLUSH_EMPTY：仅更新状态行文案", () => {
  let r = replay([...BOOT, { type: "MIC_INTERIM", text: "导航去" }]);
  assert.strictEqual(r.model.ctx.statusOverride, "识别中：导航去");
  r = replay([{ type: "FLUSH_EMPTY" }], r.model);
  assert.match(r.model.ctx.statusOverride, /还没识别到内容/);
});

// ── 点屏路由（吸收旧 voice-tap）─────────────────────────────────────────────
test("TAP@capturing → primeAudio(手势解锁走命令通道) + flushMic（立即发送，不等去抖）", () => {
  const r = replay([...BOOT, { type: "TAP" }]);
  assert.deepStrictEqual(types(r.cmds), ["primeAudio", "flushMic"],
    "手势解锁与 OPEN/TOGGLE 同一命令通道，可回放测试可见");
});

test("E2：TAP@thinking(turn开) → cancelTurn；再点只更新提示不重复刷", () => {
  let r = replay([
    ...BOOT,
    { type: "MIC_FINAL", text: "x" },
    { type: "CHAT_ACCEPTED", turnId: "t1" },
    { type: "TAP" },
  ]);
  assert.strictEqual(get(r.cmds, "cancelTurn").turnId, "t1");
  assert.match(r.model.ctx.statusOverride, /正在停止/);
  r = replay([{ type: "TAP" }], r.model);
  assert.ok(!has(r.cmds, "cancelTurn"), "已发过取消不重复刷请求");
});

test("E1：TAP@speaking(turn开) → 停播+取消整轮，muted 后续 delta 不再开口", () => {
  let r = replay([
    ...BOOT,
    { type: "MIC_FINAL", text: "x" },
    { type: "CHAT_ACCEPTED", turnId: "t1" },
    { type: "TEXT_DELTA", text: "第一句。" },
    { type: "TAP" },
  ]);
  assert.strictEqual(r.model.state, "thinking");
  assert.ok(has(r.cmds, "stopSpeech") && has(r.cmds, "cancelTurn"));
  r = replay([{ type: "TEXT_DELTA", text: "第二句。" }], r.model);
  assert.ok(!has(r.cmds, "speak"), "打断后本轮禁播报");
  r = replay([{ type: "TURN_DONE", turnId: "t1" }], r.model);
  assert.notStrictEqual(r.model.state, "speaking", "muted 轮收尾直接回聆听路径");
});

test("TAP@speaking(turn已done) → 只停本地剩余播报，立即续听", () => {
  const r = replay([
    ...BOOT,
    { type: "MIC_FINAL", text: "x" },
    { type: "CHAT_ACCEPTED", turnId: "t1" },
    { type: "TEXT_DELTA", text: "好。" },
    { type: "TURN_DONE", turnId: "t1" },
    { type: "TAP" },
  ]);
  assert.strictEqual(r.model.state, "starting");
  assert.ok(has(r.cmds, "stopSpeech") && has(r.cmds, "startMic"));
  assert.ok(!has(r.cmds, "cancelTurn"), "没有可取消的 turn");
});

test("TAP@thinking(无turn) / TAP@cooldown → 直接回聆听", () => {
  let r = replay([
    ...BOOT,
    { type: "MIC_FINAL", text: "x" },   // thinking，但 CHAT_ACCEPTED 还没来
  ]);
  r = replay([{ type: "TAP" }], r.model);
  assert.strictEqual(r.model.state, "starting");

  r = replay([
    ...BOOT,
    { type: "MIC_FINAL", text: "x" },
    { type: "CHAT_ACCEPTED", turnId: "t1" },
    { type: "TEXT_DELTA", text: "好。" },
    { type: "TURN_DONE", turnId: "t1" },
    { type: "SPEAK_AUDIBLE" },          // 出过声 → drain 后才会进 cooldown
    { type: "SPEAK_DRAINED" },          // cooldown
    { type: "TAP" },
  ]);
  assert.strictEqual(r.model.state, "starting");
  assert.ok(has(r.cmds, "clearTimer"), "撤销冷却计时器");
});

// ── turn 异常路径 ───────────────────────────────────────────────────────────
test("TURN_ERROR：停播+读「出错了」→ drain 后冷却续听", () => {
  let r = replay([
    ...BOOT,
    { type: "MIC_FINAL", text: "x" },
    { type: "CHAT_ACCEPTED", turnId: "t1" },
    { type: "TURN_ERROR", message: "boom" },
  ]);
  assert.strictEqual(r.model.state, "speaking");
  assert.ok(has(r.cmds, "stopSpeech"));
  assert.strictEqual(get(r.cmds, "speakOnce").text, "出错了");
  r = replay([{ type: "SPEAK_DRAINED" }], r.model);
  assert.strictEqual(r.model.state, "cooldown");
});

test("TURN_CANCELLED：清场直接续听", () => {
  const r = replay([
    ...BOOT,
    { type: "MIC_FINAL", text: "x" },
    { type: "CHAT_ACCEPTED", turnId: "t1" },
    { type: "TAP" },
    { type: "TURN_CANCELLED", turnId: "t1" },
  ]);
  assert.strictEqual(r.model.state, "starting");
  assert.ok(has(r.cmds, "stopSpeech") && has(r.cmds, "startMic"));
});

test("CHAT_ACCEPTED 抢占 capturing（外部并行发消息）：停麦转 thinking，新 turn 接管", () => {
  const r = replay([...BOOT, { type: "CHAT_ACCEPTED", turnId: "ext" }]);
  assert.strictEqual(r.model.state, "thinking");
  assert.ok(has(r.cmds, "stopMic") && has(r.cmds, "stopSpeech"));
});

test("paused 时接 turn：muted——字幕照走（text 累积）但绝不开口", () => {
  let r = replay([{ type: "OPEN" }, { type: "CHAT_ACCEPTED", turnId: "t1" }]);
  assert.strictEqual(r.model.state, "paused");
  r = replay([{ type: "TEXT_DELTA", text: "第一句。" }], r.model);
  assert.ok(!has(r.cmds, "speak"));
  assert.strictEqual(r.model.ctx.turn.text, "第一句。", "字幕数据仍累积");
  r = replay([{ type: "TURN_DONE", turnId: "t1" }], r.model);
  assert.strictEqual(r.model.state, "paused", "暂停态收尾原地不动");
});

// ── D 组：锁屏/后台 ─────────────────────────────────────────────────────────
test("D2：锁屏期间 delta+done 不起播报只标记；回前台全文重播一次且只一次", () => {
  let r = replay([
    ...BOOT,
    { type: "MIC_FINAL", text: "x" },
    { type: "CHAT_ACCEPTED", turnId: "t1" },
    { type: "HIDDEN" },
    { type: "TEXT_DELTA", text: "锁屏时的第一句。" },
  ]);
  assert.ok(!has(r.cmds, "speak"), "后台不起播报");
  r = replay([{ type: "TURN_DONE", turnId: "t1" }], r.model);
  assert.ok(!has(r.cmds, "speakerEnd"), "后台不收尾合成流");
  r = replay([{ type: "VISIBLE" }], r.model);
  assert.strictEqual(r.model.state, "speaking");
  assert.ok(has(r.cmds, "stopSpeech"), "重播前先整链拆干净【D1】");
  assert.strictEqual(get(r.cmds, "speak").text, "锁屏时的第一句。", "从本轮开头完整重播");
  assert.ok(has(r.cmds, "speakerEnd"), "turn 已 done：补收尾，避免卡『朗读中』");
  // 再来一次 HIDDEN/VISIBLE：不得重播第二遍
  r = replay([{ type: "SPEAK_DRAINED" }, { type: "HIDDEN" }, { type: "VISIBLE" }], r.model);
  assert.ok(!has(r.cmds, "speak"), "重播一次且只一次");
});

test("D2：回前台发现本地 synth 卡着挂起队列（speechBusy）→ 同样触发全文重播", () => {
  let r = replay([
    ...BOOT,
    { type: "MIC_FINAL", text: "x" },
    { type: "CHAT_ACCEPTED", turnId: "t1" },
    { type: "TEXT_DELTA", text: "第一句。" },
    { type: "TURN_DONE", turnId: "t1" },
    { type: "HIDDEN" },                                  // 锁屏时正在播
    { type: "VISIBLE", speechBusy: true },               // synth 队列卡死
  ]);
  assert.strictEqual(r.model.state, "speaking");
  assert.strictEqual(get(r.cmds, "speak").text, "第一句。");
});

test("云端播报跨锁屏仍在放：回前台不打断不重播", () => {
  const r = replay([
    ...BOOT,
    { type: "MIC_FINAL", text: "x" },
    { type: "CHAT_ACCEPTED", turnId: "t1" },
    { type: "TEXT_DELTA", text: "第一句。" },
    { type: "TURN_DONE", turnId: "t1" },
    { type: "HIDDEN" },
    { type: "VISIBLE" },                                 // 没有重播标记、没有 busy
  ]);
  assert.strictEqual(r.model.state, "speaking", "保持播放等 drain");
  assert.ok(!has(r.cmds, "stopSpeech"));
});

test("回前台时 turn 还开着：thinking 等待，不插队开麦", () => {
  const r = replay([
    ...BOOT,
    { type: "MIC_FINAL", text: "x" },
    { type: "CHAT_ACCEPTED", turnId: "t1" },
    { type: "HIDDEN" },
    { type: "VISIBLE" },
  ]);
  assert.strictEqual(r.model.state, "thinking");
  assert.ok(!has(r.cmds, "startMic"));
});

test("锁屏中发生 TURN_ERROR/CANCELLED：不放声音，回前台走恢复路径", () => {
  let r = replay([
    ...BOOT,
    { type: "MIC_FINAL", text: "x" },
    { type: "CHAT_ACCEPTED", turnId: "t1" },
    { type: "HIDDEN" },
    { type: "TURN_ERROR", message: "boom" },
  ]);
  assert.ok(!has(r.cmds, "speakOnce"), "后台不读错误播报");
  r = replay([{ type: "VISIBLE" }], r.model);
  assert.strictEqual(r.model.state, "cooldown", "回前台延迟重建聆听");
});

// ── SPEAKER_RESET（换输出引擎/音色）─────────────────────────────────────────
test("换输出引擎时正在播：立即停，本轮剩余不再开口，turn 开着回 thinking", () => {
  let r = replay([
    ...BOOT,
    { type: "MIC_FINAL", text: "x" },
    { type: "CHAT_ACCEPTED", turnId: "t1" },
    { type: "TEXT_DELTA", text: "第一句。" },
    { type: "SPEAKER_RESET" },
  ]);
  assert.strictEqual(r.model.state, "thinking");
  assert.ok(has(r.cmds, "stopSpeech"));
  r = replay([{ type: "TEXT_DELTA", text: "第二句。" }], r.model);
  assert.ok(!has(r.cmds, "speak"), "旧链已拆，本轮不再投");
});

// ── 连接层失败 ──────────────────────────────────────────────────────────────
test("SEND_FAILED → error（ws 断开时发送/取消都到不了后端）", () => {
  const r = replay([...BOOT, { type: "MIC_FINAL", text: "x" }, { type: "SEND_FAILED", message: "未连接到服务器" }]);
  assert.strictEqual(r.model.state, "error");
  assert.match(r.model.ctx.statusOverride, /未连接/);
});

// ── C3：焦点派生纯函数（车机最重要回归）────────────────────────────────────
test("C3：focusMode 全表——浮层开着一律 silent-audio 瞬态保持音，绝无持麦档", () => {
  // 刻意没有 mic-hold：AEC 采集的假"通话"周期会劫持音量键/TTS 路由，
  // 且释放麦时车机按"通话结束"自动唤醒已暂停的音乐（实测：朗读开始网易云被叫醒）
  for (const s of ["paused", "starting", "capturing", "thinking", "speaking", "cooldown"]) {
    assert.strictEqual(focusMode(s), "silent-audio", s);
  }
  // 关闭/错误：释放，外部音乐可恢复仲裁
  assert.strictEqual(focusMode("closed"), "released");
  assert.strictEqual(focusMode("error"), "released");
});

// ── W 组：唤醒词门控（待唤醒模式）──────────────────────────────────────────
const WAKE_BOOT = [
  { type: "OPEN", autoStart: true, wakeKeyword: "小克,小可" },
  { type: "MIC_STARTED" },
];

test("W1：带 wakeKeyword 进循环 → 待机聆听；非唤醒词丢弃不发送、原地续听", () => {
  let r = replay([{ type: "OPEN", autoStart: true, wakeKeyword: "小克,小可" }]);
  assert.strictEqual(r.model.state, "starting");
  assert.deepStrictEqual(r.model.ctx.wake, { keywords: ["小克", "小可"], awake: false });
  assert.ok(!r.cmds.some((c) => c.type === "armTimer" && c.tag === "wakeIdle"), "待机不armed回落计时器");
  r = replay([{ type: "MIC_STARTED" }, { type: "MIC_FINAL", text: "今天天气真好" }], r.model);
  assert.strictEqual(r.model.state, "capturing", "非唤醒词原地续听");
  assert.ok(!has(r.cmds, "chatSend"), "待机听到的话绝不发给 agent");
  assert.ok(!has(r.cmds, "stopMic"), "recognizer 继续跑，不重启");
  assert.strictEqual(r.model.ctx.statusOverride, "",
    "未命中不得顶掉待机提示语（顶掉后没有事件会清回来，提示会永久消失）");
});

test("W1：不配 wakeKeyword 行为与无此功能完全一致（wake=null 直接对话）", () => {
  const r = replay([...BOOT, { type: "MIC_FINAL", text: "你好" }]);
  assert.strictEqual(r.model.state, "thinking");
  assert.ok(has(r.cmds, "chatSend"));
});

test("W2：唤醒词命中 → chime + 停麦重开（shell 据 awake 切回所选引擎）+ 进入连续对话", () => {
  let r = replay([...WAKE_BOOT, { type: "MIC_FINAL", text: "小克" }]);
  assert.strictEqual(r.model.ctx.wake.awake, true);
  assert.ok(has(r.cmds, "chime"), "唤醒反馈提示音");
  assert.ok(has(r.cmds, "stopMic") && has(r.cmds, "startMic"), "停待机麦、按 awake 重开（引擎切换）");
  assert.ok(r.cmds.some((c) => c.type === "armTimer" && c.tag === "wakeIdle" && c.ms === 20000),
    "唤醒后聆听armed 20s 静默回落计时器");
  // 唤醒后说话走正常发送
  r = replay([{ type: "MIC_STARTED" }, { type: "MIC_FINAL", text: "今天天气" }], r.model);
  assert.strictEqual(get(r.cmds, "chatSend").text, "今天天气");
  assert.ok(r.cmds.some((c) => c.type === "clearTimer" && c.tag === "wakeIdle"), "进入对话回落计时失效");
});

test("W3：一句话直达——\"小克今天天气\" → chime + 直接发送尾巴（原文切片，标点/空格保留）", () => {
  const r = replay([...WAKE_BOOT, { type: "MIC_FINAL", text: "小克，今天天气。" }]);
  assert.strictEqual(r.model.state, "thinking");
  assert.ok(has(r.cmds, "chime"));
  assert.strictEqual(get(r.cmds, "chatSend").text, "今天天气。", "remainder 是原文切片（仅修剪头部分隔符）");
  assert.strictEqual(r.model.ctx.wake.awake, true);
});

test("W3：英文一句话直达保留空格——不能把指令压成无空格串", () => {
  const r = replay([
    { type: "OPEN", autoStart: true, wakeKeyword: "hey nano" },
    { type: "MIC_STARTED" },
    { type: "MIC_FINAL", text: "hey nano, play some music" },
  ]);
  assert.strictEqual(get(r.cmds, "chatSend").text, "play some music",
    "英文指令去空格会严重降低可懂度（playsomemusic）");
});

test("W4：唤醒后聆听静默 20s → 回落待机（停麦重开，shell 切回本地引擎）；说话中 interim 重置计时", () => {
  let r = replay([
    ...WAKE_BOOT,
    { type: "MIC_FINAL", text: "小可" },          // 唤醒
    { type: "MIC_STARTED" },
  ]);
  r = replay([{ type: "MIC_INTERIM", text: "正在说" }], r.model);
  assert.ok(r.cmds.some((c) => c.type === "armTimer" && c.tag === "wakeIdle"), "语音活动重置回落计时");
  r = replay([{ type: "TIMEOUT", tag: "wakeIdle" }], r.model);
  assert.strictEqual(r.model.ctx.wake.awake, false, "静默到点回落待机");
  assert.ok(r.cmds.some((c) => c.type === "chime" && c.variant === "sleep"), "回落待机降调提示音");
  assert.ok(has(r.cmds, "stopMic") && has(r.cmds, "startMic"), "重开麦让 shell 切回待机本地引擎");
  // 回落后再说非唤醒词：不发送
  r = replay([{ type: "MIC_STARTED" }, { type: "MIC_FINAL", text: "随便聊聊" }], r.model);
  assert.ok(!has(r.cmds, "chatSend"));
});

test("W4：对话进行中（thinking/speaking）不存在回落——wakeIdle 只在聆听态armed", () => {
  const r = replay([
    ...WAKE_BOOT,
    { type: "MIC_FINAL", text: "小克今天天气" },   // 一句话直达 → thinking
    { type: "TIMEOUT", tag: "wakeIdle" },          // 迟到的回落计时（已 clear，防御性触发）
  ]);
  assert.strictEqual(r.model.state, "thinking", "对话中迟到的 wakeIdle 不得回落");
  assert.strictEqual(r.model.ctx.wake.awake, true);
});

test("W4：turn 读完续听重新armed回落计时（连续对话窗口随轮次滚动）", () => {
  const r = replay([
    ...WAKE_BOOT,
    { type: "MIC_FINAL", text: "小克今天天气" },
    { type: "CHAT_ACCEPTED", turnId: "t1" },
    { type: "TEXT_DELTA", text: "晴。" },
    { type: "TURN_DONE", turnId: "t1" },
    { type: "SPEAK_AUDIBLE" },
    { type: "SPEAK_DRAINED" },                     // cooldown
    { type: "TIMEOUT", tag: "cooldown" },          // → 续听
  ]);
  assert.strictEqual(r.model.state, "starting");
  assert.ok(r.cmds.some((c) => c.type === "armTimer" && c.tag === "wakeIdle"), "续听时窗口重新armed");
});

test("W4：wakeIdle 在所有离开聆听的路径上清理（外部 CHAT_ACCEPTED / 回前台恢复）", () => {
  // 外部并行发消息抢占 awake 聆听 → 进入对话，回落计时必须清
  let r = replay([
    ...WAKE_BOOT,
    { type: "MIC_FINAL", text: "小克" },
    { type: "MIC_STARTED" },
    { type: "CHAT_ACCEPTED", turnId: "ext" },
  ]);
  assert.ok(r.cmds.some((c) => c.type === "clearTimer" && c.tag === "wakeIdle"));
  // 锁屏回前台的恢复路径重建聆听 → 旧回落计时清掉（恢复后续听会重新armed）
  r = replay([
    ...WAKE_BOOT,
    { type: "MIC_FINAL", text: "小克" },
    { type: "MIC_STARTED" },
    { type: "HIDDEN" },
    { type: "VISIBLE" },
  ]);
  assert.ok(r.cmds.some((c) => c.type === "clearTimer" && c.tag === "wakeIdle"));
});

test("W5：待机点屏 = 手动唤醒（兜底唤醒词识别不出）；TOGGLE 暂停再恢复 → 重新待机", () => {
  let r = replay([...WAKE_BOOT, { type: "TAP" }]);
  assert.strictEqual(r.model.ctx.wake.awake, true, "点屏直接唤醒");
  assert.ok(has(r.cmds, "chime") && has(r.cmds, "startMic"));
  // 暂停 → 恢复：重新待机
  r = replay([{ type: "TOGGLE" }], r.model);
  assert.strictEqual(r.model.state, "paused");
  assert.ok(r.cmds.some((c) => c.type === "clearTimer" && c.tag === "wakeIdle"));
  r = replay([{ type: "TOGGLE", wakeKeyword: "小克,小可" }], r.model);
  assert.strictEqual(r.model.ctx.wake.awake, false, "恢复后重新要求唤醒");
});

test("W6：matchWake 纯函数——标点/大小写/remainder 原文切片/英文唤醒词", () => {
  const kws = ["小克", "Hey Nano"];
  assert.deepStrictEqual(matchWake("小克", kws), { matched: true, remainder: "" });
  assert.deepStrictEqual(matchWake("小克，今天天气！", kws), { matched: true, remainder: "今天天气！" });
  assert.deepStrictEqual(matchWake("hey nano, play music", kws), { matched: true, remainder: "play music" });
  assert.strictEqual(matchWake("你好世界", kws).matched, false);
  assert.strictEqual(matchWake("", kws).matched, false);
  assert.strictEqual(matchWake("前缀小克后缀", kws).remainder, "后缀", "句中命中取关键词之后的尾巴");
});

test("W6：拼音同音匹配——ASR 写成哪个同音字都命中，remainder 保留原文", () => {
  const kws = ["小克"];
  // ASR 常见同音误写全部等价（xiao-ke）
  for (const variant of ["小课", "小柯", "小科", "小可", "晓客", "笑克"]) {
    assert.strictEqual(matchWake(variant, kws).matched, true, variant);
  }
  // 一句话直达的 remainder 取原文切片（不是拼音、不丢空格标点）
  assert.deepStrictEqual(matchWake("小课，今天天气", kws), { matched: true, remainder: "今天天气" });
  // 声母/韵母不同不命中（拼音等价类，不是模糊匹配）
  assert.strictEqual(matchWake("小哥", kws).matched, false, "ge≠ke");
  assert.strictEqual(matchWake("校长", kws).matched, false, "zhang≠ke");
  // 多音字两侧同映射：唤醒词本身含多音字也一致命中
  assert.strictEqual(matchWake("银行", ["银行"]).matched, true);
});

// ── 杂项防御 ────────────────────────────────────────────────────────────────
test("迟到/乱序事件不越权：closed 态聊天事件、capturing 态 SPEAK_DRAINED 均忽略", () => {
  let r = replay([{ type: "TEXT_DELTA", text: "x" }, { type: "SPEAK_DRAINED" }]);
  assert.strictEqual(r.model.state, "closed");
  r = replay([...BOOT, { type: "SPEAK_DRAINED" }]);
  assert.strictEqual(r.model.state, "capturing");
});

test("HIDDEN 幂等；VISIBLE 未 hidden 时忽略", () => {
  let r = replay([...BOOT, { type: "HIDDEN" }, { type: "HIDDEN" }]);
  assert.strictEqual(r.model.state, "capturing");
  r = replay([...BOOT, { type: "VISIBLE" }]);
  assert.strictEqual(r.model.state, "capturing");
});
