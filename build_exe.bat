@echo off
rem Optional: build a standalone CatVision.exe (needs: pip install pyinstaller)
cd /d "%~dp0"
py -m PyInstaller --noconfirm --onefile --windowed --name CatVision run_catvision.py
echo.
echo Done. Your program is in the dist folder.
pause
