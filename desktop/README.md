# Jarvis Overlay — настольный UI для «Джарвиса»

Отдельное приложение на **Electron + TypeScript + React**. Рисует поверх рабочего
стола полупрозрачную карточку с визуализатором в стиле Winamp, умеет показывать
диалог и отправлять команды в Python-ядро. **Python-часть не переписана** — она
запускается как дочерний процесс (сайдкар) и общается по уже существующему
HTTP/SSE API (`api.py` — `CoreAPI`, `127.0.0.1:8765`).

---

## 1. Структура

```
desktop/
├── package.json              скрипты: dev / build / dist
├── tsconfig.json             общий конфиг (редактор)
├── tsconfig.main.json        main + preload → CommonJS в dist/
├── tsconfig.renderer.json    проверка типов renderer-а (без emit)
├── vite.config.mts           сборка renderer-а (React, base: "./")
├── electron-builder.yml      установщик NSIS + упаковка Python-сайдкара
├── assets/                   icon.png, tray.png
├── scripts/gen-test-audio.py генерирует тестовый WAV голосом Piper
└── src/
    ├── shared/
    │   └── ipc.ts            ЕДИНЫЙ контракт: имена каналов + типы
    ├── main/
    │   ├── main.ts           окно, режимы, трей, хоткеи, IPC
    │   ├── config.ts         пути, сохранённые настройки, запуск Python
    │   ├── sidecar.ts        spawn/перезапуск/остановка Python
    │   ├── sse.ts            клиент /api/stream (события ядра)
    │   ├── ttsAudio.ts       готовый WAV от Piper из кэша → base64
    │   └── log.ts            лог в консоль + файл
    ├── preload/
    │   └── preload.ts        contextBridge → window.jarvis
    └── renderer/
        ├── index.html        CSP, точка входа
        ├── main.tsx          createRoot
        ├── App.tsx           состояние, click-through, режимы
        ├── audio.ts          AudioContext + AnalyserNode
        ├── status.ts         подписи/цвета состояний
        ├── styles.css        прозрачный фон, стеклянная карточка
        ├── components/
        │   ├── Visualizer.tsx  Canvas + requestAnimationFrame
        │   └── DialogPanel.tsx история + ввод
        └── public/
            └── test-voice.wav  заглушка для визуализатора
```

Почему main/preload собираются `tsc`, а renderer — Vite: main и preload живут в
Node-окружении и им нужен CommonJS без бандлинга; renderer — обычное веб-приложение,
и Vite даёт мгновенный HMR.

---

## 2. Click-through: прозрачное окно, которое всё-таки принимает клики

Задача противоречивая: окно должно пропускать клики на рабочий стол, но поле
ввода и кнопки должны работать.

Есть три способа, и они не равнозначны:

| Способ | Плюсы | Минусы |
|---|---|---|
| Два окна: визуализатор (сквозной) + диалог (обычный) | полная независимость | два окна в списке, синхронизация позиции, второй `alwaysOnTop` |
| `setIgnoreMouseEvents` + hit-test по `mousemove` | одно окно, гибко | нужен `forward: true`, есть задержка в один кадр |
| Глобальная горячая клавиша «включить мышь» | просто | неудобно: надо помнить про режим |

Выбран второй. Механика:

1. Окно создаётся сразу сквозным:
   ```ts
   win.setIgnoreMouseEvents(true, { forward: true });
   ```
   `forward: true` — ключевой флаг: окно **продолжает получать `mousemove`**,
   хотя клики уходят дальше. Без него hit-test невозможен.

2. Renderer на каждом `mousemove` смотрит, что под курсором:
   ```ts
   const el = document.elementFromPoint(event.clientX, event.clientY);
   const interactive = el instanceof Element && el.closest("[data-hit]") !== null;
   if (interactive !== ignoring) { ignoring = interactive; window.jarvis.setIgnoreMouse(ignoring); }
   ```
   Состояние меняется только на переходах — иначе IPC захлебнётся.

