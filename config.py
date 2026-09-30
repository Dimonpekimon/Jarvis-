# -*- coding: utf-8 -*-
"""Конфигурация Джарвиса v3.0.

Все параметры ниже можно переопределить в файле ``jarvis_settings.json``
(он создаётся автоматически при первом запуске рядом с этим модулем).
Формат — обычный JSON, ключи совпадают с именами констант::

    {
        "WHISPER_MODEL": "medium",
        "TTS_VOICE": "ru-RU-SvetlanaNeural",
        "OLLAMA_MODEL": "mistral"
    }

Файл читается при импорте модуля, поэтому менять настройки нужно до запуска.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

# Windows не всегда позволяет создавать симлинки (нужен режим разработчика),
# из-за чего huggingface_hub скачивает модели «пустыми» — файлы по 0 байт.
# Заставляем его копировать файлы вместо ссылок.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# =====================================================================
#  ПУТИ
# =====================================================================
BASE_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BASE_DIR / "jarvis_settings.json"
DATA_DIR = BASE_DIR / "data"
CACHE_DIR = DATA_DIR / "tts_cache"
MODELS_DIR = DATA_DIR / "models"
VOICES_DIR = DATA_DIR / "voices"
LOG_FILE = DATA_DIR / "jarvis.log"

NOTES_FILE = DATA_DIR / "notes.txt"
REMINDERS_FILE = DATA_DIR / "reminders.json"

for _d in (DATA_DIR, CACHE_DIR, MODELS_DIR, VOICES_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


# =====================================================================
#  РЕЖИМ РАБОТЫ
# =====================================================================
DEFAULT_INPUT_MODE = "mixed"        # text | voice | mixed
ENABLE_TTS = True                   # озвучивать ответы
WAKE_WORD_REQUIRED = True           # в голосовом режиме ждать слово «Джарвис»
VERBOSE = False                     # подробный лог
BACKGROUND_MODE = False             # фоновый запуск без консоли (ярлык на рабочем столе)
LOG_TO_FILE = False                 # дублировать лог в data/jarvis.log (в фоне — да)
THINKING_FILLER = True              # говорить «секунду, сэр» перед сетевыми командами

# =====================================================================
#  АУДИО / VAD
# =====================================================================
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_MS = 100                      # размер блока захвата звука, мс
WAKE_WINDOW_SEC = 8.0               # максимум записи ПЕРВОЙ фразы после обращения
MAX_COMMAND_SEC = 15.0              # максимум записи одной команды
SILENCE_SEC = 0.5                   # столько тишины = конец фразы
MIN_SPEECH_SEC = 0.3                # фраза короче — игнорируется
VAD_RMS_THRESHOLD = 0.010           # абсолютный порог «тишины» (0..1)
MIC_CALIBRATION_SEC = 2.0           # длительность калибровки фонового шума
MIC_CLEAR_GUARD = 0.3               # пауза после конца озвучки перед записью

WAKE_WORDS = ["джарвис", "джарвиз", "jarvis", "дарвис", "жарвис", "джавис", "джарви"]
WAKE_WORD_CUTOFF = 0.78             # порог нечёткого совпадения (0..1)
WAKE_SILENCE_SEC = 0.6              # тишина, завершающая фразу с обращением

# Отклик на обращение. Звучит ТОЛЬКО когда команды в той же фразе не было:
# «Джарвис» -> «Да, сэр?». Если сказано «Джарвис, который час», отклик не нужен —
# он лишь отнял бы время у ответа. Сами фразы — responses.py, ключ «wake_ack».
WAKE_ACK_ENABLED = True

# =====================================================================
#  WHISPER — РАСПОЗНАВАНИЕ РЕЧИ (faster-whisper)
# =====================================================================
WHISPER_MODEL = "small"             # tiny | base | small | medium | large-v3
WHISPER_DEVICE = "cpu"              # cpu | cuda
WHISPER_COMPUTE_TYPE = "int8"       # int8 (cpu) | int8_float16 | float16 (gpu)
WHISPER_LANGUAGE = "ru"             # "ru" или None (автоопределение)
WHISPER_BEAM_SIZE = 1               # 1 — быстро, 5 — точнее
WHISPER_VAD_FILTER = True           # встроенный фильтр тишины Whisper
WHISPER_NO_SPEECH_THRESHOLD = 0.6   # порог «это не речь» (выше = строже)
WHISPER_CONDITION_ON_PREVIOUS = False
WHISPER_INITIAL_PROMPT = (
    "Джарвис, погода, громкость, яркость, скриншот, напоминание, "
    "ютуб, стим, вскод, терминал, браузер."
)

# =====================================================================
#  TTS — СИНТЕЗ РЕЧИ
# =====================================================================
TTS_ENGINE = "auto"                 # auto | edge | piper | sapi
TTS_VOICE = "ru-RU-DmitryNeural"    # голос edge-tts
TTS_RATE = "+10%"                   # скорость edge-tts ("+20%" / "-10%")
TTS_VOLUME = "+0%"                  # громкость edge-tts
TTS_PITCH = "+0Hz"                  # тон edge-tts
TTS_USE_CACHE = True                # кэшировать синтезированные фразы

PIPER_MODEL = VOICES_DIR / "ru_RU-dmitri-medium.onnx"
PIPER_SPEED = 1.0

SAPI_RATE = 185                     # pyttsx3 (SAPI5) — офлайн-резерв
SAPI_VOLUME = 1.0

# =====================================================================
#  LLM — МОЗГ (Ollama)
# =====================================================================
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = "mistral"            # mistral | llama3.2 | qwen2.5 | gemma2 ...
OLLAMA_VISION_MODEL = ""            # напр. "llama3.2-vision" или "llava" (пусто = выкл.)
OLLAMA_TIMEOUT = 90                 # сек
OLLAMA_MAX_TOKENS = 250
OLLAMA_TEMPERATURE = 0.6
OLLAMA_KEEP_ALIVE = "10m"           # держать модель в памяти
OLLAMA_RECHECK_SEC = 30             # как часто перепроверять недоступную Ollama
LLM_HISTORY_SIZE = 8                # сколько пар «вопрос-ответ» помнить

LLM_SYSTEM_PROMPT = (
    "Ты — Джарвис, голосовой ассистент уровня ИИ из фильмов про Железного человека. "
    "Ты работаешь на компьютере пользователя под Windows. "
    "Отвечай КРАТКО — 1-3 предложения, если не попросили подробнее. "
    "Пиши обычным текстом без markdown, списков и эмодзи — ответ будет озвучен вслух. "
    "Обращайся к пользователю «сэр». Не выдумывай факты: если не знаешь — честно скажи."
)

# =====================================================================
#  ВЕБ-ПОИСК И ПОГОДА
# =====================================================================
WEB_SEARCH_RESULTS = 3
WEATHER_DEFAULT_CITY = "Москва"
WEATHER_TIMEOUT = 6
WEATHER_DEADLINE = 8.0              # общий лимит на поиск города, сек

# =====================================================================
#  TELEGRAM-БОТ
# =====================================================================
TELEGRAM_ENABLED = False
TELEGRAM_TOKEN = ""                 # токен от @BotFather
TELEGRAM_ALLOWED_USERS: list = []   # [] = разрешить всем
TELEGRAM_NOTIFY_ON_START = True

# =====================================================================
#  ВЕБ-ИНТЕРФЕЙС (ядро и диалог в браузере)
# =====================================================================
WEB_UI_ENABLED = True               # поднимать интерфейс вместе с ассистентом
WEB_UI_HOST = "127.0.0.1"           # слушаем только локально
WEB_UI_PORT = 8765                  # если порт занят, берётся следующий свободный
WEB_UI_OPEN_BROWSER = True          # открывать браузер при старте

# =====================================================================
#  ВИДЕНИЕ (скриншот + анализ)
# =====================================================================
# Скриншоты — в «Документы», рядом с остальными файлами пользователя.
SCREENSHOT_DIR = Path.home() / "Documents" / "Скриншоты Джарвиса"
OCR_LANGUAGE = "ru"                 # язык встроенного OCR Windows (ru, en-US, ...)

# Список команд текстом. Создаётся и обновляется скриптом make_commands_list.py.
COMMANDS_FILE = Path.home() / "Documents" / "Джарвис — команды.txt"

# =====================================================================
#  ИМЯ ПРОЦЕССА
# =====================================================================
# Заголовок, под которым ассистент виден в диспетчере задач (см. main.py).
PROCESS_TITLE = "Джарвис"

# =====================================================================
#  ВИРТУАЛЬНЫЕ КЛАВИШИ WINDOWS
# =====================================================================
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_UP = 0xAF
VK_VOLUME_DOWN = 0xAE

# =====================================================================
#  ПРИЛОЖЕНИЯ (команды запуска)
# =====================================================================
APPS = {
    "notepad": "notepad.exe",
    "калькулятор": "calc.exe",
    "проводник": "explorer.exe",
    "диспетчер задач": "taskmgr.exe",
    "paint": "mspaint.exe",
}


# =====================================================================
#  ЗАГРУЗКА ПОЛЬЗОВАТЕЛЬСКИХ НАСТРОЕК
# =====================================================================
_TUNABLE = [
    "DEFAULT_INPUT_MODE", "ENABLE_TTS", "WAKE_WORD_REQUIRED", "VERBOSE",
    "BACKGROUND_MODE", "LOG_TO_FILE", "THINKING_FILLER",
    "SAMPLE_RATE", "CHANNELS", "CHUNK_MS", "WAKE_WINDOW_SEC", "MAX_COMMAND_SEC",
    "SILENCE_SEC", "MIN_SPEECH_SEC", "VAD_RMS_THRESHOLD", "MIC_CALIBRATION_SEC",
    "MIC_CLEAR_GUARD", "WAKE_WORDS", "WAKE_WORD_CUTOFF", "WAKE_SILENCE_SEC",
    "WAKE_ACK_ENABLED",
    "WHISPER_MODEL", "WHISPER_DEVICE", "WHISPER_COMPUTE_TYPE", "WHISPER_LANGUAGE",
    "WHISPER_BEAM_SIZE", "WHISPER_VAD_FILTER", "WHISPER_INITIAL_PROMPT",
    "TTS_ENGINE", "TTS_VOICE", "TTS_RATE", "TTS_VOLUME", "TTS_PITCH", "TTS_USE_CACHE",
    "PIPER_MODEL", "PIPER_SPEED", "SAPI_RATE", "SAPI_VOLUME",
    "OLLAMA_HOST", "OLLAMA_MODEL", "OLLAMA_VISION_MODEL", "OLLAMA_TIMEOUT",
    "OLLAMA_MAX_TOKENS", "OLLAMA_TEMPERATURE", "OLLAMA_KEEP_ALIVE",
    "OLLAMA_RECHECK_SEC",
    "LLM_HISTORY_SIZE", "LLM_SYSTEM_PROMPT",
    "WEB_SEARCH_RESULTS", "WEATHER_DEFAULT_CITY", "WEATHER_DEADLINE",
    "OCR_LANGUAGE", "SCREENSHOT_DIR", "COMMANDS_FILE", "PROCESS_TITLE",
    "WEB_UI_ENABLED", "WEB_UI_HOST", "WEB_UI_PORT", "WEB_UI_OPEN_BROWSER",
    "TELEGRAM_ENABLED", "TELEGRAM_TOKEN", "TELEGRAM_ALLOWED_USERS",
    "TELEGRAM_NOTIFY_ON_START", "APPS",
]

_STRING_PATHS = {"PIPER_MODEL", "SCREENSHOT_DIR", "NOTES_FILE", "REMINDERS_FILE",
                 "COMMANDS_FILE"}


def _load_settings() -> dict:
    """Читает jarvis_settings.json, игнорируя неизвестные и битые ключи."""
    if not SETTINGS_FILE.exists():
        return {}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[config] Не удалось прочитать {SETTINGS_FILE.name}: {exc}")
        return {}


def reload_settings() -> list:
    """Применяет пользовательские настройки. Возвращает список применённых ключей."""
    applied = []
    for key, value in _load_settings().items():
        if key not in _TUNABLE:
            continue
        if key in _STRING_PATHS and isinstance(value, str):
            value = Path(value).expanduser()
        globals()[key] = value
        applied.append(key)
    return applied


reload_settings()


def write_default_settings() -> bool:
    """Создаёт jarvis_settings.json с текущими значениями, если файла нет."""
    if SETTINGS_FILE.exists():
        return False
    defaults = {}
    for key in _TUNABLE:
        value = globals().get(key)
        if isinstance(value, Path):
            value = str(value)
        defaults[key] = value
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(defaults, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


# =====================================================================
#  ОПРЕДЕЛЕНИЕ ДОСТУПНЫХ БИБЛИОТЕК
# =====================================================================
def _has(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


FEATURES = {
    "sounddevice": _has("sounddevice"),
    "numpy": _has("numpy"),
    "faster_whisper": _has("faster_whisper"),
    "edge_tts": _has("edge_tts"),
    "piper": _has("piper"),
    "pyttsx3": _has("pyttsx3"),
    "pycaw": _has("pycaw"),
    "screen_brightness_control": _has("screen_brightness_control"),
    "duckduckgo": _has("ddgs") or _has("duckduckgo_search"),
    "pyautogui": _has("pyautogui"),
    "PIL": _has("PIL"),
    "win32gui": _has("win32gui"),
    "pyperclip": _has("pyperclip"),
    "telegram": _has("telegram"),
    "av": _has("av"),
    "windows_ocr": _has("winrt.windows.media.ocr"),
    "pytesseract": _has("pytesseract"),
}


def feature_report() -> list:
    """Человекочитаемый список доступных/отсутствующих возможностей."""
    labels = {
        "sounddevice": "Микрофон (sounddevice)",
        "numpy": "Численные расчёты (numpy)",
        "faster_whisper": "Распознавание речи (faster-whisper)",
        "edge_tts": "Нейронный голос (edge-tts)",
        "piper": "Офлайн-голос (piper)",
        "pyttsx3": "Резервный голос (pyttsx3/SAPI)",
        "pycaw": "Громкость системы (pycaw)",
        "screen_brightness_control": "Яркость экрана",
        "duckduckgo": "Веб-поиск (duckduckgo)",
        "pyautogui": "Горячие клавиши (pyautogui)",
        "PIL": "Скриншоты (Pillow)",
        "win32gui": "Управление окнами (pywin32)",
        "pyperclip": "Буфер обмена (pyperclip)",
        "telegram": "Telegram-бот",
        "av": "Декодер аудио (PyAV)",
        "windows_ocr": "OCR экрана (движок Windows)",
        "pytesseract": "OCR экрана (pytesseract)",
    }
    return [(labels.get(k, k), v) for k, v in FEATURES.items()]
