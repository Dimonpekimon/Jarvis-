# -*- coding: utf-8 -*-
"""Ядро Джарвиса v3.0.

Отвечает за распознавание намерения (intent), выполнение команды и главный цикл
ассистента. Вся логика действий вынесена в отдельные модули:

* :mod:`speech`    — микрофон и Whisper;
* :mod:`tts`       — озвучка;
* :mod:`brain`     — локальная LLM (Ollama);
* :mod:`actions`   — системные операции;
* :mod:`scheduler` — таймеры и напоминания;
* :mod:`vision`    — скриншот и анализ изображения;
* :mod:`telegram_bot` — управление с телефона.
"""

from __future__ import annotations

import ctypes
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from functools import wraps

import config
from actions import Actions, CalcError, calculate, parse_duration
from brain import Brain, BrainUnavailable
from events import emit_message, emit_state
from responses import ResponseBank
from scheduler import Scheduler
from speech import Listener, SpeechError
from telegram_bot import TelegramBridge
from tts import get_speaker
from utils import contains_word, log, normalize_text
from vision import Vision

_MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
_WEEKDAYS = (
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
)


@dataclass
class Intent:
    """Намерение пользователя: набор фраз-синонимов и обработчик."""

    name: str
    aliases: list
    handler: str
    priority: int = 0
    example: str = ""
    tags: list = field(default_factory=list)


