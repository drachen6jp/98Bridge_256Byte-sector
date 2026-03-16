"""
Sector-level hex viewer widget for PC-98 disk images.
Provides a traditional hex editor view with sector navigation,
bookmarking, search, and raw export.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import struct
import re


# Known PC-98 I/O and structure signatures for auto-annotation
KNOWN_SIGNATURES = {
    0: "Boot Sector / IPL",
}

# Common byte patterns to flag
BYTE_PATTERNS = {
    b'\xEB': 'JMP short (x86)',
    b'\xE9': 'JMP near (x86)',
    b'\xCD\x1B': 'INT 1Bh (PC-98 BIOS disk)',
    b'\xCD\x21': 'INT 21h (DOS)',
    b'\xCD\x18': 'INT 18h (PC-98 BIOS)',
    b'\x55\xAA': 'Boot signature',
    b'\xEB\x3C\x90': 'DOS boot jump',
}

# PC-98 specific structures at known BPB offsets
BPB_FIELDS = {
    0x0B: ("Bytes/Sector", "<H"),
    0x0D: ("Sects/Cluster", "B"),
    0x0E: ("Reserved Sects", "<H"),
    0x10: ("Num FATs", "B"),
    0x11: ("Root Entries", "<H"),
    0x13: ("Total Sects 16", "<H"),
    0x15: ("Media Desc", "B"),
    0x16: ("FAT Size", "<H"),
    0x18: ("Sects/Track", "<H"),
    0x1A: ("Num Heads", "<H"),
    0x1C: ("Hidden Sects", "<H"),
}


class HexViewerWidget(ttk.Frame):
    """
    A sector-level hex viewer with navigation and analysis features.
    Embeds into a parent tkinter container.
    """

    BYTES_PER_ROW = 16

    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self.disk = None
        self.current_sector = 0
        self.bookmarks = {}  # sector -> label
        self.sector_annotations = {}  # sector -> list of (offset, length, label)
        self._build_ui()

    def set_disk(self, disk_image):
        """Attach a disk image to this viewer."""
        self.disk = disk_image
        self.current_sector = 0
        self.bookmarks.clear()
        self.sector_annotations.clear()
        self._update_nav_limits()
        self._show_sector(0)

    # -- UI Construction --

    def _build_ui(self):
        # Navigation bar
        nav = ttk.Frame(self)
        nav.pack(fill=tk.X, pady=(0, 4))

        ttk.Button(nav, text="◀◀", width=3, command=self._go_first).pack(side=tk.LEFT)
        ttk.Button(nav, text="◀", width=3, command=self._go_prev).pack(side=tk.LEFT)

        ttk.Label(nav, text="Sector:").pack(side=tk.LEFT, padx=(8, 2))
        self.sector_var = tk.StringVar(value="0")
        self.sector_entry = ttk.Entry(nav, textvariable=self.sector_var, width=8)
        self.sector_entry.pack(side=tk.LEFT)
        self.sector_entry.bind('<Return>', lambda e: self._go_to_sector())
        ttk.Button(nav, text="Go", command=self._go_to_sector).pack(side=tk.LEFT, padx=2)

        ttk.Button(nav, text="▶", width=3, command=self._go_next).pack(side=tk.LEFT)
        ttk.Button(nav, text="▶▶", width=3, command=self._go_last).pack(side=tk.LEFT)

        self.sector_label = ttk.Label(nav, text="/ 0")
        self.sector_label.pack(side=tk.LEFT, padx=4)

        ttk.Separator(nav, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        # Offset display mode
        ttk.Label(nav, text="Show:").pack(side=tk.LEFT, padx=(0, 2))
        self.offset_mode = tk.StringVar(value="sector")
        ttk.Radiobutton(nav, text="Sector", variable=self.offset_mode,
                         value="sector", command=self._refresh).pack(side=tk.LEFT)
        ttk.Radiobutton(nav, text="Absolute", variable=self.offset_mode,
                         value="absolute", command=self._refresh).pack(side=tk.LEFT)

        ttk.Separator(nav, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        ttk.Button(nav, text="Bookmark", command=self._add_bookmark).pack(side=tk.LEFT, padx=2)
        ttk.Button(nav, text="Bookmarks…", command=self._show_bookmarks).pack(side=tk.LEFT, padx=2)

        # Tools bar
        tools = ttk.Frame(self)
        tools.pack(fill=tk.X, pady=(0, 4))

        ttk.Label(tools, text="Search hex:").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(tools, textvariable=self.search_var, width=30)
        self.search_entry.pack(side=tk.LEFT, padx=2)
        self.search_entry.bind('<Return>', lambda e: self._search_hex())
        ttk.Button(tools, text="Find", command=self._search_hex).pack(side=tk.LEFT, padx=2)
        ttk.Button(tools, text="Find Next", command=self._search_next).pack(side=tk.LEFT, padx=2)

        ttk.Separator(tools, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        ttk.Label(tools, text="Export sectors:").pack(side=tk.LEFT)
        self.export_from_var = tk.StringVar(value="0")
        ttk.Entry(tools, textvariable=self.export_from_var, width=6).pack(side=tk.LEFT, padx=1)
        ttk.Label(tools, text="to").pack(side=tk.LEFT)
        self.export_to_var = tk.StringVar(value="0")
        ttk.Entry(tools, textvariable=self.export_to_var, width=6).pack(side=tk.LEFT, padx=1)
        ttk.Button(tools, text="Export…", command=self._export_range).pack(side=tk.LEFT, padx=2)

        # Main hex display
        hex_frame = ttk.Frame(self)
        hex_frame.pack(fill=tk.BOTH, expand=True)

        self.hex_text = tk.Text(
            hex_frame,
            font=("Consolas", 10),
            wrap=tk.NONE,
            state=tk.DISABLED,
            bg='#1e1e2e',
            fg='#cdd6f4',
            insertbackground='#cdd6f4',
            selectbackground='#45475a',
            selectforeground='#cdd6f4',
            padx=8,
            pady=4,
        )

        yscroll = ttk.Scrollbar(hex_frame, orient=tk.VERTICAL, command=self.hex_text.yview)
        xscroll = ttk.Scrollbar(hex_frame, orient=tk.HORIZONTAL, command=self.hex_text.xview)
        self.hex_text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)

        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        xscroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.hex_text.pack(fill=tk.BOTH, expand=True)

        # Configure text tags for highlighting
        self.hex_text.tag_configure('offset', foreground='#89b4fa')
        self.hex_text.tag_configure('hex_byte', foreground='#cdd6f4')
        self.hex_text.tag_configure('hex_zero', foreground='#585b70')
        self.hex_text.tag_configure('hex_high', foreground='#f9e2af')
        self.hex_text.tag_configure('hex_ascii_printable', foreground='#a6e3a1')
        self.hex_text.tag_configure('ascii', foreground='#a6e3a1')
        self.hex_text.tag_configure('ascii_dot', foreground='#585b70')
        self.hex_text.tag_configure('separator', foreground='#45475a')
        self.hex_text.tag_configure('search_hit', background='#f9e2af', foreground='#1e1e2e')
        self.hex_text.tag_configure('annotation', foreground='#f38ba8')
        self.hex_text.tag_configure('header', foreground='#89b4fa', font=("Consolas", 10, "bold"))
        self.hex_text.tag_configure('bpb_field', foreground='#cba6f7')
        self.hex_text.tag_configure('signature', foreground='#f38ba8', font=("Consolas", 10, "bold"))

        # Annotation panel at bottom
        self.annot_var = tk.StringVar(value="")
        annot_label = ttk.Label(self, textvariable=self.annot_var, anchor=tk.W,
                                 font=("Consolas", 9), wraplength=700)
        annot_label.pack(fill=tk.X, pady=(4, 0))

        # Search state
        self._search_bytes = None
        self._search_pos = 0  # absolute byte offset for "find next"

    def _update_nav_limits(self):
        if self.disk:
            total = self.disk.total_sectors
            self.sector_label.configure(text=f"/ {total - 1}")
        else:
            self.sector_label.configure(text="/ 0")

    # -- Navigation --

    def _go_first(self):
        self._show_sector(0)

    def _go_prev(self):
        if self.current_sector > 0:
            self._show_sector(self.current_sector - 1)

    def _go_next(self):
        if self.disk and self.current_sector < self.disk.total_sectors - 1:
            self._show_sector(self.current_sector + 1)

    def _go_last(self):
        if self.disk:
            self._show_sector(self.disk.total_sectors - 1)

    def _go_to_sector(self):
        try:
            s = self.sector_var.get().strip()
            # Support hex input
            if s.startswith('0x') or s.startswith('0X'):
                sector = int(s, 16)
            else:
                sector = int(s)
            if self.disk and 0 <= sector < self.disk.total_sectors:
                self._show_sector(sector)
            else:
                messagebox.showwarning("Invalid Sector",
                    f"Sector must be 0–{self.disk.total_sectors - 1 if self.disk else 0}")
        except ValueError:
            messagebox.showwarning("Invalid Input", "Enter a decimal or hex (0x...) sector number.")

    # -- Display --

    def _show_sector(self, sector_num):
        if not self.disk:
            return

        self.current_sector = sector_num
        self.sector_var.set(str(sector_num))
        data = self.disk.read_sector(sector_num)
        self._render_hex(data, sector_num)

    def _refresh(self):
        self._show_sector(self.current_sector)

    def _render_hex(self, data, sector_num):
        self.hex_text.configure(state=tk.NORMAL)
        self.hex_text.delete('1.0', tk.END)

        sector_size = len(data)
        abs_offset_base = sector_num * sector_size
        use_absolute = self.offset_mode.get() == "absolute"

        # Header line
        header = f"{'Offset':>10s}  "
        for i in range(self.BYTES_PER_ROW):
            header += f"{i:02X} "
        header += " ASCII\n"
        self.hex_text.insert(tk.END, header, 'header')
        self.hex_text.insert(tk.END, "─" * 78 + "\n", 'separator')

        # Sector annotation
        annot_parts = []
        if sector_num in KNOWN_SIGNATURES:
            annot_parts.append(KNOWN_SIGNATURES[sector_num])

        # Detect BPB on sector 0
        is_boot = (sector_num == 0 and len(data) >= 64 and data[0] in (0xEB, 0xE9))

        for row_start in range(0, sector_size, self.BYTES_PER_ROW):
            # Offset column
            if use_absolute:
                off_val = abs_offset_base + row_start
                off_str = f"0x{off_val:08X}"
            else:
                off_str = f"0x{row_start:04X}"
            self.hex_text.insert(tk.END, f"{off_str:>10s}  ", 'offset')

            # Hex bytes
            row_data = data[row_start:row_start + self.BYTES_PER_ROW]
            for i, byte in enumerate(row_data):
                if byte == 0x00:
                    tag = 'hex_zero'
                elif byte >= 0x80:
                    tag = 'hex_high'
                else:
                    tag = 'hex_byte'

                # Check if this is a BPB field
                if is_boot and (row_start + i) in BPB_FIELDS:
                    tag = 'bpb_field'

                self.hex_text.insert(tk.END, f"{byte:02X} ", tag)

            # Pad short rows
            if len(row_data) < self.BYTES_PER_ROW:
                pad = self.BYTES_PER_ROW - len(row_data)
                self.hex_text.insert(tk.END, "   " * pad)

            self.hex_text.insert(tk.END, " ", 'separator')

            # ASCII column
            for byte in row_data:
                if 0x20 <= byte <= 0x7E:
                    self.hex_text.insert(tk.END, chr(byte), 'ascii')
                else:
                    self.hex_text.insert(tk.END, '·', 'ascii_dot')

            self.hex_text.insert(tk.END, "\n")

        # BPB annotation for boot sector
        if is_boot:
            self.hex_text.insert(tk.END, "\n", '')
            self.hex_text.insert(tk.END, "─── BPB Fields ─────────────────────\n", 'header')
            for off, (name, fmt) in sorted(BPB_FIELDS.items()):
                try:
                    if fmt == 'B':
                        val = data[off]
                        val_str = f"{val} (0x{val:02X})"
                    else:
                        val = struct.unpack_from(fmt, data, off)[0]
                        val_str = f"{val} (0x{val:04X})"
                    line = f"  0x{off:04X}  {name:<18s} = {val_str}\n"
                    self.hex_text.insert(tk.END, line, 'bpb_field')
                except (struct.error, IndexError):
                    pass

            # Detect signatures
            sigs = self._find_signatures(data)
            if sigs:
                self.hex_text.insert(tk.END, "\n─── Signatures ─────────────────────\n", 'header')
                for off, label in sigs:
                    self.hex_text.insert(tk.END,
                        f"  0x{off:04X}  {label}\n", 'signature')

        # Update annotation bar
        if sector_num in self.bookmarks:
            annot_parts.append(f"Bookmark: {self.bookmarks[sector_num]}")
        self.annot_var.set("  │  ".join(annot_parts) if annot_parts else
                           f"Sector {sector_num} — {sector_size} bytes"
                           f" — Abs offset 0x{abs_offset_base:X}")

        self.hex_text.configure(state=tk.DISABLED)

    def _find_signatures(self, data):
        """Scan sector data for known byte patterns."""
        results = []
        for pattern, label in BYTE_PATTERNS.items():
            idx = 0
            while True:
                pos = data.find(pattern, idx)
                if pos == -1:
                    break
                results.append((pos, label))
                idx = pos + 1
        results.sort(key=lambda x: x[0])
        return results

    # -- Search --

    def _search_hex(self):
        """Search for a hex pattern across the entire image."""
        query = self.search_var.get().strip()
        if not query or not self.disk:
            return

        try:
            # Accept formats: "CD 21", "CD21", "0xCD 0x21"
            cleaned = query.replace('0x', '').replace('0X', '').replace(',', ' ')
            hex_bytes = bytes.fromhex(cleaned.replace(' ', ''))
        except ValueError:
            # Try as ASCII string
            hex_bytes = query.encode('ascii', errors='replace')

        self._search_bytes = hex_bytes
        self._search_pos = self.current_sector * self.disk.sector_size
        self._do_search()

    def _search_next(self):
        if self._search_bytes and self.disk:
            self._search_pos += 1
            self._do_search()

    def _do_search(self):
        if not self._search_bytes or not self.disk:
            return

        pattern = self._search_bytes
        sector_size = self.disk.sector_size
        total = self.disk.total_sectors

        # Search sector by sector from current position
        start_sector = self._search_pos // sector_size
        start_offset = self._search_pos % sector_size

        for s in range(start_sector, total):
            data = self.disk.read_sector(s)
            search_start = start_offset if s == start_sector else 0
            pos = data.find(pattern, search_start)
            if pos != -1:
                self._search_pos = s * sector_size + pos
                self._show_sector(s)
                self._highlight_search(pos, len(pattern))
                self.annot_var.set(
                    f"Found at sector {s}, offset 0x{pos:X} "
                    f"(absolute 0x{s * sector_size + pos:X})"
                )
                return

        messagebox.showinfo("Not Found", "Pattern not found (searched to end of image).")

    def _highlight_search(self, byte_offset, length):
        """Highlight the found bytes in the hex view."""
        self.hex_text.configure(state=tk.NORMAL)
        self.hex_text.tag_remove('search_hit', '1.0', tk.END)

        row = byte_offset // self.BYTES_PER_ROW
        col = byte_offset % self.BYTES_PER_ROW
        text_line = row + 3  # +1 for header, +1 for separator, +1 for 1-indexed

        for i in range(length):
            byte_col = col + i
            byte_row = row
            if byte_col >= self.BYTES_PER_ROW:
                byte_row += byte_col // self.BYTES_PER_ROW
                byte_col = byte_col % self.BYTES_PER_ROW
            line = byte_row + 3
            # Each hex byte is "XX " = 3 chars, offset column is 12 chars
            char_start = 12 + byte_col * 3
            start = f"{line}.{char_start}"
            end = f"{line}.{char_start + 2}"
            self.hex_text.tag_add('search_hit', start, end)

        # Scroll to the found line
        self.hex_text.see(f"{text_line}.0")
        self.hex_text.configure(state=tk.DISABLED)

    # -- Bookmarks --

    def _add_bookmark(self):
        if not self.disk:
            return
        label = f"Sector {self.current_sector}"
        # Simple dialog
        dlg = tk.Toplevel(self)
        dlg.title("Add Bookmark")
        dlg.geometry("300x100")
        dlg.transient(self)

        ttk.Label(dlg, text=f"Label for sector {self.current_sector}:").pack(pady=(10, 2))
        var = tk.StringVar(value=label)
        entry = ttk.Entry(dlg, textvariable=var, width=30)
        entry.pack(pady=2)
        entry.select_range(0, tk.END)
        entry.focus()

        def save():
            self.bookmarks[self.current_sector] = var.get()
            self._refresh()
            dlg.destroy()

        entry.bind('<Return>', lambda e: save())
        ttk.Button(dlg, text="Save", command=save).pack(pady=8)

    def _show_bookmarks(self):
        if not self.bookmarks:
            messagebox.showinfo("Bookmarks", "No bookmarks yet.\nUse the Bookmark button to mark sectors.")
            return

        dlg = tk.Toplevel(self)
        dlg.title("Bookmarks")
        dlg.geometry("350x300")
        dlg.transient(self)

        listbox = tk.Listbox(dlg)
        listbox.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        sorted_bm = sorted(self.bookmarks.items())
        for sector, label in sorted_bm:
            listbox.insert(tk.END, f"Sector {sector:>6d}: {label}")

        def go_to():
            sel = listbox.curselection()
            if sel:
                sector = sorted_bm[sel[0]][0]
                self._show_sector(sector)
                dlg.destroy()

        listbox.bind('<Double-1>', lambda e: go_to())
        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(fill=tk.X, padx=5, pady=5)
        ttk.Button(btn_frame, text="Go To", command=go_to).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="Close", command=dlg.destroy).pack(side=tk.RIGHT)

    # -- Export --

    def _export_range(self):
        if not self.disk:
            return

        try:
            from_s = int(self.export_from_var.get())
            to_s = int(self.export_to_var.get())
        except ValueError:
            messagebox.showwarning("Invalid Range", "Enter valid sector numbers.")
            return

        if from_s < 0 or to_s >= self.disk.total_sectors or from_s > to_s:
            messagebox.showwarning("Invalid Range",
                f"Sectors must be 0–{self.disk.total_sectors - 1}, from ≤ to.")
            return

        path = filedialog.asksaveasfilename(
            title="Export Sector Range",
            initialfile=f"sectors_{from_s}-{to_s}.bin",
            defaultextension=".bin",
            filetypes=[("Binary", "*.bin"), ("All Files", "*.*")],
        )
        if not path:
            return

        data = self.disk.read_sectors(from_s, to_s - from_s + 1)
        with open(path, 'wb') as f:
            f.write(data)

        size = len(data)
        messagebox.showinfo("Exported",
            f"Exported sectors {from_s}–{to_s} ({size:,} bytes) to:\n{path}")
