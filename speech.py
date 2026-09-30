# -*- coding: utf-8 -*-
"""Распознавание речи Джарвиса.

Запись звука — через ``sounddevice`` с VAD-детектором (останавливаемся, когда
пользователь замолчал). Распознавание — локальный Whisper через
``faster-whisper`` (модель скачивается один раз в ``data/models``).
"""

from __future__ import annotations

import difflib
import re
import threading
import time

import config
from events import emit_level
from utils import contains_word, log, normalize_audio, normalize_text, rms, to_float32

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    import sounddevice as sd
    HAS_SD = True
except ImportError:
    HAS_SD = False

# Типичные «галлюцинации» Whisper на тишине и шуме — отбрасываем их.
_HALLUCINATIONS = (
    "продолжение следует",
    "субтитры",
    "редактор субтитров",
    "корректор",
    "спасибо за просмотр",
    "подпишитесь на канал",
    "ставьте лайк",
    "не забудьте подписаться",
    "продолжение в следующей серии",
    "транскрипция",
    "www",
)


class SpeechError(RuntimeError):
    """Проблема с записью или распознаванием."""


class Listener:
    """Микрофон + Whisper."""

    def __init__(self, model_size: str | None = None):
        self.model_size = model_size or config.WHISPER_MODEL
        self.sample_rate = config.SAMPLE_RATE
        self.noise_floor = 0.015
        self._model = None
        self._model_lock = threading.Lock()
        self._busy = threading.Lock()

    # ------------------------------------------------------------------
    #  Модель
    # ------------------------------------------------------------------
    @property
    def model_ready(self) -> bool:
        return self._model is not None

    def preload_model(self) -> bool:
        """Загружает (при необходимости скачивает) модель Whisper."""
        if not config.FEATURES["faster_whisper"]:
            log("faster-whisper не установлен: pip install faster-whisper", "warn")
            return False
        try:
            self._ensure_model()
            return True
        except Exception as exc:  # noqa: BLE001
            log(f"Не удалось загрузить модель Whisper: {exc}", "error")
            return False

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is not None:
                return self._model
            from faster_whisper import WhisperModel

            log(f"Загружаю Whisper «{self.model_size}» "
                f"({config.WHISPER_DEVICE}/{config.WHISPER_COMPUTE_TYPE})...", "info")
            started = time.time()
            self._model = WhisperModel(
                self.model_size,
                device=config.WHISPER_DEVICE,
                compute_type=config.WHISPER_COMPUTE_TYPE,
                download_root=str(config.MODELS_DIR),
            )
            log(f"Whisper готов за {time.time() - started:.1f} с.", "info")
        return self._model

    # ------------------------------------------------------------------
    #  Калибровка микрофона
    # ------------------------------------------------------------------
    def calibrate(self, duration: float | None = None) -> float:
        if not HAS_SD:
            return self.noise_floor
        duration = duration or config.MIC_CALIBRATION_SEC
        log(f"Калибровка микрофона: не говорите {duration:.0f} секунды...", "info")
        try:
            frames = int(duration * self.sample_rate)
            with sd.InputStream(samplerate=self.sample_rate, channels=config.CHANNELS,
                                dtype="int16") as stream:
                data, _ = stream.read(frames)
            chunk = data[:, 0] if data.ndim > 1 else data
            measured = rms(chunk)
            # Не даём калибровке уйти в крайности.
            self.noise_floor = min(max(measured, 0.003), 0.05)
            log(f"Фоновый шум: {self.noise_floor:.4f}", "info")
        except Exception as exc:  # noqa: BLE001
            log(f"Калибровка не удалась ({exc}), беру значение по умолчанию", "warn")
            self.noise_floor = config.VAD_RMS_THRESHOLD
        return self.noise_floor

    # ------------------------------------------------------------------
    #  Запись с VAD
    # ------------------------------------------------------------------
    def _speech_threshold(self) -> float:
        return max(config.VAD_RMS_THRESHOLD, self.noise_floor * 1.8)

    def record_until_silence(
        self,
        *,
        max_sec: float | None = None,
        silence_sec: float | None = None,
        start_timeout_sec: float = 5.0,
        min_speech_sec: float | None = None,
    ):
        """Пишет звук, пока пользователь говорит. Возвращает int16-массив или None."""
        if not HAS_SD or not HAS_NUMPY:
            raise SpeechError("sounddevice/numpy недоступны — запись невозможна")

        max_sec = max_sec or config.MAX_COMMAND_SEC
        silence_sec = silence_sec if silence_sec is not None else config.SILENCE_SEC
        min_speech_sec = min_speech_sec if min_speech_sec is not None else config.MIN_SPEECH_SEC

        chunk_frames = max(1, int(self.sample_rate * config.CHUNK_MS / 1000))
        max_chunks = max(1, int(max_sec * 1000 / config.CHUNK_MS))
        start_chunks = max(1, int(start_timeout_sec * 1000 / config.CHUNK_MS))
        silence_chunks = max(1, int(silence_sec * 1000 / config.CHUNK_MS))
        threshold = self._speech_threshold()

        collected = []
        silent_run = 0
        speech_started = False

        with self._busy:
            with sd.InputStream(samplerate=self.sample_rate, channels=config.CHANNELS,
                                dtype="int16", blocksize=chunk_frames) as stream:
                for index in range(max_chunks):
                    data, overflowed = stream.read(chunk_frames)
                    chunk = data[:, 0] if data.ndim > 1 else data
                    chunk = chunk.copy()
                    level = rms(chunk)

                    if level >= threshold:
                        speech_started = True
                        silent_run = 0
                    elif speech_started:
                        silent_run += 1

                    # Микрофонный уровень — чтобы интерфейс реагировал и на вас.
                    # Речь даёт rms порядка 0.05-0.2, поэтому поднимаем масштаб.
                    emit_level(min(1.0, level * 5.0), "mic")

                    collected.append(chunk)

                    if not speech_started and index >= start_chunks:
                        log("Речь не обнаружена.", "debug")
                        return None
                    if speech_started and silent_run >= silence_chunks:
                        break

        if not speech_started:
            return None

        audio = np.concatenate(collected)
        duration = len(audio) / self.sample_rate
        if duration < min_speech_sec:
            log(f"Слишком короткая фраза ({duration:.2f} с) — пропускаю.", "debug")
            return None
        return audio

    # ------------------------------------------------------------------
    #  Распознавание
    # ------------------------------------------------------------------
    def transcribe(self, audio) -> str:
        """int16-массив -> текст."""
        if audio is None or len(audio) == 0:
            return ""
        model = self._ensure_model()
        audio_f = to_float32(audio)

        segments, _info = model.transcribe(
            audio_f,
            language=config.WHISPER_LANGUAGE,
            beam_size=config.WHISPER_BEAM_SIZE,
            vad_filter=config.WHISPER_VAD_FILTER,
            no_speech_threshold=config.WHISPER_NO_SPEECH_THRESHOLD,
            condition_on_previous_text=config.WHISPER_CONDITION_ON_PREVIOUS,
            initial_prompt=config.WHISPER_INITIAL_PROMPT or None,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return self._clean(text)

    @staticmethod
    def _clean(text: str) -> str:
        text = re.sub(r"\s+", " ", text or "").strip()
        if not text:
            return ""
        low = text.lower().strip(" .,!?-…")
        if len(low) < 2:
            return ""
        if any(bad in low for bad in _HALLUCINATIONS):
            log(f"Отброшена галлюцинация Whisper: «{text}»", "debug")
            return ""
        # Сплошные повторы одного слова — тоже артефакт.
        words = low.split()
        if len(words) >= 4 and len(set(words)) == 1:
            log(f"Отброшен повтор: «{text}»", "debug")
            return ""
        return text

    # ------------------------------------------------------------------
    #  Wake-word
    # ------------------------------------------------------------------
    def listen_wake(self) -> str | None:
        """Слушает окно и отдаёт фразу ЦЕЛИКОМ, если в ней прозвучало обращение.

        Возвращает None, если обращения не было. Отдаём всю фразу, а не «да/нет»:
        если пользователь сказал «Джарвис, который час» на одном дыхании, команда
        уже внутри — записывать и распознавать её второй раз не нужно. Это экономит
        один вызов Whisper (около 1,6 с на CPU) на каждой команде.
        """
        try:
            audio = self.record_until_silence(
                max_sec=config.WAKE_WINDOW_SEC,
                silence_sec=config.WAKE_SILENCE_SEC,
                start_timeout_sec=config.WAKE_WINDOW_SEC,
                min_speech_sec=0.15,
            )
        except SpeechError:
            raise
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка записи: {exc}", "debug")
            return None

        if audio is None:
            return None

        audio = normalize_audio(audio)
        text = normalize_text(self.transcribe(audio))
        if not text:
            return None
        if self.has_wake_word(text):
            log(f"Услышал обращение: «{text}»", "info")
            return text
        return None

    def listen_wake_word(self) -> bool:
        """Короткая форма: прозвучало ли обращение. Подробности — в listen_wake."""
        return self.listen_wake() is not None

    @staticmethod
    def has_wake_word(text: str) -> bool:
        """Есть ли в тексте обращение — точное или похожее на слух."""
        for wake in config.WAKE_WORDS:
            if contains_word(text, wake):
                return True

        # Нечёткое сравнение по отдельным словам — на случай оговорок.
        for token in text.split():
            if len(token) < 4:
                continue
            for wake in config.WAKE_WORDS:
                ratio = difflib.SequenceMatcher(None, token, wake).ratio()
                if ratio >= config.WAKE_WORD_CUTOFF:
                    log(f"Похоже на обращение: «{token}» ≈ «{wake}» ({ratio:.2f})", "info")
                    return True
        return False

    # ------------------------------------------------------------------
    #  Команда
    # ------------------------------------------------------------------
    def listen_command(self, start_timeout_sec: float = 6.0) -> str:
        """Записывает и распознаёт команду. Возвращает текст (может быть пустым)."""
        try:
            audio = self.record_until_silence(
                max_sec=config.MAX_COMMAND_SEC,
                silence_sec=config.SILENCE_SEC,
                start_timeout_sec=start_timeout_sec,
            )
        except SpeechError:
            raise
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка микрофона: {exc}", "error")
            return ""

        if audio is None:
            return ""

        audio = normalize_audio(audio)
        level = rms(audio)
        if level < self._speech_threshold() * 0.8:
            log("Слишком тихо — не распознаю.", "debug")
            return ""

        text = self.transcribe(audio)
        if text:
            log(f"Распознано: «{text}»", "info")
        return text
