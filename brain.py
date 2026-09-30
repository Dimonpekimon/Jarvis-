# -*- coding: utf-8 -*-
"""«Мозг» Джарвиса — локальная LLM через Ollama.

По умолчанию используется модель ``mistral`` (7B), но в настройках можно указать
любую другую: ``llama3.2``, ``qwen2.5``, ``gemma2`` и т.д. Для анализа
скриншотов можно дополнительно указать ``OLLAMA_VISION_MODEL``.
"""

from __future__ import annotations

import base64
import re
import time
from collections import deque
from pathlib import Path

import requests

import config
from utils import log

_MARKDOWN_PATTERNS = [
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),
    (re.compile(r"\*(.+?)\*"), r"\1"),
    (re.compile(r"__(.+?)__"), r"\1"),
    (re.compile(r"`{1,3}([^`]+)`{1,3}"), r"\1"),
    (re.compile(r"^#{1,6}\s*", re.MULTILINE), ""),
    (re.compile(r"^\s*[-*+]\s+", re.MULTILINE), ""),
    (re.compile(r"^\s*\d+\.\s+", re.MULTILINE), ""),
    (re.compile(r"\[(.+?)\]\(.+?\)"), r"\1"),
]


def plain_text(text: str) -> str:
    """Убирает markdown, чтобы синтезатор не читал звёздочки и решётки."""
    result = text or ""
    for pattern, repl in _MARKDOWN_PATTERNS:
        result = pattern.sub(repl, result)
    result = result.replace("—", " — ")
    return re.sub(r"\s{2,}", " ", result).strip()


class BrainUnavailable(RuntimeError):
    """Ollama недоступна (не запущена или не установлена)."""


