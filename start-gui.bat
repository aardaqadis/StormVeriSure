@echo off
cd /d "%~dp0"
py -3 -m stormcopy gui
if errorlevel 1 pause
