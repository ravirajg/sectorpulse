"""Chunked recovery with retries and bad-sector padding."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .doc_repair import is_pdf_file, pdf_looks_valid, repair_pdf_document
from .recovery_journal import JournalEntry, RecoveryJournal
from .scanner import FileNode, NodeStatus
from .video_repair import is_video_file, repair_video_container, video_looks_valid


def _timeout_for_node(node: FileNode) -> float:
    """Seconds allowed for one file before treating it as hung."""
    size = max(0, int(getattr(node, "size", 0) or 0))
    estimate = size / (1.5 * 1024 * 1024) + 90
    return float(min(900, max(60, estimate)))


@dataclass
class RecoveryResult:
    source: str
    destination: str
    success: bool
    bytes_recovered: int = 0
    bytes_total: int = 0
    bad_sectors: int = 0
    retries: int = 0
    error: str = ""
    partial: bool = False


@dataclass
class RecoveryBatchResult:
    results: list[RecoveryResult] = field(default_factory=list)
    destination_root: str = ""

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.success and not r.partial)

    @property
    def partial_count(self) -> int:
        return sum(1 for r in self.results if r.success and r.partial)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if not r.success)

    @property
    def timeout_count(self) -> int:
        return sum(
            1
            for r in self.results
            if not r.success and "timed out" in (r.error or "").lower()
        )


class RecoveryEngine:
    """
    Copy files from a damaged volume using small chunk reads.
    On unrecoverable chunks, zero-pad and continue so the rest of the
    file can still be salvaged (classic bad-sector recovery behavior).
    """

    def __init__(
        self,
        chunk_size: int = 64 * 1024,
        max_retries: int = 4,
        retry_delay: float = 0.05,
        file_timeout_sec: Optional[float] = None,
    ):
        self.chunk_size = chunk_size
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.file_timeout_sec = file_timeout_sec  # None = size-based
        self._cancel = threading.Event()
        self.journal: Optional[RecoveryJournal] = None

    def cancel(self) -> None:
        self._cancel.set()

    def recover_nodes(
        self,
        nodes: list[FileNode],
        destination_root: str,
        source_root: str,
        on_progress: Optional[Callable[[str, int, int, RecoveryResult | None], None]] = None,
        force: bool = False,
    ) -> RecoveryBatchResult:
        self._cancel.clear()
        os.makedirs(destination_root, exist_ok=True)
        batch = RecoveryBatchResult(destination_root=destination_root)
        self.journal = RecoveryJournal(destination_root)

        files: list[FileNode] = []
        for node in nodes:
            if node.is_dir:
                files.extend(list(node.iter_files()))
            else:
                files.append(node)

        seen: set[str] = set()
        unique: list[FileNode] = []
        for f in files:
            key = os.path.normcase(f.path)
            if key not in seen:
                seen.add(key)
                unique.append(f)

        total = len(unique)
        for index, node in enumerate(unique, start=1):
            if self._cancel.is_set():
                break

            rel = getattr(node, "_rel", None) or os.path.relpath(node.path, source_root)
            dest = os.path.join(destination_root, rel)
            if on_progress:
                on_progress(dest, index, total, None)

            result = self._recover_one_with_timeout(node, dest, force=force)
            batch.results.append(result)
            self._journal_result(node, dest, result)
            if on_progress:
                on_progress(dest, index, total, result)

        return batch

    def _recover_one_with_timeout(
        self, node: FileNode, dest: str, *, force: bool = False
    ) -> RecoveryResult:
        timeout = self.file_timeout_sec or _timeout_for_node(node)
        box: dict[str, RecoveryResult] = {}
        file_cancel = threading.Event()

        def worker() -> None:
            class _Combined:
                def is_set(self_inner) -> bool:
                    return self._cancel.is_set() or file_cancel.is_set()

            combined = _Combined()
            try:
                if getattr(node, "_raw_volume", None) is not None:
                    box["r"] = self.recover_raw_node(
                        node, dest, cancel=combined, force=force  # type: ignore[arg-type]
                    )
                else:
                    box["r"] = self.recover_file(
                        node.path, dest, expected_size=node.size, force=force
                    )
            except Exception as exc:
                box["r"] = RecoveryResult(
                    source=getattr(node, "_rel", None) or node.path,
                    destination=dest,
                    success=False,
                    bytes_total=node.size,
                    error=f"exception: {exc}",
                )

        thread = threading.Thread(
            target=worker, daemon=True, name=f"Recover:{os.path.basename(dest)[:40]}"
        )
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            file_cancel.set()
            self._cancel.set()
            thread.join(5.0)
            self._cancel.clear()
            # Incomplete output from a hung read — remove so retry isn't skipped.
            try:
                if os.path.isfile(dest):
                    os.remove(dest)
            except OSError:
                pass
            return RecoveryResult(
                source=getattr(node, "_rel", None) or node.path,
                destination=dest,
                success=False,
                bytes_total=node.size,
                error=f"timed out after {int(timeout)}s — logged for retry",
            )
        return box.get(
            "r",
            RecoveryResult(
                source=getattr(node, "_rel", None) or node.path,
                destination=dest,
                success=False,
                error="no result",
            ),
        )

    def _journal_result(self, node: FileNode, dest: str, result: RecoveryResult) -> None:
        if not self.journal:
            return
        if result.success and (result.error or "").startswith("already exists"):
            status = "skipped"
        elif not result.success and "timed out" in (result.error or "").lower():
            status = "timeout"
        elif not result.success:
            status = "failed"
        elif result.partial:
            status = "partial"
        else:
            status = "ok"

        drive = ""
        letter = getattr(getattr(node, "_raw_volume", None), "letter", "") or ""
        if letter:
            drive = letter
        elif len(node.path) >= 2 and node.path[1] == ":":
            drive = node.path[:2]

        from datetime import datetime, timezone

        self.journal.record(
            JournalEntry(
                ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                status=status,
                source=result.source or getattr(node, "_rel", None) or node.path,
                destination=result.destination or dest,
                bytes_recovered=result.bytes_recovered,
                bytes_total=result.bytes_total or node.size,
                bad_sectors=result.bad_sectors,
                error=result.error or "",
                mft_ref=getattr(node, "_mft_ref", None),
                drive=drive,
                relative_path=getattr(node, "_rel", None) or "",
            )
        )

    @staticmethod
    def _already_restored(destination: str, expected_size: int = 0) -> Optional[RecoveryResult]:
        """Return a success result when the destination file is already present."""
        try:
            if not os.path.isfile(destination):
                return None
            existing = os.path.getsize(destination)
        except OSError:
            return None
        # Treat matching size (or any non-empty file when size unknown) as done.
        if expected_size > 0 and existing != expected_size:
            return None
        if expected_size <= 0 and existing <= 0:
            return None
        # Force re-restore of broken PDFs / videos left from earlier partial runs.
        if is_pdf_file(destination) and not pdf_looks_valid(destination):
            return None
        if is_video_file(destination) and not video_looks_valid(destination):
            return None
        return RecoveryResult(
            source=destination,
            destination=destination,
            success=True,
            bytes_recovered=existing,
            bytes_total=expected_size or existing,
            error="already exists",
        )

    @staticmethod
    def _annotate_repair(result: RecoveryResult, msg: str) -> None:
        if result.error == "already exists":
            result.error = f"already exists · {msg}"
        elif not result.error:
            result.error = msg
        else:
            result.error = f"{result.error} · {msg}"

    @staticmethod
    def _maybe_repair_video(destination: str, result: RecoveryResult) -> None:
        if not result.success or not is_video_file(destination):
            return
        changed, msg = repair_video_container(destination)
        if changed:
            try:
                result.bytes_recovered = os.path.getsize(destination)
            except OSError:
                pass
            RecoveryEngine._annotate_repair(result, msg)

    @staticmethod
    def _maybe_repair_pdf(destination: str, result: RecoveryResult, *, aggressive: bool = True) -> None:
        if not result.success or not is_pdf_file(destination):
            return
        # On resume skips, never re-enter expensive/fragile repair if PDF already looks OK.
        if not aggressive and pdf_looks_valid(destination):
            return
        changed, msg = repair_pdf_document(destination, aggressive=aggressive)
        if changed:
            try:
                result.bytes_recovered = os.path.getsize(destination)
            except OSError:
                pass
            if pdf_looks_valid(destination) and "rebuilt PDF" in msg:
                result.partial = False
            RecoveryEngine._annotate_repair(result, msg)
        elif msg and "timed out" in msg:
            RecoveryEngine._annotate_repair(result, msg)

    def recover_raw_node(
        self,
        node: FileNode,
        destination: str,
        cancel: Optional[threading.Event] = None,
        force: bool = False,
    ) -> RecoveryResult:
        from .ntfs_raw import RawVolume, recover_raw_file

        cancel_flag = cancel or self._cancel
        if not force:
            skipped = self._already_restored(destination, node.size)
            if skipped is not None:
                skipped.source = getattr(node, "_rel", None) or node.path
                if is_video_file(destination) and not video_looks_valid(destination):
                    self._maybe_repair_video(destination, skipped)
                if is_pdf_file(destination) and not pdf_looks_valid(destination):
                    self._maybe_repair_pdf(destination, skipped, aggressive=True)
                return skipped

        result = RecoveryResult(
            source=getattr(node, "_rel", None) or node.path,
            destination=destination,
            success=False,
            bytes_total=node.size,
        )

        # Dedicated volume handle so a hung read doesn't block the next file.
        shared = getattr(node, "_raw_volume", None)
        own: Optional[RawVolume] = None
        written, bad, err = 0, 0, ""
        try:
            vol = None
            if shared is not None:
                own = RawVolume(shared.letter)
                own.open()
                vol = own
            written, bad, err = recover_raw_file(
                node, destination, cancel=cancel_flag, volume=vol
            )
        except Exception as exc:
            err = str(exc)
        finally:
            if own is not None:
                try:
                    own.close()
                except Exception:
                    pass

        result.bytes_recovered = written
        result.bad_sectors = bad
        if err and not written:
            result.error = err
            return result
        result.success = written > 0 or node.size == 0
        result.partial = bad > 0 or (node.size > 0 and written < node.size)
        if err and written:
            result.error = err
            result.partial = True
        if result.success:
            self._maybe_repair_video(destination, result)
            self._maybe_repair_pdf(
                destination,
                result,
                aggressive=bool(result.partial or not pdf_looks_valid(destination)),
            )
        return result

    def recover_file(
        self,
        source: str,
        destination: str,
        expected_size: int = 0,
        force: bool = False,
    ) -> RecoveryResult:
        result = RecoveryResult(
            source=source,
            destination=destination,
            success=False,
            bytes_total=expected_size,
        )

        if not force:
            skipped = self._already_restored(destination, expected_size)
            if skipped is not None:
                skipped.source = source
                if is_video_file(destination) and not video_looks_valid(destination):
                    self._maybe_repair_video(destination, skipped)
                if is_pdf_file(destination) and not pdf_looks_valid(destination):
                    self._maybe_repair_pdf(destination, skipped, aggressive=True)
                return skipped

        try:
            os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
        except OSError as exc:
            result.error = f"Cannot create destination folder: {exc}"
            return result

        # Unreadable metadata — still attempt recovery
        try:
            if expected_size <= 0:
                try:
                    expected_size = os.path.getsize(source)
                    result.bytes_total = expected_size
                except OSError:
                    pass
        except Exception:
            pass

        out_fh = None
        try:
            # Use buffered binary open; on Windows this goes through the FS.
            # Chunk retries + zero-fill mimic sector-skip recovery tools.
            # Probe open once — hard fail if the file handle cannot be obtained
            try:
                with open(source, "rb") as probe:
                    probe.read(1)
                    probe.seek(0)
            except OSError as exc:
                result.error = f"Cannot open source: {exc}"
                return result

            out_fh = open(destination, "wb")
            offset = 0
            eof = False
            consecutive_fails = 0

            while not eof:
                if self._cancel.is_set():
                    result.error = "Cancelled"
                    result.partial = result.bytes_recovered > 0
                    result.success = result.bytes_recovered > 0
                    return result

                data, status = self._read_chunk(source, offset, self.chunk_size)
                if status == "eof":
                    eof = True
                    break
                if status == "fail":
                    consecutive_fails += 1
                    # Without a known size, abort after sustained failures
                    if not expected_size and consecutive_fails >= 3:
                        result.error = "Unreadable (repeated I/O errors)"
                        result.success = result.bytes_recovered > 0
                        result.partial = result.bytes_recovered > 0
                        return result
                    # Pad a chunk-sized hole and skip ahead (bad-sector skip)
                    pad_len = self.chunk_size
                    if expected_size:
                        remaining = max(0, expected_size - offset)
                        pad_len = min(self.chunk_size, remaining) or self.chunk_size
                    out_fh.write(bytes(pad_len))
                    result.bad_sectors += 1
                    result.partial = True
                    offset += pad_len
                    if expected_size and offset >= expected_size:
                        eof = True
                    continue

                consecutive_fails = 0
                if not data:
                    eof = True
                    break

                out_fh.write(data)
                result.bytes_recovered += len(data)
                offset += len(data)
                if len(data) < self.chunk_size:
                    eof = True
                if expected_size and offset >= expected_size:
                    eof = True

            out_fh.flush()
            out_fh.close()
            out_fh = None
            result.success = True
            if result.bad_sectors:
                result.partial = True
            if expected_size and result.bytes_recovered < expected_size and result.bad_sectors == 0:
                # Shorter than expected without explicit pads — mark partial
                if result.bytes_recovered > 0:
                    result.partial = True
            self._maybe_repair_video(destination, result)
            self._maybe_repair_pdf(
                destination,
                result,
                aggressive=bool(result.partial or not pdf_looks_valid(destination)),
            )
            return result

        except OSError as exc:
            result.error = str(exc)
            result.success = result.bytes_recovered > 0
            result.partial = result.bytes_recovered > 0
            if result.success:
                self._maybe_repair_video(destination, result)
                self._maybe_repair_pdf(
                    destination,
                    result,
                    aggressive=bool(result.partial or not pdf_looks_valid(destination)),
                )
            return result
        finally:
            if out_fh is not None:
                try:
                    out_fh.close()
                except OSError:
                    pass

    def _read_chunk(self, path: str, offset: int, size: int) -> tuple[bytes, str]:
        """
        Returns (data, status) where status is 'ok', 'eof', or 'fail'.
        """
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                with open(path, "rb") as fh:
                    fh.seek(offset)
                    data = fh.read(size)
                if not data:
                    return b"", "eof"
                return data, "ok"
            except OSError as exc:
                last_err = exc
                result = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
                # Retry transient / media errors
                time.sleep(self.retry_delay * (attempt + 1))
                continue

        # If seek past end, treat as eof
        if last_err and "Invalid argument" in str(last_err):
            return b"", "eof"
        return b"", "fail"


def collect_selected(root: FileNode) -> list[FileNode]:
    """Return selected nodes (files or whole folders)."""
    selected: list[FileNode] = []

    def walk(node: FileNode) -> None:
        if node.selected:
            selected.append(node)
            return  # whole folder selected — children implied
        if node.is_dir:
            for child in node.children:
                walk(child)

    walk(root)
    return selected
