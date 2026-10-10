@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0save-yandex-smtp-password.ps1" %*
pause