3. Что считать интерактивным, решает **CSS**, а не JS: атрибут `data-hit` стоит
   на карточке (в режиме диалога) и на кнопках (в компактном режиме). В компакте
   `.card { pointer-events: none }`, поэтому `elementFromPoint` «видит» сквозь неё
   и возвращает кнопку или `body`.

Подводные камни, на которые легко наступить:

* **Первый клик после наведения может пройти мимо.** `mousemove` → IPC →
  `setIgnoreMouseEvents` — это асинхронная цепочка. На практике курсор успевает
  сдвинуться несколько раз, но если «ткнуть» сразу после появления окна под
  курсором — клик уйдёт на рабочий стол. Лечится расширением хитбокса (padding)
  или отдельным «якорем» по `mouseenter`.
* **`-webkit-app-region: drag` съедает клики.** Шапка диалога — ручка для
  перетаскивания, поэтому все кнопки внутри помечены `no-drag`.
* **`pointer-events: none` у родителя не мешает ребёнку** — если у кнопки стоит
  `auto`, она остаётся кликабельной. На этом и построен компактный режим.
* **`mouseleave` ненадёжен** при сквозном окне, поэтому дополнительно ставим
  `ignore = true` при уходе курсора из окна.

---

## 3. Два режима

| Режим | Что делает | Как переключить |
|---|---|---|
| Обычный | `win.setOpacity(1)` | `Ctrl+Alt+J`, трей, кнопка ◐ |
| Полупрозрачный | `win.setOpacity(0.35)` + более прозрачный фон карточки | `Ctrl+Alt+J`, трей, кнопка ◐ |
| Компактный | 320×168, только столбики | `Ctrl+Alt+U`, кнопка ▾ |
| Диалог | 430×470, история + ввод | `Ctrl+Alt+U`, кнопка ⌃ |

Окно при смене размера «растёт» от правого нижнего угла, чтобы не уезжать за
экран. Выбранные режимы сохраняются в `%APPDATA%/jarvis-desktop/overlay.json`.

Дополнительно: `Ctrl+Alt+Space` — прервать речь, `Esc` — прервать/свернуть.

---

## 4. Как подключить настоящий Piper, не трогая Python

Визуализатору нужен сигнал. Вариантов четыре — от «совсем без правок» до
«идеально синхронно».

### A. Уровни из SSE (работает уже сейчас, 0 правок)

`tts.py` во время проигрывания фразы публикует `emit_level(value, "tts")` ~30 раз
в секунду. Это уже есть в `/api/stream`, и Electron просто рисует по нему
синтетический спектр. Ничего не меняется, работает всегда — в том числе для
edge-tts и SAPI.

### B. Настоящий спектр из кэша Piper (реализовано, 0 правок)

Piper складывает результат в `data/tts_cache/<sha1>.wav`, где ключ считается так
(`tts.py: _cache_key` + `_speak_piper`):

```python
sha1("|".join(["piper", text, str(model_path), str(PIPER_SPEED), str(PIPER_VOLUME)]))[:20]
```

`src/main/ttsAudio.ts` считает **точно такой же** хеш в Node, ждёт появления файла
(синтез ~1.7 с, Python пишет `.part.wav` → `rename`) и отдаёт base64 в renderer.
Дальше `AudioContext.decodeAudioData` → `AnalyserNode` — настоящий спектр голоса.

Тонкости, из-за которых это вообще работает:

* числа берутся из `jarvis_settings.json` **как текст**: Python хеширует `str(1.0)`
  = `"1.0"`, а `JSON.parse` в JS превратил бы это в `1`;
* путь к модели тоже берётся из настроек дословно (там абсолютный путь);
* нужен `TTS_USE_CACHE: true` и `TTS_ENGINE: "piper"`.

Звук при этом играет Python, а Electron подключает граф с `gain = 0.0001`
(`silent: true`) — анализатор стоит **до** регулятора громкости, поэтому спектр
есть, а двойного звука нет.

### C. Если понадобится полный контроль (10 строк в `api.py`)

