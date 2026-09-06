@echo off
cd /d "%~dp0"
echo Syncing dependencies...
uv sync
echo Starting Command Center server...
start "Command Center Server" cmd /k uv run uvicorn command_center.app:app --reload --port 8000
timeout /t 3 /nobreak >nul
start "" http://localhost:8000
