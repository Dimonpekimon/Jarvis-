# -*- coding: utf-8 -*-
"""Веб-интерфейс Джарвиса: ядро и диалог в браузере.

Поднимает крошечный HTTP-сервер на localhost и отдаёт страницу
``web/index.html``. Состояние ассистента уходит в браузер через Server-Sent
Events, текст из интерфейса возвращается в ядро. Используется только
стандартная библиотека — никаких новых зависимостей, и наружу ничего не уходит.

Безопасность. Сервер слушает только петлевой адрес (127.0.0.1/localhost),
никаких ``0.0.0.0``. Каждый запрос сверяется с реально занятым портом:

* ``Host`` обязан указывать на loopback и наш порт;
* ``Sec-Fetch-Site: cross-site`` и чужой ``Origin`` отклоняются;
* POST принимается только как ``application/json`` с корректным
  ``Content-Length`` и телом-объектом — значит, сторонний сайт не может
  отправить «простой» кросс-доменный запрос и выполнить команду в ОС.

Реплики пользователя исполняет единственный FIFO-диспетчер на весь сервер,
а не поток на каждое сообщение: команды не перемешиваются, а при
переполнении очередь честно отвечает ``429`` до подтверждения.

Адреса:

===========================  ===============================================
``GET  /``                   страница интерфейса
``GET  /api/health``         сервер жив? (по нему интерфейс отличает демо-режим)
``GET  /api/state``          текущее состояние и статус систем
``GET  /api/stream``         поток событий (SSE)
``POST /api/message``        ``{"text": "..."}`` — реплика пользователя
``POST /api/speak``          ``{"text": "..."}`` — озвучить правку ответа
``POST /api/interrupt``      прервать текущую фразу
``POST /api/reset``          очистить историю диалога
===========================  ===============================================

Запуск только интерфейса (без микрофона): ``python webui.py``.
"""

from __future__ import annotations

import argparse
import json
import queue
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import config
import events
from events import bus, emit_message, emit_state
from utils import log

WEB_DIR = Path(config.BASE_DIR) / "web"
INDEX_FILE = WEB_DIR / "index.html"

MAX_BODY = 64 * 1024        # больше этого от интерфейса прийти не может
MAX_TEXT = 4 * 1024         # предел длины одной реплики
SSE_PING_SEC = 15.0         # пустой комментарий, чтобы соединение не уснуло
PORT_ATTEMPTS = 10          # сколько портов перебрать, если основной занят
READ_TIMEOUT = 10.0         # сколько ждать запрос/тело, прежде чем отвалиться
QUEUE_MAX = 8               # сколько реплик ждут обработки в очереди
STOP_TIMEOUT = 2.0          # сколько ждать FIFO-диспетчер при остановке

# Единственные имена, которые сервер считает «своими». Никаких масок.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


# =====================================================================
#  ВСПОМОГАТЕЛЬНОЕ
# =====================================================================
def resolve_static(request_path: str):
    """Отдаёт файл из ``web/``, не выпуская запрос за пределы папки.

    Возвращает ``Path`` или ``None``. Проверка через ``relative_to`` нужна,
    чтобы запрос вида ``/../../config.py`` не ушёл наружу.
    """
    relative = (request_path or "").split("?", 1)[0].lstrip("/") or "index.html"
    root = WEB_DIR.resolve()
    try:
        candidate = (root / relative).resolve()
        candidate.relative_to(root)
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def safe_status(jarvis) -> str:
    """Статус систем — интерфейс не должен падать из-за проблем в ядре."""
    try:
        return jarvis.status()
    except Exception as exc:  # noqa: BLE001
        return f"Статус недоступен: {exc}"


def interrupt(jarvis) -> None:
    """Прерывает текущую фразу и очищает очередь озвучки."""
    try:
        jarvis.speaker.stop()
    except Exception as exc:  # noqa: BLE001
        log(f"Не удалось прервать озвучку: {exc}", "warn")


