# -*- coding: utf-8 -*-
"""Общие утилиты Джарвиса: логирование, работа со звуком, безопасная математика."""

from __future__ import annotations

import ast
import json
import math
import operator
import re
import sys
import threading
import time
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import config

# =====================================================================
#  ЛОГИРОВАНИЕ (потокобезопасное)
# =====================================================================
_log_lock = threading.Lock()
_LOG_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}


def log(message: str, level: str = "info", *, end: str = "\n", flush: bool = True) -> None:
    """Печатает сообщение, не смешивая строки из разных потоков."""
    # Уровень читаем каждый раз: config.VERBOSE может включиться уже после импорта.
    min_level = _LOG_LEVELS["debug"] if config.VERBOSE else _LOG_LEVELS["info"]
    if _LOG_LEVELS.get(level, 20) < min_level:
        return
    stamp = datetime.now().strftime("%H:%M:%S")
    tag = {"debug": "·", "info": " ", "warn": "!", "error": "×"}.get(level, " ")
    with _log_lock:
        try:
            print(f"[{stamp}]{tag} {message}", end=end, flush=flush)
        except UnicodeEncodeError:
            enc = sys.stdout.encoding or "utf-8"
            print(f"[{stamp}]{tag} {message}".encode(enc, "replace").decode(enc),
                  end=end, flush=flush)
    # В фоновом запуске (pythonw) печатать некуда — пишем файл, иначе от
    # ассистента не останется следов для отладки.
    if config.LOG_TO_FILE:
        log_to_file(f"[{level}] {message}")


