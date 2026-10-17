@echo off
chcp 65001 >nul
title Time Tracker Report (Native)
cd /d "%~dp0"
echo.
echo   Opening native report window (no browser needed)...
py time_tracker.py gui
if errorlevel 1 (
  echo.
  echo   Failed to open the window. Try: py time_tracker.py report
  pause
)
