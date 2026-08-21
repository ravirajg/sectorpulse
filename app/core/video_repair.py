"""Rebuild playable video containers after recovery (fixes VLC missing-index dialogs)."""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

VIDEO_EXTENSIONS = {
    ".avi",
    ".mp4",
    ".m4v",
    ".mov",
    ".mkv",
    ".wmv",
    ".mpg",
    ".mpeg",
    ".flv",
    ".webm",
    ".ts",
    ".m2ts",
    ".3gp",
}


def is_video_file(path: str) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def video_looks_valid(path: str) -> bool:
    """
    Cheap check that a restored video isn't an empty/zero-padded shell.
    Avoids full decode so skip-checks stay fast during large restores.
    """
    try:
        size = os.path.getsize(path)
        if size < 64:
            return False
        with open(path, "rb") as fh:
            head = fh.read(64)
            # Sample near the end — all-zero tails are common after bad-sector padding.
            fh.seek(max(0, size - 64))
            tail = fh.read(64)
    except OSError:
        return False

    if head.count(0) >= 60 or tail.count(0) >= 60:
        return False

    ext = Path(path).suffix.lower()
    if ext == ".avi":
        return head[0:4] == b"RIFF" and head[8:12] == b"AVI "
    if ext in {".mp4", ".m4v", ".mov"}:
        return b"ftyp" in head
    if ext in {".mkv", ".webm"}:
        return head.startswith(b"\x1a\x45\xdf\xa3")
    if ext == ".wmv":
        return head.startswith(b"\x30\x26\xb2\x75")
    if ext == ".flv":
        return head.startswith(b"FLV")
    if ext in {".mpg", ".mpeg", ".ts", ".m2ts"}:
        return head.startswith(b"\x00\x00\x01") or b"ftyp" in head
    # Unknown / generic video extension — accept if not empty/zeroed.
    return True


def _find_ffmpeg() -> Optional[str]:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _remux_with_ffmpeg(path: str, ffmpeg: str) -> tuple[bool, str]:
    ext = Path(path).suffix.lower() or ".mp4"
    fd, tmp = tempfile.mkstemp(prefix="sp_fix_", suffix=ext, dir=os.path.dirname(path) or None)
    os.close(fd)
    try:
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-err_detect",
            "ignore_err",
            "-i",
            path,
            "-map",
            "0",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
        ]
        # MP4/MOV: place moov up front so players don't need a full index rebuild prompt.
        if ext in {".mp4", ".m4v", ".mov"}:
            cmd.extend(["-movflags", "+faststart"])
        cmd.append(tmp)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60 * 30)
        if proc.returncode != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) < 64:
            err = (proc.stderr or proc.stdout or "ffmpeg remux failed").strip()
            return False, err[:300]
        os.replace(tmp, path)
        return True, "rebuilt container index"
    except Exception as exc:
        return False, str(exc)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _rebuild_avi_index(path: str) -> tuple[bool, str]:
    """
    Pure-Python AVI idx1 rebuild.
    Scans the movi list and rewrites/appends an idx1 chunk so VLC can seek
    without the 'Broken or missing Index' prompt.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return False, str(exc)

    if len(data) < 16 or data[0:4] != b"RIFF" or data[8:12] != b"AVI ":
        return False, "not an AVI file"

    # Find LIST movi
    movi = data.find(b"LIST")
    movi_off = -1
    while movi != -1 and movi + 12 <= len(data):
        size = struct.unpack_from("<I", data, movi + 4)[0]
        if data[movi + 8 : movi + 12] == b"movi":
            movi_off = movi
            movi_size = size
            break
        movi = data.find(b"LIST", movi + 4)
    if movi_off < 0:
        return False, "AVI movi list not found"

    movi_data_start = movi_off + 12
    movi_data_end = movi_off + 8 + movi_size
    movi_data_end = min(movi_data_end, len(data))

    entries = bytearray()
    pos = movi_data_start
    while pos + 8 <= movi_data_end:
        chunk_id = data[pos : pos + 4]
        chunk_size = struct.unpack_from("<I", data, pos + 4)[0]
        if chunk_size < 0 or pos + 8 + chunk_size > len(data):
            break
        # Skip nested LIST junk inside movi
        if chunk_id == b"LIST":
            pos += 8 + chunk_size + (chunk_size & 1)
            continue
        # Typical stream chunks: ##dc ##db ##wb ##tx ##pc
        if len(chunk_id) == 4 and chunk_id[2:4] in (b"dc", b"db", b"wb", b"tx", b"pc"):
            flags = 0x10  # KEYFRAME — safe default for seeking
            # Offset relative to the start of the 'movi' fourcc (standard idx1).
            offset = pos - (movi_off + 8)
            entries += chunk_id
            entries += struct.pack("<III", flags, offset, chunk_size)
        pos += 8 + chunk_size + (chunk_size & 1)
        if len(entries) > 64 * 1024 * 1024:
            break

    if not entries:
        return False, "no AVI chunks found to index"

    # Keep everything before an existing idx1 (or end of movi list).
    idx1_at = data.find(b"idx1", movi_data_end - 4 if movi_data_end >= 4 else movi_off)
    if idx1_at < 0 or idx1_at < movi_off:
        # Prefer truncating after movi list.
        base = data[:movi_data_end]
        # Word-align
        if len(base) & 1:
            base += b"\x00"
    else:
        base = data[:idx1_at]

    idx1_chunk = b"idx1" + struct.pack("<I", len(entries)) + entries
    if len(idx1_chunk) & 1:
        idx1_chunk += b"\x00"

    out = bytearray(base)
    # Ensure we don't leave garbage after movi if base ended early
    out += idx1_chunk
    # Patch RIFF size
    riff_size = len(out) - 8
    struct.pack_into("<I", out, 4, riff_size)

    fd, tmp = tempfile.mkstemp(prefix="sp_avi_", suffix=".avi", dir=os.path.dirname(path) or None)
    os.close(fd)
    try:
        with open(tmp, "wb") as fh:
            fh.write(out)
        os.replace(tmp, path)
        return True, f"rebuilt AVI index ({len(entries) // 16} entries)"
    except OSError as exc:
        return False, str(exc)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def repair_video_container(path: str) -> tuple[bool, str]:
    """
    Repair a recovered video so players don't prompt about a broken index.
    Returns (changed, message).
    """
    if not path or not os.path.isfile(path) or not is_video_file(path):
        return False, "not a video"

    ffmpeg = _find_ffmpeg()
    if ffmpeg:
        ok, msg = _remux_with_ffmpeg(path, ffmpeg)
        if ok:
            return True, msg

    if Path(path).suffix.lower() == ".avi":
        ok, msg = _rebuild_avi_index(path)
        if ok:
            return True, msg
        return False, msg

    if not ffmpeg:
        return False, "ffmpeg not available for container repair"
    return False, msg if ffmpeg else "repair failed"
