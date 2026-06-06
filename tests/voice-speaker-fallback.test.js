/* 合成回退链组合器回归（旧实现 5 个 TTS 标志的收敛点）：
 * 【B3】降级会话内记忆——之后每轮直接从降级层级起
 * 【B5】零发声失败把已投文本全量重投下一级；已出声不重播
 * 【B4】上游已 end() 时降级重投要补 end（否则永不 complete 卡死朗读中）
 * 【B1】云端级经播放器 markEnded→drain 才算读完；本地级 onCompleted 即读完
 * 【B8】每次降级 onFallback 上报（UI 反映生效引擎）
 */
const test = require("node:test");
const assert = require("node:assert");
const createFallbackSpeaker = require("../nano_openclaw/gateway/webui/static/voice-speaker-fallback.js");
const createLocalSpeaker = require("../nano_openclaw/gateway/webui/static/voice-speaker-local.js");
const createVoicePcmPlayer = require("../nano_openclaw/gateway/webui/static/voice-pcm-player.js");

// 可控的假引擎：记录调用，错误/音频/完成由测试触发
function makeFakeEngine(name) {
  const e = {
    name, begun: 0, pushed: [], ended: 0, aborted: 0, cb: null,
    begin() { this.begun++; },
    push(t) { this.pushed.push(t); },
    end() { this.ended++; },
    abort() { this.aborted++; },
  };
  return e;
}

function makeFakePlayer() {
  return {
    enqueued: [], markEndedCount: 0, stopped: 0, unlocked: 0, disposed: 0, active: false,
    onDrained: null, fireAudible: null, fireError: null,   // 由 createPlayer 注入的回调
    enqueue(b) { this.enqueued.push(b); },
    markEnded() { this.markEndedCount++; },
    stop() { this.stopped++; },
    unlock() { this.unlocked++; },
    dispose() { this.disposed++; },
    isActive() { return this.active; },
    drain() { this.onDrained(); },
  };
}

function makeChain(levelNames) {
  const fakes = {};
  const player = makeFakePlayer();
  const out = { audible: 0, drained: 0, fallbacks: [] };
  const levels = levelNames.map((name) => ({
    name,
    usesPlayer: name !== "local",
    create(cb) {
      const e = makeFakeEngine(name);
      e.cb = cb;
      fakes[name] = e;
      return e;
    },
  }));
  const sp = createFallbackSpeaker({
    levels,
    createPlayer: (cb) => {
      player.onDrained = cb.onDrained;
      player.fireAudible = cb.onAudible;
      player.fireError = cb.onError;
      return player;
    },
    onAudible: () => out.audible++,
    onDrained: () => out.drained++,
    onFallback: (name, reason) => out.fallbacks.push([name, reason]),
    log: () => {},
  });
  return { sp, fakes, player, out };
}

test("正常链路：push 走当前级；出声由播放器上报（字节到达不算）；completed→markEnded→drain→onDrained", () => {
  const { sp, fakes, player, out } = makeChain(["aliyun-flowing", "aliyun-rest", "local"]);
  sp.begin();
  sp.push("第一句。");
  sp.push("第二句。");
  const fl = fakes["aliyun-flowing"];
  assert.deepStrictEqual(fl.pushed, ["第一句。", "第二句。"]);
  fl.cb.onAudio(new ArrayBuffer(2));
  fl.cb.onAudio(new ArrayBuffer(2));
  assert.strictEqual(out.audible, 0, "字节到达≠出过声：ctx 起不来时字节照样流入但全程无声");
  assert.strictEqual(player.enqueued.length, 2);
  player.fireAudible();                     // 播放器首个音源真正排程成功
  assert.strictEqual(out.audible, 1, "出声判定来自播放器 onAudible");
  sp.end();
  assert.strictEqual(fl.ended, 1);
  fl.cb.onCompleted();
  assert.strictEqual(player.markEndedCount, 1, "云端级读完信号经播放器 gate");
  assert.strictEqual(out.drained, 0, "播放器没 drain 前不得上报读完");
  player.drain();
  assert.strictEqual(out.drained, 1);
});

test("B5+B4：零发声失败 → 全量重投下一级并补 end；onFallback 上报", () => {
  const { sp, fakes, out } = makeChain(["aliyun-flowing", "aliyun-rest", "local"]);
  sp.begin();
  sp.push("一。");
  sp.push("二。");
  sp.end();                                 // turn.done 先到【B4 场景】
  fakes["aliyun-flowing"].cb.onError("aliyun-task-failed", "未开通商用版");
  const rest = fakes["aliyun-rest"];
  assert.ok(rest, "应已建下一级引擎");
  assert.strictEqual(rest.begun, 1);
  assert.deepStrictEqual(rest.pushed, ["一。", "二。"], "零发声：已投文本全量重投");
  assert.strictEqual(rest.ended, 1, "上游已 end → 对新引擎补 end，否则永不 complete");
  assert.deepStrictEqual(out.fallbacks, [["aliyun-rest", "aliyun-task-failed: 未开通商用版"]]);
});

