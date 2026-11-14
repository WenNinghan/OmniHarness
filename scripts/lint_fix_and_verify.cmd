@echo off
rem Double-click: auto-fix import order, then run all offline checks. No model or network calls.
cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0lint_fix_and_verify.ps1"
echo.
echo Finished. Logs are in .omniharness\context-v3-verify-*
pause
