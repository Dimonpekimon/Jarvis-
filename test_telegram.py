# -*- coding: utf-8 -*-
"""Тесты Telegram-моста Джарвиса (:mod:`telegram_bot`).

Полностью изолированы: нет сети, нет реального бота, нет команд ОС и нет
настоящих токенов. Telegram-объекты (``Update``/``Message``/``Chat``) заменены
лёгкими заглушками, а ``python-telegram-bot`` может вообще не быть установлен.

Проверяется ровно то, что чинили:

* в докстроке больше нет похожего на секрет значения, а пример ID числовой;
* пустой список ``TELEGRAM_ALLOWED_USERS`` больше НЕ открывает доступ всем —
  доступ закрыт по умолчанию (deny-by-default);
* разрешённые ID валидируются: принимаются положительные ``int`` и строки из
  цифр, отбрасываются ``bool``, ноль, отрицательные и мусор;
* ``start`` отказывается поднимать бота с пустым списком и пишет подсказку;
* ``/start`` проверяет авторизацию ДО регистрации чата;
* ``/status`` и ``/reset`` уводят синхронные колбэки в ``asyncio.to_thread``;
* длинные ответы режутся на части <= 4000 символов без потери Unicode.

Запуск: ``python test_telegram.py`` (или ``python -m unittest test_telegram``).
"""

from __future__ import annotations

import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
import telegram_bot  # noqa: E402
from telegram_bot import TELEGRAM_MAX_MESSAGE_LEN, TelegramBridge, split_message  # noqa: E402

# Заведомо ненастоящий токен: тесты не должны содержать секретов.
DUMMY_TOKEN = "DUMMY_TOKEN_FOR_TESTS"


# =====================================================================
#  ЗАГЛУШКИ TELEGRAM
# =====================================================================
class FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id
        self.actions: list = []

    async def send_action(self, action):
        self.actions.append(action)


class FakeMessage:
    def __init__(self, text=None, chat=None):
        self.text = text
        self.chat = chat
        self.replies: list = []

    async def reply_text(self, text, **_kwargs):
        self.replies.append(text)
        return self


class FakeUpdate:
    """Минимальный ``Update``: то, что читает ``TelegramBridge``."""

    def __init__(self, user_id=None, chat_id=1, text=None):
        self.effective_user = FakeUser(user_id) if user_id is not None else None
        self.effective_chat = FakeChat(chat_id)
        self.message = FakeMessage(text=text, chat=self.effective_chat)


def make_bridge(allowed, token=DUMMY_TOKEN, handler=None, status_provider=None, reset_callback=None):
    """Собирает мост с подменёнными настройками config (без чтения файла)."""
    with mock.patch.object(config, "TELEGRAM_TOKEN", token), \
            mock.patch.object(config, "TELEGRAM_ALLOWED_USERS", allowed):
        return TelegramBridge(
            handler or (lambda _text: "ok"),
            status_provider,
            reset_callback,
        )


# =====================================================================
#  РАЗБИЕНИЕ ДЛИННЫХ СООБЩЕНИЙ
# =====================================================================
class SplitMessageTests(unittest.TestCase):
    def test_short_text_single_part(self):
        self.assertEqual(split_message("привет"), ["привет"])

    def test_text_at_limit_single_part(self):
        text = "a" * TELEGRAM_MAX_MESSAGE_LEN
        self.assertEqual(split_message(text), [text])

    def test_long_text_is_split_within_limit(self):
        text = "слово " * 2000  # ~12000 символов
        parts = split_message(text)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), TELEGRAM_MAX_MESSAGE_LEN)

    def test_join_restores_original_text(self):
        text = "Джарвис " * 1500
        self.assertEqual("".join(split_message(text)), text)

    def test_unicode_and_emoji_not_broken(self):
        text = ("привет мир 🚀 " * 600).strip()
        parts = split_message(text)
        for part in parts:
            self.assertLessEqual(len(part), TELEGRAM_MAX_MESSAGE_LEN)
        self.assertEqual("".join(parts), text)

    def test_prefers_newline_boundary(self):
        first = "a" * 3000
        second = "b" * 3000
        parts = split_message(f"{first}\n{second}")
        self.assertEqual(parts[0], first + "\n")
        self.assertEqual(parts[1], second)

    def test_no_space_hard_split(self):
        text = "x" * 9001
        parts = split_message(text)
        self.assertEqual([len(p) for p in parts], [4000, 4000, 1001])

    def test_empty_and_none(self):
        self.assertEqual(split_message(""), [""])
        self.assertEqual(split_message(None), [""])

    def test_invalid_limit_rejected(self):
        with self.assertRaises(ValueError):
            split_message("abc", limit=0)


