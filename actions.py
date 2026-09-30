# -*- coding: utf-8 -*-
"""Системные действия Джарвиса.

Каждый метод возвращает строку-ответ (её озвучит ядро) либо None.
Все операции защищены проверками: если нужной библиотеки нет, ассистент
скажет об этом, а не упадёт.
"""

from __future__ import annotations

import ctypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime
from pathlib import Path

import requests

import config
from utils import (
    CalcError,
    human_duration,
    log,
    normalize_text,
    plural_ru,
    safe_calculate,
    words_to_number,
)

# =====================================================================
#  РАЗБОР ДЛИТЕЛЬНОСТИ
# =====================================================================
_DURATION_UNITS = {
    "час": 3600, "часа": 3600, "часов": 3600, "ч": 3600,
    "минут": 60, "минуты": 60, "минуту": 60, "мин": 60, "м": 60,
    "секунд": 1, "секунды": 1, "секунду": 1, "сек": 1, "с": 1,
}


_DURATION_UNITS.update({"минута": 60, "секунда": 1})
_UNIT_PATTERN = "|".join(sorted(_DURATION_UNITS, key=len, reverse=True))
_DURATION_PART = re.compile(
    rf"(?<![\w.,])([+-]?\d+(?:[.,]\d+)?)\s*({_UNIT_PATTERN})(?!\w)"
)


def extract_duration(text: str) -> tuple[int | None, str]:
    """Разбирает один непрерывный интервал и убирает только его из текста.

    Поддерживает «1 час 30 минут», числа прописью, полчаса и полторы минуты.
    Отдельный предлог «с» и слова «часть/секция» не являются единицами времени.
    """
    normalized = words_to_number(normalize_text(text))
    normalized = re.sub(r"\bполчаса\b", "30 минут", normalized)
    normalized = re.sub(r"\bполтор[аы]\s+(часа?|минуты?|секунды?)\b",
                        r"1.5 \1", normalized)
    normalized = re.sub(r"\b(через|на)\s+(час|минуту|секунду)\b",
                        r"\1 1 \2", normalized)
    if normalized in ("час", "минута", "минуту", "секунда", "секунду"):
        normalized = "1 " + normalized
    first = _DURATION_PART.search(normalized)
    if first is None:
        return None, normalized
    total = 0.0
    end = first.end()
    match = first
    while match:
        amount = float(match.group(1).replace(",", "."))
        if amount <= 0 or amount > 10 * 365 * 24 * 3600:
            return None, normalized
        total += amount * _DURATION_UNITS[match.group(2)]
        end = match.end()
        following = _DURATION_PART.search(normalized, end)
        if following is None or not re.fullmatch(r"\s*(?:и\s+)?", normalized[end:following.start()]):
            break
        match = following
    if total > 10 * 365 * 24 * 3600:
        return None, normalized
    remainder = normalized[:first.start()] + " " + normalized[end:]
    return max(1, int(total)), " ".join(remainder.split())


def parse_duration(text: str) -> int | None:
    """Возвращает длительность в секундах либо None для невалидного интервала."""
    return extract_duration(text)[0]


# =====================================================================
#  КАЛЬКУЛЯТОР
# =====================================================================
_FILLER_PHRASES = [
    "сколько будет", "чему равно", "сколько это", "посчитай пожалуйста",
    "посчитай", "посчитайте", "вычисли", "вычислить", "рассчитай",
    "калькулятор", "сколько", "будет", "равно", "итого", "получится",
]

_OPERATOR_PHRASES = [
    ("умножить на", "*"), ("умнож на", "*"), ("умножить", "*"), ("помножить на", "*"),
    ("помножить", "*"), ("разделить на", "/"), ("разделить", "/"),
    ("поделить на", "/"), ("поделить", "/"), ("делить на", "/"),
    ("в квадрате", "**2"), ("в кубе", "**3"),
    ("квадратный корень из", "sqrt "), ("корень из", "sqrt "),
    ("прибавить", "+"), ("сложить", "+"), ("плюс", "+"),
    ("вычесть", "-"), ("отнять", "-"), ("минус", "-"),
    ("модуль", "abs "),
]


def prepare_expression(raw: str) -> str:
    """Превращает фразу в арифметическое выражение.

    Именно здесь раньше была ошибка: слова-операторы удалялись до замены,
    поэтому «два плюс два» не считалось.
    """
    text = (raw or "").lower().replace("ё", "е")
    text = (text.replace("×", "*").replace("÷", "/").replace("—", "-")
                .replace("–", "-").replace("^", "**").replace(":", "/"))
    text = re.sub(r"\s+", " ", text).strip()

    for filler in _FILLER_PHRASES:
        text = text.replace(filler, " ")

    for phrase, symbol in _OPERATOR_PHRASES:
        text = text.replace(phrase, f" {symbol} ")

    text = words_to_number(text)
    text = normalize_text(text)
    # «sqrt 81» -> «sqrt(81)», иначе выражение не разберётся.
    text = re.sub(r"\b(sqrt|abs|log|log10|exp|sin|cos|tan)\s+(\d+(?:\.\d+)?)",
                  r"\1(\2)", text)
    return text.strip()


