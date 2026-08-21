"""Delete files/folders from healthy paths or raw NTFS volumes."""

from __future__ import annotations

import ctypes
import os
import shutil
import struct
import threading
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Callable, Optional

from .ntfs_raw import (
    GENERIC_READ,
    FILE_SHARE_READ,
    FILE_SHARE_WRITE,
    OPEN_EXISTING,
    RawVolume,
    DataRun,
    apply_fix_fixup,
    _parse_file_record,
)
from .scanner import FileNode

GENERIC_WRITE = 0x40000000
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_UNLOCK_VOLUME = 0x0009001C
FSCTL_DISMOUNT_VOLUME = 0x00090020

# Never auto-target these without an explicit name match from the user tree
PROTECTED_NAMES = {
    "$mft",
    "$mftmirr",
    "$logfile",
    "$volume",
    "$attrdef",
    "$bitmap",
    "$boot",
    "$badclus",
    "$secure",
    "$upcase",
    "$extend",
    "system volume information",
}


@dataclass
class DeleteResult:
    path: str
    success: bool
    error: str = ""


@dataclass
class DeleteBatchResult:
    results: list[DeleteResult] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.success)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if not r.success)


def _is_protected(node: FileNode) -> bool:
    name = (node.name or "").strip().lower()
    if name in PROTECTED_NAMES:
        return True
    if name.startswith("$") and len(name) <= 16:
        return True
    return False


def collect_delete_targets(nodes: list[FileNode]) -> list[FileNode]:
    """Expand folders depth-first (children before parents) for deletion order."""
    ordered: list[FileNode] = []

    def walk(n: FileNode) -> None:
        if _is_protected(n):
            return
        if n.is_dir:
            for child in list(n.children):
                walk(child)
        ordered.append(n)

    for node in nodes:
        walk(node)

    # Deduplicate by identity / mft ref / path
    seen: set[str] = set()
    unique: list[FileNode] = []
    for n in ordered:
        key = str(getattr(n, "_mft_ref", None) or os.path.normcase(n.path))
        if key in seen:
            continue
        seen.add(key)
        unique.append(n)
    return unique


class VolumeDeleter:
    """Delete selected nodes via Win32 paths or raw MFT in-use clearing."""

    def __init__(self) -> None:
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def delete_nodes(
        self,
        nodes: list[FileNode],
        drive_letter: str,
        on_progress: Optional[Callable[[str, int, int, DeleteResult], None]] = None,
    ) -> DeleteBatchResult:
        self._cancel.clear()
        batch = DeleteBatchResult()
        targets = collect_delete_targets(nodes)
        total = len(targets)
        if total == 0:
            return batch

        raw_mode = any(getattr(n, "_raw_volume", None) is not None for n in targets)
        writer: Optional[_RawMftDeleter] = None
        if raw_mode:
            writer = _RawMftDeleter(drive_letter)
            writer.open()

        try:
            for index, node in enumerate(targets, start=1):
                if self._cancel.is_set():
                    break
                if _is_protected(node):
                    result = DeleteResult(node.name, False, "Protected system object")
                elif writer is not None and getattr(node, "_mft_ref", None) is not None:
                    result = writer.delete_node(node)
                else:
                    result = self._delete_via_filesystem(node, drive_letter)
                batch.results.append(result)
                if on_progress:
                    on_progress(node.name, index, total, result)
        finally:
            if writer is not None:
                writer.close()

        return batch

    def _delete_via_filesystem(self, node: FileNode, drive_letter: str) -> DeleteResult:
        rel = getattr(node, "_rel", None) or node.name
        letter = drive_letter.rstrip("\\/")
        if len(letter) == 1:
            letter += ":"
        path = node.path
        if getattr(node, "_raw_mode", False) or path.lower().startswith(letter.lower() + "\\raw\\"):
            path = os.path.join(letter + "\\", rel.replace("/", "\\"))

        try:
            if node.is_dir or os.path.isdir(path):
                shutil.rmtree(path, onerror=self._rmtree_onerror)
            else:
                os.chmod(path, 0o666)
                os.remove(path)
            return DeleteResult(path, True)
        except OSError as exc:
            return DeleteResult(path, False, str(exc))

    @staticmethod
    def _rmtree_onerror(func, path, _exc_info) -> None:
        try:
            os.chmod(path, 0o666)
            func(path)
        except OSError:
            pass


