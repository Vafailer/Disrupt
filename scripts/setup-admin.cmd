@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-admin.ps1" %*
pause
