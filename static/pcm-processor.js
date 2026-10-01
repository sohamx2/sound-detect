class PCMProcessor extends AudioWorkletProcessor {
  constructor() { super(); this.chunks = []; this.length = 0; }
  process(inputs) {
    const input = inputs[0]?.[0];
    if (input) {
      this.chunks.push(new Float32Array(input));
      this.length += input.length;
      if (this.length >= 4096) {
        const samples = new Float32Array(this.length);
        let offset = 0;
        for (const chunk of this.chunks) { samples.set(chunk, offset); offset += chunk.length; }
        this.port.postMessage(samples, [samples.buffer]);
        this.chunks = []; this.length = 0;
      }
    }
    return true;
  }
}
registerProcessor('pcm-processor', PCMProcessor);
