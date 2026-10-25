# -*- coding: utf-8 -*-
"""Слух Пятницы: микрофон + распознавание речи.

Запись звука — через ``sounddevice`` с VAD-детектором (останавливаемся, когда
пользователь замолчал). Распознавание — локальный движок, выбранный в
``config.ASR_ENGINE``:

* **GigaAM v3 RNN-T** (по умолчанию) — модель Сбера на ONNX Runtime, точнее и
  устойчивее на русском. Вся работа с ней — в :mod:`gigaam_asr`; здесь только
  точка вызова, поэтому остальной код (ядро, API, интерфейс) не меняется.
* **Vosk** — резерв, если ``onnx-asr`` не установлен. Оставлен как аварийный
  путь: он легче и работает даже на совсем слабом железе.

Оба движка принимают один и тот же int16-буфер 16 кГц и возвращают строку,
поэтому выше по стеку разницы между ними нет.
"""

from __future__ import annotations

import json
import re
import threading
import time
import zipfile
from pathlib import Path

import config
from events import emit_level
from utils import contains_word, log, normalize_audio, normalize_command_text, rms

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

# sounddevice импортируется по требованию (≈120 мс и 8 МБ на старте): нужен
# только для записи с микрофона, а в текстовом режиме не нужен вовсе.
# Наличие пакета проверяет config.FEATURES — это find_spec, без загрузки.
HAS_SD = bool(config.FEATURES.get("sounddevice"))
_SD = None


def _sd():
    """Модуль sounddevice (None, если его нет). Кэшируется после импорта."""
    global _SD
    if _SD is None and HAS_SD:
        import sounddevice
        _SD = sounddevice
    return _SD

