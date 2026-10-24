# -*- coding: utf-8 -*-
"""«Зрение» Пятницаа: скриншот, распознавание текста и анализ изображения.

Распознавание текста идёт через **встроенный движок Windows**
(``Windows.Media.Ocr``) — ничего скачивать не нужно, русский язык поддерживается
системным языковым пакетом. Если движок недоступен, используется ``pytesseract``.

Анализ картинки целиком (а не только текста) идёт через мультимодальную
модель Gemini — она задаётся в настройке ``GEMINI_VISION_MODEL``.

.. note::
   Импорт ``winrt`` по-прежнему делается ПОСЛЕ инициализации распознавателя.
   Раньше обратный порядок ронял загрузку faster-whisper сегфолтом; сейчас
   распознавание работает на Vosk, но ядро всё равно вызывает
   ``Pyatnitsa._warm_recognizer()`` перед любым обращением к OCR — так первая
   команда не ждёт загрузку модели.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import config
from brain import BrainUnavailable
from utils import log


def _run_async(factory):
    """Выполняет корутину, даже если текущий поток уже внутри событийного цикла.

    Vision вызывается и из главного цикла, и из потока Telegram-бота,
    где цикл уже запущен — там ``asyncio.run`` бросил бы исключение.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    result: dict = {}

    def worker():
        try:
            result["value"] = asyncio.run(factory())
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=worker, name="vision-ocr", daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


class Vision:
    def __init__(self, actions, brain):
        self.actions = actions
        self.brain = brain

    # ------------------------------------------------------------------
    def capture(self) -> Path | None:
        return self.actions.screenshot(prefix="Vision")

    # ------------------------------------------------------------------
    #  OCR
    # ------------------------------------------------------------------
    def ocr(self, image_path: Path) -> str:
        """Распознаёт текст на изображении. Пустая строка, если не получилось."""
        if config.FEATURES["windows_ocr"]:
            try:
                text = self._ocr_windows(Path(image_path))
                if text:
                    return text
            except Exception as exc:  # noqa: BLE001
                log(f"Windows OCR не сработал: {exc}", "debug")
        return self._ocr_pytesseract(Path(image_path))

    @staticmethod
    def _ocr_windows(image_path: Path) -> str:
        """Встроенный OCR Windows. Поддерживает русский, если установлен языковой пакет."""
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage import FileAccessMode, StorageFile

        async def recognize():
            storage_file = await StorageFile.get_file_from_path_async(str(image_path))
            stream = await storage_file.open_async(FileAccessMode.READ)
            decoder = await BitmapDecoder.create_async(stream)
            bitmap = await decoder.get_software_bitmap_async()

            engine = None
            for tag in (config.OCR_LANGUAGE, "en-US"):
                try:
                    if OcrEngine.is_language_supported(Language(tag)):
                        engine = OcrEngine.try_create_from_language(Language(tag))
                        if engine is not None:
                            break
                except Exception:  # noqa: BLE001
                    continue
            if engine is None:
                engine = OcrEngine.try_create_from_user_profile_languages()
            if engine is None:
                raise RuntimeError("движок OCR недоступен для этого языка")

            result = await engine.recognize_async(bitmap)
            return result.text

        text = _run_async(recognize)
        return " ".join((text or "").split())

    @staticmethod
    def _ocr_pytesseract(image_path: Path) -> str:
        if not config.FEATURES["pytesseract"]:
            return ""
        try:
            import pytesseract
            from PIL import Image

            with Image.open(image_path) as image:
                text = pytesseract.image_to_string(image, lang="rus+eng")
            return " ".join(text.split())
        except Exception as exc:  # noqa: BLE001
            log(f"pytesseract не сработал: {exc}", "debug")
            return ""

    # ------------------------------------------------------------------
    #  Публичные сценарии
    # ------------------------------------------------------------------
    def read_screen_text(self) -> str:
        """«Пятница, прочитай, что на экране»."""
        path = self.capture()
        if path is None:
            return "Не удалось сделать скриншот — установите Pillow: pip install pillow"

        text = self.ocr(path)
        if text:
            return f"На экране написано: {text[:700]}"

        if not (config.FEATURES["windows_ocr"] or config.FEATURES["pytesseract"]):
            return ("Распознавание текста недоступно: установите компонент Windows OCR "
                    "(pip install winrt-Windows.Media.Ocr winrt-Windows.Storage "
                    "winrt-Windows.Storage.Streams winrt-Windows.Graphics.Imaging "
                    "winrt-Windows.Globalization winrt-Windows.Foundation). "
                    "А сказать «что на экране» — я разберу картинку целиком.")
        return "Не нашёл текст на экране, сэр."

    def analyze(self, question: str = "") -> str:
        """Анализирует текущий экран и отвечает на вопрос по нему."""
        question = (question or "").strip() or "Опиши кратко, что происходит на экране."
        path = self.capture()
        if path is None:
            return "Не удалось сделать скриншот — установите Pillow: pip install pillow"

        # 1. Полноценный анализ картинки мультимодальной моделью Gemini.
        if self.brain.vision_available():
            try:
                return self.brain.ask_with_image(question, path)
            except BrainUnavailable as exc:
                log(f"Анализ изображения не удался: {exc}", "warn")
            except Exception as exc:  # noqa: BLE001
                log(f"Анализ изображения не удался: {exc}", "warn")

        # 2. Иначе — распознаём текст и отдаём его обычной модели.
        text = self.ocr(path)
        if text:
            prompt = (f"Пользователь смотрит на свой экран. Текст с экрана:\n{text[:1500]}\n\n"
                      f"Вопрос: {question}")
            try:
                return self.brain.ask(prompt, remember=False)
            except BrainUnavailable as exc:
                return f"{exc} Текст с экрана: {text[:400]}"

        if config.GEMINI_VISION_MODEL:
            return (f"Скриншот сохранён в {path}. Модель "
                    f"«{config.GEMINI_VISION_MODEL}» сейчас недоступна: "
                    "проверьте ключ Gemini и интернет.")
        return (f"Скриншот сохранён в {path}, но текста на нём я не нашёл. "
                "Чтобы я мог описывать картинки, задайте GEMINI_VISION_MODEL "
                "(например «gemini-3.5-flash»).")
