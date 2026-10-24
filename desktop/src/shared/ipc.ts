/**
 * Контракт между main-процессом и renderer-ом.
 *
 * Один файл на обе стороны — чтобы имена каналов и типы не разъезжались.
 * Renderer не имеет доступа к Node (contextIsolation: true), поэтому всё
 * общение идёт через узкий мост, описанный в preload.ts.
 */

/** Каналы IPC. Префиксы: pyatnitsa: — данные от Python, ui: — команды интерфейса. */
export const CH = {
  // main → renderer
  event: "pyatnitsa:event", // событие шины Python (message/state/level)
  audio: "pyatnitsa:audio", // готовый WAV в base64
  sidecar: "pyatnitsa:sidecar", // состояние процесса Python
  uiMode: "pyatnitsa:ui-mode", // compact | dialog
  overlayMode: "pyatnitsa:overlay-mode", // normal | dim

  // renderer → main (invoke)
  getConfig: "app:get-config",
  sendMessage: "pyatnitsa:message",
  interrupt: "pyatnitsa:interrupt",
  reset: "pyatnitsa:reset",
  playStub: "audio:play-stub",
  playTts: "audio:play-tts",

  // renderer → main (send, без ответа — важно для mousemove)
  setIgnoreMouse: "ui:ignore-mouse",
  setUiMode: "ui:set-mode",
  setOverlayMode: "ui:set-overlay-mode",
  quit: "app:quit",
} as const;

/** Состояние ассистента. offline — Python ещё не поднялся или упал. */
export type AssistantState =
  | "idle"
  | "listening"
  | "thinking"
  | "speaking"
  | "loading"
  | "offline";

/** normal — обычный, dim — полупрозрачный (окно «тает» поверх рабочего стола). */
export type OverlayMode = "normal" | "dim";

/** compact — только визуализатор, dialog — плюс история и поле ввода. */
export type UiMode = "compact" | "dialog";

export type Role = "user" | "assistant" | "system";

/** Событие из events.py, как оно пришло по SSE. */
export interface BusEvent {
  type: string;
  role?: Role;
  text?: string;
  value?: number | string;
  source?: "tts" | "mic";
  state?: AssistantState;
  ts?: string;
}

export interface ChatMessage {
  id: string;
  role: Role;
  text: string;
  at: number;
}

export interface SidecarStatus {
  state: "starting" | "ready" | "offline" | "error";
  detail?: string;
}

export interface AudioPayload {
  base64: string;
  mime: string;
  /** Откуда взяли звук: stub — тестовый файл, piper — кэш TTS. */
  label: string;
}

export interface AppConfig {
  apiBase: string;
  overlayMode: OverlayMode;
  uiMode: UiMode;
  dev: boolean;
  /** Есть ли смысл просить у main настоящий WAV (найден кэш Piper). */
  ttsBridge: boolean;
}

export interface SendResult {
  ok: boolean;
  error?: string;
}

/** То, что preload кладёт в window.pyatnitsa. */
export interface PyatnitsaBridge {
  getConfig(): Promise<AppConfig>;
  sendMessage(text: string): Promise<SendResult>;
  interrupt(): void;
  reset(): Promise<SendResult>;
  /** Тестовый WAV — чтобы визуализатор работал без запущенного Python. */
  playStub(): Promise<AudioPayload | null>;
  /** Настоящий WAV от Piper из кэша TTS (null — если не нашёлся). */
  playTts(text: string): Promise<AudioPayload | null>;
  setIgnoreMouse(ignore: boolean): void;
  setUiMode(mode: UiMode): void;
  setOverlayMode(mode: OverlayMode): void;
  quit(): void;

  onEvent(cb: (event: BusEvent) => void): () => void;
  onAudio(cb: (payload: AudioPayload) => void): () => void;
  onSidecar(cb: (status: SidecarStatus) => void): () => void;
  onUiMode(cb: (mode: UiMode) => void): () => void;
  onOverlayMode(cb: (mode: OverlayMode) => void): () => void;
}

export const DEFAULT_API_BASE = "http://127.0.0.1:8765";
