@echo off
chcp 65001 >nul
pwsh.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0shoot-demo.ps1"
pause
