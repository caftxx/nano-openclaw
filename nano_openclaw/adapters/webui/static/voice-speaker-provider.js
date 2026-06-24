/* 语音合成 provider 工厂。
 *
 * 负责把 local / aliyun-flowing / aliyun-rest 三类 speaker 组装成
 * FallbackSpeaker 所需的 levels，并集中创建共享 PCM player。
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.createVoiceSpeakerProvider = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function createVoiceSpeakerProvider(opts) {
    opts = opts || {};
    var createFallbackSpeaker = opts.createFallbackSpeaker;
    var createLocalSpeaker = opts.createLocalSpeaker;
    var createFlowingSpeaker = opts.createFlowingSpeaker;
    var createRestSpeaker = opts.createRestSpeaker;
    var createVoicePcmPlayer = opts.createVoicePcmPlayer;
    var ssml = opts.ssml || null;
    var getSelectedSystemVoice = opts.getSelectedSystemVoice || function () { return null; };
    var getAliyunConfig = opts.getAliyunConfig || function () { return {}; };
    var getToken = opts.getToken;
    var headers = opts.headers || function () { return {}; };
    var sampleRate = opts.sampleRate || function () { return 16000; };
    var callbacks = opts.callbacks || {};
    var log = opts.log || function () {};

    function localLevel() {
      return {
        name: "local",
        usesPlayer: false,
        create: function (cb) {
          return createLocalSpeaker({
            getVoice: getSelectedSystemVoice,
            onAudible: cb.onAudible,
            onCompleted: cb.onCompleted,
            onError: cb.onError,
          });
        },
      };
    }

    function flowingLevel() {
      return {
        name: "aliyun-flowing",
        usesPlayer: true,
        create: function (cb) {
          return createFlowingSpeaker({
            getConfig: getAliyunConfig,
            getToken: getToken,
            onAudio: cb.onAudio,
            onCompleted: cb.onCompleted,
            onError: cb.onError,
          });
        },
      };
    }

    function restLevel() {
      return {
        name: "aliyun-rest",
        usesPlayer: true,
        create: function (cb) {
          return createRestSpeaker({
            url: "/api/talk/speak",
            headers: headers(),
            getConfig: function () {
              var cfg = getAliyunConfig() || {};
              return { voice: cfg.voice, sampleRate: cfg.sampleRate };
            },
            onAudio: cb.onAudio,
            onCompleted: cb.onCompleted,
            onError: cb.onError,
          });
        },
      };
    }

    function levelsFor(output) {
      if (output === "aliyun-flowing") return [flowingLevel(), restLevel(), localLevel()];
      if (output === "aliyun-rest") return [restLevel(), localLevel()];
      return [localLevel()];
    }

    function build(output) {
      if (!createFallbackSpeaker) throw new Error("Fallback speaker provider is unavailable");
      var levels = levelsFor(output);
      return createFallbackSpeaker({
        levels: levels,
        // cb 全量展开转发（onDrained/onAudible/onInterrupted/onError，键名与播放器
        // 选项 1:1）——不逐键枚举：曾因按过期契约只转发两个键，把零发声判定
        // （onAudible）和解卡先掐引擎（onInterrupted）在生产路径上整段丢成死代码。
        createPlayer: function (cb) {
          return createVoicePcmPlayer(Object.assign({
            sampleRate: sampleRate(),
          }, cb));
        },
        onAudible: callbacks.onAudible,
        onDrained: callbacks.onDrained,
        onFallback: callbacks.onFallback,
        log: log,
        ssml: ssml,
      });
    }

    return { build: build, levelsFor: levelsFor };
  }

  return createVoiceSpeakerProvider;
});
