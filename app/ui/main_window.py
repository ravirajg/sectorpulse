"""SectorPulse main window — cool dark recovery console."""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional

import customtkinter as ctk

from app.core.deleter import VolumeDeleter, collect_delete_targets
from app.core.drive_monitor import DriveInfo, DriveMonitor
from app.core.file_opener import app_hint_for, open_path, reveal_in_explorer
from app.core.recovery import RecoveryEngine, RecoveryResult, collect_selected
from app.core.recovery_journal import iter_retry_entries
from app.core.scanner import DriveScanner, FileNode, NodeStatus
from app.ui.theme import COLORS, FONTS


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit, div in (("KB", 1024), ("MB", 1024**2), ("GB", 1024**3), ("TB", 1024**4)):
        if n < div * 1024 or unit == "TB":
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


class RecoveryApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("SectorPulse — Drive Recovery")
        self.geometry("1280x800")
        self.minsize(1024, 680)
        self.configure(fg_color=COLORS["bg"])

        self.monitor = DriveMonitor(interval=1.5)
        self.drives: list[DriveInfo] = []
        self.selected_drive: Optional[DriveInfo] = None
        self.tree_root: Optional[FileNode] = None
        self.node_by_iid: dict[str, FileNode] = {}
        self.iid_by_path: dict[str, str] = {}
        self.restored_map: dict[str, str] = {}  # source path -> dest path
        self._restore_dest: Optional[str] = None
        self._scanner: Optional[DriveScanner] = None
        self._engine: Optional[RecoveryEngine] = None
        self._deleter: Optional[VolumeDeleter] = None
        self._scan_thread: Optional[threading.Thread] = None
        self._recover_thread: Optional[threading.Thread] = None
        self._delete_thread: Optional[threading.Thread] = None
        self._pulse_phase = 0
        self._waiting_animation = True
        self._closed = False
        self._busy = False

        self._build_style()
        self._build_ui()
        self._bind_monitor()
        self.after(80, self._animate_pulse)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.monitor.start()

    # ── chrome ──────────────────────────────────────────────────────────

    def _build_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Dark.Treeview",
            background=COLORS["bg_elevated"],
            fieldbackground=COLORS["bg_elevated"],
            foreground=COLORS["text"],
            borderwidth=0,
            rowheight=28,
            font=FONTS["body"],
        )
        style.configure(
            "Dark.Treeview.Heading",
            background=COLORS["bg_panel"],
            foreground=COLORS["text_dim"],
            relief="flat",
            font=FONTS["tiny"],
        )
        style.map(
            "Dark.Treeview",
            background=[("selected", COLORS["bg_hover"])],
            foreground=[("selected", COLORS["accent_glow"])],
        )
        style.configure(
            "Dark.Vertical.TScrollbar",
            background=COLORS["bg_panel"],
            troughcolor=COLORS["bg"],
            bordercolor=COLORS["bg"],
            arrowcolor=COLORS["text_dim"],
        )

    def _build_ui(self) -> None:
        # Header
        header = ctk.CTkFrame(self, fg_color=COLORS["bg"], height=88, corner_radius=0)
        header.pack(fill="x", padx=0, pady=0)
        header.pack_propagate(False)

        brand_row = ctk.CTkFrame(header, fg_color="transparent")
        brand_row.pack(fill="x", padx=28, pady=(18, 0))

        self.brand = ctk.CTkLabel(
            brand_row,
            text="SECTORPULSE",
            font=FONTS["display"],
            text_color=COLORS["accent"],
        )
        self.brand.pack(side="left")

        ctk.CTkLabel(
            brand_row,
            text="  RESILIENT RECOVERY",
            font=FONTS["tiny"],
            text_color=COLORS["text_mute"],
        ).pack(side="left", padx=(8, 0), pady=(12, 0))

        self.status_pill = ctk.CTkLabel(
            brand_row,
            text="●  SCANNING FOR DRIVES",
            font=FONTS["tiny"],
            text_color=COLORS["accent"],
            fg_color=COLORS["bg_panel"],
            corner_radius=12,
            padx=12,
            pady=4,
        )
        self.status_pill.pack(side="right", pady=(8, 0))

        ctk.CTkLabel(
            header,
            text="Plug in the damaged drive over USB — SectorPulse will detect volumes, map readable folders, and salvage files past bad sectors.",
            font=FONTS["tiny"],
            text_color=COLORS["text_dim"],
            anchor="w",
        ).pack(fill="x", padx=28, pady=(4, 0))

        # Accent line
        ctk.CTkFrame(self, fg_color=COLORS["accent_dim"], height=2, corner_radius=0).pack(fill="x")

        body = ctk.CTkFrame(self, fg_color=COLORS["bg"], corner_radius=0)
        body.pack(fill="both", expand=True, padx=20, pady=16)

        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        self._build_drive_panel(body)
        self._build_center(body)
        self._build_action_panel(body)

        # Footer log
        footer = ctk.CTkFrame(self, fg_color=COLORS["bg_panel"], height=140, corner_radius=0)
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)

        log_header = ctk.CTkFrame(footer, fg_color="transparent")
        log_header.pack(fill="x", padx=20, pady=(10, 0))
        ctk.CTkLabel(
            log_header, text="ACTIVITY", font=FONTS["tiny"], text_color=COLORS["text_mute"]
        ).pack(side="left")
        self.progress = ctk.CTkProgressBar(
            log_header, width=220, height=8, progress_color=COLORS["accent"], fg_color=COLORS["border"]
        )
        self.progress.pack(side="right")
        self.progress.set(0)

        self.log_box = ctk.CTkTextbox(
            footer,
            font=FONTS["mono_small"],
            fg_color=COLORS["bg_elevated"],
            text_color=COLORS["text_dim"],
            border_color=COLORS["border"],
            border_width=1,
            height=90,
        )
        self.log_box.pack(fill="both", expand=True, padx=20, pady=(6, 12))
        self.log_box.insert("end", "Waiting for a non-system drive… connect the crashed disk via USB.\n")
        self.log_box.configure(state="disabled")

    def _build_drive_panel(self, parent: ctk.CTkFrame) -> None:
        panel = ctk.CTkFrame(parent, fg_color=COLORS["bg_panel"], corner_radius=14, width=280)
        panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        panel.grid_propagate(False)

        ctk.CTkLabel(
            panel, text="DETECTED VOLUMES", font=FONTS["tiny"], text_color=COLORS["text_mute"]
        ).pack(anchor="w", padx=16, pady=(16, 8))

        self.drive_list = ctk.CTkScrollableFrame(
            panel, fg_color=COLORS["bg_elevated"], corner_radius=10, height=360
        )
        self.drive_list.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.wait_card = ctk.CTkFrame(self.drive_list, fg_color=COLORS["bg_hover"], corner_radius=12)
        self.wait_card.pack(fill="x", pady=8, padx=4)
        self.pulse_ring = ctk.CTkLabel(
            self.wait_card,
            text="◎",
            font=("Segoe UI", 36),
            text_color=COLORS["accent"],
        )
        self.pulse_ring.pack(pady=(18, 4))
        ctk.CTkLabel(
            self.wait_card,
            text="Listening for USB…",
            font=FONTS["body_bold"],
            text_color=COLORS["text"],
        ).pack()
        ctk.CTkLabel(
            self.wait_card,
            text="Attach the damaged drive.\nSystem drive is ignored.",
            font=FONTS["tiny"],
            text_color=COLORS["text_dim"],
            justify="center",
        ).pack(pady=(4, 18))

        self.drive_buttons: list[ctk.CTkButton] = []

        hint = ctk.CTkLabel(
            panel,
            text="Tip: USB adapters / enclosures\nshow up as Removable or Fixed.",
            font=FONTS["tiny"],
            text_color=COLORS["text_mute"],
            justify="left",
        )
        hint.pack(anchor="w", padx=16, pady=(0, 16))

    def _build_center(self, parent: ctk.CTkFrame) -> None:
        center = ctk.CTkFrame(parent, fg_color=COLORS["bg_panel"], corner_radius=14)
        center.grid(row=0, column=1, sticky="nsew")

        top = ctk.CTkFrame(center, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(14, 8))

        ctk.CTkLabel(
            top, text="FILE MAP", font=FONTS["tiny"], text_color=COLORS["text_mute"]
        ).pack(side="left")

        self.stats_label = ctk.CTkLabel(
            top, text="No scan yet", font=FONTS["tiny"], text_color=COLORS["text_dim"]
        )
        self.stats_label.pack(side="right")

        tree_wrap = ctk.CTkFrame(center, fg_color=COLORS["bg_elevated"], corner_radius=10)
        tree_wrap.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.tree = ttk.Treeview(
            tree_wrap,
            columns=("size", "status"),
            show="tree headings",
            style="Dark.Treeview",
            selectmode="extended",
        )
        self.tree.heading("#0", text="Name", anchor="w")
        self.tree.heading("size", text="Size", anchor="e")
        self.tree.heading("status", text="Health", anchor="w")
        self.tree.column("#0", width=420, stretch=True)
        self.tree.column("size", width=90, anchor="e", stretch=False)
        self.tree.column("status", width=120, anchor="w", stretch=False)

        scroll_y = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll_y.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        scroll_y.pack(side="right", fill="y", pady=8, padx=(0, 8))

        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-1>", self._on_tree_double)
        self.tree.bind("<space>", self._toggle_selected_checks)
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<<TreeviewOpen>>", self._on_tree_open)

        # Checkbox legend / selection bar
        sel_bar = ctk.CTkFrame(center, fg_color="transparent")
        sel_bar.pack(fill="x", padx=16, pady=(0, 12))

        self.select_label = ctk.CTkLabel(
            sel_bar,
            text="0 items marked · click row to mark · arrow to expand",
            font=FONTS["tiny"],
            text_color=COLORS["text_dim"],
        )
        self.select_label.pack(side="left")

        ctk.CTkButton(
            sel_bar,
            text="Select all",
            width=90,
            height=28,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["border"],
            text_color=COLORS["text"],
            command=self._select_all,
        ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(
            sel_bar,
            text="Clear",
            width=70,
            height=28,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["border"],
            text_color=COLORS["text"],
            command=self._clear_selection,
        ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(
            sel_bar,
            text="Mark / Unmark",
            width=110,
            height=28,
            fg_color=COLORS["accent_dim"],
            hover_color=COLORS["accent"],
            text_color="#04140F",
            command=lambda: self._toggle_selected_checks(),
        ).pack(side="right")

    def _build_action_panel(self, parent: ctk.CTkFrame) -> None:
        panel = ctk.CTkFrame(parent, fg_color=COLORS["bg_panel"], corner_radius=14, width=260)
        panel.grid(row=0, column=2, sticky="nsew", padx=(12, 0))
        panel.grid_propagate(False)

        ctk.CTkLabel(
            panel, text="ACTIONS", font=FONTS["tiny"], text_color=COLORS["text_mute"]
        ).pack(anchor="w", padx=16, pady=(16, 10))

        self.btn_scan = ctk.CTkButton(
            panel,
            text="Scan Drive",
            height=42,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_glow"],
            text_color="#04140F",
            font=FONTS["body_bold"],
            command=self._start_scan,
        )
        self.btn_scan.pack(fill="x", padx=16, pady=(0, 8))

        self.btn_cancel_scan = ctk.CTkButton(
            panel,
            text="Stop Scan",
            height=34,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["border"],
            text_color=COLORS["text"],
            command=self._cancel_scan,
            state="disabled",
        )
        self.btn_cancel_scan.pack(fill="x", padx=16, pady=(0, 16))

        ctk.CTkFrame(panel, fg_color=COLORS["border"], height=1).pack(fill="x", padx=16, pady=4)

        ctk.CTkLabel(
            panel,
            text="Restore selected folders\nand files to a safe disk.",
            font=FONTS["tiny"],
            text_color=COLORS["text_dim"],
            justify="left",
        ).pack(anchor="w", padx=16, pady=(12, 8))

        self.btn_restore = ctk.CTkButton(
            panel,
            text="Restore Selected",
            height=42,
            fg_color=COLORS["info"],
            hover_color="#6BB6FF",
            text_color="#061018",
            font=FONTS["body_bold"],
            command=self._start_restore,
            state="disabled",
        )
        self.btn_restore.pack(fill="x", padx=16, pady=(0, 8))

        self.btn_set_dest = ctk.CTkButton(
            panel,
            text="Set Destination…",
            height=32,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["border"],
            text_color=COLORS["text"],
            command=self._choose_destination,
        )
        self.btn_set_dest.pack(fill="x", padx=16, pady=(0, 8))

        self.btn_retry_failed = ctk.CTkButton(
            panel,
            text="Retry Failed from Log…",
            height=32,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["border"],
            text_color=COLORS["text"],
            command=self._retry_failed_from_log,
            state="disabled",
        )
        self.btn_retry_failed.pack(fill="x", padx=16, pady=(0, 8))

        self.btn_delete = ctk.CTkButton(
            panel,
            text="Delete from Drive",
            height=38,
            fg_color=COLORS["danger"],
            hover_color="#FF7A93",
            text_color="#1A0508",
            font=FONTS["body_bold"],
            command=self._start_delete,
            state="disabled",
        )
        self.btn_delete.pack(fill="x", padx=16, pady=(0, 8))

        self.btn_open = ctk.CTkButton(
            panel,
            text="Open Restored File",
            height=38,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["border"],
            text_color=COLORS["text"],
            font=FONTS["body_bold"],
            command=self._open_selected_restored,
            state="disabled",
        )
        self.btn_open.pack(fill="x", padx=16, pady=(0, 8))

        self.btn_reveal = ctk.CTkButton(
            panel,
            text="Show in Explorer",
            height=34,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["border"],
            text_color=COLORS["text"],
            command=self._reveal_selected,
            state="disabled",
        )
        self.btn_reveal.pack(fill="x", padx=16, pady=(0, 16))

        self.dest_label = ctk.CTkLabel(
            panel,
            text="Destination: not set",
            font=FONTS["tiny"],
            text_color=COLORS["text_mute"],
            wraplength=220,
            justify="left",
            anchor="w",
        )
        self.dest_label.pack(fill="x", padx=16, pady=(0, 8))

        self.detail_card = ctk.CTkFrame(panel, fg_color=COLORS["bg_elevated"], corner_radius=10)
        self.detail_card.pack(fill="both", expand=True, padx=12, pady=(8, 16))
        ctk.CTkLabel(
            self.detail_card,
            text="SELECTION DETAIL",
            font=FONTS["tiny"],
            text_color=COLORS["text_mute"],
        ).pack(anchor="w", padx=12, pady=(10, 4))
        self.detail_text = ctk.CTkLabel(
            self.detail_card,
            text="Select a file to preview\nhealth and open hints.",
            font=FONTS["tiny"],
            text_color=COLORS["text_dim"],
            justify="left",
            anchor="nw",
        )
        self.detail_text.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    # ── drive monitor wiring ────────────────────────────────────────────

    def _bind_monitor(self) -> None:
        self.monitor.on_drives_updated = lambda drives: self.after(
            0, lambda d=list(drives): self._refresh_drives(d)
        )
        self.monitor.on_drive_added = lambda drive: self.after(
            0, lambda d=drive: self._on_drive_added(d)
        )
        self.monitor.on_drive_removed = lambda letter: self.after(
            0, lambda L=letter: self._log(f"Drive {L} disconnected.", "warn")
        )

    def _refresh_drives(self, drives: list[DriveInfo]) -> None:
        if self._closed:
            return
        self.drives = drives
        for btn in self.drive_buttons:
            try:
                btn.destroy()
            except Exception:
                pass
        self.drive_buttons.clear()

        if not drives:
            self.selected_drive = None
            self.wait_card.pack(fill="x", pady=8, padx=4)
            self._waiting_animation = True
            self.status_pill.configure(text="●  SCANNING FOR DRIVES", text_color=COLORS["accent"])
            return

        self.wait_card.pack_forget()
        self._waiting_animation = False
        self.status_pill.configure(
            text=f"●  {len(drives)} VOLUME{'S' if len(drives) != 1 else ''} READY",
            text_color=COLORS["ok"],
        )

        if self.selected_drive and self.selected_drive.letter not in {d.letter for d in drives}:
            self.selected_drive = None

        for drive in drives:
            is_sel = self.selected_drive and self.selected_drive.letter == drive.letter
            if not drive.is_accessible:
                badge = "DAMAGED"
                detail = "unreadable · scan may still recover files"
            else:
                badge = "USB" if drive.is_removable else drive.drive_type.upper()
                detail = f"{_fmt_size(drive.total_bytes)} · {drive.filesystem}"
            btn = ctk.CTkButton(
                self.drive_list,
                text=f"{drive.letter}  {drive.label or 'Volume'}\n{badge} · {detail}",
                anchor="w",
                height=64,
                fg_color=COLORS["accent_dim"] if is_sel else COLORS["bg_hover"],
                hover_color=COLORS["border"],
                text_color=COLORS["text"] if not is_sel else "#04140F",
                font=FONTS["tiny"],
                command=lambda d=drive: self._select_drive(d),
            )
            btn.pack(fill="x", pady=4, padx=4)
            self.drive_buttons.append(btn)

        if drives and not self.selected_drive:
            # Prefer an inaccessible recovery target when present.
            pick = next((d for d in drives if not d.is_accessible), drives[0])
            self._select_drive(pick)

    def _on_drive_added(self, drive: DriveInfo) -> None:
        level = "warn" if not drive.is_accessible else "ok"
        self._log(f"Detected {drive.display_name}", level)
        if drive.error:
            self._log(drive.error, "warn")
        self._select_drive(drive)

    def _select_drive(self, drive: DriveInfo) -> None:
        self.selected_drive = drive
        # Restyle existing buttons only — do not rebuild from a possibly stale list.
        for btn, d in zip(self.drive_buttons, self.drives):
            is_sel = d.letter == drive.letter
            btn.configure(
                fg_color=COLORS["accent_dim"] if is_sel else COLORS["bg_hover"],
                text_color="#04140F" if is_sel else COLORS["text"],
            )
        self.btn_scan.configure(state="normal")
        self._log(f"Target set → {drive.display_name}")

    # ── scanning ────────────────────────────────────────────────────────

    def _start_scan(self) -> None:
        if self._busy:
            return
        if not self.selected_drive:
            messagebox.showinfo("SectorPulse", "Select a drive first (or plug one in).")
            return
        if self._scan_thread and self._scan_thread.is_alive():
            return

        self.tree.delete(*self.tree.get_children())
        self.node_by_iid.clear()
        self.iid_by_path.clear()
        self.tree_root = None
        self.restored_map.clear()
        self.btn_scan.configure(state="disabled")
        self.btn_cancel_scan.configure(state="normal")
        self.btn_restore.configure(state="disabled")
        self.btn_retry_failed.configure(state="disabled")
        self.btn_delete.configure(state="disabled")
        self.btn_open.configure(state="disabled")
        self.btn_reveal.configure(state="disabled")
        self.progress.set(0)
        self.stats_label.configure(text="Scanning…")
        self.status_pill.configure(text="●  SCANNING SECTORS", text_color=COLORS["warn"])
        mode = "raw NTFS" if not self.selected_drive.is_accessible else "filesystem"
        self._log(f"Deep scan started on {self.selected_drive.path} ({mode})")
        if not self.selected_drive.is_accessible:
            self._log(
                "Volume is unreadable via Explorer — using raw sector / MFT recovery.",
                "warn",
            )

        root_path = self.selected_drive.path
        self._scanner = DriveScanner(
            root_path,
            on_progress=lambda path, files, errs: self.after(
                0, lambda p=path, f=files, e=errs: self._scan_progress(p, f, e)
            ),
        )

        def worker() -> None:
            assert self._scanner is not None
            try:
                root = self._scanner.scan()
                self.after(0, lambda: self._scan_done(root))
            except Exception as exc:
                self.after(0, lambda: self._scan_failed(str(exc)))

        self._scan_thread = threading.Thread(target=worker, daemon=True, name="Scanner")
        self._scan_thread.start()

    def _cancel_scan(self) -> None:
        if self._scanner:
            self._scanner.cancel()
            self._log("Scan cancel requested…", "warn")

    def _scan_progress(self, path: str, files: int, errs: int) -> None:
        short = path if len(path) < 70 else "…" + path[-67:]
        self.stats_label.configure(text=f"{files:,} files · {errs} I/O issues · {short}")
        # Indeterminate-ish pulse
        self.progress.set(min(0.95, (files % 200) / 200))

    def _scan_done(self, root: FileNode) -> None:
        self.tree_root = root
        self._populate_tree(root)
        files, dirs, bad = root.count_stats()
        # count_stats on root counts root as dir
        self.stats_label.configure(
            text=f"{files:,} files · {max(0, dirs - 1):,} folders · {bad} damaged/partial"
        )
        self.progress.set(1)
        self.btn_scan.configure(state="normal")
        self.btn_cancel_scan.configure(state="disabled")
        self.btn_restore.configure(state="normal")
        self.btn_retry_failed.configure(state="normal")
        self.status_pill.configure(text="●  MAP READY", text_color=COLORS["ok"])
        self._log(
            f"Scan complete — {files:,} files, {bad} with read issues. Select items to restore.",
            "ok",
        )

    def _scan_failed(self, err: str) -> None:
        self.btn_scan.configure(state="normal")
        self.btn_cancel_scan.configure(state="disabled")
        self.status_pill.configure(text="●  SCAN FAILED", text_color=COLORS["danger"])
        self._log(f"Scan failed: {err}", "bad")
        messagebox.showerror("Scan failed", err)

    def _populate_tree(self, root: FileNode) -> None:
        self.tree.delete(*self.tree.get_children())
        self.node_by_iid.clear()
        self.iid_by_path.clear()

        # Lazy insert: only top-level rows. Children load when a folder is expanded.
        for child in root.children:
            self._insert_tree_node(child, "")

    def _insert_tree_node(self, node: FileNode, parent_iid: str) -> str:
        mark = "☑ " if node.selected else "☐ "
        if node.is_dir:
            label = f"{mark}📁 {node.name}"
            size = ""
        else:
            label = f"{mark}📄 {node.name}"
            size = _fmt_size(node.size)
        status = {
            NodeStatus.OK: "Healthy",
            NodeStatus.PARTIAL: "Partial risk",
            NodeStatus.UNREADABLE: "Bad sectors",
            NodeStatus.SKIPPED: "Skipped",
        }.get(node.status, node.status.value)

        iid = self.tree.insert(parent_iid, "end", text=label, values=(size, status), open=False)
        self.node_by_iid[iid] = node
        self.iid_by_path[os.path.normcase(node.path)] = iid
        if node.is_dir and node.children:
            # Dummy row so the expand arrow appears; replaced on open.
            self.tree.insert(iid, "end", text="…", values=("", ""), tags=("placeholder",))
        return iid

    def _is_placeholder(self, iid: str) -> bool:
        tags = self.tree.item(iid, "tags")
        if not tags:
            return False
        if isinstance(tags, str):
            return tags == "placeholder" or "placeholder" in tags.split()
        return "placeholder" in tags

    def _ensure_children_loaded(self, iid: str) -> None:
        """Replace lazy placeholder with real children (if any)."""
        node = self.node_by_iid.get(iid)
        if not node or not node.is_dir:
            return
        children = self.tree.get_children(iid)
        if len(children) != 1 or not self._is_placeholder(children[0]):
            return
        self.tree.delete(children[0])
        for child in node.children:
            if node.selected:
                child.selected = True
            self._insert_tree_node(child, iid)

    def _toggle_folder_open(self, iid: str) -> None:
        node = self.node_by_iid.get(iid)
        if not node or not node.is_dir or not node.children:
            return
        self._ensure_children_loaded(iid)
        self.tree.item(iid, open=not bool(self.tree.item(iid, "open")))

    def _on_tree_open(self, _event=None) -> None:
        iid = self.tree.focus()
        if iid:
            self._ensure_children_loaded(iid)

    def _click_is_expand_arrow(self, event, iid: str) -> bool:
        """True when the user clicked the tree expand/collapse control."""
        element = str(self.tree.identify_element(event.x, event.y) or "").lower()
        if "indicator" in element:
            return True
        bbox = self.tree.bbox(iid, "#0")
        if bbox and event.x < bbox[0]:
            # Indent gutter to the left of the label (where the arrow lives).
            return True
        return False

    def _on_tree_click(self, event) -> Optional[str]:
        """Arrow expands folders; anywhere else on the row toggles the checkbox."""
        if self._busy:
            return "break"
        region = self.tree.identify_region(event.x, event.y)
        if region not in ("tree", "cell"):
            return None

        iid = self.tree.identify_row(event.y)
        if not iid or iid not in self.node_by_iid:
            return None

        node = self.node_by_iid[iid]

        if self._click_is_expand_arrow(event, iid):
            if node.is_dir:
                self._toggle_folder_open(iid)
                self.tree.selection_set(iid)
                self.tree.focus(iid)
                self._show_detail(node)
            return "break"

        # Click on checkbox, name, size, or health → mark for restore
        self._set_node_check(iid, not node.selected)
        self._update_selection_label()
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self._show_detail(node)
        return "break"

    # ── selection ───────────────────────────────────────────────────────

    def _mark_model(self, node: FileNode, selected: bool) -> None:
        node.selected = selected
        if node.is_dir:
            for child in node.children:
                self._mark_model(child, selected)

    def _sync_check_glyphs(self, iid: str) -> None:
        """Update ☐/☑ for this row and any already-loaded descendants."""
        node = self.node_by_iid.get(iid)
        if not node:
            return
        text = self.tree.item(iid, "text")
        if text.startswith(("☐ ", "☑ ")):
            rest = text[2:]
        else:
            rest = text
        self.tree.item(iid, text=("☑ " if node.selected else "☐ ") + rest)
        for child_iid in self.tree.get_children(iid):
            if self._is_placeholder(child_iid):
                continue
            self._sync_check_glyphs(child_iid)

    def _set_node_check(self, iid: str, selected: bool) -> None:
        node = self.node_by_iid.get(iid)
        if not node:
            return
        # Update the FileNode tree in memory (fast even for huge folders).
        self._mark_model(node, selected)
        # Only rewrite glyphs for rows already in the Treeview.
        self._sync_check_glyphs(iid)

    def _toggle_selected_checks(self, _event=None) -> str:
        if self._busy:
            return "break"
        selection = self.tree.selection()
        if not selection:
            return "break"
        # If mixed, mark all; if all marked, unmark.
        states = []
        for iid in selection:
            node = self.node_by_iid.get(iid)
            if node:
                states.append(node.selected)
        new_state = not all(states) if states else True
        for iid in selection:
            if iid in self.node_by_iid:
                self._set_node_check(iid, new_state)
        self._update_selection_label()
        return "break"

    def _on_tree_select(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        node = self.node_by_iid.get(sel[0])
        if node:
            self._show_detail(node)

    def _on_tree_double(self, event) -> str:
        """Double-click expands folders; opens restored files only."""
        if self._busy:
            return "break"
        iid = self.tree.identify_row(event.y) or (self.tree.selection()[0] if self.tree.selection() else "")
        if not iid or iid not in self.node_by_iid:
            return "break"
        node = self.node_by_iid[iid]
        if node.is_dir:
            self._toggle_folder_open(iid)
            return "break"
        if node.path in self.restored_map:
            self._open_selected_restored()
        return "break"

    def _update_selection_label(self) -> None:
        if not self.tree_root:
            self.select_label.configure(text="0 items marked · click row to mark · arrow to expand")
            return
        selected = collect_selected(self.tree_root)
        file_count = 0
        for n in selected:
            if n.is_dir:
                file_count += sum(1 for _ in n.iter_files())
            else:
                file_count += 1
        self.select_label.configure(
            text=f"{len(selected)} item(s) marked · ~{file_count:,} file(s) queued"
        )
        state = "normal" if selected else "disabled"
        self.btn_restore.configure(state=state)
        self.btn_delete.configure(state=state)

    def _select_all(self) -> None:
        if self._busy:
            return
        for iid in self.tree.get_children(""):
            self._set_node_check(iid, True)
        self._update_selection_label()

    def _clear_selection(self) -> None:
        if self._busy:
            return
        if self.tree_root:
            for child in self.tree_root.children:
                self._mark_model(child, False)
        for iid in list(self.node_by_iid):
            if self._is_placeholder(iid):
                continue
            text = self.tree.item(iid, "text")
            rest = text[2:] if text.startswith(("☐ ", "☑ ")) else text
            self.tree.item(iid, text="☐ " + rest)
        self._update_selection_label()

    def _show_detail(self, node: FileNode) -> None:
        health = node.status.value
        hint = app_hint_for(node.path) if not node.is_dir else "Folder"
        restored = self.restored_map.get(node.path)
        lines = [
            f"{node.name}",
            f"",
            f"Type: {'Folder' if node.is_dir else 'File'}",
            f"Size: {_fmt_size(node.size) if not node.is_dir else '—'}",
            f"Health: {health}",
            f"Opens with: {hint}",
        ]
        if node.error:
            lines.append(f"Note: {node.error}")
        if restored:
            lines.append(f"Restored → {restored}")
            self.btn_open.configure(state="normal")
            self.btn_reveal.configure(state="normal")
        else:
            self.btn_open.configure(state="disabled")
            self.btn_reveal.configure(state="disabled")
        self.detail_text.configure(text="\n".join(lines))

    # ── restore ─────────────────────────────────────────────────────────

    def _set_ui_busy(self, busy: bool, status: str = "") -> None:
        """Lock interactive controls while a long job runs."""
        self._busy = busy
        if busy:
            self.tree.configure(selectmode="none")
            for btn in (
                self.btn_scan,
                self.btn_restore,
                self.btn_retry_failed,
                self.btn_delete,
                self.btn_set_dest,
                self.btn_open,
                self.btn_reveal,
            ):
                btn.configure(state="disabled")
            if status:
                self.status_pill.configure(text=status, text_color=COLORS["info"])
        else:
            self.tree.configure(selectmode="extended")
            self.btn_scan.configure(state="normal")
            self.btn_set_dest.configure(state="normal")
            if self.tree_root:
                self.btn_retry_failed.configure(state="normal")
            self._update_selection_label()
            if self.restored_map:
                self.btn_open.configure(state="normal")
                self.btn_reveal.configure(state="normal")

    def _choose_destination(self) -> None:
        if self._busy:
            return
        if not self.selected_drive:
            messagebox.showinfo("SectorPulse", "Select a source drive first.")
            return
        kwargs: dict = {
            "title": "Choose restore destination (resume folder)",
            "mustexist": True,
        }
        if self._restore_dest and os.path.isdir(self._restore_dest):
            kwargs["initialdir"] = self._restore_dest
        dest = filedialog.askdirectory(**kwargs)
        if not dest:
            return
        dest_norm = os.path.normcase(os.path.abspath(dest))
        src_norm = os.path.normcase(os.path.abspath(self.selected_drive.path))
        if dest_norm.startswith(src_norm.rstrip("\\")):
            messagebox.showerror(
                "Invalid destination",
                "Choose a folder on a different, healthy drive — not the damaged volume.",
            )
            return
        self._restore_dest = dest
        self.dest_label.configure(text=f"Destination:\n{dest}")
        self._log(f"Destination set → {dest} (existing good files will be skipped)", "ok")

    def _start_restore(self) -> None:
        if self._busy:
            return
        if not self.tree_root or not self.selected_drive:
            return
        selected = collect_selected(self.tree_root)
        if not selected:
            messagebox.showinfo("SectorPulse", "Select folders or files to restore.")
            return

        dest = self._restore_dest if self._restore_dest and os.path.isdir(self._restore_dest) else None
        if dest:
            if not messagebox.askyesno(
                "Resume restore?",
                f"Restore into:\n{dest}\n\n"
                "Files already present with matching size will be skipped.\n"
                "Choose No to pick a different folder.",
            ):
                dest = None
        if not dest:
            kwargs = {
                "title": "Choose a healthy destination for recovered files",
                "mustexist": True,
            }
            if self._restore_dest and os.path.isdir(self._restore_dest):
                kwargs["initialdir"] = self._restore_dest
            dest = filedialog.askdirectory(**kwargs)
            if not dest:
                return

        dest_norm = os.path.normcase(os.path.abspath(dest))
        src_norm = os.path.normcase(os.path.abspath(self.selected_drive.path))
        if dest_norm.startswith(src_norm.rstrip("\\")):
            messagebox.showerror(
                "Invalid destination",
                "Choose a folder on a different, healthy drive — not the damaged volume.",
            )
            return

        self._restore_dest = dest
        self.dest_label.configure(text=f"Destination:\n{dest}")
        self._set_ui_busy(True, "●  RECOVERING")
        self.btn_cancel_scan.configure(state="disabled")
        self.progress.set(0)
        self.stats_label.configure(text="Starting restore…")
        self._log(f"Restoring {len(selected)} selection(s) → {dest}")
        self._log("Resume enabled: matching destination files will be skipped.", "ok")
        self._log(
            "Hang guard on: files that stall are timed out, logged under "
            f"{os.path.join(dest, 'SectorPulse_logs')}, and the rest continue.",
            "ok",
        )

        engine = RecoveryEngine()
        self._engine = engine
        source_root = self.selected_drive.path

        def worker() -> None:
            batch = engine.recover_nodes(
                selected,
                destination_root=dest,
                source_root=source_root,
                on_progress=lambda path, i, total, result: self.after(
                    0, lambda p=path, a=i, t=total, r=result: self._restore_progress(p, a, t, r)
                ),
            )
            self.after(0, lambda: self._restore_done(batch))

        self._recover_thread = threading.Thread(target=worker, daemon=True, name="Recovery")
        self._recover_thread.start()

    def _restore_progress(
        self, path: str, index: int, total: int, result: Optional[RecoveryResult]
    ) -> None:
        self.progress.set(index / max(total, 1))
        full_path = path or ""

        if result is None:
            self.stats_label.configure(text=f"[{index}/{total}] {full_path}")
            self._log(f"[{index}/{total}] Working — {full_path}")
            return

        dest_path = result.destination or full_path
        if result.success and not result.partial:
            tag = "ok"
            if (result.error or "").startswith("already exists"):
                msg = f"[{index}/{total}] Skipped (already restored) — {dest_path}"
                if result.error and "·" in result.error:
                    msg += f" ({result.error.split('·', 1)[1].strip()})"
            elif result.error:
                msg = (
                    f"[{index}/{total}] Recovered {_fmt_size(result.bytes_recovered)} "
                    f"— {dest_path} ({result.error})"
                )
            else:
                msg = f"[{index}/{total}] Recovered {_fmt_size(result.bytes_recovered)} — {dest_path}"
            self.restored_map[result.source] = result.destination
            self.restored_map[path] = result.destination
        elif result.success and result.partial:
            tag = "warn"
            detail = result.error or f"{result.bad_sectors} bad sector(s)"
            msg = f"[{index}/{total}] Partial ({detail}) — {dest_path}"
            self.restored_map[result.source] = result.destination
            self.restored_map[path] = result.destination
        elif "timed out" in (result.error or "").lower():
            tag = "warn"
            msg = (
                f"[{index}/{total}] TIMEOUT — skipped, logged for retry — {dest_path}: "
                f"{result.error}"
            )
        else:
            tag = "bad"
            msg = f"[{index}/{total}] Failed — {dest_path}: {result.error}"
        self._log(msg, tag)
        self.stats_label.configure(text=f"[{index}/{total}] {dest_path}")

    def _restore_done(self, batch) -> None:
        self._set_ui_busy(False)
        self.status_pill.configure(text="●  RECOVERY COMPLETE", text_color=COLORS["ok"])
        self.progress.set(1)
        skipped = sum(
            1
            for r in batch.results
            if r.success and (r.error or "").startswith("already exists")
        )
        log_dir = os.path.join(batch.destination_root, "SectorPulse_logs")
        timeouts = getattr(batch, "timeout_count", 0)
        self._log(
            f"Done — {batch.ok_count} clean, {batch.partial_count} partial, "
            f"{batch.failed_count} failed ({timeouts} timed out), {skipped} skipped. "
            f"Logs: {log_dir}",
            "ok",
        )
        messagebox.showinfo(
            "Recovery complete",
            f"Restored to:\n{batch.destination_root}\n\n"
            f"Clean: {batch.ok_count}\n"
            f"Partial (bad sectors padded): {batch.partial_count}\n"
            f"Failed: {batch.failed_count}"
            + (f"  (including {timeouts} timed out / hung)\n" if timeouts else "\n")
            + f"Skipped (already present): {skipped}\n\n"
            f"Running logs:\n{log_dir}\n"
            f"  • recovery_processed.txt — files done\n"
            f"  • recovery_failed.txt — failed / timed out / partial\n"
            f"  • recovery_failed.jsonl — machine-readable retry list\n\n"
            f"Use “Retry Failed from Log…” later to re-run those files.",
        )
        if self.restored_map:
            self.btn_open.configure(state="normal")
            self.btn_reveal.configure(state="normal")

    def _index_tree_files(self) -> tuple[dict[str, FileNode], dict[int, FileNode]]:
        """Map relative_path / mft_ref → FileNode for journal retry matching."""
        by_rel: dict[str, FileNode] = {}
        by_mft: dict[int, FileNode] = {}
        if not self.tree_root:
            return by_rel, by_mft
        for n in self.tree_root.iter_files():
            rel = getattr(n, "_rel", None) or n.name
            if rel:
                by_rel[os.path.normcase(rel)] = n
            mft = getattr(n, "_mft_ref", None)
            if mft is not None:
                by_mft[int(mft)] = n
        return by_rel, by_mft

    def _retry_failed_from_log(self) -> None:
        if self._busy:
            return
        if not self.tree_root or not self.selected_drive:
            messagebox.showinfo("SectorPulse", "Scan the damaged drive first.")
            return

        dest = self._restore_dest if self._restore_dest and os.path.isdir(self._restore_dest) else None
        if not dest:
            kwargs = {
                "title": "Choose the destination folder that has SectorPulse_logs",
                "mustexist": True,
            }
            dest = filedialog.askdirectory(**kwargs)
            if not dest:
                return
            self._restore_dest = dest
            self.dest_label.configure(text=f"Destination:\n{dest}")

        entries = list(iter_retry_entries(dest))
        if not entries:
            messagebox.showinfo(
                "Nothing to retry",
                f"No pending failed/timed-out/partial files in:\n"
                f"{os.path.join(dest, 'SectorPulse_logs')}\n\n"
                "Either everything succeeded later, or no failures were logged yet.",
            )
            return

        by_rel, by_mft = self._index_tree_files()
        nodes: list[FileNode] = []
        unmatched: list[str] = []
        seen: set[str] = set()
        for entry in entries:
            node = None
            if entry.relative_path:
                node = by_rel.get(os.path.normcase(entry.relative_path))
            if node is None and entry.mft_ref is not None:
                node = by_mft.get(int(entry.mft_ref))
            if node is None and entry.source:
                # source may be relative path from earlier runs
                node = by_rel.get(os.path.normcase(entry.source.replace("/", "\\")))
            if node is None:
                unmatched.append(entry.destination or entry.source or "?")
                continue
            key = os.path.normcase(node.path)
            if key in seen:
                continue
            seen.add(key)
            nodes.append(node)

        if not nodes:
            messagebox.showerror(
                "Cannot match log entries",
                f"Found {len(entries)} failed log entr(y/ies) but none match the "
                f"current scan tree.\n\nRe-scan the same drive, then try again.\n"
                f"Logs: {os.path.join(dest, 'SectorPulse_logs')}",
            )
            return

        msg = (
            f"Retry {len(nodes)} file(s) from the failure log into:\n{dest}\n\n"
            f"Pending in log: {len(entries)}\n"
            f"Matched to current scan: {len(nodes)}\n"
        )
        if unmatched:
            msg += f"Unmatched (need re-scan): {len(unmatched)}\n"
        msg += "\nContinue?"
        if not messagebox.askyesno("Retry failed files", msg):
            return

        self._set_ui_busy(True, "●  RETRYING FAILED")
        self.btn_cancel_scan.configure(state="disabled")
        self.progress.set(0)
        self.stats_label.configure(text="Retrying failed files…")
        self._log(
            f"Retrying {len(nodes)} failed/timed-out file(s) from log → {dest}",
            "warn",
        )
        if unmatched:
            self._log(f"{len(unmatched)} log entr(y/ies) unmatched — re-scan if needed", "warn")

        engine = RecoveryEngine()
        self._engine = engine
        source_root = self.selected_drive.path

        def worker() -> None:
            batch = engine.recover_nodes(
                nodes,
                destination_root=dest,
                source_root=source_root,
                force=True,
                on_progress=lambda path, i, total, result: self.after(
                    0, lambda p=path, a=i, t=total, r=result: self._restore_progress(p, a, t, r)
                ),
            )
            self.after(0, lambda: self._restore_done(batch))

        self._recover_thread = threading.Thread(target=worker, daemon=True, name="RetryFailed")
        self._recover_thread.start()

    # ── delete from crashed drive ───────────────────────────────────────

    def _start_delete(self) -> None:
        if self._busy:
            return
        if not self.tree_root or not self.selected_drive:
            return
        selected = collect_selected(self.tree_root)
        if not selected:
            messagebox.showinfo("SectorPulse", "Mark folders or files to delete.")
            return

        targets = collect_delete_targets(selected)
        names = [getattr(n, "_rel", None) or n.name for n in selected[:8]]
        extra = "" if len(selected) <= 8 else f"\n…and {len(selected) - 8} more"
        preview = "\n".join(f"• {n}" for n in names) + extra

        if not messagebox.askyesno(
            "Delete from crashed drive?",
            "This permanently removes the marked items from the SOURCE drive "
            f"({self.selected_drive.letter}).\n\n"
            "Restore anything you need first — deletion cannot be undone.\n\n"
            f"{len(selected)} selection(s) → ~{len(targets)} object(s):\n{preview}\n\n"
            "Continue?",
            icon="warning",
        ):
            return

        if not messagebox.askyesno(
            "Final confirmation",
            f"Really delete these items from {self.selected_drive.letter}?\n\n"
            "On unreadable volumes SectorPulse clears NTFS MFT records directly. "
            "Run as Administrator if delete fails.",
            icon="warning",
        ):
            return

        self._set_ui_busy(True, "●  DELETING")
        self.status_pill.configure(text="●  DELETING", text_color=COLORS["danger"])
        self.progress.set(0)
        self._log(f"Deleting {len(targets)} object(s) from {self.selected_drive.letter}", "warn")

        deleter = VolumeDeleter()
        self._deleter = deleter
        letter = self.selected_drive.letter

        def worker() -> None:
            try:
                batch = deleter.delete_nodes(
                    selected,
                    drive_letter=letter,
                    on_progress=lambda name, i, total, result: self.after(
                        0, lambda n=name, a=i, t=total, r=result: self._delete_progress(n, a, t, r)
                    ),
                )
                self.after(0, lambda: self._delete_done(batch))
            except Exception as exc:
                self.after(0, lambda: self._delete_failed(str(exc)))

        self._delete_thread = threading.Thread(target=worker, daemon=True, name="Deleter")
        self._delete_thread.start()

    def _delete_progress(self, name: str, index: int, total: int, result) -> None:
        self.progress.set(index / max(total, 1))
        if result.success:
            self._log(f"[{index}/{total}] Deleted — {name}", "ok")
            self._remove_deleted_node(name, result.path)
        else:
            self._log(f"[{index}/{total}] Delete failed — {name}: {result.error}", "bad")
        self.stats_label.configure(text=f"Deleting {index}/{total}")

    def _remove_deleted_node(self, name: str, result_path: str) -> None:
        """Drop a successfully deleted node from the file map UI + model."""
        match_iid = None
        match_node = None
        for iid, node in list(self.node_by_iid.items()):
            rel = getattr(node, "_rel", None) or node.name
            if rel == result_path or node.name == name or rel == name:
                match_iid = iid
                match_node = node
                break
        if not match_node:
            return
        if match_node.parent and match_node in match_node.parent.children:
            match_node.parent.children.remove(match_node)
        if match_iid:
            try:
                self.tree.delete(match_iid)
            except Exception:
                pass
            self.node_by_iid.pop(match_iid, None)

    def _delete_done(self, batch) -> None:
        self._set_ui_busy(False)
        self.status_pill.configure(text="●  DELETE COMPLETE", text_color=COLORS["ok"])
        self.progress.set(1)
        self._log(
            f"Delete finished — {batch.ok_count} removed, {batch.failed_count} failed.",
            "ok" if batch.failed_count == 0 else "warn",
        )
        messagebox.showinfo(
            "Delete complete",
            f"Removed: {batch.ok_count}\nFailed: {batch.failed_count}\n\n"
            "If many failed, restart SectorPulse as Administrator and try again.",
        )

    def _delete_failed(self, err: str) -> None:
        self._set_ui_busy(False)
        self.status_pill.configure(text="●  DELETE FAILED", text_color=COLORS["danger"])
        self._log(f"Delete failed: {err}", "bad")
        messagebox.showerror(
            "Delete failed",
            f"{err}\n\nTip: run SectorPulse as Administrator so the volume can be locked for writes.",
        )

    def _open_selected_restored(self) -> None:
        node = self._current_node()
        if not node:
            messagebox.showinfo("Open", "Select a restored file in the list.")
            return
        dest = self.restored_map.get(node.path)
        if not dest:
            # Try any selected restored file
            for n in self.node_by_iid.values():
                if n.selected and n.path in self.restored_map:
                    dest = self.restored_map[n.path]
                    break
        if not dest:
            messagebox.showinfo(
                "Not restored yet",
                "Restore this file first, then open it.\n"
                f"It would open with: {app_hint_for(node.path)}",
            )
            return
        ok, msg = open_path(dest)
        self._log(msg, "ok" if ok else "bad")
        if not ok:
            messagebox.showerror("Open failed", msg)

    def _reveal_selected(self) -> None:
        node = self._current_node()
        path = None
        if node:
            path = self.restored_map.get(node.path)
        if not path and self.restored_map:
            path = next(iter(self.restored_map.values()))
        if not path:
            return
        ok, msg = reveal_in_explorer(path)
        self._log(msg, "ok" if ok else "bad")

    def _current_node(self) -> Optional[FileNode]:
        sel = self.tree.selection()
        if not sel:
            return None
        return self.node_by_iid.get(sel[0])

    # ── misc ────────────────────────────────────────────────────────────

    def _log(self, message: str, level: str = "info") -> None:
        prefix = {"ok": "✓", "warn": "!", "bad": "✗", "info": "·"}.get(level, "·")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{prefix}  {message}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _animate_pulse(self) -> None:
        if self._closed:
            return
        if self._waiting_animation:
            self._pulse_phase = (self._pulse_phase + 1) % 6
            glyphs = ["◎", "◯", "◉", "◯", "◎", "●"]
            colors = [
                COLORS["accent"],
                COLORS["accent_dim"],
                COLORS["accent_glow"],
                COLORS["accent_dim"],
                COLORS["accent"],
                COLORS["info"],
            ]
            try:
                self.pulse_ring.configure(
                    text=glyphs[self._pulse_phase],
                    text_color=colors[self._pulse_phase],
                )
                self.brand.configure(
                    text_color=COLORS["accent_glow"] if self._pulse_phase % 2 == 0 else COLORS["accent"]
                )
            except Exception:
                return
        self.after(280, self._animate_pulse)

    def _on_close(self) -> None:
        self._closed = True
        self._waiting_animation = False
        if self._scanner:
            self._scanner.cancel()
        if self._engine:
            self._engine.cancel()
        if self._deleter:
            self._deleter.cancel()
        self.monitor.stop()
        self.destroy()
