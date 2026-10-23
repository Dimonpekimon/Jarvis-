@echo off
chcp 65001 >nul
title Jarvis Overlay
cd /d "%~dp0"

rem Запуск ассистента с настольным оверлеем (Electron).
rem При первом запуске оверлей соберётся сам — это займёт пару минут.
set "PY=C:\Users\dima\AppData\Local\Programs\Python\Python314\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" main.py --desktop %*
echo.
echo --- Джарвис завершил работу ---
pause