test("B3：降级会话内记忆——下一轮 begin 直接从降级层级起，不再先试失败级", () => {
  const { sp, fakes } = makeChain(["aliyun-flowing", "aliyun-rest", "local"]);
  sp.begin();
  sp.push("一。");
  fakes["aliyun-flowing"].cb.onError("ws", "连接失败");
  // 第二轮
  sp.begin();
  sp.push("二。");
  assert.strictEqual(fakes["aliyun-flowing"].begun, 1, "失败级不再重试");
  assert.strictEqual(fakes["aliyun-rest"].begun, 2, "新轮直接从 RESTful 起");
  assert.deepStrictEqual(fakes["aliyun-rest"].pushed, ["一。", "二。"]);
});

test("B5：已出过声（播放器确认）的失败不重播（避免重读），按读完收尾", () => {
  const { sp, fakes, player, out } = makeChain(["aliyun-flowing", "aliyun-rest", "local"]);
  sp.begin();
  sp.push("一。");
  const fl = fakes["aliyun-flowing"];
  fl.cb.onAudio(new ArrayBuffer(2));
  player.fireAudible();                     // 播放器确认真正出过声
  fl.cb.onError("ws", "中途断开");
  assert.strictEqual((fakes["aliyun-rest"] && fakes["aliyun-rest"].begun) || 0, 0, "不得重投（会重读）");
  assert.strictEqual(player.markEndedCount, 1, "按读完收尾，等播放器 drain");
  assert.strictEqual(out.fallbacks.length, 1, "降级记忆仍生效（下轮从 RESTful 起）");
});

test("B5：字节到达但播放器没出声（ctx 挂死）→ 引擎报错时仍算零发声，全量重投下一级", () => {
  const { sp, fakes } = makeChain(["aliyun-flowing", "aliyun-rest", "local"]);
  sp.begin();
  sp.push("一。");
  sp.end();
  const fl = fakes["aliyun-flowing"];
  fl.cb.onAudio(new ArrayBuffer(2));        // 字节进了挂起的播放器，无声
  fl.cb.onError("aliyun-task-failed", "中途失败");
  const rest = fakes["aliyun-rest"];
  assert.deepStrictEqual(rest.pushed, ["一。"], "audible-but-silent 不得丢文：必须重投");
  assert.strictEqual(rest.ended, 1);
});

test("播放器自身故障升级为当前级失败：零发声 → 降级重投，最终可落到不依赖 Web Audio 的本地级", () => {
  const { sp, fakes, player, out } = makeChain(["aliyun-flowing", "aliyun-rest", "local"]);
  sp.begin();
  sp.push("一。");
  sp.end();
  fakes["aliyun-flowing"].cb.onAudio(new ArrayBuffer(2));   // 触发播放器懒建
  player.fireError("audio-context", "创建 AudioContext 失败");
  assert.deepStrictEqual(out.fallbacks.map(([n]) => n), ["aliyun-rest"], "播放器故障必须触发降级，不能只 log");
  assert.deepStrictEqual(fakes["aliyun-rest"].pushed, ["一。"], "零发声全量重投");
  // RESTful 同样依赖坏掉的播放器 → 再报错 → 落到本地级
  player.fireError("source", "创建播放源失败");
  assert.deepStrictEqual(fakes["local"].pushed, ["一。"], "链尾本地级不依赖 Web Audio，完成补读");
  assert.strictEqual(fakes["local"].ended, 1);
});

test("级联降级：flowing→rest→local，零发声逐级全量重投", () => {
  const { sp, fakes, out } = makeChain(["aliyun-flowing", "aliyun-rest", "local"]);
  sp.begin();
  sp.push("一。");
  sp.end();
  fakes["aliyun-flowing"].cb.onError("task", "f1");
  fakes["aliyun-rest"].cb.onError("restful", "f2");
  const local = fakes["local"];
  assert.deepStrictEqual(local.pushed, ["一。"], "应一路降到本地补读");
  assert.strictEqual(local.ended, 1);
  assert.deepStrictEqual(out.fallbacks.map(([n]) => n), ["aliyun-rest", "local"]);
});

