/* Web Speech 识别适配器 —— 浏览器内置 SpeechRecognition 包装为统一 Recognizer 端口。
 *
 * 端口契约（与 voice-recognizer-aliyun.js 一致，核心状态机零感知引擎差异）：
 *   { start, stop, rebuild, flushNow, busy, startTimeoutMs, name }
 *   回调：onStarted / onInterim(shown) / onFinal(text) / onError(kind, msg) / onEnded
 *   - onFinal：一句话已断好，调用方负责停麦+发送（适配器不自作主张拆自己）
 *   - onEnded：**非主动**结束（自然静音超时 / 浏览器拆服务）才触发；stop()/flushNow()
 *     这类主动停止被吞掉——调用方既然下了命令就不需要回执，旧实现靠 wantListen
 *     标志区分的语义在这里内聚。
 *   - onError("denied")：麦克风权限被拒。是否当真（后台拒麦是暂时的【A1】）由核心
 *     依 hidden 状态裁决，适配器只如实上报。
 *
 * 历史坑位（语义必须保持）：
 *  - 【A3】Android Chrome 在说话中途的自然停顿就吐 isFinal，「首 final 即发送」会把
 *    长句截断。内嵌整句去抖累积：累积分片 final，任何语音活动重置静音计时器，持续
 *    静音才 flush 整句。等待时间动态分档：base 1600ms 起步，按累积长度
 *    （≥20/40/80 字各 +300/+400/+300），末次仍是未定 interim 再 +400（识别没收尾），
 *    封顶 3200ms。不依赖句末标点——zh-CN 后端 transcript 基本不带标点。
 *  - 【A2】stop 用 abort()（立即丢弃挂起结果）而非 stop()（等末尾 final，收尾久），
 *    压缩「上一段没真正结束就要重开」的竞态窗口；被 rebuild 替换的旧对象回调按
 *    对象身份一律忽略；start() 抛错（InvalidStateError 等）不吞——log 暴露，
 *    自愈交给核心的 starting 超时重建【A4】。
 *  - 手动「点屏立即发送」（flushNow）要带上当前未定 interim：最后几个字常还没 final。
 *    自动（静音）flush 只发已确认 buffer：到点时 interim 多半已 final 或已作废。
 *
 * UMD：SR / 计时器可注入，node --test 可测。
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.createWebspeechRecognizer = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function createWebspeechRecognizer(opts) {
    opts = opts || {};
    var SRImpl = opts.SRImpl !== undefined
      ? opts.SRImpl
      : (typeof window !== "undefined" ? (window.SpeechRecognition || window.webkitSpeechRecognition) : null);
    var setTimer = opts.setTimer || setTimeout;
    var clearTimer = opts.clearTimer || clearTimeout;
    var lang = opts.lang || "zh-CN";
    var baseSilenceMs = opts.baseSilenceMs != null ? opts.baseSilenceMs : 1600;
    var maxSilenceMs = opts.maxSilenceMs != null ? opts.maxSilenceMs : 3200;
    var log = opts.log || function () {};
    var onStarted = opts.onStarted || function () {};
    var onInterim = opts.onInterim || function () {};
    var onFinal = opts.onFinal || function () {};
    var onError = opts.onError || function () {};
    var onEnded = opts.onEnded || function () {};

    var recog = null;        // 当前 SR 对象；被 rebuild 替换的旧对象回调按身份忽略【A2】
    var running = false;     // onstart 已确认
    var starting = false;    // start() 已发、onstart 未回的中间态
    var buffer = "";         // 已确认 final 累积【A3】
    var lastInterim = "";    // 末次未定 interim（flushNow 要带上）
    var timer = null;        // 静音去抖计时器

    function disarm() { if (timer != null) { clearTimer(timer); timer = null; } }

    // 动态静音窗口【A3】：base 起步，长句分档加码，末次仍是 interim 再加一档，封顶。
    function silenceMs(interim) {
      var len = (buffer + (interim || "")).trim().length;
      var ms = baseSilenceMs;
      if (len >= 20) ms += 300;
      if (len >= 40) ms += 400;
      if (len >= 80) ms += 300;
      if (interim) ms += 400;
      return Math.min(ms, maxSilenceMs);
    }

    function arm(interim) { disarm(); timer = setTimer(autoFlush, silenceMs(interim)); }

    function clearBuffer() { buffer = ""; lastInterim = ""; }

    // 主动停麦（不触发 onEnded）：abort 立即终止【A2】。
    function abortQuiet() {
      disarm();
      var r = recog;
      recog = null;            // 先摘引用：旧对象后续回调（onend/onerror）按身份全忽略
      running = false;
      starting = false;
      if (r) { try { r.abort(); } catch (_) {} }
    }

    // 静音到点：整句 flush。先清 buffer 再回调（onFinal 里可能再驱动本适配器）。
    function autoFlush() {
      timer = null;
      var text = buffer.trim();
      clearBuffer();
      if (text) onFinal(text);
    }

    function build() {
      var r = new SRImpl();
      r.lang = lang;
      r.continuous = true;
      r.interimResults = true;
      r.onstart = function () {
        if (r !== recog) return;
        starting = false;
        running = true;
        onStarted();
      };
      r.onresult = function (e) {
        if (r !== recog) return;
        var interim = "", finalText = "";
        for (var i = e.resultIndex; i < e.results.length; i++) {
          var res = e.results[i];
          if (res.isFinal) finalText += res[0].transcript;
          else interim += res[0].transcript;
        }
        if (finalText) buffer += finalText;
        lastInterim = interim || "";
        if (buffer || interim) arm(interim);   // 任何语音活动重置静音计时器【A3】
        var shown = (buffer + (interim || "")).trim();
        if (shown) onInterim(shown);
      };
      r.onerror = function (e) {
        if (r !== recog) return;
        if (e.error === "not-allowed" || e.error === "service-not-allowed") {
          // 如实上报；后台拒麦是否当真由核心裁决【A1】。立即清运行态——权限被拒
          // 正是浏览器最易丢 onend 的脆弱边角，不等 onend。
          running = false;
          starting = false;
          onError("denied", e.error);
        }
      };
      r.onend = function () {
        if (r !== recog) return;     // 被替换的旧对象
        running = false;
        starting = false;
        onEnded();                   // 非主动结束（主动停止已在 abortQuiet 摘引用）
      };
      return r;
    }

    function start() {
      if (!SRImpl) { onError("unsupported", "SpeechRecognition 不可用"); return; }
      if (running || starting) return;   // 抗重入：重复 start 抛 InvalidStateError【A2】
      if (!recog) recog = build();
      starting = true;
      try { recog.start(); }
      catch (err) {
        starting = false;
        // 不吞：暴露原因；自愈交给核心 starting 超时 → rebuild【A4】。
        log("start-failed", (err && err.name) + " " + (err && err.message));
      }
    }

    function stop() {
      clearBuffer();   // 主动停麦清半句，避免误发
      abortQuiet();
    }

    // 卡死自愈【A4】：丢弃可能永不回 onend 的旧对象，建新的重开。
    function rebuild() {
      abortQuiet();
      recog = null;
      start();
    }

    // 点屏立即发送：buffer + 未定 interim 一起发；返回实际发出的文本（空=没发）。
    function flushNow() {
      disarm();
      var text = (buffer + lastInterim).trim();
      clearBuffer();
      if (text) {
        abortQuiet();
        onFinal(text);
      }
      return text;
    }

    function busy() { return running || starting; }

    return {
      start: start, stop: stop, rebuild: rebuild, flushNow: flushNow, busy: busy,
      startTimeoutMs: opts.startTimeoutMs != null ? opts.startTimeoutMs : 1500,
      name: "webspeech",
    };
  }

  return createWebspeechRecognizer;
});
