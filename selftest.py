# -*- coding: utf-8 -*-
"""Быстрые проверки логики Пятницы без микрофона и без нейросети.

Запуск:  python selftest.py
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

import actions
import brain
import config
import events
import pyatnitsa_core
import make_commands_list
import make_shortcut
import speech
import tone
import tts
import voices
import api
from actions import (
    _city_alias,
    _city_candidates,
    _city_stem,
    _strip_preposition,
    calculate,
    parse_duration,
    prepare_expression,
)
from pyatnitsa_core import Pyatnitsa
from responses import ResponseBank
from speech import Listener
from utils import (CalcError, normalize_command_text, plural_ru, safe_calculate,
                   stem_matches, stem_ru)

PASSED = 0
FAILED = 0


def check(label: str, got, expected) -> None:
    global PASSED, FAILED
    if got == expected:
        PASSED += 1
        print(f"  [ok]   {label}")
    else:
        FAILED += 1
        print(f"  [FAIL] {label}\n         ожидалось: {expected!r}\n         получено:  {got!r}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    print(f"Проверка {config.ASSISTANT_NAME} v{config.APP_VERSION}")

    # ---------------- Калькулятор ----------------
    section("Калькулятор (главный исправленный баг)")
    cases = [
        ("сколько будет 2 плюс 2", 4),
        ("2 плюс 2", 4),
        ("посчитай 10 минус 3", 7),
        ("5 умножить на 6", 30),
        ("100 разделить на 4", 25),
        ("два плюс два", 4),
        ("двадцать пять умножить на четыре", 100),
        ("2 + 2 * 2", 6),
        ("корень из 81", 9),
        ("2 в квадрате", 4),
    ]
    for phrase, expected in cases:
        try:
            check(phrase, calculate(phrase), expected)
        except CalcError as exc:
            check(phrase, f"CalcError: {exc}", expected)

    section("Калькулятор: защита от опасных выражений")
    for bad in ["__import__('os').system('dir')", "9**9**9", "open('x')", "1/0"]:
        try:
            result = safe_calculate(bad)
            check(f"отклонено: {bad}", f"ВЫПОЛНЕНО ({result})", "исключение")
        except (CalcError, ZeroDivisionError):
            check(f"отклонено: {bad}", "исключение", "исключение")

    check("prepare: 'сколько будет 2 плюс 2'", prepare_expression("сколько будет 2 плюс 2"), "2 + 2")

    # ---------------- Разбор длительности ----------------
    section("Разбор длительности (исправлен баг с секундами)")
    duration_cases = [
        ("напомни через 5 минут", 300),
        ("таймер на 30 секунд", 30),
        ("через 10 секунд", 10),
        ("через час", 3600),
        ("через 2 часа", 7200),
        ("через полчаса", 1800),
        ("через полтора часа", 5400),
        ("через минуту", 60),
        ("поставь таймер на 90 секунд", 90),
    ]
    for phrase, expected in duration_cases:
        check(phrase, parse_duration(phrase), expected)

    # ---------------- Роутинг команд ----------------
    section("Роутинг команд (исправлен приоритет)")
    pyatnitsa = Pyatnitsa(mode="text", enable_tts=False)

    routing_cases = [
        # Главный баг: «выключи компьютер» уходило в громкость.
        ("выключи компьютер", "shutdown"),
        ("выключи звук", "volume_mute"),
        ("сделай громче", "volume_up"),
        ("сделай тише", "volume_down"),
        ("включи звук", "volume_unmute"),
        # «час» не должен перехватывать напоминания.
        ("напомни через час позвонить маме", "reminder"),
        ("который час", "time"),
        # «сейчас» содержит «час», но это не время.
        ("посчитай два плюс два", "calc"),
        ("какая погода в Москве", "weather"),
        ("погода", "weather"),
        ("перезагрузи компьютер", "reboot"),
        ("заблокируй экран", "lock"),
        ("закрой окно", "close_window"),
        ("сверни окно", "minimize"),
        ("сделай скриншот", "screenshot"),
        ("расскажи шутку", "joke"),
        ("выключись", "exit"),
        ("стоп", "media_pause"),
        ("следующий трек", "media_next"),
        ("что ты умеешь", "help"),
        ("привет", "greeting"),
        ("очисти корзину", "trash"),
        ("какое сегодня число", "date"),
        ("мои заметки", "note_read"),
        ("прочитай заметки", "note_read"),
        ("найди в интернете курс доллара", "search"),
        ("открой ютуб", "youtube"),
        ("открой терминал", "terminal"),
        ("яркость на 70", "brightness_set"),
        ("таймер на 5 минут", "timer"),
        ("брифинг", "briefing"),
        # Выключение ассистента. «Отключи» без уточнения — это про саму Пятницу,
        # а «отключи звук» по-прежнему про звук: совпадение длиннее, значит точнее.
        ("выключайся", "exit"),
        ("выключись", "exit"),
        ("отключайся", "exit"),
        ("отключись", "exit"),
        ("выключи", "exit"),
        ("отключи", "exit"),
        # ...но уточнение важнее короткого слова:
        ("выключи компьютер", "shutdown"),
        ("выключи звук", "volume_mute"),
        ("выключи интерфейс", "exit"),
        ("отключи интерфейсы", "exit"),
        ("выключи ассистента", "exit"),
        ("завершение работы", "shutdown"),
        ("заверши работу ассистента", "exit"),
        ("отмена выключения", "cancel_shutdown"),
        # Roblox Player и Roblox Studio — разные программы, команды тоже разные.
        ("запусти роблокс", "roblox"),
        ("открой роблокс", "roblox"),
        ("роблокс", "roblox"),
        ("запусти роблокс студио", "roblox_studio"),
        ("роблокс студио", "roblox_studio"),
        ("запусти стим", "steam"),
        # Утилиты о компьютере и мелкие помощники.
        ("повтори ответ", "repeat"),
        ("отмени последнюю задачу", "cancel_last_task"),
        ("сколько места на диске", "disk_space"),
        ("заряд батареи", "battery"),
        ("какой у меня ip", "ip_address"),
        ("сколько работает компьютер", "uptime"),
        ("сколько оперативной памяти", "memory"),
        ("выключи монитор", "monitor_off"),
        ("спящий режим", "sleep_mode"),
        ("подбрось монетку", "random_choice"),
        ("сгенерируй пароль", "password"),
        ("открой загрузки", "open_folder"),
        ("сведения о системе", "system_info"),
    ]
    for phrase, expected_intent in routing_cases:
        intent, alias = pyatnitsa.match_intent(phrase)
        got = intent.name if intent else None
        check(f"«{phrase}»", got, expected_intent)

    # ---------------- Падежи и окончания ----------------
    # Распознавание речи отдаёт фразу в произнесённой форме, а реестр хранит
    # один канонический вид. Раньше «закрой окна» и «выключи интерфейса»
    # уходили нейросети — со стороны это выглядело как «не понял простую
    # команду». Теперь слова сравниваются по основе (utils.stem_ru).
    section("Падежи и окончания")
    check("основа «окна»", stem_ru("окна"), "окн")
    check("основа «окно»", stem_ru("окно"), "окн")
    check("основа «закрой»", stem_ru("закрой"), stem_ru("закрою"))
    check("основа «громкости»", stem_ru("громкости"), stem_ru("громкость"))
    check("основа «интерфейса»", stem_ru("интерфейса"), stem_ru("интерфейс"))
    check("основа короткого слова не режется", stem_ru("час"), "час")
    check("«пока» не совпадает с «показать»",
          stem_matches(stem_ru("пока"), stem_ru("показать")), False)
    check("«время» совпадает с «времени»",
          stem_matches(stem_ru("время"), stem_ru("времени")), True)

    inflection_cases = [
        ("закрой окна", "close_window"),
        ("закрой все окна", "close_window"),
        ("закрой, пожалуйста, все окна", "close_window"),
        ("закрою окна", "close_window"),
        ("выключи интерфейса", "exit"),
        ("закрой интерфейс", "exit"),
        ("отключайся", "exit"),
        ("вырубайся", "exit"),
        ("прибавь громкости", "volume_up"),
        ("убавь громкости", "volume_down"),
        ("сделай погромче", "volume_up"),
        ("запусти роблокс студию", "roblox_studio"),
        ("выключи музыку", "media_pause"),
        ("сколько на часах", "time"),
        ("погаси дисплей", "monitor_off"),
        ("разбуди через 10 минут", "reminder"),
        # Фраза не должна «притягиваться» за уши к похожей команде.
        ("закрой дверь", None),
        ("включи свет", None),
        ("открой холодильник", None),
        ("сколько стоит билет", None),
        ("что такое квантовая физика", None),
    ]
    for phrase, expected_intent in inflection_cases:
        intent, _alias = pyatnitsa.match_intent(phrase)
        check(f"«{phrase}»", intent.name if intent else None, expected_intent)

    # ---------------- Обработчики команд ----------------
    # Каждая команда из реестра обязана иметь метод-обработчик: без него фраза
    # молча уходит в LLM. Именно так «повтори ответ» и «отмени последнюю задачу»
    # однажды перестали работать — реестр их знал, а обработчиков не было.
    section("У каждой команды реестра есть обработчик")
    missing_handlers = [
        intent.name for intent in pyatnitsa_core.INTENTS
        if not callable(getattr(Pyatnitsa, intent.handler, None))
    ]
    check("все обработчики из INTENTS существуют", missing_handlers, [])
    check("у каждой команды есть хотя бы один синоним",
          all(intent.aliases for intent in pyatnitsa_core.INTENTS), True)
    check("имена команд в реестре уникальны",
          len({intent.name for intent in pyatnitsa_core.INTENTS}), len(pyatnitsa_core.INTENTS))

    # ---------------- Новые утилиты ----------------
    section("Утилиты: компьютер и мелочи")
    check("есть сведения о системе", hasattr(actions.Actions, "system_info"), True)
    check("есть свободное место на диске", hasattr(actions.Actions, "disk_space"), True)
    check("есть заряд батареи", hasattr(actions.Actions, "battery"), True)
    check("есть аптайм", hasattr(actions.Actions, "uptime"), True)
    check("есть локальный IP", hasattr(actions.Actions, "ip_address"), True)
    check("есть выключение монитора", hasattr(actions.Actions, "monitor_off"), True)
    check("есть спящий режим", hasattr(actions.Actions, "sleep_mode"), True)
    check("есть генератор пароля", hasattr(actions.Actions, "generate_password"), True)
    check("есть случайный выбор", hasattr(actions.Actions, "random_choice"), True)
    check("есть открытие папки", hasattr(actions.Actions, "open_common_folder"), True)

    # Случайный выбор и пароль считаются без обращения к системе — проверяем сами.
    check("монетка отвечает орлом или решкой",
          actions.Actions.random_choice("подбрось монетку") in ("Орёл!", "Решка!"), True)
    check("кубик даёт 1..6",
          actions.Actions.random_choice("брось кубик").startswith("Выпало "), True)
    check("число из диапазона",
          actions.Actions.random_choice("случайное число от 1 до 1"), "Случайное число: 1.")
    password = actions.Actions.generate_password()
    check("пароль длиной 16", len(password), 16)
    check("пароль без похожих символов",
          any(ch in password for ch in "lI1O0"), False)
    check("пароль заданной длины", len(actions.Actions.generate_password(24)), 24)
    check("длина пароля зажата снизу", len(actions.Actions.generate_password(2)), 8)
    check("место на диске непустое", bool(actions.Actions().disk_space()), True)
    check("память сообщает проценты", "процент" in actions.Actions.memory(), True)
    check("аптайм сообщает время", "работ" in actions.Actions.uptime(), True)
    check("локальный IP распознан", "IP-адрес" in actions.Actions.ip_address(), True)
    check("сводка о системе собирается", len(actions.Actions().system_info()) > 60, True)
    check("заряд батареи без отрицательных значений",
          "-1 " not in actions.Actions.battery(), True)

    # Русское склонение числительных — иначе ассистент говорит «51 процентов».
    section("Склонение числительных")
    check("1 процент", plural_ru(1, "процент", "процента", "процентов"), "процент")
    check("2 процента", plural_ru(2, "процент", "процента", "процентов"), "процента")
    check("4 процента", plural_ru(4, "процент", "процента", "процентов"), "процента")
    check("5 процентов", plural_ru(5, "процент", "процента", "процентов"), "процентов")
    check("11 процентов", plural_ru(11, "процент", "процента", "процентов"), "процентов")
    check("21 процент", plural_ru(21, "процент", "процента", "процентов"), "процент")
    check("51 процент", plural_ru(51, "процент", "процента", "процентов"), "процент")
    check("ноль процентов", plural_ru(0, "процент", "процента", "процентов"), "процентов")

    # ---------------- Составные команды ----------------
    section("Составные команды")
    compound_cases = [
        ("сделай громче и напомни через 5 минут позвонить маме", 2),
        ("какая погода и который час", 2),
        ("сверни окно и выключи звук", 2),
        ("таймер на 5 минут и расскажи шутку", 2),
        # Не должно делиться: вторая часть не является командой.
        ("включи музыку и танцуй", 0),
        ("объясни разницу между и или", 0),
        ("найди в интернете курс доллара и евро", 0),
        # Обычная одиночная команда.
        ("который час", 0),
    ]
    for phrase, expected_parts in compound_cases:
        check(f"«{phrase}»", len(pyatnitsa._split_compound(phrase)), expected_parts)

    # ---------------- Извлечение города ----------------
    section("Извлечение города из фразы")
    city_cases = [
        ("какая погода в Москве", "москве"),
        ("погода в санкт-петербурге", "санкт-петербурге"),
        ("погода", ""),
        ("погода.", ""),
        ("погода в Минске.", "минске"),
        ("какая погода", ""),
        ("погода Москва", "москва"),
    ]
    for phrase, expected in city_cases:
        check(f"«{phrase}»", Pyatnitsa._extract_city(phrase), expected)

    # ---------------- Нормализация названия города ----------------
    # Геокодер не понимает падежи, а «Питер» трактует буквально
    # (находит деревню в Пермском крае). Проверяем обе ловушки.
    section("Нормализация города для геокодера")
    prep_cases = [
        ("в Москве", "москве"),
        ("на Кубе", "кубе"),
        ("Москва", "москва"),
        ("Набережные", "набережные"),   # «на» — часть названия, не предлог
        ("", ""),
    ]
    for phrase, expected in prep_cases:
        check(f"снять предлог «{phrase}»", _strip_preposition(phrase), expected)

    stem_cases = [
        ("москве", ("москв", "е")),
        ("казани", ("казан", "и")),
        ("питере", ("питер", "е")),
        ("москва", ("москва", None)),
    ]
    for phrase, expected in stem_cases:
        check(f"основа «{phrase}»", _city_stem(phrase), expected)

    alias_cases = [
        ("в Питере", "Санкт-Петербург"),
        ("питер", "Санкт-Петербург"),
        ("спб", "Санкт-Петербург"),
        ("в Санкт-Петербурге", "Санкт-Петербург"),
        ("мск", "Москва"),
        ("в Нижнем Новгороде", "Нижний Новгород"),
        ("в Ростове-на-Дону", "Ростов-на-Дону"),
        ("в Набережных Челнах", "Набережные Челны"),
        ("в Москве", None),
        ("в Казани", None),
    ]
    for phrase, expected in alias_cases:
        check(f"разговорное «{phrase}»", _city_alias(phrase), expected)

    # Ключевое правило: для разговорного названия канонический город идёт первым,
    # иначе при сетевой осечке «Питер» деградирует до деревни в Пермском крае.
    check("«в Питере»: канон первым",
          _city_candidates("в Питере")[0], "Санкт-Петербург")
    check("«в Москве»: без предлога",
          _city_candidates("в Москве")[0], "москве")
    check("«в Москве»: есть форма «москва»",
          "москва" in _city_candidates("в Москве"), True)

    # ---------------- Текст напоминания ----------------
    section("Извлечение текста напоминания")
    reminder_cases = [
        ("напомни через 10 минут позвонить маме", "позвонить маме"),
        ("напомни через 5 минут выключить чайник", "выключить чайник"),
        ("напомни мне через 2 часа проверить почту", "проверить почту"),
    ]
    for phrase, expected in reminder_cases:
        check(f"«{phrase}»", pyatnitsa._extract_reminder_text(phrase), expected)

    # ---------------- Фильтрация распознавания (GigaAM / Vosk) ----------------
    section("Распознавание: артефакты, повторы и оба движка")

    class _RecognizerStub:
        """Заглушка Vosk-распознавателя."""

        def __init__(self, text):
            self.text = text
            self.words = False

        def SetWords(self, value):
            self.words = bool(value)

        def AcceptWaveform(self, _data):
            return True

        def FinalResult(self):
            return json.dumps({"text": self.text})

    class _AsrStub:
        """Заглушка GigaAM-распознавателя: тот же контракт, что у настоящего."""

        def __init__(self, text):
            self.text = text
            self.model_ready = True
            self.model_name = "gigaam-v3-rnnt-stub"
            self.calls = []

        def transcribe(self, audio, sample_rate=None):
            self.calls.append((len(audio), sample_rate))
            return self.text

        def preload(self):
            return True

    listener = Listener()
    # Vosk-путь: подменяем распознаватель, движок фиксируем явно.
    listener.engine = "vosk"
    listener._new_recognizer = lambda: _RecognizerStub("какая погода")
    check("достоверная речь проходит",
          listener.transcribe(np.array([1, 2], dtype=np.int16)), "какая погода")

    listener._new_recognizer = lambda: _RecognizerStub("")
    check("тишина даёт пустую строку",
          listener.transcribe(np.array([0], dtype=np.int16)), "")

    # GigaAM-путь: тот же вызов transcribe, другой движок внутри.
    gigaam_listener = Listener()
    gigaam_listener.engine = "gigaam"
    stub = _AsrStub("какая погода")
    gigaam_listener._asr = stub
    check("GigaAM-движок отдаёт текст",
          gigaam_listener.transcribe(np.array([1, 2], dtype=np.int16)), "какая погода")
    check("GigaAM получил PCM16 и частоту микрофона",
          stub.calls and stub.calls[0][1] == gigaam_listener.sample_rate, True)
    check("GigaAM: пустой буфер не доходит до модели",
          gigaam_listener.transcribe(np.array([], dtype=np.int16)), "")

    check("артефакт «продолжение следует» отброшен",
          Listener._clean("продолжение следует"), "")
    check("повтор одного слова отброшен", Listener._clean("ну ну ну ну"), "")
    check("обычная фраза сохраняется", Listener._clean("какая погода"), "какая погода")
    check("модель активного движка проверяема", isinstance(speech.asr_model_ready(), bool), True)
    check("имя движка для статуса непустое", bool(speech.engine_label()), True)
    check("пунктуация удаляется только для команд", normalize_command_text("погода в Минске."),
          "погода в минске")

    # ---------------- Снятие слова-активатора ----------------
    section("Снятие слова-активатора")
    check("пятница, какая погода", Pyatnitsa._strip_wake_word("пятница, какая погода"), "какая погода")
    check("Пятница который час", Pyatnitsa._strip_wake_word("Пятница который час"), "который час")
    check("какая погода", Pyatnitsa._strip_wake_word("какая погода"), "какая погода")
    check("слово-активатор — «пятница»", "пятница" in config.WAKE_WORDS, True)
    check("имя ассистента в промпте", config.ASSISTANT_NAME in config.LLM_SYSTEM_PROMPT, True)

    # ---------------- Голосовой цикл ----------------
    # Первая команда — по обращению. Проверяем оба пути: когда команда
    # прозвучала вместе с обращением и когда было сказано только «Пятница».
    section("Голосовой цикл: первая команда по обращению")

    class FakePyatnitsa(Pyatnitsa):
        """Ассистент с подменённым слухом: микрофон и Vosk не нужны."""

        def __init__(self, heard: str, command: str = ""):
            super().__init__(mode="voice", enable_tts=False)
            self._heard = heard
            self._command = command
            self.command_calls = 0
            self.acks = 0

        def _voice_available(self):
            return True

        def _listen_for_wake(self):
            return self._heard

        def _listen_command(self, *args, **kwargs):
            self.command_calls += 1
            return self._command

        def _acknowledge_wake(self):
            self.acks += 1

        def _wait_mic_clear(self):
            pass

    one_breath = FakePyatnitsa("пятница который час")
    check("команда вместе с обращением идёт сразу",
          one_breath._next_utterance(), "который час")
    check("...без второй записи", one_breath.command_calls, 0)
    check("...без отклика", one_breath.acks, 0)

    bare = FakePyatnitsa("пятница", "который час")
    check("одинокое «Пятница» ждёт команду", bare._next_utterance(), "который час")
    check("...отклик прозвучал один раз", bare.acks, 1)
    check("...команда записана один раз", bare.command_calls, 1)

    silent = FakePyatnitsa("")
    check("без обращения команды нет", silent._next_utterance(), "")
    check("...и ничего не записывается", silent.command_calls, 0)

    noise = FakePyatnitsa("пятница", "ну")
    check("шум вместо команды отброшен", noise._next_utterance(), "")
    check("...но ассистент переспросил", noise.acks, 1)

    # Регрессия: после отклика команда приходила вместе со словом-активатором
    # и не совпадала с реестром — ассистент отвечал «не расслышал».
    dice = FakePyatnitsa("пятница подбрось кости")
    check("«кости» понимаются сразу, без отклика",
          dice._next_utterance(), "подбрось кости")
    check("...отклик не звучит", dice.acks, 0)

    dice_after_ack = FakePyatnitsa("пятница", "пятница подбрось кости")
    check("повтор команды с обращением очищается",
          dice_after_ack._next_utterance(), "подбрось кости")
    check("...прозвучал один отклик", dice_after_ack.acks, 1)

    unknown = FakePyatnitsa("пятница включи музыку")
    check("незнакомая просьба уходит нейросети",
          unknown._next_utterance(), "включи музыку")
    check("...без лишнего отклика", unknown.acks, 0)
    check("«кости» — известная команда",
          pyatnitsa.match_intent("подбрось кости")[0].name, "random_choice")

    follow_unknown = FakePyatnitsa("пятница", "включи музыку")
    check("просьба после отклика не теряется",
          follow_unknown._next_utterance(), "включи музыку")

    short_noise = FakePyatnitsa("пятница", "ну да")
    check("короткий шум после отклика отброшен",
          short_noise._next_utterance(), "")

    # ---------------- Диалог без повторного обращения ----------------
    # После ответа окно диалога открыто: продолжение принимается молча.
    section("Диалог без повторного обращения")

    class DialogPyatnitsa(FakePyatnitsa):
        """Слух с очередью реплик: сначала обращение, потом продолжения."""

        def __init__(self, heard: str, replies):
            super().__init__(heard)
            self._queue = list(replies)

        def _listen_command(self, *args, **kwargs):
            self.command_calls += 1
            return self._queue.pop(0) if self._queue else ""

    check("диалог включён по умолчанию", config.DIALOG_MODE, True)
    check("окно продолжения задано", config.DIALOG_FOLLOW_UP_SEC >= 5, True)
    check("«хватит» — конец диалога", Pyatnitsa._is_dialog_end("хватит"), True)
    check("«пока» не закрывает диалог", Pyatnitsa._is_dialog_end("пока"), False)
    check("«а завтра?» — продолжение", pyatnitsa._looks_like_follow_up("а завтра"), True)
    check("«мне нужно в магазин» — не продолжение",
          pyatnitsa._looks_like_follow_up("мне нужно в магазин"), False)

    talk = DialogPyatnitsa("пятница который час", ["а завтра", "хватит"])
    check("первая реплика — по обращению", talk._next_utterance(), "который час")
    talk._open_dialog()
    check("окно диалога открылось", talk._dialog_open(), True)
    check("продолжение понято без оклика", talk._next_utterance(), "а завтра")
    check("...отклика на обращение не было", talk.acks, 0)
    check("«хватит» закрывает разговор", talk._next_utterance(), "")
    check("...окно диалога закрыто", talk._dialog_open(), False)

    quiet = DialogPyatnitsa("пятница который час", [""])
    quiet._next_utterance()
    quiet._open_dialog()
    check("тишина закрывает разговор", quiet._next_utterance(), "")
    check("...окно диалога закрыто", quiet._dialog_open(), False)

    room = DialogPyatnitsa("пятница который час", ["мне нужно в магазин"])
    room._next_utterance()
    room._open_dialog()
    check("посторонний разговор не считается продолжением",
          room._next_utterance(), "")
    check("...и окно закрывается", room._dialog_open(), False)

    back_to_wake = DialogPyatnitsa("пятница который час", [])
    back_to_wake._next_utterance()
    back_to_wake._open_dialog()
    back_to_wake._next_utterance()
    check("после тишины снова нужно обращение",
          back_to_wake._next_utterance(), "который час")

    off = config.DIALOG_MODE
    config.DIALOG_MODE = False
    try:
        disabled = DialogPyatnitsa("пятница который час", [])
        disabled._next_utterance()
        disabled._open_dialog()
        check("DIALOG_MODE=False отключает окно", disabled._dialog_open(), False)
    finally:
        config.DIALOG_MODE = off

    check("отклик есть в банке реплик", ResponseBank.has("wake_ack"), True)
    check("отклик непустой", bool(ResponseBank.get("wake_ack")), True)
    check("«ну» — не команда", pyatnitsa._looks_like_command("ну"), False)
    check("«грам» — не команда", pyatnitsa._looks_like_command("грам"), False)
    check("«погода» — команда", pyatnitsa._looks_like_command("погода"), True)
    check("свободный вопрос допускается", pyatnitsa._looks_like_command("почему небо синее"), True)
    check("отклик включается настройкой", config.WAKE_ACK_ENABLED, True)
    check("тишина в конце фразы укорочена", config.SILENCE_SEC <= 0.6, True)
    check("пауза после озвучки задана", config.MIC_CLEAR_GUARD > 0, True)
    check("долгие команды помечены для отбивки",
          sorted(Pyatnitsa._SLOW_INTENTS),
          ["analyze_screen", "briefing", "read_screen", "search", "weather"])
    check("у оратора общий пульс громкости",
          hasattr(tts.Speaker(enabled=False), "_emit_level"), True)
    check("piper не ищет несуществующий атрибут",
          hasattr(tts.Speaker(enabled=False), "_on_level"), False)
    check("слушатель отдаёт фразу целиком", hasattr(Listener, "listen_wake"), True)
    check("фоновый режим по умолчанию выключен", config.BACKGROUND_MODE, False)
    check("лимит на поиск города задан", config.WEATHER_DEADLINE <= 10, True)
    # Распознаватель поднимается заранее — до первого обращения к OCR экрана.
    check("чтение экрана поднимает распознаватель заранее",
          "_warm_recognizer" in inspect.getsource(Pyatnitsa._h_read_screen), True)
    check("анализ экрана поднимает распознаватель заранее",
          "_warm_recognizer" in inspect.getsource(Pyatnitsa._h_analyze_screen), True)

    # ---------------- Приветствие по времени суток ----------------
    # Ассистент здоровается при запуске по-разному: утром, днём, вечером и ночью.
    section("Приветствие при запуске зависит от времени суток")
    check("в 7 утра — утро", ResponseBank.part_of_day(7), "morning")
    check("в 5 утра — уже утро", ResponseBank.part_of_day(5), "morning")
    check("в 11 — ещё утро", ResponseBank.part_of_day(11), "morning")
    check("в 12 — день", ResponseBank.part_of_day(12), "day")
    check("в 17 — день", ResponseBank.part_of_day(17), "day")
    check("в 18 — вечер", ResponseBank.part_of_day(18), "evening")
    check("в 22 — вечер", ResponseBank.part_of_day(22), "evening")
    check("в 23 — ночь", ResponseBank.part_of_day(23), "night")
    check("в 2 ночи — ночь", ResponseBank.part_of_day(2), "night")
    check("в 0 — ночь", ResponseBank.part_of_day(0), "night")
    # 25 часов — это 1:00 следующих суток (ночь), 29 — 5:00 (уже утро).
    check("час за пределами суток приводится к суткам",
          ResponseBank.part_of_day(25), "night")
    check("...и граница утра считается верно", ResponseBank.part_of_day(29), "morning")
    check("утром — «Доброе утро»",
          ResponseBank.startup_greeting(7).startswith("Доброе утро"), True)
    check("днём — «Добрый день»",
          ResponseBank.startup_greeting(14).startswith("Добрый день"), True)
    check("вечером — «Добрый вечер»",
          ResponseBank.startup_greeting(20).startswith("Добрый вечер"), True)
    check("ночью — «Доброй ночи»",
          ResponseBank.startup_greeting(3).startswith("Доброй ночи"), True)
    check("приветствие всегда непустое",
          all(ResponseBank.startup_greeting(hour) for hour in range(24)), True)
    check("запуск здоровается по времени суток",
          "startup_greeting" in inspect.getsource(Pyatnitsa.run), True)

    # ---------------- Roblox ----------------
    section("Roblox: версии меняются, имя папки не зашито")
    check("у действий есть запуск Roblox", hasattr(actions.Actions, "open_roblox"), True)
    check("есть обработчик Roblox", hasattr(Pyatnitsa, "_h_roblox"), True)
    check("есть обработчик Roblox Studio", hasattr(Pyatnitsa, "_h_roblox_studio"), True)
    check("студийный обработчик просит именно Studio",
          "studio=True" in inspect.getsource(Pyatnitsa._h_roblox_studio), True)

    # Roblox при каждом обновлении создаёт новую папку version-<hex>, старые
    # остаются. Проверяем на поддельном LOCALAPPDATA, что берём свежую версию:
    # папка version-aaa старше, но её имя «больше» — сортировка по имени
    # выбрала бы именно её, и это была бы ошибка.
    with tempfile.TemporaryDirectory() as tmp:
        versions = Path(tmp) / "Roblox" / "Versions"
        for name, age in (("version-aaa", 500), ("version-bbb", 10)):
            folder = versions / name
            folder.mkdir(parents=True)
            (folder / "RobloxPlayerBeta.exe").write_bytes(b"x")
            (folder / "RobloxStudioBeta.exe").write_bytes(b"x")
            stamp = time.time() - age
            os.utime(folder, (stamp, stamp))

        saved = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = tmp
        try:
            player = actions.Actions()._roblox_candidates("RobloxPlayerBeta.exe")
            studio = actions.Actions()._roblox_candidates("RobloxStudioBeta.exe")
        finally:
            if saved is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = saved

    check("нашлись обе версии", len(player), 2)
    check("свежая версия идёт первой", player[0].parent.name, "version-bbb")
    check("старая версия остаётся в списке", player[-1].parent.name, "version-aaa")
    check("имя файла не подменяется",
          [item.name for item in player], ["RobloxPlayerBeta.exe"] * 2)
    check("Studio ищется отдельным файлом",
          [item.name for item in studio], ["RobloxStudioBeta.exe"] * 2)

    # ---------------- Список команд для пользователя ----------------
    section("Список команд в Документах")
    documented = {name for _group, name, _phrase, _text in make_commands_list.COMMANDS
                  if name}
    from_source = set(make_commands_list.intents_from_source())
    check("все команды из кода описаны", sorted(from_source - documented), [])
    check("в списке нет лишних команд", sorted(documented - from_source), [])
    check("команд в списке столько же, сколько в коде",
          len(documented), len(from_source))
    guide = make_commands_list.build_text()
    check("список не пустой", len(guide) > 2000, True)
    check("разговор без оклика объяснён",
          "БЕЗ ПОВТОРНОГО ОКЛИКА" in guide, True)
    check("папка скриншотов указана", "Скриншоты Пятницы" in guide, True)
    check("по одной фразе на команду",
          all(guide.count(phrase) >= 1 for _g, _n, phrase, _d in make_commands_list.COMMANDS
              if phrase), True)
    check("файл списка лежит в Документах",
          config.COMMANDS_FILE.name, "Пятница — команды.txt")
    check("файл списка создан", config.COMMANDS_FILE.exists(), True)

    # ---------------- Имя процесса ----------------
    section("Имя процесса в диспетчере задач")
    check("имя задано в настройках", config.PROCESS_TITLE, "Пятница")
    check("есть функция установки заголовка", hasattr(Pyatnitsa, "_set_process_title"), True)
    check("заголовок ставится при запуске",
          "_set_process_title" in inspect.getsource(Pyatnitsa.run), True)
    check("ярлык готовит копию интерпретатора",
          hasattr(make_shortcut, "pyatnitsa_exe"), True)
    check("имя копии — «Пятница.exe»", make_shortcut.PYATNITSA_EXE_NAME, "Пятница.exe")
    check("копия интерпретатора сделана",
          (make_shortcut.BASE_DIR / make_shortcut.PYATNITSA_EXE_NAME).exists(), True)

    # ---------------- Интерфейс: шина событий ----------------
    section("Интерфейс: шина событий")
    check("состояние по умолчанию",
          events.current_state() in ("idle", "loading", "listening", "thinking", "speaking"),
          True)

    channel = events.bus.subscribe()
    events.emit_state("speaking")
    events.emit_level(0.5, "tts")
    events.emit_message("user", "  привет   мир ")
    check("состояние доехало", channel.get_nowait().get("value"), "speaking")
    check("громкость доехала", channel.get_nowait().get("value"), 0.5)
    tail = channel.get_nowait()
    check("текст нормализован", tail.get("text"), "привет мир")
    check("роль реплики", tail.get("role"), "user")

    events.emit_level(4.0)
    check("громкость зажата в 0..1", channel.get_nowait().get("value"), 1.0)
    events.emit_message("user", "   ")
    check("пустая реплика не публикуется", channel.empty(), True)

    events.bus.unsubscribe(channel)
    events.emit_state("idle")
    check("после отписки событий нет", channel.empty(), True)
    check("состояние помнится и без подписчиков", events.current_state(), "idle")

    # ---------------- API ядра: единственный канал окна ----------------
    section("API ядра (канал настольного окна)")
    # Веб-версии больше нет: страницу рисует Electron, а Python отдаёт только API.
    check("веб-страницы в проекте нет", (config.BASE_DIR / "web").exists(), False)
    check("адрес API локальный",
          config.API_HOST in ("127.0.0.1", "localhost"), True)
    check("порт API задан",
          isinstance(config.API_PORT, int) and config.API_PORT > 0, True)
    check("класс API на месте", callable(api.CoreAPI), True)
    check("версия API объявлена",
          isinstance(api.VERSION, str) and api.VERSION.count(".") == 1, True)
    check("нет раздачи статики",
          hasattr(api, "resolve_static") or hasattr(api, "INDEX_FILE"), False)
    check("main.py знает флаг --no-desktop",
          '"--no-desktop"' in (config.BASE_DIR / "main.py").read_text("utf-8"), True)
    check("состояния интерфейса перечислены",
          sorted([events.STATE_IDLE, events.STATE_LISTENING, events.STATE_THINKING,
                  events.STATE_SPEAKING, events.STATE_LOADING]),
          ["idle", "listening", "loading", "speaking", "thinking"])

    # ---------------- Конфигурация ----------------
    section("Конфигурация и файлы")
    check("data-папка создана", config.DATA_DIR.exists(), True)
    check("папка моделей создана", config.MODELS_DIR.exists(), True)
    check("numpy доступен", config.FEATURES["numpy"], True)
    check("GigaAM (onnx-asr) доступен", config.FEATURES["gigaam"], True)
    check("движок распознавания по умолчанию — GigaAM", config.ASR_ENGINE, "gigaam")
    check("edge-tts доступен", config.FEATURES["edge_tts"], True)
    check("версия совпадает с __init__.py",
          f'__version__ = "{config.APP_VERSION}"' in
          (config.BASE_DIR / "__init__.py").read_text("utf-8"), True)
    check("мозг настроен на Gemini", config.GEMINI_MODEL.startswith("gemini"), True)
    check("поле ключа Gemini задано", isinstance(config.GEMINI_API_KEY, str), True)
    check("есть запасная модель", bool(config.GEMINI_FALLBACK_MODELS), True)
    check("markdown вычищается из ответа", brain.plain_text("**привет**, сэр"), "привет, сэр")

    # ---------------- Озвучка Piper ----------------
    section("Озвучка: Piper основной")
    check("движок по умолчанию — piper", config.TTS_ENGINE, "piper")
    check("модель Piper на месте", tts.piper_model_ready(), True)
    # length_scale — обратная скорость, умноженная на подъём тона профиля.
    # Считаем ожидание из настроек и каталога, иначе тест ломается при смене
    # темпа или профиля голоса.
    expected_length = round(voices.length_scale(speed=tts.piper_speed()), 3)
    check("скорость Piper переводится в length_scale",
          round(tts.Speaker._piper_config().length_scale, 3), expected_length)
    check("темп в разумных пределах", 0.5 <= tts.piper_speed() <= 2.0, True)
    silent_speaker = tts.Speaker(enabled=False, engine="auto")
    check("auto начинает с Piper", silent_speaker._engine_order()[0], "piper")
    check("резерв edge и sapi сохранён",
          silent_speaker._engine_order()[1:], ["edge", "sapi"])
    check("путь к модели скачивается при отсутствии",
          hasattr(tts, "ensure_piper_model"), True)

    # ---------------- Голос: каталог и переключение ----------------
    section("Голос Пятницы: каталог и переключение")

    check("каталог не пуст", len(voices.VOICE_CATALOG) >= 4, True)
    check("голос по умолчанию — женский",
          voices.VOICE_CATALOG[voices.DEFAULT_VOICE]["gender"], "female")
    check("активный голос есть в каталоге",
          voices.active_name() in voices.VOICE_CATALOG, True)
    check("у каждого голоса есть обязательные поля",
          [k for k in voices.VOICE_CATALOG
           if not {"title", "gender", "file", "speed"} <= set(voices.VOICE_CATALOG[k])],
          [])
    check("путь выводится из имени голоса",
          voices.model_path("irina").name, "ru_RU-irina-medium.onnx")
    check("темп голоса в разумных пределах",
          all(0.5 <= info["speed"] <= 2.0 for info in voices.VOICE_CATALOG.values()), True)
    check("женский голос скачан", voices.is_installed("irina"), True)
    check("голос по умолчанию скачан",
          voices.is_installed(voices.DEFAULT_VOICE), True)
    check("у каждого голоса есть адрес загрузки",
          [k for k in voices.VOICE_CATALOG
           if not voices.remote_url(k).startswith("https://")], [])
    check("имя файла выводится из ключа, а не из репозитория",
          voices.model_path("mari").name, "ru_RU-mari-medium.onnx")
    check("описание показывает пол",
          "женский" in voices.describe("irina"), True)

    # Смена голоса меняет путь — иначе переключатель был бы бутафорией.
    saved_voice, saved_model = config.PIPER_VOICE, config.PIPER_MODEL
    try:
        config.PIPER_VOICE, config.PIPER_MODEL = "irina", None
        irina_path = voices.active_path()
        config.PIPER_VOICE = "dmitri"
        dmitri_path = voices.active_path()
        check("смена голоса меняет файл модели",
              irina_path.name != dmitri_path.name, True)
        check("путь ведёт в папку голосов",
              irina_path.parent.name, "voices")
        # Старые настройки: PIPER_MODEL указывает на конкретный файл, а
        # PIPER_VOICE ещё не было — голос должен угадаться по имени файла.
        config.PIPER_VOICE = ""
        config.PIPER_MODEL = dmitri_path
        check("старый PIPER_MODEL угадывает голос", voices.active_name(), "dmitri")
        # Пустая строка и «.» не должны подменять голос текущим каталогом.
        config.PIPER_MODEL = Path(".")
        check("пустой путь не ломает выбор",
              voices.active_path().name, voices.model_path(voices.DEFAULT_VOICE).name)
    finally:
        config.PIPER_VOICE, config.PIPER_MODEL = saved_voice, saved_model

    # ---------------- Тембр: обработка после синтеза ----------------
    section("Тембр: профили обработки")

    check("есть профиль «без обработки»", tone.get("off"), {})
    check("неизвестный профиль не ломает голос", tone.get("нет-такого"), {})
    check("у «уверенного» профиля есть компрессор",
          "compress" in tone.get("confident"), True)
    check("профиль «мягкий» без компрессора",
          "compress" in tone.get("soft"), False)
    check("«бодрый» профиль поднимает тон",
          tone.get("brisk").get("pitch", 1.0) > 1.0, True)
    check("«Мари» звучит бодрее без правки настроек",
          voices.tone_name("mari"), "brisk")
    check("у голосов без записи обработки нет",
          voices.tone_profile("irina"), {})

    # Подъём тона и темп независимы: синтез растягивается на столько же,
    # на сколько пересчёт отсчётов его укорачивает. Ожидаемое значение берём из
    # самого профиля: раньше здесь стояло «1.10», и после подстройки тембра на
    # слух тест краснел, хотя голос работал правильно.
    brisk_pitch = round(float(tone.PROFILES["brisk"]["pitch"]), 2)
    check("тон голоса берётся из профиля",
          round(voices.tone_pitch("mari"), 2), brisk_pitch)
    check("у голоса без профиля тон не меняется",
          voices.tone_pitch("irina"), 1.0)
    check("length_scale компенсирует подъём тона",
          round(voices.length_scale("mari", 1.35), 4), round(brisk_pitch / 1.35, 4))
    check("length_scale без профиля — просто обратный темп",
          round(voices.length_scale("irina", 1.2), 4), round(1 / 1.2, 4))
    # За пределами 0.8..1.35 голос звучит «из бочки» или по-мультяшному.
    saved_brisk = tone.PROFILES["brisk"]
    tone.PROFILES["brisk"] = {"pitch": 5.0}
    try:
        check("тон ограничен сверху", voices.tone_pitch("mari"), 1.35)
    finally:
        tone.PROFILES["brisk"] = saved_brisk

    # Явная настройка сильнее каталога — иначе нельзя сравнить варианты.
    saved_tone = getattr(config, "PIPER_TONE", "")
    try:
        config.PIPER_TONE = "off"
        check("PIPER_TONE отключает обработку у «Мари»",
              voices.tone_profile("mari"), {})
        config.PIPER_TONE = "soft"
        check("PIPER_TONE перекрывает каталог",
              voices.tone_name("mari"), "soft")
        config.PIPER_TONE = "чепуха"
        check("опечатка в PIPER_TONE возвращает каталожный профиль",
              voices.tone_name("mari"), "brisk")
    finally:
        config.PIPER_TONE = saved_tone

    # Фильтры проверяем на сигнале: пик не должен вырасти, громкость — упасть.
    if tone.np is not None:
        import wave as _wave
        import tempfile as _tempfile

        rate = 22050
        samples = (0.3 * tone.np.sin(2 * tone.np.pi * 220
                                     * tone.np.arange(rate) / rate)).astype("float32")
        shaped = tone.apply(samples, rate, tone.get("confident"))
        source_rms = float(tone.np.sqrt(tone.np.mean(samples ** 2)))
        shaped_rms = float(tone.np.sqrt(tone.np.mean(shaped ** 2)))
        check("компрессор не поднимает пик",
              float(tone.np.max(tone.np.abs(shaped))) <= 0.3 + 1e-3, True)
        check("компрессор осаживает ровный тон", shaped_rms < source_rms, True)

        # Подъём тона — пересчёт отсчётов: короче ровно во столько же раз.
        shifted = tone.apply(samples, rate, {"pitch": 1.1})
        check("подъём тона укорачивает звук пропорционально",
              abs(shifted.size - samples.size / 1.1) <= 2, True)
        check("подъём тона не трогает уровень",
              abs(float(tone.np.max(tone.np.abs(shifted))) - 0.3) < 0.02, True)

        with _tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "tone.wav"
            with _wave.open(str(wav_path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(rate)
                handle.writeframes((samples * 32767).astype("int16").tobytes())
            changed = tone.apply_to_wav(wav_path, tone.get("confident"))
            with _wave.open(str(wav_path), "rb") as handle:
                result = tone.np.frombuffer(handle.readframes(handle.getnframes()),
                                            dtype="int16") / 32768.0
            check("обработка меняет файл", changed, True)
            check("громкость файла сохраняется по RMS",
                  abs(float(tone.np.sqrt(tone.np.mean(result ** 2))) - source_rms) < 0.02,
                  True)
            check("без профиля файл не трогается",
                  tone.apply_to_wav(wav_path, {}), False)

    # Подпись профиля — по ней tts понимает, что кэш устарел.
    check("подпись профиля различает варианты",
          tone.signature(tone.get("soft")) != tone.signature(tone.get("confident")),
          True)
    check("подпись пустого профиля", tone.signature({}), "off")

    # ---------------- Выключение ассистента ----------------
    # Идёт последним: shutdown() глушит общий динамик, после него проверять
    # озвучку уже нечего.
    section("Выключение: «выключайся» гасит и интерфейс")
    check("есть подписка на выключение", hasattr(Pyatnitsa, "add_shutdown_hook"), True)
    check("main.py подписывает интерфейс",
          "add_shutdown_hook(ui.stop)" in (config.BASE_DIR / "main.py").read_text("utf-8"),
          True)

    quitting = Pyatnitsa(mode="text", enable_tts=False)
    seen = []
    quitting.add_shutdown_hook(lambda: seen.append("ui"))
    quitting.shutdown()
    check("подписчик вызван", seen, ["ui"])
    check("главный цикл остановлен", quitting._running, False)
    quitting.shutdown()
    check("повторное выключение безвредно", seen, ["ui"])
    check("подписка на None не ломает",
          quitting.add_shutdown_hook(None) is None, True)

    section("Настольный оверлей (--desktop)")
    import overlay

    info = overlay.status()
    check("desktop/ на месте", overlay.DESKTOP_DIR.exists(), True)
    check("status() отдаёт ключи",
          set(info) >= {"node", "npm", "electron", "deps", "built", "stale"}, True)
    check("проверка сборки не падает", isinstance(overlay.needs_rebuild(), bool), True)
    check("main.py знает флаг --desktop",
          '"--desktop"' in (config.BASE_DIR / "main.py").read_text("utf-8"), True)
    # Сборка без исходников бессмысленна: если dist нет, needs_rebuild обязан
    # вернуть True, иначе оверлей запустится с пустой папкой.
    check("без сборки needs_rebuild = True",
          overlay.needs_rebuild() or overlay.is_built(), True)
    check("путь к Electron не выдумывается",
          overlay.electron_path() is None or overlay.electron_path().exists(), True)

    print("\n" + "=" * 50)
    print(f"Пройдено: {PASSED}   Провалено: {FAILED}")
    print("=" * 50)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