def log_to_file(message: str) -> None:
    """Дублирует важное сообщение в data/jarvis.log."""
    try:
        with open(config.LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
    except OSError:
        pass


# =====================================================================
#  АУДИО
# =====================================================================
def rms(audio) -> float:
    """Среднеквадратичная громкость сигнала, нормированная в 0..1.

    Полная шкала берётся из ИСХОДНОГО целочисленного типа (int16 -> 32768), а
    не угадывается по амплитуде: раньше тихий int16-сигнал (±1) из-за эвристики
    «max > 1.5» считался плавающим и давал громкость 1.0 вместо ~3e-5.
    Плавающие массивы уже нормированы, поэтому не масштабируются.
    """
    import numpy as np

    data = np.asarray(audio)
    if data.size == 0:
        return 0.0

    if np.issubdtype(data.dtype, np.integer):
        info = np.iinfo(data.dtype)
        # Для знаковых — 2**(bits-1) (int16 -> 32768), для беззнаковых — максимум.
        scale = float(info.max) + 1.0 if info.min < 0 else float(info.max)
    else:
        scale = 1.0

    values = data.astype(np.float64)
    return float(math.sqrt(float(np.mean(values ** 2))) / scale)


def normalize_audio(audio, target_peak: float = 0.85, max_gain: float = 8.0):
    """Мягко поднимает уровень записи до target_peak, не более max_gain раз."""
    import numpy as np

    data = np.asarray(audio)
    if data.size == 0:
        return data
    original_dtype = data.dtype
    data_f = data.astype(np.float32)
    peak = float(np.max(np.abs(data_f)))
    if peak < 1e-4:
        return data
    if original_dtype == np.int16:
        ceiling = 32767.0
    elif original_dtype == np.float32:
        ceiling = 1.0
    else:
        ceiling = 32767.0
    gain = min((target_peak * ceiling) / peak, max_gain)
    return (data_f * gain).astype(original_dtype)


def to_float32(audio):
    """int16 PCM -> float32 в диапазоне [-1, 1] (формат для Whisper)."""
    import numpy as np

    data = np.asarray(audio)
    if data.dtype == np.float32:
        return data
    return (data.astype(np.float32) / 32768.0).clip(-1.0, 1.0)


# =====================================================================
#  ФАЙЛЫ
# =====================================================================
def read_json(path: Path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, data) -> bool:
    """Атомарная запись JSON: сначала во временный файл, потом подмена."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
        return True
    except OSError as exc:
        log(f"Не удалось записать {path.name}: {exc}", "warn")
        return False


# =====================================================================
#  ТЕКСТ
# =====================================================================
_PUNCT_RE = re.compile(r"[^\w\s+\-*/^().,=]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Приводит фразу к виду, удобному для сопоставления с командами."""
    if not text:
        return ""
    text = text.lower().replace("ё", "е")
    text = _PUNCT_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


@lru_cache(maxsize=1024)
def _word_boundary_re(needle: str):
    """Скомпилированное выражение «слово целиком» для одного синонима.

    Раньше ``contains_word`` собирал и компилировал регулярное выражение на
    КАЖДЫЙ вызов, а одна фраза прогоняется через все ~250 синонимов (и до
    четырёх раз за команду — при разборе составной фразы). Кэш убирает сотни
    компиляций на каждую реплику.
    """
    return re.compile(r"(?<![\w])" + re.escape(needle) + r"(?![\w])", re.UNICODE)


def contains_word(haystack: str, needle: str) -> bool:
    """Проверяет вхождение по границам слов (чтобы «час» не ловил «сейчас»)."""
    if not needle:
        return False
    return _word_boundary_re(needle).search(haystack) is not None


# =====================================================================
#  ЧИСЛА ПРОПИСЬЮ -> ЦИФРЫ
# =====================================================================
_NUM_UNITS = {
    "ноль": 0, "нуль": 0, "один": 1, "одна": 1, "одну": 1, "два": 2, "две": 2,
    "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8,
    "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
    "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16,
    "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19,
}
_NUM_TENS = {
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90,
}
_NUM_HUNDREDS = {
    "сто": 100, "двести": 200, "триста": 300, "четыреста": 400,
    "пятьсот": 500, "шестьсот": 600, "семьсот": 700, "восемьсот": 800,
    "девятьсот": 900,
}
_NUM_SCALES = {"тысяча": 1000, "тысячи": 1000, "тысяч": 1000,
               "миллион": 1_000_000, "миллиона": 1_000_000, "миллионов": 1_000_000}


def words_to_number(text: str) -> str:
    """Заменяет числа прописью на цифры: «двадцать пять плюс три» -> «25 + 3».

    Понимает составные числа (двадцать пять, сто двадцать) и убывающие разряды:
    «два миллиона триста тысяч» -> «2300000». Раньше «тысяча» после «миллиона»
    умножалась на уже собранное число, давая 2000300000.
    """
    tokens = text.split()
    result: list[str] = []
    total = 0
    current = 0
    has_number = False

    def flush():
        nonlocal total, current, has_number
        if has_number:
            result.append(str(total + current))
            total = 0
            current = 0
            has_number = False

    for token in tokens:
        word = token.strip(",.")
        if word in _NUM_UNITS:
            current += _NUM_UNITS[word]
            has_number = True
        elif word in _NUM_TENS:
            current += _NUM_TENS[word]
            has_number = True
        elif word in _NUM_HUNDREDS:
            current += _NUM_HUNDREDS[word]
            has_number = True
        elif word in _NUM_SCALES:
            scale = _NUM_SCALES[word]
            current = max(current, 1) * scale
            if scale >= 1000:          # тысяча и выше закрывают разряд
                total += current
                current = 0
            has_number = True
        else:
            flush()
            result.append(token)
    flush()
    return " ".join(result)


# =====================================================================
#  БЕЗОПАСНЫЙ КАЛЬКУЛЯТОР (без eval)
# =====================================================================
_ALLOWED_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_ALLOWED_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_ALLOWED_FUNCS = {
    "abs": abs, "round": round, "min": min, "max": max, "sqrt": math.sqrt,
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "log": math.log,
    "log10": math.log10, "log2": math.log2, "exp": math.exp, "floor": math.floor,
    "ceil": math.ceil,
}
_ALLOWED_CONSTS = {"pi": math.pi, "e": math.e, "тау": math.tau}

_MAX_EXPR_LEN = 200
_MAX_POW_EXPONENT = 1000
# Потолок на размер промежуточных целых: без него вложенные степени вида
# ((10**1000)**1000)**1000 мгновенно съедают память.
_MAX_RESULT_BITS = 4096

# Символы, которыми распознавание речи иногда обозначает умножение.
_MUL_CHARS = "xх×"


class CalcError(ValueError):
    """Выражение недопустимо или слишком сложное."""


def _guard(value):
    """Отсекает комплексные/нечисловые/слишком большие результаты."""
    if isinstance(value, bool) or isinstance(value, complex):
        raise CalcError("результат не является действительным числом")
    if isinstance(value, int):
        if value.bit_length() > _MAX_RESULT_BITS:
            raise CalcError("число слишком большое")
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise CalcError("результат не является числом")
        return value
    raise CalcError("недопустимое значение")


def _predicted_pow_bits(base, exponent):
    """Оценка сверху числа бит результата ``base ** exponent`` (или None).

    Считается ДО возведения в степень: ``bit_length`` базы, умноженный на
    показатель, не меньше реального числа бит и не требует перевода огромного
    int в float (что само по себе могло бы переполниться).
    """
    if not isinstance(base, int) or isinstance(base, bool):
        return None
    if not isinstance(exponent, int) or isinstance(exponent, bool) or exponent <= 0:
        return None
    if base in (0, 1, -1):
        return 1
    return base.bit_length() * exponent


def _normalize_multiply(expr: str) -> str:
    """Заменяет «иксы» на ``*`` только между операндами.

    Глобальная замена (как было раньше) ломала имена функций: ``exp`` и ``max``
    превращались в ``e*p``/``ma*``. Теперь символ умножения принимается лишь
    тогда, когда слева стоит цифра или ``)``, а справа — цифра или ``(``.
    Это покрывает «2x3», «2 х 3», «2×3», «2x(3+1)», но не трогает ``exp(2)``.
    """
    pattern = r"(?<=[\d)])\s*[" + re.escape(_MUL_CHARS) + r"]\s*(?=[\d(])"
    return re.sub(pattern, "*", expr)


def _normalize_commas(expr: str) -> str:
    """Запятые внутри вызовов функций — разделители аргументов, вне — десятичные.

    Внутри вызовов десятичный разделитель — точка: ``max(1.5, 2)``. Вне вызовов
    десятичная запятая сохраняется в прежнем смысле: ``1,5 + 2`` -> ``1.5 + 2``.
    """
    out: list[str] = []
    stack: list[bool] = []          # True, если скобка открывает вызов функции
    skip_space = False
    for ch in expr:
        if skip_space:
            if ch.isspace():
                continue
            skip_space = False
        if ch == "(":
            j = len(out) - 1
            while j >= 0 and out[j].isspace():
                j -= 1
            stack.append(j >= 0 and out[j].isalpha())
            out.append(ch)
        elif ch == ")":
            if stack:
                stack.pop()
            out.append(ch)
        elif ch == ",":
            if stack and stack[-1]:
                out.append(",")     # разделитель аргументов
            else:
                while out and out[-1].isspace():
                    out.pop()
                out.append(".")     # десятичная запятая
                skip_space = True
        else:
            out.append(ch)
    return "".join(out)


def _eval_node(node):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise CalcError("разрешены только числа")
        return _guard(node.value)

    if isinstance(node, ast.BinOp):
        op = _ALLOWED_BINOPS.get(type(node.op))
        if op is None:
            raise CalcError("недопустимая операция")
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Pow):
            if isinstance(right, bool) or not isinstance(right, (int, float)):
                raise CalcError("недопустимая степень")
            if abs(right) > _MAX_POW_EXPONENT:
                raise CalcError("слишком большая степень")
            predicted = _predicted_pow_bits(left, right)
            if predicted is not None and predicted > _MAX_RESULT_BITS:
                raise CalcError("результат слишком большой")
        try:
            result = op(left, right)
        except (ZeroDivisionError, ValueError, TypeError, OverflowError) as exc:
            raise CalcError(f"ошибка вычисления: {exc}") from exc
        return _guard(result)

    if isinstance(node, ast.UnaryOp):
        op = _ALLOWED_UNARYOPS.get(type(node.op))
        if op is None:
            raise CalcError("недопустимая операция")
        return _guard(op(_eval_node(node.operand)))

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise CalcError("недопустимый вызов функции")
        func = _ALLOWED_FUNCS.get(node.func.id)
        if func is None:
            raise CalcError(f"функция {node.func.id} недоступна")
        if len(node.args) > 4:
            raise CalcError("слишком много аргументов")
        args = [_eval_node(a) for a in node.args]
        try:
            result = func(*args)
        except (ZeroDivisionError, ValueError, TypeError, OverflowError) as exc:
            raise CalcError(f"ошибка вычисления: {exc}") from exc
        return _guard(result)

    if isinstance(node, ast.Name):
        if node.id in _ALLOWED_CONSTS:
            return _guard(_ALLOWED_CONSTS[node.id])
        raise CalcError(f"неизвестное имя {node.id}")

    raise CalcError("недопустимое выражение")


