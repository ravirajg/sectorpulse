"""Detect and watch for USB / external drives."""

from __future__ import annotations

import string
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import psutil


@dataclass(frozen=True)
class DriveInfo:
    letter: str
    path: str
    label: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    filesystem: str
    drive_type: str
    is_removable: bool
    is_accessible: bool = True
    error: str = ""

    @property
    def display_name(self) -> str:
        label = self.label or ("Damaged" if not self.is_accessible else "Unlabeled")
        if not self.is_accessible:
            return f"{self.letter}  {label}  (unreadable)"
        gb = self.total_bytes / (1024**3)
        return f"{self.letter}  {label}  ({gb:.1f} GB · {self.filesystem})"

    @property
    def health_hint(self) -> str:
        if not self.is_accessible:
            return "Corrupted / unreadable volume"
        if self.is_removable:
            return "External / USB"
        return "Fixed volume"


def _logical_drive_letters() -> list[str]:
    """Fast bitmask of mounted drive letters (no I/O on the volume)."""
    try:
        import ctypes

        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        return []
    return [f"{c}:" for i, c in enumerate(string.ascii_uppercase) if mask & (1 << i)]


def _windows_drive_type(letter: str) -> tuple[str, bool]:
    """Return (type_name, is_removable) via WinAPI GetDriveTypeW."""
    try:
        import ctypes

        path = f"{letter}\\"
        dtype = ctypes.windll.kernel32.GetDriveTypeW(path)
        # 2=REMOVABLE, 3=FIXED, 4=REMOTE, 5=CDROM, 6=RAMDISK
        mapping = {
            2: ("Removable", True),
            3: ("Fixed", False),
            4: ("Network", False),
            5: ("Optical", False),
            6: ("RAM", False),
        }
        return mapping.get(dtype, ("Unknown", False))
    except Exception:
        return ("Unknown", False)


def _system_drive_letter() -> str:
    import os

    windir = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or "C:\\Windows"
    return windir[0].upper() + ":"


def _format_os_error(exc: BaseException) -> str:
    if isinstance(exc, OSError) and getattr(exc, "winerror", None):
        return f"WinError {exc.winerror}: {exc.strerror or exc}"
    return str(exc) or type(exc).__name__


class DriveMonitor:
    """Poll for newly attached non-system volumes (USB / external)."""

    def __init__(self, interval: float = 2.0):
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._known: set[str] = set()
        self._system = _system_drive_letter()
        self._cache: list[DriveInfo] = []
        self._last_mask_letters: set[str] = set()
        self.on_drive_added: Optional[Callable[[DriveInfo], None]] = None
        self.on_drive_removed: Optional[Callable[[str], None]] = None
        self.on_drives_updated: Optional[Callable[[list[DriveInfo]], None]] = None

    def list_candidate_drives(self, force: bool = True) -> list[DriveInfo]:
        """Enumerate candidate volumes.

        Expensive volume probes (disk_usage / volume info) only run when
        ``force`` is True or the set of mounted letters changed. Damaged
        volumes that fail size queries are still returned as inaccessible.
        """
        present = set(_logical_drive_letters())
        if not force and present == self._last_mask_letters and self._cache:
            return list(self._cache)

        drives: list[DriveInfo] = []
        # Resolve partition metadata once per refresh (cheap vs per-letter).
        partitions: dict[str, str] = {}
        try:
            for part in psutil.disk_partitions(all=True):
                key = part.device.upper().rstrip("\\")
                if len(key) >= 2 and key[1] == ":":
                    partitions[key[:2]] = part.fstype or ""
        except Exception:
            pass

        for letter in sorted(present):
            if letter.upper() == self._system:
                continue

            dtype_name, is_removable = _windows_drive_type(letter)
            if dtype_name in ("Network", "Optical", "RAM"):
                continue

            path = letter + "\\"
            total = used = free = 0
            accessible = True
            error = ""
            try:
                usage = psutil.disk_usage(path)
                total, used, free = usage.total, usage.used, usage.free
            except (PermissionError, FileNotFoundError, OSError) as exc:
                # Bad / crashing disks often still mount a letter in Explorer
                # but fail size queries (e.g. WinError 1392). Keep them listed.
                accessible = False
                error = _format_os_error(exc)

            fstype = partitions.get(letter.upper(), "")
            vol_label = ""
            if accessible:
                # GetVolumeInformation can stall on dying disks — skip when
                # the volume already failed basic usage.
                vol_label = self._volume_label(letter) or ""
                if not fstype:
                    fstype = self._filesystem(letter)

            drives.append(
                DriveInfo(
                    letter=letter,
                    path=path,
                    label=vol_label or ("Damaged" if not accessible else ""),
                    total_bytes=total,
                    used_bytes=used,
                    free_bytes=free,
                    filesystem=fstype or ("?" if not accessible else "?"),
                    drive_type=dtype_name,
                    is_removable=is_removable or dtype_name == "Removable",
                    is_accessible=accessible,
                    error=error,
                )
            )

        # Damaged first (recovery targets), then removable, then letter.
        drives.sort(key=lambda d: (d.is_accessible, not d.is_removable, d.letter))
        self._cache = drives
        self._last_mask_letters = present
        return list(drives)

    @staticmethod
    def _volume_label(letter: str) -> str:
        try:
            import ctypes
            from ctypes import wintypes

            volume_name = ctypes.create_unicode_buffer(261)
            fs_name = ctypes.create_unicode_buffer(261)
            serial = wintypes.DWORD()
            max_comp = wintypes.DWORD()
            flags = wintypes.DWORD()
            ok = ctypes.windll.kernel32.GetVolumeInformationW(
                f"{letter}\\",
                volume_name,
                261,
                ctypes.byref(serial),
                ctypes.byref(max_comp),
                ctypes.byref(flags),
                fs_name,
                261,
            )
            if ok:
                return volume_name.value
        except Exception:
            pass
        return ""

    @staticmethod
    def _filesystem(letter: str) -> str:
        try:
            import ctypes
            from ctypes import wintypes

            volume_name = ctypes.create_unicode_buffer(261)
            fs_name = ctypes.create_unicode_buffer(261)
            serial = wintypes.DWORD()
            max_comp = wintypes.DWORD()
            flags = wintypes.DWORD()
            ok = ctypes.windll.kernel32.GetVolumeInformationW(
                f"{letter}\\",
                volume_name,
                261,
                ctypes.byref(serial),
                ctypes.byref(max_comp),
                ctypes.byref(flags),
                fs_name,
                261,
            )
            if ok:
                return fs_name.value
        except Exception:
            pass
        return "?"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        initial = self.list_candidate_drives(force=True)
        self._known = {d.letter for d in initial}
        self._thread = threading.Thread(target=self._loop, daemon=True, name="DriveMonitor")
        self._thread.start()
        if self.on_drives_updated:
            self.on_drives_updated(initial)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            # Cheap presence check — avoid hammering dying disks every tick.
            present = set(_logical_drive_letters())
            if present != self._last_mask_letters:
                current = self.list_candidate_drives(force=True)
            else:
                current = list(self._cache)

            letters = {d.letter for d in current}
            added = letters - self._known
            removed = self._known - letters
            for letter in sorted(added):
                info = next((d for d in current if d.letter == letter), None)
                if info and self.on_drive_added:
                    self.on_drive_added(info)
            for letter in sorted(removed):
                if self.on_drive_removed:
                    self.on_drive_removed(letter)
            if added or removed:
                if self.on_drives_updated:
                    self.on_drives_updated(current)
            self._known = letters
            self._stop.wait(self.interval)
