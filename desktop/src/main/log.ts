/**
 * Мини-логгер main-процесса.
 *
 * В dev пишем в консоль, в собранном приложении дублируем в файл — иначе
 * ошибки запуска Python-сайдкара увидеть негде (у .exe нет консоли).
 */

import fs from "node:fs";
import path from "node:path";
import { app } from "electron";

let logFile: string | null = null;

function file(): string | null {
  if (logFile) return logFile;
  try {
    const dir = app.getPath("logs");
    fs.mkdirSync(dir, { recursive: true });
    logFile = path.join(dir, "jarvis-overlay.log");
  } catch {
    logFile = null;
  }
  return logFile;
}

type Level = "info" | "warn" | "error" | "debug";

export function log(level: Level, message: string): void {
  const line = `[${new Date().toISOString().slice(11, 19)}] ${level.toUpperCase()} ${message}`;
  if (level === "error") console.error(line);
  else if (level === "warn") console.warn(line);
  else console.log(line);

  const target = file();
  if (!target) return;
  try {
    fs.appendFileSync(target, line + "\n", "utf8");
  } catch {
    /* лог не должен ронять приложение */
  }
}
