# -*- coding: utf-8 -*-
"""Тесты оратора (``tts.Speaker``) — без звука, сети и реального SAPI.

Проверяем именно то, что раньше было сломано:

* ``say`` НЕ должен увеличивать эпоху отмены — иначе вторая фраза «съедала» бы
  первую, уже стоящую в очереди;
* ``stop`` — единственный, кто увеличивает эпоху и взводит отмену активной
  фразы, причём отмена доходит до синтеза (без «запоздалого» проигрывания) и НЕ
  перетекает в резервные движки;
* ``busy`` держится True от постановки до конца активной фразы (нет провала
  между «взял из очереди» и «начал говорить»);
* отмена SAPI вызывается ``engine.stop()`` в СВОЁМ рабочем потоке;
* ``shutdown`` дожидается прощальной фразы, а не выбрасывает её;
* ``publish=False`` подавляет транскрипт.

Все тесты используют только стандартную библиотеку и временные каталоги.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import config
import tts


def _wait_idle(speaker: "tts.Speaker", timeout: float = 5.0) -> bool:
    """Ждёт, пока оратор освободится. True — успел."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not speaker.busy:
            return True
        time.sleep(0.01)
    return not speaker.busy


class _FakeSapiEngine:
    """Минимальная замена pyttsx3-движка с внешним циклом событий."""

    def __init__(self) -> None:
        self._connects: dict = {}
        self._finished = False
        self._ticks = 0
        self.say_calls: list = []
        self.spoken = threading.Event()
        self.stop_called = threading.Event()
        self.end_loop_called = threading.Event()
        self.stop_thread: threading.Thread | None = None

    # --- API, которое использует tts.Speaker ---
    def connect(self, topic, cb):
        self._connects.setdefault(topic, []).append(cb)
        return {"topic": topic, "cb": cb}

    def disconnect(self, token) -> None:
        pass

    def setProperty(self, name, value) -> None:
        pass

    def getProperty(self, name):
        return []

    def say(self, text, name=None) -> None:
        self.say_calls.append(text)
        self.spoken.set()

    def startLoop(self, useDriverLoop: bool = True) -> None:
        self.driver_loop = useDriverLoop

    def endLoop(self) -> None:
        self.end_loop_called.set()

    def iterate(self) -> None:
        if self._finished:
            return
        self._ticks += 1
        if self._ticks > 400:  # страховка, чтобы тест не завис
            self._finish(completed=True)
        time.sleep(0.005)

    def stop(self) -> None:
        self.stop_thread = threading.current_thread()
        self.stop_called.set()
        self._finish(completed=False)

    def _finish(self, completed: bool) -> None:
        if self._finished:
            return
        self._finished = True
        for cb in self._connects.get("finished-utterance", []):
            try:
                cb(name="utt", completed=completed)
            except TypeError:
                cb()


