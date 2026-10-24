/**
 * Мост между изолированным renderer-ом и main-процессом.
 *
 * contextIsolation: true + nodeIntegration: false означают, что у страницы нет
 * ни require, ни process, ни fs. Всё, что ей разрешено, перечислено здесь —
 * это единственная точка расширения прав.
 */

import { contextBridge, ipcRenderer, type IpcRendererEvent } from "electron";
import {
  CH,
  type AppConfig,
  type AudioPayload,
  type BusEvent,
  type PyatnitsaBridge,
  type OverlayMode,
  type SendResult,
  type SidecarStatus,
  type UiMode,
} from "../shared/ipc";

/** Подписка с отпиской: React-эффект должен уметь снимать слушателя. */
function subscribe<T>(channel: string, callback: (payload: T) => void): () => void {
  const listener = (_event: IpcRendererEvent, payload: T): void => callback(payload);
  ipcRenderer.on(channel, listener);
  return () => {
    ipcRenderer.removeListener(channel, listener);
  };
}

const bridge: PyatnitsaBridge = {
  getConfig: () => ipcRenderer.invoke(CH.getConfig) as Promise<AppConfig>,
  sendMessage: (text: string) => ipcRenderer.invoke(CH.sendMessage, text) as Promise<SendResult>,
  interrupt: () => ipcRenderer.send(CH.interrupt),
  reset: () => ipcRenderer.invoke(CH.reset) as Promise<SendResult>,
  playStub: () => ipcRenderer.invoke(CH.playStub) as Promise<AudioPayload | null>,
  playTts: (text: string) => ipcRenderer.invoke(CH.playTts, text) as Promise<AudioPayload | null>,

  // send, а не invoke: эти вызовы идут десятки раз в секунду (mousemove),
  // и ждать ответа на каждый незачем.
  setIgnoreMouse: (ignore: boolean) => ipcRenderer.send(CH.setIgnoreMouse, ignore),
  setUiMode: (mode: UiMode) => ipcRenderer.send(CH.setUiMode, mode),
  setOverlayMode: (mode: OverlayMode) => ipcRenderer.send(CH.setOverlayMode, mode),
  quit: () => ipcRenderer.send(CH.quit),

  onEvent: (cb) => subscribe<BusEvent>(CH.event, cb),
  onAudio: (cb) => subscribe<AudioPayload>(CH.audio, cb),
  onSidecar: (cb) => subscribe<SidecarStatus>(CH.sidecar, cb),
  onUiMode: (cb) => subscribe<UiMode>(CH.uiMode, cb),
  onOverlayMode: (cb) => subscribe<OverlayMode>(CH.overlayMode, cb),
};

contextBridge.exposeInMainWorld("pyatnitsa", bridge);
