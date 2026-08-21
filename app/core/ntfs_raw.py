"""Raw NTFS access via \\\\.\\X: — works when the Win32 path is unreadable."""

from __future__ import annotations

import ctypes
import os
import struct
import threading
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

from .scanner import FileNode, NodeStatus

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3

ATTR_ATTRIBUTE_LIST = 0x20
ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80
ATTR_END = 0xFFFFFFFF

FILE_NAME_WIN32 = 1
FILE_NAME_WIN32_AND_DOS = 3

ROOT_MFT_REF = 5


@dataclass
class DataRun:
    lcn: int  # -1 = sparse hole
    length: int  # clusters


@dataclass
class MftFile:
    mft_ref: int
    parent_ref: int
    name: str
    is_dir: bool
    size: int
    in_use: bool
    resident_data: bytes = b""
    runs: list[DataRun] = field(default_factory=list)
    has_attr_list: bool = False
    data_incomplete: bool = False


class RawVolume:
    """Sector-level reader for a mounted volume (\\\\.\\X:)."""

    def __init__(self, letter: str):
        letter = letter.rstrip("\\/")
        if len(letter) == 1:
            letter = letter + ":"
        self.letter = letter.upper()
        self.path = f"\\\\.\\{self.letter}"
        self._handle: Optional[int] = None
        self.bytes_per_sector = 512
        self.sectors_per_cluster = 8
        self.bytes_per_cluster = 4096
        self.mft_lcn = 0
        self.file_record_size = 1024

    def open(self) -> None:
        CreateFileW = ctypes.windll.kernel32.CreateFileW
        CreateFileW.restype = wintypes.HANDLE
        handle = CreateFileW(
            self.path,
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle in (None, 0, invalid, 0xFFFFFFFF):
            err = ctypes.GetLastError()
            raise OSError(err, f"Cannot open raw volume {self.path} (WinError {err})")
        self._handle = int(handle)
        self._parse_boot()

    def close(self) -> None:
        if self._handle:
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "RawVolume":
        self.open()
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def read_at(self, offset: int, size: int, retries: int = 4) -> bytes:
        if not self._handle:
            raise OSError("Volume not open")
        if size <= 0:
            return b""
        sector = self.bytes_per_sector
        aligned_off = (offset // sector) * sector
        skip = offset - aligned_off
        aligned_size = ((skip + size + sector - 1) // sector) * sector

        last_err: Optional[OSError] = None
        for attempt in range(retries):
            try:
                raw = self._read_exact(aligned_off, aligned_size)
                return raw[skip : skip + size]
            except OSError as exc:
                last_err = exc
                if attempt + 1 < retries:
                    continue
        assert last_err is not None
        raise last_err

    def _read_exact(self, offset: int, size: int) -> bytes:
        kernel32 = ctypes.windll.kernel32
        dist = ctypes.c_longlong(offset)
        newpos = ctypes.c_longlong()
        if not kernel32.SetFilePointerEx(self._handle, dist, ctypes.byref(newpos), 0):
            raise OSError(ctypes.GetLastError(), f"Seek failed at {offset}")
        buf = ctypes.create_string_buffer(size)
        read = wintypes.DWORD()
        if not kernel32.ReadFile(self._handle, buf, size, ctypes.byref(read), None):
            raise OSError(ctypes.GetLastError(), f"Read failed at {offset}")
        if read.value != size:
            return buf.raw[: read.value] + bytes(size - read.value)
        return buf.raw

    def _parse_boot(self) -> None:
        boot = self._read_exact(0, 512)
        if boot[3:11] != b"NTFS    ":
            raise OSError(f"{self.letter} is not NTFS (oem={boot[3:11]!r})")
        self.bytes_per_sector = struct.unpack_from("<H", boot, 0x0B)[0] or 512
        self.sectors_per_cluster = boot[0x0D] or 8
        self.bytes_per_cluster = self.bytes_per_sector * self.sectors_per_cluster
        self.mft_lcn = struct.unpack_from("<q", boot, 0x30)[0]
        clusters_per_fr = struct.unpack_from("<b", boot, 0x40)[0]
        if clusters_per_fr > 0:
            self.file_record_size = clusters_per_fr * self.bytes_per_cluster
        else:
            self.file_record_size = 1 << (-clusters_per_fr)

    def read_clusters(self, lcn: int, count: int) -> bytes:
        if lcn < 0:
            return bytes(count * self.bytes_per_cluster)
        return self.read_at(lcn * self.bytes_per_cluster, count * self.bytes_per_cluster)


def apply_fix_fixup(buf: bytearray, usa_offset: int, usa_count: int, block_size: int = 512) -> None:
    if usa_offset <= 0 or usa_count < 2:
        return
    usa = buf[usa_offset : usa_offset + usa_count * 2]
    if len(usa) < usa_count * 2:
        return
    for i in range(1, usa_count):
        pos = i * block_size - 2
        if pos + 2 > len(buf):
            break
        buf[pos : pos + 2] = usa[i * 2 : i * 2 + 2]


def decode_runlist(data: bytes) -> list[DataRun]:
    runs: list[DataRun] = []
    offset = 0
    prev_lcn = 0
    while offset < len(data):
        header = data[offset]
        offset += 1
        if header == 0:
            break
        len_size = header & 0x0F
        off_size = (header >> 4) & 0x0F
        if len_size == 0 or offset + len_size + off_size > len(data):
            break
        length = int.from_bytes(data[offset : offset + len_size], "little")
        offset += len_size
        if off_size == 0:
            runs.append(DataRun(lcn=-1, length=length))
            continue
        rel = int.from_bytes(data[offset : offset + off_size], "little", signed=True)
        offset += off_size
        prev_lcn = prev_lcn + rel
        runs.append(DataRun(lcn=prev_lcn, length=length))
    return runs


def _fixup_record(record: bytes) -> Optional[bytearray]:
    if len(record) < 48 or record[:4] != b"FILE":
        return None
    buf = bytearray(record)
    apply_fix_fixup(
        buf,
        struct.unpack_from("<H", buf, 0x04)[0],
        struct.unpack_from("<H", buf, 0x06)[0],
    )
    return buf


def _iter_attrs(buf: bytearray):
    attrs_offset = struct.unpack_from("<H", buf, 0x14)[0]
    used = struct.unpack_from("<I", buf, 0x18)[0]
    limit = min(len(buf), used if used > 0 else len(buf))
    pos = attrs_offset
    while pos + 8 < limit:
        attr_type = struct.unpack_from("<I", buf, pos)[0]
        if attr_type == ATTR_END:
            break
        attr_len = struct.unpack_from("<I", buf, pos + 4)[0]
        if attr_len < 16 or pos + attr_len > len(buf):
            break
        yield pos, attr_type, attr_len
        pos += attr_len


def _extract_unnamed_data(buf: bytearray) -> tuple[int, bytes, list[DataRun], int]:
    size = 0
    resident = b""
    runs: list[DataRun] = []
    start_vcn = 0
    found = False
    for pos, attr_type, attr_len in _iter_attrs(buf):
        if attr_type != ATTR_DATA or buf[pos + 9] != 0:
            continue
        found = True
        if buf[pos + 8] == 0:
            content_len = struct.unpack_from("<I", buf, pos + 0x10)[0]
            content_off = struct.unpack_from("<H", buf, pos + 0x14)[0]
            resident = bytes(buf[pos + content_off : pos + content_off + content_len])
            size = content_len
            runs = []
            start_vcn = 0
        else:
            start_vcn = struct.unpack_from("<Q", buf, pos + 0x10)[0]
            size = struct.unpack_from("<Q", buf, pos + 0x30)[0]
            run_off = struct.unpack_from("<H", buf, pos + 0x20)[0]
            runs = decode_runlist(bytes(buf[pos + run_off : pos + attr_len]))
    if not found:
        return 0, b"", [], 0
    return size, resident, runs, start_vcn


def _parse_attribute_list(buf: bytearray) -> list[tuple[int, int]]:
    content = b""
    for pos, attr_type, attr_len in _iter_attrs(buf):
        if attr_type != ATTR_ATTRIBUTE_LIST:
            continue
        if buf[pos + 8] == 0:
            content_len = struct.unpack_from("<I", buf, pos + 0x10)[0]
            content_off = struct.unpack_from("<H", buf, pos + 0x14)[0]
            content = bytes(buf[pos + content_off : pos + content_off + content_len])
        break

    entries: list[tuple[int, int]] = []
    off = 0
    while off + 26 <= len(content):
        attr_type = struct.unpack_from("<I", content, off)[0]
        entry_len = struct.unpack_from("<H", content, off + 4)[0]
        if entry_len < 26 or off + entry_len > len(content):
            break
        mft_ref = struct.unpack_from("<Q", content, off + 16)[0] & 0xFFFFFFFFFFFF
        entries.append((attr_type, mft_ref))
        off += entry_len
    return entries


def _extract_file_name(buf: bytearray) -> tuple[str, int]:
    best_name = ""
    best_ns = -1
    parent_ref = 0
    for pos, attr_type, _attr_len in _iter_attrs(buf):
        if attr_type != ATTR_FILE_NAME or buf[pos + 8] != 0:
            continue
        content_len = struct.unpack_from("<I", buf, pos + 0x10)[0]
        content_off = struct.unpack_from("<H", buf, pos + 0x14)[0]
        content = bytes(buf[pos + content_off : pos + content_off + content_len])
        if len(content) < 0x42:
            continue
        parent_ref = struct.unpack_from("<Q", content, 0)[0] & 0xFFFFFFFFFFFF
        nlen = content[0x40]
        ns = content[0x41]
        try:
            name = content[0x42 : 0x42 + nlen * 2].decode("utf-16le", errors="replace")
        except Exception:
            name = ""
        score = {FILE_NAME_WIN32: 3, FILE_NAME_WIN32_AND_DOS: 2, 0: 1, 2: 0}.get(ns, 0)
        if name and score >= best_ns:
            best_ns = score
            best_name = name
    return best_name, parent_ref


def _merge_data_segments(
    segments: list[tuple[int, int, bytes, list[DataRun]]],
) -> tuple[int, bytes, list[DataRun]]:
    if not segments:
        return 0, b"", []
    segments = sorted(segments, key=lambda s: s[0])
    size = max((s[1] for s in segments), default=0)
    residents = [s[2] for s in segments if s[2]]
    if residents and not any(s[3] for s in segments):
        return size or len(residents[0]), residents[0], []
    runs: list[DataRun] = []
    for _vcn, _sz, _res, seg_runs in segments:
        runs.extend(seg_runs)
    return size, b"", runs


def _parse_file_record(
    record: bytes,
    mft_ref: int,
    record_size: int,
    record_map: Optional[dict[int, bytes]] = None,
) -> Optional[MftFile]:
    buf = _fixup_record(record)
    if buf is None:
        return None

    flags = struct.unpack_from("<H", buf, 0x16)[0]
    in_use = bool(flags & 0x01)
    is_dir = bool(flags & 0x02)
    base = struct.unpack_from("<Q", buf, 0x20)[0] & 0xFFFFFFFFFFFF
    if base != 0:
        return None

    best_name, parent_ref = _extract_file_name(buf)
    has_attr_list = any(t == ATTR_ATTRIBUTE_LIST for _, t, _ in _iter_attrs(buf))
    data_incomplete = False

    segments: list[tuple[int, int, bytes, list[DataRun]]] = []
    size, resident_data, runs, start_vcn = _extract_unnamed_data(buf)
    if resident_data or runs or size:
        segments.append((start_vcn, size, resident_data, runs))

    if has_attr_list and record_map is not None:
        seen_refs = {mft_ref}
        for attr_type, ext_ref in _parse_attribute_list(buf):
            if attr_type != ATTR_DATA or ext_ref in seen_refs:
                continue
            seen_refs.add(ext_ref)
            ext_raw = record_map.get(ext_ref)
            if not ext_raw:
                data_incomplete = True
                continue
            ext_buf = _fixup_record(ext_raw)
            if ext_buf is None:
                data_incomplete = True
                continue
            sz, res, ext_runs, vcn = _extract_unnamed_data(ext_buf)
            if res or ext_runs or sz:
                segments.append((vcn, sz, res, ext_runs))

    size, resident_data, runs = _merge_data_segments(segments)
    found_data = bool(resident_data or runs or size)

    if not best_name and mft_ref != ROOT_MFT_REF:
        if not in_use:
            return None
        best_name = f"orphan_{mft_ref}"

    if mft_ref == ROOT_MFT_REF:
        best_name = best_name or "."
        parent_ref = ROOT_MFT_REF

    if is_dir:
        size = 0
        resident_data = b""
        runs = []

    return MftFile(
        mft_ref=mft_ref,
        parent_ref=parent_ref,
        name=best_name,
        is_dir=is_dir,
        size=size,
        in_use=in_use,
        resident_data=resident_data,
        runs=runs,
        has_attr_list=has_attr_list,
        data_incomplete=data_incomplete or (has_attr_list and not found_data and not is_dir),
    )


def _iter_mft_records(
    vol: RawVolume,
    cancel: Optional[threading.Event] = None,
) -> Iterator[tuple[int, bytes]]:
    first = vol.read_at(vol.mft_lcn * vol.bytes_per_cluster, vol.file_record_size)
    mft0 = _parse_file_record(first, 0, vol.file_record_size)
    if not mft0 or (not mft0.runs and not mft0.resident_data):
        runs = [DataRun(lcn=vol.mft_lcn, length=64)]
        mft_size = 64 * vol.bytes_per_cluster
    else:
        runs = mft0.runs
        mft_size = mft0.size or sum(r.length for r in runs) * vol.bytes_per_cluster

    record_size = vol.file_record_size
    index = 0
    bytes_seen = 0
    for run in runs:
        if cancel and cancel.is_set():
            return
        if run.length <= 0:
            continue
        chunk_clusters = 64
        remaining = run.length
        lcn = run.lcn
        while remaining > 0:
            if cancel and cancel.is_set():
                return
            take = min(remaining, chunk_clusters)
            if lcn < 0:
                data = bytes(take * vol.bytes_per_cluster)
            else:
                try:
                    data = vol.read_clusters(lcn, take)
                except OSError:
                    data = bytes(take * vol.bytes_per_cluster)
            for off in range(0, len(data), record_size):
                if bytes_seen >= mft_size and index > 0:
                    return
                rec = data[off : off + record_size]
                if len(rec) < record_size:
                    rec = rec + bytes(record_size - len(rec))
                yield index, rec
                index += 1
                bytes_seen += record_size
            if lcn >= 0:
                lcn += take
            remaining -= take


class NtfsRawScanner:
    """Build a FileNode tree by walking the MFT over a raw volume handle."""

    def __init__(
        self,
        letter: str,
        on_progress: Optional[Callable[[str, int, int], None]] = None,
    ):
        self.letter = letter.rstrip("\\/").upper()
        if len(self.letter) == 1:
            self.letter += ":"
        self.on_progress = on_progress
        self._cancel = threading.Event()
        self.files_seen = 0
        self.dirs_seen = 0
        self.errors_seen = 0
        self.volume: Optional[RawVolume] = None

    def cancel(self) -> None:
        self._cancel.set()

    def scan(self) -> FileNode:
        self._cancel.clear()
        vol = RawVolume(self.letter)
        vol.open()
        self.volume = vol

        # Phase 1: cache raw MFT records so attribute-list extensions resolve.
        record_map: dict[int, bytes] = {}
        scanned = 0
        for index, raw in _iter_mft_records(vol, self._cancel):
            record_map[index] = raw
            scanned += 1
            if scanned % 1024 == 0 and self.on_progress:
                self.on_progress(
                    f"{self.letter}\\ [MFT {scanned:,}]",
                    self.files_seen,
                    self.errors_seen,
                )

        files: dict[int, MftFile] = {}
        for index, raw in record_map.items():
            if self._cancel.is_set():
                break
            parsed = _parse_file_record(raw, index, vol.file_record_size, record_map)
            if not parsed or not parsed.in_use:
                continue
            if parsed.data_incomplete:
                self.errors_seen += 1
            files[index] = parsed
            if parsed.is_dir:
                self.dirs_seen += 1
            else:
                self.files_seen += 1

        root = FileNode(name=self.letter + "\\", path=self.letter + "\\", is_dir=True)
        root._rel = ""
        root._raw_volume = vol  # type: ignore[attr-defined]
        root._raw_mode = True  # type: ignore[attr-defined]

        nodes: dict[int, FileNode] = {ROOT_MFT_REF: root}
        for ref, meta in files.items():
            if ref == ROOT_MFT_REF:
                continue
            if meta.name.startswith("$") and meta.parent_ref == ROOT_MFT_REF:
                continue
            path = f"{self.letter}\\raw\\{ref}"
            incomplete = meta.data_incomplete
            node = FileNode(
                name=meta.name,
                path=path,
                is_dir=meta.is_dir,
                size=meta.size,
                status=NodeStatus.PARTIAL if incomplete else NodeStatus.OK,
                error="Incomplete NTFS attribute list" if incomplete else "",
            )
            node._raw_volume = vol  # type: ignore[attr-defined]
            node._raw_runs = meta.runs  # type: ignore[attr-defined]
            node._raw_resident = meta.resident_data  # type: ignore[attr-defined]
            node._mft_ref = ref  # type: ignore[attr-defined]
            nodes[ref] = node

        lost = FileNode(name="_orphaned", path=f"{self.letter}\\_orphaned", is_dir=True, parent=root)
        lost._rel = "_orphaned"
        lost_used = False

        for ref, meta in files.items():
            if ref == ROOT_MFT_REF or ref not in nodes:
                continue
            node = nodes[ref]
            parent = nodes.get(meta.parent_ref)
            if parent is None or meta.parent_ref == ref:
                parent = lost
                lost_used = True
            node.parent = parent
            parent.children.append(node)

        def assign_rel(n: FileNode) -> None:
            if n is root:
                n._rel = ""
            elif n.parent is root:
                n._rel = n.name
            elif n.parent is not None:
                parent_rel = getattr(n.parent, "_rel", "")
                n._rel = os.path.join(parent_rel, n.name) if parent_rel else n.name
            for child in n.children:
                assign_rel(child)

        assign_rel(root)
        if lost_used:
            root.children.append(lost)
            assign_rel(lost)

        for n in nodes.values():
            n.children.sort(key=lambda c: (not c.is_dir, c.name.lower()))
        if lost_used:
            lost.children.sort(key=lambda c: (not c.is_dir, c.name.lower()))
        root.children.sort(key=lambda c: (not c.is_dir, c.name.lower()))

        if self.on_progress:
            self.on_progress(self.letter + "\\", self.files_seen, self.errors_seen)
        return root


def _read_cluster_sectors(vol: RawVolume, lcn: int, cancel: Optional[threading.Event]) -> tuple[bytes, int]:
    """Read one cluster sector-by-sector with retries. Returns (data, bad_sectors)."""
    import time

    out = bytearray()
    bad = 0
    base = lcn * vol.bytes_per_cluster
    for s in range(vol.sectors_per_cluster):
        if cancel and cancel.is_set():
            out += bytes(vol.bytes_per_sector * (vol.sectors_per_cluster - s))
            break
        off = base + s * vol.bytes_per_sector
        ok = False
        for attempt in range(8):
            try:
                out += vol.read_at(off, vol.bytes_per_sector, retries=1)
                ok = True
                break
            except OSError:
                time.sleep(0.015 * (attempt + 1))
        if not ok:
            out += bytes(vol.bytes_per_sector)
            bad += 1
    return bytes(out), bad


def _read_clusters_resilient(
    vol: RawVolume,
    lcn: int,
    count: int,
    want_bytes: int,
    cancel: Optional[threading.Event] = None,
) -> tuple[bytes, int]:
    """
    Read clusters with progressive fallback.
    Large multi-cluster reads fail often on dying disks — fall back to
    per-cluster then per-sector so we only zero truly unreadable sectors.
    """
    if lcn < 0:
        return bytes(want_bytes), 0

    # Fast path
    try:
        data = vol.read_clusters(lcn, count)[:want_bytes]
        return data, 0
    except OSError:
        pass

    out = bytearray()
    bad = 0
    for i in range(count):
        if cancel and cancel.is_set():
            break
        if len(out) >= want_bytes:
            break
        try:
            piece = vol.read_clusters(lcn + i, 1)
            out += piece
        except OSError:
            piece, sector_bad = _read_cluster_sectors(vol, lcn + i, cancel)
            out += piece
            bad += sector_bad

    if len(out) < want_bytes:
        out += bytes(want_bytes - len(out))
    return bytes(out[:want_bytes]), bad


def recover_raw_file(
    node: FileNode,
    destination: str,
    chunk_clusters: int = 4,
    cancel: Optional[threading.Event] = None,
    volume: Optional[RawVolume] = None,
) -> tuple[int, int, str]:
    """
    Recover a file from raw NTFS runs.
    Returns (bytes_written, bad_sectors, error).
    """
    vol: Optional[RawVolume] = volume or getattr(node, "_raw_volume", None)
    if vol is None:
        return 0, 0, "No raw volume attached"

    os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
    resident: bytes = getattr(node, "_raw_resident", b"") or b""
    runs: list[DataRun] = getattr(node, "_raw_runs", None) or []

    try:
        with open(destination, "wb") as out:
            if resident:
                out.write(resident)
                return len(resident), 0, ""

            written = 0
            bad = 0
            remaining = node.size if node.size > 0 else None
            for run in runs:
                if cancel and cancel.is_set():
                    return written, bad, "Cancelled"
                left = run.length
                lcn = run.lcn
                while left > 0:
                    if cancel and cancel.is_set():
                        return written, bad, "Cancelled"
                    take = min(left, chunk_clusters)
                    want = take * vol.bytes_per_cluster
                    if remaining is not None:
                        want = min(want, max(0, remaining))
                        if want == 0:
                            return written, bad, ""
                    if lcn < 0:
                        data, chunk_bad = bytes(want), 0
                    else:
                        data, chunk_bad = _read_clusters_resilient(
                            vol, lcn, take, want, cancel
                        )
                    bad += chunk_bad
                    out.write(data)
                    written += len(data)
                    if remaining is not None:
                        remaining -= len(data)
                    if lcn >= 0:
                        lcn += take
                    left -= take
            if node.size > 0 and written > node.size:
                out.truncate(node.size)
                written = node.size
            return written, bad, ""
    except OSError as exc:
        return 0, 0, str(exc)


def can_use_raw_volume(letter: str) -> bool:
    """True if \\\\.\\X: opens and looks like NTFS."""
    try:
        with RawVolume(letter) as vol:
            return vol.bytes_per_cluster > 0
    except OSError:
        return False
