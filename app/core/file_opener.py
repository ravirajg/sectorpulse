"""Open restored files with the user's default associated applications."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


# Friendly hints shown in the UI (Windows still uses file associations).
APP_HINTS = {
    ".doc": "Microsoft Word",
    ".docx": "Microsoft Word",
    ".dotx": "Microsoft Word",
    ".rtf": "WordPad / Word",
    ".xls": "Microsoft Excel",
    ".xlsx": "Microsoft Excel",
    ".ppt": "Microsoft PowerPoint",
    ".pptx": "Microsoft PowerPoint",
    ".pdf": "PDF reader",
    ".txt": "Notepad",
    ".csv": "Excel / spreadsheet app",
    ".mp4": "VLC / media player",
    ".mkv": "VLC / media player",
    ".avi": "VLC / media player",
    ".mov": "VLC / media player",
    ".wmv": "VLC / media player",
    ".mp3": "VLC / media player",
    ".flac": "VLC / media player",
    ".wav": "VLC / media player",
    ".aac": "VLC / media player",
    ".jpg": "Photos",
    ".jpeg": "Photos",
    ".png": "Photos",
    ".gif": "Photos",
    ".bmp": "Photos",
    ".webp": "Photos",
    ".zip": "Explorer / archive tool",
    ".7z": "7-Zip",
    ".rar": "WinRAR / 7-Zip",
}


def app_hint_for(path: str) -> str:
    ext = Path(path).suffix.lower()
    return APP_HINTS.get(ext, "Default application")


def open_path(path: str) -> tuple[bool, str]:
    """Open a file or folder with the Windows shell association."""
    if not path or not os.path.exists(path):
        return False, "File not found. Restore it first."

    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
            return True, f"Opened with {app_hint_for(path)}"
        # Fallbacks for non-Windows (shouldn't be needed)
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True, "Opened"
    except OSError as exc:
        return False, str(exc)


def reveal_in_explorer(path: str) -> tuple[bool, str]:
    """Select the file in Windows Explorer."""
    if not path or not os.path.exists(path):
        return False, "Path not found."
    try:
        if os.path.isdir(path):
            subprocess.Popen(["explorer", path])
        else:
            subprocess.Popen(["explorer", "/select,", path])
        return True, "Opened in Explorer"
    except OSError as exc:
        return False, str(exc)
