@echo off
REM ASCII-only launcher. All logic lives in tools\start.ps1 (handles Cyrillic + port polling reliably).
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0tools\start.ps1"
