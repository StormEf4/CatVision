"""Launcher script (double-click, or used by build_exe.bat for PyInstaller)."""

import sys

from catvision.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
