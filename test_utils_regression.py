# -*- coding: utf-8 -*-
"""Регрессионные тесты :mod:`utils` — только стандартная библиотека.

Фиксируют именно те дефекты, которые были в utils.py:

* ``safe_calculate`` глобально менял «x»/«х»/«×» на ``*``, ломая имена функций
  (``exp`` -> ``e*p``, ``max`` -> ``ma*``), а запятые внутри вызовов
  превращал в десятичные точки (``max(1,2)`` -> ``max(1.2)``);
* вложенные степени вида ``(10**1000)**1000`` не ограничивались и могли
  выделить гигантские целые;
* ``ZeroDivisionError``/``ValueError``/``TypeError`` вылетали наружу вместо
  ``CalcError``; комплексный результат тоже не отсекался;
* ``words_to_number`` неверно складывал убывающие разряды
  («два миллиона триста тысяч» -> 2000300000);
* ``rms`` угадывал масштаб по амплитуде, из-за чего тихий int16-сигнал (±1)
  считался полной шкалой (1.0 вместо ~3e-5).

Тесты изолированы: не трогают файлы, сеть и звуковое оборудование.
Тесты ``rms`` требуют numpy и пропускаются, если его нет.
"""

from __future__ import annotations

import math
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import utils  # noqa: E402
from utils import CalcError, rms, safe_calculate, words_to_number  # noqa: E402

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:  # pragma: no cover - зависит от окружения
    np = None
    HAS_NUMPY = False


class MultiplyNormalizationTests(unittest.TestCase):
    """«Икс» как знак умножения — только между операндами."""

    def test_latin_x_between_digits(self):
        self.assertEqual(safe_calculate("2x3"), 6)

    def test_cyrillic_kha_between_digits(self):
        self.assertEqual(safe_calculate("2 х 3"), 6)

    def test_multiplication_sign_between_digits(self):
        self.assertEqual(safe_calculate("2×3"), 6)

    def test_x_before_parenthesis(self):
        self.assertEqual(safe_calculate("2x(3+1)"), 8)

    def test_x_after_parenthesis(self):
        self.assertEqual(safe_calculate("(2+1)x3"), 9)

    def test_exp_is_not_broken(self):
        # Раньше превращалось в «e*p(2)» и падало.
        self.assertAlmostEqual(safe_calculate("exp(1)"), math.e, places=5)

    def test_max_is_not_broken(self):
        # Раньше превращалось в «ma*(3, 7)».
        self.assertEqual(safe_calculate("max(3, 7)"), 7)

    def test_exp_call_with_argument(self):
        self.assertAlmostEqual(safe_calculate("exp(0)"), 1.0, places=6)


class CommaHandlingTests(unittest.TestCase):
    """Запятые: разделитель аргументов внутри вызова, иначе — десятичные."""

    def test_comma_inside_call_is_separator(self):
        self.assertEqual(safe_calculate("max(1,2)"), 2)

    def test_comma_inside_call_three_args(self):
        self.assertEqual(safe_calculate("max(1,2,3)"), 3)

    def test_decimal_comma_outside_call(self):
        self.assertEqual(safe_calculate("1,5 + 2"), 3.5)

    def test_decimal_comma_with_spaces(self):
        self.assertEqual(safe_calculate("1 , 5 + 1"), 2.5)

    def test_decimal_dot_inside_call(self):
        self.assertEqual(safe_calculate("max(1.5, 2)"), 2)

    def test_comma_inside_call_is_not_decimal(self):
        # Внутри вызова запятая — разделитель, поэтому sqrt(2,25) — это два
        # аргумента, а не sqrt(2.25); десятичный разделитель внутри вызова — точка.
        with self.assertRaises(CalcError):
            safe_calculate("sqrt(2,25)")

    def test_dot_inside_call_works(self):
        self.assertEqual(safe_calculate("sqrt(2.25)"), 1.5)


class PowBoundingTests(unittest.TestCase):
    """Ограничение размера промежуточных чисел и предсказание роста степени."""

    def test_simple_pow_still_works(self):
        self.assertEqual(safe_calculate("2**10"), 1024)

    def test_negative_exponent(self):
        self.assertEqual(safe_calculate("2**-2"), 0.25)

    def test_huge_exponent_rejected(self):
        with self.assertRaises(CalcError):
            safe_calculate("9**9**9")

    def test_nested_pow_rejected_quickly(self):
        # Раньше это пыталось выделить ~10**(10**9) бит.
        start = time.monotonic()
        with self.assertRaises(CalcError):
            safe_calculate("(10**1000)**1000")
        self.assertLess(time.monotonic() - start, 1.0)

    def test_deeply_nested_pow_rejected(self):
        start = time.monotonic()
        with self.assertRaises(CalcError):
            safe_calculate("((10**1000)**1000)**1000")
        self.assertLess(time.monotonic() - start, 1.0)

    def test_moderately_large_pow_allowed(self):
        # 10**1000 (~3322 бита) укладывается в лимит 4096 бит.
        result = safe_calculate("10**1000")
        self.assertEqual(result, 10 ** 1000)


