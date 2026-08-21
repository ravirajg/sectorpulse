"""
SectorPulse — resilient file recovery from damaged Windows drives.

Run:  python main.py
  or: run.bat
"""

from __future__ import annotations

import sys


def main() -> int:
    if sys.platform != "win32":
        print("SectorPulse is designed for Windows.")
        return 1

    from app.ui.main_window import RecoveryApp

    app = RecoveryApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
