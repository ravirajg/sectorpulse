<div align="center">

<h1>
  <img src="docs/images/icon.svg" alt="" height="52" />
  SectorPulse
</h1>

<p><em>Resilient file recovery from damaged Windows drives</em></p>




</div>

[![Windows](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D4.svg?logo=windows&logoColor=white)](#requirements)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB.svg?logo=python&logoColor=white)](#quick-start)
[![UI](https://img.shields.io/badge/UI-CustomTkinter-2EE6A6.svg)](#features)
[![NTFS](https://img.shields.io/badge/raw%20NTFS-%5C%5C.%5CX%3A-F0B429.svg)](#how-it-works)
[![Repair](https://img.shields.io/badge/repair-PDF%20%2B%20video-FF5C7A.svg)](#features)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

<br>

## <img src="docs/images/icon_why.svg" height="28" alt="" /> Why this exists

Recently one of my hard drives crashed — the one with years of family photos, PDFs, and videos. I bought professional recovery software to fix it. It *did* restore a ton of files… but I couldn’t open any of them. Photos wouldn’t load, PDFs were broken, videos just sat there useless.

So I built **SectorPulse**. It doesn’t stop when a sector is bad — it skips the hole, keeps the rest of the file, and then tries to repair recovered PDFs and videos so they actually open again. I got my files back. I’m sharing this in case someone else hits the same wall.

<br>

**What it does in practice:** plug the dying drive in over USB. SectorPulse detects the volume (even when Windows size queries fail), maps folders via Win32 or raw NTFS, copies selected files in small chunks with retries and zero-padding past bad sectors, then best-effort repairs PDFs/videos and journals every attempt so you can resume later.

<img src="docs/images/divider.svg" width="100%" alt="">

## <img src="docs/images/icon_toc.svg" height="22" alt="" /> Table of Contents

- [<img src="docs/images/icon_why.svg" height="16" alt="" /> Why this exists](#why-this-exists)
- [<img src="docs/images/icon_how.svg" height="16" alt="" /> How It Works](#how-it-works)
- [<img src="docs/images/icon_features.svg" height="16" alt="" /> Features](#features)
- [<img src="docs/images/icon_can.svg" height="16" alt="" /> What It Can Do](#what-it-can-do)
- [<img src="docs/images/icon_cannot.svg" height="16" alt="" /> What It Cannot Do](#what-it-cannot-do)
- [<img src="docs/images/icon_req.svg" height="16" alt="" /> Requirements](#requirements)
- [<img src="docs/images/icon_start.svg" height="16" alt="" /> Quick Start](#quick-start)
- [<img src="docs/images/icon_usage.svg" height="16" alt="" /> Usage Walkthrough](#usage-walkthrough)
- [<img src="docs/images/icon_eng.svg" height="16" alt="" /> Engineering Notes](#engineering-notes)
- [<img src="docs/images/icon_structure.svg" height="16" alt="" /> Project Structure](#project-structure)
- [<img src="docs/images/icon_config.svg" height="16" alt="" /> Configuration Knobs](#configuration-knobs)
- [<img src="docs/images/icon_warn.svg" height="16" alt="" /> Limitations & Responsible Use](#limitations--responsible-use)
- [<img src="docs/images/icon_heart.svg" height="16" alt="" /> Contributing](#contributing)
- [<img src="docs/images/icon_license.svg" height="16" alt="" /> License](#license)

<br>

## <img src="docs/images/icon_how.svg" height="28" alt="" /> How It Works

<p>
  <img src="docs/images/icon_architecture.svg" height="22" alt="" />
  &nbsp;<strong>Architecture — detect · scan · restore · repair + journal (Win32 + raw NTFS)</strong>
</p>

<img src="docs/images/architecture.svg" alt="" width="100%">

SectorPulse is a four-stage pipeline with a dual scan/restore path:

1. **Detect.** [`DriveMonitor`](app/core/drive_monitor.py) watches for newly attached letters via `GetLogicalDrives` + `psutil`, ignores the system drive, and still lists volumes that fail `disk_usage` (classic `WinError 1392` damaged disks).

2. **Scan.** [`DriveScanner`](app/core/scanner.py) walks the tree with error tolerance. If `scandir` already fails, it falls back to [`NtfsRawScanner`](app/core/ntfs_raw.py) — opens `\\.\X:`, parses the boot sector and `$MFT`, and rebuilds the folder map from MFT records (including orphans under `_orphaned`).

3. **Restore.** [`RecoveryEngine`](app/core/recovery.py) copies selected files in **64 KiB chunks** with retries. Unreadable chunks are **zero-padded** so the rest of the file is still salvaged. Each file runs on a worker thread with a **size-based timeout** so one hung read cannot stall the batch.

4. **Repair + journal.** Recovered PDFs and videos get a best-effort fix. Every attempt is appended under `<destination>/SectorPulse_logs/` so you can **Retry Failed from Log…** later.

<img src="docs/images/divider.svg" width="100%" alt="">

<br>

## <img src="docs/images/icon_features.svg" height="28" alt="" /> Features

### <img src="docs/images/icon_usb.svg" height="22" alt="" /> Hot-plug drive detection
Lists Removable and Fixed USB volumes as they appear. Damaged volumes that throw on size queries still show up as *unreadable* so you can scan them in raw mode. Network, optical, and RAM drives are filtered out.

### <img src="docs/images/icon_map.svg" height="22" alt="" /> Error-tolerant file map
Continues after `scandir` / `stat` / probe failures. Each node is tagged `ok` · `partial` · `unreadable`. Lazy tree expand keeps large maps snappy. Cancel mid-scan anytime.

<img src="docs/images/feature_status.svg" alt="" width="100%">

### <img src="docs/images/icon_dual.svg" height="22" alt="" /> Dual recovery paths
| Mode | When | How |
|------|------|-----|
| **Win32 path** | Volume still lists folders | Chunked open/seek/read + zero-fill |
| **Raw NTFS** | Path API already dead | `\\.\X:` boot + MFT + data runs + sector fallback |

### <img src="docs/images/icon_badsector.svg" height="22" alt="" /> Bad-sector skip (classic salvage behavior)
On repeated I/O errors, SectorPulse pads a hole and keeps going instead of aborting the file. Partials are marked amber — still often useful for photos, docs, and many media files.

### <img src="docs/images/icon_hang.svg" height="22" alt="" /> Hang protection
Per-file timeouts (roughly 60–900s by size). Timed-out destinations are removed and logged as `timeout` so a later retry starts clean.

### <img src="docs/images/icon_repair.svg" height="22" alt="" /> PDF & video repair

<p>
  <img src="docs/images/icon_repair.svg" height="20" alt="" />
  &nbsp;<strong>PDF carve + pypdf rewrite · video ffmpeg remux / AVI idx1 rebuild</strong>
</p>

<img src="docs/images/feature_repair.svg" alt="" width="100%">

- **PDF** — carve `%PDF`…`%%EOF`, then timed `pypdf` rewrite in a child process (skip unreadable pages; hard timeout ~12s).
- **Video** — prefer `ffmpeg` remux (`-c copy`, `+faststart` for MP4/MOV); pure-Python **AVI `idx1` rebuild** if ffmpeg is unavailable. Covers `.mp4` `.m4v` `.mov` `.mkv` `.avi` `.wmv` `.webm` `.mpg` `.mpeg` `.flv` `.ts` `.m2ts` `.3gp`.

### <img src="docs/images/icon_journal.svg" height="22" alt="" /> Resume journals

<p>
  <img src="docs/images/icon_journal.svg" height="20" alt="" />
  &nbsp;<strong>SectorPulse_logs — resume &amp; retry journals</strong>
</p>

<img src="docs/images/feature_journal.svg" alt="" width="100%">

Skip already-restored files (matching size). Force-retry broken PDFs/videos left from earlier partial runs. **Retry Failed from Log…** reloads unresolved `failed` / `timeout` / `partial` entries.

### <img src="docs/images/icon_delete.svg" height="22" alt="" /> Delete from drive (optional, destructive)
Filesystem delete when paths work; raw MFT in-use clear in raw mode (Admin often required). Protects `$MFT`, `$LogFile`, `$Bitmap`, `System Volume Information`, and other `$` metadata.

### <img src="docs/images/icon_ui.svg" height="22" alt="" /> Dark recovery console
CustomTkinter UI — graphite + signal teal. Drive list, file map with health column, mark/select-all, activity log, progress bar, open restored file, reveal in Explorer, selection detail panel.

<br>

## <img src="docs/images/icon_can.svg" height="28" alt="" /> What It Can Do

- <img src="docs/images/icon_can.svg" height="14" alt="" /> Recover files from dying USB / external **NTFS** disks that Explorer cannot copy
- <img src="docs/images/icon_can.svg" height="14" alt="" /> Keep going when individual sectors are bad (partial files with zero gaps)
- <img src="docs/images/icon_can.svg" height="14" alt="" /> Map folders when Win32 listing fails, via raw MFT
- <img src="docs/images/icon_can.svg" height="14" alt="" /> Salvage resident + non-resident NTFS data (runlists, sparse holes)
- <img src="docs/images/icon_can.svg" height="14" alt="" /> Follow `$ATTRIBUTE_LIST` extension records when they are still in the MFT cache
- <img src="docs/images/icon_can.svg" height="14" alt="" /> Place orphaned MFT entries under `_orphaned`
- <img src="docs/images/icon_can.svg" height="14" alt="" /> Auto-repair many recovered PDFs and videos enough to open in a reader/player
- <img src="docs/images/icon_can.svg" height="14" alt="" /> Resume large jobs and retry failures from the journal
- <img src="docs/images/icon_can.svg" height="14" alt="" /> Time out hung files instead of freezing forever

<br>

## <img src="docs/images/icon_cannot.svg" height="28" alt="" /> What It Cannot Do

- <img src="docs/images/icon_cannot.svg" height="14" alt="" /> **Not a full disk imager** — selected files/folders, not a forensic `dd` of the whole device
- <img src="docs/images/icon_cannot.svg" height="14" alt="" /> **Not for non-Windows hosts** — drive letters + WinAPI
- <img src="docs/images/icon_cannot.svg" height="14" alt="" /> **Not for the system drive as a target** — intentionally ignored
- <img src="docs/images/icon_cannot.svg" height="14" alt="" /> **Not magic undelete** for fully overwritten data
- <img src="docs/images/icon_cannot.svg" height="14" alt="" /> **Limited / no support** for exFAT, FAT32, ReFS, locked BitLocker, APFS, ext4 — raw path is **NTFS-only**
- <img src="docs/images/icon_cannot.svg" height="14" alt="" /> **Encrypted / compressed** NTFS streams may be incomplete without Windows decrypting them
- <img src="docs/images/icon_cannot.svg" height="14" alt="" /> **ADS / hard links / reparse points** are not first-class targets
- <img src="docs/images/icon_cannot.svg" height="14" alt="" /> Heavily fragmented `$ATTRIBUTE_LIST` extents missing from the MFT cache → marked incomplete
- <img src="docs/images/icon_cannot.svg" height="14" alt="" /> **Does not guarantee** playable/openable files — zero-padded holes can still break codecs, Office docs, archives
- <img src="docs/images/icon_cannot.svg" height="14" alt="" /> **Not a substitute** for a professional lab on mechanically failing platters (clicking drive → clone first if you can)
- <img src="docs/images/icon_cannot.svg" height="14" alt="" /> **Delete is destructive** — raw delete clears MFT in-use flags; it is not a secure wipe

<br>

## <img src="docs/images/icon_req.svg" height="28" alt="" /> Requirements

| Requirement | Why |
|---|---|
| **Windows 10 / 11** | Drive letters, WinAPI, `\\.\X:` raw volumes |
| **Python 3.10+** | Runtime |
| **Git** (optional) | To clone the repo |
| **Administrator** (often) | Raw volume open / raw delete on dying NTFS |
| **Healthy destination disk** | Always restore *off* the dying drive |
| *(Optional)* `ffmpeg` on `PATH` | Better video remux (else bundled `imageio-ffmpeg`) |

**Python packages** (`requirements.txt`):

```
customtkinter>=5.2.0
psutil>=5.9.0
pillow>=10.0.0
imageio-ffmpeg>=0.5.0
pypdf>=4.0.0
```

<br>

## <img src="docs/images/icon_start.svg" height="28" alt="" /> Quick Start

```bat
git clone https://github.com/ravirajg/sectorpulse.git
cd sectorpulse
python -m pip install -r requirements.txt
python main.py
```

Or double-click **`run.bat`** — it installs deps quietly, then launches.

> **Tip:** Run as Administrator if the volume is unreadable in Explorer and raw NTFS mode is required.

<br>

## <img src="docs/images/icon_usage.svg" height="28" alt="" /> Usage Walkthrough

1. Connect the **damaged** drive over USB (do not use it as the only copy destination).
2. Launch SectorPulse — wait until the volume appears under **Detected Volumes**.
3. Select the drive → **Scan Drive**.
4. Mark folders/files (click rows; use Select all / Clear / Mark).
5. **Set Destination…** to a healthy disk with enough free space.
6. **Restore Selected** — watch the activity log; partials are still useful.
7. Optionally **Open Restored File** / **Show in Explorer**.
8. If some files timed out → **Retry Failed from Log…** later.
9. Only after you have verified copies: **Delete from Drive** if you need space (destructive).

### <img src="docs/images/icon_safety.svg" height="22" alt="" /> Safety checklist

- Always restore **off** the dying disk.
- Prefer cloning first if the drive is clicking or dropping offline.
- Expect **partial** files when sectors are dead — open and verify critical data.
- Keep `SectorPulse_logs` until the job is fully done.

<br>

## <img src="docs/images/icon_eng.svg" height="28" alt="" /> Engineering Notes

A few parts that were interesting to build:

- **Raw NTFS without a mountable tree** — `CreateFileW("\\.\X:")`, boot-sector parse, `$MFT` run walking, USA fixups, `$FILE_NAME` / `$DATA` / `$ATTRIBUTE_LIST` merge, then sector-by-sector fallback so only truly dead sectors become zeros.
- **Hang isolation** — each restore runs on a daemon worker; the UI thread only joins with a timeout. PDF repair uses a spawned process pool so corrupt object streams cannot freeze the parent.
- **Cheap monitor loop** — letter-mask checks every tick; expensive `disk_usage` / volume info only when the set of letters changes, so dying disks are not hammered every 1.5 seconds.
- **Resume without a database** — append-only JSONL journals under the destination, pruned by later `ok` / `skipped` statuses.

Core modules: [`app/core/ntfs_raw.py`](app/core/ntfs_raw.py) · [`app/core/recovery.py`](app/core/recovery.py) · [`app/core/scanner.py`](app/core/scanner.py) · [`app/ui/main_window.py`](app/ui/main_window.py)

<br>

## <img src="docs/images/icon_structure.svg" height="28" alt="" /> Project Structure

```
Recovery/
├── main.py                     # Entry — Windows gate + RecoveryApp
├── run.bat                     # One-click launch
├── requirements.txt
├── docs/images/                # README banners & diagrams
└── app/
    ├── ui/
    │   ├── main_window.py      # CustomTkinter console
    │   └── theme.py            # Graphite + teal palette
    └── core/
        ├── drive_monitor.py    # Hot-plug volume watcher
        ├── scanner.py          # Resilient tree scan + FileNode
        ├── ntfs_raw.py         # Raw volume I/O, MFT, raw recover
        ├── recovery.py         # Chunked restore + timeouts
        ├── recovery_journal.py # JSONL / TXT session logs
        ├── doc_repair.py       # PDF carve + pypdf rewrite
        ├── video_repair.py     # ffmpeg remux + AVI idx1
        ├── deleter.py          # FS + raw MFT delete
        └── file_opener.py      # Shell open / Explorer reveal
```

<br>

## <img src="docs/images/icon_config.svg" height="28" alt="" /> Configuration Knobs

In `RecoveryEngine` ([`app/core/recovery.py`](app/core/recovery.py)):

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `chunk_size` | `64 * 1024` | Read/pad granularity (Win32 path) |
| `max_retries` | `4` | Attempts per chunk before zero-fill |
| `retry_delay` | `0.05` | Backoff base (seconds × attempt) |
| `file_timeout_sec` | size-based | Override per-file hang limit |

Raw reads fall back: multi-cluster → per-cluster → **per-sector** (up to 8 attempts/sector).

Theme tokens live in [`app/ui/theme.py`](app/ui/theme.py) (`#0B0F14` graphite · `#2EE6A6` signal teal · `#F0B429` partial · `#FF5C7A` bad · `#4DA3FF` info).

<br>

## <img src="docs/images/icon_warn.svg" height="28" alt="" /> Limitations & Responsible Use

- **Your data, your disk.** Only recover media you own or are authorized to access.
- **Lossy by nature.** Bad sectors mean missing bytes. Always verify restored files before wiping the source.
- **Prefer read-only until verified.** Use Delete only after copies exist on healthy storage.
- **Mechanical failure.** Clicking / reconnecting drives belong under a cloner or a lab — SectorPulse helps software-visible bad sectors, not dying heads.
- **Provided as-is**, without warranty. Authors are not responsible for data loss or misuse of delete features.

<br>

## <img src="docs/images/icon_heart.svg" height="28" alt="" /> Contributing

Issues and PRs welcome. High-value areas:

- Broader filesystem support (exFAT)
- Better `$ATTRIBUTE_LIST` / non-resident list handling
- Compression / EFS awareness
- Unit tests with synthetic MFT fixtures
- Packaging (`pyinstaller` / MSIX) for non-Python users

Please do **not** open issues asking for help recovering data you do not own.

<br>

## <img src="docs/images/icon_license.svg" height="28" alt="" /> License

[MIT](LICENSE) — free to use, modify, and distribute.

<img src="docs/images/divider.svg" width="100%" alt="">

<div align="center">

<img src="docs/images/icon.svg" height="36" alt="" />

**SECTORPULSE** · resilient recovery · bad-sector skip · raw NTFS · resume journals

`python main.py` &nbsp;·&nbsp; `run.bat`

</div>
