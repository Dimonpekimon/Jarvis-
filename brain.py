# -*- coding: utf-8 -*-
"""«Мозг» Джарвиса — бесплатный Gemini API.

По умолчанию используется модель ``gemini-3.5-flash``: она быстро отвечает
по-русски, понимает картинки и доступна на бесплатном тарифе. Модель задаётся
настройкой ``GEMINI_MODEL``, список запасных — ``GEMINI_FALLBACK_MODELS``.

Ключ берётся из ``jarvis_settings.json`` (ключ ``GEMINI_API_KEY``) или из
переменной окружения ``GEMINI_API_KEY``. В репозиторий ключ не попадает.
"""

from __future__ import annotations

import base64
import io
import mimetypes
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

# Картинку больше этого размера отправлять накладно: запрос получается
# тяжёлым, а бесплатный тариф считает входные токены. Уменьшаем перед отправкой.
_MAX_INLINE_IMAGE_BYTES = 3_500_000
_MAX_IMAGE_SIDE = 1600


def plain_text(text: str) -> str:
    """Убирает markdown, чтобы синтезатор не читал звёздочки и решётки."""
    result = text or ""
    for pattern, repl in _MARKDOWN_PATTERNS:
        result = pattern.sub(repl, result)
    result = result.replace("—", " — ")
    return re.sub(r"\s{2,}", " ", result).strip()


class BrainUnavailable(RuntimeError):
    """Gemini недоступна: нет ключа, нет сети или исчерпан лимит."""


class _TryNextModel(RuntimeError):
    """Внутренний сигнал: эта модель не подошла, есть смысл взять запасную."""


