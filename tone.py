# -*- coding: utf-8 -*-
"""Тонкая правка тембра синтезированной речи.

Зачем: Piper отдаёт голос как есть, а «характер» голоса — это не только сама
модель. У «Мари» тихий и глухой тембр (спектральный центроид 652 Гц против
1387 у «Совы»), из-за чего она звучит вяло и «из-за стены». Поднять темп и
громкость мало: нужна яркость в области присутствия и плотность — чтобы
фраза звучала собранно, а не расслабленно.

Здесь три фильтра из кулинарной книги Роберта Бристоу-Джонсона (RBJ) —
низкая полка, пик и высокая полка — и компрессор, который выравнивает
громкость слогов. Никаких новых зависимостей: только numpy, который уже
нужен для Piper.

Обработка идёт по готовому файлу, а не по потоку, поэтому её можно
проверить замером: сравнить полосы спектра до и после.
"""

from __future__ import annotations

import math
import wave
from pathlib import Path

try:
    import numpy as np
except ImportError:  # без numpy работает только «без обработки»
    np = None

#: Потолок уровня после обработки: запас на межсэмпловые пики при
#: воспроизведении. Обычно до него не доходит — громкость сохраняется по RMS.
_CEILING = 0.99

#: Готовые характеры. Числа подобраны замерами на «Мари» (см. README):
#: центроид 616 → 1475 Гц, наклон «присутствие/низы» −12.4 → −5.6 дБ.
#: ``soft`` — тот же подъём, но мягче и без компрессора: вариант «подобрее».
PROFILES = {
    "off": {},
    "soft": {
        "low_shelf": (-3.0, 180.0),
        "presence": (3.0, 2500.0, 0.9),
        "high_shelf": (2.5, 6000.0),
    },
    "confident": {
        "low_shelf": (-4.0, 200.0),
        "presence": (4.0, 3000.0, 1.0),
        "high_shelf": (3.5, 6000.0),
        "compress": (-19.0, 2.2, 8.0, 130.0),
    },
    "brisk": {
        # «Бодрее» — это не только быстрее. Первый вариант (тон ×1.10,
        # компрессор −20 дБ 2.6:1) всё равно читался как «побитый»: голос
        # оставался низким (206 Гц) и ровным — компрессор такой степени
        # выравнивает слоги до одеяла, а вялость слышится именно в отсутствии
        # перепадов. Теперь тон ×1.18 (206 → 223 Гц), низы срезаны сильнее
        # (−6 дБ @ 230 Гц — уходит «вата»), присутствие +5.5 дБ @ 3.5 кГц,
        # а компрессор мягче (−16 дБ 1.8:1): тихие слоги подтягиваются, но
        # фраза дышит. Разборчивость по Vosk не изменилась (85 → 82% в
        # пределах разброса малой модели), на пределе 0% сэмплов.
        "pitch": 1.18,
        "low_shelf": (-6.0, 230.0),
        "presence": (5.5, 3500.0, 1.0),
        "high_shelf": (4.5, 6500.0),
        "compress": (-16.0, 1.8, 4.0, 90.0),
    },
}

#: Что делать голосу без записи в каталоге.
DEFAULT_PROFILE = "off"


def get(name: str | None) -> dict:
    """Профиль по имени. Неизвестное имя — пустой профиль, а не исключение."""
    return PROFILES.get(str(name or "").strip().lower(), {})


def signature(profile: dict | None) -> str:
    """Короткая подпись профиля — чтобы понять, что кэш устарел."""
    if not profile:
        return "off"
    parts = []
    if profile.get("pitch"):
        parts.append(f"pitch:{float(profile['pitch']):g}")
    for key in ("low_shelf", "presence", "high_shelf", "compress"):
        value = profile.get(key)
        if value:
            parts.append(key + ":" + ",".join(f"{float(item):g}" for item in value))
    return ";".join(parts) or "off"