class Brain:
    def __init__(self, model: str | None = None):
        self.model = model or config.OLLAMA_MODEL
        self.host = config.OLLAMA_HOST.rstrip("/")
        self.history: deque = deque(maxlen=config.LLM_HISTORY_SIZE)
        self._session = requests.Session()
        # Ollama всегда локальная: игнорируем системный HTTP_PROXY, иначе запрос
        # к 127.0.0.1 уходит в прокси и падает с 502.
        self._session.trust_env = False
        self._available: bool | None = None
        self._models: list = []
        # Когда Ollama недоступна, не тратим время на заведомо провальное
        # подключение: на части систем закрытый порт отвечает только через ~2 с.
        self._probe_after = 0.0
        self._last_error = ""

    # ------------------------------------------------------------------
    #  Доступность
    # ------------------------------------------------------------------
    def available_models(self, force: bool = False) -> list:
        """Список моделей Ollama. Результат кэшируется на OLLAMA_RECHECK_SEC.

        Без кэша каждая нераспознанная фраза заново ждала бы недоступную Ollama
        (около 2 с), а потом всё равно уходила в веб-поиск. ``force=True`` —
        для настоящей диагностики (ключ --check).
        """
        if (not force and self._available is not None
                and time.monotonic() < self._probe_after):
            return self._models
        try:
            response = self._session.get(f"{self.host}/api/tags", timeout=5)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            self._available = False
            self._models = []
            self._last_error = (
                f"Ollama не отвечает ({exc.__class__.__name__}). Установите её с "
                "https://ollama.com/download и запустите."
            )
            recheck = float(getattr(config, "OLLAMA_RECHECK_SEC", 30) or 30)
            self._probe_after = time.monotonic() + recheck
            return []
        self._available = True
        self._last_error = ""
        self._models = [m.get("name", "") for m in payload.get("models", [])]
        return self._models

    def is_available(self) -> bool:
        if self._available is None:
            self.available_models()
        return bool(self._available)

    def model_present(self) -> bool:
        if not self._models:
            self.available_models()
        base = self.model.split(":")[0]
        return any(name.split(":")[0] == base for name in self._models)

    def check(self, force: bool = False) -> tuple:
        """Возвращает (ок: bool, сообщение: str) — для стартовой диагностики.

        По умолчанию пользуется кэшем: команда «статус» не должна каждый раз
        ждать недоступную Ollama. Для настоящей проверки — ``force=True``.
        """
        models = self.available_models(force=force)
        if not models:
            return False, (
                "Ollama не отвечает. Установите её с https://ollama.com/download "
                f"и запустите, затем выполните: ollama pull {self.model}"
            )
        if not self.model_present():
            return False, (
                f"Модель «{self.model}» не найдена. Скачайте её командой: "
                f"ollama pull {self.model}"
            )
        return True, f"Ollama готова, модель «{self.model}» на месте."

    def warmup(self) -> bool:
        """Загружает модель в память, чтобы первый ответ был быстрым."""
        if not self.is_available() or not self.model_present():
            return False
        try:
            self._session.post(
                f"{self.host}/api/generate",
                json={"model": self.model, "prompt": "привет", "stream": False,
                      "options": {"num_predict": 1}, "keep_alive": config.OLLAMA_KEEP_ALIVE},
                timeout=config.OLLAMA_TIMEOUT,
            )
            log(f"Модель «{self.model}» загружена в память.", "debug")
            return True
        except Exception as exc:  # noqa: BLE001
            log(f"Прогрев модели не удался: {exc}", "debug")
            return False

    # ------------------------------------------------------------------
    #  Диалог
    # ------------------------------------------------------------------
    def reset(self) -> None:
        self.history.clear()

    def _messages(self, prompt: str, image_b64: str | None = None) -> list:
        messages = [{"role": "system", "content": config.LLM_SYSTEM_PROMPT}]
        for question, answer in self.history:
            messages.append({"role": "user", "content": question})
            messages.append({"role": "assistant", "content": answer})
        user_message = {"role": "user", "content": prompt}
        if image_b64:
            user_message["images"] = [image_b64]
        messages.append(user_message)
        return messages

    def ask(self, prompt: str, *, remember: bool = True,
            image_b64: str | None = None, model: str | None = None,
            temperature: float | None = None) -> str:
        """Отправляет запрос в Ollama и возвращает ответ.

        Raises:
            BrainUnavailable: если Ollama не запущена/не установлена.
        """
        # Заведомо недоступную Ollama не дёргаем: подключение к закрытому порту
        # может занимать секунды, а ответ всё равно придёт из веб-поиска.
        if self._available is False and time.monotonic() < self._probe_after:
            raise BrainUnavailable(
                self._last_error or "Ollama недоступна. Проверьте, запущена ли она.")

        target_model = model or self.model
        payload = {
            "model": target_model,
            "messages": self._messages(prompt, image_b64),
            "stream": False,
            "keep_alive": config.OLLAMA_KEEP_ALIVE,
            "options": {
                "temperature": config.OLLAMA_TEMPERATURE if temperature is None else temperature,
                "num_predict": config.OLLAMA_MAX_TOKENS,
            },
        }
        try:
            response = self._session.post(
                f"{self.host}/api/chat", json=payload, timeout=config.OLLAMA_TIMEOUT
            )
        except requests.exceptions.ConnectionError as exc:
            self._available = False
            raise BrainUnavailable(
                "Ollama не запущена. Откройте терминал и выполните: ollama serve"
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise BrainUnavailable(
                f"Ollama не ответила за {config.OLLAMA_TIMEOUT} с. "
                "Попробуйте модель поменьше или увеличьте OLLAMA_TIMEOUT."
            ) from exc

        if response.status_code == 404:
            raise BrainUnavailable(
                f"Модель «{target_model}» не установлена. Выполните: ollama pull {target_model}"
            )
        if response.status_code >= 400:
            raise BrainUnavailable(f"Ollama вернула ошибку {response.status_code}: {response.text[:200]}")

        try:
            data = response.json()
        except ValueError as exc:
            raise BrainUnavailable("Ollama вернула некорректный ответ.") from exc

        answer = plain_text((data.get("message") or {}).get("content", ""))
        if not answer:
            answer = "Сэр, модель вернула пустой ответ."
        if remember:
            self.history.append((prompt, answer))
        return answer

    # ------------------------------------------------------------------
    #  Видение
    # ------------------------------------------------------------------
    @property
    def vision_enabled(self) -> bool:
        return bool(config.OLLAMA_VISION_MODEL)

    def vision_available(self) -> bool:
        if not self.vision_enabled:
            return False
        models = self.available_models()
        base = config.OLLAMA_VISION_MODEL.split(":")[0]
        return any(name.split(":")[0] == base for name in models)

    def ask_with_image(self, prompt: str, image_path: Path) -> str:
        if not self.vision_enabled:
            raise BrainUnavailable(
                "Модель для анализа изображений не задана. "
                "Установите OLLAMA_VISION_MODEL, например «llava» "
                "(ollama pull llava)."
            )
        image_b64 = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
        return self.ask(prompt, image_b64=image_b64,
                        model=config.OLLAMA_VISION_MODEL, remember=False)
