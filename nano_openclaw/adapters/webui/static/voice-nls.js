/* 阿里云 NLS 协议公共件 —— 识别（SpeechTranscriber）与流式合成
 * （FlowingSpeechSynthesizer）共用的纯函数，消除两个引擎间的协议层复制。
 *
 *  - makeId()：32 hex 随机 id（message_id 每条消息重新生成；task_id 整个会话一致）
 *  - envelope()：指令帧 header 构造（appkey/message_id/task_id/namespace/name + payload）
 *  - failureText()：TaskFailed 失败原因归一化。两个 namespace 的字段不同——
 *    识别用 header.status_text，合成用 header.status_message，曾因抄错字段把真实
 *    原因丢成 "task failed"（B6）。统一双字段查找，任一引擎都拿到真因。
 *
 * UMD：node --test 可 require；浏览器挂 window.VoiceNls。
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.VoiceNls = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var HEX = "0123456789abcdef";

  // 32 hex 随机 id。优先 crypto，退 Math.random（id 只需唯一、非安全敏感）。
  function makeId(randomFn) {
    var out = "";
    if (randomFn) {
      for (var i = 0; i < 32; i++) out += HEX[randomFn() & 15];
      return out;
    }
    var cryptoObj = typeof crypto !== "undefined" ? crypto : null;
    if (cryptoObj && cryptoObj.getRandomValues) {
      var buf = new Uint8Array(16);
      cryptoObj.getRandomValues(buf);
      for (var j = 0; j < 16; j++) out += HEX[buf[j] >> 4] + HEX[buf[j] & 15];
      return out;
    }
    for (var k = 0; k < 32; k++) out += HEX[(Math.random() * 16) | 0];
    return out;
  }

  // 指令帧构造：payload 为 undefined 时不带 payload 字段（Stop 类指令）。
  function envelope(appkey, taskId, namespace, name, payload, makeMsgId) {
    var frame = {
      header: {
        appkey: appkey,
        message_id: (makeMsgId || makeId)(),
        task_id: taskId,
        namespace: namespace,
        name: name,
      },
    };
    if (payload !== undefined) frame.payload = payload;
    return frame;
  }

  // TaskFailed 失败原因归一化【B6】：合成在 status_message、识别在 status_text，
  // 双字段查找 + 兜底文案，杜绝抄错字段把真因丢掉。
  function failureText(header) {
    header = header || {};
    return header.status_message || header.status_text || "task failed";
  }

  return { makeId: makeId, envelope: envelope, failureText: failureText };
});
