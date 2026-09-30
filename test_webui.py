# -*- coding: utf-8 -*-
"""Тесты веб-интерфейса Джарвиса.

Поднимают настоящий HTTP-сервер на 127.0.0.1 с эфемерным портом и бьют по нему
реальными запросами. Ядро подменено заглушкой: никаких команд ОС, звука и сети,
кроме самого loopback.

Проверяется ровно то, что чинили:

* сервер не пускает чужие ``Host``/``Origin``/``Sec-Fetch-Site``;
* POST принимается только как ``application/json`` с валидным телом;
* отклонённый запрос не доходит до обработчика;
* реплики исполняет один FIFO-диспетчер, а не поток на сообщение;
* переполнение очереди даёт ``429`` до подтверждения;
* ``stop``/``start`` завершаются конечно и повторно;
* правка ответа озвучивается с ``publish=False`` (без дубля реплики).

Запуск: ``python test_webui.py`` (или ``python -m unittest test_webui``).
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import unittest

import webui


# =====================================================================
#  ЗАГЛУШКИ ЯДРА
# =====================================================================
class DummySpeaker:
    """Озвучка-заглушка: только запоминает, что и как просили сказать."""

    def __init__(self):
        self._lock = threading.Lock()
        self.said: list = []          # пары (текст, publish)
        self.stop_calls = 0
        self.busy = False

    def say(self, text, *, publish=True, block=False):  # noqa: A003
        with self._lock:
            self.said.append((text, publish))

    def stop(self):
        with self._lock:
            self.stop_calls += 1
        self.busy = False

    def shutdown(self):
        pass

    def wait_until_done(self, timeout=None):
        pass


class DummyJarvis:
    """Ядро-заглушка: считает вызовы и умеет притормозить первый из них."""

    def __init__(self, gate: threading.Event | None = None):
        self.speaker = DummySpeaker()
        self.gate = gate
        self.entered = threading.Event()      # рабочий поток взял первое сообщение
        self._lock = threading.Lock()
        self.processed: list = []
        self.resets = 0
        self._active = 0
        self.max_active = 0

    def handle(self, text):
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            self.entered.set()
            if self.gate is not None:
                self.gate.wait(5.0)
            with self._lock:
                self.processed.append(text)
            self.speaker.say("ответ: " + text)
        finally:
            with self._lock:
                self._active -= 1
        return "ответ: " + text

    def reset_dialog(self):
        with self._lock:
            self.resets += 1

    def status(self):
        return "dummy"


# =====================================================================
#  ВСПОМОГАТЕЛЬНОЕ
# =====================================================================
def wait_for(predicate, timeout=5.0, interval=0.01) -> bool:
    """Ждёт выполнения условия. True — дождались."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def http_call(port, method, path, body=None, headers=None,
              content_type="application/json", timeout=5.0):
    """Один запрос через http.client. Возвращает (статус, сырое тело)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        hdrs = dict(headers or {})
        payload = None
        if body is not None:
            if isinstance(body, (dict, list)):
                payload = json.dumps(body).encode("utf-8")
            elif isinstance(body, str):
                payload = body.encode("utf-8")
            else:
                payload = body
            hdrs.setdefault("Content-Type", content_type)
        conn.request(method, path, body=payload, headers=hdrs)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def raw_exchange(port, request_bytes, timeout=5.0) -> bytes:
    """Шлёт сырые байты в сокет и читает ответ до закрытия."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(request_bytes)
        sock.settimeout(timeout)
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


def build_raw(port, extra_headers, body=b"", path="/api/message", method="POST",
              host=None) -> bytes:
    """Собирает сырой HTTP/1.0 запрос с произвольными заголовками."""
    lines = [f"{method} {path} HTTP/1.0"]
    if host is not None:
        lines.append(f"Host: {host}")
    lines.extend(extra_headers)
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
    return head + body


def status_of(raw: bytes) -> int:
    line = raw.split(b"\r\n", 1)[0].decode("latin-1")
    parts = line.split()
    return int(parts[1]) if len(parts) >= 2 else 0


