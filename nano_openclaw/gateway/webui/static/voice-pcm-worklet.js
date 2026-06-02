/* 阿里云实时识别音频前处理 —— AudioWorklet processor。
 *
 * 阿里云要求送入单声道 / 16bit / 16000Hz 的 PCM，且建议每帧约 3200 字节
 * （= 1600 个 Int16 采样 = 16000Hz 下 100ms）。但浏览器麦克风的 AudioContext
 * 采样率通常是 44100/48000Hz、Float32、可能多声道，所以这里在音频线程里完成：
 *   1. 取首声道（麦克风多为单声道，多声道也只取一路即可）；
 *   2. 线性插值降采样到 16000Hz；
 *   3. Float32 [-1,1] 转 Int16；
 *   4. 攒够约 3200 字节切一帧，postMessage 给主线程发往 wss。
 *
 * 放在 worklet（音频渲染线程）而非主线程，是为了不被主线程 GC/渲染卡顿拖累采集，
 * 降低丢帧。重采样用简单线性插值——语音识别对此精度足够，不引入额外滤波依赖。
 */
class PcmDownsampler extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this.targetRate = opts.targetRate || 16000;
    this.frameBytes = opts.frameBytes || 3200;          // 每帧目标字节数
    this.frameSamples = this.frameBytes / 2;            // Int16 → 每帧采样数（1600）
    this._acc = new Int16Array(this.frameSamples);      // 当前帧累积缓冲
    this._accLen = 0;
    this._resamplePos = 0;                              // 跨 render-quantum 的小数读取位置
  }

  // 把一段 Float32（源采样率）线性插值降采样为 Int16（目标采样率），逐样推进累积帧，
  // 满帧即 postMessage。_resamplePos 跨调用保留小数相位，避免块边界处的相位跳变。
  _pushResampled(input, sourceRate) {
    const ratio = sourceRate / this.targetRate;   // > 1：每个目标样本跨多个源样本
    let pos = this._resamplePos;
    while (pos < input.length) {
      const i = Math.floor(pos);
      const frac = pos - i;
      const s0 = input[i];
      const s1 = i + 1 < input.length ? input[i + 1] : s0;
      let sample = s0 + (s1 - s0) * frac;        // 线性插值
      // Float32 [-1,1] → Int16，带 clamp 防溢出
      if (sample > 1) sample = 1; else if (sample < -1) sample = -1;
      this._acc[this._accLen++] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
      if (this._accLen >= this.frameSamples) {
        // 拷一份 buffer 出去（transfer 让所有权转移，避免拷贝开销）
        const out = this._acc.slice(0, this.frameSamples);
        this.port.postMessage(out.buffer, [out.buffer]);
        this._acc = new Int16Array(this.frameSamples);
        this._accLen = 0;
      }
      pos += ratio;
    }
    // 保留跨块小数相位：减去本块已消费的整数长度
    this._resamplePos = pos - input.length;
  }

  process(inputs) {
    const input = inputs[0];
    if (input && input.length > 0 && input[0] && input[0].length > 0) {
      this._pushResampled(input[0], sampleRate);   // sampleRate 是 worklet 全局：AudioContext 采样率
    }
    return true;   // 持续运行直到节点断开
  }
}

registerProcessor("pcm-downsampler", PcmDownsampler);
