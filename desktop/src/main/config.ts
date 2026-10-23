/**
 * Пути, сохранённые настройки окна и способ запуска Python-сайдкара.
 *
 * Ничего не импортирует из renderer-а, чтобы main оставался автономным.
 */

import fs from "node:fs";
import path from "node:path";
import { app } from "electron";
import { DEFAULT_API_BASE, type OverlayMode, type UiMode } from "../shared/ipc";
import { log } from "./log";

export const isDev = !app.isPackaged;

/**
 * Папка python-части ассистента.
 * dev:  <проект>/desktop/dist/main → <проект>
 * prod: <установка>/resources/python (кладётся electron-builder-ом)
 */
export function pythonRoot(): string {
  if (app.isPackaged) return path.join(process.resourcesPath, "python");
  return path.resolve(__dirname, "..", "..", "..");
}

export function dataDir(): string {
  return path.join(pythonRoot(), "data");
}

export function ttsCacheDir(): string {
  return path.join(dataDir(), "tts_cache");
}

/** Адрес HTTP-API Python-ядра. Порт можно переопределить переменной окружения. */
export const apiBase = (process.env.JARVIS_API ?? DEFAULT_API_BASE).replace(/\/+$/, "");

export function apiPort(): number {
  try {
    return Number(new URL(apiBase).port) || 8765;
  } catch {
    return 8765;
  }
}

// ---------------------------------------------------------------- настройки UI

export interface Prefs {
  overlayMode: OverlayMode;
  uiMode: UiMode;
}

const DEFAULT_PREFS: Prefs = { overlayMode: "normal", uiMode: "compact" };

function prefsFile(): string {
  return path.join(app.getPath("userData"), "overlay.json");
}

export function loadPrefs(): Prefs {
  try {
    const raw = fs.readFileSync(prefsFile(), "utf8");
    const parsed = JSON.parse(raw) as Partial<Prefs>;
    return {
      overlayMode: parsed.overlayMode === "dim" ? "dim" : "normal",
      uiMode: parsed.uiMode === "dialog" ? "dialog" : "compact",
    };
  } catch {
    return { ...DEFAULT_PREFS };
  }
}

export function savePrefs(prefs: Prefs): void {
  try {
    fs.mkdirSync(path.dirname(prefsFile()), { recursive: true });
    fs.writeFileSync(prefsFile(), JSON.stringify(prefs, null, 2), "utf8");
  } catch (error) {
    log("warn", `Не удалось сохранить настройки окна: ${String(error)}`);
  }
}

// ------------------------------------------------------------ запуск Python-а

export interface PythonCommand {
  command: string;
  /** Флаги самого интерпретатора, например ["-3"] у лаунчера py. */
  prefix: string[];
  /** Аргументы приложения: main.py --no-browser ... */
  args: string[];
}

function pythonArgs(): string[] {
  const args = ["main.py", "--no-browser", "--ui-port", String(apiPort())];
  const extra = (process.env.JARVIS_PYTHON_ARGS ?? "").trim();
  if (extra) args.push(...extra.split(/\s+/));
  return args;
}

/**
 * Кандидаты на запуск по очереди.
 *
 * Порядок важен: в PATH часто оказывается «чужой» python (менеджеры версий,
 * Microsoft Store, другой проект), в котором нет requests/vosk. Поэтому сначала
 * явный путь, затем venv проекта, и только потом общие имена.
 */
export function pythonCandidates(): PythonCommand[] {
  const args = pythonArgs();
  const list: PythonCommand[] = [];

  const push = (command: string, prefix: string[] = []): void => {
    if (!command) return;
    const duplicate = list.some((item) => item.command === command && item.prefix.join(" ") === prefix.join(" "));
    if (!duplicate) list.push({ command, prefix, args });
  };

  const explicit = process.env.JARVIS_PYTHON?.trim();
  if (explicit) push(explicit);

  // Виртуальное окружение внутри проекта — самый предсказуемый вариант.
  push(path.join(pythonRoot(), ".venv", "Scripts", "python.exe"));

  push("python");
  push("py", ["-3"]);
  return list;
}

/** Сайдкар можно отключить: приложение тогда работает как «пустой» оверлей. */
export function sidecarEnabled(): boolean {
  return process.env.JARVIS_NO_SIDECAR !== "1";
}
