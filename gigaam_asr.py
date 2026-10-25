# -*- coding: utf-8 -*-
"""Распознавание речи Пятницы на GigaAM v3 (RNN-T) через ONNX Runtime.

Модель
-----
GigaAM v3 — акустическая модель Salute Developers (SberDevices), обученная на
русской речи. Используется вариант **RNN-T** (``v3_rnnt``): он точнее CTC
(4.39 % против 4.72 % WER на бенчмарке GigaAM) и лучше «схватывает» короткие
команды с первого раза.

Лицензия модели — **MIT**, © SberDevices / salute-developers:
https://github.com/salute-developers/GigaAM
Конвертация в ONNX и обвязка инференса — пакет ``onnx-asr`` (MIT):
https://github.com/istupakov/onnx-asr
ONNX-веса: https://huggingface.co/istupakov/gigaam-v3-onnx

Атрибуция обязательна при распространении продукта — этот блок комментария
нужно сохранять в исходниках.

Почему ONNX, а не PyTorch
-------------------------
В продакшн тянется только ``onnxruntime`` (CPU). PyTorch для инференса не
нужен: он весит сотни мегабайт, конфликтует с драйверами и не даёт выигрыша
на коротких фразах. Замер на обычном ноутбуке (INT8, настройки ORT по умолчанию):
RTFx ≈ 20, то есть 4-секундная фраза распознаётся за 0.2 с. Для сравнения
``vosk-model-small-ru`` на тех же фразах даёт RTFx ≈ 4.6.

Требования к звуку
------------------
Модель обучена на **16 кГц, моно, PCM**. Всё остальное приводится здесь:

    микрофон (44.1/48 кГц, int16)
        → numpy float32, моно
        → soxr.resample(...16 кГц)
        → окна ≤ 22 с (модель обучена на 30-секундных фрагментах)
        → model.recognize(waveform, sample_rate=16000)

``onnx-asr`` умеет ресемплировать сам, но мы делаем это явно: так контролируем
качество (soxr, а не линейная интерполяция), момент и стоимость операции, а
также можем отдать модели уже готовый float32-буфер без повторных копий.

Пример
------
::

    from gigaam_asr import GigaAMRecognizer

    rec = GigaAMRecognizer()          # ленивый: модель грузится в preload()
    rec.preload()
    text = rec.transcribe(pcm_int16, sample_rate=48000)

    # поток с микрофона: отдаём блоки, получаем готовые фразы
    for phrase in rec.transcribe_stream(mic_chunks(), sample_rate=48000):
        print(phrase)

CLI для проверки без запуска ассистента::

    python gigaam_asr.py --file sample.wav
    python gigaam_asr.py --mic
    python gigaam_asr.py --info
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

import config
from utils import log

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:                                    # pragma: no cover
    HAS_NUMPY = False

#: Частота, на которой обучена GigaAM. Менять нельзя.
TARGET_RATE = 16000

#: Имя модели в каталоге onnx-asr (см. supported model names).
DEFAULT_MODEL = "gigaam-v3-rnnt"

#: GigaAM обучена на 30-секундных окнах. 22 с — с запасом: длиннее окно,
#: тем больше пиков памяти, а команды Пятницы и так ограничены 15 секундами.
MAX_WINDOW_SEC = 22.0

#: Фраза короче — шум; декодировать её бессмысленно.
MIN_WINDOW_SEC = 0.2

#: Кадр для поиска тихого места при разрезании длинного аудио.
_FRAME_MS = 20

_ONNX_ASR = None          # кэш импорта: import onnx_asr стоит ~100 мс
_IMPORT_LOCK = threading.Lock()


class GigaAMError(RuntimeError):
    """Модель не загрузилась, зависимости не установлены или сбой инференса."""


# =====================================================================
#  ЗАВИСИМОСТИ
# =====================================================================
def _import_onnx_asr():
    """Импортирует ``onnx_asr`` один раз и переводит ошибку на русский."""
    global _ONNX_ASR
    if _ONNX_ASR is not None:
        return _ONNX_ASR
    with _IMPORT_LOCK:
        if _ONNX_ASR is not None:
            return _ONNX_ASR
        try:
            import onnx_asr
        except ImportError as exc:
            raise GigaAMError(
                "onnx-asr не установлен. Установите: "
                "pip install \"onnx-asr[cpu,hub]\" soundfile soxr"
            ) from exc
        _ONNX_ASR = onnx_asr
    return _ONNX_ASR


def _import_soxr():
    """soxr — быстрый ресемплер без GPL-хвостов (в отличие от librosa)."""
    try:
        import soxr
        return soxr
    except ImportError:
        return None


def available() -> bool:
    """Есть ли в окружении всё необходимое для GigaAM (без загрузки модели)."""
    try:
        _import_onnx_asr()
    except GigaAMError:
        return False
    return True


# =====================================================================
#  АУДИО → FLOAT32 МОНО
# =====================================================================
def _to_float_mono(audio, sample_rate: int | None):
    """Приводит любой вход к ``(float32 ndarray, sample_rate)``.

    Понимает сырые байты PCM16, numpy-массив (int16/int32/float), список и
    путь к WAV. Многоканальный массив сводится в моно усреднением каналов.
    """
    if not HAS_NUMPY:
        raise GigaAMError("numpy недоступен — распознавание невозможно")

    # --- путь к файлу ---------------------------------------------------
    if isinstance(audio, (str, Path)):
        return _read_wav(Path(audio))

    # --- сырые байты PCM16 ---------------------------------------------
    if isinstance(audio, (bytes, bytearray, memoryview)):
        raw = np.frombuffer(bytes(audio), dtype=np.int16)
        return raw.astype(np.float32) / 32768.0, int(sample_rate or TARGET_RATE)

    data = np.asarray(audio)
    if data.size == 0:
        return np.zeros(0, dtype=np.float32), int(sample_rate or TARGET_RATE)

    # --- каналы ---------------------------------------------------------
    if data.ndim > 1:
        # (frames, channels) — усредняем; (channels, frames) — тоже усредняем
        # по той оси, которая короче: у моно-записи вторая ось равна 1.
        axis = 1 if data.shape[1] <= data.shape[0] else 0
        data = data.mean(axis=axis)

    # --- тип ------------------------------------------------------------
    if np.issubdtype(data.dtype, np.integer):
        info = np.iinfo(data.dtype)
        # Для знаковых шкала 2**(bits-1): int16 -> 32768, int32 -> 2147483648.
        scale = float(info.max) + 1.0 if info.min < 0 else float(info.max)
        samples = data.astype(np.float32) / scale
    else:
        samples = data.astype(np.float32, copy=False)
        peak = float(np.max(np.abs(samples))) if samples.size else 0.0
        # На всякий случай: если пришёл «int16 в float-обёртке», нормируем.
        if peak > 1.5:
            samples = samples / 32768.0

    return np.clip(samples, -1.0, 1.0), int(sample_rate or TARGET_RATE)


def _read_wav(path: Path):
    """Читает WAV через soundfile (PCM 8/16/24/32, float)."""
    try:
        import soundfile as sf
    except ImportError as exc:
        raise GigaAMError(
            "soundfile не установлен — не могу прочитать WAV: pip install soundfile"
        ) from exc
    try:
        data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    except Exception as exc:                           # noqa: BLE001
        raise GigaAMError(f"Не удалось прочитать «{path}»: {exc}") from exc
    return data.mean(axis=1), int(rate)


def _resample(samples, source_rate: int, target_rate: int):
    """Ресемплинг soxr с линейным резервом, если soxr не установлен."""
    if source_rate == target_rate or samples.size == 0:
        return samples
    soxr = _import_soxr()
    if soxr is not None:
        # HQ — «высокое» качество: для речи этого достаточно, VHQ дороже.
        resampled = soxr.resample(samples, source_rate, target_rate, quality="HQ")
        return np.asarray(resampled, dtype=np.float32)

    # Резерв: линейная интерполяция. Заметно хуже на 48→16 кГц (алиасинг),
    # поэтому ругаемся в лог, но не роняем ассистента.
    log("soxr не установлен — ресемплинг линейный, точность будет ниже "
        "(pip install soxr)", "warn")
    duration = samples.size / float(source_rate)
    target_size = max(1, int(round(duration * target_rate)))
    source_points = np.linspace(0.0, duration, num=samples.size, endpoint=False)
    target_points = np.linspace(0.0, duration, num=target_size, endpoint=False)
    return np.interp(target_points, source_points, samples).astype(np.float32)


# =====================================================================
#  РАЗРЕЗАНИЕ ДЛИННОГО АУДИО
# =====================================================================
def _quiet_cut(samples, start: int, end: int, rate: int) -> int:
    """Ищет самое тихое место в последних 3 секундах окна — там и режем.

    Резать по таймеру нельзя: разрежем слово пополам — потеряем его на обоих
    краях. Тихая точка (пауза между словами) почти всегда рядом с границей.
    """
    frame = max(1, int(rate * _FRAME_MS / 1000))
    search_from = max(start + frame, end - 3 * rate)
    best_index, best_energy = end, None
    for offset in range(search_from, end - frame + 1, frame):
        chunk = samples[offset:offset + frame]
        energy = float(np.mean(np.square(chunk)))
        if best_energy is None or energy < best_energy:
            best_energy, best_index = energy, offset + frame // 2
    # Прогресс обязателен: иначе бесконечный цикл на ровном сигнале.
    return best_index if best_index > start + (end - start) // 2 else end


def _split_windows(samples, rate: int, max_window_sec: float):
    """Делит длинную запись на окна, которые «переварит» GigaAM."""
    limit = max(1, int(max_window_sec * rate))
    if samples.size <= limit:
        yield samples
        return
    start = 0
    while start < samples.size:
        end = min(start + limit, samples.size)
        if end < samples.size:
            end = _quiet_cut(samples, start, end, rate)
        yield samples[start:end]
        start = end


# =====================================================================
#  РАСПОЗНАВАТЕЛЬ
# =====================================================================
class GigaAMRecognizer:
    """GigaAM v3 RNN-T на ONNX Runtime (CPU).

    Объект потокобезопасен: модель грузится один раз под локом, инференс
    сериализуется (одновременные ``recognize`` на одном сеансе только жгут
    ядра и не ускоряют работу).

    Параметры
    ---------
    model:
        Имя модели в каталоге onnx-asr. По умолчанию ``gigaam-v3-rnnt``.
    model_dir:
        Куда скачать/откуда читать ONNX-файлы. По умолчанию
        ``config.GIGAAM_MODEL_DIR`` (``data/models/gigaam-v3-rnnt``).
    quantization:
        ``None`` — fp32 (≈890 МБ), ``"int8"`` — квантованная (≈226 МБ).
        INT8 вчетверо меньше на диске и в памяти; на нашем тесте скорость и
        текст совпали с fp32, но на CPU без VNNI квантование может дать
        прирост, а может и просадку на 10–20 %. Точность INT8 обычно ниже
        на десятые доли процентного пункта WER — проверяйте на своих фразах.
    num_threads:
        Сколько ядер отдать ONNX Runtime. ``None`` — не трогать настройки
        ORT (он берёт физические ядра сам). Ставить «на глаз» нельзя:
        лишние потоки на 2-ядерном ноутбуке замедляют декодирование.
    max_window_sec:
        Предел одного окна распознавания.
    warmup:
        Прогреть граф пустым буфером при загрузке (первый вызов иначе
        заметно медленнее — на нём инициализируются арены памяти).
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        model_dir: str | Path | None = None,
        quantization: str | None = None,
        num_threads: int | None = None,
        max_window_sec: float = MAX_WINDOW_SEC,
        warmup: bool = True,
        target_rate: int = TARGET_RATE,
    ):
        self.model = model or str(getattr(config, "GIGAAM_MODEL_NAME", DEFAULT_MODEL))
        self.model_dir = model_dir
        self.quantization = quantization
        self.num_threads = num_threads
        self.max_window_sec = max(1.0, float(max_window_sec))
        self.warmup = bool(warmup)
        self.target_rate = int(target_rate)

        self._model = None
        self._load_lock = threading.RLock()
        self._infer_lock = threading.Lock()
        self._load_seconds = 0.0

    # ------------------------------------------------------------------
    #  Состояние
    # ------------------------------------------------------------------
    @property
    def model_ready(self) -> bool:
        """Загружена ли модель в память (файлы на диске не считаются)."""
        return self._model is not None

    @property
    def model_name(self) -> str:
        suffix = ".int8" if self.quantization else ""
        return f"{self.model}{suffix}"

    @property
    def model_path(self) -> Path:
        """Папка с ONNX-файлами.

        Создаётся только РОДИТЕЛЬСКАЯ папка. Саму папку модели трогать нельзя:
        ``onnx-asr`` считает существующую папку локальной моделью и в этом
        случае не скачивает ничего (падает с «file not found»). Если папки
        нет — библиотека скачает в неё веса при первом запуске.
        """
        if self.model_dir:
            path = Path(self.model_dir)
        else:
            path = Path(getattr(config, "GIGAAM_MODEL_DIR",
                                config.MODELS_DIR / "gigaam-v3-rnnt"))
        if not path.is_absolute():
            path = config.BASE_DIR / path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise GigaAMError(f"Не удалось создать папку модели {path}: {exc}") from exc
        return path

    def files_present(self) -> bool:
        """Лежат ли ONNX-файлы на диске (без загрузки в память)."""
        try:
            path = self.model_path
            names = [p.name for p in path.glob("*.onnx")]
        except (GigaAMError, OSError):
            return False
        if self.quantization:
            return any(name.endswith(".int8.onnx") for name in names)
        return any(not name.endswith(".int8.onnx") for name in names)

    # ------------------------------------------------------------------
    #  Загрузка
    # ------------------------------------------------------------------
    def _session_options(self):
        """Настройки ORT: оптимизация графа и (опционально) число потоков."""
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise GigaAMError(
                "onnxruntime не установлен: pip install onnxruntime"
            ) from exc

        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.log_severity_level = 3          # без служебных простыней ORT
        if self.num_threads:
            # Явное число потоков: 1 inter-op + N intra-op, последовательный
            # режим — иначе ORT плодит потоки и на слабом CPU всё встаёт.
            options.intra_op_num_threads = max(1, int(self.num_threads))
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        return options

    def _prepare_dir(self, path: Path) -> None:
        """Готовит папку модели к загрузке.

        ``onnx-asr`` считает любую существующую папку готовой локальной моделью
        и в этом случае ничего не скачивает. Пустая папка (след неудачного
        скачивания) навсегда ломала бы запуск — убираем её. Непустую папку без
        ONNX-файлов не трогаем: это может быть чужая папка, честнее сказать
        пользователю, что делать.
        """
        if not path.exists():
            return
        try:
            entries = list(path.iterdir())
        except OSError:
            return
        if not entries:
            try:
                path.rmdir()
                log(f"Пустая папка модели {path} удалена — скачаю заново.", "debug")
            except OSError:
                pass
        elif not any(item.suffix == ".onnx" for item in entries):
            raise GigaAMError(
                f"Папка {path} существует, но ONNX-файлов в ней нет: onnx-asr "
                f"считает её готовой моделью и не станет ничего скачивать. "
                f"Удалите папку и запустите снова — веса скачаются заново."
            )

    def _ensure_loaded(self):
        """Ленивая загрузка модели. Бросает ``GigaAMError`` при проблеме."""
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model

            onnx_asr = _import_onnx_asr()
            path = self.model_path
            self._prepare_dir(path)
            started = time.time()
            log(f"Загружаю GigaAM «{self.model_name}» из {path}...", "info")

            try:
                model = onnx_asr.load_model(
                    self.model,
                    str(path),
                    quantization=self.quantization,
                    sess_options=self._session_options(),
                )
            except Exception as exc:                   # noqa: BLE001
                raise GigaAMError(
                    f"Не удалось загрузить GigaAM «{self.model}»: {exc}. "
                    f"Проверьте интернет (первый запуск скачивает модель) "
                    f"и свободное место в {path}."
                ) from exc

            self._model = model
            self._load_seconds = time.time() - started
            log(f"GigaAM готова за {self._load_seconds:.1f} с "
                f"({self.model_name}).", "info")

            if self.warmup:
                self._warmup()
        return self._model

    def _warmup(self) -> None:
        """Прогрев: первый инференс всегда медленнее следующих."""
        try:
            silence = np.zeros(self.target_rate, dtype=np.float32)   # 1 с
            self._model.recognize(silence, sample_rate=self.target_rate)
        except Exception as exc:                       # noqa: BLE001
            # Прогрев не критичен: настоящий вызов всё равно покажет ошибку.
            log(f"Прогрев GigaAM не удался (не критично): {exc}", "debug")

    def preload(self) -> bool:
        """Грузит модель заранее. ``False`` — не получилось (уже залогировано)."""
        try:
            self._ensure_loaded()
            return True
        except GigaAMError as exc:
            log(str(exc), "error")
            return False
        except Exception as exc:                       # noqa: BLE001
            log(f"Неожиданная ошибка загрузки GigaAM: {exc}", "error")
            return False

    # ------------------------------------------------------------------
    #  Распознавание
    # ------------------------------------------------------------------
    def transcribe(self, audio, sample_rate: int | None = None) -> str:
        """Распознаёт фразу целиком.

        Параметры
        ---------
        audio:
            ``numpy``-массив (int16/float32, моно или каналы), сырые байты
            PCM16, список сэмплов или путь к WAV-файлу.
        sample_rate:
            Частота дискретизации входа. Для WAV определяется автоматически.
            Для байтов обязательна (по умолчанию 16 кГц).

        Возвращает
        ----------
        str
            Текст без пунктуации (модель RNN-T её не ставит), пустая строка —
            если речи нет или аудио слишком короткое.
        """
        samples, rate = _to_float_mono(audio, sample_rate)
        if samples.size == 0:
            return ""

        samples = _resample(samples, rate, self.target_rate)
        if samples.size < int(MIN_WINDOW_SEC * self.target_rate):
            return ""

        model = self._ensure_loaded()
        parts: list[str] = []
        with self._infer_lock:
            for window in _split_windows(samples, self.target_rate, self.max_window_sec):
                try:
                    result = model.recognize(window, sample_rate=self.target_rate)
                except Exception as exc:               # noqa: BLE001
                    raise GigaAMError(f"Сбой распознавания GigaAM: {exc}") from exc
                if isinstance(result, str) and result.strip():
                    parts.append(result.strip())
        return _join(parts)

    def transcribe_stream(
        self,
        chunks,
        *,
        sample_rate: int | None = None,
        threshold: float | None = None,
        silence_sec: float = 0.4,
        min_speech_sec: float = 0.25,
        max_utterance_sec: float | None = None,
        on_partial=None,
        partial_interval: float = 0.0,
    ):
        """Распознаёт поток блоков с микрофона и отдаёт готовые фразы.

        GigaAM v3 RNN-T в onnx-asr — **офлайн-модель**: она принимает фразу
        целиком и не имеет cache-aware энкодера, поэтому «настоящего»
        потокового декодирования здесь нет. Поток реализован как накопление
        буфера и декодирование по паузе — для ассистента это то, что нужно:
        команда всё равно произносится до конца.

        Параметры
        ---------
        chunks:
            Итерируемое из блоков (numpy int16/float32, байты PCM16).
            ``None`` в потоке — принудительный финал (конец записи).
        sample_rate:
            Частота блоков. Обязательна, если блоки — сырые байты.
        threshold:
            RMS-порог речи (0..1). По умолчанию ``config.VAD_RMS_THRESHOLD``.
        silence_sec:
            Сколько тишины считать концом фразы.
        min_speech_sec:
            Фразы короче не отдаются (щелчки, кашель).
        max_utterance_sec:
            Принудительная отдача, если паузы нет слишком долго.
        on_partial:
            Колбэк ``(text) -> None`` для промежуточных гипотез. Вызывается
            не чаще ``partial_interval`` секунд. Промежуточные гипотезы не
            отдаются из генератора — только финальные.
        partial_interval:
            Период промежуточных гипотез в секундах. ``0`` — выключено
            (каждый частичный декод стоит столько же, сколько финальный).

        Yields
        ------
        str
            Распознанная фраза после каждой паузы.
        """
        rate = int(sample_rate or self.target_rate)
        if threshold is None:
            threshold = float(getattr(config, "VAD_RMS_THRESHOLD", 0.010))
        max_utterance_sec = float(max_utterance_sec or config.MAX_COMMAND_SEC)

        buffer: list = []
        buffered = 0.0          # секунд в буфере
        silent_run = 0.0        # секунд тишины подряд
        speech_started = False
        last_partial_at = 0.0
        started_at = time.time()

        for chunk in chunks:
            if chunk is None:                       # конец потока
                break

            samples, _ = _to_float_mono(chunk, rate)
            if samples.size == 0:
                continue
            duration = samples.size / float(rate)
            level = float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0

            if level >= threshold:
                speech_started = True
                silent_run = 0.0
            elif speech_started:
                silent_run += duration

            buffer.append(samples)
            buffered += duration

            # Промежуточная гипотеза — по таймеру, а не по каждому блоку.
            if (on_partial and partial_interval > 0 and speech_started
                    and buffered >= 0.6
                    and time.time() - last_partial_at >= partial_interval):
                last_partial_at = time.time()
                try:
                    on_partial(self.transcribe(np.concatenate(buffer), sample_rate=rate))
                except GigaAMError as exc:
                    log(f"Промежуточное распознавание: {exc}", "debug")

            finished = speech_started and silent_run >= silence_sec
            too_long = buffered >= max_utterance_sec
            if not (finished or too_long):
                continue

            audio = np.concatenate(buffer)
            spoken = buffered - silent_run
            buffer, buffered, silent_run = [], 0.0, 0.0
            speech_started = False
            last_partial_at = 0.0

            if spoken < min_speech_sec:
                continue
            text = self.transcribe(audio, sample_rate=rate)
            if text:
                yield text

        # Хвост: поток закончился, а в буфере осталась речь.
        if buffer:
            audio = np.concatenate(buffer)
            if buffered >= min_speech_sec:
                text = self.transcribe(audio, sample_rate=rate)
                if text:
                    yield text
        log(f"Поток завершён за {time.time() - started_at:.1f} с.", "debug")

    def close(self) -> None:
        """Забывает модель (сеансы ORT освободит сборщик мусора)."""
        with self._load_lock:
            self._model = None