class Brain:
    def __init__(self, model: str | None = None):
        self.model = model or config.GEMINI_MODEL
        self.api_base = str(config.GEMINI_API_BASE).rstrip("/")
        self.history: deque = deque(maxlen=config.LLM_HISTORY_SIZE)
        self._session = requests.Session()
        # Gemini — внешний сервис, поэтому системный прокси здесь как раз нужен
        # (в отличие от прежней локальной Ollama, где он всё ломал).
        self._session.trust_env = True
        self._available: bool | None = None
        self._models: list = []
        self._probe_after = 0.0
        self._last_error = ""
        # Некоторые модели (например, flash-lite) не принимают thinkingConfig.
        # После первого отказа запоминаем это и больше не мешаем запрос.
        self._thinking_supported = True

    # ------------------------------------------------------------------
    #  Ключ и доступность
    # ------------------------------------------------------------------
    @property
    def api_key(self) -> str:
        return str(getattr(config, "GEMINI_API_KEY", "") or "").strip()

    def key_present(self) -> bool:
        return bool(self.api_key)

    def _require_key(self) -> str:
        key = self.api_key
        if not key:
            raise BrainUnavailable(
                "Ключ Gemini не задан. Впишите GEMINI_API_KEY в jarvis_settings.json "
                "или задайте переменную окружения GEMINI_API_KEY."
            )
        return key

    def available_models(self, force: bool = False) -> list:
        """Список моделей, доступных этому ключу. Кэшируется на GEMINI_RECHECK_SEC."""
        if not self.key_present():
            self._available = False
            self._models = []
            self._last_error = (
                "Ключ Gemini не задан. Впишите GEMINI_API_KEY в jarvis_settings.json."
            )
            return []
        if (not force and self._available is not None
                and time.monotonic() < self._probe_after):
            return self._models
        try:
            response = self._session.get(
                f"{self.api_base}/models",
                params={"key": self.api_key},
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001
            self._available = False
            self._models = []
            self._last_error = (
                f"Нет связи с Gemini ({exc.__class__.__name__}). "
                "Проверьте интернет и прокси."
            )
            self._schedule_recheck()
            return []

        if response.status_code in (400, 401, 403):
            self._available = False
            self._models = []
            if _is_region_block(response.text or ""):
                self._last_error = REGION_BLOCK_HINT
            else:
                self._last_error = (
                    "Ключ Gemini неверный или отозван. Получите новый в Google AI Studio "
                    "и обновите GEMINI_API_KEY."
                )
            self._schedule_recheck()
            return []
        if response.status_code >= 400:
            self._available = False
            self._models = []
            self._last_error = f"Gemini вернула ошибку {response.status_code}."
            self._schedule_recheck()
            return []

        try:
            payload = response.json()
        except ValueError:
            self._available = False
            self._models = []
            self._last_error = "Gemini вернула некорректный ответ."
            self._schedule_recheck()
            return []

        self._models = [
            str(item.get("name", "")).removeprefix("models/")
            for item in payload.get("models", [])
            if "generateContent" in (item.get("supportedGenerationMethods") or [])
        ]
        self._available = True
        self._last_error = ""
        # Успешный ответ тоже кэшируем: иначе каждое «статус» заново ходило бы
        # в сеть за списком моделей (проверка занимала ~0,1–0,2 с на вызов).
        self._schedule_recheck()
        return self._models

    def _schedule_recheck(self) -> None:
        recheck = float(getattr(config, "GEMINI_RECHECK_SEC", 30) or 30)
        self._probe_after = time.monotonic() + recheck

    def is_available(self) -> bool:
        if self._available is None:
            self.available_models()
        return bool(self._available)

    def model_present(self) -> bool:
        if not self._models:
            self.available_models()
        return self.model in self._models

    def check(self, force: bool = False) -> tuple:
        """Возвращает (ок: bool, сообщение: str) — для стартовой диагностики."""
        if not self.key_present():
            return False, (
                "Ключ Gemini не задан. Впишите GEMINI_API_KEY в jarvis_settings.json "
                "или задайте переменную окружения GEMINI_API_KEY."
            )
        models = self.available_models(force=force)
        if not models:
            return False, self._last_error or "Gemini не отвечает."
        if self.model not in models:
            preview = ", ".join(models[:5]) or "нет"
            return False, (
                f"Модель «{self.model}» недоступна для этого ключа. "
                f"Доступны, например: {preview}."
            )
        return True, f"Gemini готова, модель «{self.model}»."

    def warmup(self) -> bool:
        """Проверяет ключ и модель. Генерацию не запускает — не тратит квоту."""
        ok, message = self.check()
        if ok:
            log(f"Мозг готов: {message}", "debug")
        else:
            log(message, "warn")
        return ok

    # ------------------------------------------------------------------
    #  Диалог
    # ------------------------------------------------------------------
    def reset(self) -> None:
        self.history.clear()

    def _contents(self, prompt: str, image_b64: str | None = None,
                  image_mime: str = "image/png") -> list:
        contents = []
        for question, answer in self.history:
            contents.append({"role": "user", "parts": [{"text": question}]})
            contents.append({"role": "model", "parts": [{"text": answer}]})

        parts: list = []
        if prompt:
            parts.append({"text": prompt})
        if image_b64:
            parts.append({"inline_data": {"mime_type": image_mime, "data": image_b64}})
        contents.append({"role": "user", "parts": parts})
        return contents

    def _payload(self, prompt: str, *, image_b64: str | None,
                 image_mime: str, temperature: float | None) -> dict:
        payload: dict = {"contents": self._contents(prompt, image_b64, image_mime)}

        system_prompt = str(getattr(config, "LLM_SYSTEM_PROMPT", "") or "").strip()
        if system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

        generation: dict = {
            "temperature": (config.GEMINI_TEMPERATURE if temperature is None
                            else float(temperature)),
            "maxOutputTokens": int(config.GEMINI_MAX_TOKENS),
        }
        budget = getattr(config, "GEMINI_THINKING_BUDGET", None)
        if budget is not None and self._thinking_supported:
            generation["thinkingConfig"] = {"thinkingBudget": int(budget)}
        payload["generationConfig"] = generation
        return payload

    def _post(self, model: str, payload: dict) -> dict:
        """Один запрос к Gemini с понятными ошибками.

        Raises:
            _TryNextModel: модель не найдена или не ответила вовремя.
            BrainUnavailable: проблема с ключом, лимитом или сетью.
        """
        key = self._require_key()
        url = f"{self.api_base}/models/{model}:generateContent"
        try:
            response = self._session.post(
                url, params={"key": key}, json=payload,
                timeout=float(config.GEMINI_TIMEOUT),
            )
        except requests.exceptions.Timeout as exc:
            raise _TryNextModel(
                f"Модель «{model}» не ответила за {config.GEMINI_TIMEOUT} с."
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise BrainUnavailable(
                f"Нет связи с Gemini ({exc.__class__.__name__}). Проверьте интернет."
            ) from exc

        if response.status_code == 429:
            # Бесплатный тариф считает запросы в минуту: короткая пауза помогает.
            time.sleep(1.5)
            try:
                response = self._session.post(
                    url, params={"key": key}, json=payload,
                    timeout=float(config.GEMINI_TIMEOUT),
                )
            except requests.exceptions.RequestException as exc:
                raise BrainUnavailable(
                    f"Нет связи с Gemini ({exc.__class__.__name__})."
                ) from exc
            if response.status_code == 429:
                raise BrainUnavailable(
                    "Бесплатный лимит Gemini исчерпан. Подождите минуту и повторите."
                )

        if response.status_code == 404:
            raise _TryNextModel(f"Модель «{model}» недоступна для этого ключа.")
        if response.status_code in (400, 401, 403):
            detail = _error_text(response)
            # Часть моделей (flash-lite) не принимает thinkingConfig и отвечает
            # просто «invalid argument». Повторяем без параметра — это безопасно:
            # если дело было в другом, второй запрос вернёт ту же ошибку.
            if (response.status_code == 400
                    and "thinkingConfig" in payload.get("generationConfig", {})):
                self._thinking_supported = False
                log(f"«{model}» не приняла thinkingConfig — повторяю без него.",
                    "debug")
                retry_payload = dict(payload)
                generation = dict(retry_payload["generationConfig"])
                generation.pop("thinkingConfig", None)
                retry_payload["generationConfig"] = generation
                return self._post(model, retry_payload)
            if _is_region_block(detail):
                raise BrainUnavailable(REGION_BLOCK_HINT)
            if response.status_code in (401, 403) or "api key" in detail.lower():
                raise BrainUnavailable(
                    "Ключ Gemini неверный или отозван. Обновите GEMINI_API_KEY."
                )
            raise BrainUnavailable(f"Gemini отклонила запрос: {detail}")
        if response.status_code >= 500:
            raise _TryNextModel(f"Сервис Gemini временно недоступен ({response.status_code}).")

        try:
            return response.json()
        except ValueError as exc:
            raise BrainUnavailable("Gemini вернула некорректный ответ.") from exc

    def ask(self, prompt: str, *, remember: bool = True,
            image_b64: str | None = None, image_mime: str = "image/png",
            model: str | None = None, temperature: float | None = None) -> str:
        """Отправляет запрос в Gemini и возвращает ответ.

        Raises:
            BrainUnavailable: нет ключа, сети, квоты или модель недоступна.
        """
        self._require_key()
        if not prompt and not image_b64:
            raise BrainUnavailable("Пустой запрос: нечего отправлять в Gemini.")

        target = model or self.model
        candidates = [target]
        fallbacks = config.GEMINI_FALLBACK_MODELS or []
        if isinstance(fallbacks, str):
            # В настройках легко случайно указать строку вместо списка —
            # иначе цикл пошёл бы по буквам и пробовал модели «g», «e», «m»...
            fallbacks = [fallbacks]
        for fallback in fallbacks:
            if fallback and fallback not in candidates:
                candidates.append(fallback)

        last_error = ""
        data = None
        used_model = target
        for candidate in candidates:
            payload = self._payload(prompt, image_b64=image_b64,
                                    image_mime=image_mime, temperature=temperature)
            try:
                data = self._post(candidate, payload)
                used_model = candidate
                break
            except _TryNextModel as exc:
                last_error = str(exc)
                log(f"{exc} Пробую запасную модель.", "debug")

        if data is None:
            raise BrainUnavailable(
                last_error or "Ни одна модель Gemini не ответила. Попробуйте позже."
            )

        answer = plain_text(_extract_text(data))
        if not answer:
            reason = _block_reason(data)
            if reason:
                answer = f"Сэр, Gemini отклонила запрос: {reason}."
            else:
                answer = "Сэр, модель вернула пустой ответ."
            log(f"Пустой ответ Gemini ({used_model}).", "warn")
            return answer

        if remember:
            self.history.append((prompt, answer))
        return answer

    # ------------------------------------------------------------------
    #  Видение
    # ------------------------------------------------------------------
    @property
    def vision_enabled(self) -> bool:
        return bool(config.GEMINI_VISION_MODEL)

    def vision_available(self) -> bool:
        return self.vision_enabled and self.key_present()

    def ask_with_image(self, prompt: str, image_path: Path) -> str:
        if not self.vision_enabled:
            raise BrainUnavailable(
                "Модель для анализа изображений не задана. "
                "Укажите GEMINI_VISION_MODEL, например «gemini-3.5-flash»."
            )
        image_b64, mime = _prepare_image(Path(image_path))
        return self.ask(prompt, image_b64=image_b64, image_mime=mime,
                        model=config.GEMINI_VISION_MODEL, remember=False)


# =====================================================================
#  Вспомогательные функции
# =====================================================================
def _error_text(response) -> str:
    """Достаёт человекочитаемое сообщение из ответа об ошибке."""
    try:
        data = response.json()
        message = str((data.get("error") or {}).get("message", ""))
    except ValueError:
        message = (response.text or "")[:200]
    return message or f"ошибка {response.status_code}"


REGION_BLOCK_HINT = (
    "Gemini недоступен из этой сети: Google блокирует регион "
    "(«User location is not supported»). Ключ при этом рабочий — включите "
    "VPN или прокси и повторите."
)


def _is_region_block(text: str) -> bool:
    """Google отказывает по географии, а не из-за ключа.

    Такой ответ приходит с кодом 400 и легко маскируется под «неверный ключ».
    """
    lowered = (text or "").lower()
    return "location is not supported" in lowered or "failed_precondition" in lowered


def _extract_text(data: dict) -> str:
    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    parts = ((candidates[0].get("content") or {}).get("parts") or [])
    chunks = []
    for part in parts:
        if part.get("thought"):
            continue
        text = part.get("text")
        if text:
            chunks.append(str(text))
    return "".join(chunks).strip()


def _block_reason(data: dict) -> str:
    feedback = data.get("promptFeedback") or {}
    reason = str(feedback.get("blockReason") or "")
    if reason:
        return reason
    candidates = data.get("candidates") or []
    if candidates:
        finish = str(candidates[0].get("finishReason") or "")
        if finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"):
            return finish
    return ""


def _prepare_image(image_path: Path) -> tuple[str, str]:
    """Готовит картинку к отправке: при необходимости сжимает и уменьшает."""
    data = Path(image_path).read_bytes()
    mime = mimetypes.guess_type(str(image_path))[0] or "image/png"

    if len(data) > _MAX_INLINE_IMAGE_BYTES:
        try:
            from PIL import Image

            with Image.open(image_path) as image:
                image = image.convert("RGB")
                image.thumbnail((_MAX_IMAGE_SIDE, _MAX_IMAGE_SIDE))
                buffer = io.BytesIO()
                image.save(buffer, "JPEG", quality=85, optimize=True)
            data = buffer.getvalue()
            mime = "image/jpeg"
            log(f"Скриншот сжат для отправки: {len(data) // 1024} КБ.", "debug")
        except Exception as exc:  # noqa: BLE001
            log(f"Не удалось сжать картинку: {exc}", "debug")

    return base64.b64encode(data).decode("ascii"), mime
