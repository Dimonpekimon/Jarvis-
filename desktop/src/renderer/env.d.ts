/**
 * Объявление моста для TypeScript: window.pyatnitsa появляется из preload.ts.
 * Без этого файла renderer не знает о его существовании.
 */

import type { PyatnitsaBridge } from "../shared/ipc";

declare global {
  interface Window {
    pyatnitsa: PyatnitsaBridge;
  }
}

export {};
