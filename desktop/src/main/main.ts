/**
 * Главный процесс: прозрачное окно-оверлей, два режима прозрачности,
 * click-through, трей, горячие клавиши и связь с Python-ядром.
 *
 * Роли файлов:
 *   config.ts   — пути, настройки окна, как запускать Python
 *   sidecar.ts  — сам дочерний процесс
 *   sse.ts      — поток событий от ядра
 *   ttsAudio.ts — готовый WAV от Piper для визуализатора
 *   main.ts     — окно + IPC + меню (этот файл)
 */

import fs from "node:fs";
import path from "node:path";
import {
  app,
  BrowserWindow,
  Menu,
  Tray,
  globalShortcut,
  ipcMain,
  nativeImage,
  screen,
} from "electron";

import {
  CH,
  type AppConfig,
  type AudioPayload,
  type BusEvent,
  type OverlayMode,
  type SidecarStatus,
  type UiMode,
  type SendResult,
} from "../shared/ipc";
import { apiBase, isDev, loadPrefs, pythonRoot, savePrefs, type Prefs } from "./config";
import { log } from "./log";
import { Sidecar } from "./sidecar";
import { SseClient } from "./sse";
import { waitForWav } from "./ttsAudio";

// AudioContext в Electron стартует «спящим» до жеста пользователя; для оверлея
// это неприемлемо — звук должен играть по событию из Python.
app.commandLine.appendSwitch("autoplay-policy", "no-user-gesture-required");
app.setAppUserModelId("ru.dima.jarvis.desktop");

// Аварийный выключатель для машин, где драйвер роняет GPU-процесс (в логе это
// «GPU process exited unexpectedly» + «GPU process isn't usable. Goodbye.»).
// Живопись Canvas на CPU медленнее, но окно хотя бы открывается.
if (process.env.JARVIS_DISABLE_GPU === "1") {
  app.disableHardwareAcceleration();
}

const DEV_URL = process.env.JARVIS_DEV_URL ?? "http://127.0.0.1:5273";
/** Грузить страницу с Vite-сервера (HMR) или из готовой сборки. */
const useDevServer = process.env.JARVIS_DEV === "1";
const MARGIN = 24; // отступ от края экрана
const DIM_OPACITY = 0.35; // «полупрозрачный» режим

const SIZES: Record<UiMode, { width: number; height: number }> = {
  compact: { width: 320, height: 168 },
  dialog: { width: 430, height: 470 },
};

let win: BrowserWindow | null = null;
let tray: Tray | null = null;
let prefs: Prefs = { overlayMode: "normal", uiMode: "compact" };
let sidecar: Sidecar | null = null;
let sse: SseClient | null = null;
let apiReady = false;
let quitting = false;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

// ------------------------------------------------------------------ окно

