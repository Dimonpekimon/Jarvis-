# -*- coding: utf-8 -*-
"""Запуск настольного оверлея (Electron) из main.py.

Python остаётся мозгом и голосом: он поднимает HTTP/SSE-API, а Electron — это
только окно. Поэтому оверлей запускается с PYATNITSA_NO_SIDECAR=1: второй экземпляр
ядра не нужен, иначе два процесса начнут делить микрофон.

Что делает модуль:
  * проверяет, есть ли Node.js и зависимости (desktop/node_modules);
  * собирает renderer/main, если сборки нет или исходники новее сборки;
  * запускает electron.exe и аккуратно гасит его при выходе ассистента.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterator

import config
from utils import log

DESKTOP_DIR = Path(config.BASE_DIR) / "desktop"
DIST_MAIN = DESKTOP_DIR / "dist" / "main" / "main.js"
DIST_INDEX = DESKTOP_DIR / "dist" / "renderer" / "index.html"
NODE_MODULES = DESKTOP_DIR / "node_modules"

# Что считаем исходниками для проверки «сборка устарела».
SOURCE_SUFFIXES = {".ts", ".tsx", ".css", ".html", ".mts"}
SOURCE_DIRS = ("src", "scripts")

NODE_EXE = "node.exe" if os.name == "nt" else "node"
NPM_NAMES = ("npm.cmd", "npm.bat", "npm.exe", "npm") if os.name == "nt" else ("npm",)


# ------------------------------------------------------------ поиск Node.js
#
# PATH — не единственный источник: установщик Node прописывает
# C:\Program Files\nodejs в системный PATH, и если папку потом удалили
# (обновление, «чистильщик», антивирус), npm «исчезает» при живом реестре.
# Поэтому дополнительно смотрим стандартные места установки и менеджеры версий.

def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def _node_install_dirs() -> Iterator[Path]:
    """Каталоги, где обычно лежит node.exe / npm.cmd."""
    seen: set[str] = set()

    def push(path: Path | None) -> Iterator[Path]:
        if path is None:
            return
        key = str(path).lower()
        if key in seen or not path.is_dir():
            return
        seen.add(key)
        yield path

    fixed = [
        (_env_path("ProgramFiles") or Path(r"C:\Program Files")) / "nodejs",
        (_env_path("ProgramFiles(x86)") or Path(r"C:\Program Files (x86)")) / "nodejs",
        (_env_path("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        / "Programs" / "nodejs",
        (_env_path("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Volta" / "bin",
        (_env_path("ProgramData") or Path(r"C:\ProgramData")) / "chocolatey" / "bin",
        (_env_path("USERPROFILE") or Path.home()) / "scoop" / "apps" / "nodejs" / "current",
        (_env_path("USERPROFILE") or Path.home()) / "scoop" / "shims",
    ]
    for folder in fixed:
        yield from push(folder)

    # nvm-windows: %APPDATA%\nvm\v24.20.0 — версии перебираем от новых к старым.
    versioned: list[Path] = []
    for root in (
        (_env_path("APPDATA") or Path.home() / "AppData" / "Roaming") / "nvm",
        (_env_path("ProgramData") or Path(r"C:\ProgramData")) / "nvm",
        (_env_path("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        / "fnm" / "node-versions",
    ):
        if not root.is_dir():
            continue
        try:
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                versioned.append(child)
                versioned.append(child / "installation")   # раскладка fnm
        except OSError:
            continue
    versioned.sort(key=lambda p: p.name, reverse=True)
    for folder in versioned:
        yield from push(folder)


def node_path() -> str | None:
    """Путь к node.exe: явная настройка → PATH → стандартные места."""
    explicit = os.environ.get("PYATNITSA_NODE")
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which("node")
    if found:
        return found
    for folder in _node_install_dirs():
        candidate = folder / NODE_EXE
        if candidate.exists():
            return str(candidate)
    return None


def node_version() -> str | None:
    """Версия Node.js или None, если его нет."""
    node = node_path()
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
    """Путь к npm. Ищем рядом с найденным node и в стандартных каталогах."""
    explicit = os.environ.get("PYATNITSA_NPM")
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which("npm")
    if found:
        return found

    folders: list[Path] = []
    node = node_path()
    if node:
        folders.append(Path(node).parent)
    folders.extend(_node_install_dirs())
    for folder in folders:
        for name in NPM_NAMES:
            candidate = folder / name
            if candidate.exists():
                return str(candidate)
    return None


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


def electron_package_installed() -> bool:
    return (NODE_MODULES / "electron" / "package.json").exists()


def _ensure_electron_binary() -> bool:
    """Докачивает бинарник Electron, если пакет есть, а файла ещё нет.

    `npm install` кладёт только JS-обёртку: сам Electron (~100 МБ) скачивается
    при первом обращении `require('electron')`. Без этого шага свежий клон
    выглядел бы как «Electron не установлен».
    """
    if electron_path() is not None:
        return True
    if not electron_package_installed():
        return False
    node = node_path()
    if node is None:
        log("node не найден — не могу скачать бинарник Electron", "error")
        return False
    log("Скачиваю бинарник Electron (~100 МБ, только первый раз)", "info")
    if not _run([node, "-e", "require('electron')"]):
        return False
    return electron_path() is not None


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
        "node_path": node_path(),
        "npm": npm_path(),
        "electron": electron_path(),
        "deps": node_modules_ready(),
        "built": is_built(),
        "stale": needs_rebuild() if is_built() else False,
        "ready": is_built() and electron_path() is not None,
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


NODE_HINT = ("Установите Node.js 20+ с https://nodejs.org (или укажите путь "
             "переменной PYATNITSA_NODE), затем запустите: python main.py --rebuild-desktop")


def ensure_built(rebuild: bool = False, install: bool = True) -> bool:
    """Готовит desktop/ к запуску. True — можно запускать Electron.

    npm нужен только для установки зависимостей и сборки. Если сборка уже
    готова, а electron.exe на месте, окно запускается и без Node: иначе
    сломанный/удалённый Node навсегда «убивал» интерфейс, хотя всё собрано.
    """
    if not DESKTOP_DIR.exists():
        log("Папка desktop/ не найдена — оверлей недоступен", "error")
        return False

    npm = npm_path()
    need_install = install and not node_modules_ready()
    need_build = rebuild or needs_rebuild()

    if (need_install or need_build) and npm is None:
        if is_built() and electron_path() is not None:
            log("npm не найден, но сборка окна уже готова — запускаю как есть. "
                "Для пересборки нужен Node.js 20+", "warn")
            return True
        log("npm не найден, а окно нужно собрать. " + NODE_HINT, "error")
        return False

    if need_install:
        log("Первый запуск оверлея: ставлю зависимости (это займёт пару минут)", "info")
        if not _run([npm, "install", "--no-audit", "--no-fund"]):   # type: ignore[list-item]
            log("Зависимости не установились — оверлей не запустится", "error")
            return False

    if need_build:
        if not _run([npm, "run", "build"]):   # type: ignore[list-item]
            log("Сборка оверлея не удалась", "error")
            return False

    if not _ensure_electron_binary():
        log("Electron не найден в node_modules — попробуйте удалить desktop/node_modules "
            "и запустить снова", "error")
        return False
    return True


# -------------------------------------------------------------------- запуск

class OverlayProcess:
    """Дочерний Electron. Гасится деревом процессов.

    Следит за процессом сам: если окно умирает на первых секундах (падение
    GPU-процесса), перезапускает его с флагами обхода и запоминает их. Это
    не блокирует запуск ассистента — голос стартует сразу.
    """

    def __init__(self, process: subprocess.Popen, api_url: str = "",
                 base_flags: list[str] | None = None,
                 allow_fallback: bool = True) -> None:
        self.process = process
        self._api_url = api_url
        self._base_flags = list(base_flags or [])
        self._allow_fallback = allow_fallback
        self._started_at = time.monotonic()
        self._stopped = False
        self._watcher = threading.Thread(target=self._watch, name="overlay-watch",
                                         daemon=True)
        self._watcher.start()

    @property
    def pid(self) -> int | None:
        return self.process.pid

    def _try_fallback(self, code: int) -> bool:
        """Перезапуск с обходом падения GPU. True — процесс заменён."""
        if not self._allow_fallback or self._stopped:
            return False
        if time.monotonic() - self._started_at > EARLY_CRASH_WINDOW:
            return False        # окно жило нормально — это обычное закрытие
        self._allow_fallback = False
        log(f"Окно закрылось через {time.monotonic() - self._started_at:.1f} с "
            f"(код {code}) — пробую обход падения GPU", "warn")
        flags = [*self._base_flags, *FALLBACK_FLAGS]
        process = _launch_once(self._api_url, flags)
        if process is None:
            return False
        self.process = process
        self._started_at = time.monotonic()
        _save_flags(FALLBACK_FLAGS)
        return True

    def _watch(self) -> None:
        while True:
            code = self.process.wait()
            if self._stopped:
                return
            if self._try_fallback(code):
                continue
            if code == 0:
                log("Оверлей закрыт пользователем", "info")
            else:
                log(f"Оверлей завершился с кодом {code}. "
                    "Проверьте окружение: python main.py --check", "warn")
            return

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


# --------------------------------------------------------------- запуск GPU
#
# На части машин GPU-процесс Electron падает с 0xC0000005 сразу после старта:
# под sandbox-ом в него инжектятся сторонние DLL (антивирус, оверлеи, «чистильщики»).
# Chromium после нескольких падений пишет «GPU process isn't usable. Goodbye» и
# закрывает всё приложение — снаружи это выглядит как «окно не запускается».
# Лечится флагом --disable-gpu-sandbox: рендер остаётся аппаратным, sandbox
# снимается только с GPU-процесса.

GPU_SANDBOX_FLAG = "--disable-gpu-sandbox"
FALLBACK_FLAGS = [GPU_SANDBOX_FLAG]

# Файл-памятка: если обход уже сработал, второй раз не тратим время на падение.
FLAGS_FILE = Path(config.DATA_DIR) / "overlay_flags.json"

# Сколько ждём при старте, чтобы понять «живёт или упал».
EARLY_CRASH_WINDOW = 8.0


def _load_flags() -> list[str]:
    """Сохранённые флаги обхода (только из белого списка)."""
    try:
        raw = FLAGS_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return []
    flags = data.get("flags") if isinstance(data, dict) else None
    if not isinstance(flags, list):
        return []
    return [flag for flag in flags if flag in FALLBACK_FLAGS]


def _save_flags(flags: list[str]) -> None:
    try:
        FLAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        FLAGS_FILE.write_text(json.dumps({"flags": flags}, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except OSError as exc:
        log(f"Не удалось сохранить флаги оверлея: {exc}", "debug")


def _launch_once(api_url: str, extra_flags: list[str]) -> subprocess.Popen | None:
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
    env["PYATNITSA_NO_SIDECAR"] = "1"          # Python уже запущен — второй не нужен
    env["PYATNITSA_API"] = api_url.rstrip("/")

    args = [str(electron), *extra_flags, "."]

    log(f"Запускаю оверлей: {api_url}"
        + (f" (флаги: {' '.join(extra_flags)})" if extra_flags else ""), "info")
    try:
        return subprocess.Popen(args, cwd=str(DESKTOP_DIR), env=env)
    except OSError as exc:
        log(f"Оверлей не запустился: {exc}", "error")
        return None


def launch(api_url: str, disable_gpu: bool | None = None) -> OverlayProcess | None:
    """Запускает Electron, привязанный к уже работающему ядру.

    Флаги обхода (если обход уже находили) подставляются сразу, иначе окно
    перезапустится автоматически при падении GPU-процесса.
    """
    gpu_off = disable_gpu if disable_gpu is not None else \
        os.environ.get("PYATNITSA_DISABLE_GPU") == "1"

    base_flags: list[str] = ["--disable-gpu"] if gpu_off else []
    known = _load_flags()

    process = _launch_once(api_url, [*base_flags, *known])
    if process is None:
        return None
    return OverlayProcess(process, api_url=api_url, base_flags=base_flags,
                          allow_fallback=not known)


def run(api_url: str, rebuild: bool = False) -> OverlayProcess | None:
    """Собрать (если нужно) и запустить. Одна строка для main.py."""
    if not ensure_built(rebuild=rebuild):
        return None
    return launch(api_url)


def show_window(api_url: str) -> int | None:
    """Показать окно уже работающего ядра (повторный клик по ярлыку).

    Electron держит single-instance lock: если окно открыто, новый процесс
    сразу выходит, а существующее окно всплывает наверх. Поэтому за процессом
    не следим — нам нужен только факт запуска.
    """
    flags: list[str] = []
    if os.environ.get("PYATNITSA_DISABLE_GPU") == "1":
        flags.append("--disable-gpu")
    flags.extend(_load_flags())

    process = _launch_once(api_url, flags)
    return process.pid if process is not None else None


def main() -> int:
    """Ручной запуск: python overlay.py [--rebuild] — для отладки UI."""
    import argparse

    parser = argparse.ArgumentParser(description="Собрать и запустить оверлей")
    parser.add_argument("--rebuild", action="store_true", help="собрать заново")
    parser.add_argument("--api", default="http://127.0.0.1:8765",
                        help="адрес уже запущенного ядра")
    args = parser.parse_args()

    info = status()
    print(f"Node:     {info['node'] or 'не найден'}  ({info['node_path'] or '—'})")
    print(f"npm:      {info['npm'] or 'не найден'}")
    print(f"Electron: {info['electron'] or 'не установлен'}")
    print(f"Сборка:   {'есть' if info['built'] else 'нет'}"
          f"{' (устарела)' if info['stale'] else ''}")
    if not info["ready"]:
        print("           окно не готово к запуску — см. подсказку ниже")

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
