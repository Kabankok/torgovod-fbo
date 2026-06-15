# Создаёт (или обновляет) ярлык «Торговод FBO» на рабочем столе.
# Ярлык запускает start.bat: гасит старый сервер, поднимает новый и открывает браузер.
# Запускается автоматически из start.bat; можно и вручную:
#   powershell -ExecutionPolicy Bypass -File tools\make_shortcut.ps1

$ErrorActionPreference = 'Stop'

$root    = Split-Path -Parent $PSScriptRoot   # tools\ -> корень проекта
$target  = Join-Path $root 'start.bat'
$icon    = Join-Path $root 'assets\torgovod.ico'
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk     = Join-Path $desktop 'Торговод FBO.lnk'

$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath       = $target
$sc.WorkingDirectory = $root
$sc.Description      = 'Торговод · Отгрузки FBO — запустить'
$sc.WindowStyle      = 1
if (Test-Path $icon) { $sc.IconLocation = "$icon,0" }
$sc.Save()

Write-Host "Ярлык на рабочем столе готов: $lnk"
