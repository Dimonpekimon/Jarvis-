# -*- coding: utf-8 -*-
"""Шина событий «ядро → интерфейс».

Модуль намеренно не зависит ни от чего, кроме стандартной библиотеки: его
импортируют и ядро, и озвучка, и веб-интерфейс. Если подписчиков нет,
публикация ничего не стоит — это важно, потому что громкость речи передаётся
по 30 раз в секунду.

События, которые понимает интерфейс:

* ``{"type": "message", "role": "user|assistant|system", "text": "..."}``
* ``{"type": "state",   "value": "idle|listening|thinking|speaking|loading"}``
* ``{"type": "level",   "value": 0..1, "source": "tts|mic"}``
"""

from __future__ import annotations

import queue
import threading
from datetime import datetime

# Медленный клиент не должен копить события бесконечно.
MAX_QUEUE = 512

STATE_IDLE = "idle"
STATE_LISTENING = "listening"
STATE_THINKING = "thinking"
STATE_SPEAKING = "speaking"
STATE_LOADING = "loading"

_last_state = {"value": STATE_IDLE}


class EventBus:
    """Потокобезопасная рассылка событий подписчикам."""

    def __init__(self) -> None:
        self._subscribers: list = []
        self._lock = threading.Lock()

    def subscribe(self) -> "queue.Queue":
        channel: "queue.Queue" = queue.Queue(maxsize=MAX_QUEUE)
        with self._lock:
            self._subscribers.append(channel)
        return channel

    def unsubscribe(self, channel) -> None:
        with self._lock:
            if channel in self._subscribers:
                self._subscribers.remove(channel)

    def publish(self, event: dict) -> None:
        with self._lock:
            targets = list(self._subscribers)
        for channel in targets:
            try:
                channel.put_nowait(event)
            except queue.Full:
                pass

    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._subscribers)


bus = EventBus()


def current_state() -> str:
    """Последнее объявленное состояние — для тех, кто подключился позже."""
    return _last_state["value"]


def emit(event_type: str, **payload) -> None:
    """Публикует событие. Если никто не слушает — сразу выходит."""
    if not bus.subscribers:
        return
    event = {"type": event_type, "ts": datetime.now().isoformat(timespec="seconds")}
    event.update(payload)
    bus.publish(event)


def emit_message(role: str, text: str) -> None:
    """Строка диалога: реплика пользователя, ассистента или служебная."""
    text = " ".join(str(text or "").split())
    if text:
        emit("message", role=role, text=text)


def emit_state(value: str) -> None:
    """Состояние ассистента. Запоминается даже без подписчиков."""
    _last_state["value"] = value
    emit("state", value=value)


def emit_level(value: float, source: str = "tts") -> None:
    """Текущая громкость звука (0..1) — по ней пульсирует ядро интерфейса."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return
    emit("level", value=round(max(0.0, min(1.0, numeric)), 4), source=source)
