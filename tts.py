# -*- coding: utf-8 -*-
"""Синтез речи Пятницы.

Основной движок — **Piper**: локальная нейросеть, работает офлайн и не зависит
от доступности серверов Microsoft. Цепочка (первый доступный побеждает):

1. ``piper`` — локальная нейросеть, офлайн (модель скачивается при первом запуске);
2. ``edge`` — нейронные голоса Microsoft (резерв, нужен интернет);
3. ``sapi`` — системный голос Windows через pyttsx3 (работает всегда).

Все фразы проговариваются в отдельном потоке через очередь, поэтому основной
цикл ассистента не блокируется.

Отмена (``stop``) — это «эпоха»: ``stop`` увеличивает ``_generation``, а фразы
запоминают эпоху в момент постановки в очередь. Всё, что было поставлено раньше,
пропускается, а активная фраза получает персональный ``threading.Event`` и
прекращает работу (синтез → без проигрывания, воспроизведение → глушится).
``say`` эпоху НЕ трогает — иначе каждая новая фраза обнуляла бы уже стоящие в
очереди.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import queue
import threading
import time
import wave
from pathlib import Path

import config
import tone
import voices
from events import emit_level, emit_message, emit_state
from utils import log

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

try:
    import av
    HAS_AV = True
except ImportError:
    HAS_AV = False


class _SpeechCancelled(Exception):
    """Фраза отменена (``stop``): движок прекращает работу без проигрывания.

    Это не ошибка — исключение поднимается наверх и говорит ``_speak`` «не
    пробуй следующий движок»: отменённую фразу не должен озвучить резерв.
    """


# =====================================================================
#  ВОСПРОИЗВЕДЕНИЕ АУДИОФАЙЛА
# =====================================================================
# Один и тот же псевдоним MCI на весь процесс: так команда stop может добраться
# до играющего файла из другого потока (у MCI нет объектов, только строки).
_MCI_ALIAS = "pyatnitsa_tts_play"


def _decode_with_pyav(path: Path, target_rate: int = 24000):
    """Декодирует mp3/wav в моно int16 через PyAV. Возвращает (numpy, rate)."""
    container = av.open(str(path))
    try:
        stream = container.streams.audio[0]
        resampler = av.audio.resampler.AudioResampler(
            format="s16", layout="mono", rate=target_rate
        )
        chunks = []
        for frame in container.decode(stream):
            resampled = resampler.resample(frame)
            if resampled is None:
                continue
            frames = resampled if isinstance(resampled, list) else [resampled]
            for item in frames:
                chunks.append(item.to_ndarray())
        # Сбрасываем буфер ресемплера.
        tail = resampler.resample(None)
        if tail:
            for item in (tail if isinstance(tail, list) else [tail]):
                chunks.append(item.to_ndarray())
        if not chunks:
            raise RuntimeError("PyAV не декодировал ни одного кадра")
        audio = np.concatenate(chunks, axis=1).reshape(-1)
        return audio.astype(np.int16), target_rate
    finally:
        container.close()


def _decode_wav_native(path: Path):
    """Читает PCM WAV без ресемплинга — родная частота Piper (22050 Гц).

    PyAV всегда приводит звук к 24 кГц (это нужно для mp3 edge-tts). Для WAV
    пересэмплирование только тратит время и слегка портит голос, поэтому WAV
    читаем напрямую стандартным ``wave``.
    """
    with wave.open(str(path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        width = wav_file.getsampwidth()
        rate = wav_file.getframerate()
        frames = wav_file.readframes(wav_file.getnframes())
    if width != 2:
        raise RuntimeError(f"ожидался 16-битный PCM, получено {width * 8} бит")
    audio = np.frombuffer(frames, dtype=np.int16)
    if channels > 1:
        audio = audio.reshape(-1, channels)[:, 0]
    if audio.size == 0:
        raise RuntimeError("файл не содержит сэмплов")
    return audio, rate


def _play_with_mci(path: Path, cancel: threading.Event | None = None) -> None:
    """Проигрывает файл средствами Windows (MCI). Работает без зависимостей.

    Раньше здесь стоял ``play ... wait``: вызов блокировал поток и его нельзя
    было прервать. Теперь играем асинхронно и опрашиваем ``status mode``, чтобы
    отмена доходила и до этого пути.
    """
    winmm = ctypes.windll.winmm
    alias = _MCI_ALIAS
    winmm.mciSendStringW(f"close {alias}", None, 0, None)

    def send(cmd: str) -> int:
        return winmm.mciSendStringW(cmd, None, 0, None)

    err = send(f'open "{path}" alias {alias}')
    if err:
        err = send(f'open "{path}" type mpegvideo alias {alias}')
    if err:
        raise RuntimeError(f"MCI не смог открыть файл (код {err})")
    try:
        err = send(f"play {alias}")
        if err:
            raise RuntimeError(f"MCI не смог воспроизвести (код {err})")
        mode = ctypes.create_unicode_buffer(64)
        while True:
            if cancel is not None and cancel.is_set():
                send(f"stop {alias}")
                raise _SpeechCancelled
            winmm.mciSendStringW(f"status {alias} mode", mode, 64, None)
            if mode.value.strip().lower() not in ("playing", "seeking"):
                break
            time.sleep(0.05)
    finally:
        send(f"close {alias}")


def stop_playback() -> None:
    """Немедленно глушит любой активный вывод звука (sounddevice/MCI/winsound).

    Вызывается из ``Speaker.stop()`` (то есть из чужого потока), поэтому не
    трогает COM/SAPI — тот останавливает сам себя в своём рабочем потоке.
    """
    if HAS_SD:
        try:
            sd.stop()
        except Exception:  # noqa: BLE001
            pass
    try:
        ctypes.windll.winmm.mciSendStringW(f"stop {_MCI_ALIAS}", None, 0, None)
    except Exception:  # noqa: BLE001
        pass
    try:
        import winsound
        winsound.PlaySound(None, 0)  # None глушит текущий поток звука
    except Exception:  # noqa: BLE001
        pass


def _level_monitor(audio, rate: int, stop: threading.Event, on_level) -> None:
    """Сообщает громкость уже прозвучавшего куска — «пульс» для интерфейса.

    Считаем RMS по таймеру, а не в аудиоколбэке: так не нужно переписывать
    воспроизведение и остаётся совместимость с любым способом вывода звука.
    """
    window = max(1, int(rate * 0.04))
    # И RMS, и пик берём в одних единицах (int16) — иначе деление даёт нули.
    peak = float(np.max(np.abs(audio.astype(np.float32)))) or 1.0
    started = time.monotonic()
    while not stop.is_set():
        index = int((time.monotonic() - started) * rate)
        if index >= len(audio):
            break
        chunk = audio[index:index + window].astype(np.float32)
        if chunk.size:
            value = float(np.sqrt(float(np.mean(chunk ** 2))))
            # Нормируем на пик фразы: тихий голос должен так же «дышать» ядром.
            on_level(min(1.0, value / peak * 1.15))
        stop.wait(0.033)


def _play_decoded(audio, rate: int, on_level=None,
                  cancel: threading.Event | None = None) -> None:
    """Проигрывает массив через sounddevice, попутно сообщая громкость."""
    if cancel is not None and cancel.is_set():
        raise _SpeechCancelled
    stop = threading.Event()
    if callable(on_level) and HAS_NUMPY and len(audio):
        threading.Thread(target=_level_monitor, args=(audio, rate, stop, on_level),
                         name="tts-level", daemon=True).start()
    try:
        sd.play(audio, rate)
        # Закрываем окно гонки: stop() мог проглушить звук между проверкой и
        # запуском — тогда глушим повторно уже после старта.
        if cancel is not None and cancel.is_set():
            try:
                sd.stop()
            except Exception:  # noqa: BLE001
                pass
            raise _SpeechCancelled
        sd.wait()
    finally:
        stop.set()
        if callable(on_level):
            on_level(0.0)


def play_audio_file(path: Path, on_level=None,
                    cancel: threading.Event | None = None) -> None:
    """Проигрывает аудиофайл, перебирая доступные способы.

    ``on_level`` — необязательный колбэк(0..1) с текущей громкостью фразы:
    по нему интерфейс заставляет ядро пульсировать в такт речи.
    ``cancel`` — событие отмены: как только оно взведено, ни один способ вывода
    не должен начать (или продолжить) воспроизведение отменённой фразы.
    """
    def cancelled() -> bool:
        return cancel is not None and cancel.is_set()

    errors = []
    # WAV (Piper) играем в родной частоте: без декодера и без ресемплинга.
    if path.suffix.lower() == ".wav" and HAS_NUMPY and HAS_SD:
        try:
            if cancelled():
                raise _SpeechCancelled
            audio, rate = _decode_wav_native(path)
            if cancelled():
                raise _SpeechCancelled
            _play_decoded(audio, rate, on_level, cancel)
            return
        except _SpeechCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"wav: {exc}")
    if HAS_AV and HAS_SD:
        try:
            if cancelled():
                raise _SpeechCancelled
            audio, rate = _decode_with_pyav(path)
            if cancelled():
                raise _SpeechCancelled
            _play_decoded(audio, rate, on_level, cancel)
            return
        except _SpeechCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"pyav: {exc}")
    if cancelled():
        raise _SpeechCancelled
    if path.suffix.lower() == ".wav":
        try:
            import winsound
            winsound.PlaySound(str(path), winsound.SND_FILENAME)
            return
        except Exception as exc:  # noqa: BLE001
            errors.append(f"winsound: {exc}")
    if cancelled():
        raise _SpeechCancelled
    try:
        _play_with_mci(path, cancel)
        return
    except _SpeechCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        errors.append(f"mci: {exc}")
    raise RuntimeError("не удалось воспроизвести звук (" + "; ".join(errors) + ")")


# =====================================================================
#  ГЕНЕРАЦИЯ АУДИО
# =====================================================================
def _cache_key(*parts: str) -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:20]
    return digest


def tone_generation() -> str:
    """Подпись текущей обработки тембра: имя профиля + его числа.

    Нужна, чтобы отличить «кэш от прошлого профиля» от «кэш от текущего».
    """
    return voices.tone_name() + "/" + tone.signature(voices.tone_profile())


def ensure_tone_cache(cache_dir: Path | None = None) -> bool:
    """Сбрасывает кэш WAV, если профиль тембра сменился. True — сбрасывал.

    Почему не через ключ кэша: ключ повторяет настольное окно
    (``desktop/src/main/ttsAudio.ts``), а оно о профилях не знает. Добавить
    туда ещё одно поле значило бы держать в синхроне уже не только строку
    ключа, но и таблицу профилей. Вместо этого рядом с кэшем лежит подпись
    профиля: сменилась — старые файлы удаляются, и голос не звучит по-старому
    до истечения срока жизни кэша.
    """
    directory = Path(cache_dir or config.CACHE_DIR)
    current = tone_generation()
    marker = directory / ".piper-tone"
    try:
        if marker.read_text(encoding="utf-8").strip() == current:
            return False
    except OSError:
        pass

    removed = 0
    try:
        for path in directory.iterdir():
            if path.is_file() and path.suffix.lower() == ".wav":
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
        marker.write_text(current, encoding="utf-8")
    except OSError as exc:
        log(f"Не удалось обновить кэш тембра: {exc}", "debug")
        return False
    if removed:
        log(f"Профиль тембра изменился — кэш озвучки очищен ({removed} файлов).",
            "info")
    return True


def prune_tts_cache(cache_dir: Path | None = None, *, now: float | None = None) -> None:
    """Удаляет устаревшие и самые старые файлы кэша TTS.

    Кэш — ускорение, а не архив: ограничение по возрасту и количеству файлов
    предотвращает бесконечный рост данных при долгой работе ассистента.
    """
    directory = Path(cache_dir or config.CACHE_DIR)
    if not directory.exists():
        return
    current = time.time() if now is None else float(now)
    ttl_days = max(0.0, float(getattr(config, "TTS_CACHE_TTL_DAYS", 0) or 0))
    max_files = max(0, int(getattr(config, "TTS_CACHE_MAX_FILES", 0) or 0))
    files = [path for path in directory.iterdir()
             if path.is_file() and path.suffix.lower() in {".mp3", ".wav"}]
    if ttl_days:
        deadline = current - ttl_days * 86400
        for path in files[:]:
            try:
                if path.stat().st_mtime < deadline:
                    path.unlink()
                    files.remove(path)
            except OSError:
                continue
    if max_files and len(files) > max_files:
        try:
            oldest_first = sorted(files, key=lambda path: path.stat().st_mtime)
        except OSError:
            return
        for path in oldest_first[:len(files) - max_files]:
            try:
                path.unlink()
            except OSError:
                continue


def _edge_synthesize(text: str, out_path: Path) -> None:
    """Синтез через edge-tts (асинхронный API внутри синхронной обёртки)."""
    import edge_tts

    async def _run():
        communicate = edge_tts.Communicate(
            text,
            config.TTS_VOICE,
            rate=config.TTS_RATE,
            volume=config.TTS_VOLUME,
            pitch=config.TTS_PITCH,
        )
        await communicate.save(str(out_path))

    asyncio.run(_run())


def list_edge_voices(language_filter: str = "ru") -> list:
    """Список доступных голосов edge-tts (для флага --list-voices)."""
    import edge_tts

    async def _run():
        return await edge_tts.list_voices()

    voices = asyncio.run(_run())
    result = []
    for voice in voices:
        locale = voice.get("Locale", "")
        if language_filter and not locale.lower().startswith(language_filter.lower()):
            continue
        result.append({
            "name": voice.get("ShortName", ""),
            "gender": voice.get("Gender", ""),
            "locale": locale,
        })
    return sorted(result, key=lambda v: v["name"])


# =====================================================================
#  ГОЛОС PIPER (локальная модель)
# =====================================================================
def piper_model_path() -> Path:
    """Файл модели активного голоса.

    Каталог голосов и приоритеты выбора живут в ``voices.py``: там же описано,
    почему имя голоса важнее явного пути и как угадывается голос по старым
    настройкам (когда ``PIPER_MODEL`` ещё указывал на конкретный файл).
    """
    return voices.active_path()


def piper_model_ready(model_path: Path | None = None) -> bool:
    """Есть ли на диске модель Piper вместе с её конфигом."""
    model = Path(model_path) if model_path else piper_model_path()
    try:
        return (model.exists() and model.stat().st_size > 1_000_000
                and Path(str(model) + ".json").exists())
    except OSError:
        return False


def ensure_piper_model(model_path: Path | None = None) -> Path:
    """Возвращает модель Piper, при необходимости скачивая её.

    Явный путь (``model_path``) — это своя модель: её не подменяем голосом из
    каталога. Без аргумента работает активный голос, и скачиванием занимается
    ``voices.ensure`` — там же проверяется целостность файлов.
    """
    if model_path is not None:
        model = Path(model_path)
        if piper_model_ready(model):
            return model
        raise FileNotFoundError(f"нет файла модели Piper: {model}")

    started = time.time()
    model = voices.ensure(voices.active_name(),
                          on_log=lambda message: log(message, "info"))
    if not piper_model_ready(model):
        raise RuntimeError("файл модели Piper повреждён (слишком маленький)")
    log(f"Голос Piper готов за {time.time() - started:.1f} с: {model.name}", "info")
    return model


# =====================================================================
#  ПАРАМЕТРЫ ГОЛОСА
# =====================================================================
def piper_speed() -> float:
    """Темп речи Piper (1.0 — обычный).

    Пределы 0.5..2.0 — не вкусовщина: за ними VITS начинает «жевать» звуки
    или растягивать их до неестественности, и разборчивость падает.
    """
    raw = getattr(config, "PIPER_SPEED", 1.0)
    try:
        speed = 1.0 if raw is None else float(raw)
    except (TypeError, ValueError):
        speed = 1.0
    return min(max(speed, 0.5), 2.0)


def piper_volume() -> float:
    """Громкость синтеза 0..1 с поправкой на конкретный голос.

    ``PIPER_VOLUME`` — пользовательская настройка, а ``voices.effective_volume``
    умножает её на поправку голоса: разные модели при одинаковом пике звучат с
    разной громкостью («Сова» на 3.7 дБ громче «Ирины»), и без выравнивания
    смена голоса воспринималась бы как «стало громче».

    Проверка на ``None`` обязательна: ``volume=0.0`` — законное «выключить
    звук», а ``raw or 1.0`` превратил бы его в полную громкость.
    """
    return voices.effective_volume()


def piper_noise() -> tuple:
    """(noise_scale, noise_w_scale) — вариативность тембра и длительности.

    ``None`` — взять значения из модели. Меньше шума — ровнее голос, больше —
    живее, но с шероховатостями.
    """
    def read(name: str):
        raw = getattr(config, name, None)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        try:
            return min(max(float(raw), 0.0), 2.0)
        except (TypeError, ValueError):
            return None

    return read("PIPER_NOISE_SCALE"), read("PIPER_NOISE_W")


# =====================================================================
#  ОРАТОР
# =====================================================================
class Speaker:
    """Очередь фраз + один рабочий поток.

    Инвариант занятости держит счётчик ``_pending`` (сколько фраз поставлено и
    ещё не окончено) под общим ``RLock``: активная фраза остаётся «в работе» с
    момента постановки до конца воспроизведения, поэтому ``busy`` не мигает
    между «взял из очереди» и «начал говорить».
    """

    def __init__(self, enabled: bool = True, engine: str | None = None):
        self.enabled = enabled
        self.requested_engine = engine or config.TTS_ENGINE
        self._queue: "queue.Queue[tuple[int, str, bool]]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = False
        self._closing = False
        # Эпоха отмены: увеличивается ТОЛЬКО в stop()/shutdown().
        self._generation = 0
        self._pending = 0
        self._active: int | None = None
        self._active_cancel: threading.Event | None = None
        self._active_publish = True
        self._lock = threading.RLock()
        self._piper_voice = None
        self._piper_path: str | None = None
        self._piper_lock = threading.Lock()
        self._sapi_engine = None
        self._sapi_lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self.on_spoken = None  # callable(text) -> None
        # Никаких сетевых или аудиоопераций здесь нет — чистка быстрая и нужна
        # только при создании единственного экземпляра Speaker.
        prune_tts_cache()

    # ---------------- публичный интерфейс ----------------
    def start(self) -> None:
        with self._lock:
            if self._running or self._closing:
                return
            self._start_locked()

    def _start_locked(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._worker, name="tts", daemon=True)
        self._thread.start()

    def say(self, text: str, *, block: bool = False, publish: bool = True) -> None:
        """Ставит фразу в очередь. При block=True ждёт окончания озвучки.

        ``publish=False`` подавляет только транскрипт в интерфейсе
        (``emit_message``) — нужен для озвучки правок из веб-UI, чтобы реплика
        не появлялась в диалоге дважды. Эпоху отмены метод не трогает.
        """
        if not text:
            return
        text = " ".join(str(text).split())
        if not text:
            return
        if not self.enabled:
            log(f"{config.ASSISTANT_NAME} (текст): {text}", "info")
            if publish:
                emit_message("assistant", text)
            return
        with self._lock:
            if self._closing:
                log("Озвучка выключена — фраза не поставлена в очередь.", "debug")
                return
            if not self._running:
                self._start_locked()
            self._queue.put((self._generation, text, publish))
            self._pending += 1
            self._idle.clear()
        emit_state("speaking")
        if block:
            self.wait_until_done()

    @property
    def busy(self) -> bool:
        """True, пока звучит фраза или в очереди есть непроизнесённые."""
        with self._lock:
            return self._pending > 0

    def wait_until_done(self, timeout: float | None = None) -> None:
        self._idle.wait(timeout)

    def stop(self) -> None:
        """Прерывает текущую фразу и очищает очередь.

        Увеличивает эпоху отмены (единственное место, где это происходит), из-за
        чего всё, что было поставлено раньше, будет пропущено. Активной фразе
        взводится персональное событие — синтез прекратится без проигрывания, а
        воспроизведение глушится. ``busy`` остаётся True, пока рабочий поток не
        подтвердит остановку активной фразы.
        """
        with self._lock:
            self._generation += 1
            cancel = self._active_cancel
            drained = 0
            while True:
                try:
                    self._queue.get_nowait()
                    drained += 1
                except queue.Empty:
                    break
            self._pending = max(0, self._pending - drained)
            self._maybe_idle_locked()
        if cancel is not None:
            cancel.set()
        stop_playback()
        emit_level(0.0)

    def shutdown(self, timeout: float = 10.0) -> None:
        """Дожидается очереди (не дольше ``timeout``) и останавливает поток.

        Прощальная фраза по умолчанию НЕ выбрасывается: сначала даём очереди
        доиграть, и только если она не успела — глушим. После закрытия новые
        фразы и повторный ``start`` игнорируются.
        """
        with self._lock:
            first = not self._closing
            self._closing = True
        if first:
            deadline = time.monotonic() + max(0.0, float(timeout))
            while True:
                with self._lock:
                    pending = self._pending > 0
                if not pending:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log("Озвучка не успела завершиться — прерываю.", "warn")
                    break
                self._idle.wait(min(0.2, remaining))
        self._finish_worker()

    def _finish_worker(self) -> None:
        with self._lock:
            self._running = False
        self.stop()
        try:
            self._queue.put_nowait((-1, "", True))
        except Exception:  # noqa: BLE001
            pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    # ---------------- внутренняя логика ----------------
    def _maybe_idle_locked(self) -> None:
        """Переводит оратора в «простой», если очередь и активная фраза пусты."""
        if self._pending <= 0 and self._active is None:
            self._idle.set()
            emit_state("idle")

    def _worker(self) -> None:
        while True:
            try:
                generation, text, publish = self._queue.get(timeout=0.2)
            except queue.Empty:
                with self._lock:
                    if self._closing:
                        break
                continue
            if text == "" and generation == -1:
                break
            cancel = threading.Event()
            with self._lock:
                if generation < self._generation:
                    # Фразу отменили до начала — тихо пропускаем.
                    self._pending = max(0, self._pending - 1)
                    self._maybe_idle_locked()
                    continue
                self._active = generation
                self._active_cancel = cancel
                self._active_publish = publish
                self._idle.clear()
            if publish:
                emit_message("assistant", text)
            log(f"{config.ASSISTANT_NAME}: {text}", "info")
            try:
                spoken = self._speak(text, cancel)
                if spoken and not cancel.is_set() and callable(self.on_spoken):
                    self.on_spoken(text)
            except _SpeechCancelled:
                pass
            except Exception as exc:  # noqa: BLE001
                log(f"Ошибка озвучки: {exc}", "error")
            finally:
                with self._lock:
                    self._active = None
                    self._active_cancel = None
                    self._active_publish = True
                    self._pending = max(0, self._pending - 1)
                    self._maybe_idle_locked()
                emit_level(0.0)

    def _engine_order(self) -> list:
        order = []
        if self.requested_engine == "auto":
            # Piper первым: он офлайн и не зависит от доступности edge-tts.
            if config.FEATURES["piper"]:
                order.append("piper")
            if config.FEATURES["edge_tts"]:
                order.append("edge")
            if config.FEATURES["pyttsx3"]:
                order.append("sapi")
        else:
            order.append(self.requested_engine)
            for fallback in ("piper", "edge", "sapi"):
                if fallback != self.requested_engine:
                    order.append(fallback)
        return order

    def warmup(self) -> None:
        """Заранее поднимает Piper в фоне, чтобы первая фраза не ждала загрузку."""
        if not self.enabled:
            return
        order = self._engine_order()
        if not order or order[0] != "piper":
            return
        threading.Thread(target=self._warmup_piper, name="tts-warmup",
                         daemon=True).start()

    def _warmup_piper(self) -> None:
        try:
            self._load_piper_voice()
        except Exception as exc:  # noqa: BLE001
            log(f"Прогрев Piper не удался: {exc}", "debug")

    def _speak(self, text: str, cancel: threading.Event | None = None) -> bool:
        """Проговаривает фразу, перебирая движки. True — фраза прозвучала.

        Отмена не «перетекает» в резервный движок: если фразу отменили, метод
        возвращает False и больше ничего не пробует.
        """
        for engine in self._engine_order():
            if cancel is not None and cancel.is_set():
                return False
            try:
                if engine == "edge":
                    self._speak_edge(text, cancel)
                elif engine == "piper":
                    self._speak_piper(text, cancel)
                elif engine == "sapi":
                    self._speak_sapi(text, cancel)
                else:
                    continue
                return True
            except _SpeechCancelled:
                return False
            except Exception as exc:  # noqa: BLE001
                log(f"[{engine}] не смог озвучить: {exc}", "warn")
        return False

    # ----- edge-tts -----
    def _speak_edge(self, text: str, cancel: threading.Event | None = None) -> None:
        if not config.FEATURES["edge_tts"]:
            raise RuntimeError("edge-tts не установлен")
        if cancel is not None and cancel.is_set():
            raise _SpeechCancelled
        key = _cache_key("edge", text, config.TTS_VOICE, config.TTS_RATE,
                         config.TTS_VOLUME, config.TTS_PITCH)
        out = config.CACHE_DIR / f"{key}.mp3"
        if not (config.TTS_USE_CACHE and out.exists() and out.stat().st_size > 0):
            # Через временный файл: оборванная загрузка не должна осесть в кэше
            # и потом «звучать» тишиной.
            partial = out.with_suffix(".part.mp3")
            try:
                _edge_synthesize(text, partial)
                # Синтез мог закончиться уже после stop() — не проигрываем.
                if cancel is not None and cancel.is_set():
                    raise _SpeechCancelled
                if not partial.exists() or partial.stat().st_size == 0:
                    raise RuntimeError("edge-tts вернул пустой файл")
                partial.replace(out)
            finally:
                partial.unlink(missing_ok=True)
        if cancel is not None and cancel.is_set():
            raise _SpeechCancelled
        play_audio_file(out, self._emit_level, cancel)

    # ----- piper -----
    def _load_piper_voice(self):
        """Загружает модель Piper (потокобезопасно) и следит за сменой голоса.

        Модель запоминается вместе с путём: если голос переключили на ходу
        (``voices.set_active`` меняет ``config.PIPER_VOICE``), старую модель
        нужно выгрузить — иначе Пятница продолжит говорить прежним голосом до
        перезапуска, что выглядит как «переключатель не работает».
        """
        path = piper_model_path()
        if self._piper_voice is not None and self._piper_path == str(path):
            return self._piper_voice
        with self._piper_lock:
            if self._piper_voice is None or self._piper_path != str(path):
                from piper import PiperVoice

                model_path = ensure_piper_model()
                log(f"Загружаю голос Piper: {model_path.name}", "info")
                started = time.time()
                self._piper_voice = PiperVoice.load(str(model_path))
                self._piper_path = str(model_path)
                log(f"Голос Piper готов за {time.time() - started:.1f} с.", "debug")
        return self._piper_voice

    @staticmethod
    def _piper_config():
        """Настройки синтеза Piper.

        В piper-tts скорость задаётся не полем ``speed`` (его там нет), а
        обратным ``length_scale``: 1.2 → быстрее, 0.8 → медленнее. Раньше здесь
        передавался несуществующий ``speed``, исключение глушилось — и настройка
        ``PIPER_SPEED`` молча не работала.

        ``noise_scale`` / ``noise_w_scale`` — вариативность тембра и длительности.
        ``None`` означает «как в модели»: так настройка не ломает голоса, у
        которых свои значения записаны в JSON рядом с моделью.
        """
        from piper.config import SynthesisConfig

        noise_scale, noise_w = piper_noise()
        return SynthesisConfig(
            # Подъём тона укорачивает звук — синтез растягивается на столько же,
            # иначе голос стал бы не выше, а просто быстрее.
            length_scale=voices.length_scale(speed=piper_speed()),
            volume=piper_volume(),
            noise_scale=noise_scale,
            noise_w_scale=noise_w,
        )

    @staticmethod
    def _piper_cache_valid(path: Path) -> bool:
        """Проверяет, что в кэше лежит целый WAV, а не обрывок синтеза."""
        if not config.TTS_USE_CACHE:
            return False
        try:
            if not path.exists() or path.stat().st_size <= 44:
                return False
            with wave.open(str(path), "rb") as wav_file:
                return wav_file.getnframes() > 0 and wav_file.getframerate() > 0
        except (OSError, EOFError, wave.Error):
            return False

    def _speak_piper(self, text: str, cancel: threading.Event | None = None) -> None:
        if cancel is not None and cancel.is_set():
            raise _SpeechCancelled
        voice = self._load_piper_voice()

        model_path = piper_model_path()
        # ВАЖНО: состав ключа повторяет Electron (desktop/src/main/ttsAudio.ts) —
        # по нему оверлей находит готовый WAV и рисует настоящий спектр. Меняя
        # порядок или добавляя поля, обновите и его, иначе визуализатор молча
        # переключится на уровни из SSE. Смена голоса меняет путь к модели, а
        # значит и ключ: старый кэш чужого голоса не зазвучит. Профиль тембра
        # сюда не входит — вместо него tts.ensure_tone_cache() сбрасывает кэш
        # при смене профиля.
        key = _cache_key("piper", text, str(model_path),
                         str(config.PIPER_SPEED), str(config.PIPER_VOLUME))
        out = config.CACHE_DIR / f"{key}.wav"

        if not self._piper_cache_valid(out):
            # Проверяем подпись профиля до синтеза: если тембр сменили, старый
            # кэш уже удалён и эта фраза соберётся заново. Чтение одного
            # маленького файла на новую фразу — дешевле, чем неверное звучание.
            ensure_tone_cache()
            # Синтезируем во временный файл: недописанный WAV не должен попасть
            # в кэш и «озвучиваться» при следующем запросе той же фразы.
            partial = out.with_suffix(".part.wav")
            try:
                with wave.open(str(partial), "wb") as wav_file:
                    voice.synthesize_wav(text, wav_file, syn_config=self._piper_config())
                if cancel is not None and cancel.is_set():
                    raise _SpeechCancelled
                if not self._piper_cache_valid(partial):
                    raise RuntimeError("Piper вернул пустой файл")
                # Тембр правим до попадания в кэш: иначе каждая фраза
                # обрабатывалась бы заново при каждом повторе.
                tone.apply_to_wav(partial, voices.tone_profile())
                partial.replace(out)
            finally:
                partial.unlink(missing_ok=True)

        if cancel is not None and cancel.is_set():
            raise _SpeechCancelled
        play_audio_file(out, self._emit_level, cancel)

    # ----- общий «пульс» для интерфейса -----
    @staticmethod
    def _emit_level(value: float) -> None:
        """Громкость текущей фразы — по ней пульсирует ядро интерфейса.

        Раньше путь piper вызывал ``self._on_level``, которого у класса нет:
        движок молча падал с AttributeError и уходил на SAPI. Теперь это один
        общий метод, и он же подключён к основному движку edge-tts.
        """
        emit_level(value, "tts")

    # ----- pyttsx3 / SAPI -----
    @staticmethod
    def _sapi_word_pulse(*args, **kwargs) -> None:
        """SAPI сообщает о начале слова — единственный сигнал речи, что он даёт.

        Формат события ``started-word(name, location, length)``; длинное слово
        даёт более яркую вспышку. Интерфейс сглаживает эти импульсы сам.
        ``pyttsx3`` вызывает подписчиков именованными аргументами, поэтому
        ``length`` ищем и в kwargs, и в позиционных — иначе пульс молча падал.
        """
        length = kwargs.get("length")
        if not isinstance(length, int) or length <= 0:
            length = 4
            for value in reversed(args):
                if isinstance(value, int) and value > 0:
                    length = value
                    break
        emit_level(min(1.0, 0.45 + length * 0.06), "tts")

    def _ensure_sapi_engine(self):
        """Создаёт (один раз) движок SAPI и подписывает пульс по словам."""
        if self._sapi_engine is None:
            import pyttsx3
            engine = pyttsx3.init()
            engine.setProperty("rate", config.SAPI_RATE)
            engine.setProperty("volume", config.SAPI_VOLUME)
            for voice in engine.getProperty("voices"):
                if "ru" in (voice.id or "").lower() or "russian" in (voice.name or "").lower():
                    engine.setProperty("voice", voice.id)
                    break
            try:
                engine.connect("started-word", self._sapi_word_pulse)
            except Exception as exc:  # noqa: BLE001
                log(f"Пульс SAPI недоступен: {exc}", "debug")
            self._sapi_engine = engine
        return self._sapi_engine

    def _speak_sapi(self, text: str, cancel: threading.Event | None = None) -> bool:
        """Озвучивает через SAPI во внешнем цикле событий.

        ``runAndWait()`` блокировал поток и не давал отменить фразу. Вместо этого
        качаем COM-сообщения сами (``startLoop(useDriverLoop=False)`` +
        ``iterate()``) прямо в рабочем потоке, поэтому и ``engine.stop()``
        вызывается там же — никакого cross-thread COM. Возвращает False, если
        фраза была отменена.
        """
        with self._sapi_lock:
            engine = self._ensure_sapi_engine()
        if cancel is not None and cancel.is_set():
            return False

        done = threading.Event()

        def _on_finished(name=None, completed=None, **_kwargs):  # noqa: ARG001
            done.set()

        token = None
        try:
            token = engine.connect("finished-utterance", _on_finished)
        except Exception as exc:  # noqa: BLE001
            log(f"SAPI: не удалось подписаться на завершение: {exc}", "debug")

        cancelled = False
        engine.say(text)
        engine.startLoop(useDriverLoop=False)
        # Страховка от зависания: если SAPI не пришлёт finished-utterance
        # (сбой драйвера), поток озвучки не должен застрять навсегда.
        deadline = time.monotonic() + max(15.0, len(text) * 0.45 + 5.0)
        try:
            while not done.is_set():
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    engine.stop()  # тот же рабочий поток — COM безопасен
                    deadline = time.monotonic() + 1.0
                    while not done.is_set() and time.monotonic() < deadline:
                        engine.iterate()
                    break
                if time.monotonic() > deadline:
                    log("SAPI не подтвердил завершение фразы — останавливаю.", "warn")
                    engine.stop()
                    cancelled = True   # фраза не прозвучала целиком
                    break
                engine.iterate()
                time.sleep(0.005)
        finally:
            try:
                engine.endLoop()
            except Exception:  # noqa: BLE001
                pass
            if token is not None:
                try:
                    engine.disconnect(token)
                except Exception:  # noqa: BLE001
                    pass
            emit_level(0.0)
        return not cancelled


# =====================================================================
#  ГЛОБАЛЬНЫЙ ОРАТОР (используется напоминаниями и таймерами)
# =====================================================================
_speaker: Speaker | None = None


def get_speaker() -> Speaker:
    global _speaker
    if _speaker is None:
        _speaker = Speaker(enabled=config.ENABLE_TTS)
    return _speaker


def speak(text: str, *, block: bool = False) -> None:
    """Совместимый с прежним API вызов озвучки."""
    get_speaker().say(text, block=block)
