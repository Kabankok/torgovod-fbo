@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Торговод · Отгрузки FBO

echo ============================================
echo  Торговод · Отгрузки FBO
echo ============================================

REM [1/4] Виртуальное окружение
if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Создаю окружение Python...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo Не найден Python. Установите Python 3.11+ с python.org
        echo и при установке отметьте "Add Python to PATH".
        pause
        exit /b 1
    )
)
set "PY=.venv\Scripts\python.exe"

REM [2/4] Зависимости (ставим один раз; чтобы переустановить - удалите .venv\.deps_ok)
if not exist ".venv\.deps_ok" (
    echo [2/4] Устанавливаю зависимости, это разово...
    "%PY%" -m pip install --quiet --upgrade pip
    "%PY%" -m pip install --quiet -r requirements.txt
    if errorlevel 1 ( echo Ошибка установки зависимостей. & pause & exit /b 1 )
    echo ok> ".venv\.deps_ok"
)

REM [3/4] Ярлык на рабочем столе + гасим старый сервер на порту 4000
echo [3/4] Готовлю ярлык и освобождаю порт...
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0tools\make_shortcut.ps1" 2>nul
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":4000 " ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1

REM [4/4] Откроем браузер, как только сервер ответит, и запустим сервер
echo [4/4] Запускаю Торговод на http://localhost:4000 ...
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "for($i=0;$i -lt 40;$i++){try{Invoke-WebRequest -Uri 'http://localhost:4000' -UseBasicParsing -TimeoutSec 1 ^| Out-Null; break}catch{Start-Sleep -Milliseconds 500}}; Start-Process 'http://localhost:4000'"

"%PY%" -m uvicorn dashboard.app:app --port 4000
