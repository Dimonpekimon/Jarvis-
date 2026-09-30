# -*- coding: utf-8 -*-
"""Быстрые проверки логики Джарвиса без микрофона и без Ollama.

Запуск:  python selftest.py
"""

from __future__ import annotations

import inspect
import os
import sys
import tempfile
import time
from pathlib import Path

import actions
import config
import events
import jarvis_core
import make_commands_list
import make_shortcut
import tts
import webui
from actions import (
    _city_alias,
    _city_candidates,
    _city_stem,
    _strip_preposition,
    calculate,
    parse_duration,
    prepare_expression,
)
from jarvis_core import Jarvis
from responses import ResponseBank
from speech import Listener
from utils import CalcError, plural_ru, safe_calculate

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
    print("Проверка Джарвиса v3.0")

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
    jarvis = Jarvis(mode="text", enable_tts=False)

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
        # Выключение ассистента. «Отключи» без уточнения — это про Джарвиса,
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
        intent, alias = jarvis.match_intent(phrase)
        got = intent.name if intent else None
        check(f"«{phrase}»", got, expected_intent)

    # ---------------- Обработчики команд ----------------
    # Каждая команда из реестра обязана иметь метод-обработчик: без него фраза
    # молча уходит в LLM. Именно так «повтори ответ» и «отмени последнюю задачу»
    # однажды перестали работать — реестр их знал, а обработчиков не было.
    section("У каждой команды реестра есть обработчик")
    missing_handlers = [
        intent.name for intent in jarvis_core.INTENTS
        if not callable(getattr(Jarvis, intent.handler, None))
    ]
    check("все обработчики из INTENTS существуют", missing_handlers, [])
    check("у каждой команды есть хотя бы один синоним",
          all(intent.aliases for intent in jarvis_core.INTENTS), True)
    check("имена команд в реестре уникальны",
          len({intent.name for intent in jarvis_core.INTENTS}), len(jarvis_core.INTENTS))

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
        check(f"«{phrase}»", len(jarvis._split_compound(phrase)), expected_parts)

    # ---------------- Извлечение города ----------------
    section("Извлечение города из фразы")
    city_cases = [
        ("какая погода в Москве", "москве"),
        ("погода в санкт-петербурге", "санкт-петербурге"),
        ("погода", ""),
        ("какая погода", ""),
        ("погода Москва", "москва"),
    ]
    for phrase, expected in city_cases:
        check(f"«{phrase}»", Jarvis._extract_city(phrase), expected)

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
        check(f"«{phrase}»", jarvis._extract_reminder_text(phrase), expected)

    # ---------------- Снятие слова-активатора ----------------
    section("Снятие слова-активатора")
    check("джарвис, какая погода", Jarvis._strip_wake_word("джарвис, какая погода"), "какая погода")
    check("Джарвис который час", Jarvis._strip_wake_word("Джарвис который час"), "который час")
    check("какая погода", Jarvis._strip_wake_word("какая погода"), "какая погода")

    # ---------------- Голосовой цикл ----------------
    # Правило: одна команда на одно «Джарвис». Проверяем оба пути — когда команда
    # прозвучала вместе с обращением и когда было сказано только «Джарвис».
    section("Голосовой цикл: одна команда на одно «Джарвис»")

    class FakeJarvis(Jarvis):
        """Джарвис с подменённым слухом: микрофон и Whisper не нужны."""

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

        def _listen_command(self):
            self.command_calls += 1
            return self._command

        def _acknowledge_wake(self):
            self.acks += 1

        def _wait_mic_clear(self):
            pass

    one_breath = FakeJarvis("джарвис который час")
    check("команда вместе с обращением идёт сразу",
          one_breath._next_utterance(), "который час")
    check("...без второй записи", one_breath.command_calls, 0)
    check("...без отклика", one_breath.acks, 0)

    bare = FakeJarvis("джарвис", "который час")
    check("одинокое «Джарвис» ждёт команду", bare._next_utterance(), "который час")
    check("...отклик прозвучал один раз", bare.acks, 1)
    check("...команда записана один раз", bare.command_calls, 1)

    silent = FakeJarvis("")
    check("без обращения команды нет", silent._next_utterance(), "")
    check("...и ничего не записывается", silent.command_calls, 0)

    noise = FakeJarvis("джарвис", "ну")
    check("шум вместо команды отброшен", noise._next_utterance(), "")
    check("...но ассистент переспросил", noise.acks, 1)

    check("отклик есть в банке реплик", ResponseBank.has("wake_ack"), True)
    check("отклик непустой", bool(ResponseBank.get("wake_ack")), True)
    check("«ну» — не команда", Jarvis._looks_like_command("ну"), False)
    check("«погода» — команда", Jarvis._looks_like_command("погода"), True)
    check("отклик включается настройкой", config.WAKE_ACK_ENABLED, True)
    check("тишина в конце фразы укорочена", config.SILENCE_SEC <= 0.6, True)
    check("пауза после озвучки задана", config.MIC_CLEAR_GUARD > 0, True)
    check("долгие команды помечены для отбивки",
          sorted(Jarvis._SLOW_INTENTS),
          ["analyze_screen", "briefing", "read_screen", "search", "weather"])
    check("у оратора общий пульс громкости",
          hasattr(tts.Speaker(enabled=False), "_emit_level"), True)
    check("piper не ищет несуществующий атрибут",
          hasattr(tts.Speaker(enabled=False), "_on_level"), False)
    check("слушатель отдаёт фразу целиком", hasattr(Listener, "listen_wake"), True)
    check("фоновый режим по умолчанию выключен", config.BACKGROUND_MODE, False)
    check("лимит на поиск города задан", config.WEATHER_DEADLINE <= 10, True)
    # Сегфолт: winrt (OCR), импортированный раньше Whisper, роняет загрузку модели.
    check("чтение экрана поднимает Whisper заранее",
          "_warm_recognizer" in inspect.getsource(Jarvis._h_read_screen), True)
    check("анализ экрана поднимает Whisper заранее",
          "_warm_recognizer" in inspect.getsource(Jarvis._h_analyze_screen), True)

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
          "startup_greeting" in inspect.getsource(Jarvis.run), True)

    # ---------------- Roblox ----------------
    section("Roblox: версии меняются, имя папки не зашито")
    check("у действий есть запуск Roblox", hasattr(actions.Actions, "open_roblox"), True)
    check("есть обработчик Roblox", hasattr(Jarvis, "_h_roblox"), True)
    check("есть обработчик Roblox Studio", hasattr(Jarvis, "_h_roblox_studio"), True)
    check("студийный обработчик просит именно Studio",
          "studio=True" in inspect.getsource(Jarvis._h_roblox_studio), True)

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
    check("правило «одна команда на одно Джарвис» объяснено",
          "ОДНА КОМАНДА НА ОДНО" in guide, True)
    check("папка скриншотов указана", "Скриншоты Джарвиса" in guide, True)
    check("по одной фразе на команду",
          all(guide.count(phrase) >= 1 for _g, _n, phrase, _d in make_commands_list.COMMANDS
              if phrase), True)
    check("файл списка лежит в Документах",
          config.COMMANDS_FILE.name, "Джарвис — команды.txt")
    check("файл списка создан", config.COMMANDS_FILE.exists(), True)

    # ---------------- Имя процесса ----------------
    section("Имя процесса в диспетчере задач")
    check("имя задано в настройках", config.PROCESS_TITLE, "Джарвис")
    check("есть функция установки заголовка", hasattr(Jarvis, "_set_process_title"), True)
    check("заголовок ставится при запуске",
          "_set_process_title" in inspect.getsource(Jarvis.run), True)
    check("ярлык готовит копию интерпретатора",
          hasattr(make_shortcut, "jarvis_exe"), True)
    check("имя копии — «Джарвис.exe»", make_shortcut.JARVIS_EXE_NAME, "Джарвис.exe")
    check("копия интерпретатора сделана",
          (make_shortcut.BASE_DIR / make_shortcut.JARVIS_EXE_NAME).exists(), True)

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

    # ---------------- Интерфейс: раздача файлов ----------------
    section("Интерфейс: раздача файлов")
    check("страница интерфейса есть", webui.INDEX_FILE.exists(), True)
    check("корень отдаёт index.html", webui.resolve_static("/").name, "index.html")
    check("выход из папки заблокирован", webui.resolve_static("/../config.py"), None)
    check("выход в системную папку заблокирован",
          webui.resolve_static("/../../Windows/win.ini"), None)
    check("неизвестный файл", webui.resolve_static("/nope.html"), None)
    check("интерфейс включён по умолчанию", config.WEB_UI_ENABLED, True)
    check("порт интерфейса задан", isinstance(config.WEB_UI_PORT, int), True)
    check("состояния интерфейса перечислены",
          sorted([events.STATE_IDLE, events.STATE_LISTENING, events.STATE_THINKING,
                  events.STATE_SPEAKING, events.STATE_LOADING]),
          ["idle", "listening", "loading", "speaking", "thinking"])

    # ---------------- Конфигурация ----------------
    section("Конфигурация и файлы")
    check("data-папка создана", config.DATA_DIR.exists(), True)
    check("папка моделей создана", config.MODELS_DIR.exists(), True)
    check("numpy доступен", config.FEATURES["numpy"], True)
    check("faster-whisper доступен", config.FEATURES["faster_whisper"], True)
    check("edge-tts доступен", config.FEATURES["edge_tts"], True)

    # ---------------- Выключение ассистента ----------------
    # Идёт последним: shutdown() глушит общий динамик, после него проверять
    # озвучку уже нечего.
    section("Выключение: «выключайся» гасит и интерфейс")
    check("есть подписка на выключение", hasattr(Jarvis, "add_shutdown_hook"), True)
    check("main.py подписывает интерфейс",
          "add_shutdown_hook(ui.stop)" in (config.BASE_DIR / "main.py").read_text("utf-8"),
          True)

    quitting = Jarvis(mode="text", enable_tts=False)
    seen = []
    quitting.add_shutdown_hook(lambda: seen.append("ui"))
    quitting.shutdown()
    check("подписчик вызван", seen, ["ui"])
    check("главный цикл остановлен", quitting._running, False)
    quitting.shutdown()
    check("повторное выключение безвредно", seen, ["ui"])
    check("подписка на None не ломает",
          quitting.add_shutdown_hook(None) is None, True)

    print("\n" + "=" * 50)
    print(f"Пройдено: {PASSED}   Провалено: {FAILED}")
    print("=" * 50)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
