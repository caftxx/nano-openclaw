/* 麦克风音频前处理 —— AudioWorklet processor（阿里云实时识别上行用）。
 *
 * 阿里云要求单声道 / 16bit / 16000Hz PCM，建议每帧约 3200 字节
 * （= 1600 个 Int16 采样 = 16kHz 下 100ms）。浏览器麦克风的 AudioContext
 * 通常是 44100/48000Hz、Float32、可能多声道，故在音频渲染线程完成：
 *   1. 取首声道；
 *   2. 线性插值降采样到目标采样率（语音识别精度足够，不引滤波依赖）；
 *   3. Float32 [-1,1] → Int16（带 clamp）；
 *   4. 攒满一帧 postMessage（transfer 所有权，零拷贝）给主线程发 wss。
 *
 * 放 worklet 而非主线程：不被主线程 GC/渲染卡顿拖累采集，降低丢帧。
 * 小数读取位置跨 render-quantum 保留，避免块边界相位跳变。
 */
class VoicePcmDownsampler extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this.targetRate = opts.targetRate || 16000;
    this.frameBytes = opts.frameBytes || 3200;
    this.frameSamples = this.frameBytes / 2;        // Int16 → 每帧采样数
    this._acc = new Int16Array(this.frameSamples);  // 当前帧累积缓冲
    this._accLen = 0;
    this._pos = 0;                                  // 跨块的小数读取位置（相位）
  }

  _pushResampled(input, sourceRate) {
    const ratio = sourceRate / this.targetRate;     // >1：每个目标样本跨多个源样本
    let pos = this._pos;
    while (pos < input.length) {
      const i = Math.floor(pos);
      const frac = pos - i;
      const s0 = input[i];
      const s1 = i + 1 < input.length ? input[i + 1] : s0;
      let sample = s0 + (s1 - s0) * frac;           // 线性插值
      if (sample > 1) sample = 1; else if (sample < -1) sample = -1;
      this._acc[this._accLen++] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
      if (this._accLen >= this.frameSamples) {
        const out = this._acc.slice(0, this.frameSamples);
        this.port.postMessage(out.buffer, [out.buffer]);
        this._acc = new Int16Array(this.frameSamples);
        this._accLen = 0;
      }
      pos += ratio;
    }
    this._pos = pos - input.length;                 // 保留小数相位
  }

  process(inputs) {
    const input = inputs[0];
    if (input && input.length > 0 && input[0] && input[0].length > 0) {
      this._pushResampled(input[0], sampleRate);    // sampleRate：worklet 全局（ctx 采样率）
    }
    return true;                                    // 常驻直到节点断开
  }
}

registerProcessor("voice-pcm-downsampler", VoicePcmDownsampler);
