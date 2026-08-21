"""Resilient directory tree scan that survives I/O / bad-sector errors."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class NodeStatus(str, Enum):
    OK = "ok"
    UNREADABLE = "unreadable"
    PARTIAL = "partial"
    SKIPPED = "skipped"


@dataclass
class FileNode:
    name: str
    path: str
    is_dir: bool
    size: int = 0
    status: NodeStatus = NodeStatus.OK
    error: str = ""
    children: list["FileNode"] = field(default_factory=list)
    parent: Optional["FileNode"] = field(default=None, repr=False)
    selected: bool = False

    @property
    def relative_path(self) -> str:
        # Built during scan relative to drive root
        return getattr(self, "_rel", self.name)

    def iter_files(self):
        if not self.is_dir:
            yield self
            return
        for child in self.children:
            yield from child.iter_files()

    def count_stats(self) -> tuple[int, int, int]:
        """Return (files, dirs, bad_or_partial)."""
        files = dirs = bad = 0
        if self.is_dir:
            dirs = 1
            for child in self.children:
                f, d, b = child.count_stats()
                files += f
                dirs += d
                bad += b
        else:
            files = 1
            if self.status in (NodeStatus.UNREADABLE, NodeStatus.PARTIAL):
                bad = 1
        return files, dirs, bad


class DriveScanner:
    """Walk a drive root, capturing folders/files even when some reads fail."""

    def __init__(
        self,
        root_path: str,
        on_progress: Optional[Callable[[str, int, int], None]] = None,
        on_node: Optional[Callable[[FileNode], None]] = None,
    ):
        self.root_path = root_path
        self.on_progress = on_progress
        self.on_node = on_node
        self._cancel = threading.Event()
        self.files_seen = 0
        self.dirs_seen = 0
        self.errors_seen = 0

    def cancel(self) -> None:
        self._cancel.set()

    def scan(self) -> FileNode:
        self._cancel.clear()
        root_name = os.path.basename(self.root_path.rstrip("\\/")) or self.root_path
        root = FileNode(name=root_name, path=self.root_path, is_dir=True)
        root._rel = ""

        # If the Win32 path is unreadable (common on crashed NTFS volumes),
        # fall back to raw \\\\.\\X: MFT parsing.
        try:
            with os.scandir(self.root_path) as it:
                next(it, None)
        except OSError:
            letter = self.root_path.rstrip("\\/")
            if len(letter) >= 2 and letter[1] == ":":
                return self._scan_raw_ntfs(letter[:2])
            raise

        self._walk(root)
        return root

    def _scan_raw_ntfs(self, letter: str) -> FileNode:
        from .ntfs_raw import NtfsRawScanner

        raw = NtfsRawScanner(letter, on_progress=self.on_progress)
        # Share cancel flag
        raw._cancel = self._cancel
        root = raw.scan()
        self.files_seen = raw.files_seen
        self.dirs_seen = raw.dirs_seen
        self.errors_seen = raw.errors_seen
        return root

    def _walk(self, node: FileNode) -> None:
        if self._cancel.is_set():
            return

        try:
            entries = list(os.scandir(node.path))
        except OSError as exc:
            node.status = NodeStatus.UNREADABLE
            node.error = str(exc)
            self.errors_seen += 1
            if self.on_progress:
                self.on_progress(node.path, self.files_seen, self.errors_seen)
            return

        # Stable ordering: folders first, then name
        try:
            entries.sort(key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        except OSError:
            pass

        for entry in entries:
            if self._cancel.is_set():
                return

            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError as exc:
                child = FileNode(
                    name=entry.name,
                    path=entry.path,
                    is_dir=False,
                    status=NodeStatus.UNREADABLE,
                    error=str(exc),
                    parent=node,
                )
                child._rel = os.path.join(node._rel, entry.name) if node._rel else entry.name
                node.children.append(child)
                self.errors_seen += 1
                continue

            child = FileNode(
                name=entry.name,
                path=entry.path,
                is_dir=is_dir,
                parent=node,
            )
            child._rel = os.path.join(node._rel, entry.name) if node._rel else entry.name

            if is_dir:
                self.dirs_seen += 1
                node.children.append(child)
                if self.on_node:
                    self.on_node(child)
                if self.on_progress:
                    self.on_progress(child.path, self.files_seen, self.errors_seen)
                self._walk(child)
            else:
                size = 0
                status = NodeStatus.OK
                err = ""
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError as exc:
                    status = NodeStatus.UNREADABLE
                    err = str(exc)
                    self.errors_seen += 1

                # Probe first bytes — detects bad sectors near start of file
                if status == NodeStatus.OK and size > 0:
                    probe = self._probe_readable(entry.path)
                    if probe is False:
                        status = NodeStatus.UNREADABLE
                        err = "Unreadable (bad sector / I/O error)"
                        self.errors_seen += 1
                    elif probe is None:
                        status = NodeStatus.PARTIAL
                        err = "Partial read risk detected"
                        self.errors_seen += 1

                child.size = size
                child.status = status
                child.error = err
                node.children.append(child)
                self.files_seen += 1
                if self.on_node:
                    self.on_node(child)
                if self.on_progress:
                    self.on_progress(child.path, self.files_seen, self.errors_seen)

        if any(c.status != NodeStatus.OK for c in node.children):
            if node.status == NodeStatus.OK:
                node.status = NodeStatus.PARTIAL

    @staticmethod
    def _probe_readable(path: str) -> Optional[bool]:
        """
        True = readable, False = hard fail, None = uncertain/partial.
        Reads a small header with retries.
        """
        retries = 3
        for attempt in range(retries):
            try:
                with open(path, "rb") as fh:
                    fh.read(4096)
                return True
            except PermissionError:
                # May still be restorable with elevated rights / different API
                return None
            except OSError:
                if attempt + 1 < retries:
                    continue
                return False
        return False
