#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "============================================"
echo " Торговод · Отгрузки FBO — запуск"
echo "============================================"

if [ ! -d .venv ]; then
  echo "Создаю виртуальное окружение..."
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "Устанавливаю зависимости..."
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

# Гасим старый сервер на порту 4000, если запущен
if command -v lsof >/dev/null 2>&1; then
  OLD_PID="$(lsof -ti tcp:4000 2>/dev/null || true)"
  if [ -n "$OLD_PID" ]; then
    echo "Останавливаю старый сервер (PID $OLD_PID)..."
    kill -9 $OLD_PID 2>/dev/null || true
  fi
fi

echo "Открываю http://localhost:4000 ..."
( sleep 2; (command -v xdg-open >/dev/null && xdg-open http://localhost:4000) || (command -v open >/dev/null && open http://localhost:4000) ) >/dev/null 2>&1 &

python -m uvicorn dashboard.app:app --port 4000
