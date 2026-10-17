@echo off
chcp 65001 >nul
title Time Tracker
cd /d "%~dp0"
echo.
echo   Starting background auto-monitor...
py time_tracker.py daemon start
echo.
echo   Monitor is running in background. This window closes in a few seconds.
echo   View today : py time_tracker.py today   (or report / html)
echo.
ping 127.0.0.1 -n 7 >nul
