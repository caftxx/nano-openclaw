/* 浏览器本地合成引擎 —— speechSynthesis 包装为与云端引擎同构的 {begin,push,end,abort}。
 *
 * 回退链末端【B3】：阿里云流式 / RESTful 都失败后退到这里；输出引擎选「本地」时
 * 则恒用本级。不经 PCM 播放器（speechSynthesis 自己出声），所以「读完」信号由
 * 本引擎直接给：end() 后队列排空且最后一条 utterance 结束 → onCompleted。
 *
 * 历史坑位（语义必须保持）：
 *  - utterance 串行队列：一条 onend/onerror 才放下一条；synth.speak 抛错也要推进，
 *    否则队列卡死。
 *  - 锁屏期间 speechSynthesis 会取消/挂起已排队 utterance（静默丢）【D2】：
 *    busy() 暴露 synth.speaking/pending/paused，供 shell 回前台判断要不要全文重播。
 *  - 试听/错误播报这类单句直接 sayOnce：不进回退链，不影响当前队列语义。
 *  - rate 1.05、未选音色时 lang 固定 zh-CN（系统默认声音可能不是中文）。
 *
 * UMD：synth / Utterance 可注入，node --test 可测。
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.createLocalSpeaker = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // 工厂：opts = { synth, UtteranceImpl, getVoice, onAudible, onCompleted, onError, rate }
  //   getVoice() -> SpeechSynthesisVoice|null（音色选择由 view/shell 管，这里只消费）
  function createLocalSpeaker(opts) {
    opts = opts || {};
    var synth = opts.synth !== undefined
      ? opts.synth
      : (typeof window !== "undefined" ? window.speechSynthesis : null);
    var UtteranceImpl = opts.UtteranceImpl
      || (typeof SpeechSynthesisUtterance !== "undefined" ? SpeechSynthesisUtterance : null);
    var getVoice = opts.getVoice || function () { return null; };
    var onAudible = opts.onAudible || function () {};
    var onCompleted = opts.onCompleted || function () {};
    var onError = opts.onError || function () {};
    var rate = opts.rate != null ? opts.rate : 1.05;

    var queue = [];
    var speaking = false;       // 有 utterance 在播
    var endRequested = false;
    var completed = false;
    var audibleFired = false;

    function applyVoice(u) {
      var vo = null;
      try { vo = getVoice(); } catch (_) {}
      if (vo) { u.voice = vo; u.lang = vo.lang; }
      else u.lang = "zh-CN";
    }

    function maybeComplete() {
      if (endRequested && queue.length === 0 && !speaking && !completed) {
        completed = true;
        onCompleted();
      }
    }

    function drain() {
      if (speaking) return;
      var next = queue.shift();
      if (next == null) { maybeComplete(); return; }
      if (!synth || !UtteranceImpl) {
        // 端口契约：onError 即本轮终态，之后不得再补 onCompleted（消费者会双推进）。
        completed = true;
        queue = [];
        onError("unsupported", "speechSynthesis 不可用");
        return;
      }
      var u = new UtteranceImpl(next);
      applyVoice(u);
      u.rate = rate;
      u.onstart = function () {
        if (!audibleFired) { audibleFired = true; onAudible(); }
      };
      u.onend = function () { speaking = false; drain(); };
      u.onerror = function () { speaking = false; drain(); };   // 单条失败跳过，不卡队列
      speaking = true;
      try { if (typeof synth.resume === "function") synth.resume(); } catch (_) {}
      try { synth.speak(u); }
      catch (_) { speaking = false; drain(); }   // speak 抛错也要推进
    }

    function begin() {
      queue = [];
      speaking = false;
      endRequested = false;
      completed = false;
      audibleFired = false;
    }

    function push(text) {
      if (!text) return;
      queue.push(text);
      drain();
    }

    function end() {
      endRequested = true;
      maybeComplete();
    }

    function abort() {
      queue = [];
      endRequested = false;
      completed = false;
      speaking = false;
      try { if (synth) synth.cancel(); } catch (_) {}
    }

    // 锁屏/后台后 synth 可能卡着挂起队列【D2】：shell 回前台据此决定全文重播。
    function busy() {
      var synthBusy = false;
      try { synthBusy = Boolean(synth && (synth.speaking || synth.pending || synth.paused)); } catch (_) {}
      return speaking || queue.length > 0 || synthBusy;
    }

    // 单句短播报（错误提示「出错了」、音色试听）：独立于队列语义，先清场再说。
    function sayOnce(text, done) {
      if (!synth || !UtteranceImpl) { if (done) done(); return; }
      var u = new UtteranceImpl(text);
      applyVoice(u);
      u.rate = rate;
      u.onend = function () { if (done) done(); };
      u.onerror = function () { if (done) done(); };
      try { if (typeof synth.resume === "function") synth.resume(); } catch (_) {}
      try { synth.cancel(); synth.speak(u); }
      catch (_) { if (done) done(); }
    }

    return {
      begin: begin, push: push, end: end, abort: abort,
      busy: busy, sayOnce: sayOnce, name: "local",
    };
  }

  return createLocalSpeaker;
});
