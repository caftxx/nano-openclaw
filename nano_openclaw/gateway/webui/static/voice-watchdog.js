/* 聆听看门狗：免提续听依赖 SpeechRecognition 的"abort→onend 接力重开"或"resume 后 start()"，
 * 但 recognizer 偶发卡死——abort 的 onend 永不回（v.recognizing 卡在 true，startRecognition 的
 * 守卫一直 early-return）或 recog.start() 抛 InvalidStateError 被吞——两者都会让免提停在
 * "思考中/已停止朗读，等待回复结束…"且无人再驱动，只能刷新页面恢复（前后台切换才会触发
 * scheduleRecognizerReset 的 800ms 兜底，纯前台卡死无解）。
 *
 * 看门狗：每次进入"已发起 start / 想听但还没被 onstart 确认在听"的中间态时 arm 一个计时器；
 * 到点若仍应当聆听（shouldListen 为真）却没真正在听，就回调 onTimeout——由调用方丢弃卡死对象、
 * 重建并重开，不依赖那个不可靠的 onend。onstart 确认在听后 confirmed() 撤销兜底；稳态续听期间
 * 不 arm，故不会误杀健康的连续识别会话。
 *
 * 无 DOM 依赖、定时器注入，便于 node --test 单测。 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.createListenWatchdog = factory();
})(typeof self !== "undefined" ? self : this, function () {
  function createListenWatchdog(opts) {
    opts = opts || {};
    var timeoutMs = opts.timeoutMs != null ? opts.timeoutMs : 1500;
    var setTimer = opts.setTimer || setTimeout;
    var clearTimer = opts.clearTimer || clearTimeout;
    var shouldListen = opts.shouldListen || function () { return false; };
    var onTimeout = opts.onTimeout || function () {};
    var timer = null;
    function clear() { if (timer != null) { clearTimer(timer); timer = null; } }
    function fire() {
      timer = null;
      // 已不该听（在读 / 等回复 / 暂停 / 切后台）→ 不插手；想听却没真正在听 → 让调用方强制重建重开。
      if (shouldListen()) onTimeout();
    }
    return {
      // 进入"已发起 start、待 onstart 确认"或"想听但被守卫挡住没真正起来"的中间态时调用。
      arm: function () { clear(); timer = setTimer(fire, timeoutMs); },
      // onstart 确认真的在听了：撤销兜底。
      confirmed: clear,
      // 主动停麦 / 暂停 / 退出：撤销兜底。
      clear: clear,
      armed: function () { return timer !== null; },
    };
  }
  return createListenWatchdog;
});