# =====================================================================
#  РЕЕСТР КОМАНД
# =====================================================================
INTENTS = [
    Intent("time", ["который час", "сколько времени", "сколько время", "текущее время",
                    "время сейчас", "подскажи время", "время", "часы"], "_h_time",
           example="который час"),
    Intent("date", ["какое сегодня число", "какое число", "какая дата", "какое сегодня",
                    "дата сегодня", "сегодняшняя дата", "какой день недели", "какой сегодня день"],
           "_h_date", example="какое сегодня число"),
    Intent("weather", ["какая погода", "прогноз погоды", "погода сегодня", "погода",
                       "погоду", "погоды", "погодка", "температура на улице"],
           "_h_weather", example="погода в Москве"),
    Intent("briefing", ["утренний брифинг", "брифинг", "сводка на день", "сводка",
                        "что по планам", "обстановка"], "_h_briefing", example="брифинг"),

    Intent("greeting", ["доброе утро", "добрый день", "добрый вечер", "здравствуй",
                        "приветствую", "привет", "здорово", "хай"], "_h_greeting",
           example="привет"),
    Intent("how_are_you", ["как дела", "как ты", "как настроение", "как жизнь",
                           "как сам", "что нового"], "_h_how_are_you"),
    Intent("thanks", ["спасибо", "благодарю", "молодец", "умница"], "_h_thanks"),

    Intent("browser", ["открой браузер", "запусти браузер", "открой хром", "открой гугл",
                       "открой google", "браузер", "хром", "chrome"], "_h_browser"),
    Intent("youtube", ["открой ютуб", "запусти ютуб", "ютуб", "youtube", "you tube"], "_h_youtube"),
    Intent("steam", ["открой стим", "запусти стим", "стим", "steam"], "_h_steam"),
    Intent("roblox", ["запусти роблокс", "открой роблокс", "роблокс", "roblox"], "_h_roblox"),
    Intent("roblox_studio", ["запусти роблокс студио", "открой роблокс студио",
                             "роблокс студио", "roblox studio"], "_h_roblox_studio"),
    Intent("vscode", ["открой вскод", "запусти вскод", "вскод", "vs code", "visual studio code",
                      "открой редактор кода", "открой код"], "_h_vscode"),
    Intent("pycharm", ["открой пайчарм", "запусти пайчарм", "пайчарм", "pycharm"], "_h_pycharm"),
    Intent("terminal", ["открой терминал", "запусти терминал", "командную строку",
                        "командная строка", "терминал", "консоль", "cmd"], "_h_terminal"),
    Intent("powershell", ["открой powershell", "павершелл", "power shell"], "_h_powershell"),
    Intent("notepad", ["открой блокнот", "запусти блокнот", "блокнот", "notepad"], "_h_notepad"),
    Intent("calculator_app", ["открой калькулятор", "запусти калькулятор", "калькулятор приложение"],
           "_h_calculator_app"),
    Intent("explorer", ["открой проводник", "проводник", "мои файлы", "открой папку документы"],
           "_h_explorer"),
    Intent("taskmgr", ["диспетчер задач", "открой диспетчер"], "_h_taskmgr"),
    Intent("open_url", ["открой сайт", "открой ссылку", "перейди на сайт", "открой страницу"],
           "_h_open_url"),

    Intent("volume_up", ["громче", "сделай громче", "прибавь громкость", "увеличь громкость",
                         "повысь громкость", "громкость выше", "громкость больше"],
           "_h_volume_up", example="сделай громче"),
    Intent("volume_down", ["тише", "потише", "сделай тише", "уменьши громкость",
                           "снизь громкость", "убавь громкость", "громкость ниже"],
           "_h_volume_down", example="сделай тише"),
    Intent("volume_set", ["установи громкость", "громкость на", "поставь громкость"],
           "_h_volume_set"),
    Intent("volume_mute", ["выключи звук", "отключи звук", "без звука", "замолчи",
                           " mute", "выключи громкость"], "_h_volume_mute"),
    Intent("volume_unmute", ["включи звук", "верни звук", "unmute", "отмени mute"],
           "_h_volume_unmute"),

    Intent("brightness_up", ["ярче", "сделай ярче", "увеличь яркость", "повысь яркость",
                             "яркость выше", "светлее"], "_h_brightness_up"),
    Intent("brightness_down", ["тусклее", "темнее", "сделай темнее", "уменьши яркость",
                               "снизь яркость", "яркость ниже"], "_h_brightness_down"),
    Intent("brightness_set", ["установи яркость", "яркость на", "поставь яркость"],
           "_h_brightness_set"),

    Intent("lock", ["заблокируй экран", "заблокируй компьютер", "блокировка экрана",
                    "заблокируй пк", "заблокируй"], "_h_lock"),
    Intent("shutdown", ["выключи компьютер", "выключить компьютер", "завершение работы",
                        "выключи пк", "заверши работу", "выруби компьютер"], "_h_shutdown"),
    Intent("reboot", ["перезагрузи компьютер", "перезагрузить компьютер", "перезагрузка",
                      "перезагрузи пк", "рестарт системы"], "_h_reboot"),
    Intent("cancel_shutdown", ["отмена выключения", "отмени выключение", "не выключай",
                               "отмени перезагрузку", "отмена перезагрузки"], "_h_cancel_shutdown"),

    Intent("reminder", ["напомни мне", "напомни через", "поставь напоминание", "напоминание",
                        "напомни", "будильник"], "_h_reminder", example="напомни через 10 минут позвонить"),
    Intent("timer", ["поставь таймер", "таймер на", "засеки", "обратный отсчёт",
                     "таймер", "отсчёт"], "_h_timer", example="таймер на 5 минут"),
    Intent("list_tasks", ["какие напоминания", "мои напоминания", "список напоминаний",
                          "активные напоминания", "что по напоминаниям", "список таймеров"],
           "_h_list_tasks"),
    Intent("cancel_tasks", ["отмени все задачи", "отмени все напоминания", "отмени напоминания",
                            "убери напоминание", "отмени напоминание", "отмени таймер",
                            "отмени все таймеры", "отмени таймеры"], "_h_cancel_tasks"),
    Intent("cancel_last_task", ["отмени последнюю задачу", "отмени последний таймер",
                               "отмени последнее напоминание"], "_h_cancel_last_task",
           example="отмени последнюю задачу"),

    Intent("calc", ["посчитай", "вычисли", "сколько будет", "сколько это", "чему равно",
                    "рассчитай"], "_h_calc", example="сколько будет 2 плюс 2"),
    Intent("search", ["найди в интернете", "поищи в интернете", "погугли", "загугли",
                      "найди информацию", "поищи", "найди", "ищи"], "_h_search",
           example="найди погоду на завтра"),

    Intent("switch_app", ["переключи приложение", "переключи окно", "следующее окно",
                          "другое приложение", "альт таб", "alt tab", "смени окно"],
           "_h_switch_app"),
    Intent("list_windows", ["какие окна открыты", "список окон", "что открыто"], "_h_list_windows"),
    Intent("close_window", ["закрой окно", "закрой приложение", "закрыть окно", "закрыть приложение"],
           "_h_close_window"),
    Intent("minimize", ["сверни окно", "сверни приложение", "сверни", "минимизируй"],
           "_h_minimize"),
    Intent("maximize", ["разверни окно", "разверни приложение", "разверни", "максимизируй",
                        "во весь экран"], "_h_maximize"),

    Intent("screenshot", ["сделай скриншот", "скриншот", "снимок экрана", "скрин"], "_h_screenshot"),
    Intent("read_screen", ["прочитай что на экране", "прочитай экран", "что написано на экране",
                           "распознай текст на экране"], "_h_read_screen"),
    Intent("analyze_screen", ["посмотри на экран", "что на экране", "проанализируй экран",
                              "что происходит на экране", "оцени экран", "что я делаю"],
           "_h_analyze_screen"),

    Intent("note_add", ["запиши заметку", "сделай заметку", "добавь заметку", "запиши в заметки",
                        "запомни что", "запомни"], "_h_note_add", example="запиши заметку купить хлеб"),
    Intent("note_read", ["прочитай заметки", "покажи заметки", "мои заметки", "какие заметки"],
           "_h_note_read"),

    Intent("clipboard_read", ["что в буфере", "прочитай буфер", "покажи буфер обмена",
                              "что скопировано"], "_h_clipboard_read"),
    Intent("clipboard_write", ["скопируй текст", "положи в буфер", "запиши в буфер обмена"],
           "_h_clipboard_write"),

    Intent("media_play", ["воспроизведи", "включи музыку", "запусти музыку", "продолжи музыку",
                          "играй", "плей"], "_h_media_play"),
    Intent("media_pause", ["пауза", "стоп музыку", "останови музыку", "останови", "стоп"],
           "_h_media_pause", priority=10),
    Intent("media_next", ["следующий трек", "следующая песня", "переключи трек вперёд",
                          "следующий"], "_h_media_next"),
    Intent("media_prev", ["предыдущий трек", "предыдущая песня", "переключи трек назад",
                          "предыдущий"], "_h_media_prev"),

    Intent("trash", ["очисти корзину", "очистить корзину", "удали мусор", "опустоши корзину"],
           "_h_trash"),

    # Утилиты о самом компьютере. Всё через ctypes и стандартную библиотеку —
    # новых зависимостей проект не тянет.
    Intent("system_info", ["сведения о системе", "информация о системе",
                           "характеристики компьютера", "параметры компьютера",
                           "что с компьютером"], "_h_system_info"),
    Intent("disk_space", ["сколько места на диске", "свободное место на диске",
                          "сколько свободно на диске", "место на диске",
                          "сколько места", "свободное место"], "_h_disk_space"),
    Intent("memory", ["сколько оперативной памяти", "загрузка памяти",
                      "использование памяти", "сколько памяти", "сколько озу"],
           "_h_memory"),
    Intent("battery", ["заряд батареи", "заряд аккумулятора", "состояние батареи",
                       "уровень заряда", "сколько заряда"], "_h_battery"),
    Intent("uptime", ["сколько работает компьютер", "время работы компьютера",
                      "сколько включён компьютер", "аптайм", "uptime"], "_h_uptime"),
    Intent("ip_address", ["какой у меня ip", "мой ip адрес", "локальный ip",
                          "ip адрес", "айпи адрес", "мой ip"], "_h_ip_address"),
    Intent("monitor_off", ["выключи монитор", "погаси экран", "отключи монитор",
                           "выключи дисплей"], "_h_monitor_off"),
    Intent("sleep_mode", ["спящий режим", "режим сна", "усыпи компьютер",
                          "перевести в сон"], "_h_sleep_mode"),

    Intent("random_choice", ["брось кубик", "подбрось монетку", "брось монетку",
                             "случайное число", "монетка", "кубик"], "_h_random",
           example="подбрось монетку"),
    Intent("password", ["сгенерируй пароль", "сгенерируй надёжный пароль",
                        "создай пароль", "придумай пароль", "новый пароль"],
           "_h_password"),
    Intent("open_folder", ["открой папку загрузки", "открой загрузки",
                           "открой рабочий стол", "открой мои документы",
                           "открой документы", "открой музыку", "открой картинки"],
           "_h_open_folder"),

    Intent("joke", ["расскажи шутку", "расскажи анекдот", "пошути", "рассмеши",
                    "шутка", "анекдот"], "_h_joke"),
    Intent("reset_dialog", ["забудь всё", "очисти историю", "сбрось контекст",
                            "начни заново", "забудь наш разговор"], "_h_reset_dialog"),
    Intent("status", ["статус систем", "проверка систем", "диагностика", "статус"], "_h_status"),
    Intent("help", ["что ты умеешь", "список команд", "помощь", "твои возможности"], "_h_help"),
    Intent("repeat", ["повтори ответ", "повтори последний ответ", "что ты сказал", "повтори"],
           "_h_repeat", example="повтори ответ"),

    # «Отключи» без уточнения выключает самого Джарвиса вместе с интерфейсом;
    # «отключи звук» и «выключи компьютер» разбираются точнее — они длиннее и
    # побеждают по длине совпадения (см. Jarvis._score).
    Intent("exit", ["выключайся", "выключись", "отключайся", "отключись",
                    "выключи", "отключи", "выключи ассистента", "отключи ассистента",
                    "выключи интерфейс", "отключи интерфейс", "отключи интерфейсы",
                    "завершить работу ассистента", "заверши работу ассистента",
                    "до свидания", "пока", "закройся", "спать"], "_h_exit",
           example="выключайся"),
]

