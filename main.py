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
        description="Джарвис v3.0 — голосовой ассистент для Windows",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--mode", choices=["text", "voice", "mixed"],
                        help="режим ввода (по умолчанию из настроек)")
    parser.add_argument("--text", action="store_true", help="только текстовый ввод")
    parser.add_argument("--voice", action="store_true", help="только голосовой ввод")
    parser.add_argument("--no-tts", action="store_true", help="отключить озвучку")
    parser.add_argument("--no-wake", action="store_true",
                        help="не требовать слово «Джарвис» перед командой")
    parser.add_argument("--whisper-model",
                        help="модель Whisper: tiny, base, small, medium, large-v3")
    parser.add_argument("--voice-name", help="голос edge-tts, например ru-RU-SvetlanaNeural")
    parser.add_argument("--telegram", action="store_true",
                        help="включить Telegram-бота (нужен TELEGRAM_TOKEN)")
    parser.add_argument("--no-ui", action="store_true",
                        help="не поднимать веб-интерфейс с ядром и диалогом")
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
    print(f"  Whisper:            {config.WHISPER_MODEL} "
          f"({config.WHISPER_DEVICE}/{config.WHISPER_COMPUTE_TYPE})")
    print(f"  Озвучка:            {config.TTS_ENGINE} / {config.TTS_VOICE}")
    print(f"  LLM:                {config.OLLAMA_MODEL} @ {config.OLLAMA_HOST}")
    print(f"  Telegram:           {'включён' if config.TELEGRAM_ENABLED else 'выключен'}")
    print("-" * 64)

    print("Ollama:")
    try:
        from brain import Brain
        ok, message = Brain().check(force=True)
        print(f"  [{'✓' if ok else ' '}] {message}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [ ] Не удалось проверить: {exc}")
    print("-" * 64)

    problems = []
    if not config.FEATURES["faster_whisper"]:
        problems.append("pip install faster-whisper   (распознавание речи)")
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
    print("Если Ollama ещё не установлена: https://ollama.com/download")
    print(f"После установки выполните:  ollama pull {config.OLLAMA_MODEL}")
    return 0


def run_list_voices() -> int:
    import config
    import tts

    if not config.FEATURES["edge_tts"]:
        print("edge-tts не установлен: pip install edge-tts")
        return 1
    try:
        voices = tts.list_edge_voices("ru")
    except Exception as exc:  # noqa: BLE001
        print(f"Не удалось получить список голосов: {exc}")
        return 1
    if not voices:
        print("Русские голоса не найдены (проверьте интернет).")
        return 1
    print("Доступные русские голоса edge-tts:")
    for voice in voices:
        print(f"  {voice['name']:<28} {voice['gender']}")
    print("\nУкажите нужный в jarvis_settings.json -> TTS_VOICE")
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
    if args.whisper_model:
        config.WHISPER_MODEL = args.whisper_model
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
    if not args.no_ui and config.WEB_UI_ENABLED:
        from webui import WebUI

        ui = WebUI(jarvis, port=args.ui_port,
                   open_browser=config.WEB_UI_OPEN_BROWSER and not args.no_browser)
        ui.start()
        # Команда «выключайся» гасит и ядро, и интерфейс — а не только цикл.
        jarvis.add_shutdown_hook(ui.stop)

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
