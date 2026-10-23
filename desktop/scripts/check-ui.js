/**
 * Проверка, что renderer действительно монтируется и что разметка
 * click-through на месте. Окно не показывается.
 *
 *     npm run check:ui
 *
 * Сборка раздаётся локальным http-сервером: file:// в некоторых средах
 * (песочницы, CI) блокируется, и тест падал бы не по делу.
 */

const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { app, BrowserWindow, ipcMain } = require("electron");

const rendererDir = path.join(__dirname, "..", "dist", "renderer");
const preload = path.join(__dirname, "..", "dist", "preload", "preload.js");

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".wav": "audio/wav",
  ".png": "image/png",
  ".svg": "image/svg+xml",
};

// Минимальные обработчики: при монтировании App запрашивает конфиг.
ipcMain.handle("app:get-config", () => ({
  apiBase: "http://127.0.0.1:8765",
  overlayMode: "normal",
  uiMode: "compact",
  dev: false,
  ttsBridge: true,
}));

// Эхо, как в main.ts: смена режима приходит от renderer-а и возвращается
// ему же событием. Заодно копим решения click-through.
const ignoreStates = [];
ipcMain.on("ui:set-mode", (event, mode) => {
  event.sender.send("jarvis:ui-mode", mode === "dialog" ? "dialog" : "compact");
});
ipcMain.on("ui:set-overlay-mode", (event, mode) => {
  event.sender.send("jarvis:overlay-mode", mode === "dim" ? "dim" : "normal");
});
ipcMain.on("ui:ignore-mouse", (_event, ignore) => {
  ignoreStates.push(ignore);
});
ipcMain.on("app:quit", () => {});
ipcMain.on("jarvis:interrupt", () => {});

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function serve() {
  return new Promise((resolve) => {
    const server = http.createServer((req, res) => {
      const relative = decodeURIComponent((req.url || "/").split("?")[0]);
      const file = path.join(rendererDir, relative === "/" ? "index.html" : relative);
      const exists = file.startsWith(rendererDir) && fs.existsSync(file);
      if (process.env.JARVIS_CHECK_VERBOSE === "1") {
        console.log(`  GET ${relative} → ${exists ? "200" : "404"}`);
      }
      if (!exists) {
        res.writeHead(404).end("not found");
        return;
      }
      res.writeHead(200, { "Content-Type": MIME[path.extname(file)] || "application/octet-stream" });
      fs.createReadStream(file).pipe(res);
    });
    server.listen(0, "127.0.0.1", () => resolve(server));
  });
}

app.whenReady().then(async () => {
  const server = await serve();
  const url = `http://127.0.0.1:${server.address().port}/`;

  const win = new BrowserWindow({
    width: 320,
    height: 168,
    show: false,
    transparent: true,
    frame: false,
    webPreferences: {
      preload,
      contextIsolation: true,
      nodeIntegration: false,
      // Как и в main.ts: preload импортирует ../shared/ipc, а в песочнице
      // локальный require недоступен.
      sandbox: false,
      backgroundThrottling: false,
    },
  });

  const errors = [];
  win.webContents.on("console-message", (...args) => {
    // Сигнатура менялась: (event, level, message, line, sourceId) → (event, details).
    const second = args[1];
    if (second && typeof second === "object") {
      errors.push(`[${second.level}] ${second.message}`);
    } else {
      errors.push(`[${second}] ${String(args[2] ?? "")}`);
    }
  });
  win.webContents.on("preload-error", (_event, preloadPath, error) => {
    errors.push(`preload ${preloadPath}: ${error.message}`);
  });

  await win.loadURL(url);
  await sleep(1500); // React + первый кадр анимации

  /** Выполняет код в странице и не роняет тест на исключении. */
  const js = async (code) => {
    try {
      return { ok: true, value: await win.webContents.executeJavaScript(code) };
    } catch (error) {
      return { ok: false, value: String(error.message || error) };
    }
  };

  const body = await js("document.body.innerHTML.slice(0, 300)");
  if (!body.ok || body.value.length < 10) console.log("body:", body);
  const rootInfo = await js(
    "({ scripts: document.scripts.length, root: document.getElementById('root')?.innerHTML.length ?? -1, hasBridge: typeof window.jarvis })",
  );
  console.log("root:    ", rootInfo.value);

  const probe = await js(`(() => {
    const card = document.querySelector('.card');
    const canvas = document.querySelector('canvas.visualizer');
    return {
      mounted: !!card,
      canvas: !!canvas,
      canvasSize: canvas ? [canvas.width, canvas.height] : null,
      compactHit: card ? card.hasAttribute('data-hit') : null,
      expandButton: !!document.querySelector('.expand[data-hit]'),
      bodyBg: getComputedStyle(document.body).backgroundColor
    };
  })()`);
  if (!probe.ok) {
    console.log("ПРОВАЛ: проба renderer-а не выполнилась:", probe.value);
    server.close();
    app.exit(1);
    return;
  }

  // Переключаемся в диалог так же, как это делает пользователь.
  const clicked = await js("(() => { const b = document.querySelector('.expand'); if (!b) return false; b.click(); return true; })()");
  await sleep(500);

  const dialog = await js(`(() => ({
    messages: !!document.querySelector('.messages'),
    input: !!document.querySelector('.input'),
    cardHit: document.querySelector('.card')?.hasAttribute('data-hit'),
    tools: document.querySelectorAll('.dialog__head .tool').length
  }))()`);

  // Click-through: имитируем движение мыши по центру окна.
  // В диалоге под курсором карточка с data-hit → окно должно принимать мышь.
  ignoreStates.length = 0;
  await js(`(() => {
    window.dispatchEvent(new MouseEvent('mousemove', { clientX: 160, clientY: 84, bubbles: true }));
    return true;
  })()`);
  await sleep(300);
  const dialogIgnore = [...ignoreStates];

  // Сворачиваем обратно: под курсором та же точка, но карточка уже
  // не участвует в hit-testing → окно снова должно стать сквозным.
  await js("window.jarvis.setUiMode('compact'); true");
  await sleep(500);
  ignoreStates.length = 0;
  await js(`(() => {
    window.dispatchEvent(new MouseEvent('mousemove', { clientX: 160, clientY: 84, bubbles: true }));
    return true;
  })()`);
  await sleep(300);
  const compactIgnore = [...ignoreStates];

  console.log("renderer:", probe.value);
  console.log("click:   ", clicked.value);
  console.log("dialog:  ", dialog.value);
  console.log("click-through: dialog →", dialogIgnore, "| compact →", compactIgnore);
  if (errors.length) console.log("console warnings/errors:", errors);

  const p = probe.value;
  const d = dialog.ok ? dialog.value : {};
  const ok =
    p.mounted &&
    p.canvas &&
    p.canvasSize[0] > 0 &&
    p.compactHit === false && // в компакте карточка не должна ловить мышь
    p.expandButton &&
    d.messages &&
    d.input &&
    d.cardHit === true && // в диалоге — должна
    dialogIgnore.includes(false) && // в диалоге окно принимает мышь
    compactIgnore.includes(true); // в компакте — сквозное

  console.log(ok ? "OK: UI монтируется, click-through работает." : "ПРОВАЛ: см. выше.");
  server.close();
  app.exit(ok ? 0 : 1);
});
