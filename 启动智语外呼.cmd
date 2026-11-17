@echo off
setlocal
cd /d "%~dp0"
python -X utf8 scripts\launch.py %*
if errorlevel 1 pause