# =====================================================================
#  УТИЛИТЫ
# =====================================================================
def _join(parts) -> str:
    """Склеивает окна и убирает лишние пробелы."""
    text = " ".join(part.strip() for part in parts if part and part.strip())
    return " ".join(text.split())


def recognizer_from_config() -> GigaAMRecognizer:
    """Собирает распознаватель по настройкам ``config``."""
    quantization = getattr(config, "GIGAAM_QUANTIZATION", None) or None
    threads = int(getattr(config, "GIGAAM_NUM_THREADS", 0) or 0) or None
    return GigaAMRecognizer(
        model=str(getattr(config, "GIGAAM_MODEL_NAME", DEFAULT_MODEL)),
        model_dir=getattr(config, "GIGAAM_MODEL_DIR", None),
        quantization=quantization,
        num_threads=threads,
        max_window_sec=float(getattr(config, "GIGAAM_MAX_WINDOW_SEC", MAX_WINDOW_SEC)),
    )


# =====================================================================
#  CLI: проверка без запуска ассистента
# =====================================================================
def _record_microphone(seconds: float = 5.0, rate: int = TARGET_RATE):
    """Пишет микрофон до паузы (упрощённый VAD для CLI-проверки)."""
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise GigaAMError("sounddevice не установлен: pip install sounddevice") from exc

    block = int(rate * 0.1)
    collected, silent, speech = [], 0.0, False
    with sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                        blocksize=block) as stream:
        for _ in range(int(seconds * 10)):
            data, _ = stream.read(block)
            chunk = data[:, 0] if data.ndim > 1 else data
            level = float(np.sqrt(np.mean(np.square(chunk.astype(np.float32) / 32768.0))))
            if level >= config.VAD_RMS_THRESHOLD:
                speech, silent = True, 0.0
            elif speech:
                silent += 0.1
            collected.append(chunk.copy())
            if speech and silent >= config.SILENCE_SEC:
                break
    return np.concatenate(collected) if collected else np.zeros(0, dtype=np.int16)


