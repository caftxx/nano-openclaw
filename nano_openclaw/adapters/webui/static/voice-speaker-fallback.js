/* 合成回退链组合器 —— 把多级合成引擎组成单一 Speaker，对上只暴露
 * {begin, push, end, abort} + onAudible/onDrained/onFallback。
 *
 * 链构造（shell 按用户「语音输出」选择组装）：
 *   本地        → [local]
 *   阿里云      → [rest, local]
 *   阿里云流式  → [flowing, rest, local]
 *
 * 历史坑位（语义必须保持，旧实现靠 ttsFallback/ttsRestfulFallback/ttsBegun/
 * ttsTurnAudio/spokenLen 五个标志手工对账，全部收敛进本组合器）：
 *  - 【B3】某级致命失败 → 会话内记住降级层级，之后每轮直接从降级后的层级起，
 *    不再每轮先失败一次（体验割裂）。换音色/换输出引擎时由 shell 重建组合器复位。
 *  - 【B5】零发声失败不丢文本：跟踪本轮「已投递文本」与「是否出过声」（首帧音频/
 *    utterance onstart 确认）。降级时若一帧未响 → 把已投文本全量重投给下一级；
 *    已出过声则不重播（避免重读），按读完收尾。
 *  - 【B4】turn.done 可能先于 TaskFailed 到达：重投时若上游已调过 end()，对新引擎
 *    补 end()——否则新引擎永不 onCompleted → 播放器永不 drain → 卡死在「朗读中」。
 *  - 【B1】云端级的「读完」= 引擎 onCompleted（音频字节下发完）→ player.markEnded()
 *    → 播放器 drain（真正播完）→ onDrained 上报。本地级自己出声，onCompleted 即
 *    onDrained。
 *  - 降级后被换下引擎的迟到事件（旧 ws 残帧/迟到 TaskFailed）按层级身份一律忽略。
 *
 * onFallback(levelName, reason)：每次降级上报——核心据此更新「生效引擎」让 UI 不
 * 骗人【B8】，降到 local 时把 reason 上横幅【B6】。
 *
 * UMD：levels / createPlayer 全注入，node --test 可测。
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.createFallbackSpeaker = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function parseTalkDirective(text) {
    var raw = String(text || "").replace(/\r\n/g, "\n");
    var lines = raw.split("\n");
    var first = -1;
    for (var i = 0; i < lines.length; i++) {
      if (lines[i].trim()) { first = i; break; }
    }
    if (first < 0) return { directive: null, text: text };
    var head = lines[first].trim();
    if (head.charAt(0) !== "{" || head.charAt(head.length - 1) !== "}") {
      return { directive: null, text: text };
    }
    var obj;
    try { obj = JSON.parse(head); } catch (_) { return { directive: null, text: text }; }
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) return { directive: null, text: text };
    var directive = {};
    var voice = obj.voice || obj.voice_id || obj.voiceId;
    if (typeof voice === "string" && voice.trim()) directive.voiceId = voice.trim();
    var speed = typeof obj.speed === "number" ? obj.speed : null;
    if (speed != null && speed > 0.5 && speed < 2.0) directive.speed = speed;
    var rate = typeof obj.rateWpm === "number" ? obj.rateWpm
      : (typeof obj.rate === "number" ? obj.rate : (typeof obj.wpm === "number" ? obj.wpm : null));
    if (rate != null && rate > 0) directive.rateWpm = rate;
    var format = obj.output_format || obj.outputFormat || obj.format;
    if (typeof format === "string" && format.trim()) directive.outputFormat = format.trim();
    if (!Object.keys(directive).length) return { directive: null, text: text };
    lines.splice(first, 1);
    if (first < lines.length && !lines[first].trim()) lines.splice(first, 1);
    return { directive: directive, text: lines.join("\n") };
  }

  // 工厂：opts = {
  //   levels: [{ name, usesPlayer, create(cb) }],
  //     cb = { onAudio(buf), onAudible(), onCompleted(), onError(name, msg) }
  //     云端级用 onAudio/onCompleted/onError；本地级用 onAudible/onCompleted/onError。
  //   createPlayer({ onDrained, onAudible, onInterrupted, onError }) -> voice-pcm-player
  //     实例（云端级才需要）。键名与播放器选项 1:1，调用方应整体展开转发、勿逐键枚举
  //     ——漏转发不报错，只在生产上静默退化（onAudible 丢失=零发声判定失效，
  //     onInterrupted 丢失=解卡不掐引擎）。
  //   onAudible() / onDrained() / onFallback(levelName, reason) / log(name, msg)
  // }
  function createFallbackSpeaker(opts) {
    opts = opts || {};
    var levels = opts.levels || [];
    var createPlayer = opts.createPlayer || function () { return null; };
    var onAudible = opts.onAudible || function () {};
    var onDrained = opts.onDrained || function () {};
    var onFallback = opts.onFallback || function () {};
    var log = opts.log || function () {};

    var currentIdx = 0;        // 会话内记住的降级层级【B3】
    var engines = [];          // 懒建的各级引擎实例（按 levels 下标）
    var player = null;         // 共享 PCM 播放器（云端级）
    var pushed = [];           // 本轮已投递文本【B5】
    var endRequested = false;  // 本轮上游已调 end()【B4】
    var audioHeard = false;    // 本轮是否出过声【B5】
    var audibleFired = false;  // onAudible 仅上报一次
    var begun = false;
    var turnDirective = null;

    function anyCloudLevel() {
      for (var i = 0; i < levels.length; i++) if (levels[i].usesPlayer) return true;
      return false;
    }

    function ensurePlayer() {
      if (player) return player;
      player = createPlayer({
        onDrained: function () { onDrained(); },
        // closed-ctx 解卡（≠正常读完）：引擎可能仍在向重建后的 ctx 流尾部帧——
        // 先掐断全部引擎再上报读完，确保 mic 重开时不再有音频出声（自回声）。
        onInterrupted: function () {
          abort();
          onDrained();
        },
        // 「真正出过声」由播放器上报（首个音源排程成功）——字节到达不算【B5】：
        // ctx 起不来时字节照样流入但全程无声，误判会丢掉整段重投机会。
        onAudible: function () {
          if (levels[currentIdx] && levels[currentIdx].usesPlayer) markAudible();
        },
        // 播放器自身故障（ctx 建不起来/音源起不来）= 当前云端级实际不可用：
        // 升级为该级失败走降级链，最终落到不依赖 Web Audio 的本地级。
        // 只 log 会让整段回复无声「完成」且永不降级。
        onError: function (name, msg) {
          log("pcm-" + name, msg);
          if (levels[currentIdx] && levels[currentIdx].usesPlayer) {
            handleError(currentIdx, "pcm-" + name, msg);
          }
        },
      });
      return player;
    }

    function markAudible() {
      audioHeard = true;
      if (!audibleFired) { audibleFired = true; onAudible(); }
    }

    function engineAt(idx) {
      if (engines[idx]) return engines[idx];
      var level = levels[idx];
      engines[idx] = level.create({
        // 全部回调带层级身份：被换下引擎的迟到事件一律忽略。
        // 注意这里**不**置 audioHeard——出声与否由播放器 onAudible 判定【B5】。
        onAudio: function (buf) {
          if (idx !== currentIdx) return;
          var p = ensurePlayer();
          if (p) p.enqueue(buf);
        },
        onAudible: function () {
          if (idx !== currentIdx) return;
          markAudible();
        },
        onCompleted: function () {
          if (idx !== currentIdx) return;
          if (level.usesPlayer) {
            var p = ensurePlayer();
            if (p) p.markEnded();        // 字节下发完 → 等播放器真正播完才 drain【B1】
            else onDrained();
          } else {
            onDrained();                 // 本地级自己出声，完成即读完
          }
        },
        onError: function (name, msg) {
          if (idx !== currentIdx) return;
          handleError(idx, name, msg);
        },
      });
      return engines[idx];
    }

    function handleError(idx, name, msg) {
      var reason = name + ": " + (msg || "");
      log("tts-" + levels[idx].name, reason);
      if (idx + 1 < levels.length) {
        // 降级【B3】：会话内记住，之后每轮直接从新层级起。
        currentIdx = idx + 1;
        onFallback(levels[currentIdx].name, reason);
        if (!audioHeard && pushed.length) {
          // 零发声重投【B5】：把本轮已投文本全量交给下一级。
          if (player) { try { player.stop(); } catch (_) {} }
          var eng = engineAt(currentIdx);
          eng.begin(turnDirective);
          for (var i = 0; i < pushed.length; i++) eng.push(pushed[i], turnDirective);
          if (endRequested) eng.end();   // 上游已收尾：补 end，别让新引擎永不 complete【B4】
          return;
        }
        // 已出过声：不重播（避免重读），本轮按读完收尾。
        finishTurn(idx);
        return;
      }
      // 回退链末端也失败：解卡收尾，别停在「朗读中」。
      finishTurn(idx);
    }

    function finishTurn(idx) {
      if (levels[idx] && levels[idx].usesPlayer && player) player.markEnded();
      else onDrained();
    }

    // ── 对上接口 ─────────────────────────────────────────────────────────────
    function begin() {
      pushed = [];
      endRequested = false;
      audioHeard = false;
      audibleFired = false;
      begun = false;
      turnDirective = null;
    }

    function push(text) {
      if (!text) return;
      if (!begun) {
        var parsed = parseTalkDirective(text);
        turnDirective = parsed.directive;
        text = parsed.text;
        engineAt(currentIdx).begin(turnDirective);
        begun = true;
      }
      if (!text) return;
      pushed.push(text);
      engineAt(currentIdx).push(text, turnDirective);
    }

    function end() {
      endRequested = true;
      if (!begun) {
        engineAt(currentIdx).begin(turnDirective);
        begun = true;
      }
      engineAt(currentIdx).end();
    }

    // 中止本轮（打断/新 turn/退出）：清已投文本——之后即便降级也无可重投。幂等。
    function abort() {
      pushed = [];
      endRequested = false;
      audioHeard = false;
      audibleFired = false;
      begun = false;
      turnDirective = null;
      for (var i = 0; i < engines.length; i++) {
        if (engines[i]) { try { engines[i].abort(); } catch (_) {} }
      }
      if (player) { try { player.stop(); } catch (_) {} }
    }

    // 用户手势内解锁播放器 ctx【B2】；纯本地链不建 ctx。
    function unlock() {
      if (!anyCloudLevel()) return;
      var p = ensurePlayer();
      if (p) { try { p.unlock(); } catch (_) {} }
    }

    function dispose() {
      abort();
      if (player) { try { player.dispose(); } catch (_) {} player = null; }
    }

    // 是否还有声音在播/待播（shell 回前台判断要不要全文重播【D2】）。
    function busy() {
      if (player && player.isActive()) return true;
      var eng = engines[currentIdx];
      if (eng && typeof eng.busy === "function") return eng.busy();
      return false;
    }

    function effectiveName() { return levels[currentIdx] ? levels[currentIdx].name : ""; }

    return {
      begin: begin, push: push, end: end, abort: abort,
      unlock: unlock, dispose: dispose, busy: busy, effectiveName: effectiveName,
    };
  }

  createFallbackSpeaker.parseTalkDirective = parseTalkDirective;
  return createFallbackSpeaker;
});