Когда текст перед озвучкой чистится и хеш не совпадает, честнее отдать байты
напрямую. Это **добавление**, а не переписывание:

```python
if path == "/api/tts.wav":
    text, error, status = self._extract_text(payload)
    ...
    data = tts.synthesize_bytes(text)   # тонкая обёртка над _speak_piper без play
    self._send(data, "audio/wav")
```

### D. Стриминг «на лету»

`piper-tts` умеет писать в файл-объект. Можно отдавать чанки через WebSocket и
кормить `AudioWorklet` — тогда визуализация совпадёт с речью с точностью до
буфера. Это следующий шаг, когда A/B перестанет хватать.

---

## 5. Грабли Windows

**Прозрачность**
* `transparent: true` + `backgroundColor: "#00000000"` + `hasShadow: false`.
  Тень Windows рисует прямоугольник — с ней весь смысл пропадает.
* Фон `html/body` обязан быть `transparent`, иначе получите чёрный квадрат.
* Прозрачное окно **нельзя** ресайзить мышью за край — поэтому `resizable: false`
  и переключение размеров кодом.
* `backdrop-filter: blur()` на прозрачном окне не размывает рабочий стол
  (у Chromium нет доступа к пикселям за окном). Если нужен настоящий акрил —
  `backgroundMaterial: "acrylic"` (Windows 11), но он конфликтует с `transparent`.

**alwaysOnTop**
* `alwaysOnTop: true` в конструкторе недостаточно: поверх полноэкранных игр и
  UWP-приложений держит только уровень `screen-saver`:
  `win.setAlwaysOnTop(true, "screen-saver")`.
* Некоторые приложения (демонстрация экрана, защищённый вывод DRM) всё равно
  окажутся выше — это ограничение ОС, а не Electron.

**Click-through**
* `forward: true` работает на Windows и macOS, на Linux — нет (там нужен
  `setShape`).
* Не вызывайте `setIgnoreMouseEvents` на каждое движение — только на переходах.
* `skipTaskbar: true` + `frame: false` = окно не найти в Alt+Tab. Поэтому в
  приложении есть трей и глобальные хоткеи, иначе его не закрыть.

**Упаковка Python**
* `electron-builder.yml` кладёт `*.py` в `resources/python`. Модели Vosk (46 МБ)
  и Piper (63 МБ) **не** пакуются — они докачиваются при первом запуске, иначе
  установщик распухает до сотен мегабайт.
* Интерпретатор не встраивается. Кандидаты перебираются по порядку:
  `JARVIS_PYTHON` → `.venv/Scripts/python.exe` проекта → `python` → `py -3`.
  Перед запуском каждый проверяется пробой `python -c "import requests"`:
  в PATH часто оказывается «чужой» python (менеджеры версий, Store, другой
  проект), и без проверки сайдкар падал бы на `import requests` по кругу.
  В логе видно, кто именно выбран и кто отсеян.
* `stdin` сайдкара — открытый канал, а не `/dev/null`: в режиме `--mode text`
  ядро читает `input()`, и на закрытом stdin получало бы EOF в бесконечном
  цикле, съедая процессор.
* Убивать сайдкар нужно деревом: `taskkill /pid <pid> /T /F`, иначе останутся
  висящие потоки аудио.
* `windowsHide: true` при `spawn`, иначе на секунду мигает консоль.

**SmartScreen и антивирус**
* Неподписанный `.exe` из NSIS встретит «Windows защитила ваш компьютер».
  Лечится только подписью (EV-сертификат) либо «Подробнее → Выполнить всё равно».
* `spawn` питона из Electron некоторые антивирусы считают подозрительным —
  помогает установка в `%LOCALAPPDATA%` (per-user, по умолчанию) и подпись.

**DPI и несколько мониторов**
* Позиция считается от `screen.getPrimaryDisplay().workArea` — при переносе окна
  на монитор с другим масштабом координаты пересчитываются заново.
* Визуализатор учитывает `devicePixelRatio` (ограничен 2), иначе на 4K столбики
  выглядят мылом.

---

