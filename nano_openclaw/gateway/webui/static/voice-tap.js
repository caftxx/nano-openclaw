/* 语音浮层「点屏空白处」手势意图解析：依据当前 phase + 是否正在朗读，
 * 决定这一下点击该做什么。纯函数、无 DOM 依赖，便于 node --test 单测；
 * 实际的 DOM 绑定与副作用（打断 / 取消 / 发送）留在 voice-mode.js。
 *
 * 规则（优先级自上而下）：
 *   - 正在朗读（speaking 或 phase==="speaking"）→ "interrupt"：打断本地播报
 *   - 思考中（phase==="thinking"，已发送等后端回复）→ "cancel"：向后端发 turn.cancel
 *   - 聆听中（phase==="listening"）→ "flush"：立即发送当前累积文本，不等去抖
 *   - 其它（idle / error / 未知）→ "none"：不处理
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.resolveTapAction = factory();
})(typeof self !== "undefined" ? self : this, function () {
  function resolveTapAction(phase, speaking) {
    if (speaking || phase === "speaking") return "interrupt";
    if (phase === "thinking") return "cancel";
    if (phase === "listening") return "flush";
    return "none";
  }
  return resolveTapAction;
});