_WEATHER_STOPWORDS = {
    "погода", "погоду", "погоды", "погодка", "прогноз", "какая", "какой", "какое",
    "скажи", "в", "во", "на", "сейчас", "сегодня", "завтра", "пожалуйста", "джарвис",
    "температура", "улице", "сколько", "градусов",
}


def _serialized(method):
    """Один исполнитель для голоса, HTTP и Telegram; допускает вложенный handle/process."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._command_lock:
            return method(self, *args, **kwargs)
    return wrapped


class Jarvis:
    """Голосовой ассистент."""

    def __init__(self, mode: str | None = None, enable_tts: bool | None = None,
                 wake_required: bool | None = None, background: bool | None = None):
        self.mode = mode or config.DEFAULT_INPUT_MODE
        self._command_lock = threading.RLock()
        self._shutdown_lock = threading.Lock()
        self._allow_filler = False
        self._running = True
        # Фоновый запуск (ярлык на рабочем столе): консоли нет, текстовый ввод
        # невозможен. Управление — голосом и через веб-интерфейс.
        self.background = config.BACKGROUND_MODE if background is None else background
        self._no_input_warned = False

        self.speaker = get_speaker()
        if enable_tts is not None:
            self.speaker.enabled = enable_tts
        if wake_required is not None:
            config.WAKE_WORD_REQUIRED = wake_required

        self.actions = Actions()
        self.brain = Brain()
        self.listener: Listener | None = None
        self.scheduler = Scheduler(speak_fn=self.speaker.say,
                                   notify_fn=self.actions.notify)
        self.vision = Vision(self.actions, self.brain)
        self.telegram = TelegramBridge(
            handler=self.process,
            status_provider=self.status,
            reset_callback=self._reset_dialog,
        )
        self.last_response = ""
        self._llm_warned = False
        # Подписчики на выключение (веб-интерфейс): ядро не знает про сервер,
        # его регистрирует main.py через add_shutdown_hook().
        self._shutdown_hooks = []
        self._shutdown_done = False
        self._alias_index = self._build_alias_index()
        # Кэш сопоставлений: одна фраза проходит через match_intent до четырёх
        # раз (разбор составной команды + само выполнение).
        self._match_cache: dict = {}

    # ==================================================================
    #  Сопоставление команд
    # ==================================================================
    @staticmethod
    def _build_alias_index() -> list:
        """Все синонимы, отсортированные от длинных к коротким."""
        pairs = []
        for intent in INTENTS:
            for alias in intent.aliases:
                pairs.append((normalize_text(alias), intent))
        pairs.sort(key=lambda item: len(item[0]), reverse=True)
        return pairs

    @staticmethod
    def _score(text: str, alias: str):
        """Оценка совпадения фразы с синонимом. None — не совпало.

        Чем точнее и длиннее совпадение, тем выше балл. Именно это исправляет
        прежнюю ошибку, когда «выключи компьютер» попадало в команду громкости.
        """
        if not alias:
            return None
        if text == alias:
            return 10000 + len(alias) * 10
        if contains_word(text, alias):
            return 5000 + len(alias) * 10
        if len(alias) >= 5 and alias in text:
            return 1000 + len(alias) * 10
        return None

    #: Предел кэша сопоставлений — защита от роста на случайных фразах.
    _MATCH_CACHE_MAX = 256

    def match_intent(self, text: str):
        """Возвращает (Intent, совпавший_синоним) или (None, None).

        Результат кэшируется: в пределах одной реплики фраза (или её часть)
        сопоставляется несколько раз, а перебор идёт по всем ~250 синонимам.
        """
        normalized = normalize_text(text)
        if not normalized:
            return None, None
        cached = self._match_cache.get(normalized)
        if cached is not None:
            return cached
        best = None
        for alias, intent in self._alias_index:
            score = self._score(normalized, alias)
            if score is None:
                continue
            score += intent.priority
            if best is None or score > best[0]:
                best = (score, intent, alias)
        result = (best[1], best[2]) if best else (None, None)
        if len(self._match_cache) >= self._MATCH_CACHE_MAX:
            self._match_cache.clear()
        self._match_cache[normalized] = result
        return result

    # ==================================================================
    #  Обработка фразы
    # ==================================================================
    @_serialized
    def process(self, raw: str) -> str:
        """Обрабатывает фразу и возвращает ответ (без озвучки).

        Понимает составные команды: «сделай громче и напомни через 5 минут позвонить».
        Фраза делится только если КАЖДАЯ часть распознаётся как известная команда —
        иначе она целиком уходит в LLM, чтобы не разорвать обычный вопрос.
        """
        if not raw or not raw.strip():
            return ""
        if not self._running:
            return "Джарвис уже выключается, сэр."
        text = self._strip_wake_word(raw.strip())
        log(f"Вы: {text}", "info")
        emit_message("user", text)
        emit_state("thinking")

        parts = self._split_compound(text)
        if parts:
            log(f"Составная команда: {len(parts)} части.", "debug")
            responses = [self._execute_single(part) for part in parts]
            answer = " ".join(response for response in responses if response)
        else:
            answer = self._execute_single(text)

        self.last_response = answer
        # Если ответ уже озвучивается, состояние «говорит» выставит поток озвучки.
        if not self.speaker.busy:
            emit_state("idle")
        return answer

    def _execute_single(self, text: str) -> str:
        """Выполняет одну команду. Если она не распознана — передаёт вопрос модели."""
        intent, alias = self.match_intent(text)
        if intent is not None:
            handler = getattr(self, intent.handler, None)
            if handler is None:
                log(f"Нет обработчика для «{intent.name}»", "error")
            else:
                self._fill_silence(intent.name)
                try:
                    result = handler(text, alias)
                except Exception as exc:  # noqa: BLE001
                    log(f"Ошибка выполнения «{intent.name}»: {exc}", "error")
                    result = "Сэр, при выполнении команды произошла ошибка."
                if result:
                    return result
                log(f"Команда «{intent.name}» не дала результата — пробую LLM.", "debug")
        return self._ask_llm(text)

    # Разделители составных команд. Длинные варианты обязаны идти первыми,
    # иначе «и» разорвёт фразу раньше, чем «и потом».
    _COMPOUND_SPLIT = re.compile(
        r"\s+(?:и|а)\s+потом\s+|\s+и\s+затем\s+|\s+затем\s+|\s+после\s+этого\s+|\s+и\s+",
        re.IGNORECASE,
    )
    _MAX_COMPOUND_PARTS = 3

    def _split_compound(self, text: str) -> list:
        """Делит фразу на команды, если все части распознаются как команды."""
        if not self._COMPOUND_SPLIT.search(text):
            return []
        parts = [part.strip(" ,.;:!?—-") for part in self._COMPOUND_SPLIT.split(text)]
        parts = [part for part in parts if len(part) >= 3]
        if not 2 <= len(parts) <= self._MAX_COMPOUND_PARTS:
            return []
        if not all(self.match_intent(part)[0] for part in parts):
            return []
        return parts

    # Команды, которые ходят в сеть или распознают экран: между «услышал» и
    # «ответил» проходит заметное время, и молчание выглядит как зависание.
    _SLOW_INTENTS = {"weather", "briefing", "search", "read_screen", "analyze_screen"}

    def _fill_silence(self, intent_name: str) -> None:
        """Подаёт голос перед долгой командой: «Секунду, сэр...».

        Вызывается из главного потока, а озвучка идёт своим потоком, поэтому
        саму команду отбивка не задерживает. Если ассистент уже говорит
        (например, в составной команде), второй раз не повторяем.
        """
        if not config.THINKING_FILLER or intent_name not in self._SLOW_INTENTS:
            return
        if self.speaker.busy:
            return
        self.speaker.say(ResponseBank.get("thinking"))

    def handle(self, raw: str) -> str:
        """Обрабатывает фразу и озвучивает ответ."""
        response = self.process(raw)
        if response:
            self.speaker.say(response)
        return response

    @staticmethod
    def _strip_wake_word(text: str) -> str:
        lowered = text.lower().replace("ё", "е")
        for wake in sorted(config.WAKE_WORDS, key=len, reverse=True):
            if lowered.startswith(wake):
                return text[len(wake):].lstrip(" ,.!?-—")
        return text

    # ==================================================================
    #  LLM
    # ==================================================================
    def _ask_llm(self, text: str) -> str:
        try:
            return self.brain.ask(text)
        except BrainUnavailable as exc:
            log(str(exc), "warn")
            return self._llm_offline_answer(text, str(exc))

    def _llm_offline_answer(self, text: str, reason: str) -> str:
        """Что делать, если Ollama недоступна: ищем в интернете.

        Подсказку про установку показываем только один раз за сеанс, чтобы
        она не примешивалась к каждому ответу.
        """
        self._fill_silence("search")
        found = self.actions.web_search(text)

        note = ""
        if not self._llm_warned:
            self._llm_warned = True
            note = f"{reason} Пока отвечу по данным из интернета. "

        if not found or "не удалось" in found.lower():
            if note:
                return (note + "По этому запросу ничего не нашлось — "
                        "попробуйте переформулировать или скажите «список команд».")
            return ResponseBank.get("unknown")
        return f"{note}Вот что нашёл: {found}"

    # ==================================================================
    #  ОБРАБОТЧИКИ: ВРЕМЯ, ПОГОДА, ОБЩЕНИЕ
    # ==================================================================
    def _h_time(self, _raw, _alias) -> str:
        return ResponseBank.get("time", value=datetime.now().strftime("%H:%M"))

    def _h_date(self, _raw, _alias) -> str:
        now = datetime.now()
        value = f"{now.day} {_MONTHS_GENITIVE[now.month - 1]} {now.year} года, {_WEEKDAYS[now.weekday()]}"
        return ResponseBank.get("date", value=value)

    def _h_weather(self, raw, _alias) -> str:
        return self.actions.weather(self._extract_city(raw))

    def _h_briefing(self, _raw, _alias) -> str:
        now = datetime.now()
        parts = [
            ResponseBank.get("briefing_lead"),
            f"Сейчас {now.strftime('%H:%M')}, {now.day} {_MONTHS_GENITIVE[now.month - 1]}.",
            self.actions.weather(None),
        ]
        upcoming = self.scheduler.upcoming()
        if upcoming:
            parts.append("Планы: " + "; ".join(upcoming) + ".")
        else:
            parts.append("Напоминаний на сегодня нет.")
        return " ".join(parts)

    def _h_greeting(self, _raw, _alias) -> str:
        return ResponseBank.get("greeting")

    def _h_how_are_you(self, _raw, _alias) -> str:
        return ResponseBank.get("how_are_you")

    def _h_thanks(self, _raw, _alias) -> str:
        return ResponseBank.get("thanks")

    def _h_joke(self, _raw, _alias) -> str:
        return ResponseBank.get("joke")

    def _h_status(self, _raw, _alias) -> str:
        return self.status()

    def _h_help(self, _raw, _alias) -> str:
        return self.help_text()

    def _h_reset_dialog(self, _raw, _alias) -> str:
        self._reset_dialog()
        return ResponseBank.get("reset_dialog")

    def _h_exit(self, _raw, _alias) -> str:
        self._running = False
        return ResponseBank.get("exit")

    # ==================================================================
    #  ОБРАБОТЧИКИ: ЗАПУСК ПРИЛОЖЕНИЙ
    # ==================================================================
    def _h_browser(self, _raw, _alias) -> str:
        return self.actions.open_browser("https://google.com")

    def _h_youtube(self, _raw, _alias) -> str:
        return self.actions.open_browser("https://youtube.com")

    def _h_steam(self, _raw, _alias) -> str:
        return self.actions.open_app("steam")

    def _h_roblox(self, _raw, _alias) -> str:
        return self.actions.open_roblox()

    def _h_roblox_studio(self, _raw, _alias) -> str:
        return self.actions.open_roblox(studio=True)

    def _h_vscode(self, _raw, _alias) -> str:
        return self.actions.open_app("vscode")

    def _h_pycharm(self, _raw, _alias) -> str:
        return self.actions.open_app("pycharm")

    def _h_terminal(self, _raw, _alias) -> str:
        return self.actions.open_terminal()

    def _h_powershell(self, _raw, _alias) -> str:
        return self.actions.open_powershell()

    def _h_notepad(self, _raw, _alias) -> str:
        return self.actions.open_app("notepad")

    def _h_calculator_app(self, _raw, _alias) -> str:
        return self.actions.open_app("калькулятор")

    def _h_explorer(self, _raw, _alias) -> str:
        return self.actions.open_app("проводник")

    def _h_taskmgr(self, _raw, _alias) -> str:
        return self.actions.open_app("диспетчер задач")

    def _h_open_url(self, raw, _alias) -> str:
        match = re.search(r"(https?://\S+|[\w-]+\.(?:ru|com|org|net|io)\S*)", raw, re.IGNORECASE)
        if not match:
            return "Какой сайт открыть, сэр?"
        url = match.group(1)
        if not url.startswith("http"):
            url = "https://" + url
        return self.actions.open_browser(url)

    # ==================================================================
    #  ОБРАБОТЧИКИ: ЗВУК И ЯРКОСТЬ
    # ==================================================================
    def _h_volume_up(self, _raw, _alias) -> str:
        return self.actions.volume("up")

    def _h_volume_down(self, _raw, _alias) -> str:
        return self.actions.volume("down")

    def _h_volume_set(self, raw, _alias) -> str:
        level = self._extract_percent(raw)
        if level is None:
            return "На сколько процентов поставить громкость, сэр?"
        return self.actions.volume("set", level=level)

    def _h_volume_mute(self, _raw, _alias) -> str:
        return self.actions.volume("mute")

    def _h_volume_unmute(self, _raw, _alias) -> str:
        return self.actions.volume("unmute")

    def _h_brightness_up(self, _raw, _alias) -> str:
        return self.actions.brightness("up")

    def _h_brightness_down(self, _raw, _alias) -> str:
        return self.actions.brightness("down")

    def _h_brightness_set(self, raw, _alias) -> str:
        level = self._extract_percent(raw)
        if level is None:
            return "Какая яркость нужна, сэр? Скажите, например, «яркость на 70»."
        return self.actions.brightness("set", level=level)

    @staticmethod
    def _extract_percent(raw: str):
        match = re.search(r"(\d{1,3})\s*(?:%|процент\w*)?", normalize_text(raw))
        if not match:
            return None
        value = int(match.group(1))
        return value if 0 <= value <= 100 else None

    # ==================================================================
    #  ОБРАБОТЧИКИ: ПИТАНИЕ
    # ==================================================================
    def _h_lock(self, _raw, _alias) -> str:
        return self.actions.lock_screen()

    def _h_shutdown(self, _raw, _alias) -> str:
        return self.actions.shutdown()

    def _h_reboot(self, _raw, _alias) -> str:
        return self.actions.reboot()

    def _h_cancel_shutdown(self, _raw, _alias) -> str:
        return self.actions.cancel_shutdown()

    # ==================================================================
    #  ОБРАБОТЧИКИ: НАПОМИНАНИЯ И ТАЙМЕРЫ
    # ==================================================================
    def _h_reminder(self, raw, alias) -> str:
        seconds = parse_duration(raw)
        text = self._extract_reminder_text(raw)
        if not text:
            return "Что именно напомнить, сэр?"
        if seconds is None:
            return f"Через какое время напомнить: «{text}»?"
        return self.scheduler.add_reminder(seconds, text)

    def _h_timer(self, raw, _alias) -> str:
        seconds = parse_duration(raw)
        if seconds is None:
            return "На сколько поставить таймер, сэр?"
        return self.scheduler.add_timer(seconds)

    def _h_list_tasks(self, _raw, _alias) -> str:
        return self.scheduler.list_items()

    def _h_cancel_tasks(self, _raw, _alias) -> str:
        return self.scheduler.cancel_all()

    def _extract_reminder_text(self, raw: str) -> str:
        text = normalize_text(raw)
        for phrase in ("напомни мне", "напомни", "напоминание", "будильник",
                       "поставь", "через"):
            text = text.replace(phrase, " ")
        # Убираем указание времени: «10 минут», «полтора часа», «30 секунд».
        text = re.sub(r"\b\d+[.,]?\d*\s*(час\w*|мин\w*|сек\w*)\b", " ", text)
        text = re.sub(r"\b(полчаса|полтора часа)\b", " ", text)
        text = re.sub(r"\b(час\w*|минут\w*|минуту|секунд\w*|секунду)\b", " ", text)
        text = re.sub(r"\b(мне|пожалуйста|сэр|джарвис)\b", " ", text)
        return " ".join(text.split()).strip(" ,.-")

    # ==================================================================
    #  ОБРАБОТЧИКИ: МАТЕМАТИКА И ПОИСК
    # ==================================================================
    def _h_calc(self, raw, _alias):
        try:
            result = calculate(raw)
        except CalcError as exc:
            log(f"Калькулятор: {exc} — передаю запрос модели.", "debug")
            return None  # пусть ответит LLM
        return f"Получается {result}."

    def _h_search(self, raw, alias) -> str:
        query = self._strip_aliases(raw, ("найди в интернете", "поищи в интернете",
                                          "найди информацию", "погугли", "загугли",
                                          "найди", "поищи", "ищи"))
        if not query:
            return "Что именно найти, сэр?"
        return self.actions.web_search(query)

    @staticmethod
    def _strip_aliases(raw: str, aliases) -> str:
        text = normalize_text(raw)
        for alias in sorted(aliases, key=len, reverse=True):
            text = text.replace(normalize_text(alias), " ")
        return " ".join(text.split()).strip(" ,.-")

    # ==================================================================
    #  ОБРАБОТЧИКИ: ОКНА
    # ==================================================================
    def _h_switch_app(self, _raw, _alias) -> str:
        return self.actions.switch_window()

    def _h_list_windows(self, _raw, _alias) -> str:
        return self.actions.list_windows()

    def _h_close_window(self, _raw, _alias) -> str:
        return self.actions.close_window()

    def _h_minimize(self, _raw, _alias) -> str:
        return self.actions.minimize_window()

    def _h_maximize(self, _raw, _alias) -> str:
        return self.actions.maximize_window()

    # ==================================================================
    #  ОБРАБОТЧИКИ: ЭКРАН И ЗАМЕТКИ
    # ==================================================================
    def _warm_recognizer(self) -> None:
        """Поднимает Whisper ДО первого обращения к OCR экрана.

        Особенность Windows: если модуль ``winrt`` (движок OCR) импортирован
        РАНЬШЕ, чем создана модель faster-whisper, то загрузка модели валит
        процесс сегфолтом — проверено, код возврата 139. Обратный порядок
        (сначала Whisper, потом OCR) работает штатно.

        В текстовом режиме Whisper не загружается вовсе, поэтому и опасности
        нет — там ничего не делаем и время не тратим.
        """
        if self.mode == "text" or not self._voice_available():
            return
        if self.listener is not None and self.listener.model_ready:
            return
        try:
            self._ensure_listener().preload_model()
        except Exception as exc:  # noqa: BLE001
            log(f"Whisper не поднялся перед распознаванием экрана: {exc}", "warn")

    def _h_screenshot(self, _raw, _alias) -> str:
        path = self.actions.screenshot()
        if path is None:
            return "Не удалось сделать скриншот — установите Pillow: pip install pillow"
        return ResponseBank.get("screenshot", value=str(path))

    def _h_read_screen(self, _raw, _alias) -> str:
        self._warm_recognizer()   # иначе winrt уронит загрузку Whisper
        return self.vision.read_screen_text()

    def _h_analyze_screen(self, raw, _alias) -> str:
        self._warm_recognizer()   # иначе winrt уронит загрузку Whisper
        question = self._strip_aliases(raw, ("посмотри на экран", "проанализируй экран",
                                             "что происходит на экране", "оцени экран",
                                             "что на экране", "что я делаю"))
        return self.vision.analyze(question)

    def _h_note_add(self, raw, _alias) -> str:
        text = self._strip_aliases(raw, ("запиши заметку", "сделай заметку", "добавь заметку",
                                         "запиши в заметки", "запомни что", "запомни",
                                         "запиши"))
        if not text:
            return "Что записать, сэр?"
        try:
            config.NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(config.NOTES_FILE, "a", encoding="utf-8") as handle:
                handle.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] {text}\n")
        except OSError as exc:
            log(f"Не удалось сохранить заметку: {exc}", "error")
            return "Не удалось сохранить заметку, сэр."
        return ResponseBank.get("note_saved")

    def _h_note_read(self, _raw, _alias) -> str:
        try:
            if not config.NOTES_FILE.exists():
                return ResponseBank.get("note_empty")
            content = config.NOTES_FILE.read_text(encoding="utf-8").strip()
        except OSError as exc:
            log(f"Не удалось прочитать заметки: {exc}", "error")
            return "Не удалось прочитать заметки, сэр."
        if not content:
            return ResponseBank.get("note_empty")
        lines = [line for line in content.splitlines() if line.strip()]
        return "Ваши заметки: " + "; ".join(lines[-5:])

    # ==================================================================
    #  ОБРАБОТЧИКИ: БУФЕР, МЕДИА, КОРЗИНА
    # ==================================================================
    def _h_clipboard_read(self, _raw, _alias) -> str:
        return self.actions.clipboard_get()

    def _h_clipboard_write(self, raw, _alias) -> str:
        text = self._strip_aliases(raw, ("скопируй текст", "положи в буфер",
                                         "запиши в буфер обмена", "скопируй"))
        if not text:
            return "Что скопировать, сэр?"
        return self.actions.clipboard_set(text)

    def _h_media_play(self, _raw, _alias) -> str:
        self.actions.media_key(config.VK_MEDIA_PLAY_PAUSE)
        return "Воспроизведение."

    def _h_media_pause(self, _raw, _alias) -> str:
        self.actions.media_key(config.VK_MEDIA_PLAY_PAUSE)
        return "Пауза."

    def _h_media_next(self, _raw, _alias) -> str:
        self.actions.media_key(config.VK_MEDIA_NEXT_TRACK)
        return "Следующий трек."

    def _h_media_prev(self, _raw, _alias) -> str:
        self.actions.media_key(config.VK_MEDIA_PREV_TRACK)
        return "Предыдущий трек."

    def _h_trash(self, _raw, _alias) -> str:
        return self.actions.empty_trash()

    # ==================================================================
    #  ОБРАБОТЧИКИ: СВЕДЕНИЯ О КОМПЬЮТЕРЕ И УТИЛИТЫ
    # ==================================================================
    def _h_system_info(self, _raw, _alias) -> str:
        return self.actions.system_info()

    def _h_disk_space(self, _raw, _alias) -> str:
        return self.actions.disk_space()

    def _h_memory(self, _raw, _alias) -> str:
        return self.actions.memory()

    def _h_battery(self, _raw, _alias) -> str:
        return self.actions.battery()

    def _h_uptime(self, _raw, _alias) -> str:
        return self.actions.uptime()

    def _h_ip_address(self, _raw, _alias) -> str:
        return self.actions.ip_address()

    def _h_monitor_off(self, _raw, _alias) -> str:
        return self.actions.monitor_off()

    def _h_sleep_mode(self, _raw, _alias) -> str:
        return self.actions.sleep_mode()

    def _h_random(self, raw, _alias) -> str:
        return self.actions.random_choice(raw)

    def _h_password(self, _raw, _alias) -> str:
        return f"Ваш новый пароль: {self.actions.generate_password()}"

    def _h_open_folder(self, raw, _alias) -> str:
        return self.actions.open_common_folder(raw)

    def _h_repeat(self, _raw, _alias) -> str:
        """Повторяет последний ответ ассистента."""
        if not self.last_response:
            return "Мне пока нечего повторять, сэр."
        return self.last_response

    def _h_cancel_last_task(self, _raw, _alias) -> str:
        """Отменяет только самую недавно созданную задачу."""
        return self.scheduler.cancel_latest()

    # ==================================================================
    #  ВСПОМОГАТЕЛЬНОЕ
    # ==================================================================
    @staticmethod
    def _extract_city(raw: str) -> str:
        tokens = normalize_text(raw).split()
        for index, token in enumerate(tokens):
            if token in ("в", "во") and index + 1 < len(tokens):
                candidate = tokens[index + 1]
                if candidate not in _WEATHER_STOPWORDS:
                    return candidate
        for token in reversed(tokens):
            if (token not in _WEATHER_STOPWORDS and len(token) > 2
                    and not token.isdigit() and token not in config.WAKE_WORDS):
                return token
        return ""

    def _reset_dialog(self) -> None:
        self.brain.reset()
        self._llm_warned = False

    def reset_dialog(self) -> None:
        """Очищает историю диалога — вызывается из веб-интерфейса."""
        self._reset_dialog()

    def help_text(self) -> str:
        groups = {
            "Система": "громче, тише, ярче, темнее, заблокируй экран, выключи компьютер",
            "Программы": "открой браузер, ютуб, стим, вскод, терминал, проводник",
            "Окна": "переключи окно, сверни, разверни, закрой окно",
            "Время": "который час, какое сегодня число, погода в Москве, брифинг",
            "Задачи": "напомни через 10 минут позвонить, таймер на 5 минут, мои напоминания",
            "Умное": "посчитай 2 плюс 2, найди в интернете ..., прочитай что на экране",
            "Разное": "сделай скриншот, запиши заметку ..., прочитай заметки, расскажи шутку, повтори ответ",
            "Компьютер": "сколько памяти, место на диске, заряд батареи, аптайм, мой ip",
            "Утилиты": "сгенерируй пароль, подбрось монетку, открой загрузки, выключи монитор",
        }
        parts = ["Вот что я умею, сэр:"]
        for name, examples in groups.items():
            parts.append(f"{name} — {examples}.")
        parts.append("Можно отдавать и по несколько команд сразу, через «и».")
        parts.append("Всё остальное я просто передам нейросети.")
        return " ".join(parts)

    def status(self) -> str:
        whisper_state = "загружена" if (self.listener and self.listener.model_ready) else "не загружена"
        brain_ok, brain_message = self.brain.check()
        tts_engines = ", ".join(e for e in ("edge", "piper", "sapi")
                                if e == "edge" and config.FEATURES["edge_tts"]
                                or e == "piper" and config.FEATURES["piper"]
                                or e == "sapi" and config.FEATURES["pyttsx3"]) or "нет"
        tasks = self.scheduler.upcoming(limit=2)
        wake_state = "включено" if config.WAKE_WORD_REQUIRED else "выключено"
        return (
            f"Режим: {self.mode}. "
            f"Распознавание: Whisper «{config.WHISPER_MODEL}» ({whisper_state}). "
            f"Озвучка: {tts_engines}. "
            f"Мозг: {'работает' if brain_ok else 'недоступен'} — {brain_message} "
            f"Слово-активатор: {wake_state}. "
            f"Запуск: {'фоновый' if self.background else 'обычный'}. "
            f"Telegram: {'активен' if self.telegram.running else 'выключен'}. "
            f"Активных задач: {len(tasks)}."
        )

    def _print_banner(self) -> None:
        log("=" * 62, "info")
        log("  ДЖАРВИС v3.0 — голосовой ассистент", "info")
        log(f"  Режим: {self.mode} | Whisper: {config.WHISPER_MODEL} | "
            f"Модель: {config.OLLAMA_MODEL}", "info")
        if self.background:
            log("  Фоновый запуск (ярлык на рабочем столе): без консоли, "
                "управление голосом и через веб-интерфейс.", "info")
        log("=" * 62, "info")

    # ==================================================================
    #  ГЛАВНЫЙ ЦИКЛ
    # ==================================================================
    def _voice_available(self) -> bool:
        return config.FEATURES["sounddevice"] and config.FEATURES["faster_whisper"]

    def _ensure_listener(self) -> Listener:
        if self.listener is None:
            self.listener = Listener()
        return self.listener

    def _text_input_available(self) -> bool:
        """Можно ли спрашивать текст в консоли.

        В фоновом запуске (pythonw, ярлык на рабочем столе) консоли нет:
        ``input()`` сразу бросает EOFError, и цикл крутился бы вхолостую.
        """
        if self.background:
            return False
        try:
            return sys.stdin is not None and sys.stdin.readable()
        except (ValueError, AttributeError):
            return False

    def _read_text(self) -> str:
        if not self._text_input_available():
            if not self._no_input_warned:
                self._no_input_warned = True
                log("Консольный ввод недоступен — жду команду голосом "
                    "или из веб-интерфейса.", "info")
            time.sleep(0.5)
            return ""
        try:
            return input("\n[Вы] ").strip()
        except EOFError:
            # Ввод закрыт (Ctrl+D или перенаправленный поток) — не крутим цикл
            # вхолостую: веб-интерфейс может продолжать работать.
            time.sleep(0.2)
            return ""
        # KeyboardInterrupt сюда не попадает: его обрабатывает главный цикл.

    def _wait_mic_clear(self) -> None:
        """Ждёт, пока ассистент договорит, и короткую паузу после этого.

        Без эхоподавления микрофон слышит самого Джарвиса: он произносил
        «Системы Джарвиса запущены», и это же принималось за обращение
        (нечёткое сравнение считает «джарвиса» похожим на «джарвис»). Теперь
        запись начинается только после тишины в динамиках.
        """
        if self.speaker.busy:
            self.speaker.wait_until_done()
        guard = float(getattr(config, "MIC_CLEAR_GUARD", 0.3) or 0.0)
        if guard > 0:
            time.sleep(guard)

    def _listen_for_wake(self) -> str:
        """Ждёт обращение и отдаёт распознанную фразу целиком (или пусто)."""
        self._wait_mic_clear()
        emit_state("listening")
        try:
            heard = self._ensure_listener().listen_wake()
        except SpeechError as exc:
            log(f"{exc}. Перехожу в текстовый режим.", "error")
            self.mode = "text"
            return ""
        return heard or ""

    def _listen_command(self) -> str:
        """Записывает и распознаёт команду — уже после обращения."""
        self._wait_mic_clear()
        emit_state("listening")
        try:
            return self._ensure_listener().listen_command()
        except SpeechError as exc:
            log(f"{exc}. Перехожу в текстовый режим.", "error")
            self.mode = "text"
            return self._read_text()

    # Короче этого — не команда, а шум распознавания («да», «ну»).
    _MIN_COMMAND_CHARS = 3

    @classmethod
    def _looks_like_command(cls, text: str) -> bool:
        return len((text or "").strip()) >= cls._MIN_COMMAND_CHARS

    def _acknowledge_wake(self) -> None:
        """Отклик на обращение: «Да, сэр?».

        Говорится только тогда, когда команда не прозвучала в той же фразе:
        иначе отклик отнял бы время у ответа. Отключается WAKE_ACK_ENABLED.
        """
        if not config.WAKE_ACK_ENABLED:
            return
        self.speaker.say(ResponseBank.get("wake_ack"))

    def _next_utterance(self) -> str:
        """Один шаг диалога: обращение -> (отклик) -> команда.

        Правило проекта: **одна команда на одно «Джарвис»**. После выполнения
        команды цикл возвращается к ожиданию обращения и продолжение не слушает —
        иначе ассистент реагировал бы на любой разговор в комнате.

        Быстрый путь: если команда прозвучала в той же фразе, что и обращение
        («Джарвис, который час»), она уходит на выполнение сразу — без отклика
        и без второй записи с распознаванием.
        """
        if self.mode == "text" or not self._voice_available():
            if self.mode != "text" and not self._voice_available():
                log("Голосовой ввод недоступен — перехожу в текстовый режим.", "warn")
                self.mode = "text"
            return self._read_text()

        if not config.WAKE_WORD_REQUIRED:
            return self._listen_command()

        heard = self._listen_for_wake()
        if not heard:
            return ""

        command = self._strip_wake_word(heard).strip()
        if self._looks_like_command(command):
            return command

        # Прозвучало только «Джарвис» — откликаемся и слушаем команду.
        self._acknowledge_wake()
        command = self._listen_command()
        if not self._looks_like_command(command):
            # Распознался шум («ну», «да») — не отправляем его в веб-поиск,
            # а возвращаемся к ожиданию обращения.
            log("Команду не расслышал.", "info")
            return ""
        return command

    @staticmethod
    def _set_process_title() -> None:
        """Помечает процесс как «Джарвис» — виден в диспетчере задач.

        Имя процесса в диспетчере берётся из имени exe, поэтому заголовок
        консоли — только косметика (вкладка «Приложения», панель задач).
        Полноценно задача решается ярлыком: ``make_shortcut.py`` кладёт рядом
        с проектом копию ``pythonw.exe`` под именем ``Джарвис.exe``, и тогда
        в диспетчере задач так и написано. Здесь — вежливая попытка.
        """
        title = getattr(config, "PROCESS_TITLE", "Джарвис")
        try:
            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:  # noqa: BLE001
            pass
        try:  # запасной путь, если ctypes недоступен
            import win32api
            win32api.SetConsoleTitle(title)
        except Exception:  # noqa: BLE001
            pass

    def run(self) -> None:
        self._set_process_title()
        self._print_banner()

        # Без микрофона или Whisper голосовой режим невозможен — уходим в текст.
        if self.mode != "text" and not self._voice_available():
            log("Голосовой ввод недоступен (нет sounddevice или faster-whisper) — "
                "перехожу в текстовый режим.", "warn")
            self.mode = "text"

        restored = self.scheduler.restore()
        if restored:
            log(f"Восстановлено напоминаний: {restored}.", "info")

        if self.mode != "text":
            emit_state("loading")
            self._ensure_listener().preload_model()
            self.listener.calibrate()

        if self.telegram.start():
            log("Telegram-бот активен.", "info")

        # Приветствие по времени суток: «Доброе утро, сэр» / «Добрый вечер, сэр».
        self.speaker.say(ResponseBank.startup_greeting())

        try:
            while self._running:
                text = self._next_utterance()
                if not text:
                    continue
                self.handle(text)
        except KeyboardInterrupt:
            log("\nПрерывание с клавиатуры.", "warn")
        finally:
            self.shutdown()

    def add_shutdown_hook(self, callback) -> None:
        """Действие при выключении — например, гашение веб-интерфейса.

        Команда «выключайся» останавливала только главный цикл: сервер
        интерфейса жил в своём потоке, порт оставался занят, и вкладка в
        браузере продолжала отвечать. Теперь выключение проходит через
        ``shutdown()``, а подписчиков регистрирует main.py — ядро по-прежнему
        ничего не знает про веб-сервер.
        """
        if callback is not None:
            self._shutdown_hooks.append(callback)

    def shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._running = False
        self._shutdown_done = True
        for hook in self._shutdown_hooks:
            try:
                hook()
            except Exception as exc:  # noqa: BLE001
                log(f"Ошибка при выключении интерфейса: {exc}", "debug")
        try:
            self.telegram.stop()
        except Exception:  # noqa: BLE001
            pass
        self.scheduler.stop_all()
        self.speaker.shutdown()
        log("Джарвис остановлен. До встречи, сэр.", "info")
