# -*- coding: utf-8 -*-
"""Загрузка голосовых моделей Piper.

Тонкая обёртка над ``voices.py``: каталог, пути и скачивание живут там, чтобы
не было двух источников правды. Старые команды продолжают работать.

Запуск::

    python download_voices.py            # активный голос (сейчас irina)
    python download_voices.py dmitri     # другой голос
    python download_voices.py --list     # что вообще есть

Выбрать скачанный голос основным: ``python voice.py use irina``.
"""

from __future__ import annotations

import argparse
import sys

import voices


def main() -> int:
    parser = argparse.ArgumentParser(description="Загрузка голосов Piper")
    parser.add_argument("voice", nargs="?", default=voices.active_name(),
                        choices=sorted(voices.VOICE_CATALOG),
                        help="какой голос скачать (по умолчанию активный)")
    parser.add_argument("--list", action="store_true", help="показать список голосов")
    args = parser.parse_args()

    if args.list:
        print("Доступные русские голоса Piper:")
        for key in voices.VOICE_CATALOG:
            print("  " + voices.describe(key))
        print("\n  → — выбран сейчас")
        return 0

    info = voices.VOICE_CATALOG[args.voice]
    print(f"Загружаю голос: {info['title']} ({args.voice})")
    try:
        model = voices.ensure(args.voice, on_log=lambda message: print("  " + message))
    except Exception as exc:  # noqa: BLE001
        print(f"\nНе удалось скачать: {exc}")
        print("Проверьте интернет и попробуйте снова.")
        return 1

    print(f"\nГотово. Модель: {model}")
    if voices.active_name() != args.voice:
        print(f"Чтобы Пятница заговорила этим голосом: python voice.py use {args.voice}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