# =====================================================================
#  ВАЛИДАЦИЯ РАЗРЕШЁННЫХ ID
# =====================================================================
class AllowedUsersParsingTests(unittest.TestCase):
    def parse(self, raw):
        return TelegramBridge._parse_allowed_users(raw)

    def test_accepts_positive_ints(self):
        self.assertEqual(self.parse([111, 222]), {111, 222})

    def test_accepts_digit_strings(self):
        self.assertEqual(self.parse(["111", " 222 "]), {111, 222})

    def test_rejects_bool(self):
        self.assertEqual(self.parse([True, False]), set())

    def test_rejects_zero_and_negative(self):
        self.assertEqual(self.parse([0, -5, "0", "-7"]), set())

    def test_rejects_invalid_strings(self):
        self.assertEqual(self.parse(["abc", "12a", "1.5", "", "  ", "+3"]), set())

    def test_mixed_keeps_only_valid(self):
        self.assertEqual(self.parse([111, "222", True, 0, "nope", -1]), {111, 222})

    def test_none_and_empty(self):
        self.assertEqual(self.parse(None), set())
        self.assertEqual(self.parse([]), set())


# =====================================================================
#  АВТОРИЗАЦИЯ
# =====================================================================
class IsAllowedTests(unittest.TestCase):
    def test_empty_list_denies_everyone(self):
        bridge = make_bridge([])
        self.assertFalse(bridge._is_allowed(FakeUpdate(user_id=111)))

    def test_listed_user_allowed(self):
        bridge = make_bridge([111, 222])
        self.assertTrue(bridge._is_allowed(FakeUpdate(user_id=111)))

    def test_unlisted_user_denied(self):
        bridge = make_bridge([111])
        self.assertFalse(bridge._is_allowed(FakeUpdate(user_id=999)))

    def test_missing_user_denied(self):
        bridge = make_bridge([111])
        self.assertFalse(bridge._is_allowed(FakeUpdate(user_id=None)))

    def test_digit_string_config_allows(self):
        bridge = make_bridge(["111"])
        self.assertTrue(bridge._is_allowed(FakeUpdate(user_id=111)))


# =====================================================================
#  /start — АВТОРИЗАЦИЯ ДО РЕГИСТРАЦИИ ЧАТА
# =====================================================================
class StartCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_start_registers_chat(self):
        bridge = make_bridge([111])
        update = FakeUpdate(user_id=111, chat_id=555)
        await bridge._cmd_start(update, None)
        self.assertIn(555, bridge._chats)
        self.assertEqual(len(update.message.replies), 1)

    async def test_unauthorized_start_does_not_register_chat(self):
        bridge = make_bridge([111])
        update = FakeUpdate(user_id=999, chat_id=555)
        await bridge._cmd_start(update, None)
        self.assertNotIn(555, bridge._chats)
        self.assertEqual(bridge._chats, set())
        self.assertEqual(update.message.replies, ["Извините, доступ ограничен."])

    async def test_empty_allowlist_start_denied(self):
        bridge = make_bridge([])
        update = FakeUpdate(user_id=111, chat_id=555)
        await bridge._cmd_start(update, None)
        self.assertEqual(bridge._chats, set())


# =====================================================================
#  /status и /reset — СИНХРОННЫЕ КОЛБЭКИ В ОТДЕЛЬНОМ ПОТОКЕ
# =====================================================================
class SyncCallbackOffloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_provider_runs_off_event_loop(self):
        loop_thread = threading.get_ident()
        seen = {}

        def provider():
            seen["thread"] = threading.get_ident()
            return "всё хорошо"

        bridge = make_bridge([111], status_provider=provider)
        update = FakeUpdate(user_id=111)
        await bridge._cmd_status(update, None)

        self.assertIn("thread", seen)
        self.assertNotEqual(seen["thread"], loop_thread)
        self.assertEqual(update.message.replies, ["всё хорошо"])

    async def test_reset_callback_runs_off_event_loop(self):
        loop_thread = threading.get_ident()
        seen = {}

        def reset():
            seen["thread"] = threading.get_ident()

        bridge = make_bridge([111], reset_callback=reset)
        update = FakeUpdate(user_id=111)
        await bridge._cmd_reset(update, None)

        self.assertIn("thread", seen)
        self.assertNotEqual(seen["thread"], loop_thread)
        self.assertEqual(update.message.replies, ["История диалога очищена, сэр."])

    async def test_status_without_provider_uses_default(self):
        bridge = make_bridge([111], status_provider=None)
        update = FakeUpdate(user_id=111)
        await bridge._cmd_status(update, None)
        self.assertEqual(update.message.replies, ["Нет данных."])

    async def test_status_denied_for_unauthorized(self):
        called = []

        def provider():
            called.append(True)
            return "секрет"

        bridge = make_bridge([111], status_provider=provider)
        update = FakeUpdate(user_id=999)
        await bridge._cmd_status(update, None)
        self.assertEqual(update.message.replies, [])
        self.assertEqual(called, [])

    async def test_reset_denied_for_unauthorized(self):
        called = []

        def reset():
            called.append(True)

        bridge = make_bridge([111], reset_callback=reset)
        update = FakeUpdate(user_id=999)
        await bridge._cmd_reset(update, None)
        self.assertEqual(update.message.replies, [])
        self.assertEqual(called, [])

    async def test_status_long_text_is_split(self):
        long_status = ("состояние " * 1000).strip()
        bridge = make_bridge([111], status_provider=lambda: long_status)
        update = FakeUpdate(user_id=111)
        await bridge._cmd_status(update, None)
        self.assertGreater(len(update.message.replies), 1)
        for part in update.message.replies:
            self.assertLessEqual(len(part), TELEGRAM_MAX_MESSAGE_LEN)
        self.assertEqual("".join(update.message.replies), long_status)