test("回退链末端失败：解卡收尾（本地级直接 onDrained），不停留在朗读中", () => {
  const { sp, fakes, out } = makeChain(["local"]);
  sp.begin();
  sp.push("一。");
  fakes["local"].cb.onError("unsupported", "synth 不可用");
  assert.strictEqual(out.drained, 1, "末端失败必须解卡");
});

test("本地级读完：onCompleted 直接 onDrained（不经播放器）；onAudible 经 cb.onAudible", () => {
  const { sp, fakes, player, out } = makeChain(["local"]);
  sp.begin();
  sp.push("一。");
  fakes["local"].cb.onAudible();
  assert.strictEqual(out.audible, 1);
  fakes["local"].cb.onCompleted();
  assert.strictEqual(out.drained, 1);
  assert.strictEqual(player.markEndedCount, 0);
});

test("abort 清已投文本：之后降级无可重投（打断后不得复读旧文本）", () => {
  const { sp, fakes } = makeChain(["aliyun-flowing", "aliyun-rest", "local"]);
  sp.begin();
  sp.push("一。");
  sp.abort();
  fakes["aliyun-flowing"].cb.onError("ws", "断开");
  assert.strictEqual((fakes["aliyun-rest"] && fakes["aliyun-rest"].pushed.length) || 0, 0);
});

test("降级后被换下引擎的迟到事件一律忽略", () => {
  const { sp, fakes, player, out } = makeChain(["aliyun-flowing", "aliyun-rest", "local"]);
  sp.begin();
  sp.push("一。");
  const fl = fakes["aliyun-flowing"];
  fl.cb.onError("task", "f1");              // 已降到 rest
  fl.cb.onAudio(new ArrayBuffer(2));        // 旧 ws 残帧
  fl.cb.onCompleted();
  fl.cb.onError("task", "迟到失败");
  assert.strictEqual(player.enqueued.length, 0, "旧引擎残帧不得入播放器");
  assert.strictEqual(out.fallbacks.length, 1, "迟到失败不得再次降级");
});

test("unlock：含云端级才建播放器并解锁；纯本地链不建 ctx", () => {
  const a = makeChain(["aliyun-flowing", "local"]);
  a.sp.unlock();
  assert.strictEqual(a.player.unlocked, 1);
  const b = makeChain(["local"]);
  b.sp.unlock();
  assert.strictEqual(b.player.unlocked, 0, "纯本地链不必占 AudioContext");
});

test("effectiveName / busy / dispose", () => {
  const { sp, fakes, player } = makeChain(["aliyun-flowing", "aliyun-rest", "local"]);
  assert.strictEqual(sp.effectiveName(), "aliyun-flowing");
  sp.begin();
  sp.push("一。");
  fakes["aliyun-flowing"].cb.onError("task", "f");
  assert.strictEqual(sp.effectiveName(), "aliyun-rest");
  fakes["aliyun-rest"].cb.onAudio(new ArrayBuffer(2));   // 触发播放器懒建
  player.active = true;
  assert.strictEqual(sp.busy(), true, "播放器有声在播即 busy");
  sp.dispose();
  assert.strictEqual(player.disposed, 1);
});

// ── createPlayer 端口契约：键面 + shell 全量转发 ────────────────────────────
test("契约：createPlayer 收到的 cb 必须含全部四个回调（端口面变更必须同步本测试与 shell）", () => {
  let captured = null;
  const sp = createFallbackSpeaker({
    levels: [{ name: "aliyun-flowing", usesPlayer: true, create: (cb) => Object.assign(makeFakeEngine("x"), { cb }) }],
    createPlayer: (cb) => { captured = cb; return makeFakePlayer(); },
    onAudible: () => {}, onDrained: () => {}, onFallback: () => {}, log: () => {},
  });
  sp.unlock();   // 触发播放器懒建
  for (const key of ["onDrained", "onAudible", "onInterrupted", "onError"]) {
    assert.strictEqual(typeof captured[key], "function", `createPlayer cb 缺 ${key}`);
  }
});

test("契约：shell 的播放器工厂必须整体展开 cb（防逐键枚举漏转发把修复丢成死代码）", () => {
  // voice-shell.js 引用 window/document，node 装载不了——退而求其次做 source 级断言：
  // 工厂必须以 Object.assign({...}, cb) 形式全量转发。曾因按过期契约只转发
  // onDrained/onError，onAudible（零发声判定）与 onInterrupted（解卡掐引擎）
  // 在生产路径上整段失效而测试全绿。
  const fs = require("node:fs");
  const path = require("node:path");
  const src = fs.readFileSync(
    path.join(__dirname, "../nano_openclaw/gateway/webui/static/voice-shell.js"), "utf8");
  assert.match(src, /createPlayer:\s*\(cb\)\s*=>\s*window\.createVoicePcmPlayer\(Object\.assign\([^;]*\}\s*,\s*cb\)\)/,
    "shell 的 createPlayer 必须 Object.assign({...}, cb) 整体展开转发");
});

