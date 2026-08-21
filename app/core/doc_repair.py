"""Salvage / repair recovered PDF documents."""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import tempfile
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path

# pypdf is noisy on corrupt files ("Object N not defined", object streams, …)
logging.getLogger("pypdf").setLevel(logging.CRITICAL)
logging.getLogger("pypdf._reader").setLevel(logging.CRITICAL)
logging.getLogger("pypdf.generic").setLevel(logging.CRITICAL)


def is_pdf_file(path: str) -> bool:
    return Path(path).suffix.lower() == ".pdf"


def pdf_looks_valid(path: str) -> bool:
    """True when the file has a PDF header and an %%EOF marker."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
            if not head.startswith(b"%PDF"):
                return False
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size < 32:
                return False
            fh.seek(max(0, size - 4096))
            tail = fh.read()
        return b"%%EOF" in tail
    except OSError:
        return False


def _carve_pdf_window(data: bytes) -> bytes | None:
    start = data.find(b"%PDF")
    if start < 0:
        return None
    end = data.rfind(b"%%EOF")
    if end < start:
        return None
    end += len(b"%%EOF")
    while end < len(data) and data[end] in b"\r\n \t":
        end += 1
    carved = data[start:end]
    return carved if len(carved) >= 32 else None


def _pypdf_rewrite_job(src: str, dst: str) -> tuple[bool, str]:
    """Worker run in a child process so hangs can be killed via timeout."""
    logging.getLogger("pypdf").setLevel(logging.CRITICAL)
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        return False, "pypdf not installed"

    try:
        reader = PdfReader(src, strict=False)
        # Avoid full object-stream reconstruction loops when possible.
        try:
            n_pages = len(reader.pages)
        except Exception as exc:
            return False, f"page list failed: {exc}"[:180]
        if n_pages <= 0:
            return False, "no pages"

        writer = PdfWriter()
        copied = 0
        for i in range(n_pages):
            try:
                writer.add_page(reader.pages[i])
                copied += 1
            except Exception:
                # Skip unreadable pages; keep going.
                continue
        if copied <= 0:
            return False, "no readable pages"

        with open(dst, "wb") as out:
            writer.write(out)
        if os.path.getsize(dst) < 32:
            return False, "rewrite too small"
        return True, f"rebuilt PDF ({copied}/{n_pages} pages)"
    except Exception as exc:
        # PdfReadError and friends — never hang the parent on corrupt streams.
        return False, f"{type(exc).__name__}: {exc}"[:180]


def _rewrite_with_pypdf(path: str, timeout_sec: float = 12.0) -> tuple[bool, str]:
    """Rewrite via pypdf in a separate process; abandon if it hangs."""
    fd, tmp = tempfile.mkstemp(
        prefix="sp_pdf_", suffix=".pdf", dir=os.path.dirname(path) or None
    )
    os.close(fd)
    try:
        ctx = mp.get_context("spawn")
        with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as pool:
            fut = pool.submit(_pypdf_rewrite_job, path, tmp)
            try:
                ok, msg = fut.result(timeout=timeout_sec)
            except FuturesTimeout:
                fut.cancel()
                return False, "pdf repair timed out"
            except Exception as exc:
                return False, str(exc)[:180]
        if not ok:
            return False, msg
        os.replace(tmp, path)
        return True, msg
    except Exception as exc:
        return False, str(exc)[:180]
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _carve_and_replace(path: str) -> tuple[bool, str]:
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return False, str(exc)
    carved = _carve_pdf_window(data)
    if not carved:
        return False, "no PDF header/EOF found"
    if carved == data:
        return False, "already trimmed"
    fd, tmp = tempfile.mkstemp(
        prefix="sp_pdf_", suffix=".pdf", dir=os.path.dirname(path) or None
    )
    os.close(fd)
    try:
        with open(tmp, "wb") as out:
            out.write(carved)
        os.replace(tmp, path)
        return True, "trimmed to %PDF…%%EOF"
    except OSError as exc:
        return False, str(exc)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def repair_pdf_document(path: str, *, aggressive: bool = True) -> tuple[bool, str]:
    """
    Best-effort PDF salvage after raw recovery.
    Returns (changed, message).

    Never blocks indefinitely — pypdf runs with a hard timeout.
    If the file already has %PDF + %%EOF and aggressive is False, skip work.
    """
    if not path or not os.path.isfile(path) or not is_pdf_file(path):
        return False, "not a pdf"

    if pdf_looks_valid(path) and not aggressive:
        return False, "pdf already readable"

    # Carve first — cheap and avoids hanging inside corrupt object streams.
    carved_ok, carved_msg = _carve_and_replace(path)

    # Then try a timed structural rebuild (may still fail on heavy corruption).
    ok, msg = _rewrite_with_pypdf(path, timeout_sec=12.0)
    if ok:
        return True, msg

    if carved_ok:
        return True, carved_msg

    if pdf_looks_valid(path):
        return False, "pdf kept as-is (repair skipped)"
    return False, msg if msg else carved_msg
