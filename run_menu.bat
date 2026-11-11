@echo off
setlocal
title Comic Translation & Inpainting Pipeline

where pwsh >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0menu.ps1" %*
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0menu.ps1" %*
)

endlocal
