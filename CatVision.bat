@echo off
rem Start CatVision without a console window.
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw -m catvision) || (start "" pythonw -m catvision)