def _coeffs(kind: str, rate: int, freq: float, gain_db: float, q: float = 0.707):
    """Коэффициенты биквада (b0, b1, b2, a1, a2) — уже нормированные на a0."""
    a = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * freq / rate
    cos_w0, sin_w0 = math.cos(w0), math.sin(w0)

    if kind == "low_shelf":
        # Крутизна полки S = 1, поэтому (1/S - 1) = 0 и alpha = sin/2 * sqrt(2).
        alpha = sin_w0 / 2.0 * math.sqrt(2.0)
        b0 = a * ((a + 1) - (a - 1) * cos_w0 + 2 * math.sqrt(a) * alpha)
        b1 = 2 * a * ((a - 1) - (a + 1) * cos_w0)
        b2 = a * ((a + 1) - (a - 1) * cos_w0 - 2 * math.sqrt(a) * alpha)
        a0 = (a + 1) + (a - 1) * cos_w0 + 2 * math.sqrt(a) * alpha
        a1 = -2 * ((a - 1) + (a + 1) * cos_w0)
        a2 = (a + 1) + (a - 1) * cos_w0 - 2 * math.sqrt(a) * alpha
    elif kind == "high_shelf":
        # Крутизна полки S = 1, поэтому (1/S - 1) = 0 и alpha = sin/2 * sqrt(2).
        alpha = sin_w0 / 2.0 * math.sqrt(2.0)
        b0 = a * ((a + 1) + (a - 1) * cos_w0 + 2 * math.sqrt(a) * alpha)
        b1 = -2 * a * ((a - 1) + (a + 1) * cos_w0)
        b2 = a * ((a + 1) + (a - 1) * cos_w0 - 2 * math.sqrt(a) * alpha)
        a0 = (a + 1) - (a - 1) * cos_w0 + 2 * math.sqrt(a) * alpha
        a1 = 2 * ((a - 1) - (a + 1) * cos_w0)
        a2 = (a + 1) - (a - 1) * cos_w0 - 2 * math.sqrt(a) * alpha
    elif kind == "peaking":
        alpha = sin_w0 / (2.0 * max(q, 0.05))
        b0 = 1 + alpha * a
        b1 = -2 * cos_w0
        b2 = 1 - alpha * a
        a0 = 1 + alpha / a
        a1 = -2 * cos_w0
        a2 = 1 - alpha / a
    else:
        raise ValueError(f"неизвестный фильтр: {kind}")

    return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0


def _run_biquad(samples: np.ndarray, coeffs) -> np.ndarray:
    """Один биквад, прямая форма II.

    Фильтр рекурсивный, поэтому цикл по сэмплам неизбежен — но идёт он по
    обычному списку: обращение к элементам numpy-массива из Python примерно
    втрое дороже (на трёх фильтрах это давало ~130 мс на фразу). Числа те же:
    ``tolist()`` отдаёт точные Python-float, dtype результата сохраняется.
    """
    b0, b1, b2, a1, a2 = coeffs
    z1 = z2 = 0.0
    out = []
    append = out.append
    for x in samples.tolist():
        y = b0 * x + z1
        z1 = b1 * x - a1 * y + z2
        z2 = b2 * x - a2 * y
        append(y)
    return np.array(out, dtype=samples.dtype)


def _envelope(samples: np.ndarray, rate: int,
              attack_ms: float, release_ms: float) -> np.ndarray:
    """Огибающая уровня: быстро вверх (attack), медленно вниз (release).

    Асимметрия обязательна — симметричное сглаживание «съедало» бы начало
    слогов и возвращало ту самую вялость, от которой избавляемся.
    """
    attack = math.exp(-1.0 / max(rate * attack_ms / 1000.0, 1.0))
    release = math.exp(-1.0 / max(rate * release_ms / 1000.0, 1.0))
    # Список вместо numpy-массива — по той же причине, что и в _run_biquad:
    # огибающая считается по каждому сэмплу в Python.
    out = []
    append = out.append
    level = 0.0
    for value in samples.tolist():
        if value < 0:
            value = -value
        coef = attack if value > level else release
        level = coef * level + (1.0 - coef) * value
        append(level)
    return np.array(out, dtype=samples.dtype)


#: Насколько компрессор смотрит вперёд. Больше — точнее держит пики, но
#: срезает начало слова: первый согласный попадает под уже опущенное усиление,
#: и «пробки» слышится как «на садовом». 2 мс — компромисс, проверенный WER.
_LOOKAHEAD_MS = 2.0


def _lookahead(env, window: int):
    """Смотрит вперёд: значение в точке — максимум по следующим ``window``.

    Без этого первый всплеск фразы проходит нетронутым (огибающая ещё не
    успела вырасти) и после выравнивания громкости упирается в предел.
    """
    if window <= 1 or env.size <= 1:
        return env
    out = env.copy()
    shift = 1
    while shift < window:
        out[:-shift] = np.maximum(out[:-shift], env[shift:])
        shift *= 2
    return out


def _compress(samples: np.ndarray, rate: int, params) -> np.ndarray:
    """Компрессор: тихие слоги подтягиваются, громкие осаживаются.

    Именно он даёт «собранность»: без него фраза звучит ровно так же громко,
    как записана, и любая пауза или проглатывание окончания читается как
    неуверенность. Порог, степень и время — в секундах и децибелах.
    """
    threshold_db, ratio, attack_ms, release_ms = params
    env = _envelope(samples, rate, attack_ms, release_ms)
    env = _lookahead(env, max(1, int(rate * min(attack_ms, _LOOKAHEAD_MS) / 1000.0)))
    level_db = 20.0 * np.log10(np.maximum(env, 1e-6))
    over = level_db - threshold_db
    reduction = np.where(over > 0.0, -over * (1.0 - 1.0 / max(ratio, 1.0)), 0.0)
    return samples * np.power(10.0, reduction / 20.0)


