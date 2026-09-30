@echo off
chcp 65001 >nul
title Jarvis Assistant
cd /d "%~dp0"

rem Путь к Python, в котором установлены зависимости Джарвиса.
set "PY=C:\Users\dima\AppData\Local\Programs\Python\Python314\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" main.py %*
echo.
echo --- Джарвис завершил работу ---
pause