// ── 集成：真实 pcm-player 接进回退链（closed-ctx 解卡路径）──────────────────
function makeRealCtx() {
  return {
    state: "running", currentTime: 0, destination: {}, sources: [],
    resume() {}, close() { this.state = "closed"; },
    createBuffer(ch, len, rate) {
      const data = new Float32Array(len);
      return { duration: len / rate, getChannelData: () => data };
    },
    createBufferSource() {
      const src = { buffer: null, connect() {}, start() {}, stop() {}, disconnect() {}, onended: null };
      this.sources.push(src);
      return src;
    },
  };
}
function pcm(n) { return new ArrayBuffer(n * 2); }

test("集成（player→fallback）：closed-ctx 解卡先掐断引擎再上报读完——mic 重开时无尾部帧出声", () => {
  // 场景：turn.done 已到（已 end）但 SynthesisCompleted 未到，引擎仍在流尾部帧；
  // 锁屏把 ctx 杀成 closed → 下一帧 enqueue 触发解卡。必须先 abort 引擎再 drained，
  // 否则 core 走 cooldown→listening 开麦时引擎还在向重建的 ctx 出声（自回声）。
  const order = [];
  const fakes = {};
  const ctxs = [makeRealCtx(), makeRealCtx()];
  let ctxIdx = 0;
  const levels = ["aliyun-flowing", "aliyun-rest", "local"].map((name) => ({
    name,
    usesPlayer: name !== "local",
    create(cb) {
      const e = makeFakeEngine(name);
      const origAbort = e.abort.bind(e);
      e.abort = () => { order.push("abort:" + name); origAbort(); };
      e.cb = cb;
      fakes[name] = e;
      return e;
    },
  }));
  const sp = createFallbackSpeaker({
    levels,
    createPlayer: (cb) => createVoicePcmPlayer({
      sampleRate: 16000,
      AudioCtxImpl: function () { return ctxs[ctxIdx++]; },
      onDrained: cb.onDrained,
      onAudible: cb.onAudible,
      onInterrupted: cb.onInterrupted,
      onError: cb.onError,
    }),
    onAudible: () => {},
    onDrained: () => order.push("drained"),
    onFallback: () => {},
    log: () => {},
  });
  sp.begin();
  sp.push("一句长回复。");
  sp.end();                                  // turn.done：StopSynthesis 已发，Completed 未到
  const fl = fakes["aliyun-flowing"];
  fl.cb.onAudio(pcm(160));                   // 正常出声中（ctx1）
  ctxs[0].close();                           // 锁屏杀 ctx
  fl.cb.onAudio(pcm(160));                   // 引擎仍在流尾部帧 → enqueue 触发解卡
  assert.deepStrictEqual(order.filter((x) => x === "drained").length, 1, "解卡只 drain 一次");
  const abortIdx = order.indexOf("abort:aliyun-flowing");
  const drainedIdx = order.indexOf("drained");
  assert.ok(abortIdx >= 0, "解卡必须掐断当前引擎");
  assert.ok(abortIdx < drainedIdx, "先掐引擎、后上报读完——开麦时已无音频在流");
});

test("多 turn 生命周期：onAudible 每轮重新上报（audibleFired 随 begin/abort 复位）", () => {
  const { sp, fakes, player, out } = makeChain(["aliyun-flowing", "aliyun-rest", "local"]);
  // turn 1
  sp.begin();
  sp.push("一。");
  fakes["aliyun-flowing"].cb.onAudio(new ArrayBuffer(2));
  player.fireAudible();
  assert.strictEqual(out.audible, 1);
  sp.end();
  fakes["aliyun-flowing"].cb.onCompleted();
  player.drain();
  // turn 2（CHAT_ACCEPTED 路径：stopSpeech→abort 再 begin）
  sp.abort();
  sp.begin();
  sp.push("二。");
  player.fireAudible();
  assert.strictEqual(out.audible, 2, "新一轮必须重新派发 SPEAK_AUDIBLE——否则 anyAudio=false 跳过冷却，TTS 尾音被麦采集");
});