# Типичные артефакты распознавания на тишине и шуме — отбрасываем их.
_ARTIFACTS = (
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

_DOWNLOAD_CHUNK = 1 << 20          # 1 МБ
_MODEL_MARKER = Path("conf") / "model.conf"


class SpeechError(RuntimeError):
    """Проблема с записью, моделью или распознаванием."""


# =====================================================================
#  ВЫБОР ДВИЖКА
# =====================================================================
_ENGINES = ("gigaam", "vosk")
_WARNED: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    """Одна и та же предупреждение — один раз за процесс, не в каждом вызове."""
    if key in _WARNED:
        return
    _WARNED.add(key)
    log(message, "warn")


def resolve_engine() -> str:
    """Какой движок распознавания реально доступен: gigaam | vosk | none.

    ``config.ASR_ENGINE`` — это пожелание, а не гарантия: если пакет не
    установлен, движок честно понижается до резервного, а ассистент
    продолжает работать (лучше Vosk, чем совсем без слуха).
    """
    requested = str(getattr(config, "ASR_ENGINE", "gigaam") or "gigaam").strip().lower()
    if requested not in _ENGINES + ("auto", ""):
        _warn_once("bad-engine", f"Неизвестный ASR_ENGINE «{requested}» — беру gigaam.")
        requested = "gigaam"

    gigaam_ok = bool(config.FEATURES.get("gigaam"))
    vosk_ok = bool(config.FEATURES.get("vosk"))

    if requested == "gigaam":
        if gigaam_ok:
            return "gigaam"
        _warn_once(
            "no-gigaam",
            "GigaAM недоступен (pip install \"onnx-asr[cpu,hub]\") — "
            + ("перехожу на Vosk." if vosk_ok else "распознавание речи выключено."),
        )
        return "vosk" if vosk_ok else "none"

    if requested == "vosk":
        if vosk_ok:
            return "vosk"
        _warn_once("no-vosk", "Vosk не установлен: pip install vosk")
        return "none"

    # auto / пусто: сначала GigaAM, потом резерв.
    if gigaam_ok:
        return "gigaam"
    return "vosk" if vosk_ok else "none"


def engine_label() -> str:
    """Имя движка для статуса, баннера и диагностики."""
    engine = resolve_engine()
    if engine == "gigaam":
        quant = "INT8" if getattr(config, "GIGAAM_QUANTIZATION", None) else "FP32"
        return f"GigaAM v3 RNN-T ({quant})"
    if engine == "vosk":
        return f"Vosk «{config.VOSK_MODEL_NAME}»"
    return "нет (движок распознавания не установлен)"


def asr_model_ready() -> bool:
    """Есть ли на диске модель активного движка (без загрузки в память)."""
    engine = resolve_engine()
    if engine == "gigaam":
        try:
            from gigaam_asr import recognizer_from_config
            return recognizer_from_config().files_present()
        except Exception:                              # noqa: BLE001
            return False
    if engine == "vosk":
        return model_ready()
    return False


# =====================================================================
#  Модель Vosk
# =====================================================================
def model_path() -> Path:
    """Путь к папке модели (относительный путь считается от папки проекта)."""
    path = Path(config.VOSK_MODEL_PATH)
    if not path.is_absolute():
        path = config.BASE_DIR / path
    return path


def model_ready(path: Path | None = None) -> bool:
    """Есть ли на диске распакованная модель."""
    target = Path(path) if path else model_path()
    return (target / _MODEL_MARKER).exists() or (target / "am" / "final.mdl").exists()


def ensure_model(path: Path | None = None, *, allow_download: bool | None = None) -> Path:
    """Возвращает папку модели, при необходимости скачивает и распаковывает её."""
    target = Path(path) if path else model_path()
    if model_ready(target):
        return target

    if allow_download is None:
        allow_download = bool(getattr(config, "VOSK_AUTO_DOWNLOAD", True))
    if not allow_download:
        raise SpeechError(
            f"Модель Vosk не найдена: {target}. Скачайте её вручную с "
            f"{config.VOSK_MODEL_URL} и распакуйте в {target.parent}."
        )

    url = str(config.VOSK_MODEL_URL)
    archive = config.MODELS_DIR / f"{target.name}.zip"
    config.MODELS_DIR.mkdir(parents=True, exist_ok=True)

    log(f"Скачиваю модель Vosk «{target.name}» (около 45 МБ)...", "info")
    started = time.time()
    try:
        _download(url, archive)
    except Exception as exc:  # noqa: BLE001
        archive.unlink(missing_ok=True)
        raise SpeechError(
            f"Не удалось скачать модель Vosk: {exc}. Проверьте интернет или "
            f"скачайте {url} вручную и распакуйте в {config.MODELS_DIR}."
        ) from exc

    log("Распаковываю модель Vosk...", "info")
    try:
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(config.MODELS_DIR)
    except (OSError, zipfile.BadZipFile) as exc:
        raise SpeechError(f"Архив модели Vosk повреждён: {exc}") from exc
    finally:
        archive.unlink(missing_ok=True)

    if not model_ready(target):
        # В архиве может быть одна вложенная папка с другим именем.
        found = _find_model_dir(config.MODELS_DIR)
        if found is not None:
            target = found
    if not model_ready(target):
        raise SpeechError(
            f"После распаковки модель Vosk не найдена в {config.MODELS_DIR}."
        )

    log(f"Модель Vosk готова за {time.time() - started:.1f} с: {target}", "info")
    return target


def _download(url: str, destination: Path) -> None:
    """Скачивает файл с прогрессом в лог. Недокачанный файл не оставляем."""
    # urllib.request тянет ssl и http.client (~40 мс) — нужен только при
    # скачивании модели Vosk, поэтому импортируется здесь, а не на уровне модуля.
    import urllib.request

    partial = destination.with_suffix(destination.suffix + ".part")
    partial.unlink(missing_ok=True)
    downloaded = 0
    next_report = 10 * _DOWNLOAD_CHUNK

    request = urllib.request.Request(url, headers={"User-Agent": "Pyatnitsa/3.1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, \
                open(partial, "wb") as handle:
            total = int(response.headers.get("Content-Length") or 0)
            while True:
                chunk = response.read(_DOWNLOAD_CHUNK)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                if downloaded >= next_report:
                    percent = f" ({downloaded * 100 // total}%)" if total else ""
                    log(f"  ... {downloaded // (1 << 20)} МБ{percent}", "debug")
                    next_report += 10 * _DOWNLOAD_CHUNK
        if downloaded == 0:
            raise OSError("сервер вернул пустой файл")
        partial.replace(destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def _find_model_dir(root: Path) -> Path | None:
    for candidate in root.iterdir():
        if candidate.is_dir() and model_ready(candidate):
            return candidate
    return None


# =====================================================================
#  Слушатель
# =====================================================================
class Listener:
    """Микрофон + выбранный движок распознавания (GigaAM или Vosk).

    Публичный контракт не изменился: ``record_until_silence``, ``transcribe``,
    ``listen_wake``, ``listen_command``, ``model_ready``. Именно поэтому замена
    движка не требует правок ни в ядре, ни в API, ни в интерфейсе.
    """

    def __init__(self, model_path_arg: str | None = None):
        if model_path_arg:
            self.model_path = Path(model_path_arg)
            if not self.model_path.is_absolute():
                self.model_path = config.BASE_DIR / self.model_path
        else:
            self.model_path = model_path()
        # Частота микрофона. Оба движка хотят 16 кГц моно; если карта отдаёт
        # 44.1/48 кГц, gigaam_asr сам делает ресемплинг (soxr).
        self.sample_rate = int(getattr(config, "SAMPLE_RATE", config.VOSK_SAMPLE_RATE))
        self.noise_floor = 0.015
        self.engine = resolve_engine()
        self._model = None            # Vosk
        self._vosk = None
        self._asr = None              # GigaAM
        self._asr_lock = threading.Lock()
        self._model_lock = threading.Lock()
        self._busy = threading.Lock()

    # ------------------------------------------------------------------
    #  Модель
    # ------------------------------------------------------------------
    @property
    def model_ready(self) -> bool:
        if self.engine == "gigaam":
            return bool(self._asr is not None and self._asr.model_ready)
        return self._model is not None

    @property
    def model_name(self) -> str:
        if self.engine == "gigaam":
            if self._asr is not None:
                return self._asr.model_name
            return str(getattr(config, "GIGAAM_MODEL_NAME", "gigaam-v3-rnnt"))
        return self.model_path.name

    def preload_model(self) -> bool:
        """Грузит модель активного движка (GigaAM при первом запуске скачает её)."""
        if self.engine == "gigaam":
            try:
                return self._ensure_asr().preload()
            except SpeechError as exc:
                log(str(exc), "error")
                return False
            except Exception as exc:                   # noqa: BLE001
                log(f"Не удалось загрузить GigaAM: {exc}", "error")
                return False

        if self.engine == "vosk":
            if not config.FEATURES["vosk"]:
                log("Vosk не установлен: pip install vosk", "warn")
                return False
            try:
                self._ensure_model()
                return True
            except SpeechError as exc:
                log(str(exc), "error")
                return False
            except Exception as exc:                   # noqa: BLE001
                log(f"Не удалось загрузить модель Vosk: {exc}", "error")
                return False

        log("Движок распознавания не установлен: "
            "pip install \"onnx-asr[cpu,hub]\" soundfile soxr", "warn")
        return False

    def _new_asr(self):
        """Создаёт GigaAM-распознаватель. Отдельный метод — удобно подменять в тестах."""
        from gigaam_asr import recognizer_from_config
        return recognizer_from_config()

    def _ensure_asr(self):
        """Ленивая инициализация GigaAM (сама модель грузится в ``preload``)."""
        if self._asr is not None:
            return self._asr
        with self._asr_lock:
            if self._asr is None:
                self._asr = self._new_asr()
        return self._asr

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is not None:
                return self._model
            from vosk import Model, SetLogLevel

            # Kaldi пишет в stderr служебные простыни — в консоли они лишние.
            SetLogLevel(-1)
            path = ensure_model(self.model_path)
            log(f"Загружаю Vosk «{path.name}»...", "info")
            started = time.time()
            self._vosk = __import__("vosk")
            self._model = Model(str(path))
            log(f"Vosk готов за {time.time() - started:.1f} с.", "info")
        return self._model

    def _new_recognizer(self):
        """Создаёт распознаватель. Отдельный метод — удобно подменять в тестах."""
        vosk = self._vosk or __import__("vosk")
        recognizer = vosk.KaldiRecognizer(self._ensure_model(), self.sample_rate)
        if getattr(config, "VOSK_WORDS", True):
            recognizer.SetWords(True)
        return recognizer

    # ------------------------------------------------------------------
    #  Калибровка микрофона
    # ------------------------------------------------------------------
    def warm_audio(self) -> bool:
        """Поднимает sounddevice/PortAudio заранее (без записи).

        Импорт модуля и инициализация PortAudio стоят ~0.12 с сами по себе, но
        под нагрузкой (в соседнем потоке грузится голос Piper и держит GIL) —
        до 1.6 с. Поэтому греем аудио до старта Piper, а не в момент калибровки.
        """
        return _sd() is not None

    def calibrate(self, duration: float | None = None) -> float:
        sd = _sd()
        if sd is None:
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
        sd = _sd()
        if sd is None or not HAS_NUMPY:
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
    def transcribe(self, audio, sample_rate: int | None = None) -> str:
        """int16-массив (или байты PCM) -> текст.

        ``sample_rate`` — частота входного буфера; по умолчанию частота
        микрофона. Указывать её нужно, только если буфер записан с другой
        частотой (например, WAV на 44.1/48 кГц): GigaAM сам сделает ресемплинг.
        """
        raw = _pcm16_bytes(audio)
        if not raw:
            return ""
        rate = int(sample_rate or self.sample_rate)
        if self.engine == "gigaam":
            return self._clean(self._transcribe_gigaam(raw, rate))
        if self.engine == "vosk":
            return self._clean(self._transcribe_vosk(raw))
        return ""

    def _transcribe_gigaam(self, raw: bytes, sample_rate: int | None = None) -> str:
        """GigaAM v3 RNN-T: сырой PCM16 -> текст (ресемплинг внутри модели)."""
        try:
            return self._ensure_asr().transcribe(
                raw, sample_rate=sample_rate or self.sample_rate)
        except Exception as exc:                       # noqa: BLE001
            log(f"GigaAM: {exc}", "error")
            return ""

    def _transcribe_vosk(self, raw: bytes) -> str:
        """Резервный путь на Vosk."""
        recognizer = self._new_recognizer()
        recognizer.AcceptWaveform(raw)
        try:
            result = json.loads(recognizer.FinalResult())
        except (ValueError, TypeError) as exc:
            log(f"Vosk вернул некорректный результат: {exc}", "warn")
            return ""
        return str(result.get("text", ""))

    @staticmethod
    def _clean(text: str) -> str:
        text = re.sub(r"\s+", " ", text or "").strip()
        if not text:
            return ""
        low = text.lower().strip(" .,!?-…")
        if len(low) < 2:
            return ""
        if any(bad in low for bad in _ARTIFACTS):
            log(f"Отброшен артефакт распознавания: «{text}»", "debug")
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
        если пользователь сказал «Пятница, который час» на одном дыхании, команда
        уже внутри — записывать и распознавать её второй раз не нужно. Это экономит
        один проход распознавания на каждой команде.
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
        text = normalize_command_text(self.transcribe(audio))
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
        import difflib

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


def _pcm16_bytes(audio) -> bytes:
    """Приводит int16-массив (numpy/list/bytes) к сырым байтам для Vosk."""
    if audio is None:
        return b""
    if isinstance(audio, (bytes, bytearray)):
        return bytes(audio)
    if HAS_NUMPY:
        array = np.asarray(audio)
        if array.size == 0:
            return b""
        if array.dtype != np.int16:
            if array.dtype.kind == "f":
                array = np.clip(array, -1.0, 1.0) * 32767.0
            array = array.astype(np.int16, copy=False)
        return array.tobytes()
    return b""
