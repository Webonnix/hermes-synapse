/**
 * Shared microphone/TTS amplitude analyser for the Vexa core animations.
 *
 * A single instance is created per VexaCommandCenter mount and handed to both the
 * instant 2D fallback and the lazy-loaded Three.js scene, because the Web Audio API
 * only allows a MediaElementAudioSourceNode to be created once per <audio> element —
 * two independent AudioContexts each trying to wrap the same TTS element would throw.
 */
export class VexaAudioAnalyser {
  private micStreamRef: React.RefObject<MediaStream | null>;
  private ttsAudioElRef: React.RefObject<HTMLAudioElement | null>;
  private ctx: AudioContext | null = null;
  private analyser: AnalyserNode | null = null;
  private data: Uint8Array<ArrayBuffer> | null = null;
  private micSource: { stream: MediaStream; node: MediaStreamAudioSourceNode } | null = null;
  private ttsSource: { el: HTMLAudioElement; node: MediaElementAudioSourceNode } | null = null;

  constructor(micStreamRef: React.RefObject<MediaStream | null>, ttsAudioElRef: React.RefObject<HTMLAudioElement | null>) {
    this.micStreamRef = micStreamRef;
    this.ttsAudioElRef = ttsAudioElRef;
  }

  private ensureContext(): AudioContext | null {
    if (this.ctx) return this.ctx;
    try {
      const Ctor = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!Ctor) return null;
      const ctx = new Ctor();
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 128;
      analyser.smoothingTimeConstant = 0.7;
      this.ctx = ctx;
      this.analyser = analyser;
      this.data = new Uint8Array(new ArrayBuffer(analyser.frequencyBinCount));
      return ctx;
    } catch {
      return null;
    }
  }

  private syncMic() {
    const ctx = this.ensureContext();
    const analyser = this.analyser;
    if (!ctx || !analyser) return;
    const stream = this.micStreamRef.current;
    if (!stream || this.micSource?.stream === stream) return;
    try {
      const node = ctx.createMediaStreamSource(stream);
      node.connect(analyser);
      this.micSource = { stream, node };
    } catch {
      this.micSource = null;
    }
  }

  private syncTts() {
    const ctx = this.ensureContext();
    const analyser = this.analyser;
    const el = this.ttsAudioElRef.current;
    if (!ctx || !analyser || !el || this.ttsSource?.el === el) return;
    try {
      const node = ctx.createMediaElementSource(el);
      node.connect(analyser);
      node.connect(ctx.destination);
      this.ttsSource = { el, node };
    } catch {
      // Browser TTS fallback (speechSynthesis) has no element to tap — callers fall back to phase-only motion.
    }
  }

  /** Call once per animation frame with the current voice phase to keep the source graph in sync. */
  sync(phase: string) {
    const ctx = this.ensureContext();
    if (!ctx) return;
    if (phase === 'listening') this.syncMic();
    else if (phase === 'speaking') this.syncTts();
    if (ctx.state === 'suspended') void ctx.resume().catch(() => {});
  }

  /** Returns a smoothed 0..1 amplitude estimate; 0 outside listening/speaking phases. */
  read(phase: string): number {
    if (phase !== 'listening' && phase !== 'speaking' || !this.analyser || !this.data) return 0;
    this.analyser.getByteFrequencyData(this.data);
    let sum = 0;
    for (let index = 0; index < this.data.length; index += 1) sum += this.data[index];
    return Math.min(1, sum / this.data.length / 150);
  }

  /** Tears down the mic connection (called when the owning animation loop restarts/unmounts). */
  disconnectMic() {
    if (this.micSource) {
      this.micSource.node.disconnect();
      this.micSource = null;
    }
  }

  /** Fully closes the shared AudioContext (called once when VexaCommandCenter unmounts). */
  dispose() {
    this.disconnectMic();
    void this.ctx?.close().catch(() => {});
    this.ctx = null;
    this.analyser = null;
    this.data = null;
    this.ttsSource = null;
  }
}
