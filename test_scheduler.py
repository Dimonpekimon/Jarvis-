# -*- coding: utf-8 -*-
"""Тесты для :mod:`scheduler` (только стандартная библиотека).

Проверяются подтверждённые баги и требования:
  * таймеры сохраняются (раньше ``add_timer`` не писал файл);
  * метки таймеров не теряются при перезапуске;
  * восстановление устойчиво к мусору и идемпотентно по дубликатам id;
  * отмена по виду/последней задаче не трогает лишнее;
  * сбой записи не выдаёт «сохранено»;
  * после остановки старые таймеры не проговаривают и новые не планируются;
  * конкурентные добавления не ломают состояние.

Тесты НИКОГДА не трогают настоящий ``data/reminders.json`` и не запускают
настоящие уведомления: путь подменяется на временный файл, а ``Timer`` —
на фейковый, который «срабатывает» только по явному вызову.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
import scheduler as scheduler_mod  # noqa: E402
from scheduler import Scheduler  # noqa: E402


class FakeTimer:
    """Замена ``threading.Timer``: ничего не запускает, срабатывает вручную."""

    instances: list = []

    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval = interval
        self.function = function
        self.args = tuple(args or ())
        self.kwargs = dict(kwargs or {})
        self.daemon = False
        self.started = False
        self.cancelled = False
        FakeTimer.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    # --- вспомогательное для тестов ---------------------------------
    def fire(self):
        """Имитирует нормальное срабатывание (учитывает cancel)."""
        if self.cancelled:
            return
        self.function(*self.args, **self.kwargs)

    def force_fire(self):
        """Имитирует гонку: callback выполняется уже после cancel/stop."""
        self.function(*self.args, **self.kwargs)

    @classmethod
    def reset(cls):
        cls.instances = []

    @classmethod
    def live(cls):
        return [t for t in cls.instances if t.started and not t.cancelled]


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        FakeTimer.reset()
        self._tmp = __import__("tempfile").TemporaryDirectory()
        self.reminders_path = Path(self._tmp.name) / "reminders.json"
        self.addCleanup(self._tmp.cleanup)

        self._patchers = [
            mock.patch.object(config, "REMINDERS_FILE", self.reminders_path),
            mock.patch.object(scheduler_mod.threading, "Timer", FakeTimer),
            mock.patch.object(scheduler_mod, "log", mock.Mock()),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.spoken: list = []
        self.notified: list = []
        self.sched = self._make_scheduler()

    def _make_scheduler(self) -> Scheduler:
        return Scheduler(
            speak_fn=self.spoken.append,
            notify_fn=lambda title, message: self.notified.append((title, message)),
        )

    def _read_file(self):
        with open(self.reminders_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_file(self, data):
        with open(self.reminders_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, allow_nan=True)

    # ------------------------------------------------------------------
    #  Базовое
    # ------------------------------------------------------------------
    def test_count_property_starts_empty(self):
        self.assertEqual(self.sched.count, 0)

    def test_add_timer_persists_to_disk(self):
        """Регресс: add_timer раньше не вызывал сохранение."""
        self.sched.add_timer(60, "чайник")
        self.assertEqual(self.sched.count, 1)
        data = self._read_file()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["kind"], "timer")
        self.assertEqual(data[0]["label"], "чайник")

    def test_add_timer_rejects_invalid_and_bounded_delay(self):
        before = self.sched.count
        self.assertIn("Не понял", self.sched.add_timer(-5))
        self.assertIn("Не понял", self.sched.add_timer(0))
        self.assertIn("Не понял", self.sched.add_timer(float("nan")))
        self.assertIn("Не понял", self.sched.add_timer(float("inf")))
        self.assertIn("Не понял", self.sched.add_timer("abc"))
        self.assertEqual(self.sched.count, before)

        self.sched.add_timer(10 ** 12, "огромный")
        item = next(iter(self.sched._items.values()))
        self.assertLessEqual(item["due_at"] - item["created_at"],
                             Scheduler._MAX_DELAY + 1)

    def test_add_reminder_rejects_invalid_delay(self):
        before = self.sched.count
        self.assertIn("Не понял", self.sched.add_reminder(float("nan"), "x"))
        self.assertIn("Не понял", self.sched.add_reminder(-1, "x"))
        self.assertEqual(self.sched.count, before)

    # ------------------------------------------------------------------
    #  Перезапуск и метки
    # ------------------------------------------------------------------
    def test_restart_restores_timer_with_label(self):
        self.sched.add_timer(60, "чайник")
        file_before = self._read_file()
        self.assertEqual(file_before[0]["label"], "чайник")

        # «Перезапуск»: новый планировщик читает тот же файл.
        restarted = self._make_scheduler()
        restored = restarted.restore()
        self.assertEqual(restored, 1)
        self.assertEqual(restarted.count, 1)

        # Восстановленный таймер несёт исходную метку.
        timer = FakeTimer.instances[-1]
        timer.fire()
        self.assertEqual(self.spoken, ["чайник"])
        self.assertEqual(self.notified, [("Джарвис", "чайник")])
        self.assertEqual(restarted.count, 0)

    def test_restart_restores_reminder_text(self):
        self.sched.add_reminder(120, "позвонить маме")
        restarted = self._make_scheduler()
        self.assertEqual(restarted.restore(), 1)
        FakeTimer.instances[-1].fire()
        self.assertEqual(self.spoken, ["Сэр, напоминаю: позвонить маме"])

    def test_expired_reminder_fires_immediately_on_restore(self):
        import time
        self._write_file([
            {"id": "reminder-old-1", "kind": "reminder", "text": "старое",
             "due_at": time.time() - 100, "created_at": time.time() - 200},
        ])
        restarted = self._make_scheduler()
        self.assertEqual(restarted.restore(), 1)
        timer = FakeTimer.instances[-1]
        # Просроченное проговаривается почти сразу (семантика сохранена).
        self.assertAlmostEqual(timer.interval, 1.0, places=6)
        timer.fire()
        self.assertEqual(self.spoken, ["Сэр, напоминаю: старое"])

    # ------------------------------------------------------------------
    #  Восстановление: мусор и дубликаты
    # ------------------------------------------------------------------
    def test_restore_survives_malformed_entries(self):
        import time
        future = time.time() + 1000
        self._write_file([
            "not a dict",
            42,
            None,
            {"id": 123, "kind": "timer", "due_at": future},          # id не str
            {"id": "", "kind": "timer", "due_at": future},           # пустой id
            {"id": "bad-kind", "kind": "alarm", "due_at": future},   # неизвестный вид
            {"id": "nan-due", "kind": "timer", "due_at": float("nan")},
            {"id": "inf-due", "kind": "timer", "due_at": float("inf")},
            {"id": "no-due", "kind": "timer"},                       # нет due_at
            {"id": "bad-created", "kind": "timer", "due_at": future,
             "created_at": "oops"},                                  # битый created_at
            {"id": "ok-timer", "kind": "timer", "label": "метка",
             "due_at": future, "created_at": future},
            {"id": "ok-rem", "kind": "reminder", "text": "текст",
             "due_at": future},
        ])
        restarted = self._make_scheduler()
        restored = restarted.restore()  # не должно бросать исключений
        # Прошли только три корректные записи (bad-created восстановлен с now).
        self.assertEqual(restored, 3)
        self.assertEqual(restarted.count, 3)
        self.assertIn("ok-timer", restarted._items)
        self.assertIn("ok-rem", restarted._items)
        self.assertIn("bad-created", restarted._items)

    def test_restore_is_idempotent_for_duplicate_ids(self):
        import time
        future = time.time() + 1000
        entry = {"id": "dup-1", "kind": "reminder", "text": "один",
                 "due_at": future, "created_at": future}
        self._write_file([entry, dict(entry, text="два")])

        restarted = self._make_scheduler()
        self.assertEqual(restarted.restore(), 1)  # дубликат id пропущен
        self.assertEqual(restarted.count, 1)
        # Повторный вызов ничего не добавляет.
        self.assertEqual(restarted.restore(), 0)
        self.assertEqual(restarted.count, 1)

    def test_restore_ignores_non_list_payload(self):
        self._write_file({"not": "a list"})
        self.assertEqual(self._make_scheduler().restore(), 0)

    # ------------------------------------------------------------------
    #  Отмена: вид / последняя / совпадение
    # ------------------------------------------------------------------
    def test_cancel_kind_only_removes_that_kind(self):
        self.sched.add_timer(100, "t1")
        self.sched.add_reminder(100, "r1")
        self.sched.add_timer(100, "t2")
        self.sched.add_reminder(100, "r2")
        self.assertEqual(self.sched.count, 4)

        message = self.sched.cancel_kind("timer")
        self.assertEqual(self.sched.count, 2)
        remaining = {item["kind"] for item in self.sched._items.values()}
        self.assertEqual(remaining, {"reminder"})
        self.assertIn("2", message)

        self.sched.cancel_kind("reminder")
        self.assertEqual(self.sched.count, 0)

    def test_cancel_kind_invalid_is_noop(self):
        self.sched.add_reminder(100, "r1")
        self.assertIn("Не понял", self.sched.cancel_kind("alarm"))
        self.assertIn("Не понял", self.sched.cancel_kind(""))
        self.assertEqual(self.sched.count, 1)

    def test_cancel_latest_removes_only_most_recent(self):
        self.sched.add_reminder(100, "старое")
        self.sched.add_timer(100, "таймер")
        self.sched.add_reminder(100, "новое")

        message = self.sched.cancel_latest()
        self.assertEqual(self.sched.count, 2)
        texts = {item["text"] for item in self.sched._items.values()}
        self.assertEqual(texts, {"старое", "таймер"})
        self.assertIn("новое", message)

    def test_cancel_latest_with_kind_preserves_other_kind(self):
        self.sched.add_reminder(100, "r1")
        self.sched.add_timer(100, "t1")
        self.sched.add_timer(100, "t2")

        self.sched.cancel_latest("timer")
        texts = {item["text"] for item in self.sched._items.values()}
        self.assertEqual(texts, {"r1", "t1"})  # t2 (последний таймер) удалён
        self.assertEqual(self.sched.count, 2)

    def test_cancel_latest_empty_is_safe(self):
        self.assertIn("Нечего", self.sched.cancel_latest())
        self.assertIn("Нечего", self.sched.cancel_latest("timer"))

    def test_cancel_matching_empty_does_not_cancel_everything(self):
        """Регресс: пустой запрос раньше стирал все задачи."""
        self.sched.add_reminder(100, "alpha")
        self.sched.add_reminder(100, "beta")
        message = self.sched.cancel_matching("")
        self.assertEqual(self.sched.count, 2)
        self.assertIn("Уточните", message)
        self.assertIn(" ", message)  # это сообщение-подсказка, а не «Отменил 2»

    def test_cancel_matching_named_reminder_keeps_others(self):
        self.sched.add_reminder(100, "купить хлеб")
        self.sched.add_reminder(100, "позвонить врачу")
        self.sched.cancel_matching("хлеб")
        self.assertEqual(self.sched.count, 1)
        self.assertIn("позвонить врачу",
                      next(iter(self.sched._items.values()))["text"])

    # ------------------------------------------------------------------
    #  Сбой записи на диск
    # ------------------------------------------------------------------
    def test_add_reminder_save_failure_rolls_back(self):
        with mock.patch.object(scheduler_mod, "write_json", return_value=False):
            message = self.sched.add_reminder(100, "не сохранится")
        self.assertIn("не удалось", message.lower())
        self.assertEqual(self.sched.count, 0)            # элемент откатан
        self.assertEqual(self.sched._timers, {})         # таймер снят
        self.assertEqual(FakeTimer.live(), [])           # живых таймеров нет

    def test_add_timer_save_failure_rolls_back(self):
        with mock.patch.object(scheduler_mod, "write_json", return_value=False):
            message = self.sched.add_timer(100, "не сохранится")
        self.assertIn("не удалось", message.lower())
        self.assertEqual(self.sched.count, 0)
        self.assertEqual(FakeTimer.live(), [])

    def test_cancel_save_failure_reports_warning(self):
        self.sched.add_reminder(100, "живёт до сбоя")
        self.assertEqual(self.sched.count, 1)
        with mock.patch.object(scheduler_mod, "write_json", return_value=False):
            message = self.sched.cancel_all()
        self.assertEqual(self.sched.count, 0)
        self.assertIn("не удалось", message.lower())     # не выдаём «сохранено»

    # ------------------------------------------------------------------
    #  Остановка и гонки
    # ------------------------------------------------------------------
    def test_stop_all_blocks_stale_callback_and_new_scheduling(self):
        self.sched.add_timer(100, "старый")
        stale = FakeTimer.instances[-1]

        self.sched.stop_all()
        self.assertTrue(self.sched._closed)
        self.assertTrue(stale.cancelled)

        # Даже если callback всё-таки выполнился — уведомления быть не должно.
        stale.force_fire()
        self.assertEqual(self.spoken, [])
        self.assertEqual(self.notified, [])

        # Новые задачи после остановки не планируются.
        live_before = len(FakeTimer.instances)
        message = self.sched.add_reminder(100, "позднее")
        self.assertEqual(len(FakeTimer.instances), live_before)
        self.assertIn("выключается", message)

    def test_cancelled_reminder_does_not_fire(self):
        self.sched.add_reminder(100, "удалить меня")
        stale = FakeTimer.instances[-1]
        self.sched.cancel_matching("удалить")
        self.assertEqual(self.sched.count, 0)

        stale.force_fire()  # гонка: callback после отмены
        self.assertEqual(self.spoken, [])
        self.assertEqual(self.notified, [])

    def test_stop_all_prevents_scheduling_but_keeps_data(self):
        self.sched.add_reminder(100, "важное")
        self.sched.stop_all()
        # Данные не теряются (нужны для следующего запуска), но таймеров нет.
        self.assertEqual(self.sched.count, 1)
        self.assertEqual(self.sched._timers, {})

    # ------------------------------------------------------------------
    #  Конкурентность
    # ------------------------------------------------------------------
    def test_concurrent_additions_are_consistent(self):
        threads_count = 10
        per_thread = 20
        errors: list = []

        def worker(index):
            try:
                for j in range(per_thread):
                    self.sched.add_reminder(1000, f"r{index}-{j}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(threads_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        expected = threads_count * per_thread
        self.assertEqual(self.sched.count, expected)
        # Идентификаторы уникальны — счётчик не «потерялся» под блокировкой.
        self.assertEqual(len(set(self.sched._items.keys())), expected)
        # На диск легло ровно столько же записей.
        self.assertEqual(len(self._read_file()), expected)

    def test_concurrent_cancel_and_add_never_crashes(self):
        stop = threading.Event()
        errors: list = []

        def adder():
            try:
                i = 0
                while not stop.is_set():
                    self.sched.add_reminder(1000, f"a{i}")
                    i += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def canceller():
            try:
                while not stop.is_set():
                    self.sched.cancel_latest()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=adder) for _ in range(3)]
        threads += [threading.Thread(target=canceller) for _ in range(2)]
        for t in threads:
            t.start()
        import time
        time.sleep(0.3)
        stop.set()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertGreaterEqual(self.sched.count, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