## 6. Запуск

```bash
cd desktop
npm install

npm run dev        # Vite + tsc --watch + Electron с HMR
npm run build      # прод-сборка в dist/
npm start          # собрать и запустить
npm run dist       # установщик NSIS в release/
```

Полезные переменные окружения:

| Переменная | Смысл |
|---|---|
| `JARVIS_NO_SIDECAR=1` | не запускать Python (проверка одного UI) |
| `JARVIS_PYTHON` | явный путь к интерпретатору |
| `JARVIS_PYTHON_ARGS` | доп. аргументы, например `--no-tts --mode text` |
| `JARVIS_API` | другой адрес ядра (по умолчанию `http://127.0.0.1:8765`) |
| `JARVIS_DEV_URL` | адрес dev-сервера Vite |

Проверка визуализатора без ядра: `JARVIS_NO_SIDECAR=1 npm run dev`, открыть
диалог и нажать ♪ — проиграется `test-voice.wav` (синтезирован Piper-ом
скриптом `scripts/gen-test-audio.py`).

### Автопроверки

```bash
npm run typecheck    # типы main + renderer
npm run check:bridge # совпадает ли sha1-ключ кэша с tts.py (мост аудио)
npm run check:ui     # монтируется ли React и работает ли click-through
```

`check:ui` поднимает окно без показа, раздаёт `dist/renderer` локальным http,
щёлкает по кнопке «развернуть» и проверяет по очереди: карточка смонтирована,
canvas имеет ненулевой размер, в компакте карточка **не** ловит мышь, в диалоге
**ловит**, и что IPC `ui:ignore-mouse` получает `false`/`true` соответственно.

### Если что-то не запускается

| Симптом | Причина и лечение |
|---|---|
| `TypeError: Cannot read properties of undefined (reading 'isPackaged')` | в окружении стоит `ELECTRON_RUN_AS_NODE=1` — electron работает как обычный Node. Запускайте без этой переменной. |
| `GPU process exited unexpectedly` → `GPU process isn't usable. Goodbye.` | драйвер/виртуалка не даёт GL-контекст. Запустите с `--disable-gpu` или `--no-sandbox --disable-gpu-sandbox`; приложение само перезапустится с `--disable-gpu` после первого падения renderer-а. |
| `Unable to load preload script: module not found: ../shared/ipc` | preload обязан работать с `sandbox: false` — в песочнице локальный `require` недоступен. В `main.ts` это уже выставлено. |
| «ядро не на связи», в логе `ModuleNotFoundError: No module named 'requests'` | `python` из PATH — не тот интерпретатор. Укажите `JARVIS_PYTHON=...\Python314\python.exe` или создайте `.venv` в проекте. |
| окно есть, но клики проходят насквозь всегда | проверьте, что в разметке есть `data-hit`, а `pointer-events` у нужного элемента не `none`. |
| пустое прозрачное окно без содержимого | `ERR_CONNECTION_REFUSED` на 5273: запущен `JARVIS_DEV=1`, а Vite не поднят. `npm run dev` либо просто `npm start`. |

Логи: консоль + `%APPDATA%/jarvis-desktop/logs/jarvis-overlay.log`
(в собранном приложении консоли нет, файл — единственный источник правды).

---

## 7. Как это связано с ядром

```
Electron main ──spawn──▶ python main.py --no-desktop --api-port 8765
      │                            │
      │◀──── GET /api/health ──────┤  (ждём готовности моделей)
      │◀──── GET /api/stream ──────┤  SSE: message / state / level
      │──── POST /api/message ────▶│  текст из поля ввода
      │──── POST /api/interrupt ──▶│  прервать речь
      │──── POST /api/reset ──────▶│  сбросить контекст
      │
      └── читает data/tts_cache/<sha1>.wav (Piper) ──▶ base64 ──▶ AnalyserNode
```

Голосовой ввод по-прежнему делает Python (Vosk + микрофон): Electron — это
только глаза и клавиатура, он не отбирает микрофон и не мешает wake-word-циклу.