class ErrorNormalizationTests(unittest.TestCase):
    """Ошибки вычисления нормализуются в CalcError (наружу не летят)."""

    def test_division_by_zero(self):
        with self.assertRaises(CalcError):
            safe_calculate("1/0")

    def test_zero_to_negative_power(self):
        with self.assertRaises(CalcError):
            safe_calculate("0**-1")

    def test_modulo_by_zero(self):
        with self.assertRaises(CalcError):
            safe_calculate("5 % 0")

    def test_log_of_zero(self):
        with self.assertRaises(CalcError):
            safe_calculate("log(0)")

    def test_sqrt_of_negative(self):
        with self.assertRaises(CalcError):
            safe_calculate("sqrt(-1)")

    def test_complex_result_rejected(self):
        # (-8) ** 0.333 даёт комплексное число.
        with self.assertRaises(CalcError):
            safe_calculate("(-8)**0.333")

    def test_exp_overflow_rejected(self):
        with self.assertRaises(CalcError):
            safe_calculate("exp(10000)")

    def test_wrong_arity_is_calc_error(self):
        with self.assertRaises(CalcError):
            safe_calculate("sqrt(1, 2)")

    def test_zero_division_is_not_raw(self):
        # Явная проверка, что наружу не вылетает ZeroDivisionError.
        try:
            safe_calculate("1/0")
        except CalcError:
            pass
        except ZeroDivisionError:  # pragma: no cover
            self.fail("ZeroDivisionError вылетел наружу вместо CalcError")


class WordsToNumberTests(unittest.TestCase):
    """Убывающие разряды: миллион -> тысяча больше не умножаются друг на друга."""

    def test_two_million_three_hundred_thousand(self):
        self.assertEqual(words_to_number("два миллиона триста тысяч"), "2300000")

    def test_million_thousand_plus_units(self):
        self.assertEqual(
            words_to_number("два миллиона триста тысяч пятьдесят"), "2300050"
        )

    def test_full_composite(self):
        self.assertEqual(
            words_to_number(
                "сто двадцать три тысячи четыреста пятьдесят шесть"
            ),
            "123456",
        )

    def test_two_thousand(self):
        self.assertEqual(words_to_number("две тысячи"), "2000")

    def test_standalone_thousand(self):
        self.assertEqual(words_to_number("тысяча"), "1000")

    def test_simple_sum_still_works(self):
        # Слова-операторы остаются словами — их заменяет prepare_expression.
        self.assertEqual(words_to_number("двадцать пять плюс три"), "25 плюс 3")

    def test_million_alone(self):
        self.assertEqual(words_to_number("один миллион"), "1000000")


@unittest.skipUnless(HAS_NUMPY, "numpy недоступен")
class RmsTests(unittest.TestCase):
    """rms использует исходный целочисленный тип, а не эвристику по амплитуде."""

    def test_int16_small_values_are_quiet(self):
        data = np.array([1, -1, 1, -1], dtype=np.int16)
        self.assertAlmostEqual(rms(data), 1.0 / 32768.0, places=10)

    def test_int16_full_scale(self):
        data = np.array([32767, -32768, 32767, -32768], dtype=np.int16)
        self.assertAlmostEqual(rms(data), 1.0, places=4)

    def test_int16_zero(self):
        data = np.zeros(8, dtype=np.int16)
        self.assertEqual(rms(data), 0.0)

    def test_empty_is_zero(self):
        self.assertEqual(rms(np.array([], dtype=np.int16)), 0.0)

    def test_float32_behavior_preserved(self):
        # Плавающий сигнал уже нормирован и не масштабируется.
        data = np.array([0.5, -0.5, 0.5, -0.5], dtype=np.float32)
        self.assertAlmostEqual(rms(data), 0.5, places=6)

    def test_float64_behavior_preserved(self):
        data = np.array([0.25, -0.25], dtype=np.float64)
        self.assertAlmostEqual(rms(data), 0.25, places=6)

    def test_int16_quiet_not_full_scale(self):
        # Ключевая регрессия: раньше ±1 давало 1.0.
        quiet = rms(np.array([1, -1], dtype=np.int16))
        self.assertLess(quiet, 1e-3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
