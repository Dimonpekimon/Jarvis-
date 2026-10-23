/**
 * Аудио-движок визуализатора.
 *
 * Схема: BufferSource → AnalyserNode → GainNode → destination.
 * AnalyserNode стоит ДО громкости, поэтому спектр можно снимать даже когда
 * звук приглушён в ноль (silent: true) — именно так визуализатор рисует
 * настоящий Piper, не дублируя звук из Python.
 */

export interface PlayOptions {
  /** true — только анализ, без звука (Python уже озвучил фразу сам). */
  silent?: boolean;
}

function base64ToBytes(base64: string): Uint8Array {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

export class AudioEngine {
  private ctx: AudioContext | null = null;
  private analyser: AnalyserNode | null = null;
  private gain: GainNode | null = null;
  private source: AudioBufferSourceNode | null = null;
  private freq = new Uint8Array(0);
  private startedAt = 0;
  private duration = 0;
  private active = false;

  /** Играет ли сейчас звук (для выбора источника данных визуализатора). */
  get playing(): boolean {
    if (!this.active) return false;
    if (this.ctx && this.duration > 0 && this.ctx.currentTime - this.startedAt >= this.duration) {
      this.active = false;
    }
    return this.active;
  }

  get audioContext(): AudioContext | null {
    return this.ctx;
  }

  async ensure(): Promise<AudioContext> {
    if (!this.ctx) {
      const ctx = new AudioContext({ latencyHint: "interactive" });
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 2048; // 1024 бина — хватает на 32–64 столбика
      analyser.smoothingTimeConstant = 0.7;
      analyser.minDecibels = -88;
      analyser.maxDecibels = -12;

      const gain = ctx.createGain();
      gain.gain.value = 1;

      analyser.connect(gain);
      gain.connect(ctx.destination);

      this.ctx = ctx;
      this.analyser = analyser;
      this.gain = gain;
      this.freq = new Uint8Array(analyser.frequencyBinCount);
    }
    // Chromium может создать контекст в состоянии suspended — будим.
    if (this.ctx.state === "suspended") await this.ctx.resume();
    return this.ctx;
  }

  /** Текущий спектр 0..255 или null, если движок ещё не инициализирован. */
  spectrum(): Uint8Array | null {
    if (!this.analyser) return null;
    this.analyser.getByteFrequencyData(this.freq);
    return this.freq;
  }

  /** Проигрывает WAV из base64. Возвращает длительность в секундах. */
  async playBase64(base64: string, options: PlayOptions = {}): Promise<number> {
    const ctx = await this.ensure();
    const bytes = base64ToBytes(base64);
    // decodeAudioData «съедает» буфер, поэтому передаём копию.
    const copy = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
    const buffer = await ctx.decodeAudioData(copy);

    this.stop();

    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(this.analyser as AnalyserNode);
    source.onended = () => {
      if (this.source === source) this.active = false;
    };

    // 0 ровно не ставим: некоторые сборки Chromium оптимизируют «тишину».
    (this.gain as GainNode).gain.value = options.silent ? 0.0001 : 1;

    source.start();
    this.source = source;
    this.startedAt = ctx.currentTime;
    this.duration = buffer.duration;
    this.active = true;
    return buffer.duration;
  }

  stop(): void {
    if (this.source) {
      this.source.onended = null;
      try {
        this.source.stop();
      } catch {
        /* уже остановлен */
      }
      this.source.disconnect();
      this.source = null;
    }
    this.active = false;
  }
}

export const audioEngine = new AudioEngine();
