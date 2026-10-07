@echo off
title AI Job Monitor Launcher
cd /d "%~dp0"

echo ============================================
echo   AI Job Monitor - Startup
echo ============================================
echo.

:: Start the dashboard server in its own window
echo [1/2] Starting dashboard server (port 8765)...
start "Job Monitor Dashboard" cmd /k "cd /d %~dp0 && python serve.py"

:: Start the job monitor in continuous mode in its own window
echo [2/2] Starting job monitor (continuous, every 30 min)...
start "Job Monitor" cmd /k "cd /d %~dp0 && python job_monitor.py"

:: Give servers a moment to start
timeout /t 2 /nobreak >nul

:: Open dashboard in browser
echo Opening dashboard...
start "" http://localhost:8765

echo.
echo Done. Two windows are now running:
echo   - "Job Monitor Dashboard"  (serve.py  - keep open for Run Now button)
echo   - "Job Monitor"            (job_monitor.py - watches jobs every 30 min)
echo.
echo Close those windows to stop.
echo.
pause
