@echo off
chcp 65001 >nul
title Pyatnitsa Assistant
cd /d "%~dp0"

rem Путь к Python, в котором установлены зависимости Пятницаа.
set "PY=C:\Users\dima\AppData\Local\Programs\Python\Python314\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" main.py %*
echo.
echo --- Пятница завершил работу ---
pause
