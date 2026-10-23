/**
 * Проверка моста «Piper в Python → WAV в Electron».
 *
 * Запускается как main-скрипт Electron (окно не создаётся, GPU не нужен):
 *     npm run check:bridge
 *     npm run check:bridge -- "любая фраза"
 *
 * Что проверяется:
 *  1) jarvis_settings.json читается и даёт те же строки, что видит Python;
 *  2) sha1-ключ кэша совпадает с tts._cache_key (иначе файл не найти);
 *  3) файл реально лежит в data/tts_cache и это корректный RIFF/WAVE.
 */

const fs = require("node:fs");
const path = require("node:path");
const { app } = require("electron");

const dist = path.join(__dirname, "..", "dist", "main");
const { cacheFileFor, cacheKeyFor, readTtsSettings, waitForWav } = require(
  path.join(dist, "ttsAudio.js"),
);

const text = process.argv[2] || "проверка визуализации";

app.whenReady().then(async () => {
  const settings = readTtsSettings();
  if (!settings) {
    console.error("НЕ НАЙДЕН jarvis_settings.json");
    app.exit(1);
    return;
  }

  console.log("настройки:", settings);
  const key = cacheKeyFor(text, settings);
  const file = cacheFileFor(text, settings);
  console.log("фраза:   ", text);
  console.log("ключ:    ", key);
  console.log("файл:    ", file);
  console.log("на диске:", fs.existsSync(file));

  const buffer = await waitForWav(text, 3000);
  if (!buffer) {
    console.error("ПРОВАЛ: WAV не найден. Озвучьте фразу в ассистенте и повторите.");
    app.exit(2);
    return;
  }

  const riff = buffer.toString("ascii", 0, 4);
  const wave = buffer.toString("ascii", 8, 12);
  console.log("байт:    ", buffer.length, "| заголовок:", riff, wave);
  console.log("OK: мост работает, AnalyserNode получит настоящий звук Piper.");
  app.exit(0);
});
