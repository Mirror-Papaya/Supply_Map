@echo off
rem Start Warehouse Map (web editor + Feishu bot). Close this window to stop.
cd /d "%~dp0"
title Warehouse Map
if not exist ".env" copy ".env.example" ".env" >nul
start "" /b cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8765"
".venv\Scripts\python.exe" run.py
pause
