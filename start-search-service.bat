@echo off
cd /d "%~dp0"
py -3 -m stormcopy --db workshop.sqlite serve-search
if errorlevel 1 pause
