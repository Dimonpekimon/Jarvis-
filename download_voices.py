# -*- coding: utf-8 -*-
"""Загрузка голосовых моделей Piper для офлайн-озвучки.

Piper работает без интернета, поэтому пригодится, если сеть пропала
или вы не хотите отправлять текст в облако Microsoft.

Запуск::

    python download_voices.py            # русский голос по умолчанию (dmitri)
    python download_voices.py irina      # женский голос
    python download_voices.py --list     # список доступных голосов
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

import config

BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/ru/ru_RU"

VOICES = {
    "dmitri": {
        "title": "Дмитрий (мужской, среднее качество)",
        "path": "dmitri/medium/ru_RU-dmitri-medium",
    },
    "irina": {
        "title": "Ирина (женский, среднее качество)",
        "path": "irina/medium/ru_RU-irina-medium",
    },
    "denis": {
        "title": "Денис (мужской, среднее качество)",
        "path": "denis/medium/ru_RU-denis-medium",
    },
    "ruslan": {
        "title": "Руслан (мужской, высокое качество)",
        "path": "ruslan/high/ru_RU-ruslan-medium",
    },
}


def download(url: str, destination: Path) -> None:
    if destination.exists() and destination.stat().st_size > 0:
        print(f"  уже есть: {destination.name}")
        return
    print(f"  качаю:    {destination.name}")
    tmp = destination.with_suffix(destination.suffix + ".part")
    with urllib.request.urlopen(url, timeout=120) as response, open(tmp, "wb") as handle:
        while True:
            chunk = response.read(1 << 16)
            if not chunk:
                break
            handle.write(chunk)
    tmp.replace(destination)


def main() -> int:
    parser = argparse.ArgumentParser(description="Загрузка голосов Piper")
    parser.add_argument("voice", nargs="?", default="dmitri", choices=sorted(VOICES),
                        help="какой голос скачать (по умолчанию dmitri)")
    parser.add_argument("--list", action="store_true", help="показать список голосов")
    args = parser.parse_args()

    if args.list:
        print("Доступные русские голоса Piper:")
        for key, info in VOICES.items():
            print(f"  {key:<8} {info['title']}")
        return 0

    info = VOICES[args.voice]
    print(f"Загружаю голос: {info['title']}")

    config.VOICES_DIR.mkdir(parents=True, exist_ok=True)
    remote = f"{BASE_URL}/{info['path']}"
    model_name = Path(info["path"]).name

    try:
        download(f"{remote}.onnx", config.VOICES_DIR / f"{model_name}.onnx")
        download(f"{remote}.onnx.json", config.VOICES_DIR / f"{model_name}.onnx.json")
    except Exception as exc:  # noqa: BLE001
        print(f"\nНе удалось скачать: {exc}")
        print("Проверьте интернет и попробуйте снова.")
        return 1

    print(f"\nГотово. Модель: {config.VOICES_DIR / (model_name + '.onnx')}")
    print("Чтобы использовать её как основной голос, укажите в jarvis_settings.json:")
    print('  "TTS_ENGINE": "piper"')
    print(f'  "PIPER_MODEL": "{config.VOICES_DIR / (model_name + ".onnx")}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