class _RawMftDeleter:
    """Mark NTFS MFT records as not in-use (raw volume write)."""

    def __init__(self, letter: str):
        self.letter = letter.rstrip("\\/")
        if len(self.letter) == 1:
            self.letter += ":"
        self.vol = RawVolume(self.letter)
        self._locked = False
        self._mft_runs: list[DataRun] = []
        self._mft_size = 0

    def open(self) -> None:
        # Re-open underlying handle with write access.
        self.vol.open()
        self._load_mft_layout()
        self._reopen_read_write()

    def close(self) -> None:
        if self._locked and self.vol._handle:
            self._device_io(FSCTL_UNLOCK_VOLUME)
            self._locked = False
        self.vol.close()

    def _device_io(self, code: int) -> bool:
        if not self.vol._handle:
            return False
        ret = wintypes.DWORD()
        return bool(
            ctypes.windll.kernel32.DeviceIoControl(
                self.vol._handle, code, None, 0, None, 0, ctypes.byref(ret), None
            )
        )

    def _reopen_read_write(self) -> None:
        """Upgrade to a writable handle; lock/dismount if Windows requires it."""
        path = self.vol.path
        self.vol.close()
        CreateFileW = ctypes.windll.kernel32.CreateFileW
        CreateFileW.restype = wintypes.HANDLE
        invalid = ctypes.c_void_p(-1).value
        handle = CreateFileW(
            path,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle in (None, 0, invalid, 0xFFFFFFFF):
            err = ctypes.GetLastError()
            # Retry read-only layout already known — try exclusive write
            handle = CreateFileW(
                path,
                GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                0,
                None,
            )
            if handle in (None, 0, invalid, 0xFFFFFFFF):
                raise OSError(
                    err,
                    f"Cannot open {path} for writing (WinError {err}). "
                    f"Try running SectorPulse as Administrator.",
                )
        self.vol._handle = int(handle)
        # Lock helps writes succeed on mounted volumes.
        if self._device_io(FSCTL_LOCK_VOLUME):
            self._locked = True
        else:
            # Dismount then lock — last resort for stubborn mounts.
            self._device_io(FSCTL_DISMOUNT_VOLUME)
            if self._device_io(FSCTL_LOCK_VOLUME):
                self._locked = True

    def _load_mft_layout(self) -> None:
        first = self.vol.read_at(
            self.vol.mft_lcn * self.vol.bytes_per_cluster, self.vol.file_record_size
        )
        mft0 = _parse_file_record(first, 0, self.vol.file_record_size)
        if mft0 and mft0.runs:
            self._mft_runs = list(mft0.runs)
            self._mft_size = mft0.size or sum(r.length for r in mft0.runs) * self.vol.bytes_per_cluster
        else:
            self._mft_runs = [DataRun(lcn=self.vol.mft_lcn, length=64)]
            self._mft_size = 64 * self.vol.bytes_per_cluster

    def _mft_offset(self, mft_ref: int) -> int:
        record_size = self.vol.file_record_size
        byte_off = mft_ref * record_size
        if byte_off >= self._mft_size and self._mft_size > 0:
            raise OSError(f"MFT reference {mft_ref} past end of $MFT")
        remaining = byte_off
        for run in self._mft_runs:
            run_bytes = run.length * self.vol.bytes_per_cluster
            if remaining < run_bytes:
                if run.lcn < 0:
                    raise OSError(f"MFT reference {mft_ref} falls in a sparse hole")
                return run.lcn * self.vol.bytes_per_cluster + remaining
            remaining -= run_bytes
        raise OSError(f"MFT reference {mft_ref} not found in $MFT runs")

    def _write_at(self, offset: int, data: bytes) -> None:
        if not self.vol._handle:
            raise OSError("Volume not open for write")
        sector = self.vol.bytes_per_sector
        # Writes to raw volumes should be sector-aligned.
        aligned_off = (offset // sector) * sector
        skip = offset - aligned_off
        total = skip + len(data)
        aligned_size = ((total + sector - 1) // sector) * sector
        buf = bytearray(self.vol._read_exact(aligned_off, aligned_size))
        buf[skip : skip + len(data)] = data

        kernel32 = ctypes.windll.kernel32
        dist = ctypes.c_longlong(aligned_off)
        newpos = ctypes.c_longlong()
        if not kernel32.SetFilePointerEx(self.vol._handle, dist, ctypes.byref(newpos), 0):
            raise OSError(ctypes.GetLastError(), f"Seek failed at {aligned_off}")
        written = wintypes.DWORD()
        cbuf = ctypes.create_string_buffer(bytes(buf))
        if not kernel32.WriteFile(self.vol._handle, cbuf, len(buf), ctypes.byref(written), None):
            raise OSError(ctypes.GetLastError(), f"Write failed at {aligned_off}")

    @staticmethod
    def _apply_write_usa(buf: bytearray) -> None:
        """Refresh NTFS update-sequence before writing a FILE record back."""
        if len(buf) < 48 or buf[:4] != b"FILE":
            raise OSError("Not a FILE record")
        usa_offset = struct.unpack_from("<H", buf, 0x04)[0]
        usa_count = struct.unpack_from("<H", buf, 0x06)[0]
        if usa_offset <= 0 or usa_count < 2:
            return
        # Keep existing USN or invent one.
        usn = bytes(buf[usa_offset : usa_offset + 2]) or b"\x01\x00"
        usa = bytearray(usn)
        block = 512
        for i in range(1, usa_count):
            pos = i * block - 2
            if pos + 2 > len(buf):
                break
            usa += buf[pos : pos + 2]
            buf[pos : pos + 2] = usn
        buf[usa_offset : usa_offset + len(usa)] = usa

    def delete_node(self, node: FileNode) -> DeleteResult:
        mft_ref = getattr(node, "_mft_ref", None)
        label = getattr(node, "_rel", None) or node.name
        if mft_ref is None:
            return DeleteResult(label, False, "No MFT reference (rescan required)")
        if mft_ref < 16:
            return DeleteResult(label, False, "Refusing to delete NTFS metadata records")
        try:
            offset = self._mft_offset(int(mft_ref))
            raw = bytearray(self.vol.read_at(offset, self.vol.file_record_size))
            if raw[:4] != b"FILE":
                return DeleteResult(label, False, "MFT record missing FILE signature")
            # Undo USA so we can edit, then re-apply for write.
            usa_offset = struct.unpack_from("<H", raw, 0x04)[0]
            usa_count = struct.unpack_from("<H", raw, 0x06)[0]
            apply_fix_fixup(raw, usa_offset, usa_count)
            flags = struct.unpack_from("<H", raw, 0x16)[0]
            if not (flags & 0x01):
                return DeleteResult(label, True, "already deleted")
            struct.pack_into("<H", raw, 0x16, flags & ~0x01)
            self._apply_write_usa(raw)
            self._write_at(offset, bytes(raw))
            return DeleteResult(label, True)
        except OSError as exc:
            return DeleteResult(label, False, str(exc))
