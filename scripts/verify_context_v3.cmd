@echo off
rem Double-click to run the context-v3 offline checks. No model or network calls.
cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0verify_context_v3.ps1"
echo.
echo Finished. Logs are in .omniharness\context-v3-verify-*
pause