function createWindow(): void {
  const { workArea } = screen.getPrimaryDisplay();
  const size = SIZES[prefs.uiMode];
  const x = Math.round(workArea.x + workArea.width - size.width - MARGIN);
  const y = Math.round(workArea.y + workArea.height - size.height - MARGIN);

  win = new BrowserWindow({
    width: size.width,
    height: size.height,
    x,
    y,
    show: false,
    frame: false, // без рамки и заголовка
    transparent: true, // фон окна полностью прозрачный
    backgroundColor: "#00000000",
    hasShadow: false, // тень Windows нарисовала бы прямоугольник
    resizable: false,
    movable: true,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true, // оверлею не место на панели задач
    alwaysOnTop: true,
    title: "Jarvis Overlay",
    webPreferences: {
      preload: path.join(__dirname, "..", "preload", "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      // Без этого requestAnimationFrame замирает, когда окно не в фокусе,
      // и визуализатор «застывает» на последнем кадре.
      backgroundThrottling: false,
      spellcheck: false,
    },
  });

  // 'screen-saver' — самый высокий уровень: окно остаётся поверх полноэкранных
  // приложений. Просто alwaysOnTop:true этого не даёт.
  win.setAlwaysOnTop(true, "screen-saver");
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

  // Стартовое состояние: окно «сквозное», клики уходят на рабочий стол.
  // forward:true — окно продолжает получать mousemove, по нему и работает
  // определение «курсор над интерактивной зоной».
  win.setIgnoreMouseEvents(true, { forward: true });

  win.once("ready-to-show", () => {
    win?.showInactive(); // showInactive: не отбираем фокус у активного окна
    applyOverlayMode(prefs.overlayMode, false);
    sendToRenderer(CH.uiMode, prefs.uiMode);
  });

  win.on("closed", () => {
    win = null;
  });

  // Никаких новых окон: внешние ссылки не должны открываться внутри оверлея.
  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));

  const builtFile = path.join(__dirname, "..", "renderer", "index.html");

  // Ошибки из renderer-а иначе видны только в DevTools, а в собранном
  // приложении их не увидеть вовсе.
  win.webContents.on("did-finish-load", () => log("info", "Страница загружена"));
  win.webContents.on("did-fail-load", (_event, code, description, url, isMainFrame) => {
    // Vite не запущен — не показываем ERR_CONNECTION_REFUSED, а грузим сборку.
    if (useDevServer && isMainFrame && !win?.webContents.getURL().startsWith("file:")) {
      log("warn", `Vite недоступен (${code} ${description}) — гружу dist/renderer`);
      void win?.loadFile(builtFile);
      return;
    }
    log("error", `Не загрузилась страница ${url}: ${code} ${description}`);
  });
  win.webContents.on("console-message", ((...args: unknown[]) => {
    // Electron менял сигнатуру: (event, level, message, line, sourceId) →
    // (event, details). Поддерживаем обе, чтобы лог не зависел от версии.
    const details = args[1] as { level?: string; message?: string } | number;
    if (details && typeof details === "object") {
      log("debug", `renderer[${details.level}]: ${details.message ?? ""}`);
    } else {
      log("debug", `renderer: ${String(args[2] ?? "")}`);
    }
  }) as never);

  if (useDevServer) {
    void win.loadURL(DEV_URL);
  } else {
    void win.loadFile(builtFile);
  }
}

function sendToRenderer(channel: string, payload: unknown): void {
  if (!win || win.isDestroyed()) return;
  win.webContents.send(channel, payload);
}

/** Обычный режим — 100 % непрозрачности, полупрозрачный — 35 %. */
function applyOverlayMode(mode: OverlayMode, remember = true): void {
  prefs.overlayMode = mode;
  if (remember) savePrefs(prefs);
  if (win && !win.isDestroyed()) {
    win.setOpacity(mode === "dim" ? DIM_OPACITY : 1);
  }
  sendToRenderer(CH.overlayMode, mode);
  rebuildTrayMenu();
}

/** Компактный (только столбики) ↔ диалог (история + ввод). */
function applyUiMode(mode: UiMode, remember = true): void {
  prefs.uiMode = mode;
  if (remember) savePrefs(prefs);
  if (win && !win.isDestroyed()) {
    const bounds = win.getBounds();
    const size = SIZES[mode];
    const { workArea } = screen.getDisplayMatching(bounds);

    // Якорь — правый нижний угол: окно растёт вверх и влево, не «убегая».
    let x = bounds.x + bounds.width - size.width;
    let y = bounds.y + bounds.height - size.height;
    x = Math.min(Math.max(x, workArea.x), workArea.x + workArea.width - size.width);
    y = Math.min(Math.max(y, workArea.y), workArea.y + workArea.height - size.height);

    win.setBounds({ x, y, width: size.width, height: size.height });

    // В диалоге пользователь явно хочет взаимодействовать — снимаем сквозной
    // режим сразу, не дожидаясь mousemove. При сворачивании renderer вернёт его.
    if (mode === "dialog") win.setIgnoreMouseEvents(false);
  }
  sendToRenderer(CH.uiMode, mode);
  rebuildTrayMenu();
}

function toggleOverlayMode(): void {
  applyOverlayMode(prefs.overlayMode === "dim" ? "normal" : "dim");
}

function toggleUiMode(): void {
  applyUiMode(prefs.uiMode === "dialog" ? "compact" : "dialog");
}

function toggleVisible(): void {
  if (!win) return;
  if (win.isVisible()) win.hide();
  else {
    win.showInactive();
    win.setAlwaysOnTop(true, "screen-saver");
  }
}

// --------------------------------------------------------------- трей и меню

function assetPath(name: string): string {
  // dev: <проект>/desktop/assets, prod: <установка>/resources/assets
  if (app.isPackaged) return path.join(process.resourcesPath, "assets", name);
  return path.resolve(__dirname, "..", "..", "assets", name);
}

