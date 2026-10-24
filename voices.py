# -*- coding: utf-8 -*-
"""Голоса Пятницы: каталог, установка, переключение, проверка.

Одна точка правды обо всех голосах Piper. Раньше каталог жил в
``download_voices.py``, путь к модели — в ``config.PIPER_MODEL``, а скорость —
в ``config.PIPER_SPEED``, и чтобы сменить голос, нужно было вручную править
JSON с настройками. Теперь достаточно имени::

    config.PIPER_VOICE = "irina"

Из него выводятся путь к модели, адрес для скачивания, имя кэша,
рекомендуемый темп речи и профиль обработки тембра (``tone.py``).

Почему по умолчанию «Сова»: из женских голосов у неё самый высокий и светлый
тембр (216 Гц, разборчивость 96%) — ближе всего к тому «аниме»-звучанию,
которое просили. Официальная «Ирина» ровная, но тёмная и «взрослая».
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import config
import tone

#: Откуда берутся официальные модели (репозиторий голосов Piper).
BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU"

#: Голоса сообщества: тот же формат, но обучены не командой Piper и лежат
#: в отдельном репозитории. Качество проверено (см. заметки у голосов).
COMMUNITY_URL = "https://huggingface.co/rraaww/ru_piper/resolve/main"

#: Голос по умолчанию, если в настройках ничего не указано.
DEFAULT_VOICE = "sova200"

#: Каталог. ``speed`` — рекомендуемый темп: у женских голосов он выше, потому
#: что они говорят заметно медленнее мужских моделей. ``file`` — путь к модели
#: внутри репозитория ``repo`` (без расширения); локально файл всегда
#: называется ``ru_RU-<ключ>-medium.onnx`` — так имя модели выводится из ключа
#: и совпадает с тем, что ожидает настольное окно.
VOICE_CATALOG = {
    "sova200": {
        "title": "Сова",
        "gender": "female",
        "quality": "medium",
        "sample_rate": 22050,
        "speed": 1.0,
        "gain": 0.72,
        "dataset": "~20 мин речи",
        "repo": COMMUNITY_URL,
        "file": "ru_RU-sova200-medium",
        "note": "самый высокий и светлый женский голос: 216 Гц, "
                "разборчивость 96% — ближе всего к «аниме»",
    },
    "irina": {
        "title": "Ирина",
        "gender": "female",
        "quality": "medium",
        "sample_rate": 22050,
        "speed": 1.2,
        "dataset": "RHVoice",
        "file": "irina/medium/ru_RU-irina-medium",
        "note": "официальный женский голос: ровный, но тёмный и «взрослый»",
    },
    "kat580": {
        "title": "Катя",
        "gender": "female",
        "quality": "medium",
        "sample_rate": 22050,
        "speed": 1.0,
        "gain": 0.80,
        "dataset": "~20 мин речи",
        "repo": COMMUNITY_URL,
        "file": "ru_RU-kat580-medium",
        "note": "мягче Ирины, но иногда добавляет лишние слова — 87%",
    },
    "mari": {
        "title": "Мари",
        "gender": "female",
        "quality": "medium",
        "sample_rate": 22050,
        "speed": 1.35,
        # 0.72, а не 0.60: при 0.60 она выходила тише всех остальных
        # (RMS 0.122 против 0.137 у «Ирины») — тихий голос и читается как
        # неуверенный. Дальше 0.75 громкость уже не растёт: Piper нормализует
        # по пику, и всё лишнее срезается потолком.
        "gain": 0.72,
        "tone": "brisk",
        "dataset": "~20 мин речи",
        "repo": COMMUNITY_URL,
        "file": "ru_RU-mari-medium_epoch5699",
        "note": "мягкий тембр, сама по себе вялая: лечится темпом 1.35, "
                "громкостью 0.72 и профилем «brisk» (тон +18%: 206 → 223 Гц, "
                "присутствие +5.5 дБ, компрессор мягкий)",
    },
    "dmitri": {
        "title": "Дмитрий",
        "gender": "male",
        "quality": "medium",
        "sample_rate": 22050,
        "speed": 1.0,
        "dataset": "voice-datasets (CC0)",
        "file": "dmitri/medium/ru_RU-dmitri-medium",
        "note": "прежний голос Пятницы, высокий мужской",
    },
    "denis": {
        "title": "Денис",
        "gender": "male",
        "quality": "medium",
        "sample_rate": 22050,
        "speed": 1.0,
        "dataset": "voice-datasets (CC0)",
        "file": "denis/medium/ru_RU-denis-medium",
        "note": "мужской, низкий: 138 Гц",
    },
    "ruslan": {
        "title": "Руслан",
        "gender": "male",
        "quality": "medium",
        "sample_rate": 22050,
        "speed": 1.0,
        "dataset": "RUSLAN corpus (CC BY-NC-SA)",
        "file": "ruslan/medium/ru_RU-ruslan-medium",
        "note": "мужской, выразительная дикция",
    },
}


# =====================================================================
#  ПУТИ И АДРЕСА
# =====================================================================
def is_known(name: str | None) -> bool:
    """Есть ли такой голос в каталоге."""
    return bool(name) and str(name).strip().lower() in VOICE_CATALOG


def normalize_name(name: str | None) -> str:
    """Приводит имя к ключу каталога; неизвестное имя не ломает работу."""
    key = str(name or "").strip().lower()
    return key if key in VOICE_CATALOG else DEFAULT_VOICE


def model_name(name: str) -> str:
    """Имя файла модели без расширения: ``ru_RU-sova200-medium``.

    Локально файл всегда называется по ключу каталога, даже если в репозитории
    он лежит под другим именем (``ru_RU-mari-medium_epoch5699``). Так путь к
    модели однозначно выводится из имени голоса — это же правило повторяет
    настольное окно, когда ищет готовый WAV в кэше.
    """
    return f"ru_RU-{normalize_name(name)}-medium"


def model_path(name: str) -> Path:
    """Путь к ``.onnx`` внутри папки голосов."""
    return Path(config.VOICES_DIR) / f"{model_name(name)}.onnx"


def config_path(name: str) -> Path:
    """Путь к JSON-описанию модели (лежит рядом с ``.onnx``)."""
    return Path(str(model_path(name)) + ".json")


def remote_url(name: str) -> str:
    """Адрес модели в репозитории её голоса."""
    info = VOICE_CATALOG[normalize_name(name)]
    return f"{info.get('repo', BASE_URL)}/{info['file']}.onnx"


def recommended_speed(name: str) -> float:
    """Темп речи, с которым голос звучит естественно."""
    return float(VOICE_CATALOG[normalize_name(name)]["speed"])


def gain(name: str) -> float:
    """Поправка громкости конкретного голоса.

    Модели нормализованы по пику, но плотность речи у них разная: на одной
    фразе «Сова» громче «Ирины» на 3.7 дБ, «Мари» — на 4.5. Без поправки
    смена голоса звучала бы как «стало громче», поэтому громкость выравнена по
    «Ирине» — уровню, к которому слух уже привык. Множитель живёт в каталоге,
    а не в настройках: пользовательскую ``PIPER_VOLUME`` он не перекрывает,
    а умножает.
    """
    return float(VOICE_CATALOG[normalize_name(name)].get("gain", 1.0))


def effective_volume(name: str | None = None) -> float:
    """Итоговая громкость синтеза: ``PIPER_VOLUME`` × поправка голоса."""
    raw = getattr(config, "PIPER_VOLUME", 1.0)
    try:
        base = 1.0 if raw is None else float(raw)
    except (TypeError, ValueError):
        base = 1.0
    return min(max(base * gain(name or active_name()), 0.0), 1.0)


def is_installed(name: str) -> bool:
    """Скачана ли модель целиком (есть и ``.onnx``, и ``.json``)."""
    model, cfg = model_path(name), config_path(name)
    try:
        return (model.exists() and model.stat().st_size > 1_000_000
                and cfg.exists() and cfg.stat().st_size > 0)
    except OSError:
        return False


def tone_name(name: str | None = None) -> str:
    """Имя профиля обработки тембра для голоса.

    Порядок: явная настройка ``PIPER_TONE`` → запись голоса в каталоге →
    ``tone.DEFAULT_PROFILE``. Явная настройка сильнее каталога: иначе нельзя
    было бы сравнить «как задумано» и «как есть», не правя код.
    """
    explicit = str(getattr(config, "PIPER_TONE", "") or "").strip().lower()
    if explicit in tone.PROFILES:
        return explicit
    key = normalize_name(name or active_name())
    fallback = str(VOICE_CATALOG[key].get("tone", tone.DEFAULT_PROFILE))
    return fallback if fallback in tone.PROFILES else tone.DEFAULT_PROFILE


def tone_profile(name: str | None = None) -> dict:
    """Профиль обработки тембра (пустой словарь — без обработки)."""
    return tone.get(tone_name(name))


def tone_pitch(name: str | None = None) -> float:
    """Во сколько раз поднимается голос (1.0 — без изменений).

    Пределы 0.8..1.35: ниже голос звучит «из бочки», выше начинается
    мультяшность — и то и другое уже не про «бодрость».
    """
    try:
        value = float(tone_profile(name).get("pitch", 1.0) or 1.0)
    except (TypeError, ValueError):
        return 1.0
    return min(max(value, 0.8), 1.35)


def length_scale(name: str | None = None, speed: float | None = None) -> float:
    """Во сколько раз растянуть синтез перед обработкой.

    Темп и высота независимы: подъём тона на 10% укорачивает звук на 10%,
    поэтому синтез заранее растягивается на столько же. Итог — та же
    длительность при более высоком голосе.
    """
    rate = recommended_speed(name) if speed is None else float(speed)
    return min(max(tone_pitch(name) / max(rate, 0.1), 0.4), 2.5)


def installed_voices() -> list:
    """Ключи каталога, модели которых уже лежат на диске."""
    return [key for key in VOICE_CATALOG if is_installed(key)]


# =====================================================================
#  АКТИВНЫЙ ГОЛОС
# =====================================================================
def _explicit_model() -> Path | None:
    """Путь из ``PIPER_MODEL``, если он задан вручную (своя модель).

    Пустая строка и ``"."`` (во что превращается пустой путь при чтении
    настроек) считаются «не задано»: иначе голосом командовал бы случайный
    текущий каталог.
    """
    raw = getattr(config, "PIPER_MODEL", None)
    if not raw:
        return None
    text = str(raw).strip()
    if text in ("", "."):
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path(config.BASE_DIR) / path
    return path


def active_name() -> str:
    """Имя активного голоса.

    Приоритет: ``PIPER_VOICE`` → имя, угаданное по пути ``PIPER_MODEL`` (так
    работают старые настройки) → голос по умолчанию.
    """
    declared = getattr(config, "PIPER_VOICE", "")
    if is_known(declared):
        return str(declared).strip().lower()
    explicit = _explicit_model()
    if explicit is not None:
        for key in VOICE_CATALOG:
            if model_name(key) in explicit.name:
                return key
    return DEFAULT_VOICE


def active_path() -> Path:
    """Файл модели активного голоса.

    Своя модель (``PIPER_MODEL`` с существующим файлом) важнее каталога: если
    пользователь указал собственный файл, он и звучит.
    """
    explicit = _explicit_model()
    if explicit is not None and explicit.exists():
        return explicit
    return model_path(active_name())


def describe(name: str) -> str:
    """Строка для списка голосов: имя, пол, наличие, темп, обработка."""
    key = normalize_name(name)
    info = VOICE_CATALOG[key]
    mark = "установлен" if is_installed(key) else "не скачан "
    active = "→ " if key == active_name() else "  "
    gender = "женский" if info["gender"] == "female" else "мужской"
    origin = "сообщество" if info.get("repo") else "официальный"
    profile = str(info.get("tone", tone.DEFAULT_PROFILE))
    return (f"{active}{key:<8} {info['title']:<8} {gender:<8} {mark}  "
            f"темп {info['speed']:.2f}  тон {profile:<10} {origin:<11} "
            f"{info['dataset']}")


# =====================================================================
#  СКАЧИВАНИЕ
# =====================================================================
def _download(url: str, destination: Path, min_bytes: int = 1_000_000) -> None:
    """Скачивает файл через ``.part`` — оборванная загрузка не портит модель.

    ``min_bytes`` разный для модели и её JSON: описание голоса весит около
    5 КБ, и общая проверка «не меньше мегабайта» отвергала бы его как битое.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    partial.unlink(missing_ok=True)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Pyatnitsa/3.0"})
        with urllib.request.urlopen(request, timeout=120) as response, \
                open(partial, "wb") as handle:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                handle.write(chunk)
        if partial.stat().st_size < min_bytes:
            raise OSError("сервер вернул слишком маленький файл")
        partial.replace(destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def ensure(name: str, on_log=None) -> Path:
    """Возвращает модель голоса, скачивая её при необходимости."""
    key = normalize_name(name)
    if is_installed(key):
        return model_path(key)
    if not bool(getattr(config, "PIPER_AUTO_DOWNLOAD", True)):
        raise FileNotFoundError(
            f"голос «{key}» не скачан. Выполните: python voice.py use {key}"
        )
    if callable(on_log):
        on_log(f"Скачиваю голос «{VOICE_CATALOG[key]['title']}» (около 63 МБ)...")
    url = remote_url(key)
    _download(url, model_path(key))
    _download(url + ".json", config_path(key), min_bytes=100)
    if not is_installed(key):
        raise RuntimeError(f"голос «{key}» скачался не полностью")
    return model_path(key)


# =====================================================================
#  ПЕРЕКЛЮЧЕНИЕ
# =====================================================================
def _read_settings(path: Path) -> dict:
    """Читает файл настроек, не падая на битом JSON."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_settings(path: Path, data: dict) -> None:
    """Пишет настройки целиком: там же токены Telegram и ключ Gemini."""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def set_active(name: str, *, keep_speed: bool = False,
               settings_file: Path | None = None) -> dict:
    """Записывает выбранный голос в настройки и в текущий процесс.

    Возвращает словарь изменений — его печатает ``voice.py``. Файл настроек
    переписывается целиком, но с сохранением порядка и всех остальных ключей
    (там лежат токены Telegram и ключ Gemini — их нельзя терять).
    """
    key = str(name or "").strip().lower()
    if key not in VOICE_CATALOG:
        raise KeyError(f"неизвестный голос: {name}. Доступны: "
                       + ", ".join(sorted(VOICE_CATALOG)))
    path = Path(settings_file or config.SETTINGS_FILE)
    data = _read_settings(path)

    changes = {"PIPER_VOICE": key, "PIPER_MODEL": ""}
    data["PIPER_VOICE"] = key
    # Пустой PIPER_MODEL означает «брать модель по имени голоса». Иначе старый
    # абсолютный путь перекрыл бы выбор и голос не сменился бы.
    data["PIPER_MODEL"] = ""
    if not keep_speed:
        speed = recommended_speed(key)
        data["PIPER_SPEED"] = speed
        changes["PIPER_SPEED"] = speed

    _write_settings(path, data)

    # Текущий процесс тоже должен увидеть выбор: config уже прочитан.
    config.PIPER_VOICE = key
    config.PIPER_MODEL = None
    if not keep_speed:
        config.PIPER_SPEED = recommended_speed(key)
    return changes


def set_tone(name: str, *, settings_file: Path | None = None) -> str:
    """Записывает профиль тембра. ``auto`` — «как задумано для голоса».

    Пустая строка в настройках означает именно это: каталог сам решает,
    нужна ли голосу обработка. Так смена голоса не тащит за собой чужой
    характер — у каждого он свой.
    """
    raw = str(name or "").strip().lower()
    key = "" if raw in ("auto", "") else raw
    if key and key not in tone.PROFILES:
        raise KeyError(f"нет такого профиля: {name}. Доступны: auto, "
                       + ", ".join(tone.PROFILES))
    path = Path(settings_file or config.SETTINGS_FILE)
    data = _read_settings(path)
    data["PIPER_TONE"] = key
    _write_settings(path, data)
    config.PIPER_TONE = key
    return key


# =====================================================================
#  ПРОВЕРКА ГОЛОСА
# =====================================================================
#: Фраза для проверки — та же интонация, что у приветствия Пятницы.
PROBE_PHRASE = ("Добрый вечер, сэр. Системы запущены, все службы в норме. "
                "Куда прикажете направить вычислительные мощности?")


def probe(name: str | None = None, phrase: str = PROBE_PHRASE) -> dict:
    """Синтезирует пробную фразу и возвращает её параметры.

    Ничего не проигрывает: проверка не должна внезапно заговорить в комнате.
    """
    import wave

    import numpy as np
    from piper import PiperVoice
    from piper.config import SynthesisConfig

    key = normalize_name(name or active_name())
    model = ensure(key)
    voice = PiperVoice.load(str(model))
    speed = recommended_speed(key)
    result = {"voice": key, "model": model.name, "speed": speed}

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "probe.wav"
        with wave.open(str(out), "wb") as handle:
            voice.synthesize_wav(phrase, handle, syn_config=SynthesisConfig(
                length_scale=length_scale(key, speed), volume=effective_volume(key)))
        # Замеряем то, что реально услышит пользователь, а не сырой выход
        # модели: обработка тембра меняет и уровень, и спектр.
        tone.apply_to_wav(out, tone_profile(key))
        with wave.open(str(out), "rb") as handle:
            rate = handle.getframerate()
            frames = handle.getnframes()
            audio = np.frombuffer(handle.readframes(frames), dtype=np.int16)
        samples = audio.astype(np.float32) / 32768.0
        result.update({
            "rate": rate,
            "seconds": frames / rate if rate else 0.0,
            "peak": float(np.max(np.abs(samples))) if samples.size else 0.0,
            "rms": float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0,
            # Piper нормализует звук (normalize_audio=True), поэтому пик 1.0 —
            # это норма, а не клиппинг. Настоящий признак перегруза — большая
            # доля сэмплов, упёршихся в предел.
            "loud_ratio": (float(np.mean(np.abs(samples) >= 0.99))
                           if samples.size else 0.0),
            "words": len(phrase.split()),
        })
    if result["seconds"]:
        result["words_per_minute"] = result["words"] / result["seconds"] * 60
    return result