def speak_text(jarvis, text: str) -> None:
    """Озвучивает текст, отредактированный пользователем в интерфейсе.

    ``publish=False`` просит озвучку не публиковать реплику ассистента заново:
    текст уже показан в интерфейсе, и повторное событие давало дубль.
    """
    try:
        jarvis.speaker.say(text, publish=False)
    except Exception as exc:  # noqa: BLE001
        log(f"Не удалось озвучить правку: {exc}", "warn")


def normalize_host(host: str | None) -> str:
    """Оставляет только петлевой адрес. Маски (0.0.0.0, ::, '') запрещены."""
    candidate = (host or "").strip().lower()
    if candidate in LOOPBACK_HOSTS:
        return candidate
    if candidate:
        log(f"Хост интерфейса «{host}» не локальный — слушаю только 127.0.0.1.", "warn")
    return "127.0.0.1"


def _split_authority(value: str | None):
    """Делит ``host[:port]`` (в т.ч. ``[::1]:port``) на (host, port)."""
    if not value:
        return None, None
    value = value.strip()
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return None, None
        host = value[1:end].lower()
        rest = value[end + 1:]
        port = rest[1:] if rest.startswith(":") else None
        return host, port
    if ":" in value:
        host, _, port = value.rpartition(":")
        return host.lower(), port
    return value.lower(), None


def _origin_allowed(origin: str, expected_port: int) -> bool:
    """Origin считается своим, только если это тот же loopback и порт."""
    try:
        parts = urlsplit(origin.strip())
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    if (parts.hostname or "").lower() not in LOOPBACK_HOSTS:
        return False
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return port == expected_port