class SpeakerQueueTests(unittest.TestCase):
    """Очередь, эпоха отмены и FIFO."""

    def _recorder(self):
        spoken: list = []
        lock = threading.Lock()

        def fake(text, cancel=None):
            with lock:
                spoken.append(text)
            return True

        return spoken, fake

    def test_fifo_of_several_queued_phrases(self):
        speaker = tts.Speaker(enabled=True, engine="sapi")
        spoken, fake = self._recorder()
        speaker._speak = fake
        for phrase in ("один", "два", "три", "четыре"):
            speaker.say(phrase)
        self.assertTrue(_wait_idle(speaker, 5.0), "очередь не доиграла")
        # Ключевая регрессия: раньше say() повышал эпоху, и оставалась лишь
        # последняя фраза. Теперь звучат все и по порядку.
        self.assertEqual(spoken, ["один", "два", "три", "четыре"])
        speaker.shutdown(timeout=1.0)

    def test_concurrent_say_no_phrase_dropped(self):
        speaker = tts.Speaker(enabled=True, engine="sapi")
        spoken, fake = self._recorder()
        speaker._speak = fake
        phrases = [f"фраза-{i}" for i in range(24)]
        barrier = threading.Barrier(4)

        def producer(chunk):
            barrier.wait()
            for phrase in chunk:
                speaker.say(phrase)

        threads = [threading.Thread(target=producer, args=(phrases[i::4],))
                   for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(_wait_idle(speaker, 10.0), "очередь не доиграла")
        self.assertEqual(len(spoken), len(phrases))
        self.assertEqual(sorted(spoken), sorted(phrases))
        speaker.shutdown(timeout=1.0)

    def test_stop_then_new_phrase_is_spoken(self):
        speaker = tts.Speaker(enabled=True, engine="sapi")
        spoken, fake = self._recorder()
        speaker._speak = fake
        speaker.say("первая")
        self.assertTrue(_wait_idle(speaker, 5.0))
        speaker.stop()
        speaker.say("вторая")
        self.assertTrue(_wait_idle(speaker, 5.0))
        # stop() повысил эпоху, но новая фраза взяла уже актуальную — звучит.
        self.assertEqual(spoken, ["первая", "вторая"])
        speaker.shutdown(timeout=1.0)

    def test_stop_drops_queued_phrases(self):
        speaker = tts.Speaker(enabled=True, engine="sapi")
        entered = threading.Event()
        release = threading.Event()
        spoken: list = []

        def fake(text, cancel=None):
            spoken.append(text)
            entered.set()
            release.wait(5.0)
            return not cancel.is_set()

        speaker._speak = fake
        speaker.say("первая")
        self.assertTrue(entered.wait(5.0), "первая фраза не началась")
        speaker.say("вторая")
        speaker.say("третья")
        speaker.stop()
        release.set()
        self.assertTrue(_wait_idle(speaker, 5.0))
        self.assertEqual(spoken, ["первая"])
        speaker.shutdown(timeout=1.0)


class BusyInvariantTests(unittest.TestCase):
    """busy не должен мигать и держится до конца активной фразы."""

    def test_busy_stays_true_until_active_playback_ends(self):
        speaker = tts.Speaker(enabled=True, engine="sapi")
        entered = threading.Event()
        release = threading.Event()

        def fake(text, cancel=None):
            entered.set()
            release.wait(5.0)
            return not cancel.is_set()

        speaker._speak = fake
        self.assertFalse(speaker.busy)
        self.assertIsNone(speaker._active)
        speaker.say("долгая фраза")
        self.assertTrue(speaker.busy, "busy должен быть True сразу после say")
        self.assertTrue(entered.wait(5.0), "фраза не началась")
        self.assertTrue(speaker.busy)
        self.assertIsNotNone(speaker._active)
        release.set()
        self.assertTrue(_wait_idle(speaker, 5.0))
        self.assertFalse(speaker.busy)
        self.assertIsNone(speaker._active)
        speaker.shutdown(timeout=1.0)

    def test_busy_remains_true_after_stop_until_worker_confirms(self):
        speaker = tts.Speaker(enabled=True, engine="sapi")
        entered = threading.Event()
        release = threading.Event()

        def fake(text, cancel=None):
            entered.set()
            release.wait(5.0)
            return not cancel.is_set()

        speaker._speak = fake
        speaker.say("отменяемая")
        self.assertTrue(entered.wait(5.0))
        speaker.stop()
        # Активная фраза ещё не подтвердила остановку — busy обязан остаться True.
        self.assertTrue(speaker.busy, "busy не должен падать сразу после stop")
        release.set()
        self.assertTrue(_wait_idle(speaker, 5.0))
        self.assertFalse(speaker.busy)
        speaker.shutdown(timeout=1.0)


class CancellationReachTests(unittest.TestCase):
    """Отмена доходит до синтеза, воспроизведения и не течёт в резерв."""

    def test_stop_during_blocked_synthesis_no_playback_no_fallback(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        started = threading.Event()
        release = threading.Event()
        played: list = []
        fallback_calls = {"piper": 0, "sapi": 0}

        def fake_synthesize(text, out_path):
            started.set()
            release.wait(5.0)
            Path(out_path).write_bytes(b"\x00" * 32)

        speaker = tts.Speaker(enabled=True, engine="edge")
        speaker._speak_piper = lambda *a, **k: fallback_calls.__setitem__(
            "piper", fallback_calls["piper"] + 1)
        speaker._speak_sapi = lambda *a, **k: fallback_calls.__setitem__(
            "sapi", fallback_calls["sapi"] + 1)

        with mock.patch.object(tts, "_edge_synthesize", fake_synthesize), \
                mock.patch.object(tts, "play_audio_file",
                                  lambda *a, **k: played.append(a[0])), \
                mock.patch.object(config, "CACHE_DIR", Path(tmp)), \
                mock.patch.object(config, "TTS_USE_CACHE", False), \
                mock.patch.dict(config.FEATURES, {"edge_tts": True}):
            speaker.say("фраза")
            self.assertTrue(started.wait(5.0), "синтез не начался")
            speaker.stop()
            release.set()
            self.assertTrue(_wait_idle(speaker, 5.0), "оратор не освободился")

        self.assertEqual(played, [], "отменённую фразу нельзя проигрывать")
        self.assertEqual(fallback_calls, {"piper": 0, "sapi": 0},
                         "отмена не должна перетекать в резервный движок")
        speaker.shutdown(timeout=1.0)

    def test_stop_cuts_active_playback(self):
        speaker = tts.Speaker(enabled=False)
        with mock.patch.object(tts, "stop_playback") as cut:
            speaker.stop()
            cut.assert_called_once()

    def test_play_audio_file_raises_when_already_cancelled(self):
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(tts, "HAS_AV", True), \
                mock.patch.object(tts, "HAS_SD", True):
            with self.assertRaises(tts._SpeechCancelled):
                tts.play_audio_file(Path("нет-файла.mp3"), None, cancel)


class SapiCancellationTests(unittest.TestCase):
    """SAPI: engine.stop() обязан вызываться в рабочем потоке."""

    def test_sapi_cancel_on_owning_worker_thread(self):
        fake = _FakeSapiEngine()
        speaker = tts.Speaker(enabled=True, engine="sapi")
        speaker._sapi_engine = fake  # минуем реальный pyttsx3/COM
        speaker.say("привет")
        self.assertTrue(fake.spoken.wait(5.0), "SAPI не получил фразу")
        self.assertTrue(speaker.busy)
        speaker.stop()
        self.assertTrue(fake.stop_called.wait(5.0), "engine.stop() не вызван")
        self.assertIsNotNone(fake.stop_thread)
        self.assertIsNot(fake.stop_thread, threading.current_thread(),
                         "stop() нельзя звать из чужого потока (cross-thread COM)")
        self.assertEqual(fake.stop_thread.name, "tts")
        self.assertTrue(_wait_idle(speaker, 5.0))
        self.assertFalse(speaker.busy)
        self.assertTrue(fake.end_loop_called.wait(2.0), "endLoop() не вызван")
        speaker.shutdown(timeout=1.0)

    def test_sapi_returns_false_when_cancelled_before_start(self):
        fake = _FakeSapiEngine()
        speaker = tts.Speaker(enabled=True, engine="sapi")
        speaker._sapi_engine = fake
        cancel = threading.Event()
        cancel.set()
        self.assertFalse(speaker._speak_sapi("фраза", cancel))
        self.assertEqual(fake.say_calls, [])
        speaker.shutdown(timeout=1.0)


class PublishFlagTests(unittest.TestCase):
    """publish=False подавляет транскрипт, но не саму озвучку."""

    def test_disabled_text_mode_publish_flag(self):
        speaker = tts.Speaker(enabled=False)
        with mock.patch.object(tts, "emit_message") as emit:
            speaker.say("правка из веб-UI", publish=False)
            emit.assert_not_called()
            speaker.say("обычная реплика")
            emit.assert_called_once_with("assistant", "обычная реплика")
        self.assertFalse(speaker.busy)

    def test_publish_false_suppresses_transcript_but_speaks(self):
        speaker = tts.Speaker(enabled=True, engine="sapi")
        spoken: list = []
        speaker._speak = lambda text, cancel=None: (spoken.append(text), True)[1]
        with mock.patch.object(tts, "emit_message") as emit:
            speaker.say("правка", publish=False)
            self.assertTrue(_wait_idle(speaker, 5.0))
            emit.assert_not_called()
        self.assertEqual(spoken, ["правка"])
        speaker.shutdown(timeout=1.0)


class ShutdownTests(unittest.TestCase):
    """shutdown дожидается очереди и закрывает оратора для новых фраз."""

    def test_shutdown_drains_farewell(self):
        speaker = tts.Speaker(enabled=True, engine="sapi")
        spoken: list = []
        speaker._speak = lambda text, cancel=None: (spoken.append(text), True)[1]
        speaker.say("До встречи, сэр")
        speaker.shutdown(timeout=5.0)
        self.assertEqual(spoken, ["До встречи, сэр"],
                         "прощальную фразу нельзя выбрасывать")
        self.assertFalse(speaker.busy)
        self.assertFalse(speaker._running)

    def test_no_enqueue_or_restart_after_shutdown(self):
        speaker = tts.Speaker(enabled=True, engine="sapi")
        spoken: list = []
        speaker._speak = lambda text, cancel=None: (spoken.append(text), True)[1]
        speaker.shutdown(timeout=2.0)
        thread = speaker._thread
        speaker.say("поздняя фраза")
        speaker.start()
        self.assertEqual(spoken, [])
        self.assertFalse(speaker.busy)
        self.assertFalse(speaker._running)
        if thread is not None:
            self.assertFalse(thread.is_alive(), "рабочий поток должен быть завершён")

    def test_shutdown_bounded_when_synthesis_hangs(self):
        speaker = tts.Speaker(enabled=True, engine="sapi")
        release = threading.Event()
        entered = threading.Event()

        def stuck(text, cancel=None):
            entered.set()
            release.wait(30.0)
            return False

        speaker._speak = stuck
        speaker.say("зависшая фраза")
        self.assertTrue(entered.wait(5.0))
        started = time.monotonic()
        speaker.shutdown(timeout=0.5)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0, "shutdown обязан укладываться в таймаут")
        release.set()
        self.assertTrue(_wait_idle(speaker, 5.0))


class ApiCompatTests(unittest.TestCase):
    """Сохранённые точки входа."""

    def test_speak_accepts_single_argument(self):
        speaker = tts.Speaker(enabled=True, engine="sapi")
        called: list = []
        speaker._speak_sapi = lambda text, cancel=None: (called.append(text), True)[1]
        self.assertTrue(speaker._speak("одна фраза"))
        self.assertEqual(called, ["одна фраза"])
        speaker.shutdown(timeout=1.0)

    def test_emit_level_present(self):
        self.assertTrue(hasattr(tts.Speaker(enabled=False), "_emit_level"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