def main(argv=None) -> int:
    """``python gigaam_asr.py`` — быстрая проверка движка."""
    parser = argparse.ArgumentParser(description="Проверка GigaAM v3 RNN-T (ONNX)")
    parser.add_argument("--file", help="WAV для распознавания")
    parser.add_argument("--mic", action="store_true", help="записать с микрофона")
    parser.add_argument("--seconds", type=float, default=6.0,
                        help="максимум записи с микрофона")
    parser.add_argument("--quantize", choices=["int8", "fp32"], default="int8",
                        help="int8 — компактная модель (по умолчанию), fp32 — точнее")
    parser.add_argument("--threads", type=int, default=0,
                        help="потоков ONNX Runtime (0 — по умолчанию ORT)")
    parser.add_argument("--info", action="store_true", help="показать состояние и выйти")
    args = parser.parse_args(argv)

    quantization = "int8" if args.quantize == "int8" else None
    recognizer = GigaAMRecognizer(
        quantization=quantization,
        num_threads=args.threads or None,
    )

    if args.info:
        print(f"Модель:        {recognizer.model_name}")
        print(f"Папка:         {recognizer.model_path}")
        print(f"Файлы на диске:{' да' if recognizer.files_present() else ' нет (скачается при загрузке)'}")
        print(f"onnx-asr:      {'да' if available() else 'нет'}")
        print(f"soxr:          {'да' if _import_soxr() else 'нет (линейный резерв)'}")
        return 0

    if not args.file and not args.mic:
        parser.error("нужен --file или --mic (либо --info)")

    audio = None
    rate = TARGET_RATE
    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"Файл не найден: {path}")
            return 2
        audio, rate = _read_wav(path)
    else:
        print("Говорите...")
        audio = _record_microphone(args.seconds)
        if audio.size == 0:
            print("Ничего не записано.")
            return 2

    started = time.time()
    text = recognizer.transcribe(audio, sample_rate=rate)
    elapsed = time.time() - started
    duration = len(audio) / float(rate)

    print(f"Текст: «{text}»")
    print(f"Время: {elapsed:.2f} с на {duration:.1f} с аудио "
          f"(RTFx ≈ {duration / elapsed:.1f})" if elapsed > 0 else "")
    return 0


if __name__ == "__main__":                             # pragma: no cover
    sys.exit(main())
