"""Persistent recovery journals for resume / hang tracking / retry."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Optional


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class JournalEntry:
    ts: str
    status: str  # ok | partial | skipped | failed | timeout
    source: str
    destination: str
    bytes_recovered: int = 0
    bytes_total: int = 0
    bad_sectors: int = 0
    error: str = ""
    mft_ref: Optional[int] = None
    drive: str = ""
    relative_path: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class RecoveryJournal:
    """
    Append-only logs under <destination>/SectorPulse_logs/

    - recovery_all.jsonl      every file attempt
    - recovery_failed.jsonl   failed / timeout / (optional) partial
    - recovery_failed.txt     human-readable failed paths for quick review
    - recovery_processed.txt  human-readable successful/skipped paths
    """

    def __init__(self, destination_root: str):
        self.destination_root = destination_root
        self.log_dir = os.path.join(destination_root, "SectorPulse_logs")
        os.makedirs(self.log_dir, exist_ok=True)
        self.all_path = os.path.join(self.log_dir, "recovery_all.jsonl")
        self.failed_jsonl = os.path.join(self.log_dir, "recovery_failed.jsonl")
        self.failed_txt = os.path.join(self.log_dir, "recovery_failed.txt")
        self.processed_txt = os.path.join(self.log_dir, "recovery_processed.txt")
        self._lock = threading.Lock()
        self._write_header_once()

    def _write_header_once(self) -> None:
        marker = os.path.join(self.log_dir, ".session")
        if os.path.isfile(marker):
            return
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(_utc_now() + "\n")
        with open(self.failed_txt, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== SectorPulse failed / timed-out files ({_utc_now()}) =====\n")
        with open(self.processed_txt, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== SectorPulse processed files ({_utc_now()}) =====\n")

    def record(self, entry: JournalEntry) -> None:
        line = entry.to_json() + "\n"
        with self._lock:
            with open(self.all_path, "a", encoding="utf-8") as fh:
                fh.write(line)

            if entry.status in {"failed", "timeout"}:
                with open(self.failed_jsonl, "a", encoding="utf-8") as fh:
                    fh.write(line)
                with open(self.failed_txt, "a", encoding="utf-8") as fh:
                    fh.write(
                        f"{entry.status.upper():8}  {entry.destination}"
                        f"  | src={entry.source}"
                        f"  | err={entry.error or '-'}\n"
                    )
            elif entry.status == "partial":
                # Track partials in failed list so they can be retried later.
                with open(self.failed_jsonl, "a", encoding="utf-8") as fh:
                    fh.write(line)
                with open(self.failed_txt, "a", encoding="utf-8") as fh:
                    fh.write(
                        f"PARTIAL   {entry.destination}"
                        f"  | bad_sectors={entry.bad_sectors}"
                        f"  | err={entry.error or '-'}\n"
                    )
                with open(self.processed_txt, "a", encoding="utf-8") as fh:
                    fh.write(f"PARTIAL  {entry.destination}\n")
            else:
                with open(self.processed_txt, "a", encoding="utf-8") as fh:
                    tag = "SKIP" if entry.status == "skipped" else "OK"
                    fh.write(f"{tag:8}  {entry.destination}\n")

    @staticmethod
    def load_failed_entries(destination_root: str) -> list[JournalEntry]:
        log_dir = os.path.join(destination_root, "SectorPulse_logs")
        path = os.path.join(log_dir, "recovery_failed.jsonl")
        if not os.path.isfile(path):
            return []
        # Keep latest entry per destination path
        latest: dict[str, JournalEntry] = {}
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    entry = JournalEntry(**data)
                except Exception:
                    continue
                key = os.path.normcase(entry.destination)
                latest[key] = entry
        # Only retry items still marked failed/timeout/partial (not later OK)
        return list(latest.values())

    @staticmethod
    def destinations_later_ok(destination_root: str) -> set[str]:
        """Destinations that later succeeded (for pruning retry list)."""
        log_dir = os.path.join(destination_root, "SectorPulse_logs")
        path = os.path.join(log_dir, "recovery_all.jsonl")
        ok: set[str] = set()
        if not os.path.isfile(path):
            return ok
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                if data.get("status") in {"ok", "skipped"}:
                    ok.add(os.path.normcase(data.get("destination") or ""))
        return ok


def iter_retry_entries(destination_root: str) -> Iterator[JournalEntry]:
    """Yield failed entries that have not been successfully recovered since."""
    later_ok = RecoveryJournal.destinations_later_ok(destination_root)
    for entry in RecoveryJournal.load_failed_entries(destination_root):
        key = os.path.normcase(entry.destination)
        if key and key not in later_ok:
            yield entry
