# -*- coding: utf-8 -*-
"""Генерирует заглушку аудио для визуализатора.

Зачем: пока Electron не подключён к реальному Piper, визуализатору нужен
хоть какой-то файл, чтобы AnalyserNode было что анализировать. Скрипт
синтезирует короткую русскую фразу тем же голосом, что использует ассистент,
и кладёт её в public/ — Vite отдаст файл по адресу /test-voice.wav.

Запуск (из папки desktop):
    python scripts/gen-test-audio.py
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

DESKTOP = Path(__file__).resolve().parent.parent
PROJECT = DESKTOP.parent
OUT = DESKTOP / "src" / "renderer" / "public" / "test-voice.wav"

PHRASE = "Джарвис на связи. Визуализация работает."


def main() -> int:
    # Импортируем питоновскую часть ассистента как библиотеку — только чтобы
    # взять готовый голос. Electron её не запускает.
    sys.path.insert(0, str(PROJECT))
    import config  # noqa: E402
    import tts  # noqa: E402

    OUT.parent.mkdir(parents=True, exist_ok=True)

    speaker = tts.Speaker(enabled=False)  # enabled=False: не проигрывать вслух
    voice = speaker._load_piper_voice()
    with wave.open(str(OUT), "wb") as wav_file:
        voice.synthesize_wav(PHRASE, wav_file, syn_config=speaker._piper_config())

    size = OUT.stat().st_size
    print(f"OK: {OUT} ({size} байт)")
    if size < 1000:
        print("Файл подозрительно маленький — проверьте голос Piper.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
