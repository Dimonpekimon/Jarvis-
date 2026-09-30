# -*- coding: utf-8 -*-
"""Таймеры и напоминания Джарвиса с сохранением между запусками.

Раньше напоминания жили только в памяти и терялись при перезапуске.
Теперь они пишутся в ``data/reminders.json`` и восстанавливаются при старте.

Все изменения состояния (создание, отмена, сохранение и планирование
таймеров) выполняются под одним ``RLock``: без этого отмена могла гоняться
с созданием, а ``_save`` читал общий словарь, пока другой поток его менял.
"""

from __future__ import annotations

import math
import threading
import time
from datetime import datetime

import config
from utils import human_duration, log, read_json, write_json


class Scheduler:
    #: Допустимые виды задач.
    _VALID_KINDS = ("timer", "reminder")
    #: Нижняя граница задержки планировщика (секунды).
    _MIN_DELAY = 0.1
    #: Верхняя граница задержки — защита от абсурдных/переполняющих значений (~10 лет).
    _MAX_DELAY = 10 * 365 * 24 * 60 * 60

    def __init__(self, speak_fn=None, notify_fn=None):
        self._speak = speak_fn
        self._notify = notify_fn
        self._items: dict = {}
        self._timers: dict = {}
        # RLock, а не Lock: _save_locked вызывается из методов, уже держащих
        # блокировку, и не должен упираться в самого себя.
        self._lock = threading.RLock()
        self._counter = 0
        self._token_seq = 0
        # После stop_all новые задачи не планируются, а «застрявшие» таймеры
        # не проговаривают уведомления.
        self._closed = False

    # ------------------------------------------------------------------
    #  Внутреннее
    #  Методы с суффиксом _locked вызываются только с удержанием self._lock.
    # ------------------------------------------------------------------
    def _new_id(self, kind: str) -> str:
        self._counter += 1
        return f"{kind}-{int(time.time())}-{self._counter}"

    @staticmethod
    def _coerce_seconds(seconds) -> float | None:
        """Преобразует пользовательскую задержку в конечное положительное число."""
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            return None
        value = float(seconds)
        if not math.isfinite(value) or value <= 0:
            return None
        return value

    @classmethod
    def _clamp_delay(cls, delay) -> float:
        """Ограничивает задержку конечным положительным значением в допустимых рамках."""
        try:
            value = float(delay)
        except (TypeError, ValueError):
            return cls._MIN_DELAY
        if not math.isfinite(value) or value < cls._MIN_DELAY:
            return cls._MIN_DELAY
        if value > cls._MAX_DELAY:
            return cls._MAX_DELAY
        return value

    @staticmethod
    def _finite(value, default=None):
        """Возвращает конечное число или ``default`` для мусора/NaN/inf."""
        try:
            result = float(value)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(result):
            return default
        return result

    def _save_locked(self) -> bool:
        """Пишет состояние на диск. Возвращает True только при реальной записи."""
        data = [
            {
                "id": item_id,
                "kind": item["kind"],
                "text": item["text"],
                "label": item.get("label"),
                "due_at": item["due_at"],
                "created_at": item["created_at"],
            }
            for item_id, item in self._items.items()
        ]
        return bool(write_json(config.REMINDERS_FILE, data))

    def _save(self) -> bool:
        """Потокобезопасная обёртка над :meth:`_save_locked`."""
        with self._lock:
            return self._save_locked()

    def _schedule_locked(self, item_id: str, delay) -> bool:
        """Планирует срабатывание. Возвращает False, если планировщик закрыт."""
        if self._closed:
            return False
        delay = self._clamp_delay(delay)
        self._token_seq += 1
        token = self._token_seq
        timer = threading.Timer(delay, self._fire, args=(item_id, token))
        timer.daemon = True
        self._timers[item_id] = timer
        item = self._items.get(item_id)
        if item is not None:
            item["_token"] = token
        timer.start()
        return True

    def _cancel_locked(self, item_id: str) -> None:
        timer = self._timers.pop(item_id, None)
        if timer is not None:
            timer.cancel()
        self._items.pop(item_id, None)

    def _fire(self, item_id: str, token: int) -> None:
        with self._lock:
            # Закрытый планировщик и устаревшие таймеры (отменённые или
            # перепланированные) не должны ничего проговаривать.
            if self._closed:
                return
            item = self._items.get(item_id)
            if item is None or item.get("_token") != token:
                return
            self._items.pop(item_id, None)
            self._timers.pop(item_id, None)
            if not self._save_locked():
                log("Не удалось сохранить состояние после срабатывания задачи.", "warn")

        if item["kind"] == "timer":
            phrase = item.get("label") or "Сэр, время вышло."
        else:
            phrase = f"Сэр, напоминаю: {item['text']}"
        log(phrase, "info")
        if callable(self._notify):
            try:
                self._notify("Джарвис", phrase)
            except Exception:  # noqa: BLE001
                pass
        if callable(self._speak):
            try:
                self._speak(phrase)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    #  Публичный API
    # ------------------------------------------------------------------
    @property
    def count(self) -> int:
        """Полное число активных задач (таймеры + напоминания)."""
        with self._lock:
            return len(self._items)

    def add_timer(self, seconds: int, label: str | None = None) -> str:
        delay = self._coerce_seconds(seconds)
        if delay is None:
            return "Не понял, на сколько поставить таймер, сэр."
        delay = min(delay, self._MAX_DELAY)
        label = (label or "").strip() or None
        with self._lock:
            if self._closed:
                return "Джарвис уже выключается — новый таймер не ставлю, сэр."
            item_id = self._new_id("timer")
            now = time.time()
            self._items[item_id] = {
                "kind": "timer",
                "text": label or "таймер",
                "label": label,
                "due_at": now + delay,
                "created_at": now,
            }
            self._schedule_locked(item_id, delay)
            if not self._save_locked():
                # Не выдаём «установлен», если на диск ничего не легло.
                self._cancel_locked(item_id)
                return "Не удалось сохранить таймер, сэр."
        log(f"Таймер на {human_duration(delay)} установлен.", "info")
        return f"Таймер на {human_duration(delay)} установлен."

    def add_reminder(self, seconds: int, text: str) -> str:
        delay = self._coerce_seconds(seconds)
        text = (text or "").strip() or "напоминание"
        if delay is None:
            return "Не понял, через какое время напомнить, сэр."
        delay = min(delay, self._MAX_DELAY)
        with self._lock:
            if self._closed:
                return "Джарвис уже выключается — напоминание не записываю, сэр."
            item_id = self._new_id("reminder")
            now = time.time()
            self._items[item_id] = {
                "kind": "reminder",
                "text": text,
                "label": None,
                "due_at": now + delay,
                "created_at": now,
            }
            self._schedule_locked(item_id, delay)
            if not self._save_locked():
                self._cancel_locked(item_id)
                return "Не удалось сохранить напоминание, сэр."
        log(f"Напоминание «{text}» через {human_duration(delay)}.", "info")
        return f"Записал. Напомню через {human_duration(delay)}: {text}."

    def list_items(self) -> str:
        with self._lock:
            items = list(self._items.values())
        if not items:
            return "Активных напоминаний и таймеров нет, сэр."
        items.sort(key=lambda item: item["due_at"])
        parts = []
        for item in items:
            remaining = max(0, item["due_at"] - time.time())
            prefix = "таймер" if item["kind"] == "timer" else "напоминание"
            parts.append(f"{prefix} «{item['text']}» через {human_duration(remaining)}")
        return "Активные задачи: " + "; ".join(parts) + "."

    def cancel_all(self) -> str:
        with self._lock:
            count = len(self._items)
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
            self._items.clear()
            saved = self._save_locked() if count else True
        if not count:
            return "Нечего отменять, сэр."
        message = f"Отменил {count} задач."
        if not saved:
            message += " Но сохранить изменения не удалось."
        return message

    def cancel_kind(self, kind: str) -> str:
        """Отменяет только таймеры либо только напоминания."""
        kind = (kind or "").strip().lower()
        if kind not in self._VALID_KINDS:
            return "Не понял, что отменить: таймеры или напоминания, сэр."
        with self._lock:
            matched = [item_id for item_id, item in self._items.items()
                       if item["kind"] == kind]
            for item_id in matched:
                self._cancel_locked(item_id)
            saved = self._save_locked() if matched else True
        word = "таймеров" if kind == "timer" else "напоминаний"
        if not matched:
            return f"Активных {word} нет, сэр."
        message = f"Отменил {len(matched)} {word}."
        if not saved:
            message += " Но сохранить изменения не удалось."
        return message

    def cancel_latest(self, kind: str | None = None) -> str:
        """Отменяет только самую недавно созданную задачу (при желании — заданного вида)."""
        if kind is not None:
            kind = str(kind).strip().lower()
            if kind not in self._VALID_KINDS:
                return "Не понял, что отменить: таймеры или напоминания, сэр."
        with self._lock:
            best_id = None
            best_key = None
            for index, (item_id, item) in enumerate(self._items.items()):
                if kind is not None and item["kind"] != kind:
                    continue
                # created_at — основной критерий, порядок вставки — тай-брейк.
                key = (self._finite(item.get("created_at"), 0.0), index)
                if best_key is None or key > best_key:
                    best_key = key
                    best_id = item_id
            if best_id is None:
                return "Нечего отменять, сэр."
            item = self._items[best_id]
            self._cancel_locked(best_id)
            saved = self._save_locked()
        if item["kind"] == "timer":
            message = f"Отменил последний таймер «{item['text']}»."
        else:
            message = f"Отменил последнее напоминание «{item['text']}»."
        if not saved:
            message += " Но сохранить изменения не удалось."
        return message

    def cancel_matching(self, text: str) -> str:
        """Отменяет задачи, в тексте которых есть указанное слово.

        Пустой запрос больше НЕ отменяет всё подряд — иначе случайная пустая
        строка из распознавания речи стирала бы все напоминания.
        """
        needle = (text or "").strip().lower()
        if not needle:
            return "Уточните, что именно отменить, сэр."
        with self._lock:
            matched = [item_id for item_id, item in self._items.items()
                       if needle in item["text"].lower()]
            for item_id in matched:
                self._cancel_locked(item_id)
            saved = self._save_locked() if matched else True
        if not matched:
            return "Не нашёл такое напоминание, сэр."
        message = f"Отменил {len(matched)} напоминаний."
        if not saved:
            message += " Но сохранить изменения не удалось."
        return message

    def stop_all(self) -> None:
        """Останавливает планировщик: таймеры гасятся, новые задачи не создаются."""
        with self._lock:
            self._closed = True
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()

    # ------------------------------------------------------------------
    #  Восстановление после перезапуска
    # ------------------------------------------------------------------
    def restore(self) -> int:
        data = read_json(config.REMINDERS_FILE, default=[])
        if not isinstance(data, list):
            return 0
        restored = 0
        with self._lock:
            now = time.time()
            for entry in data:
                # Каждая запись проверяется отдельно: битая не должна ронять
                # восстановление остальных.
                if not isinstance(entry, dict):
                    continue
                item_id = entry.get("id")
                if not isinstance(item_id, str) or not item_id.strip():
                    continue
                if item_id in self._items:
                    # Повторное восстановление (или дубликат id в файле) идемпотентно.
                    continue
                kind = entry.get("kind", "reminder")
                if kind not in self._VALID_KINDS:
                    continue
                due_at = self._finite(entry.get("due_at"))
                if due_at is None:
                    continue
                created_at = self._finite(entry.get("created_at"), now)
                raw_text = entry.get("text")
                text = raw_text.strip() if isinstance(raw_text, str) and raw_text.strip() \
                    else "напоминание"
                raw_label = entry.get("label")
                label = raw_label.strip() if isinstance(raw_label, str) and raw_label.strip() \
                    else None
                self._items[item_id] = {
                    "kind": kind,
                    "text": text,
                    "label": label,
                    "due_at": due_at,
                    "created_at": created_at,
                }
                delay = due_at - now
                if delay <= 0:
                    # Просроченное проговариваем сразу — прежняя семантика сохранена.
                    delay = 1.0
                self._schedule_locked(item_id, delay)
                restored += 1
        if restored:
            log(f"Восстановлено напоминаний: {restored}.", "info")
        return restored

    def upcoming(self, limit: int = 3) -> list:
        """Ближайшие задачи — для утреннего брифинга."""
        with self._lock:
            items = sorted(self._items.values(), key=lambda item: item["due_at"])
        result = []
        for item in items[:limit]:
            remaining = max(0, item["due_at"] - time.time())
            result.append(f"{item['text']} через {human_duration(remaining)}")
        return result

    @staticmethod
    def now_text() -> str:
        return datetime.now().strftime("%H:%M")