def safe_calculate(expression: str):
    """Считает арифметическое выражение без eval. Бросает CalcError при ошибке."""
    expr = (expression or "").strip()
    if not expr:
        raise CalcError("пустое выражение")
    if len(expr) > _MAX_EXPR_LEN:
        raise CalcError("выражение слишком длинное")
    # Нормализуем символы, которые часто приходят из распознавания речи.
    expr = (expr.replace("^", "**").replace(":", "/")
                .replace("÷", "/").replace("—", "-").replace("–", "-"))
    expr = _normalize_multiply(expr)
    expr = _normalize_commas(expr)
    expr = re.sub(r"(?<=\d)\s+(?=\d)", "", expr)  # «2 2» -> «22»
    if not re.fullmatch(r"[\d\s+\-*/().,_a-zA-Zа-яА-Я]+", expr):
        raise CalcError("в выражении есть посторонние символы")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise CalcError("не удалось разобрать выражение") from exc
    result = _eval_node(tree.body)
    if isinstance(result, float):
        if math.isnan(result) or math.isinf(result):
            raise CalcError("результат не является числом")
        if result.is_integer():
            result = int(result)
        else:
            result = round(result, 6)
    return result


# =====================================================================
#  ПРОЧЕЕ
# =====================================================================
def plural_ru(count: int, one: str, few: str, many: str) -> str:
    """Русская форма слова по числу: 1 процент, 2 процента, 5 процентов.

    Нужна для ответов, которые произносит ассистент: «на 51 процентов» звучит
    неграмотно. Учитываются исключения 11–14, где всегда форма «много».
    """
    value = abs(int(count)) % 100
    if 11 <= value <= 19:
        return many
    last = value % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def human_duration(seconds: float) -> str:
    """«90» -> «1 минута 30 секунд»."""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} сек"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} мин {sec} сек" if sec else f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def timestamp() -> float:
    return time.time()