# =====================================================================
#  ОБЫЧНЫЕ СООБЩЕНИЯ
# =====================================================================
class OnMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_message_reaches_handler(self):
        bridge = make_bridge([111], handler=lambda text: f"ответ: {text}")
        update = FakeUpdate(user_id=111, chat_id=42, text="  погода  ")
        await bridge._on_message(update, None)
        self.assertEqual(update.message.replies, ["ответ: погода"])
        self.assertIn(42, bridge._chats)
        self.assertIn("typing", update.message.chat.actions)

    async def test_handler_runs_off_event_loop(self):
        loop_thread = threading.get_ident()
        seen = {}

        def handler(_text):
            seen["thread"] = threading.get_ident()
            return "готово"

        bridge = make_bridge([111], handler=handler)
        await bridge._on_message(FakeUpdate(user_id=111, text="привет"), None)
        self.assertNotEqual(seen["thread"], loop_thread)

    async def test_unauthorized_message_denied_without_handler(self):
        called = []

        def handler(text):
            called.append(text)
            return "нельзя"

        bridge = make_bridge([111], handler=handler)
        update = FakeUpdate(user_id=999, chat_id=42, text="привет")
        await bridge._on_message(update, None)
        self.assertEqual(update.message.replies, ["Извините, доступ ограничен."])
        self.assertEqual(called, [])
        self.assertEqual(bridge._chats, set())

    async def test_long_reply_is_split(self):
        long_reply = "Джарвис " * 1500  # ~12000 символов
        bridge = make_bridge([111], handler=lambda _text: long_reply)
        update = FakeUpdate(user_id=111, text="расскажи")
        await bridge._on_message(update, None)
        self.assertGreater(len(update.message.replies), 1)
        for part in update.message.replies:
            self.assertLessEqual(len(part), TELEGRAM_MAX_MESSAGE_LEN)
        self.assertEqual("".join(update.message.replies), long_reply)

    async def test_empty_reply_uses_fallback(self):
        bridge = make_bridge([111], handler=lambda _text: "")
        update = FakeUpdate(user_id=111, text="пусто")
        await bridge._on_message(update, None)
        self.assertEqual(update.message.replies, ["Готово, сэр."])

    async def test_handler_error_is_caught(self):
        def boom(_text):
            raise RuntimeError("падение")

        bridge = make_bridge([111], handler=boom)
        update = FakeUpdate(user_id=111, text="бум")
        with mock.patch.object(telegram_bot, "log") as fake_log:
            await bridge._on_message(update, None)
        self.assertEqual(
            update.message.replies,
            ["Произошла ошибка при обработке команды, сэр."],
        )
        fake_log.assert_called_once()

    async def test_non_text_message_ignored(self):
        called = []
        bridge = make_bridge([111], handler=lambda t: called.append(t) or "x")
        update = FakeUpdate(user_id=111, text=None)
        await bridge._on_message(update, None)
        self.assertEqual(update.message.replies, [])
        self.assertEqual(called, [])


# =====================================================================
#  START — ПРЕДОХРАНИТЕЛИ БЕЗ РЕАЛЬНОГО ЗАПУСКА
# =====================================================================
class StartGuardTests(unittest.TestCase):
    def _start_with(self, *, enabled, has_telegram, allowed, token=DUMMY_TOKEN):
        bridge = make_bridge(allowed, token=token)
        with mock.patch.object(config, "TELEGRAM_ENABLED", enabled), \
                mock.patch.object(telegram_bot, "HAS_TELEGRAM", has_telegram), \
                mock.patch.object(telegram_bot, "log") as fake_log:
            result = bridge.start()
        # Ни при каком сценарии реальный поток не поднимается.
        self.assertFalse(bridge.running)
        self.assertIsNone(bridge._thread)
        return result, fake_log

    def test_disabled_returns_false(self):
        result, _ = self._start_with(enabled=False, has_telegram=True, allowed=[111])
        self.assertFalse(result)

    def test_missing_token_returns_false(self):
        result, fake_log = self._start_with(
            enabled=True, has_telegram=True, allowed=[111], token=""
        )
        self.assertFalse(result)
        fake_log.assert_called()

    def test_empty_allowlist_refused_with_hint(self):
        result, fake_log = self._start_with(enabled=True, has_telegram=True, allowed=[])
        self.assertFalse(result)
        fake_log.assert_called()
        logged = " ".join(str(c.args[0]) for c in fake_log.call_args_list)
        self.assertIn("TELEGRAM_ALLOWED_USERS", logged)

    def test_invalid_allowlist_values_refused(self):
        # Всё мусорное -> после валидации список пуст -> отказ.
        result, _ = self._start_with(
            enabled=True, has_telegram=True, allowed=[True, 0, "abc", -3]
        )
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