// ── 本地引擎（speechSynthesis 适配器）──────────────────────────────────────
function makeFakeSynth() {
  return {
    queue: [], cancelled: 0, speaking: false, pending: false, paused: false, resumed: 0,
    speak(u) { this.queue.push(u); },
    cancel() { this.cancelled++; this.queue = []; },
    resume() { this.resumed++; },
  };
}
function FakeUtterance(text) { this.text = text; this.rate = 0; this.lang = ""; }

test("local：utterance 串行队列——一条 onend 才放下一条；end 后排空 onCompleted 一次", () => {
  const synth = makeFakeSynth();
  const out = { audible: 0, completed: 0 };
  const sp = createLocalSpeaker({
    synth, UtteranceImpl: FakeUtterance,
    onAudible: () => out.audible++,
    onCompleted: () => out.completed++,
  });
  sp.begin();
  sp.push("一。");
  sp.push("二。");
  assert.strictEqual(synth.queue.length, 1, "串行：第二条等第一条结束");
  synth.queue[0].onstart();
  assert.strictEqual(out.audible, 1);
  sp.end();
  synth.queue[0].onend();
  assert.strictEqual(synth.queue.length, 2);
  synth.queue[1].onstart();
  assert.strictEqual(out.audible, 1, "onAudible 仅一次");
  synth.queue[1].onend();
  assert.strictEqual(out.completed, 1);
});

test("local：单条 onerror 跳过不卡队列；speak 抛错也推进", () => {
  const synth = makeFakeSynth();
  let throwNext = false;
  const origSpeak = synth.speak.bind(synth);
  synth.speak = (u) => { if (throwNext) { throwNext = false; throw new Error("boom"); } origSpeak(u); };
  const out = { completed: 0 };
  const sp = createLocalSpeaker({ synth, UtteranceImpl: FakeUtterance, onCompleted: () => out.completed++ });
  sp.begin();
  sp.push("一。");
  throwNext = true;
  sp.push("二。");      // 排队
  sp.push("三。");
  sp.end();
  synth.queue[0].onerror();           // 第一条失败 → 第二条 speak 抛错跳过 → 第三条
  assert.strictEqual(synth.queue.length, 2, "抛错的第二条被跳过，第三条已 speak");
  synth.queue[1].onend();
  assert.strictEqual(out.completed, 1);
});

test("local：未选音色 lang 固定 zh-CN；选了音色用其 lang；rate 1.05", () => {
  const synth = makeFakeSynth();
  let voice = null;
  const sp = createLocalSpeaker({ synth, UtteranceImpl: FakeUtterance, getVoice: () => voice });
  sp.begin();
  sp.push("一。");
  assert.strictEqual(synth.queue[0].lang, "zh-CN");
  assert.strictEqual(synth.queue[0].rate, 1.05);
  synth.queue[0].onend();
  voice = { lang: "zh-TW", name: "x" };
  sp.push("二。");
  assert.strictEqual(synth.queue[1].lang, "zh-TW");
  assert.strictEqual(synth.queue[1].voice, voice);
});

test("local：busy 暴露 synth.speaking/pending/paused（锁屏卡队列检测）；abort cancel 清场", () => {
  const synth = makeFakeSynth();
  const sp = createLocalSpeaker({ synth, UtteranceImpl: FakeUtterance });
  assert.strictEqual(sp.busy(), false);
  synth.paused = true;
  assert.strictEqual(sp.busy(), true, "synth 挂起队列也算 busy（回前台要重播）");
  synth.paused = false;
  sp.begin();
  sp.push("一。");
  assert.strictEqual(sp.busy(), true);
  sp.abort();
  assert.ok(synth.cancelled >= 1);
  assert.strictEqual(sp.busy(), false);
});

test("local：端口契约——onError 即终态，之后 end() 不得再补 onCompleted（防消费者双推进）", () => {
  const out = { completed: 0, errors: 0 };
  const sp = createLocalSpeaker({
    synth: null, UtteranceImpl: null,   // unsupported 环境
    onCompleted: () => out.completed++,
    onError: () => out.errors++,
  });
  sp.begin();
  sp.push("一。");
  assert.strictEqual(out.errors, 1);
  sp.end();
  assert.strictEqual(out.completed, 0, "已报错的轮次不得再 completed");
});

test("local：sayOnce 先 cancel 清场再说，done 回调 onend/onerror 都触发", () => {
  const synth = makeFakeSynth();
  const sp = createLocalSpeaker({ synth, UtteranceImpl: FakeUtterance });
  let done = 0;
  sp.sayOnce("出错了", () => done++);
  assert.ok(synth.cancelled >= 1);
  synth.queue[0].onend();
  assert.strictEqual(done, 1);
});
