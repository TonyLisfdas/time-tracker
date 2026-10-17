@echo off
chcp 65001 >nul
title Time Tracker Live Report
cd /d "%~dp0"
echo.
echo   Starting live report service (no window, background)...
py time_tracker.py serve
echo.
echo   The service runs in the background. This window closes in a few seconds.
echo   Stop it with: py time_tracker.py serve stop
echo.
ping 127.0.0.1 -n 7 >nul
