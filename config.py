# -*- coding: utf-8 -*-
"""Конфигурация Пятницы.

Все параметры ниже можно переопределить в файле ``pyatnitsa_settings.json``
(он создаётся автоматически при первом запуске рядом с этим модулем).
Формат — обычный JSON, ключи совпадают с именами констант::

    {
        "VOSK_MODEL_NAME": "vosk-model-small-ru-0.22",
        "TTS_VOICE": "ru-RU-SvetlanaNeural",
        "GEMINI_MODEL": "gemini-3.5-flash"
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

# Версия приложения. Одна на всё: заголовок в логе, API, диагностика.
# __init__.__version__ держим в том же виде — selftest сверяет их.
APP_VERSION = "3.4.0"

# Имя ассистента. Одна точка правды: отсюда берут его промпт нейросети,
# заголовок процесса и имя файла со списком команд. Обращение (WAKE_WORDS)
# задаётся отдельно — там нужны падежи и варианты распознавания.
ASSISTANT_NAME = "Пятница"

# =====================================================================
#  ПУТИ
# =====================================================================
BASE_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BASE_DIR / "pyatnitsa_settings.json"

# Совместимость: до переименования настройки лежали в jarvis_settings.json.
# Переносим файл при первом запуске, чтобы не сбросились ключ Gemini, голос
# и прочие пользовательские значения.
_LEGACY_SETTINGS = BASE_DIR / "jarvis_settings.json"
if not SETTINGS_FILE.exists() and _LEGACY_SETTINGS.exists():
    try:
        _LEGACY_SETTINGS.rename(SETTINGS_FILE)
    except OSError:
        pass
DATA_DIR = BASE_DIR / "data"
CACHE_DIR = DATA_DIR / "tts_cache"
MODELS_DIR = DATA_DIR / "models"
VOICES_DIR = DATA_DIR / "voices"
LOG_FILE = DATA_DIR / "pyatnitsa.log"

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
WAKE_WORD_REQUIRED = True           # в голосовом режиме ждать слово «Пятница»
VERBOSE = False                     # подробный лог
BACKGROUND_MODE = False             # фоновый запуск без консоли (ярлык на рабочем столе)
LOG_TO_FILE = False                 # дублировать лог в data/pyatnitsa.log (в фоне — да)
LOG_MAX_BYTES = 1_000_000           # ротация лога: размер одного файла
LOG_BACKUP_COUNT = 3                # сколько архивов .1 ... .N хранить
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

# Обращение к ассистенту. Vosk работает без грамматики, поэтому подойдёт любое
# слово: чтобы переименовать ассистента, достаточно поменять этот список (и
# ASSISTANT_NAME ниже — оно идёт в промпт, отклики и заголовок процесса).
# Короткие варианты добавлены потому, что падежи и оглушённые окончания Vosk
# слышит по-разному: «пятницу», «пятниц», «пятниса».
WAKE_WORDS = ["пятница", "пятницу", "пятницы", "пятниса", "петница"]
WAKE_WORD_CUTOFF = 0.78             # порог нечёткого совпадения (0..1)
WAKE_SILENCE_SEC = 0.6              # тишина, завершающая фразу с обращением

# Отклик на обращение. Звучит ТОЛЬКО когда команды в той же фразе не было:
# «Пятница» -> «Да, сэр?». Если сказано «Пятница, который час», отклик не нужен —
# он лишь отнял бы время у ответа. Сами фразы — responses.py, ключ «wake_ack».
WAKE_ACK_ENABLED = True

# =====================================================================
#  ДИАЛОГ БЕЗ ПОВТОРНОГО ОБРАЩЕНИЯ
# =====================================================================
# После ответа ассистент не захлопывается сразу: ещё несколько секунд он
# слушает продолжение без слова-активатора. Так работает обычный разговор —
# «Пятница, какая погода» -> ответ -> «а завтра?» -> ответ. Как только
# наступает тишина (никто не заговорил за DIALOG_FOLLOW_UP_SEC) или звучит
# фраза из DIALOG_END_PHRASES, ассистент возвращается к ожиданию обращения и
# перестаёт реагировать на разговоры в комнате.
DIALOG_MODE = True                  # продолжать разговор без оклика
DIALOG_FOLLOW_UP_SEC = 12.0         # сколько ждать продолжения, сек
DIALOG_MAX_TURNS = 0                # реплик подряд; 0 — без ограничения
# Фразы, которыми разговор заканчивается досрочно. «Пока» и «до свидания» сюда
# НЕ входят: это команда полного выключения ассистента (см. INTENTS -> exit).
DIALOG_END_PHRASES = [
    "хватит", "достаточно", "отбой", "закончили", "закончим", "на этом всё",
    "всё хватит", "хватит болтать", "спасибо хватит", "пока хватит",
    "не надо больше", "замолчи совсем",
]
# Короткие ответы, которые в диалоге осмысленны, хотя и не являются командой
# («да», «нет»). Вне диалога они считаются шумом распознавания.
DIALOG_SHORT_REPLIES = ["да", "нет", "ага", "угу", "конечно", "давай", "не надо",
                        "хорошо", "ладно", "окей", "ок"]

# =====================================================================
#  VOSK — РАСПОЗНАВАНИЕ РЕЧИ (локально, офлайн)
# =====================================================================
# Небольшая русская модель (~45 МБ) распознаёт за доли секунды на CPU и не
# требует ни GPU, ни интернета после первого скачивания. Если папки модели
# нет, speech.py скачает и распакует её автоматически.
VOSK_MODEL_NAME = "vosk-model-small-ru-0.22"
VOSK_MODEL_PATH = MODELS_DIR / VOSK_MODEL_NAME
VOSK_MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip"
VOSK_AUTO_DOWNLOAD = True           # скачивать модель при первом запуске
VOSK_SAMPLE_RATE = SAMPLE_RATE      # Vosk ждёт 16 кГц моно int16
VOSK_WORDS = True                   # возвращать тайминги и уверенность слов

# =====================================================================
#  TTS — СИНТЕЗ РЕЧИ (Piper — локально, офлайн)
# =====================================================================
# Основной движок — Piper: нейронный голос работает без интернета, ничего не
# отправляет в облако и не зависит от доступности серверов Microsoft.
# edge-tts и SAPI остаются резервом, если модель Piper недоступна.
TTS_ENGINE = "piper"                # piper | edge | sapi | auto
# Голос edge-tts (резервный движок). Женский — чтобы при сбое Piper Пятница не
# сменила пол посреди разговора.
TTS_VOICE = "ru-RU-SvetlanaNeural"
TTS_RATE = "+10%"                   # скорость edge-tts ("+20%" / "-10%")
TTS_VOLUME = "+0%"                  # громкость edge-tts
TTS_PITCH = "+0Hz"                  # тон edge-tts
TTS_USE_CACHE = True                # кэшировать синтезированные фразы
TTS_CACHE_MAX_FILES = 100            # ограничение числа файлов в кэше
TTS_CACHE_TTL_DAYS = 30              # удалять файлы кэша старше этого срока

# Голос Piper — по имени из каталога voices.py (sova200, irina, kat580, mari,
# dmitri, denis, ruslan). Отсюда выводятся путь к модели, адрес загрузки и
# рекомендуемый темп, поэтому переключать голос нужно здесь, а не правкой пути.
# «sova200» — женский голос с самым высоким и светлым тембром (216 Гц):
# из доступных русских моделей он ближе всего к «аниме»-звучанию.
PIPER_VOICE = "sova200"
# Своя модель (не из каталога). Пусто — брать по имени голоса выше.
PIPER_MODEL = None
# Устаревшие адреса: оставлены, чтобы старые настройки читались без ошибок.
# Скачиванием теперь занимается voices.remote_url().
PIPER_MODEL_URL = None
PIPER_CONFIG_URL = None
PIPER_AUTO_DOWNLOAD = True          # скачивать голос при первом запуске
PIPER_SPEED = 1.2                   # 1.0 — обычная, 1.2 — быстрее (темп irina)
PIPER_VOLUME = 1.0                  # 0.0..1.0
# Тембр: None — значения из модели. Меньше noise — ровнее голос, меньше
# «дрожания»; больше — живее, но с шероховатостями. Для ассистента ровнее лучше.
PIPER_NOISE_SCALE = None            # вариативность тембра (по умолчанию 0.667)
PIPER_NOISE_W = None                # вариативность длительности (по умолчанию 0.8)
# Обработка тембра после синтеза (tone.py): "" — как задумано для голоса в
# каталоге, "off" — без обработки, "soft" — мягче, "confident" — собраннее.
# Живёт в настройках, чтобы варианты можно было сравнивать без правки кода.
PIPER_TONE = ""

SAPI_RATE = 185                     # pyttsx3 (SAPI5) — офлайн-резерв
SAPI_VOLUME = 1.0

# =====================================================================
#  LLM — МОЗГ (Gemini API, бесплатный тариф)
# =====================================================================
# Ключ можно задать здесь, в pyatnitsa_settings.json (ключ GEMINI_API_KEY) или
# переменной окружения GEMINI_API_KEY. В репозиторий ключ не попадает.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MODEL = "gemini-3.5-flash"       # основная модель для диалога
GEMINI_FALLBACK_MODELS = ["gemini-3.5-flash-lite"]   # если основная занята
GEMINI_VISION_MODEL = "gemini-3.5-flash"  # анализ скриншотов ("" = выкл.)
GEMINI_TIMEOUT = 30                 # сек на один запрос
GEMINI_MAX_TOKENS = 500             # с запасом: ответ озвучивается целиком
GEMINI_TEMPERATURE = 0.6
# 0 — не тратить токены и время на «размышления». Модели, которые этот
# параметр не поддерживают, получают запрос без него (brain.py повторяет).
GEMINI_THINKING_BUDGET = 0
GEMINI_RECHECK_SEC = 30             # как часто перепроверять ключ при ошибке
LLM_HISTORY_SIZE = 8                # сколько пар «вопрос-ответ» помнить

LLM_SYSTEM_PROMPT = (
    f"Ты — {ASSISTANT_NAME}, голосовой ассистент уровня ИИ из фильмов про Железного человека. "
    "Ты работаешь на компьютере пользователя под Windows. "
    "Отвечай КРАТКО — 1-3 предложения, если не попросили подробнее. "
    "Пиши обычным текстом без markdown, списков и эмодзи — ответ будет озвучен вслух. "
    "Обращайся к пользователю «сэр». Не выдумывай факты: если не знаешь — честно скажи."
)

# =====================================================================
#  ВЕБ-ПОИСК И ПОГОДА
# =====================================================================
WEB_SEARCH_RESULTS = 3
# Поиск выполняется только по явной команде «найди ...». Неизвестная шумовая
# реплика при недоступной нейросети не должна внезапно зачитывать статью из сети.
LLM_OFFLINE_WEB_FALLBACK = False
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
#  API ЯДРА (канал связи с настольным окном)
# =====================================================================
# Отдельной веб-версии нет: страницу рисует Electron (desktop/), а Python
# отдаёт ему состояние и принимает реплики по HTTP/SSE на петлевом адресе.
API_HOST = "127.0.0.1"              # слушаем только локально
API_PORT = 8765                     # если порт занят, берётся следующий свободный

# =====================================================================
#  ВИДЕНИЕ (скриншот + анализ)
# =====================================================================
# Скриншоты — в «Документы», рядом с остальными файлами пользователя.
SCREENSHOT_DIR = Path.home() / "Documents" / "Скриншоты Пятницы"
OCR_LANGUAGE = "ru"                 # язык встроенного OCR Windows (ru, en-US, ...)

# Список команд текстом. Создаётся и обновляется скриптом make_commands_list.py.
COMMANDS_FILE = Path.home() / "Documents" / f"{ASSISTANT_NAME} — команды.txt"

# =====================================================================
#  ИМЯ ПРОЦЕССА
# =====================================================================
# Заголовок, под которым ассистент виден в диспетчере задач (см. main.py).
PROCESS_TITLE = ASSISTANT_NAME

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
    "ASSISTANT_NAME",
    "DEFAULT_INPUT_MODE", "ENABLE_TTS", "WAKE_WORD_REQUIRED", "VERBOSE",
    "BACKGROUND_MODE", "LOG_TO_FILE", "LOG_MAX_BYTES", "LOG_BACKUP_COUNT", "THINKING_FILLER",
    "SAMPLE_RATE", "CHANNELS", "CHUNK_MS", "WAKE_WINDOW_SEC", "MAX_COMMAND_SEC",
    "SILENCE_SEC", "MIN_SPEECH_SEC", "VAD_RMS_THRESHOLD", "MIC_CALIBRATION_SEC",
    "MIC_CLEAR_GUARD", "WAKE_WORDS", "WAKE_WORD_CUTOFF", "WAKE_SILENCE_SEC",
    "WAKE_ACK_ENABLED",
    "DIALOG_MODE", "DIALOG_FOLLOW_UP_SEC", "DIALOG_MAX_TURNS", "DIALOG_END_PHRASES",
    "DIALOG_SHORT_REPLIES",
    "VOSK_MODEL_NAME", "VOSK_MODEL_PATH", "VOSK_MODEL_URL", "VOSK_AUTO_DOWNLOAD",
    "VOSK_SAMPLE_RATE", "VOSK_WORDS",
    "TTS_ENGINE", "TTS_VOICE", "TTS_RATE", "TTS_VOLUME", "TTS_PITCH", "TTS_USE_CACHE",
    "TTS_CACHE_MAX_FILES", "TTS_CACHE_TTL_DAYS",
    "PIPER_MODEL", "PIPER_VOICE", "PIPER_MODEL_URL", "PIPER_CONFIG_URL",
    "PIPER_AUTO_DOWNLOAD", "PIPER_SPEED", "PIPER_VOLUME",
    "PIPER_NOISE_SCALE", "PIPER_NOISE_W", "PIPER_TONE", "SAPI_RATE", "SAPI_VOLUME",
    "GEMINI_API_KEY", "GEMINI_API_BASE", "GEMINI_MODEL", "GEMINI_FALLBACK_MODELS",
    "GEMINI_VISION_MODEL", "GEMINI_TIMEOUT", "GEMINI_MAX_TOKENS",
    "GEMINI_TEMPERATURE", "GEMINI_THINKING_BUDGET", "GEMINI_RECHECK_SEC",
    "LLM_HISTORY_SIZE", "LLM_SYSTEM_PROMPT",
    "WEB_SEARCH_RESULTS", "LLM_OFFLINE_WEB_FALLBACK", "WEATHER_DEFAULT_CITY", "WEATHER_DEADLINE",
    "OCR_LANGUAGE", "SCREENSHOT_DIR", "COMMANDS_FILE", "PROCESS_TITLE",
    "API_HOST", "API_PORT",
    "TELEGRAM_ENABLED", "TELEGRAM_TOKEN", "TELEGRAM_ALLOWED_USERS",
    "TELEGRAM_NOTIFY_ON_START", "APPS",
]

_STRING_PATHS = {"PIPER_MODEL", "SCREENSHOT_DIR", "NOTES_FILE", "REMINDERS_FILE",
                 "COMMANDS_FILE", "VOSK_MODEL_PATH"}


def _load_settings() -> dict:
    """Читает pyatnitsa_settings.json, игнорируя неизвестные и битые ключи."""
    if not SETTINGS_FILE.exists():
        return {}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        # Совместимость: до v3.3 настройки назывались WEB_UI_*. Переносим порт
        # и хост, чтобы у пользователя не сбросилось выбранное значение.
        if "API_PORT" not in data and isinstance(data.get("WEB_UI_PORT"), int):
            data["API_PORT"] = data["WEB_UI_PORT"]
        if "API_HOST" not in data and isinstance(data.get("WEB_UI_HOST"), str):
            data["API_HOST"] = data["WEB_UI_HOST"]
        for legacy in ("WEB_UI_ENABLED", "WEB_UI_OPEN_BROWSER"):
            data.pop(legacy, None)
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
            # Пустая строка — это «не задано», а не путь: Path("") превратился
            # бы в текущий каталог и перекрыл бы выбор голоса.
            value = Path(value).expanduser() if value.strip() else None
        globals()[key] = value
        applied.append(key)
    return applied


reload_settings()


def write_default_settings() -> bool:
    """Создаёт pyatnitsa_settings.json с текущими значениями, если файла нет."""
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
    "vosk": _has("vosk"),
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
        "vosk": "Распознавание речи (Vosk)",
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