function trayIcon(): Electron.NativeImage {
  const file = assetPath("tray.png");
  const image = nativeImage.createFromPath(file);
  if (image.isEmpty()) log("warn", `Иконка трея не найдена: ${file}`);
  return image;
}

function createTray(): void {
  const icon = trayIcon();
  if (icon.isEmpty()) return; // без иконки Tray на Windows бросает исключение
  tray = new Tray(icon);
  tray.on("click", toggleVisible);
  rebuildTrayMenu();
}

function rebuildTrayMenu(): void {  if (!tray) return;
  const menu = Menu.buildFromTemplate([
    { label: "Показать / скрыть", click: toggleVisible },
    { type: "separator" },
    {
      label: "Обычный режим",
      type: "radio",
      checked: prefs.overlayMode === "normal",
      click: () => applyOverlayMode("normal"),
    },
    {
      label: "Полупрозрачный режим",
      type: "radio",
      checked: prefs.overlayMode === "dim",
      click: () => applyOverlayMode("dim"),
    },
    { type: "separator" },
    {
      label: "Диалог",
      type: "radio",
      checked: prefs.uiMode === "dialog",
      click: () => applyUiMode("dialog"),
    },
    {
      label: "Компактный вид",
      type: "radio",
      checked: prefs.uiMode === "compact",
      click: () => applyUiMode("compact"),
    },
    { type: "separator" },
    { label: "Прервать речь", click: () => void interrupt() },
    { type: "separator" },
    { label: "Выход", click: () => app.quit() },
  ]);
  tray.setContextMenu(menu);
  tray.setToolTip(`Джарвис — ${apiReady ? "ядро на связи" : "ядро не отвечает"}`);
}

function registerShortcuts(): void {
  const bind = (accelerator: string, action: () => void): void => {
    if (!globalShortcut.register(accelerator, action)) {
      log("warn", `Горячая клавиша занята: ${accelerator}`);
    }
  };
  bind("Control+Alt+J", toggleOverlayMode);
  bind("Control+Alt+U", toggleUiMode);
  bind("Control+Alt+Space", () => void interrupt());
}

// ------------------------------------------------------------- связь с Python

async function waitForApi(timeoutMs = 90_000): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${apiBase}/api/health`, { signal: AbortSignal.timeout(1500) });
      if (res.ok) return true;
    } catch {
      /* ядро ещё грузит модели — это нормально, ждём */
    }
    await sleep(500);
  }
  return false;
}

function publishSidecar(status: SidecarStatus): void {
  sendToRenderer(CH.sidecar, status);
}

async function connectToCore(): Promise<void> {
  publishSidecar({ state: "starting", detail: "ждём /api/health" });
  apiReady = await waitForApi();
  if (!apiReady) {
    log("error", "Python-ядро не поднялось за отведённое время");
    publishSidecar({ state: "error", detail: "ядро не отвечает" });
    rebuildTrayMenu();
    return;
  }

  log("info", `Ядро на связи: ${apiBase}`);
  publishSidecar({ state: "ready", detail: apiBase });
  rebuildTrayMenu();

  sse = new SseClient(
    `${apiBase}/api/stream`,
    (event) => sendToRenderer(CH.event, event as BusEvent),
    (open) => {
      if (!open) {
        log("warn", "Поток событий отключился — переподключаюсь");
        publishSidecar({ state: "starting", detail: "поток событий переподключается" });
      } else {
        log("info", "Поток событий подключён");
        publishSidecar({ state: "ready", detail: apiBase });
      }
    },
  );
  sse.start();
}

async function postJson(pathname: string, body?: unknown): Promise<SendResult> {
  try {
    const res = await fetch(`${apiBase}${pathname}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
      signal: AbortSignal.timeout(5000),
    });
    if (res.ok) return { ok: true };
    const data = (await res.json().catch(() => ({}))) as { error?: string };
    return { ok: false, error: data.error ?? `HTTP ${res.status}` };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
}

async function interrupt(): Promise<void> {
  await postJson("/api/interrupt");
}

// ------------------------------------------------------------------- IPC

function stubPath(): string {
  const candidates = [
    path.join(__dirname, "..", "renderer", "test-voice.wav"), // прод: Vite скопировал public/
    path.join(pythonRoot(), "desktop", "src", "renderer", "public", "test-voice.wav"), // dev
  ];
  return candidates.find((file) => fs.existsSync(file)) ?? candidates[0];
}

