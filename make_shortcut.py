# -*- coding: utf-8 -*-
"""Ярлык «Джарвис» на рабочем столе — фоновый запуск без консоли.

Ярлык запускает ``Джарвис.exe main.py --background``, где ``Джарвис.exe`` —
копия ``pythonw.exe`` рядом с проектом. Диспетчер задач берёт имя процесса
из имени файла, поэтому ассистент виден там как «Джарвис.exe», а не как
«pythonw.exe». Копия создаётся автоматически (см. :func:`jarvis_exe`).

Режим запуска:

* окна консоли нет — ассистент живёт в фоне;
* текстовый ввод не спрашивается (набирать его негде);
* лог пишется в ``data/jarvis.log``;
* управление — голосом («Джарвис, ...») и через веб-интерфейс,
  который открывается браузером при старте.

Тот же режим можно включить вручную: ``python main.py --background``.

Запуск::

    python make_shortcut.py                      # создать или обновить ярлык
    python make_shortcut.py --remove             # удалить ярлык
    python make_shortcut.py --name "Ассистент"   # другое имя ярлыка
    python make_shortcut.py --no-exe-copy        # не делать копию интерпретатора
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_NAME = "Джарвис"
JARVIS_EXE_NAME = "Джарвис.exe"
DESCRIPTION = "Джарвис — голосовой ассистент (фоновый запуск, интерфейс в браузере)"


def desktop_dir() -> Path:
    """Рабочий стол пользователя. Учитывает переезд папки в OneDrive."""
    candidates = [
        Path.home() / "Desktop",
        Path.home() / "OneDrive" / "Desktop",
        Path.home() / "Рабочий стол",
    ]
    for path in candidates:
        if path.is_dir():
            return path
    return candidates[0]


def pythonw() -> Path:
    """pythonw.exe рядом с текущим интерпретатором — запуск без окна консоли."""
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return candidate if candidate.exists() else Path(sys.executable)


def jarvis_exe() -> Path:
    """Копия pythonw.exe под именем «Джарвис.exe» — имя процесса в диспетчере.

    Диспетчер задач показывает имя файла, а не заголовок окна, поэтому
    переименованная копия интерпретатора видна как «Джарвис.exe». Python при
    этом работает нормально: свою библиотеку он находит по реестру, а не по
    имени файла — проверено на этой машине (``sys.prefix`` определяется верно,
    сторонние пакеты импортируются).

    Если скопировать не удалось (нет прав, занят файл) — возвращаем обычный
    ``pythonw.exe``: ярлык всё равно рабочий, просто имя процесса будет другим.
    """
    target = BASE_DIR / JARVIS_EXE_NAME
    source = pythonw()
    try:
        if not target.exists() or target.stat().st_size != source.stat().st_size:
            shutil.copy2(source, target)
    except OSError as exc:
        print(f"[i] Не удалось подготовить {JARVIS_EXE_NAME}: {exc}")
        return source
    return target


def create_shortcut(name: str, exe_copy: bool = True) -> int:
    try:
        import win32com.client  # pywin32
    except ImportError:
        print("Нужен pywin32: pip install pywin32")
        return 1

    target = jarvis_exe() if exe_copy else pythonw()
    main_py = BASE_DIR / "main.py"
    link = desktop_dir() / f"{name}.lnk"

    try:
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortcut(str(link))
        shortcut.TargetPath = str(target)
        shortcut.Arguments = f'"{main_py}" --background'
        shortcut.WorkingDirectory = str(BASE_DIR)
        shortcut.Description = DESCRIPTION
        shortcut.WindowStyle = 7          # свёрнуто (у pythonw окна нет вовсе)
        shortcut.Save()
    except Exception as exc:  # noqa: BLE001
        print(f"Не удалось создать ярлык: {exc}")
        return 1

    print(f"Ярлык создан: {link}")
    print(f"  команда:      {target.name} \"{main_py}\" --background")
    print(f"  рабочая папка: {BASE_DIR}")
    print("  окна консоли нет, лог — data/jarvis.log, интерфейс откроется в браузере.")
    print(f"  в диспетчере задач процесс виден как «{target.name}».")
    return 0


def remove_shortcut(name: str) -> int:
    link = desktop_dir() / f"{name}.lnk"
    removed = False
    if link.exists():
        link.unlink()
        print(f"Ярлык удалён: {link}")
        removed = True
    else:
        print(f"Ярлыка нет: {link}")

    # Копию интерпретатора убираем вместе с ярлыком — это её единственный смысл.
    exe = BASE_DIR / JARVIS_EXE_NAME
    if exe.exists():
        try:
            exe.unlink()
            print(f"Копия интерпретатора удалена: {exe}")
        except OSError as exc:
            print(f"[i] Не удалось удалить {exe}: {exc}")

    return 0 if removed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Ярлык Джарвиса на рабочем столе")
    parser.add_argument("--name", default=DEFAULT_NAME, help="имя ярлыка")
    parser.add_argument("--remove", action="store_true", help="удалить ярлык")
    parser.add_argument("--no-exe-copy", action="store_true",
                        help="не делать копию pythonw.exe под именем «Джарвис.exe»")
    args = parser.parse_args()

    if args.remove:
        return remove_shortcut(args.name)
    return create_shortcut(args.name, exe_copy=not args.no_exe_copy)


if __name__ == "__main__":
    sys.exit(main())