def calculate(raw: str):
    """Возвращает число или бросает CalcError."""
    expression = prepare_expression(raw)
    if not expression:
        raise CalcError("в фразе нет выражения")
    return safe_calculate(expression)


# =====================================================================
#  ДЕЙСТВИЯ
# =====================================================================
class Actions:
    def __init__(self):
        self._volume = None
        self._city_cache: dict = {}

    # ---------------- ГРОМКОСТЬ ----------------
    def _volume_interface(self):
        if self._volume is not None:
            return self._volume
        from ctypes import POINTER, cast

        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        device = AudioUtilities.GetSpeakers()
        if hasattr(device, "EndpointVolume"):          # pycaw >= 2024
            self._volume = device.EndpointVolume
        else:                                          # старый API
            interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            self._volume = cast(interface, POINTER(IAudioEndpointVolume))
        return self._volume

    def volume(self, direction: str = "up", step: float = 0.1,
               level: int | None = None) -> str:
        if not config.FEATURES["pycaw"]:
            return "Для управления звуком установите pycaw: pip install pycaw comtypes"
        try:
            volume = self._volume_interface()
            if direction == "mute":
                volume.SetMute(1, None)
                return "Звук отключён, сэр."
            if direction == "unmute":
                volume.SetMute(0, None)
                return "Звук снова включён."
            if level is not None:
                new_level = min(100, max(0, int(level))) / 100.0
            else:
                current = volume.GetMasterVolumeLevelScalar()
                delta = step if direction == "up" else -step
                new_level = min(1.0, max(0.0, current + delta))
            volume.SetMasterVolumeLevelScalar(new_level, None)
            if volume.GetMute():
                volume.SetMute(0, None)
            percent = int(round(new_level * 100))
            verb = "увеличил" if direction == "up" else "уменьшил"
            return f"Громкость {verb} до {percent} процентов."
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка управления громкостью: {exc}", "error")
            return "Не удалось изменить громкость, сэр."

    # ---------------- ЯРКОСТЬ ----------------
    def brightness(self, direction: str = "up", step: int = 10,
                   level: int | None = None) -> str:
        if not config.FEATURES["screen_brightness_control"]:
            return "Установите screen-brightness-control: pip install screen-brightness-control"
        try:
            import screen_brightness_control as sbc

            values = sbc.get_brightness()
            current = values[0] if isinstance(values, list) else values
            if level is not None:
                new_level = min(100, max(0, int(level)))
            else:
                new_level = min(100, max(0, current + (step if direction == "up" else -step)))
            sbc.set_brightness(new_level)
            return f"Яркость экрана теперь {new_level} процентов."
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка управления яркостью: {exc}", "error")
            return "Не удалось изменить яркость, сэр."

    # ---------------- ПИТАНИЕ ----------------
    def lock_screen(self) -> str:
        try:
            ctypes.windll.user32.LockWorkStation()
            return "Рабочая станция заблокирована, сэр."
        except Exception as exc:  # noqa: BLE001
            log(f"Не удалось заблокировать экран: {exc}", "error")
            return "Не удалось заблокировать экран."

    def shutdown(self, delay: int = 15) -> str:
        try:
            subprocess.run(["shutdown", "/s", "/t", str(delay)], check=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return (f"Выключаю компьютер через {delay} секунд. "
                    "Скажите «отмена выключения», чтобы остановить.")
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка выключения: {exc}", "error")
            return "Не удалось выключить компьютер."

    def reboot(self, delay: int = 15) -> str:
        try:
            subprocess.run(["shutdown", "/r", "/t", str(delay)], check=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return f"Перезагрузка через {delay} секунд. Скажите «отмена выключения», чтобы остановить."
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка перезагрузки: {exc}", "error")
            return "Не удалось перезагрузить компьютер."

    def cancel_shutdown(self) -> str:
        try:
            result = subprocess.run(["shutdown", "/a"], capture_output=True, text=True,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode == 0:
                return "Отменил выключение, сэр."
            return "Нечего отменять — таймер выключения не запущен."
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка отмены выключения: {exc}", "error")
            return "Не удалось отменить выключение."

    # ---------------- ОКНА ----------------
    def switch_window(self) -> str:
        if not config.FEATURES["pyautogui"]:
            return "Установите pyautogui: pip install pyautogui"
        try:
            import pyautogui
            pyautogui.keyDown("alt")
            pyautogui.press("tab")
            pyautogui.keyUp("alt")
            return "Переключил окно."
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка переключения окна: {exc}", "error")
            return "Не удалось переключить окно."

    def _foreground_window(self):
        import win32gui
        return win32gui.GetForegroundWindow()

    def close_window(self) -> str:
        if not config.FEATURES["win32gui"]:
            return "Установите pywin32: pip install pywin32"
        try:
            import win32con
            import win32gui
            win32gui.PostMessage(self._foreground_window(), win32con.WM_CLOSE, 0, 0)
            return "Закрыл активное окно."
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка закрытия окна: {exc}", "error")
            return "Не удалось закрыть окно."

    def minimize_window(self) -> str:
        if not config.FEATURES["win32gui"]:
            return "Установите pywin32: pip install pywin32"
        try:
            import win32con
            import win32gui
            win32gui.ShowWindow(self._foreground_window(), win32con.SW_MINIMIZE)
            return "Свернул окно."
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка сворачивания: {exc}", "error")
            return "Не удалось свернуть окно."

    def maximize_window(self) -> str:
        if not config.FEATURES["win32gui"]:
            return "Установите pywin32: pip install pywin32"
        try:
            import win32con
            import win32gui
            win32gui.ShowWindow(self._foreground_window(), win32con.SW_MAXIMIZE)
            return "Развернул окно на весь экран."
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка разворачивания: {exc}", "error")
            return "Не удалось развернуть окно."

    def list_windows(self) -> str:
        if not config.FEATURES["win32gui"]:
            return "Установите pywin32: pip install pywin32"
        try:
            import win32gui
            titles = []

            def collect(hwnd, _):
                if win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd).strip()
                    if title:
                        titles.append(title)

            win32gui.EnumWindows(collect, None)
            if not titles:
                return "Открытых окон не вижу."
            return "Открытые окна: " + "; ".join(titles[:8])
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка перечисления окон: {exc}", "error")
            return "Не удалось получить список окон."

    # ---------------- МЕДИА ----------------
    def media_key(self, vk_code: int) -> None:
        try:
            user32 = ctypes.windll.user32
            user32.keybd_event(vk_code, 0, 0, 0)
            user32.keybd_event(vk_code, 0, 2, 0)
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка медиа-клавиши: {exc}", "debug")

    # ---------------- ЗАПУСК ПРИЛОЖЕНИЙ ----------------
    @staticmethod
    def _launch(target: str, args: list | None = None) -> bool:
        try:
            subprocess.Popen([target] + (args or []),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return True
        except (FileNotFoundError, OSError):
            return False

    def open_browser(self, url: str = "https://google.com") -> str:
        try:
            webbrowser.open(url)
            return "Открываю браузер."
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка открытия браузера: {exc}", "error")
            return "Не удалось открыть браузер."

    def open_path(self, path: Path) -> str:
        try:
            os.startfile(str(path))  # noqa: S606 — Windows-only API
            return f"Открыл {path.name}."
        except Exception as exc:  # noqa: BLE001
            log(f"Не удалось открыть {path}: {exc}", "error")
            return f"Не удалось открыть {path.name}."

    def open_app(self, name: str) -> str:
        key = normalize_text(name)
        target = config.APPS.get(key)
        if target and self._launch(target):
            return f"Запускаю {key}."

        candidates = {
            "steam": [
                r"C:\Program Files (x86)\Steam\Steam.exe",
                r"C:\Program Files\Steam\Steam.exe",
            ],
            "стим": [
                r"C:\Program Files (x86)\Steam\Steam.exe",
                r"C:\Program Files\Steam\Steam.exe",
            ],
            "vscode": [
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
                r"C:\Program Files\Microsoft VS Code\Code.exe",
            ],
            "вскод": [
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
                r"C:\Program Files\Microsoft VS Code\Code.exe",
            ],
            "pycharm": [
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\PyCharm Community\bin\pycharm64.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\PyCharm Professional\bin\pycharm64.exe"),
                r"C:\Program Files\JetBrains\PyCharm Community Edition\bin\pycharm64.exe",
            ],
            "пайчарм": [
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\PyCharm Community\bin\pycharm64.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\PyCharm Professional\bin\pycharm64.exe"),
            ],
        }

        for path in candidates.get(key, []):
            if os.path.exists(path):
                return self.open_path(Path(path))

        aliases = {"стим": "steam", "вскод": "code", "пайчарм": "pycharm", "хром": "chrome"}
        command = aliases.get(key, key)
        if self._launch(command):
            return f"Запускаю {key}."
        if key == "steam":
            webbrowser.open("steam://open/main")
            return "Открываю Steam."
        return f"Не нашёл приложение «{name}», сэр."

    def open_terminal(self) -> str:
        if self._launch("cmd.exe"):
            return "Открываю командную строку."
        return "Не удалось открыть терминал."

    def open_powershell(self) -> str:
        if self._launch("powershell.exe"):
            return "Открываю PowerShell."
        return "Не удалось открыть PowerShell."

    @staticmethod
    def _roblox_candidates(exe: str) -> list:
        """Пути к исполняемому файлу Roblox во всех установленных версиях.

        Roblox держит рядом несколько папок ``version-<hex>`` и при каждом
        обновлении добавляет новую, оставляя старые. Поэтому имя папки нельзя
        вписывать в код: перебираем шаблоном и сортируем **по времени
        изменения**, а не по имени — hex-суффикс ничего не значит, и «самая
        большая» строка вовсе не самая свежая версия.
        """
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "Roblox" / "Versions"
        try:
            dirs = [item for item in base.glob("version-*") if item.is_dir()]
        except OSError:
            return []
        try:
            dirs.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        except OSError:
            pass
        return [item / exe for item in dirs]

    def open_roblox(self, studio: bool = False) -> str:
        """Запускает Roblox Player или Roblox Studio."""
        exe = "RobloxStudioBeta.exe" if studio else "RobloxPlayerBeta.exe"
        label = "Roblox Studio" if studio else "Roblox"

        for path in self._roblox_candidates(exe):
            if os.path.exists(path):
                return self.open_path(path)

        # Roblox мог быть установлен только через протокол (Microsoft Store).
        try:
            webbrowser.open("roblox-studio:" if studio else "roblox-player:")
            return f"Открываю {label}."
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка запуска {label}: {exc}", "error")
            return f"Не нашёл {label}, сэр. Проверьте, установлен ли он."

    # ---------------- СКРИНШОТ ----------------
    def screenshot(self, prefix: str = "Screenshot") -> Path | None:
        if not config.FEATURES["PIL"]:
            return None
        try:
            from PIL import ImageGrab

            config.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = config.SCREENSHOT_DIR / f"Jarvis_{prefix}_{stamp}.png"
            ImageGrab.grab(all_screens=True).save(path)
            return path
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка скриншота: {exc}", "error")
            return None

    # ---------------- БУФЕР ОБМЕНА ----------------
    def clipboard_get(self) -> str:
        if not config.FEATURES["pyperclip"]:
            return "Установите pyperclip: pip install pyperclip"
        try:
            import pyperclip
            content = (pyperclip.paste() or "").strip()
            if not content:
                return "Буфер обмена пуст."
            return f"В буфере обмена: {content[:300]}"
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка чтения буфера: {exc}", "error")
            return "Не удалось прочитать буфер обмена."

    def clipboard_set(self, text: str) -> str:
        if not config.FEATURES["pyperclip"]:
            return "Установите pyperclip: pip install pyperclip"
        try:
            import pyperclip
            pyperclip.copy(text)
            return "Скопировал в буфер обмена."
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка записи буфера: {exc}", "error")
            return "Не удалось записать в буфер обмена."

    # ---------------- КОРЗИНА ----------------
    def empty_trash(self) -> str:
        """Безопасная очистка корзины через штатную команду Windows.

        Прежний вариант (``rd /s /q C:\\$Recycle.Bin``) удалял саму служебную
        папку, требовал прав администратора и мог повредить систему.
        """
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Clear-RecycleBin -Force -Confirm:$false -ErrorAction SilentlyContinue"],
                capture_output=True, text=True, timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0:
                return "Корзина очищена."
            return "Корзина уже пуста или очистка недоступна."
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка очистки корзины: {exc}", "error")
            return "Не удалось очистить корзину."

    # ---------------- СВЕДЕНИЯ О КОМПЬЮТЕРЕ ----------------
    # Всё ниже — через системные вызовы Windows (ctypes) и стандартную
    # библиотеку: ни одной новой зависимости, как и требует проект.
    def disk_space(self) -> str:
        """Свободное место на локальных дисках."""
        try:
            parts = []
            for letter in "CDEFGH":
                root = f"{letter}:\\"
                if not os.path.exists(root):
                    continue
                try:
                    usage = shutil.disk_usage(root)
                except OSError:
                    continue
                free = usage.free / (1024 ** 3)
                total = usage.total / (1024 ** 3)
                parts.append(f"{letter}: {free:.0f} из {total:.0f} гигабайт свободно")
            if not parts:
                return "Не удалось определить диски, сэр."
            return "Место на дисках — " + "; ".join(parts) + "."
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка чтения места на диске: {exc}", "error")
            return "Не удалось узнать свободное место, сэр."

    @staticmethod
    def battery() -> str:
        """Заряд батареи через системный API Windows."""
        try:
            class _PowerStatus(ctypes.Structure):
                # Поля — беззнаковые байты: у знакового c_byte значение 255
                # («нет данных») превратилось бы в -1, и заряд показывался бы
                # как «-1 процент».
                _fields_ = [
                    ("ACLineStatus", ctypes.c_ubyte),
                    ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte),
                    ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_ulong),
                    ("BatteryFullLifeTime", ctypes.c_ulong),
                ]

            status = _PowerStatus()
            if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                return "Не удалось определить состояние питания, сэр."
            percent = int(status.BatteryLifePercent)
            if percent == 255:
                return "Батарея не обнаружена — похоже, это стационарный компьютер, сэр."
            on_ac = status.ACLineStatus == 1
            tail = "питание от сети" if on_ac else "работаю от батареи"
            word = plural_ru(percent, "процент", "процента", "процентов")
            return f"Заряд батареи {percent} {word}, {tail}."
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка чтения заряда батареи: {exc}", "error")
            return "Не удалось узнать заряд батареи, сэр."

    @staticmethod
    def memory() -> str:
        """Загрузка оперативной памяти."""
        try:
            class _MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatus()
            status.dwLength = ctypes.sizeof(_MemoryStatus)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return "Не удалось получить данные о памяти, сэр."
            load = int(status.dwMemoryLoad)
            total = status.ullTotalPhys / (1024 ** 3)
            used = (status.ullTotalPhys - status.ullAvailPhys) / (1024 ** 3)
            word = plural_ru(load, "процент", "процента", "процентов")
            return (f"Оперативная память загружена на {load} {word}: "
                    f"{used:.1f} из {total:.1f} гигабайта.")
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка чтения памяти: {exc}", "error")
            return "Не удалось узнать состояние памяти, сэр."

    @staticmethod
    def uptime() -> str:
        """Сколько времени компьютер работает без перезагрузки."""
        try:
            func = ctypes.windll.kernel32.GetTickCount64
            # По умолчанию ctypes вернул бы c_int и обнулился бы через 24,9 суток.
            func.restype = ctypes.c_ulonglong
            seconds = int(func() // 1000)
            return f"Компьютер работает без перезагрузки {human_duration(seconds)}."
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка чтения времени работы: {exc}", "error")
            return "Не удалось узнать время работы, сэр."

    @staticmethod
    def ip_address() -> str:
        """Локальный IP-адрес в текущей сети."""
        address = ""
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                # Пакеты не отправляются: сокет лишь выбирает маршрут и адрес.
                probe.connect(("8.8.8.8", 80))
                address = probe.getsockname()[0]
            finally:
                probe.close()
        except OSError:
            address = ""
        if not address or address.startswith("127."):
            try:
                address = socket.gethostbyname(socket.gethostname())
            except OSError:
                address = ""
        if not address or address.startswith("127."):
            return "Не удалось определить локальный адрес, сэр."
        return f"Ваш локальный IP-адрес — {address}."

    @staticmethod
    def monitor_off() -> str:
        """Гасит экран, не блокируя компьютер."""
        try:
            HWND_BROADCAST = 0xFFFF
            WM_SYSCOMMAND = 0x0112
            SC_MONITORPOWER = 0xF170
            ctypes.windll.user32.SendMessageW(
                HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, 2)
            return "Гашу экран, сэр. Двиньте мышью, чтобы вернуть."
        except Exception as exc:  # noqa: BLE001
            log(f"Не удалось выключить монитор: {exc}", "error")
            return "Не удалось выключить монитор, сэр."

    @staticmethod
    def sleep_mode(delay: float = 1.5) -> str:
        """Переводит компьютер в спящий режим.

        Усыпление откладывается на пару секунд в отдельном потоке: иначе
        машина засыпает раньше, чем ассистент успевает попрощаться, и ответ
        просто не звучит.
        """
        def _suspend() -> None:
            time.sleep(max(0.0, float(delay)))
            try:
                ctypes.windll.powrprof.SetSuspendState(False, False, False)
            except Exception as exc:  # noqa: BLE001
                log(f"Ошибка спящего режима: {exc}", "error")

        try:
            threading.Thread(target=_suspend, name="jarvis-sleep", daemon=True).start()
        except Exception as exc:  # noqa: BLE001
            log(f"Не удалось запланировать сон: {exc}", "error")
            return "Не удалось перейти в спящий режим, сэр."
        return f"Перехожу в спящий режим через {int(delay)} секунду, сэр."

    def system_info(self) -> str:
        """Краткая сводка о компьютере одним ответом.

        Обычный метод, а не статический: он зовёт ``self.disk_space()``, а тот
        обращается к экземпляру.
        """
        return " ".join([
            self.memory(),
            self.disk_space(),
            self.uptime(),
            self.battery(),
        ])

    # ---------------- МЕЛКИЕ УТИЛИТЫ ----------------
    @staticmethod
    def random_choice(raw: str = "") -> str:
        """Монетка, кубик или случайное число из фразы.

        Случайность берём из ``secrets``: тот же модуль даёт равномерное
        распределение без настройки seed.
        """
        text = normalize_text(raw)
        if "монет" in text:
            return "Орёл!" if secrets.randbelow(2) == 0 else "Решка!"
        if "кубик" in text or "кост" in text:
            return f"Выпало {secrets.randbelow(6) + 1}."
        low, high = 1, 100
        match = re.search(r"от\s+(\d+)\s+до\s+(\d+)", text)
        if match:
            low, high = int(match.group(1)), int(match.group(2))
            if low > high:
                low, high = high, low
        elif "числ" not in text:
            return ("Подбросить монетку, бросить кубик или загадать число "
                    "«от и до», сэр?")
        # Ограничиваем размах, чтобы не выдать бессмысленное число.
        high = min(high, low + 10 ** 6)
        return f"Случайное число: {low + secrets.randbelow(high - low + 1)}."

    @staticmethod
    def generate_password(length: int = 16) -> str:
        """Стойкий пароль: буквы обоих регистров, цифры и символы.

        Похожие символы (l, I, 1, O, 0) исключены — такой пароль проще
        продиктовать и переписать без ошибок.
        """
        alphabet = ("abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ"
                    "23456789!@#$%^&*()-_=+")
        length = max(8, min(int(length), 64))
        return "".join(secrets.choice(alphabet) for _ in range(length))

    def open_common_folder(self, raw: str) -> str:
        """Открывает стандартную папку пользователя: «открой загрузки»."""
        text = normalize_text(raw)
        home = Path.home()
        folders = (
            (("загрузк", "download"), home / "Downloads", "Загрузки"),
            (("рабочий стол", "desktop"), home / "Desktop", "Рабочий стол"),
            (("картинк", "изображени", "picture"), home / "Pictures", "Изображения"),
            (("музык", "music"), home / "Music", "Музыка"),
            (("видео", "video"), home / "Videos", "Видеозаписи"),
            (("документ", "document"), home / "Documents", "Документы"),
        )
        for keys, path, label in folders:
            if any(key in text for key in keys):
                if not path.exists():
                    return f"Папка «{label}» не найдена, сэр."
                try:
                    os.startfile(str(path))  # noqa: S606 — Windows-only API
                except OSError as exc:
                    log(f"Не удалось открыть {path}: {exc}", "error")
                    return f"Не удалось открыть папку «{label}», сэр."
                return f"Открываю папку «{label}»."
        return "Какую папку открыть, сэр? Например, «открой загрузки»."

    # ---------------- ПОГОДА ----------------
    def weather(self, city: str | None = None) -> str:
        city = city or config.WEATHER_DEFAULT_CITY
        try:
            location = self._geocode(city)
            if location is None:
                return f"Не нашёл город «{city}», сэр."
            latitude, longitude = location["latitude"], location["longitude"]
            name = location.get("name", city)

            weather_url = (
                "https://api.open-meteo.com/v1/forecast"
                f"?latitude={latitude}&longitude={longitude}"
                "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
                "&timezone=auto&wind_speed_unit=ms"
            )
            data = _http_json(weather_url, config.WEATHER_TIMEOUT, attempts=_FORECAST_ATTEMPTS)
            if not data:
                return "Метеослужба недоступна, сэр."
            current = data.get("current", {})
            temperature = round(current.get("temperature_2m", 0))
            feels = round(current.get("apparent_temperature", temperature))
            wind = round(current.get("wind_speed_10m", 0))
            status = _weather_description(current.get("weather_code", 0))
            return (f"В городе {name} сейчас {status}, {temperature} градусов, "
                    f"ощущается как {feels}. Ветер {wind} метров в секунду.")
        except Exception as exc:  # noqa: BLE001
            log(f"Ошибка метеослужбы: {exc}", "error")
            return "Метеослужба недоступна, сэр."

    def _lookup(self, name: str, timeout: float, deadline: float,
                attempts: int) -> dict | None:
        """Один запрос к геокодеру с повторами.

        Различает два исхода: «сервер не ответил» (сетевая осечка — повторяем)
        и «сервер ответил, но такого города нет» (повторять бессмысленно).
        Возвращает запись о городе или None.
        """
        for attempt in range(attempts):
            if time.time() > deadline:
                break
            try:
                url = (
                    "https://geocoding-api.open-meteo.com/v1/search"
                    f"?name={urllib.parse.quote(name)}&count=1&language=ru&format=json"
                )
                response = requests.get(url, timeout=timeout).json()
            except Exception as exc:  # noqa: BLE001
                log(f"Геокодер: «{name}» не ответил "
                    f"(попытка {attempt + 1}/{attempts}): {exc}", "debug")
                continue
            results = response.get("results")
            return results[0] if results else None
        return None

    def _geocode(self, city: str) -> dict | None:
        """Ищет координаты города, перебирая падежные формы.

        Речь даёт «в Москве», «в Казани», «в Питере» — геокодер такие формы
        не находит, поэтому пробуем исходную и нормализованные (именительный падеж).
        """
        cached = self._city_cache.get(city)
        if cached is not None:
            return cached or None

        # Общий лимит времени: перебор вариантов не должен подвешивать ассистента.
        # Удачный ответ приходит за ~0,2 с, поэтому 8 с — с большим запасом.
        deadline = time.time() + float(getattr(config, "WEATHER_DEADLINE", _GEOCODE_DEADLINE))
        # Короткий таймаут на один запрос: удачный ответ приходит за ~0.2 с,
        # а «зависшие» запросы обрываем быстро, чтобы успеть повторить.
        timeout = min(config.WEATHER_TIMEOUT, _GEOCODE_TIMEOUT)

        # Разговорное название («Питер», «МСК»): ищем только канонический город.
        # Иначе при сетевой осечке «Питер» деградирует до деревни в Пермском крае.
        alias = _city_alias(city)
        if alias:
            found = self._lookup(alias, timeout, deadline, _GEOCODE_ALIAS_ATTEMPTS)
            self._city_cache[city] = found or {}
            if found and alias.lower() != city.strip().lower():
                log(f"Город «{city}» распознан как «{alias}».", "debug")
            return found

        requests_left = _GEOCODE_MAX_REQUESTS
        for candidate in _city_candidates(city):
            if time.time() > deadline or requests_left <= 0:
                log(f"Геокодер: бюджет на «{city}» исчерпан.", "debug")
                break
            attempts = min(_GEOCODE_ATTEMPTS, requests_left)
            requests_left -= attempts
            found = self._lookup(candidate, timeout, deadline, attempts)
            if found:
                self._city_cache[city] = found
                if candidate != city:
                    log(f"Город «{city}» распознан как «{candidate}».", "debug")
                return found

        # Сетевой сбой не должен блокировать город до перезапуска.
        return None

    # ---------------- ВЕБ-ПОИСК ----------------
    def web_search(self, query: str, max_results: int | None = None) -> str:
        query = (query or "").strip()
        if not query:
            return "Уточните, что искать, сэр."
        max_results = max_results or config.WEB_SEARCH_RESULTS

        snippets = self._duckduckgo(query, max_results)
        if snippets:
            return " ".join(snippets)
        return self._wikipedia(query)

    @staticmethod
    def _duckduckgo(query: str, max_results: int) -> list:
        try:
            try:
                from ddgs import DDGS            # новый пакет
            except ImportError:
                from duckduckgo_search import DDGS  # старый пакет
        except ImportError:
            return []

        for attempt in (
            {"query": query, "max_results": max_results, "region": "ru-ru"},
            {"query": query, "max_results": max_results},
        ):
            try:
                with DDGS() as ddgs:
                    results = list(ddgs.text(**attempt))
                if results:
                    return [
                        f"{item.get('title', '')}. {item.get('body', '')}".strip()
                        for item in results[:max_results]
                    ]
            except Exception as exc:  # noqa: BLE001
                log(f"DuckDuckGo недоступен: {exc}", "debug")
        return []

    @staticmethod
    def _wikipedia(query: str) -> str:
        try:
            import wikipedia
            wikipedia.set_lang("ru")
            found = wikipedia.search(query)
            if found:
                return wikipedia.summary(found[0], sentences=2)
        except Exception as exc:  # noqa: BLE001
            log(f"Wikipedia недоступна: {exc}", "debug")
        return "Не удалось найти информацию, сэр."

    # ---------------- УВЕДОМЛЕНИЯ ----------------
    @staticmethod
    def notify(title: str, message: str) -> bool:
        """Всплывающее уведомление Windows без сторонних библиотек."""
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$n = New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon = [System.Drawing.SystemIcons]::Information;"
            "$n.Visible = $true;"
            f"$n.ShowBalloonTip(8000, '{_ps_escape(title)}', "
            f"'{_ps_escape(message)}', [System.Windows.Forms.ToolTipIcon]::Info);"
            "Start-Sleep -Seconds 9; $n.Dispose();"
        )
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log(f"Не удалось показать уведомление: {exc}", "debug")
            return False


def _ps_escape(text: str) -> str:
    return (text or "").replace("'", "''").replace("\n", " ")[:200]


# Геокодер: ограничители, чтобы поиск города не подвешивал ассистента.
# Удачный ответ приходит за ~0.2 с, «зависшие» запросы обрываются таймаутом,
# поэтому короткий таймаут + несколько повторов надёжнее одного долгого запроса.
_GEOCODE_TIMEOUT = 2.5          # секунд на один HTTP-запрос
_GEOCODE_DEADLINE = 18.0        # секунд на весь перебор вариантов
_GEOCODE_MAX_REQUESTS = 8       # потолок запросов к геокодеру на один город
_GEOCODE_ATTEMPTS = 3           # попыток на один вариант написания
_GEOCODE_ALIAS_ATTEMPTS = 5     # больше попыток для разговорного названия:
                                # отступать к буквальному совпадению нельзя
_FORECAST_ATTEMPTS = 4          # повторов для запроса самого прогноза


def _http_json(url: str, timeout: float, attempts: int = _GEOCODE_ATTEMPTS) -> dict | None:
    """GET JSON с повторами: внешние сервисы иногда отвечают таймаутом.

    Возвращает словарь или None, если все попытки не удались. Никогда не бросает
    исключение — вызывающий код не должен падать из-за капризов погодного API.
    """
    for attempt in range(max(1, attempts)):
        try:
            return requests.get(url, timeout=timeout).json()
        except Exception as exc:  # noqa: BLE001
            log(f"Запрос {url.split('?')[0]} не удался "
                f"(попытка {attempt + 1}/{attempts}): {exc}", "debug")
    return None

# Разговорные названия городов. Без них «Питер» находится в Пермском крае,
# а не в Санкт-Петербурге (геокодер честно находит одноимённую деревню).
# Для составных названий падеж меняет не только последнее слово
# («Нижнем Новгороде»), поэтому такие формы перечислены явно.
_CITY_ALIASES = {
    "питер": "Санкт-Петербург",
    "спб": "Санкт-Петербург",
    "петербург": "Санкт-Петербург",
    "санкт петербург": "Санкт-Петербург",
    "санкт-петербург": "Санкт-Петербург",
    "мск": "Москва",
    "екб": "Екатеринбург",
    "нск": "Новосибирск",
    "нижний": "Нижний Новгород",
    "нижний новгород": "Нижний Новгород",
    "нижнем новгороде": "Нижний Новгород",
    "ростов": "Ростов-на-Дону",
    "ростов на дону": "Ростов-на-Дону",
    "ростов-на-дону": "Ростов-на-Дону",
    "ростове-на-дону": "Ростов-на-Дону",
    "великий новгород": "Великий Новгород",
    "великом новгороде": "Великий Новгород",
    "владик": "Владивосток",
    "набережные": "Набережные Челны",
    "набережные челны": "Набережные Челны",
    "набережных челнах": "Набережные Челны",
    "сочи": "Сочи",
}

# Окончания предложного/родительного падежа -> варианты именительного.
# Пример: «москве» -> «москв», «москва»; «казани» -> «казань»; «питере» -> «питер».
_CASE_ENDINGS = (
    ("е", ("", "а", "я")),
    ("и", ("ь", "я", "а")),
    ("у", ("а", "я")),
    ("ю", ("я", "а")),
    ("ой", ("ая", "а")),
    ("ей", ("ея", "ь")),
)

# Предлоги, которые речь приклеивает к названию: «в Москве», «на Кубе».
# Пробел после предлога обязателен, иначе пострадают «Набережные», «Изюм».
_PREPOSITION_RE = re.compile(r"^(?:в|во|на|из|с|со|у|к|ко|по|от|до|для|о|об)\s+")


def _strip_preposition(text: str) -> str:
    """Убирает ведущий предлог: «в Москве» -> «Москве»."""
    return _PREPOSITION_RE.sub("", (text or "").strip().lower()).strip()


def _city_stem(base: str) -> tuple:
    """Отделяет падежное окончание: («питере») -> («питер», «е»).

    Возвращает (основа, окончание); окончание — None, если падеж не распознан.
    """
    for ending, _ in _CASE_ENDINGS:
        if base.endswith(ending) and len(base) - len(ending) >= 3:
            return base[: -len(ending)], ending
    return base, None


def _city_alias(city: str) -> str | None:
    """Каноническое название для разговорного: «в Питере» -> «Санкт-Петербург».

    Нужна отдельно от _city_candidates, потому что для разговорного названия
    отступать к буквальному совпадению нельзя: «Питер» — это деревня в
    Пермском крае, а пользователь имеет в виду Санкт-Петербург.
    """
    base = _strip_preposition(city)
    if not base:
        return None
    stem, ending = _city_stem(base)
    # «Санкт-Петербург» и «санкт петербург» — одно и то же название.
    keys = [base, base.replace("-", " ")]
    if ending:
        keys += [stem, stem.replace("-", " ")]
    for key in keys:
        alias = _CITY_ALIASES.get(key)
        if alias:
            return alias
    return None


def _city_candidates(city: str) -> list:
    """Формы названия города для поиска, в порядке убывания правдоподобия.

    Геокодер не понимает «в Москве», а «Питер» трактует буквально, поэтому
    сначала подставляем разговорные названия, затем исходную форму и падежи.
    """
    base = _strip_preposition(city)
    if not base:
        return []

    candidates: list = []

    def add(value: str) -> None:
        value = value.strip()
        if value and value not in candidates:
            candidates.append(value)

    stem, matched_ending = _city_stem(base)

    # Разговорные названия — строго первыми.
    for key in (base, stem):
        alias = _CITY_ALIASES.get(key)
        if alias:
            add(alias)

    add(base)
    add(base.capitalize())
    if "-" in base:
        add(base.replace("-", " "))

    if matched_ending:
        for replacement in dict(_CASE_ENDINGS)[matched_ending]:
            add(stem + replacement)

    return candidates


def _weather_description(code: int) -> str:
    if code == 0:
        return "ясно"
    if code in (1, 2, 3):
        return "переменная облачность"
    if code in (45, 48):
        return "туман"
    if code in (51, 53, 55, 56, 57):
        return "морось"
    if code in (61, 63, 65, 80, 81, 82):
        return "дождь"
    if code in (66, 67):
        return "ледяной дождь"
    if code in (71, 73, 75, 77, 85, 86):
        return "снег"
    if code in (95, 96, 99):
        return "гроза"
    return "непонятная погода"
