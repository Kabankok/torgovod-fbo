# Лаунчер «Торговод · Отгрузки FBO».
# Вся логика запуска здесь (PowerShell надёжно работает с кириллицей и опросом порта).
# Вызывается из start.bat. Делает: окружение -> зависимости -> ярлык -> гасит старый
# сервер на :4000 -> открывает браузер, когда сервер ответит -> запускает сервер.

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$root = Split-Path -Parent $PSScriptRoot     # tools\ -> корень проекта
Set-Location $root
$py = Join-Path $root '.venv\Scripts\python.exe'

Write-Host '============================================'
Write-Host ' Торговод · Отгрузки FBO'
Write-Host '============================================'

# [1/4] Виртуальное окружение
if (-not (Test-Path $py)) {
    Write-Host '[1/4] Создаю окружение Python...'
    try { python -m venv (Join-Path $root '.venv') } catch {}
    if (-not (Test-Path $py)) {
        Write-Host ''
        Write-Host 'Не найден Python. Установите Python 3.11+ с https://www.python.org/downloads/'
        Write-Host '(при установке отметьте "Add Python to PATH") и запустите ярлык снова.'
        Read-Host 'Нажмите Enter, чтобы закрыть'
        exit 1
    }
}

# [2/4] Зависимости (ставим один раз; чтобы переустановить — удалите .venv\.deps_ok)
$depsFlag = Join-Path $root '.venv\.deps_ok'
if (-not (Test-Path $depsFlag)) {
    Write-Host '[2/4] Устанавливаю зависимости, это разово...'
    & $py -m pip install --quiet --upgrade pip
    & $py -m pip install --quiet -r (Join-Path $root 'requirements.txt')
    if ($LASTEXITCODE -ne 0) {
        Read-Host 'Ошибка установки зависимостей. Нажмите Enter, чтобы закрыть'
        exit 1
    }
    'ok' | Out-File -FilePath $depsFlag -Encoding ascii
}

# [3/4] Ярлык на рабочем столе + гасим старый сервер на порту 4000
Write-Host '[3/4] Готовлю ярлык и освобождаю порт...'
try { & (Join-Path $PSScriptRoot 'make_shortcut.ps1') } catch { Write-Host "Ярлык: $_" }
Get-NetTCPConnection -LocalPort 4000 -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }

# [4/4] Откроем браузер, как только сервер ответит, и запустим сервер
Write-Host '[4/4] Запускаю Торговод на http://localhost:4000 ...'
$opener = 'for($i=0;$i -lt 120;$i++){try{Invoke-WebRequest -Uri "http://localhost:4000" -UseBasicParsing -TimeoutSec 2 | Out-Null; break}catch{Start-Sleep -Milliseconds 500}}; Start-Process "http://localhost:4000"'
Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile', '-Command', $opener | Out-Null

& $py -m uvicorn dashboard.app:app --port 4000