function readAudioFile(file: string, label: string): AudioPayload | null {
  try {
    const buffer = fs.readFileSync(file);
    if (buffer.length < 1000) return null;
    return { base64: buffer.toString("base64"), mime: "audio/wav", label };
  } catch {
    return null;
  }
}

function registerIpc(): void {
  ipcMain.handle(CH.getConfig, (): AppConfig => ({
    apiBase,
    overlayMode: prefs.overlayMode,
    uiMode: prefs.uiMode,
    dev: isDev,
    ttsBridge: true,
  }));

  ipcMain.handle(CH.sendMessage, async (_event, raw: unknown): Promise<SendResult> => {
    const text = typeof raw === "string" ? raw.trim() : "";
    if (!text) return { ok: false, error: "пустой текст" };
    return postJson("/api/message", { text });
  });

  ipcMain.handle(CH.reset, () => postJson("/api/reset"));
  ipcMain.on(CH.interrupt, () => void interrupt());

  // Тестовая заглушка: тот же путь, что и у настоящего WAV.
  ipcMain.handle(CH.playStub, (): AudioPayload | null => {
    const file = stubPath();
    const payload = readAudioFile(file, "stub");
    if (!payload) log("warn", `Не нашёл тестовый WAV: ${file}`);
    return payload;
  });

  // Настоящий Piper: ждём, пока Python допишет файл в кэш.
  ipcMain.handle(CH.playTts, async (_event, raw: unknown): Promise<AudioPayload | null> => {
    const text = typeof raw === "string" ? raw : "";
    if (!text) return null;
    const buffer = await waitForWav(text);
    if (!buffer) {
      log("debug", "WAV для фразы не появился — визуализатор возьмёт уровни из SSE");
      return null;
    }
    return { base64: buffer.toString("base64"), mime: "audio/wav", label: "piper" };
  });

  // Самый частый вызов: приходит на каждое движение мыши.
  ipcMain.on(CH.setIgnoreMouse, (_event, raw: unknown) => {
    if (!win || win.isDestroyed()) return;
    win.setIgnoreMouseEvents(raw === true, { forward: true });
  });

  ipcMain.on(CH.setUiMode, (_event, raw: unknown) => {
    applyUiMode(raw === "dialog" ? "dialog" : "compact");
  });

  ipcMain.on(CH.setOverlayMode, (_event, raw: unknown) => {
    applyOverlayMode(raw === "dim" ? "dim" : "normal");
  });

  ipcMain.on(CH.quit, () => app.quit());
}

// ------------------------------------------------------------------ запуск

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!win) return;
    win.showInactive();
    win.setAlwaysOnTop(true, "screen-saver");
  });

  void app.whenReady().then(async () => {
    prefs = loadPrefs();
    registerIpc();
    createWindow();
    createTray();
    registerShortcuts();

    log("info", `Запуск: dev=${isDev}, api=${apiBase}, python=${pythonRoot()}`);

    sidecar = new Sidecar(publishSidecar);
    sidecar.start();
    // Не ждём: окно уже на экране, а модели Vosk/Piper грузятся несколько секунд.
    void connectToCore();
  });
}

app.on("window-all-closed", () => {
  // Оверлей без окна бессмысленен — выходим, как обычное приложение.
  app.quit();
});

// Слабый/кривой драйвер видеокарты иногда роняет renderer при создании
// GL-контекста. Один раз перезапускаемся в программном режиме — на глаз
// разница только в плавности анимации.
app.on("render-process-gone", (_event, _contents, details) => {
  log("error", `Renderer упал: ${details.reason}`);
  if (quitting) return; // при закрытии приложения падение renderer-а — норма
  const alreadySoftware = process.argv.includes("--disable-gpu");
  if (!alreadySoftware && details.reason === "crashed") {
    log("warn", "Перезапуск с --disable-gpu");
    app.relaunch({ args: [...process.argv.slice(1), "--disable-gpu"] });
    app.exit(0);
  }
});

app.on("before-quit", () => {
  if (quitting) return;
  quitting = true;
  globalShortcut.unregisterAll();
  sse?.stop();
  sse = null;
  sidecar?.stop();
  sidecar = null;
  log("info", "Приложение закрывается");
});
