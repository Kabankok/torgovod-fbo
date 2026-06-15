@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo  Торговод · Отгрузки FBO — запуск
echo ============================================

if not exist .venv (
    echo Создаю виртуальное окружение...
    python -m venv .venv
)
call .venv\Scripts\activate.bat

echo Устанавливаю зависимости...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

echo Открываю http://localhost:4000 ...
start "" http://localhost:4000

python -m uvicorn dashboard.app:app --port 4000
