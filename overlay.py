# -*- coding: utf-8 -*-
"""Запуск настольного оверлея (Electron) из main.py.

Python остаётся мозгом и голосом: он поднимает HTTP/SSE-API, а Electron — это
только окно. Поэтому оверлей запускается с JARVIS_NO_SIDECAR=1: второй экземпляр
ядра не нужен, иначе два процесса начнут делить микрофон.

Что делает модуль:
  * проверяет, есть ли Node.js и зависимости (desktop/node_modules);
  * собирает renderer/main, если сборки нет или исходники новее сборки;
  * запускает electron.exe и аккуратно гасит его при выходе ассистента.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import config
from utils import log

DESKTOP_DIR = Path(config.BASE_DIR) / "desktop"
DIST_MAIN = DESKTOP_DIR / "dist" / "main" / "main.js"
DIST_INDEX = DESKTOP_DIR / "dist" / "renderer" / "index.html"
NODE_MODULES = DESKTOP_DIR / "node_modules"

# Что считаем исходниками для проверки «сборка устарела».
SOURCE_SUFFIXES = {".ts", ".tsx", ".css", ".html", ".mts"}
SOURCE_DIRS = ("src", "scripts")


# --------------------------------------------------------------------- проверки

def node_version() -> str | None:
    """Версия Node.js или None, если его нет в PATH."""
    node = shutil.which("node")
    if not node:
        return None
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True,
                             timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    version = (out.stdout or "").strip()
    return version or None


def npm_path() -> str | None:
    return shutil.which("npm")


def electron_path() -> Path | None:
    """Путь к скачанному Electron внутри node_modules."""
    candidates = [
        NODE_MODULES / "electron" / "dist" / "electron.exe",
        NODE_MODULES / "electron" / "dist" / "electron",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def node_modules_ready() -> bool:
    return (NODE_MODULES / "electron").exists()


def is_built() -> bool:
    return DIST_MAIN.exists() and DIST_INDEX.exists()


def needs_rebuild() -> bool:
    """True, если сборки нет или исходники правились позже неё."""
    if not is_built():
        return True
    try:
        built_at = min(DIST_MAIN.stat().st_mtime, DIST_INDEX.stat().st_mtime)
    except OSError:
        return True
    for folder in SOURCE_DIRS:
        root = DESKTOP_DIR / folder
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
                continue
            try:
                if path.stat().st_mtime > built_at:
                    log(f"Сборка оверлея устарела: изменён {path.name}", "debug")
                    return True
            except OSError:
                continue
    return False


def status() -> dict:
    """Короткая сводка для --check."""
    return {
        "node": node_version(),
        "npm": npm_path(),
        "electron": electron_path(),
        "deps": node_modules_ready(),
        "built": is_built(),
        "stale": needs_rebuild() if is_built() else False,
    }


# ------------------------------------------------------------------- сборка

def _run(cmd: list[str], timeout: float | None = None) -> bool:
    """Запускает команду в папке desktop. Возвращает True при коде 0."""
    # .cmd/.bat в Windows не запускаются напрямую через CreateProcess.
    if os.name == "nt":
        cmd = ["cmd", "/c", *cmd]
    log("Собираю оверлей: " + " ".join(cmd), "info")
    try:
        result = subprocess.run(cmd, cwd=str(DESKTOP_DIR), timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"Не удалось выполнить {' '.join(cmd)}: {exc}", "error")
        return False
    if result.returncode != 0:
        log(f"Команда завершилась с кодом {result.returncode}", "error")
        return False
    return True


def ensure_built(rebuild: bool = False, install: bool = True) -> bool:
    """Готовит desktop/ к запуску. True — можно запускать Electron."""
    if not DESKTOP_DIR.exists():
        log("Папка desktop/ не найдена — оверлей недоступен", "error")
        return False
    if npm_path() is None:
        log("npm не найден. Установите Node.js 20+ с https://nodejs.org", "error")
        return False

    if install and not node_modules_ready():
        log("Первый запуск оверлея: ставлю зависимости (это займёт пару минут)", "info")
        if not _run(["npm", "install", "--no-audit", "--no-fund"]):
            log("Зависимости не установились — оверлей не запустится", "error")
            return False

    if rebuild or needs_rebuild():
        if not _run(["npm", "run", "build"]):
            log("Сборка оверлея не удалась", "error")
            return False

    if electron_path() is None:
        log("Electron не найден в node_modules — попробуйте удалить desktop/node_modules "
            "и запустить снова", "error")
        return False
    return True


# -------------------------------------------------------------------- запуск

class OverlayProcess:
    """Дочерний Electron. Гасится деревом процессов."""

    def __init__(self, process: subprocess.Popen) -> None:
        self.process = process
        self._stopped = False
        self._watcher = threading.Thread(target=self._watch, name="overlay-watch",
                                         daemon=True)
        self._watcher.start()

    @property
    def pid(self) -> int | None:
        return self.process.pid

    def _watch(self) -> None:
        code = self.process.wait()
        if self._stopped:
            return
        if code == 0:
            log("Оверлей закрыт пользователем", "info")
        else:
            log(f"Оверлей завершился с кодом {code}", "warn")

    def stop(self, timeout: float = 5.0) -> None:
        """Идемпотентная остановка: можно звать из хука выключения."""
        if self._stopped:
            return
        self._stopped = True
        if self.process.poll() is not None:
            return
        log("Закрываю оверлей", "info")
        try:
            if os.name == "nt":
                # Electron порождает дочерние процессы (GPU, renderer) — валим дерево.
                subprocess.run(["taskkill", "/pid", str(self.process.pid), "/T", "/F"],
                               capture_output=True, timeout=timeout, check=False)
            else:
                self.process.terminate()
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log("Оверлей не закрылся штатно", "warn")


def launch(api_url: str, disable_gpu: bool | None = None) -> OverlayProcess | None:
    """Запускает Electron, привязанный к уже работающему ядру."""
    electron = electron_path()
    if electron is None:
        log("Electron не установлен — оверлей не запустится", "error")
        return None

    env = os.environ.copy()
    # Эти переменные ломают Electron: с ELECTRON_RUN_AS_NODE он работает как
    # обычный Node (и падает на app.isPackaged), а NODE_OPTIONS подсовывает
    # сторонний --require в его рантайм.
    env.pop("ELECTRON_RUN_AS_NODE", None)
    env.pop("NODE_OPTIONS", None)
    env["JARVIS_NO_SIDECAR"] = "1"          # Python уже запущен — второй не нужен
    env["JARVIS_API"] = api_url.rstrip("/")

    args = [str(electron)]
    gpu_off = disable_gpu if disable_gpu is not None else \
        os.environ.get("JARVIS_DISABLE_GPU") == "1"
    if gpu_off:
        args.append("--disable-gpu")
    args.append(".")

    log(f"Запускаю оверлей: {api_url}", "info")
    try:
        process = subprocess.Popen(args, cwd=str(DESKTOP_DIR), env=env)
    except OSError as exc:
        log(f"Оверлей не запустился: {exc}", "error")
        return None
    return OverlayProcess(process)


def run(api_url: str, rebuild: bool = False) -> OverlayProcess | None:
    """Собрать (если нужно) и запустить. Одна строка для main.py."""
    if not ensure_built(rebuild=rebuild):
        return None
    return launch(api_url)


def main() -> int:
    """Ручной запуск: python overlay.py [--rebuild] — для отладки UI."""
    import argparse

    parser = argparse.ArgumentParser(description="Собрать и запустить оверлей")
    parser.add_argument("--rebuild", action="store_true", help="собрать заново")
    parser.add_argument("--api", default="http://127.0.0.1:8765",
                        help="адрес уже запущенного ядра")
    args = parser.parse_args()

    info = status()
    print(f"Node:     {info['node'] or 'не найден'}")
    print(f"npm:      {info['npm'] or 'не найден'}")
    print(f"Electron: {info['electron'] or 'не установлен'}")
    print(f"Сборка:   {'есть' if info['built'] else 'нет'}"
          f"{' (устарела)' if info['stale'] else ''}")

    process = run(args.api, rebuild=args.rebuild)
    if process is None:
        return 1
    print(f"Оверлей запущен, pid={process.pid}. Ctrl+C — закрыть.")
    try:
        process.process.wait()
    except KeyboardInterrupt:
        process.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