def _resample(samples, factor: float):
    """Меняет высоту голоса пересчётом отсчётов.

    ``factor`` 1.1 — на 10% выше и на 10% короче: прореживание ускоряет всё,
    включая форманты, поэтому голос звучит не «быстрее», а именно «выше».
    Длительность возвращает вызывающая сторона (``length_scale`` в синтезе),
    так что темп и высота настраиваются независимо.

    Интерполяция линейная: при 22 кГц и коэффициенте до 1.25 разница с
    кубической на слух не ловится, а лишнего кода не требует.
    """
    if not factor or abs(factor - 1.0) < 1e-3 or samples.size < 2:
        return samples
    count = max(2, int(round(samples.size / factor)))
    positions = np.linspace(0.0, samples.size - 1.0, count)
    return np.interp(positions, np.arange(samples.size, dtype=np.float64), samples)


def apply(samples, rate: int, profile: dict | None):
    """Применяет профиль тона к массиву float (-1..1). Без профиля — как есть."""
    if not profile:
        return samples
    if np is None:
        raise RuntimeError("для обработки тембра нужен numpy")
    result = samples.astype(np.float64, copy=True)
    # Высоту меняем первым делом: после пересчёта отсчётов фильтры настраиваются
    # на тот же номинальный rate, и полосы остаются там, где задумано.
    result = _resample(result, float(profile.get("pitch", 1.0) or 1.0))
    low = profile.get("low_shelf")
    if low:
        gain, freq = low
        result = _run_biquad(result, _coeffs("low_shelf", rate, freq, gain))
    peak = profile.get("presence")
    if peak:
        gain, freq, q = peak
        result = _run_biquad(result, _coeffs("peaking", rate, freq, gain, q))
    high = profile.get("high_shelf")
    if high:
        gain, freq = high
        result = _run_biquad(result, _coeffs("high_shelf", rate, freq, gain))
    compress = profile.get("compress")
    if compress:
        result = _compress(result, rate, compress)
    return result


def apply_to_wav(path: Path, profile: dict | None) -> bool:
    """Эквалайзит готовый WAV на месте. True — файл изменён.

    Громкость сохраняется по RMS: она уже выставлена синтезом
    (``PIPER_VOLUME`` × поправка голоса), и обработка не должна её менять —
    иначе настройка громкости молча перестала бы работать, а «стало громче»
    приписывалось бы тембру. Компрессор при этом делает своё дело: тихие
    слоги подтягиваются, громкие осаживаются, средний уровень остаётся тем же.

    Пик нормируется только как страховка: если после подъёма высоких сигнал
    упёрся бы в предел, уровень слегка опускается — лучше тише, чем с хрипом.
    """
    if not profile:
        return False
    if np is None:
        return False
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if width != 2 or channels != 1:
        return False

    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float64) / 32768.0
    if audio.size == 0:
        return False

    source_rms = float(np.sqrt(np.mean(audio ** 2)))
    shaped = apply(audio, rate, profile)
    shaped_rms = float(np.sqrt(np.mean(shaped ** 2)))
    if shaped_rms > 0 and source_rms > 0:
        scale = source_rms / shaped_rms
        peak = float(np.max(np.abs(shaped))) * scale
        if peak > _CEILING:
            scale *= _CEILING / peak
        shaped = shaped * scale
    shaped = np.clip(shaped, -1.0, 1.0)

    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((shaped * 32767.0).astype(np.int16).tobytes())
    return True


def describe(profile: dict | None) -> str:
    """Человекочитаемое описание профиля — для voice.py list."""
    if not profile:
        return "без обработки"
    parts = []
    if profile.get("pitch"):
        parts.append(f"тон ×{float(profile['pitch']):.2f}")
    if profile.get("low_shelf"):
        gain, freq = profile["low_shelf"]
        parts.append(f"низы {gain:+.1f} дБ @ {freq:.0f} Гц")
    if profile.get("presence"):
        gain, freq, _ = profile["presence"]
        parts.append(f"присутствие {gain:+.1f} дБ @ {freq / 1000:.1f} кГц")
    if profile.get("high_shelf"):
        gain, freq = profile["high_shelf"]
        parts.append(f"верх {gain:+.1f} дБ @ {freq / 1000:.1f} кГц")
    if profile.get("compress"):
        threshold, ratio, _, _ = profile["compress"]
        parts.append(f"компрессор {threshold:.0f} дБ {ratio:.1f}:1")
    return ", ".join(parts) or "без обработки"
