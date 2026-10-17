@echo off
chcp 65001 >nul
title Time Tracker Report
cd /d "%~dp0"
echo.
echo   Generating today's visual report...
py time_tracker.py html --open
echo.
echo   The report is opened in your browser. This window closes in a few seconds.
echo.
ping 127.0.0.1 -n 7 >nul
