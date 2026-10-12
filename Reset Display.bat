@echo off
rem Emergency: put every display back to normal brightness.
cd /d "%~dp0"
where py >nul 2>nul && (py -m catvision --reset) || (python -m catvision --reset)
pause