# =====================================================================
#  БАЗА
# =====================================================================
class WebUITestCase(unittest.TestCase):
    def setUp(self):
        self._uis = []
        # Лог интерфейса в тестах не нужен — глушим, чтобы вывод был чистым.
        self._real_log = webui.log
        webui.log = lambda *args, **kwargs: None

    def tearDown(self):
        for ui in self._uis:
            try:
                ui.stop()
            except Exception:  # noqa: BLE001
                pass
        webui.log = self._real_log

    def start_ui(self, jarvis=None, **kwargs):
        jarvis = jarvis if jarvis is not None else DummyJarvis()
        kwargs.setdefault("port", 0)
        kwargs.setdefault("open_browser", False)
        ui = webui.WebUI(jarvis, **kwargs)
        self.assertTrue(ui.start(), "сервер не поднялся")
        self._uis.append(ui)
        return ui, jarvis

    def post(self, ui, path, body, headers=None, content_type="application/json"):
        return http_call(ui.port, "POST", path, body=body, headers=headers,
                         content_type=content_type)


# =====================================================================
#  БАЗОВОЕ
# =====================================================================
class TestBasics(WebUITestCase):
    def test_ephemeral_port_and_health(self):
        ui, _ = self.start_ui()
        self.assertGreater(ui.port, 0, "порт=0 должен занять эфемерный порт")
        self.assertNotEqual(ui.port, 0)
        status, body = http_call(ui.port, "GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_host_normalization_rejects_wildcard(self):
        ui, _ = self.start_ui(host="0.0.0.0")
        self.assertEqual(ui.host, "127.0.0.1")
        self.assertGreater(ui.port, 0)
        status, _ = http_call(ui.port, "GET", "/api/health")
        self.assertEqual(status, 200)

    def test_index_served(self):
        ui, _ = self.start_ui()
        status, body = http_call(ui.port, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<", body)

    def test_stop_is_idempotent(self):
        ui, _ = self.start_ui()
        ui.stop()
        ui.stop()  # повторная остановка не должна бросать
        self.assertFalse(ui.running)


# =====================================================================
#  HOST / ORIGIN / SEC-FETCH-SITE
# =====================================================================
class TestOriginGuards(WebUITestCase):
    def test_same_origin_accepted(self):
        ui, _ = self.start_ui()
        origin = f"http://127.0.0.1:{ui.port}"
        status, body = self.post(ui, "/api/interrupt", {},
                                 headers={"Origin": origin})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_absent_origin_accepted(self):
        ui, jarvis = self.start_ui()
        status, _ = self.post(ui, "/api/message", {"text": "привет"})
        self.assertEqual(status, 200)
        self.assertTrue(wait_for(lambda: jarvis.processed == ["привет"]))

    def test_foreign_host_rejected(self):
        ui, jarvis = self.start_ui()
        status, _ = self.post(ui, "/api/message", {"text": "x"},
                              headers={"Host": "evil.example.com"})
        self.assertEqual(status, 403)
        self.assertEqual(jarvis.processed, [])

    def test_host_port_mismatch_rejected(self):
        ui, _ = self.start_ui()
        status, _ = self.post(ui, "/api/message", {"text": "x"},
                              headers={"Host": f"127.0.0.1:{ui.port + 1}"})
        self.assertEqual(status, 403)

    def test_foreign_origin_rejected(self):
        ui, jarvis = self.start_ui()
        for origin in ("http://evil.example.com",
                       f"http://evil.example.com:{ui.port}",
                       "null",
                       f"http://127.0.0.1:{ui.port + 1}"):
            status, _ = self.post(ui, "/api/message", {"text": "x"},
                                  headers={"Origin": origin})
            self.assertEqual(status, 403, f"Origin={origin!r} должен быть отклонён")
        self.assertEqual(jarvis.processed, [])

    def test_cross_site_fetch_rejected(self):
        ui, jarvis = self.start_ui()
        status, _ = self.post(ui, "/api/message", {"text": "x"},
                              headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        self.assertEqual(jarvis.processed, [])

    def test_guard_applies_to_get(self):
        ui, _ = self.start_ui()
        status, _ = http_call(ui.port, "GET", "/api/state",
                              headers={"Host": "evil.example.com"})
        self.assertEqual(status, 403)


# =====================================================================
#  ТЕЛО ЗАПРОСА
# =====================================================================
class TestBodyValidation(WebUITestCase):
    def test_content_type_required(self):
        ui, jarvis = self.start_ui()
        status, _ = self.post(ui, "/api/message", '{"text": "x"}',
                              content_type="text/plain")
        self.assertEqual(status, 415)
        status, _ = self.post(ui, "/api/message", '{"text": "x"}',
                              content_type="application/x-www-form-urlencoded")
        self.assertEqual(status, 415)
        self.assertEqual(jarvis.processed, [])

    def test_invalid_json_rejected(self):
        ui, jarvis = self.start_ui()
        for body in ("not json at all", "[1, 2, 3]", '"строка"', "42"):
            status, _ = self.post(ui, "/api/message", body)
            self.assertEqual(status, 400, f"тело {body!r} должно быть отклонено")
        self.assertEqual(jarvis.processed, [])

    def test_non_object_json_rejected(self):
        ui, _ = self.start_ui()
        status, _ = self.post(ui, "/api/message", b"\xff\xfe\x00")
        self.assertEqual(status, 400)  # не utf-8

    def test_wrong_text_type_rejected(self):
        ui, jarvis = self.start_ui()
        for value in (123, True, ["x"], {"a": 1}):
            status, _ = self.post(ui, "/api/message", {"text": value})
            self.assertEqual(status, 400, f"text={value!r} должен быть отклонён")
        self.assertEqual(jarvis.processed, [])

    def test_text_too_long_rejected(self):
        ui, jarvis = self.start_ui()
        status, _ = self.post(ui, "/api/message", {"text": "x" * (webui.MAX_TEXT + 1)})
        self.assertEqual(status, 413)
        self.assertEqual(jarvis.processed, [])

    def test_empty_text_rejected(self):
        ui, jarvis = self.start_ui()
        status, _ = self.post(ui, "/api/message", {"text": "   "})
        self.assertEqual(status, 400)
        self.assertEqual(jarvis.processed, [])

    def test_raw_invalid_content_length(self):
        ui, jarvis = self.start_ui()
        host = f"127.0.0.1:{ui.port}"

        raw = build_raw(ui.port, ["Content-Type: application/json",
                                  "Content-Length: abc"], body=b"{}", host=host)
        self.assertEqual(status_of(raw_exchange(ui.port, raw)), 400)

        raw = build_raw(ui.port, ["Content-Type: application/json"], host=host)
        self.assertEqual(status_of(raw_exchange(ui.port, raw)), 411)

        raw = build_raw(ui.port, ["Content-Type: application/json",
                                  "Content-Length: 0"], body=b"", host=host)
        self.assertEqual(status_of(raw_exchange(ui.port, raw)), 400)

        raw = build_raw(ui.port, ["Content-Type: application/json",
                                  "Content-Length: -5"], body=b"", host=host)
        self.assertEqual(status_of(raw_exchange(ui.port, raw)), 400)

        too_big = webui.MAX_BODY + 1
        raw = build_raw(ui.port, ["Content-Type: application/json",
                                  f"Content-Length: {too_big}"], body=b"{}", host=host)
        self.assertEqual(status_of(raw_exchange(ui.port, raw)), 413)

        raw = build_raw(ui.port, ["Content-Type: application/json",
                                  "Transfer-Encoding: chunked"], body=b"0\r\n\r\n", host=host)
        self.assertEqual(status_of(raw_exchange(ui.port, raw)), 400)

        self.assertEqual(jarvis.processed, [])
        self.assertEqual(jarvis.speaker.said, [])

    def test_rejection_never_reaches_handler(self):
        ui, jarvis = self.start_ui()
        self.post(ui, "/api/message", {"text": "x"}, content_type="text/plain")
        self.post(ui, "/api/message", "broken", headers={"Host": "evil.example.com"})
        self.post(ui, "/api/reset", {"x": 1}, headers={"Origin": "http://evil.example.com"})
        time.sleep(0.2)
        self.assertEqual(jarvis.processed, [])
        self.assertEqual(jarvis.resets, 0)


# =====================================================================
#  FIFO-ДИСПЕТЧЕР
# =====================================================================
class TestDispatcher(WebUITestCase):
    def test_fifo_and_single_worker(self):
        gate = threading.Event()
        jarvis = DummyJarvis(gate=gate)
        ui, _ = self.start_ui(jarvis)

        for text in ("раз", "два", "три"):
            status, _ = self.post(ui, "/api/message", {"text": text})
            self.assertEqual(status, 200)

        self.assertTrue(jarvis.entered.wait(2.0), "рабочий поток не стартовал")
        gate.set()
        self.assertTrue(wait_for(lambda: jarvis.processed == ["раз", "два", "три"]),
                        f"порядок нарушен: {jarvis.processed}")
        self.assertEqual(jarvis.max_active, 1, "реплики исполнялись параллельно")

    def test_reset_shares_queue_order(self):
        jarvis = DummyJarvis()
        ui, _ = self.start_ui(jarvis)
        self.post(ui, "/api/message", {"text": "первое"})
        self.post(ui, "/api/reset", {})
        self.post(ui, "/api/message", {"text": "второе"})
        self.assertTrue(wait_for(lambda: jarvis.processed == ["первое", "второе"]))
        self.assertTrue(wait_for(lambda: jarvis.resets == 1))

    def test_queue_saturation_returns_429(self):
        gate = threading.Event()
        jarvis = DummyJarvis(gate=gate)
        ui, _ = self.start_ui(jarvis, queue_size=2)

        first, _ = self.post(ui, "/api/message", {"text": "A"})
        self.assertEqual(first, 200)
        self.assertTrue(jarvis.entered.wait(2.0))

        second, _ = self.post(ui, "/api/message", {"text": "B"})
        third, _ = self.post(ui, "/api/message", {"text": "C"})
        overflow, body = self.post(ui, "/api/message", {"text": "D"})
        self.assertEqual((second, third), (200, 200))
        self.assertEqual(overflow, 429)
        self.assertFalse(json.loads(body)["ok"])

        gate.set()
        self.assertTrue(wait_for(lambda: jarvis.processed == ["A", "B", "C"]))

    def test_stop_restart_finite(self):
        ui, jarvis = self.start_ui()
        self.post(ui, "/api/message", {"text": "один"})
        self.assertTrue(wait_for(lambda: jarvis.processed == ["один"]))

        ui.stop()
        self.assertFalse(ui.running)
        self.assertFalse(ui.dispatcher.running)

        self.assertTrue(ui.start(), "повторный старт не удался")
        self.post(ui, "/api/message", {"text": "два"})
        self.assertTrue(wait_for(lambda: jarvis.processed == ["один", "два"]))

    def test_stop_drops_pending(self):
        gate = threading.Event()
        jarvis = DummyJarvis(gate=gate)
        ui, _ = self.start_ui(jarvis)

        self.post(ui, "/api/message", {"text": "A"})
        self.assertTrue(jarvis.entered.wait(2.0))
        self.post(ui, "/api/message", {"text": "B"})   # копится в очереди

        ui.stop()
        gate.set()
        time.sleep(0.3)
        self.assertNotIn("B", jarvis.processed, "отложенная реплика не должна исполниться")


# =====================================================================
#  ОЗВУЧКА ПРАВКИ
# =====================================================================
class TestSpeak(WebUITestCase):
    def test_speak_uses_publish_false(self):
        ui, jarvis = self.start_ui()
        status, _ = self.post(ui, "/api/speak", {"text": "исправленный ответ"})
        self.assertEqual(status, 200)
        self.assertEqual(jarvis.speaker.said, [("исправленный ответ", False)])

    def test_interrupt_calls_speaker_stop(self):
        ui, jarvis = self.start_ui()
        status, _ = self.post(ui, "/api/interrupt", {})
        self.assertEqual(status, 200)
        self.assertEqual(jarvis.speaker.stop_calls, 1)


# =====================================================================
#  СОВМЕСТИМОСТЬ
# =====================================================================
class TestLegacyApi(WebUITestCase):
    def test_submit_message_without_dispatcher(self):
        jarvis = DummyJarvis()
        accepted = webui.submit_message(jarvis, "старый вызов")
        self.assertTrue(accepted)
        self.assertTrue(wait_for(lambda: jarvis.processed == ["старый вызов"]))
        dispatcher = webui._legacy_dispatchers.get(id(jarvis))
        if dispatcher is not None:
            dispatcher.stop()

    def test_resolve_static_unchanged(self):
        self.assertEqual(webui.resolve_static("/").name, "index.html")
        self.assertIsNone(webui.resolve_static("/../config.py"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
