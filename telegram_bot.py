# -*- coding: utf-8 -*-
"""Telegram-бот Джарвиса.

Позволяет отдавать команды ассистенту с телефона и получать ответы текстом.
Работает в отдельном потоке, чтобы не мешать голосовому циклу.

Включение: в ``jarvis_settings.json`` задайте::

    "TELEGRAM_ENABLED": true,
    "TELEGRAM_TOKEN": "123456:ABC...",
    "TELEGRAM_ALLOWED_USERS": [123456789]   // свой числовой Telegram ID

Список ``TELEGRAM_ALLOWED_USERS`` обязателен: пустой список означает «никому»
(доступ по умолчанию закрыт), и бот с таким списком не запускается.
"""

from __future__ import annotations

import asyncio
import threading

import config
from utils import log

try:
    from telegram import Update
    from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
    HAS_TELEGRAM = True
except ImportError:
    HAS_TELEGRAM = False


#: Лимит Telegram — 4096 символов; берём с запасом.
TELEGRAM_MAX_MESSAGE_LEN = 4000


def split_message(text: str, limit: int = TELEGRAM_MAX_MESSAGE_LEN) -> list[str]:
    """Делит длинный ответ на части не длиннее ``limit`` символов.

    Режет по последнему переводу строки или пробелу в окне, чтобы не рвать
    слова; если разрыва нет — режет жёстко по границе. Срез строки в Python
    идёт по code points, поэтому Unicode (кириллица, эмодзи) не ломается.
    Склейка частей даёт исходный текст.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    text = "" if text is None else str(text)
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    start = 0
    total = len(text)
    while start < total:
        end = min(start + limit, total)
        if end < total:
            boundary = max(text.rfind("\n", start, end), text.rfind(" ", start, end))
            if boundary > start:
                end = boundary + 1  # разделитель остаётся в конце части
        parts.append(text[start:end])
        start = end
    return parts


class TelegramBridge:
    """Мост между Telegram и ядром Джарвиса."""

    def __init__(self, handler, status_provider=None, reset_callback=None):
        self.handler = handler                 # callable(text) -> str
        self.status_provider = status_provider  # callable() -> str
        self.reset_callback = reset_callback    # callable() -> None
        self.token = config.TELEGRAM_TOKEN
        self.allowed_users = self._parse_allowed_users(config.TELEGRAM_ALLOWED_USERS)
        self._app = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._chats: set = set()

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_allowed_users(raw) -> set:
        """Оставляет только положительные целочисленные Telegram ID.

        Принимает ``int`` и строки из цифр; ``bool`` (подкласс ``int``) и любые
        нечисловые либо неположительные значения молча отбрасываются.
        """
        allowed: set = set()
        for item in raw or []:
            if isinstance(item, bool):
                continue
            if isinstance(item, int):
                value = item
            elif isinstance(item, str) and item.strip().isascii() and item.strip().isdigit():
                value = int(item.strip())
            else:
                continue
            if value > 0:
                allowed.add(value)
        return allowed

    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _is_allowed(self, update: Update) -> bool:
        """Доступ только для явно перечисленных ID; пустой список — никому."""
        user = update.effective_user
        if not user or not self.allowed_users:
            return False
        return user.id in self.allowed_users

    # ------------------------------------------------------------------
    async def _reply(self, update: Update, text: str) -> None:
        """Отправляет ответ, разбивая его на части в пределах лимита Telegram."""
        for part in split_message(text):
            await update.message.reply_text(part)

    async def _cmd_start(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            await update.message.reply_text("Извините, доступ ограничен.")
            return
        self._chats.add(update.effective_chat.id)
        await update.message.reply_text(
            "Джарвис на связи, сэр. Пишите команду обычным текстом — "
            "например «погода в Москве», «громче», «напомни через 10 минут позвонить маме».\n\n"
            "Команды: /status — состояние систем, /reset — очистить историю диалога."
        )

    async def _cmd_status(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            return
        if callable(self.status_provider):
            # Синхронный поставщик статуса — уводим его в отдельный поток.
            text = await asyncio.to_thread(self.status_provider)
        else:
            text = "Нет данных."
        await self._reply(update, text)

    async def _cmd_reset(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            return
        if callable(self.reset_callback):
            # Синхронный сброс истории — уводим его в отдельный поток.
            await asyncio.to_thread(self.reset_callback)
        await update.message.reply_text("История диалога очищена, сэр.")

    async def _on_message(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text:
            return
        if not self._is_allowed(update):
            await update.message.reply_text("Извините, доступ ограничен.")
            return

        self._chats.add(update.effective_chat.id)
        text = update.message.text.strip()
        try:
            await update.message.chat.send_action("typing")
            # Команды ассистента синхронные — уводим их в отдельный поток.
            reply = await asyncio.to_thread(self.handler, text)
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка обработки сообщения Telegram: {exc}", "error")
            reply = "Произошла ошибка при обработке команды, сэр."
        await self._reply(update, reply or "Готово, сэр.")

    # ------------------------------------------------------------------
    def start(self) -> bool:
        if not config.TELEGRAM_ENABLED:
            return False
        if not HAS_TELEGRAM:
            log("python-telegram-bot не установлен: pip install python-telegram-bot", "warn")
            return False
        if not self.token:
            log("Не задан TELEGRAM_TOKEN — бот не запущен.", "warn")
            return False
        if not self.allowed_users:
            log(
                "Не задан TELEGRAM_ALLOWED_USERS — бот не запущен: доступ закрыт "
                "по умолчанию. Укажите свой числовой Telegram ID в jarvis_settings.json.",
                "warn",
            )
            return False
        if self.running:
            return True

        self._thread = threading.Thread(target=self._run, name="telegram", daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            app = ApplicationBuilder().token(self.token).build()
            self._app = app
            app.add_handler(CommandHandler("start", self._cmd_start))
            app.add_handler(CommandHandler("help", self._cmd_start))
            app.add_handler(CommandHandler("status", self._cmd_status))
            app.add_handler(CommandHandler("reset", self._cmd_reset))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message))

            async def bootstrap():
                await app.initialize()
                await app.start()
                await app.updater.start_polling(drop_pending_updates=True)
                log("Telegram-бот запущен.", "info")

            loop.run_until_complete(bootstrap())
            loop.run_forever()
        except Exception as exc:  # noqa: BLE001
            log(f"Telegram-бот остановлен: {exc}", "error")
        finally:
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        app, loop = self._app, self._loop
        if app is None or loop is None or loop.is_closed():
            return

        async def shutdown():
            try:
                if app.updater and app.updater.running:
                    await app.updater.stop()
                await app.stop()
                await app.shutdown()
            except Exception:  # noqa: BLE001
                pass

        try:
            future = asyncio.run_coroutine_threadsafe(shutdown(), loop)
            future.result(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        loop.call_soon_threadsafe(loop.stop)

    # ------------------------------------------------------------------
    def broadcast(self, text: str) -> None:
        """Отправляет сообщение во все чаты, которые писали боту."""
        app, loop = self._app, self._loop
        if not text or app is None or loop is None or loop.is_closed() or not self._chats:
            return

        async def send_all():
            for chat_id in list(self._chats):
                try:
                    await app.bot.send_message(chat_id=chat_id, text=text)
                except Exception as exc:  # noqa: BLE001
                    log(f"Не удалось отправить в чат {chat_id}: {exc}", "debug")

        try:
            asyncio.run_coroutine_threadsafe(send_all(), loop)
        except Exception:  # noqa: BLE001
            pass
