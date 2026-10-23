/**
 * Мост «Python уже синтезировал фразу» → Electron получает байты WAV.
 *
 * Идея: Piper в Python кладёт результат в data/tts_cache/<sha1>.wav, где ключ —
 *     sha1("piper|текст|путь_к_модели|speed|volume")[:20]
 * (tts.py: _cache_key + _speak_piper). Мы считаем ровно такой же ключ в Node и
 * читаем файл. Так визуализатор получает НАСТОЯЩИЙ спектр Piper, а Python
 * править не нужно вообще.
 *
 * Ограничения, о которых честно стоит знать:
 *  - работает только при TTS_USE_CACHE = true и TTS_ENGINE = piper;
 *  - если Python перед озвучкой чистит текст (markdown и т.п.), ключ не совпадёт —
 *    тогда renderer автоматически переключается на уровни из SSE.
 */

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { pythonRoot, ttsCacheDir } from "./config";
import { log } from "./log";

export interface TtsSettings {
  modelPath: string;
  /** Строки, а не числа: Python хеширует именно str(), где 1.0 ≠ "1". */
  speed: string;
  volume: string;
  useCache: boolean;
}

const SETTINGS_FILE = "jarvis_settings.json";

/** Достаёт числовой литерал из JSON как есть, сохраняя «1.0». */
function rawNumber(text: string, key: string, fallback: string): string {
  const match = new RegExp(`"${key}"\\s*:\\s*(-?[0-9][0-9.eE+-]*)`).exec(text);
  return match ? match[1] : fallback;
}

export function readTtsSettings(): TtsSettings | null {
  const file = path.join(pythonRoot(), SETTINGS_FILE);
  let text: string;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch {
    log("debug", "jarvis_settings.json не найден — мост аудио выключен");
    return null;
  }

  let parsed: Record<string, unknown> = {};
  try {
    parsed = JSON.parse(text) as Record<string, unknown>;
  } catch {
    log("warn", "jarvis_settings.json не разобрался как JSON");
  }

  const storedModel = typeof parsed.PIPER_MODEL === "string" ? parsed.PIPER_MODEL : "";
  const modelPath = storedModel || path.join(pythonRoot(), "data", "voices", "ru_RU-dmitri-medium.onnx");

  return {
    modelPath,
    speed: rawNumber(text, "PIPER_SPEED", "1.0"),
    volume: rawNumber(text, "PIPER_VOLUME", "1.0"),
    useCache: /"TTS_USE_CACHE"\s*:\s*true/.test(text),
  };
}

/** Точная копия tts._cache_key для движка piper. */
export function cacheKeyFor(text: string, settings: TtsSettings): string {
  const raw = ["piper", text, settings.modelPath, settings.speed, settings.volume].join("|");
  return crypto.createHash("sha1").update(raw, "utf8").digest("hex").slice(0, 20);
}

export function cacheFileFor(text: string, settings: TtsSettings): string {
  return path.join(ttsCacheDir(), `${cacheKeyFor(text, settings)}.wav`);
}

/** Читает WAV, если он уже полностью записан (Python пишет через .part + rename). */
function tryRead(file: string): Buffer | null {
  try {
    const stat = fs.statSync(file);
    if (stat.size < 1000) return null;
    const buffer = fs.readFileSync(file);
    if (buffer.length < 44) return null;
    if (buffer.toString("ascii", 0, 4) !== "RIFF") return null;
    if (buffer.toString("ascii", 8, 12) !== "WAVE") return null;
    return buffer;
  } catch {
    return null;
  }
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Ждёт появления файла: синтез новой фразы занимает ~1.5–2 с, и событие
 * «assistant сказал» приходит раньше, чем Piper дописал WAV.
 */
export async function waitForWav(text: string, timeoutMs = 7000): Promise<Buffer | null> {
  const settings = readTtsSettings();
  if (!settings) return null;
  if (!settings.useCache) {
    log("debug", "TTS_USE_CACHE выключен — аудио-мост недоступен");
    return null;
  }

  const candidates = [text, text.trim()].filter((value, index, all) => value && all.indexOf(value) === index);
  const files = candidates.map((value) => cacheFileFor(value, settings));

  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    for (const file of files) {
      const buffer = tryRead(file);
      if (buffer) return buffer;
    }
    await sleep(120);
  }
  return null;
}
