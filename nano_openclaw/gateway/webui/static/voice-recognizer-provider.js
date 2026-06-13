/* 语音识别 provider 工厂。
 *
 * Shell 只决定当前要用哪个 provider；具体 recognizer 的创建参数集中在这里，
 * 与 speaker provider 对称。
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.createVoiceRecognizerProvider = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function createVoiceRecognizerProvider(opts) {
    opts = opts || {};
    var createAliyunRecognizer = opts.createAliyunRecognizer;
    var createWebspeechRecognizer = opts.createWebspeechRecognizer;
    var getAliyunConfig = opts.getAliyunConfig || function () { return {}; };
    var getToken = opts.getToken;
    var log = opts.log || function () {};

    function create(name, runtime) {
      runtime = runtime || {};
      var cbs = runtime.callbacks || {};
      if (name === "aliyun") {
        if (!createAliyunRecognizer) throw new Error("Aliyun recognizer provider is unavailable");
        return createAliyunRecognizer(Object.assign({
          getConfig: getAliyunConfig,
          getToken: getToken,
        }, cbs));
      }
      if (!createWebspeechRecognizer) throw new Error("WebSpeech recognizer provider is unavailable");
      var extra = runtime.standby ? { baseSilenceMs: 800, maxSilenceMs: 1600 } : {};
      return createWebspeechRecognizer(Object.assign(extra, cbs));
    }

    return { create: create, log: log };
  }

  return createVoiceRecognizerProvider;
});
