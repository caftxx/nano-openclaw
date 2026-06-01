/* 语音整句累积器：把 SpeechRecognition 的分片 final 结果按"静音去抖"合并成整句。
 * 无 DOM 依赖，定时器注入，便于 node --test 单测。
 *
 * 根因：Android Chrome 在说话中途的自然停顿处就吐 isFinal 片段，
 * "拿到首个 final 即停麦发送"会把长句在第一个停顿处截断、后半句丢失。
 * 这里改为累积 final 片段，任何语音活动重置静音计时器，
 * 持续静音一小段时间才把整段 buffer flush 出去；等待时间按已累积文本长度
 * 和 interim 活动自动调整，长句更耐心，短句回到 base 尽快响应。
 *
 * 注意：不依赖句末标点——Web Speech（Google zh-CN 后端）返回的 transcript
 * 基本不带标点，标点信号近乎恒为假，会变成对所有句子无差别加税。改用
 * “末次事件是否仍是未定 interim” 作为“还在说”的判据：interim 还在变说明
 * 识别尚未收尾，再多等一会儿。 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.createUtteranceAccumulator = factory();
})(typeof self !== "undefined" ? self : this, function () {
  function createUtteranceAccumulator(opts) {
    opts = opts || {};
    var baseSilenceMs = opts.silenceMs != null ? opts.silenceMs : (opts.baseSilenceMs != null ? opts.baseSilenceMs : 1200);
    var maxSilenceMs = opts.maxSilenceMs != null ? opts.maxSilenceMs : 2600;
    var onFlush = opts.onFlush;
    var setTimer = opts.setTimer || setTimeout;
    var clearTimer = opts.clearTimer || clearTimeout;
    var buffer = "";
    var timer = null;
    function disarm() { if (timer != null) { clearTimer(timer); timer = null; } }
    // base 起步（短句即此值，不再被无标点惩罚拖慢）；按累积长度分档加码（长句大概率还没说完，
    // 更耐心）；末次仍是未定 interim 再加一档（识别没收尾）。base 即下限，故只封顶。
    function computeSilenceMs(interim) {
      var len = (buffer + (interim || "")).trim().length;
      var ms = baseSilenceMs;
      if (len >= 20) ms += 300;
      if (len >= 40) ms += 400;
      if (len >= 80) ms += 300;
      if (interim) ms += 400;
      return Math.min(ms, maxSilenceMs);
    }
    function arm(interim) { disarm(); timer = setTimer(flush, computeSilenceMs(interim)); }
    function flush() {
      timer = null;
      var text = buffer.trim();
      buffer = "";   // 先清空再回调：onFlush 里会 stopRecognition→reset()，避免重复/递归发送
      if (text && onFlush) onFlush(text);
    }
    return {
      // 喂入一次 onresult 的结果；返回当前应展示的实时文本(buffer + interim)
      feed: function (finalText, interim) {
        if (finalText) buffer += finalText;
        if (buffer || interim) arm(interim);   // 任何语音活动都重置静音计时器
        return (buffer + (interim || "")).trim();
      },
      reset: function () { disarm(); buffer = ""; },   // 主动停麦时清空，避免发出半句
      pending: function () { return buffer.trim(); },
    };
  }
  return createUtteranceAccumulator;
});
