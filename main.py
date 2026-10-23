# -*- coding: utf-8 -*-
"""Точка входа Джарвиса.

Примеры запуска::

    python main.py                 # смешанный режим (голос + текст)
    python main.py --text          # только текст
    python main.py --voice         # только голос
    python main.py --no-tts        # без озвучки
    python main.py --no-wake       # голос без слова-активатора «Джарвис»
    python main.py --check         # диагностика окружения
    python main.py --list-voices   # доступные голоса edge-tts
    python main.py --background    # фоновый запуск без консоли

Фоновый запуск (ярлык «Джарвис» на рабочем столе):
    ярлык запускает ``Джарвис.exe main.py --background`` — это копия
    ``pythonw.exe`` рядом с проектом. Консоли нет, поэтому текстовый ввод не
    спрашивается, лог пишется в ``data/jarvis.log``, а управление идёт голосом
    и через веб-интерфейс (он же открывается браузером).

    Копия под именем «Джарвис.exe» нужна ради диспетчера задач: он показывает
    имя файла, а не заголовок окна, поэтому обычный ``pythonw.exe`` выглядел бы
    как «pythonw.exe». Ярлык и копию создаёт скрипт ``make_shortcut.py``.

Список всех команд для пользователя:
    ``python make_commands_list.py`` пишет ``Документы/Джарвис — команды.txt``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Чтобы модули проекта импортировались и при сборке в exe, и при запуске из любой папки.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys._MEIPASS)          # type: ignore[attr-defined]
else:
    BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarvis",
        description="Джарвис v3.2 — голосовой ассистент для Windows",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--mode", choices=["text", "voice", "mixed"],
                        help="режим ввода (по умолчанию из настроек)")
    parser.add_argument("--text", action="store_true", help="только текстовый ввод")
    parser.add_argument("--voice", action="store_true", help="только голосовой ввод")
    parser.add_argument("--no-tts", action="store_true", help="отключить озвучку")
    parser.add_argument("--no-wake", action="store_true",
                        help="не требовать слово «Джарвис» перед командой")
    parser.add_argument("--vosk-model",
                        help="папка или имя модели Vosk (по умолчанию из настроек)")
    parser.add_argument("--voice-name", help="голос edge-tts, например ru-RU-SvetlanaNeural")
    parser.add_argument("--telegram", action="store_true",
                        help="включить Telegram-бота (нужен TELEGRAM_TOKEN)")
    parser.add_argument("--no-ui", action="store_true",
                        help="не поднимать веб-интерфейс с ядром и диалогом")
    parser.add_argument("--desktop", action="store_true",
                        help="запустить настольный оверлей (Electron): собирает "
                             "desktop/ при необходимости и открывает окно поверх "
                             "рабочего стола")
    parser.add_argument("--rebuild-desktop", action="store_true",
                        help="пересобрать оверлей перед запуском")
    parser.add_argument("--ui-port", type=int,
                        help="порт веб-интерфейса (по умолчанию из настроек)")
    parser.add_argument("--no-browser", action="store_true",
                        help="не открывать браузер при старте интерфейса")
    parser.add_argument("--background", action="store_true",
                        help="фоновый запуск без консоли (режим ярлыка на рабочем "
                             "столе): лог в файл, управление голосом и через интерфейс")
    parser.add_argument("--verbose", action="store_true", help="подробный лог")
    parser.add_argument("--check", action="store_true", help="проверить окружение и выйти")
    parser.add_argument("--list-voices", action="store_true",
                        help="показать доступные русские голоса edge-tts и выйти")
    return parser


def run_check() -> int:
    """Диагностика: что установлено, что доступно, что нужно доустановить."""
    import config

    print("=" * 64)
    print("  ДИАГНОСТИКА ДЖАРВИСА")
    print("=" * 64)
    print(f"Python: {sys.version.split()[0]}  ({sys.executable})")
    print(f"Папка проекта: {config.BASE_DIR}")
    print(f"Настройки: {config.SETTINGS_FILE} "
          f"({'есть' if config.SETTINGS_FILE.exists() else 'нет, будут значения по умолчанию'})")
    print("-" * 64)

    print("Компоненты:")
    for label, available in config.feature_report():
        print(f"  [{'✓' if available else ' '}] {label}")
    print("-" * 64)

    print("Конфигурация:")
    print(f"  Режим ввода:        {config.DEFAULT_INPUT_MODE}")
    print(f"  Распознавание:      Vosk «{config.VOSK_MODEL_NAME}»")
    try:
        from speech import model_path as vosk_model_path
        vosk_ready = vosk_model_path().exists()
    except Exception:  # noqa: BLE001
        vosk_ready = config.VOSK_MODEL_PATH.exists()
    print(f"  Модель на диске:    {'да' if vosk_ready else 'нет, скачается при запуске'}")
    try:
        from tts import piper_model_ready
        piper_ready = piper_model_ready()
    except Exception:  # noqa: BLE001
        piper_ready = config.PIPER_MODEL.exists()
    print(f"  Озвучка:            {config.TTS_ENGINE} / {Path(config.PIPER_MODEL).stem}")
    print(f"  Голос Piper:        {'да' if piper_ready else 'нет, скачается при запуске'}")
    print(f"  Нейросеть:          {config.GEMINI_MODEL} (Gemini API)")
    print(f"  Telegram:           {'включён' if config.TELEGRAM_ENABLED else 'выключен'}")
    print("-" * 64)

    print("Gemini (мозг):")
    try:
        from brain import Brain
        ok, message = Brain().check(force=True)
        print(f"  [{'✓' if ok else ' '}] {message}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [ ] Не удалось проверить: {exc}")
    print("-" * 64)

    print("Настольный оверлей (--desktop):")
    try:
        import overlay
        info = overlay.status()
        print(f"  Node.js:   {info['node'] or 'не найден (нужен Node 20+)'}")
        print(f"  npm:       {info['npm'] or 'не найден'}")
        if info["electron"]:
            electron_text = "установлен"
        elif info["deps"]:
            electron_text = "скачается при первом запуске"
        else:
            electron_text = "нет (npm install в desktop/)"
        print(f"  Electron:  {electron_text}")
        print(f"  Сборка:    {'есть' if info['built'] else 'нет'}"
              f"{', устарела' if info['stale'] else ''}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [ ] Не удалось проверить: {exc}")
    print("-" * 64)

    problems = []
    if not config.FEATURES["vosk"]:
        problems.append("pip install vosk            (распознавание речи)")
    if not config.FEATURES["piper"]:
        problems.append("pip install piper-tts        (основная озвучка)")
    if not config.FEATURES["edge_tts"]:
        problems.append("pip install edge-tts          (нейронный голос)")
    if not config.FEATURES["sounddevice"]:
        problems.append("pip install sounddevice       (микрофон)")
    if not config.FEATURES["pycaw"]:
        problems.append("pip install pycaw comtypes    (громкость)")
    if not config.FEATURES["PIL"]:
        problems.append("pip install pillow            (скриншоты)")

    if problems:
        print("Рекомендуется установить:")
        for item in problems:
            print(f"  {item}")
    else:
        print("Все основные компоненты на месте.")

    print()
    print("Ключ Gemini задаётся в jarvis_settings.json -> GEMINI_API_KEY.")
    print("Бесплатный ключ можно получить в Google AI Studio: https://aistudio.google.com/apikey")
    return 0


def run_list_voices() -> int:
    """Показывает локальные голоса Piper и, если есть сеть, голоса edge-tts."""
    import config

    try:
        from download_voices import VOICES
    except Exception:  # noqa: BLE001
        VOICES = {}

    if VOICES:
        print("Русские голоса Piper (локальные, работают офлайн):")
        for key, info in VOICES.items():
            marker = " *" if Path(config.PIPER_MODEL).stem.startswith(
                Path(info["path"]).name) else ""
            print(f"  {key:<8} {info['title']}{marker}")
        print("\nСкачать другой голос:  python download_voices.py irina")
        print("Выбрать его:           PIPER_MODEL в jarvis_settings.json")
        print("  * — выбран сейчас")

    if not config.FEATURES["edge_tts"]:
        return 0
    try:
        import tts
        voices = tts.list_edge_voices("ru")
    except Exception as exc:  # noqa: BLE001
        print(f"\nГолоса edge-tts недоступны (резервный движок): {exc}")
        return 0
    if voices:
        print("\nРусские голоса edge-tts (резерв, нужен интернет):")
        for voice in voices:
            print(f"  {voice['name']:<28} {voice['gender']}")
    return 0


def main() -> int:
    args = build_parser().parse_args()

    import config

    if args.check:
        return run_check()
    if args.list_voices:
        return run_list_voices()

    # Создаём файл настроек при первом запуске — так их проще найти и править.
    if config.write_default_settings():
        print(f"[i] Создан файл настроек: {config.SETTINGS_FILE}")

    # Папка скриншотов создаётся заранее: пользователь сразу видит, куда
    # ложатся снимки, ещё до первой команды «сделай скриншот».
    try:
        config.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[i] Не удалось создать папку скриншотов: {exc}")

    if args.background:
        # Режим ярлыка на рабочем столе: консоли нет — см. Jarvis.background.
        config.BACKGROUND_MODE = True
        config.LOG_TO_FILE = True
    if args.verbose:
        config.VERBOSE = True
    if args.vosk_model:
        candidate = Path(args.vosk_model)
        # Голое имя модели («vosk-model-ru-0.42») ищем в data/models, а не в
        # корне проекта: именно туда распаковываются модели.
        if not candidate.is_absolute() and candidate.parent == Path("."):
            candidate = config.MODELS_DIR / candidate
        config.VOSK_MODEL_PATH = candidate
        config.VOSK_MODEL_NAME = candidate.name
    if args.voice_name:
        config.TTS_VOICE = args.voice_name
    if args.telegram:
        config.TELEGRAM_ENABLED = True

    mode = args.mode
    if args.text:
        mode = "text"
    elif args.voice:
        mode = "voice"
    mode = mode or config.DEFAULT_INPUT_MODE

    from jarvis_core import Jarvis

    jarvis = Jarvis(
        mode=mode,
        enable_tts=not args.no_tts,
        wake_required=False if args.no_wake else None,
        background=config.BACKGROUND_MODE,
    )

    ui = None
    # Оверлею нужен HTTP-API, поэтому с --desktop веб-интерфейс поднимается всегда.
    want_ui = (not args.no_ui and config.WEB_UI_ENABLED) or args.desktop
    if args.desktop and args.no_ui:
        print("[i] --desktop требует API ядра: --no-ui проигнорирован")
    if want_ui and not config.WEB_UI_ENABLED:
        print("[i] WEB_UI_ENABLED выключен в настройках — оверлею некуда подключаться")

    if want_ui and config.WEB_UI_ENABLED:
        from webui import WebUI

        ui = WebUI(jarvis, port=args.ui_port,
                   open_browser=config.WEB_UI_OPEN_BROWSER and not args.no_browser)
        ui.start()
        # Команда «выключайся» гасит и ядро, и интерфейс — а не только цикл.
        jarvis.add_shutdown_hook(ui.stop)

    if args.desktop:
        # Сборка и запуск окна: Python остаётся мозгом, Electron — только UI.
        import overlay

        process = overlay.run(ui.url if ui is not None else "", rebuild=args.rebuild_desktop)
        if process is not None:
            jarvis.add_shutdown_hook(process.stop)
        else:
            print("[!] Оверлей не запустился — работаю как обычно, через браузер")

    try:
        jarvis.run()
    except Exception as exc:  # noqa: BLE001
        print(f"\n[Критическая ошибка] {exc}")
        return 1
    finally:
        if ui is not None:
            ui.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
