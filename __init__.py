# -*- coding: utf-8 -*-
"""Пятница — голосовой ассистент для Windows.

Распознавание речи: GigaAM v3 RNN-T (ONNX Runtime, локально, офлайн) —
экспериментальный движок: точнее Vosk, но заметно сильнее грузит процессор.
Синтез речи: edge-tts → piper → SAPI.
Мозг: Gemini API (бесплатный тариф).
"""

__version__ = "3.5.0-experimental"
__all__ = ["__version__"]
