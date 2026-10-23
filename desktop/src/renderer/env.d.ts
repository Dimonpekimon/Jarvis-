/**
 * Объявление моста для TypeScript: window.jarvis появляется из preload.ts.
 * Без этого файла renderer не знает о его существовании.
 */

import type { JarvisBridge } from "../shared/ipc";

declare global {
  interface Window {
    jarvis: JarvisBridge;
  }
}

export {};