# =====================================================================
#  FIFO-ДИСПЕТЧЕР
# =====================================================================
class Dispatcher:
    """Единственный исполнитель реплик для одного сервера.

    Раньше на каждое сообщение создавался поток, и несколько команд
    выполнялись одновременно, перемешивая диалог и озвучку. Теперь команды
    кладутся в ограниченную очередь и исполняются строго по одной: ``reset``
    и ``message`` идут в общем порядке, поэтому сброс не рвёт обработку.

    Очередь ограничена: переполнение возвращает ``False``, и HTTP-слой
    отвечает ``429`` ещё до подтверждения — работа не принимается молча.
    """

    def __init__(self, jarvis, maxsize: int = QUEUE_MAX):
        self._jarvis = jarvis
        self._queue: "queue.Queue[tuple]" = queue.Queue(maxsize=max(1, int(maxsize)))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive() and not self._stop.is_set())

    def start(self) -> None:
        """Запускает рабочий поток. Повторный вызов безопасен."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and not self._stop.is_set():
                return
            # Новый Event на каждую жизнь: старый поток видит свой (взведённый)
            # и завершается, не путая состояние нового.
            stop = threading.Event()
            self._stop = stop
            self._thread = threading.Thread(
                target=self._run, args=(stop,), name="webui-dispatch", daemon=True)
            self._thread.start()

    def submit_message(self, text: str) -> bool:
        """Ставит реплику в очередь. False — очередь переполнена."""
        return self._put(("message", text))

    def submit_reset(self) -> bool:
        """Ставит сброс диалога в ту же очередь. False — очередь переполнена."""
        return self._put(("reset", None))

    def _put(self, item: tuple) -> bool:
        if self._stop.is_set():
            return False
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            return False

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                kind, payload = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if stop.is_set():
                break  # остановка — не начинаем отложенную работу
            try:
                if kind == "message":
                    self._jarvis.handle(payload)
                elif kind == "reset":
                    self._jarvis.reset_dialog()
            except Exception as exc:  # noqa: BLE001
                log(f"Ошибка обработки реплики из интерфейса: {exc}", "error")
                emit_message("system", f"Не удалось обработать: {exc}")
                emit_state("idle")

    def stop(self, timeout: float = STOP_TIMEOUT) -> None:
        """Гасит диспетчер, выбрасывая непринятое в работу.

        Если остановку вызвал сам рабочий поток (команда «выключайся» шла из
        очереди), себя не ждём — иначе был бы вечный дедлок.
        """
        with self._lock:
            stop, thread = self._stop, self._thread
            stop.set()
            self._drain()
        if thread is None or not thread.is_alive():
            return
        if threading.current_thread() is thread:
            return
        thread.join(timeout)

    def _drain(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break


# Совместимость: старые вызовы ``submit_message(jarvis, text)`` без диспетчера.
_legacy_lock = threading.Lock()
_legacy_dispatchers: "dict[int, Dispatcher]" = {}


def _legacy_dispatcher(jarvis) -> Dispatcher:
    key = id(jarvis)
    with _legacy_lock:
        dispatcher = _legacy_dispatchers.get(key)
        if dispatcher is None or not dispatcher.running:
            dispatcher = Dispatcher(jarvis)
            dispatcher.start()
            _legacy_dispatchers[key] = dispatcher
        return dispatcher


def submit_message(jarvis, text: str, dispatcher: Dispatcher | None = None) -> bool:
    """Публичная точка: реплика уходит в FIFO-очередь диспетчера.

    ``True`` — принято, ``False`` — очередь переполнена (HTTP-слой отдаст 429).
    Без явного диспетчера используется общий для этого ядра — так сохраняется
    прежний вызов ``submit_message(jarvis, text)``.
    """
    if dispatcher is None:
        dispatcher = _legacy_dispatcher(jarvis)
    return dispatcher.submit_message(text)


# =====================================================================
#  HTTP
# =====================================================================
class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _make_handler(jarvis, dispatcher: Dispatcher):
    """Собирает класс обработчика, замкнутый на ассистента и диспетчер."""

    class JarvisHandler(BaseHTTPRequestHandler):
        # HTTP/1.0 + явный Content-Length: тело заканчивается концом соединения,
        # поэтому SSE-поток работает без возни с chunked-кодированием.
        protocol_version = "HTTP/1.0"
        server_version = "JarvisUI/3.2"
        timeout = READ_TIMEOUT  # предел ожидания запроса и тела от клиента

        def log_message(self, fmt, *args):  # noqa: A003
            log(f"[ui] {fmt % args}", "debug")

        # ---------------- ответы ----------------
        def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, payload, status: int = 200) -> None:
            self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", status)

        # ---------------- защита от чужих сайтов ----------------
        def _local_request_reason(self) -> str | None:
            """Причина отказа или ``None``, если запрос свой.

            Проверяем ``Host`` (loopback + фактический порт), ``Sec-Fetch-Site``
            и ``Origin``. Отсутствие ``Origin`` — норма для нативных клиентов.
            """
            actual_port = int(self.server.server_address[1])
            host, port = _split_authority(self.headers.get("Host"))
            if host is None:
                return "нет заголовка Host"
            if host not in LOOPBACK_HOSTS:
                return f"чужой Host: {host!r}"
            if port is None or not port.isdigit() or int(port) != actual_port:
                return f"порт в Host не совпадает: {port!r} != {actual_port}"

            site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
            if site == "cross-site":
                return "Sec-Fetch-Site: cross-site"

            origin = self.headers.get("Origin")
            if origin is not None and origin.strip() and not _origin_allowed(origin, actual_port):
                return f"чужой Origin: {origin!r}"
            return None

        def _guard(self) -> bool:
            reason = self._local_request_reason()
            if reason is None:
                return True
            log(f"[ui] запрос отклонён: {reason}", "warn")
            self._json({"ok": False, "error": "forbidden"}, 403)
            return False

        # ---------------- разбор тела ----------------
        def _read_json_body(self):
            """Читает тело строго как JSON-объект.

            Возвращает ``(payload, error, status)``; при ошибке ``payload`` —
            ``None``, а обработчики не вызываются.
            """
            encoding = (self.headers.get("Transfer-Encoding") or "").strip().lower()
            if encoding and encoding != "identity":
                return None, "unsupported transfer-encoding", 400

            content_type = self.headers.get("Content-Type") or ""
            media = content_type.split(";", 1)[0].strip().lower()
            if media != "application/json":
                return None, "content-type must be application/json", 415

            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                return None, "content-length required", 411
            try:
                length = int(str(raw_length).strip())
            except (TypeError, ValueError):
                return None, "invalid content-length", 400
            if length <= 0:
                return None, "empty body", 400
            if length > MAX_BODY:
                return None, "body too large", 413

            try:
                body = self.rfile.read(length)
            except (OSError, socket.timeout):
                return None, "body read failed", 400
            if len(body) != length:
                return None, "incomplete body", 400
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                return None, "body is not utf-8", 400
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                return None, "invalid json", 400
            if not isinstance(payload, dict):
                return None, "json must be an object", 400
            return payload, None, 0

        def _extract_text(self, payload: dict):
            """Достаёт поле ``text``. Возвращает ``(text, error, status)``."""
            value = payload.get("text", "")
            if value is None:
                value = ""
            if not isinstance(value, str):
                return "", "text must be a string", 400
            value = value.strip()
            if len(value) > MAX_TEXT:
                return "", "text too long", 413
            return value, None, 0

        # ---------------- GET ----------------
        def do_GET(self):  # noqa: N802
            if not self._guard():
                return
            path = self.path.split("?", 1)[0]
            if path == "/api/health":
                self._json({"ok": True, "app": "jarvis", "version": "3.2"})
                return
            if path == "/api/state":
                self._json({"ok": True, "state": events.current_state(),
                            "status": safe_status(jarvis)})
                return
            if path == "/api/stream":
                self._stream()
                return
            if path == "/favicon.ico":
                self._send(b"", "image/x-icon", 204)
                return
            target = resolve_static(self.path)
            if target is None:
                self._json({"ok": False, "error": "not found"}, 404)
                return
            try:
                body = target.read_bytes()
            except OSError as exc:
                log(f"Не удалось прочитать {target.name}: {exc}", "warn")
                self._json({"ok": False, "error": "read error"}, 500)
                return
            self._send(body, _MIME.get(target.suffix.lower(), "application/octet-stream"))

        # ---------------- SSE ----------------
        def _stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            channel = bus.subscribe()
            try:
                self._event({"type": "hello", "state": events.current_state(),
                             "status": safe_status(jarvis)})
                while True:
                    try:
                        event = channel.get(timeout=SSE_PING_SEC)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    self._event(event)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass  # браузер закрыл вкладку — это норма
            finally:
                bus.unsubscribe(channel)

        def _event(self, payload: dict) -> None:
            # json.dumps экранирует переводы строк, поэтому формат SSE не ломается.
            data = json.dumps(payload, ensure_ascii=False)
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

        # ---------------- POST ----------------
        def do_POST(self):  # noqa: N802
            if not self._guard():
                return
            path = self.path.split("?", 1)[0]
            payload, error, status = self._read_json_body()
            if error is not None:
                self._json({"ok": False, "error": error}, status)
                return

            if path in ("/api/message", "/api/speak"):
                text, error, status = self._extract_text(payload)
                if error is not None:
                    self._json({"ok": False, "error": error}, status)
                    return
                if not text:
                    self._json({"ok": False, "error": "empty text"}, 400)
                    return
            else:
                text = ""

            if path == "/api/message":
                interrupt(jarvis)
                if not dispatcher.submit_message(text):
                    self._json({"ok": False, "error": "busy"}, 429)
                    return
                self._json({"ok": True})
                return
            if path == "/api/speak":
                self._json({"ok": True})
                speak_text(jarvis, text)
                return
            if path == "/api/interrupt":
                interrupt(jarvis)
                self._json({"ok": True})
                return
            if path == "/api/reset":
                if not dispatcher.submit_reset():
                    self._json({"ok": False, "error": "busy"}, 429)
                    return
                emit_message("system", "Контекст диалога сброшен.")
                self._json({"ok": True})
                return
            self._json({"ok": False, "error": "not found"}, 404)

    return JarvisHandler


# =====================================================================
#  ЗАПУСК
# =====================================================================
class WebUI:
    """HTTP-сервер интерфейса. Работает в отдельном потоке."""

    def __init__(self, jarvis, host: str | None = None, port: int | None = None,
                 open_browser: bool = True, queue_size: int = QUEUE_MAX):
        self.jarvis = jarvis
        self.host = normalize_host(host if host is not None else config.WEB_UI_HOST)
        self.port = int(port if port is not None else config.WEB_UI_PORT)
        self.open_browser = open_browser
        self.url = ""
        self.dispatcher = Dispatcher(jarvis, maxsize=queue_size)
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    def _bind(self, handler):
        """Занимает порт. ``port=0`` — эфемерный: узнаём фактический у сокета."""
        requested = self.port
        attempts = 1 if requested == 0 else PORT_ATTEMPTS
        last_error = None
        for offset in range(attempts):
            candidate = 0 if requested == 0 else requested + offset
            try:
                server = _Server((self.host, candidate), handler)
            except OSError as exc:
                last_error = exc
                continue
            self._server = server
            self.port = int(server.server_address[1])
            return True
        log(f"Не удалось занять порт для интерфейса: {last_error}", "warn")
        return False

    def start(self) -> bool:
        """Поднимает сервер. False — интерфейс недоступен, ассистент работает."""
        if self._server is not None:
            return True
        if not INDEX_FILE.exists():
            log(f"Нет файла интерфейса: {INDEX_FILE}", "warn")
            return False

        handler = _make_handler(self.jarvis, self.dispatcher)
        if not self._bind(handler):
            return False

        self.dispatcher.start()
        self.url = f"http://{self.host}:{self.port}/"
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="webui", daemon=True)
        self._thread.start()
        log(f"Интерфейс Джарвиса: {self.url}", "info")
        if self.open_browser:
            threading.Timer(0.8, self._open).start()
        return True

    def _open(self) -> None:
        try:
            webbrowser.open(self.url)
        except Exception as exc:  # noqa: BLE001
            log(f"Не удалось открыть браузер: {exc}", "debug")

    def stop(self) -> None:
        """Гасит сервер и диспетчер. Безопасно при повторном вызове."""
        server, self._server = self._server, None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception as exc:  # noqa: BLE001
                log(f"Остановка интерфейса: {exc}", "debug")
        self.dispatcher.stop()


def _stdin_available() -> bool:
    """Есть ли консоль для текстового ввода (нет — значит фоновый запуск)."""
    try:
        return sys.stdin is not None and sys.stdin.readable()
    except (ValueError, AttributeError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="jarvis-ui",
        description="Веб-интерфейс Джарвиса: ядро и диалог в браузере",
    )
    parser.add_argument("--port", type=int, help="порт (по умолчанию из настроек)")
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    parser.add_argument("--no-tts", action="store_true", help="без озвучки")
    args = parser.parse_args()

    from jarvis_core import Jarvis

    # Без консоли (pythonw/ярлык) текстовый ввод невозможен — работаем как фоновый:
    # управление идёт через веб-интерфейс, а не через пустой input().
    background = not _stdin_available()
    jarvis = Jarvis(mode="text", enable_tts=not args.no_tts, background=background)
    ui = WebUI(jarvis, port=args.port, open_browser=not args.no_browser)
    if not ui.start():
        return 1
    # Команда «выключайся» (голосом или из интерфейса) должна погасить и сервер.
    jarvis.add_shutdown_hook(ui.stop)
    try:
        jarvis.run()
    except KeyboardInterrupt:
        pass
    finally:
        ui.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
