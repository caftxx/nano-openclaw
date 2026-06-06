/* WAV 构建公共件 —— 8bit 单声道 PCM 包 44 字节头出 Blob。
 * voice-audio-focus.js（近静音保持音）与 voice-chime.js（唤醒提示音）共用，
 * 消除两份逐字节相同的 RIFF 头构造。
 * UMD：node --test 可 require；浏览器挂 window.VoiceWav。
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.VoiceWav = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // samples：0..255 的 8bit 样本序列（数组或 TypedArray）。
  function makeWavBlob(BlobImpl, sampleRate, samples) {
    var n = samples.length;
    var buf = new ArrayBuffer(44 + n);
    var view = new DataView(buf);
    function ascii(off, text) {
      for (var i = 0; i < text.length; i++) view.setUint8(off + i, text.charCodeAt(i));
    }
    ascii(0, "RIFF");
    view.setUint32(4, 36 + n, true);
    ascii(8, "WAVE");
    ascii(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);          // PCM
    view.setUint16(22, 1, true);          // mono
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate, true); // byteRate（8bit mono）
    view.setUint16(32, 1, true);          // blockAlign
    view.setUint16(34, 8, true);          // bitsPerSample
    ascii(36, "data");
    view.setUint32(40, n, true);
    for (var i = 0; i < n; i++) view.setUint8(44 + i, samples[i]);
    return new BlobImpl([buf], { type: "audio/wav" });
  }

  return { makeWavBlob: makeWavBlob };
});
