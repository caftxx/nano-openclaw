/* 语音免提核心状态机 —— 纯函数，零 DOM / 零 IO / 零计时器。
 *
 *   transition(model, event) -> { state, ctx, commands[] }
 *
 * 副作用全部以 command 形式返回，由 voice-shell.js 解释执行；外部发生的一切
 * （识别回调 / 合成回调 / 聊天事件 / 点击 / 可见性 / 计时器到点）统一归一成事件
 * 喂进来。这让所有竞态都能用「事件序列回放」做纯单测（tests/voice-core.test.js）。
 *
 * ── 状态 ─────────────────────────────────────────────────────────────────────
 *   closed     浮层未开
 *   paused     浮层开着、免提未激活（含深链 /voice 进入）
 *   starting   已命令开麦、等识别引擎确认（自带超时重建【A4】，窗口由引擎申报【A5】）
 *   capturing  正在聆听
 *   thinking   已发送 / turn 流式中、且当前无播报
 *   speaking   有声音在播（含云端合成排队播放、本地 synth、错误短播报）
 *   cooldown   定时等待后再开麦：读完后 500ms 防外放尾音回采【D3】；
 *              回前台 1200ms 等浏览器麦克风/语音服务恢复【D1】
 *   error      不可恢复错误（HTTPS 硬阻断 / 前台拒麦 / 连接丢失）
 *
 * ── 关键正交维度（不是状态、是 ctx 字段）────────────────────────────────────
 *   ctx.hidden        页面在后台/锁屏【A1/D2】：后台丢弃识别器，避免移动 Chrome
 *                     把 WebSpeech/getUserMedia 服务挂成半死；后台不开新麦、不起播报；
 *                     回前台重建识别器；该读未读的内容记 resumeReplay，
 *                     回前台全文重播一次且只一次。
 *   ctx.turn          当前 turn：open（流式中）/ muted（本轮禁播报：暂停态接的 turn、
 *                     或用户点屏打断【E1】）/ sentUpTo（按句末标点切句的进度游标）/
 *                     pushed（本轮是否投过合成）/ anyAudio（本轮是否出过声）/
 *                     cancelRequested（已发 turn.cancel，防重复刷【E2】）
 *   ctx.wake          唤醒词门控【W1~W5】（config 配 wakeWord 才启用）：!awake = 待唤醒
 *                     （识别结果只做关键词匹配不发送，待机用本地 Web Speech 免费听）；
 *                     命中唤醒（或点屏手动唤醒）→ chime + 切回所选引擎连续对话；
 *                     聆听中静默 20s（wakeIdle 计时器）回落待机
 *
 * 音频焦点不在命令列里：它是状态的派生纯函数 focusMode(state)【C3】，shell 每次
 * 迁移后 diff 换轨——浮层开着即持静音保持音的瞬态焦点，closed/error 释放。任何
 * 阶段都不做 AEC 采集/持麦（通信模式 = 假"来电"，其进入/退出会劫持音量键与
 * TTS 路由，并触发车机在"通话结束"时自动唤醒已暂停的音乐，详见 focusMode 注释）。
 *
 * UMD：node --test 直接 require。
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory(require("./voice-pinyin.js"));
  else root.VoiceCore = factory(root.VoicePinyin);
})(typeof self !== "undefined" ? self : this, function (pinyin) {
  "use strict";

  var SENTENCE_END = /[。！？!?；;\n]/;
  var ACTIVE_STATES = { starting: 1, capturing: 1, thinking: 1, speaking: 1, cooldown: 1 };
  var WAKE_IDLE_MS = 20000;   // 唤醒后连续对话窗口：聆听中静默 20s 回落待唤醒
  var WAKE_SEP = /[\s。，、！？!?.,;；:：'"""''()（）]/;   // 唤醒匹配忽略的分隔符（单字符判定，无 g 防 lastIndex 状态）

  function isActive(state) { return Boolean(ACTIVE_STATES[state]); }

  // 唤醒词匹配（纯函数）——**按拼音等价类**而非字面量：ASR 写成哪个同音字无法控制
  // （"小克"常被写成 小课/小柯/小科），逐字取无声调主读音音节比较，同音即命中。
  // 多音字两侧用同一份主读音映射（voice-pinyin.js），即使取的不是语境读音也一致命中。
  // 分隔符（空白/中英标点）不参与比较；非常用汉字/非汉字按小写原字符比较（英文唤醒词照常）。
  // remainder 为关键词之后的**原文切片**（仅修剪头部分隔符）：token 记录原文索引，
  // 空格与内部标点原样保留——"hey nano, play some music" 直达发送 "play some music"
  // 而不是被去空格的 "playsomemusic"。
  function wakeTokens(text) {
    var raw = String(text || "");
    var toks = [];
    for (var i = 0; i < raw.length; i++) {
      var ch = raw[i];
      if (WAKE_SEP.test(ch)) continue;   // 分隔符不是 token，但原文索引语义保留
      var sy = pinyin && pinyin.syllableOf ? pinyin.syllableOf(ch) : null;
      toks.push({ pos: i, key: sy || ch.toLowerCase() });
    }
    return { raw: raw, toks: toks };
  }
  function matchWake(text, keywords) {
    var t = wakeTokens(text);
    for (var k = 0; k < (keywords || []).length; k++) {
      var kw = wakeTokens(keywords[k]).toks;
      if (!kw.length) continue;
      for (var i = 0; i + kw.length <= t.toks.length; i++) {
        var hit = true;
        for (var j = 0; j < kw.length; j++) {
          if (t.toks[i + j].key !== kw[j].key) { hit = false; break; }
        }
        if (hit) {
          // 关键词末 token 之后的原文，修剪头部分隔符（"小克，今天" → "今天"）
          var start = t.toks[i + kw.length - 1].pos + 1;
          while (start < t.raw.length && WAKE_SEP.test(t.raw[start])) start++;
          return { matched: true, remainder: t.raw.slice(start).trim() };
        }
      }
    }
    return { matched: false, remainder: "" };
  }

  // 解析 config 下发的唤醒词（逗号分隔变体）；空 → null（不启用待唤醒，行为与无此功能一致）。
  function makeWake(keyword, awake) {
    var parts = String(keyword || "").split(",").map(function (s) { return s.trim(); }).filter(Boolean);
    return parts.length ? { keywords: parts, awake: Boolean(awake) } : null;
  }

  function inStandby(ctx) { return Boolean(ctx.wake && !ctx.wake.awake); }

  // 唤醒动作的公共序列（关键词命中 / 待机点屏手动唤醒共用）：
  // 提示音 + 停待机麦——后续由调用方决定续听（startMic 按 awake 切回所选引擎）还是直达发送。
  function wakeUpCmds(ctx, cmds) {
    ctx.wake.awake = true;
    cmds.push({ type: "chime" });
    cmds.push({ type: "stopMic" });
    cmds.push({ type: "clearTimer", tag: "start" });
  }

  // 音频焦点派生【C3】：浮层开着（含 paused）一律持 4s 近静音保持音的瞬态焦点，
  // closed/error 释放。刻意**不存在持麦档**：曾用占位麦（AEC 采集）压外部音乐，但
  // AEC 采集让 Android 进通信模式 = 系统层面"来电话"——音量键变通话音量、TTS 被
  // 路由到手机；更要命的是释放麦时"通话结束"，车机/蓝牙栈按通话结束的标准行为
  // 自动恢复媒体播放（AVRCP PLAY），把用户手动暂停的音乐在朗读开始的瞬间叫醒。
  // 瞬态保持音对实际用法足够：用户进语音页前音乐已暂停，没人发 GAIN 它就不会醒；
  // 正在播的音乐则只被压低（may-duck），不强行暂停。
  function focusMode(state) {
    if (state === "closed" || state === "error") return "released";
    return "silent-audio";
  }

  function createInitialModel() {
    return {
      state: "closed",
      ctx: {
        hidden: false,
        hardBlock: null,        // "https" | "no-sr" | null（OPEN 时注入）
        statusOverride: "",     // 状态行临时文案（识别中：xxx 等），随迁移清空
        turn: null,
        resumeReplay: false,    // 锁屏期间有该读未读的内容【D2】
        // 唤醒词门控（config 配了 wakeWord 才非 null）【W1】：
        //   { keywords:[变体], awake:false }——!awake = 待唤醒（听到的话只做关键词
        //   匹配不发送）；awake = 正常连续对话。待机/唤醒各用哪个识别引擎由 shell
        //   按本字段派生（待机本地 Web Speech 免费听关键词，唤醒切回所选引擎）。
        wake: null,
      },
    };
  }

  // ── 内部小工具（全部纯函数，操作浅拷贝）────────────────────────────────────
  function res(state, ctx, commands) { return { state: state, ctx: ctx, commands: commands || [] }; }

  function newTurn(id, muted, ssml) {
    return {
      id: id || "",
      open: true,
      text: "",
      sentUpTo: 0,        // 已投合成的文本进度（按句末标点切）
      muted: Boolean(muted),
      ssml: Boolean(ssml),
      cancelRequested: false,
      anyAudio: false,
      pushed: false,
    };
  }

  // 从 turn.text[sentUpTo..] 切出「到最后一个句末标点为止」的整句块；没有整句返回 null。
  function readyChunk(turn) {
    var rest = turn.text.slice(turn.sentUpTo);
    if (!rest) return null;
    var lastEnd = -1;
    for (var i = rest.length - 1; i >= 0; i--) {
      if (SENTENCE_END.test(rest[i])) { lastEnd = i; break; }
    }
    if (lastEnd < 0) return null;
    return { text: rest.slice(0, lastEnd + 1).trim(), advance: lastEnd + 1 };
  }

  // 给 turn 投一段合成文本：首段补 speakerBegin（懒开流——muted/paused 的 turn
  // 永远不该开 TTS 连接）。
  function speakCmds(turn, text, cmds) {
    if (!turn.pushed) { cmds.push({ type: "speakerBegin" }); turn.pushed = true; }
    cmds.push({ type: "speak", text: text });
  }

  // 进入 starting：开麦 + 启动看门狗计时器（ms 由 shell 按引擎 startTimeoutMs 填）【A4/A5】。
  // 唤醒后的连续对话窗口【W4】：awake 聆听时叠加静默回落计时器，到点回待唤醒。
  function startListening(ctx, cmds) {
    cmds.push({ type: "startMic" });
    cmds.push({ type: "armTimer", tag: "start", ms: null });
    if (ctx.wake && ctx.wake.awake) cmds.push({ type: "armTimer", tag: "wakeIdle", ms: WAKE_IDLE_MS });
    return "starting";
  }

  // turn 收尾后的去向：本轮出过声 → 冷却 500ms 防尾音回采【D3】；没出过声立即开麦。
  function resumeAfterTurn(ctx, turn, cmds) {
    if (turn && turn.anyAudio) {
      cmds.push({ type: "armTimer", tag: "cooldown", ms: 500 });
      return "cooldown";
    }
    return startListening(ctx, cmds);
  }

  function turnIsOpen(ctx, event) {
    return Boolean((ctx.turn && ctx.turn.open) || (event && event.externalTurnOpen));
  }

  // ── 主迁移函数 ──────────────────────────────────────────────────────────────
  function transition(model, event) {
    var state = model.state;
    var ctx = Object.assign({}, model.ctx);
    if (ctx.turn) ctx.turn = Object.assign({}, ctx.turn);
    ctx.statusOverride = "";   // 临时文案默认随迁移清空，需要的分支自己重设
    var cmds = [];
    var turn = ctx.turn;
    var chunk, tail;

    switch (event.type) {

      // ── 浮层开关 ──────────────────────────────────────────────────────────
      case "OPEN": {
        if (state !== "closed") {
          // 已开着：autoStart 且暂停中 → 等同点圆启动（openOverlay(true) 旧语义）
          if (event.autoStart && state === "paused" && !ctx.hardBlock) {
            return transition({ state: state, ctx: ctx }, {
              type: "TOGGLE", externalTurnOpen: event.externalTurnOpen, wakeKeyword: event.wakeKeyword,
            });
          }
          return res(state, ctx, cmds);
        }
        ctx = createInitialModel().ctx;
        ctx.hidden = Boolean(event.hidden);
        ctx.hardBlock = event.hardBlock || null;
        if (ctx.hardBlock) {
          ctx.statusOverride = ctx.hardBlock === "https"
            ? "需要 HTTPS 才能使用麦克风" : "浏览器不支持语音识别";
          return res("error", ctx, cmds);
        }
        if (event.autoStart) {
          cmds.push({ type: "primeAudio" });
          cmds.push({ type: "wakeLock", on: true });
          // 已有进行中的 turn = 对话已在进行 → 直接 awake，不要求先喊唤醒词
          ctx.wake = makeWake(event.wakeKeyword, Boolean(event.externalTurnOpen));
          if (event.externalTurnOpen) {
            ctx.statusOverride = "等待当前回复结束…";
            return res("thinking", ctx, cmds);
          }
          return res(startListening(ctx, cmds), ctx, cmds);
        }
        return res("paused", ctx, cmds);
      }

      case "CLOSE": {
        if (state === "closed") return res(state, ctx, cmds);
        cmds.push({ type: "stopMic" });
        cmds.push({ type: "stopSpeech" });
        cmds.push({ type: "clearTimer", tag: "start" });
        cmds.push({ type: "clearTimer", tag: "cooldown" });
        cmds.push({ type: "clearTimer", tag: "wakeIdle" });
        cmds.push({ type: "wakeLock", on: false });
        cmds.push({ type: "teardown" });   // shell：dispose 播放器/合成链、释放焦点 guard
        return res("closed", createInitialModel().ctx, cmds);
      }

      // ── 点圆（开始/暂停免提）─────────────────────────────────────────────
      case "TOGGLE": {
        if (state === "closed" || ctx.hardBlock) return res(state, ctx, cmds);
        if (state === "paused" || state === "error") {
          if (state === "error") ctx.hardBlock = null;   // 非硬阻断错误允许重试
          cmds.push({ type: "primeAudio" });             // 手势内解锁静音 audio/播放器【B2/C3】
          cmds.push({ type: "wakeLock", on: true });
          // 每次进入循环重新待机（对话进行中除外）【W1】
          ctx.wake = makeWake(event.wakeKeyword, turnIsOpen(ctx, event));
          if (turnIsOpen(ctx, event)) {
            ctx.statusOverride = "等待当前回复结束…";
            return res("thinking", ctx, cmds);
          }
          return res(startListening(ctx, cmds), ctx, cmds);
        }
        // 活跃 → 暂停：不释放焦点（浮层开着维持静音环境，✕ 退出才放音乐回来）
        if (turn) { turn.muted = true; }
        cmds.push({ type: "stopMic" });
        cmds.push({ type: "stopSpeech" });
        cmds.push({ type: "clearTimer", tag: "start" });
        cmds.push({ type: "clearTimer", tag: "cooldown" });
        cmds.push({ type: "clearTimer", tag: "wakeIdle" });
        cmds.push({ type: "wakeLock", on: false });
        ctx.statusOverride = "已暂停，点击麦克风继续";
        return res("paused", ctx, cmds);
      }

      // ── 点屏空白处（手势意图按状态路由，吸收旧 resolveTapAction）──────────
      case "TAP": {
        // 点屏是用户手势：无论路由到哪个分支，先顺手解锁静音保持音/播放器 ctx 的
        // autoplay（与 OPEN/TOGGLE 同一命令通道，可回放测试可见）。
        cmds.push({ type: "primeAudio" });
        switch (state) {
          case "capturing":
          case "starting":
            if (inStandby(ctx)) {
              // 待机点屏 = 手动唤醒（兜底唤醒词识别不出来的场景）【W5】
              wakeUpCmds(ctx, cmds);
              return res(startListening(ctx, cmds), ctx, cmds);
            }
            cmds.push({ type: "flushMic" });   // 立即发送：shell 调 flushNow，空则回 FLUSH_EMPTY
            return res(state, ctx, cmds);
          case "thinking":
            if (turnIsOpen(ctx, event)) {
              ctx.statusOverride = "正在停止当前回复…";
              if (turn && turn.cancelRequested) return res(state, ctx, cmds);   // 防重复刷【E2】
              if (turn) turn.cancelRequested = true;
              cmds.push({ type: "cancelTurn", turnId: (turn && turn.id) || event.externalTurnId || "" });
              return res(state, ctx, cmds);
            }
            return res(startListening(ctx, cmds), ctx, cmds);   // 无可取消的 turn：回聆听
          case "speaking":
            cmds.push({ type: "stopSpeech" });
            if (turn && turn.open) {
              // 后端仍在生成：取消整轮；muted 防后续 delta 再开口【E1】
              turn.muted = true;
              ctx.statusOverride = "正在停止当前回复…";
              if (!turn.cancelRequested) {
                turn.cancelRequested = true;
                cmds.push({ type: "cancelTurn", turnId: turn.id });
              }
              return res("thinking", ctx, cmds);
            }
            return res(startListening(ctx, cmds), ctx, cmds);   // 只剩本地播报：停掉立即续听
          case "cooldown":
            cmds.push({ type: "clearTimer", tag: "cooldown" });
            return res(startListening(ctx, cmds), ctx, cmds);
          default:
            return res(state, ctx, cmds);
        }
      }

      case "FLUSH_EMPTY": {
        if (state === "capturing" || state === "starting") {
          ctx.statusOverride = "还没识别到内容，请继续说话";
        }
        return res(state, ctx, cmds);
      }

      // ── 识别引擎事件 ──────────────────────────────────────────────────────
      case "MIC_STARTED": {
        if (state !== "starting") return res(state, ctx, cmds);
        cmds.push({ type: "clearTimer", tag: "start" });
        return res("capturing", ctx, cmds);
      }

      case "MIC_INTERIM": {
        if (state === "capturing" || state === "starting") {
          ctx.statusOverride = "识别中：" + (event.text || "");
          // 说话中重置静默回落计时器（连续对话窗口以"最后一次语音活动"计）【W4】
          if (ctx.wake && ctx.wake.awake) cmds.push({ type: "armTimer", tag: "wakeIdle", ms: WAKE_IDLE_MS });
        }
        return res(state, ctx, cmds);
      }

      case "MIC_FINAL": {
        if (state !== "capturing" && state !== "starting") return res(state, ctx, cmds);
        var text = (event.text || "").trim();
        if (!text) return res(state, ctx, cmds);
        if (inStandby(ctx)) {
          // 待机：只做唤醒词匹配，不发送【W1/W2/W3】
          var m = matchWake(text, ctx.wake.keywords);
          if (!m.matched) {
            // 非唤醒词：静默丢弃、原地续听（recognizer 仍在跑，flush 已清空其缓冲）。
            // 不设 statusOverride——顶掉待机提示语后没有事件会把它清回来，
            // 提示「说"xx"开始对话」会永久消失。
            return res(state, ctx, cmds);
          }
          wakeUpCmds(ctx, cmds);
          if (m.remainder) {
            // 一句话直达："小克今天天气" → 唤醒并直接发送尾巴【W3】
            cmds.push({ type: "chatSend", text: m.remainder });
            return res("thinking", ctx, cmds);
          }
          return res(startListening(ctx, cmds), ctx, cmds);   // startMic 按 awake 切回所选引擎
        }
        cmds.push({ type: "stopMic" });
        cmds.push({ type: "clearTimer", tag: "start" });
        cmds.push({ type: "clearTimer", tag: "wakeIdle" });   // 进入对话，回落计时随之失效
        cmds.push({ type: "chatSend", text: text });
        return res("thinking", ctx, cmds);
      }

      case "MIC_ENDED": {
        // 自然静音超时/服务掉线的续听接力；后台不重启（回前台统一恢复）【A1】
        if (state !== "capturing" && state !== "starting") return res(state, ctx, cmds);
        if (ctx.hidden) return res("starting", ctx, cmds);
        return res(startListening(ctx, cmds), ctx, cmds);
      }

      case "MIC_ERROR": {
        if (event.kind !== "denied") return res(state, ctx, cmds);   // 非权限错误：等 MIC_ENDED 续听重试
        if (ctx.hidden) return res(state, ctx, cmds);                // 后台拒麦是暂时的【A1】
        if (!isActive(state)) return res(state, ctx, cmds);
        cmds.push({ type: "clearTimer", tag: "start" });
        cmds.push({ type: "clearTimer", tag: "wakeIdle" });
        cmds.push({ type: "wakeLock", on: false });
        ctx.statusOverride = "麦克风权限被拒绝，请在浏览器设置中允许";
        return res("error", ctx, cmds);
      }

      case "TIMEOUT": {
        if (event.tag === "start") {
          // 聆听看门狗【A4】：starting 卡死（onstart 不回 / start 抛错被吞）→ 强制重建重开
          if (state !== "starting" || ctx.hidden) return res(state, ctx, cmds);
          cmds.push({ type: "rebuildMic" });
          cmds.push({ type: "armTimer", tag: "start", ms: null });
          return res(state, ctx, cmds);
        }
        if (event.tag === "cooldown") {
          if (state !== "cooldown" || ctx.hidden) return res(state, ctx, cmds);
          if (turnIsOpen(ctx, event)) {
            ctx.statusOverride = "等待当前回复结束…";
            return res("thinking", ctx, cmds);
          }
          return res(startListening(ctx, cmds), ctx, cmds);
        }
        if (event.tag === "wakeIdle") {
          // 连续对话窗口静默到点 → 回落待唤醒【W4】：stopMic + startMic 让 shell 把
          // 引擎切回待机的本地 Web Speech（阿里云不再计费）
          if ((state !== "capturing" && state !== "starting") || ctx.hidden) return res(state, ctx, cmds);
          if (!ctx.wake || !ctx.wake.awake) return res(state, ctx, cmds);
          ctx.wake.awake = false;
          cmds.push({ type: "chime", variant: "sleep" });   // 回落待机降调提示音（区别于唤醒升调），提示"又要重新唤醒了"
          cmds.push({ type: "stopMic" });
          cmds.push({ type: "clearTimer", tag: "start" });
          return res(startListening(ctx, cmds), ctx, cmds);
        }
        return res(state, ctx, cmds);
      }

      // ── 聊天事件（app.js 同一 ws/session 喂入）────────────────────────────
      case "CHAT_ACCEPTED": {
        if (state === "closed") return res(state, ctx, cmds);
        cmds.push({ type: "stopSpeech" });   // 新 turn 接管：旧播报清场
        ctx.resumeReplay = false;
        // muted：非活跃态（暂停中）接的 turn 本轮禁播报（旧 speakThisTurn=v.active）
        ctx.turn = newTurn(event.turnId, !isActive(state), event.voiceSsml || event.voice_ssml);
        if (state === "capturing" || state === "starting") {
          cmds.push({ type: "stopMic" });
          cmds.push({ type: "clearTimer", tag: "start" });
          cmds.push({ type: "clearTimer", tag: "wakeIdle" });   // 进入对话，回落计时随之失效
          return res("thinking", ctx, cmds);
        }
        if (state === "speaking" || state === "cooldown") {
          cmds.push({ type: "clearTimer", tag: "cooldown" });
          return res("thinking", ctx, cmds);
        }
        return res(state, ctx, cmds);   // thinking 维持；paused 维持（muted 已置）
      }

      case "TEXT_DELTA": {
        if (!turn) return res(state, ctx, cmds);
        turn.text += event.text || "";
        if (turn.muted || !isActive(state)) return res(state, ctx, cmds);
        if (turn.ssml) {
          if (state === "thinking") ctx.statusOverride = "正在接收回复…";
          return res(state, ctx, cmds);
        }
        if (ctx.hidden) {
          // 后台不起播报：标记回前台全文重播【D2】
          if (turn.text.trim()) ctx.resumeReplay = true;
          return res(state, ctx, cmds);
        }
        chunk = readyChunk(turn);
        if (chunk) {
          turn.sentUpTo += chunk.advance;
          if (chunk.text) {
            speakCmds(turn, chunk.text, cmds);
            return res("speaking", ctx, cmds);
          }
        }
        if (state === "thinking") ctx.statusOverride = "正在接收回复…";
        return res(state, ctx, cmds);
      }

      case "TURN_DONE": {
        if (!turn) return res(state, ctx, cmds);
        turn.open = false;
        turn.cancelRequested = false;
        if (turn.muted || !isActive(state)) {
          // 本轮禁播报（被打断/暂停）：活跃态回聆听，暂停态原地
          if (!isActive(state)) return res(state, ctx, cmds);
          if (ctx.hidden) return res("starting", ctx, cmds);
          cmds.push({ type: "stopSpeech" });   // 防御：打断后的残余队列
          return res(resumeAfterTurn(ctx, turn, cmds), ctx, cmds);
        }
        if (ctx.hidden) {
          if (turn.text.trim()) ctx.resumeReplay = true;
          return res(state, ctx, cmds);
        }
        if (turn.ssml) {
          tail = turn.text.trim();
        } else {
          tail = turn.text.slice(turn.sentUpTo).trim();
          turn.sentUpTo = turn.text.length;
        }
        if (tail) speakCmds(turn, tail, cmds);
        if (turn.pushed) {
          cmds.push({ type: "speakerEnd" });   // 文本流收尾：云端发 Stop / 本地标记排空即完【B4】
          return res("speaking", ctx, cmds);
        }
        return res(resumeAfterTurn(ctx, turn, cmds), ctx, cmds);   // 整轮无可读文本
      }

      case "TURN_ERROR": {
        ctx.turn = null;
        cmds.push({ type: "stopSpeech" });
        if (!isActive(state)) return res(state, ctx, cmds);
        if (ctx.hidden) return res("starting", ctx, cmds);
        cmds.push({ type: "speakOnce", text: "出错了" });   // 读完 SPEAK_DRAINED → cooldown → 续听
        return res("speaking", ctx, cmds);
      }

      case "TURN_CANCELLED": {
        ctx.turn = null;
        cmds.push({ type: "stopSpeech" });
        if (!isActive(state)) return res(state, ctx, cmds);
        if (ctx.hidden) return res("starting", ctx, cmds);
        return res(startListening(ctx, cmds), ctx, cmds);
      }

      // ── 合成端事件 ────────────────────────────────────────────────────────
      case "SPEAK_AUDIBLE": {
        if (turn) turn.anyAudio = true;   // 无 turn 的 speakOnce 场景：drained 时按出过声走冷却
        return res(state, ctx, cmds);
      }

      case "SPEAK_DRAINED": {
        if (state !== "speaking") return res(state, ctx, cmds);
        if (turn && turn.open) {
          ctx.statusOverride = "正在接收回复…";
          return res("thinking", ctx, cmds);   // 句间空隙：等更多 delta【E1】
        }
        return res(resumeAfterTurn(ctx, turn || { anyAudio: true }, cmds), ctx, cmds);
      }

      case "SPEAKER_RESET": {
        // 用户换「语音输出」引擎/音色：正在播立即停，本轮按读完处理；链由 shell 重建
        if (state !== "speaking") return res(state, ctx, cmds);
        cmds.push({ type: "stopSpeech" });
        if (turn) { turn.muted = true; }   // 本轮剩余 delta 不再用旧链开口
        if (turn && turn.open) {
          ctx.statusOverride = "正在接收回复…";
          return res("thinking", ctx, cmds);
        }
        return res(resumeAfterTurn(ctx, turn, cmds), ctx, cmds);
      }

      // ── 可见性（锁屏/切后台/回前台）──────────────────────────────────────
      case "HIDDEN": {
        if (state === "closed" || ctx.hidden) return res(state, ctx, cmds);
        ctx.hidden = true;
        cmds.push({ type: "wakeLock", on: false });
        if (state === "starting" || state === "capturing") {
          cmds.push({ type: "dropMic" });
          cmds.push({ type: "clearTimer", tag: "start" });
          cmds.push({ type: "clearTimer", tag: "wakeIdle" });
        }
        // 浏览器后台/锁屏不保证继续听麦；先丢弃识别器，回前台再统一 cooldown
        // 重建，避免旧 wrapper 占着 busy 却不再产出结果【A1/D1】。
        return res(state, ctx, cmds);
      }

      case "VISIBLE": {
        if (state === "closed" || !ctx.hidden) return res(state, ctx, cmds);
        ctx.hidden = false;
        cmds.push({ type: "recoverSpeechOutput" });   // synth 清卡死队列 / 播放器 unlock / 焦点 refresh
        if (!isActive(state)) return res(state, ctx, cmds);
        cmds.push({ type: "wakeLock", on: true });
        var needReplay = (ctx.resumeReplay || event.speechBusy)
          && turn && turn.text.trim() && !turn.muted;
        if (needReplay) {
          // 全文重播一次且只一次【D2】：先整链拆干净（旧合成流/陈旧播放游标【D1】），
          // 再从本轮开头完整重投
          ctx.resumeReplay = false;
          cmds.push({ type: "stopSpeech" });
          turn.sentUpTo = turn.text.length;
          turn.pushed = false;
          speakCmds(turn, turn.text.trim(), cmds);
          if (!turn.open) cmds.push({ type: "speakerEnd" });
          return res("speaking", ctx, cmds);
        }
        if (state === "speaking") return res(state, ctx, cmds);   // 云端播报跨后台仍在放：别打断
        if (turn && turn.open) {
          ctx.statusOverride = "等待当前回复结束…";
          return res("thinking", ctx, cmds);
        }
        // 聆听族：丢弃锁屏期间可能卡死的旧识别实例，延迟 1.2s 等浏览器恢复再开麦【D1】
        cmds.push({ type: "dropMic" });
        cmds.push({ type: "clearTimer", tag: "start" });
        cmds.push({ type: "clearTimer", tag: "wakeIdle" });   // 恢复路径重开麦时会重新armed
        cmds.push({ type: "armTimer", tag: "cooldown", ms: 1200 });
        return res("cooldown", ctx, cmds);
      }

      // ── 连接层失败（shell 上报）──────────────────────────────────────────
      case "SEND_FAILED": {
        cmds.push({ type: "stopMic" });
        cmds.push({ type: "wakeLock", on: false });
        ctx.statusOverride = event.message || "未连接到服务器";
        return res("error", ctx, cmds);
      }

      default:
        return res(state, ctx, cmds);
    }
  }

  return {
    createInitialModel: createInitialModel,
    transition: transition,
    focusMode: focusMode,
    isActive: isActive,
    matchWake: matchWake,
    inStandby: inStandby,
  };
});
