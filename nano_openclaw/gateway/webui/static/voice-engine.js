/* 语音识别引擎选路：依据用户持久化偏好 + 阿里云当前可用性，决定用哪个引擎。
 * 纯函数、无 DOM 依赖，便于 node --test 单测。
 *
 * 规则：
 *   - 用户显式选 "webspeech" → 永远 "webspeech"（即使阿里云可用，尊重用户）
 *   - 用户选 "aliyun" 且 aliyunUsable → "aliyun"；不可用 → 回退 "webspeech"
 *   - 无偏好（""/null/未知值）→ 沿用自动默认：aliyunUsable ? "aliyun" : "webspeech"
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.resolveVoiceEngine = factory();
})(typeof self !== "undefined" ? self : this, function () {
  function resolveEngine(stored, aliyunUsable) {
    if (stored === "webspeech") return "webspeech";
    return aliyunUsable ? "aliyun" : "webspeech";
  }
  return resolveEngine;
});
