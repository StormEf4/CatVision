@echo off
rem Build CatVision.exe yourself (normally GitHub builds it for you - see README).
rem Needs Python 3.9+ installed.
cd /d "%~dp0"
py -m pip install pyinstaller==6.11.1 pystray==0.19.5 pillow==11.0.0 || goto :fail
py -m PyInstaller --noconfirm --clean --onefile --windowed --name CatVision ^
  --icon catvision\assets\catvision.ico ^
  --add-data "catvision\assets;catvision\assets" ^
  --hidden-import pystray._win32 ^
  --version-file packaging\version_info.txt ^
  run_catvision.py || goto :fail
echo.
echo Done. Your program is dist\CatVision.exe
pause
exit /b 0
:fail
echo Build failed.
pause
exit /b 1
