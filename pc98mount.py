"""
PC-98 Disk Image Mounter — Main GUI Application

Mount PC-98 floppy/HDD disk images as native Windows drive letters.
Zero external dependencies — uses Windows `subst` for mounting.

Three mount modes:
  - FAT:     files and folders from the FAT12/16 filesystem
  - Flat:    entire raw image as a single DISK.IMG file
  - Sectors: each sector as an individual SECTOR_NNNN.BIN file

Also includes a built-in hex viewer for sector-level inspection.

Usage:
    python pc98mount.py
    python pc98mount.py image.hdm
    python pc98mount.py image.d88 -d P --mode flat
"""

import sys
import os
import ctypes
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
import logging

from disk_image import open_image
from fat_fs import FATFilesystem
from hex_viewer import HexViewerWidget
from mount_backend import MountManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("pc98mount")

IMAGE_EXTENSIONS = [
    ("PC-98 Disk Images", "*.d88 *.d68 *.d77 *.hdm *.tfd *.fdi *.hdi *.img *.ima"),
    ("D88 Images", "*.d88 *.d68 *.d77"),
    ("HDM Images", "*.hdm *.tfd"),
    ("FDI Images", "*.fdi"),
    ("HDI Images", "*.hdi"),
    ("Raw Images", "*.img *.ima"),
    ("All Files", "*.*"),
]


def _drive_letter_in_use(letter):
    """Check drive letter availability using QueryDosDeviceW on Windows."""
    if sys.platform != 'win32':
        return False
    try:
        buf = ctypes.create_unicode_buffer(1024)
        res = ctypes.windll.kernel32.QueryDosDeviceW(f"{letter}:", buf, len(buf))
        if res != 0:
            return True
        # ERROR_FILE_NOT_FOUND (2) means no mapping exists.
        err = ctypes.windll.kernel32.GetLastError()
        return err != 2
    except Exception:
        return False


def get_available_drive_letters():
    """Return a list of unused drive letters on Windows."""
    if sys.platform != 'win32':
        return [chr(c) for c in range(ord('P'), ord('Z') + 1)]
    try:
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        bitmask = 0
    used = set()
    for i in range(26):
        if bitmask & (1 << i):
            used.add(chr(ord('A') + i))
    # Also exclude letters reserved by network drives or other mappings.
    for i in range(26):
        letter = chr(ord('A') + i)
        if _drive_letter_in_use(letter):
            used.add(letter)
    preferred = [chr(c) for c in range(ord('P'), ord('Z') + 1)]
    others = [chr(c) for c in range(ord('D'), ord('P'))]
    return [l for l in preferred + others if l not in used]


class ImageInfo:
    """Holds state for a loaded disk image."""
    def __init__(self, path, disk, fs, label):
        self.path = path
        self.disk = disk
        self.fs = fs            # None if FAT parse failed
        self.label = label
        self.drive_letter = None
        self.mount_mode = None  # "fat", "flat", "sectors"


class PC98MountApp:
    """Main application window."""

    def __init__(self, root):
        self.root = root
        self.root.title("PC-98 Disk Image Mounter")
        self.root.geometry("900x620")
        self.root.minsize(700, 480)

        self.images = []
        self.mount_mgr = MountManager()
        self._setup_styles()
        self._build_ui()
        self._update_drive_letters()

    def _setup_styles(self):
        style = ttk.Style()
        if sys.platform == 'win32':
            try:
                style.theme_use('vista')
            except tk.TclError:
                style.theme_use('clam')
        else:
            style.theme_use('clam')

    # ── UI Construction ──────────────────────────────────────────────

    def _build_ui(self):
        # Toolbar
        toolbar = ttk.Frame(self.root, padding=5)
        toolbar.pack(fill=tk.X)

        ttk.Button(toolbar, text="Add Image\u2026", command=self._add_image).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Remove", command=self._remove_image).pack(side=tk.LEFT, padx=2)

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        ttk.Label(toolbar, text="Drive:").pack(side=tk.LEFT, padx=(0, 2))
        self.drive_var = tk.StringVar()
        self.drive_combo = ttk.Combobox(
            toolbar, textvariable=self.drive_var, width=4, state='readonly'
        )
        self.drive_combo.pack(side=tk.LEFT, padx=2)

        ttk.Label(toolbar, text="Mode:").pack(side=tk.LEFT, padx=(8, 2))
        self.mode_var = tk.StringVar(value="fat")
        ttk.Combobox(
            toolbar, textvariable=self.mode_var, width=10, state='readonly',
            values=["fat", "flat", "sectors"]
        ).pack(side=tk.LEFT, padx=2)

        ttk.Button(toolbar, text="Mount", command=self._mount_image).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Unmount", command=self._unmount_image).pack(side=tk.LEFT, padx=2)

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        ttk.Button(toolbar, text="Open in Explorer", command=self._open_explorer).pack(side=tk.LEFT, padx=2)

        # Main paned layout
        main_paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main_paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Left: image list
        left_frame = ttk.LabelFrame(main_paned, text="Disk Images", padding=5)
        main_paned.add(left_frame, weight=1)

        self.image_list = tk.Listbox(left_frame, activestyle='dotbox', width=22)
        self.image_list.pack(fill=tk.BOTH, expand=True)
        self.image_list.bind('<<ListboxSelect>>', self._on_image_select)

        # Right: tabs
        right_frame = ttk.Frame(main_paned)
        main_paned.add(right_frame, weight=4)

        self.notebook = ttk.Notebook(right_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self._build_files_tab()
        self._build_hex_tab()
        self._build_info_tab()

        # Status bar
        self.status_var = tk.StringVar(value="Ready. Add a PC-98 disk image to begin.")
        ttk.Label(
            self.root, textvariable=self.status_var,
            relief=tk.SUNKEN, anchor=tk.W, padding=3
        ).pack(fill=tk.X, side=tk.BOTTOM)

    def _build_files_tab(self):
        tab = ttk.Frame(self.notebook, padding=4)
        self.notebook.add(tab, text="  Files  ")

        self.info_var = tk.StringVar(value="No image loaded")
        ttk.Label(tab, textvariable=self.info_var, anchor=tk.W).pack(fill=tk.X)

        btn_bar = ttk.Frame(tab)
        btn_bar.pack(fill=tk.X, pady=(4, 2))
        ttk.Button(btn_bar, text="Extract File\u2026", command=self._extract_file).pack(side=tk.LEFT)
        ttk.Button(btn_bar, text="Extract All\u2026", command=self._extract_all).pack(side=tk.LEFT, padx=4)

        tree_frame = ttk.Frame(tab)
        tree_frame.pack(fill=tk.BOTH, expand=True, pady=(2, 0))

        cols = ("size", "date", "attr")
        self.file_tree = ttk.Treeview(tree_frame, columns=cols, selectmode='browse')
        self.file_tree.heading('#0', text='Name', anchor=tk.W)
        self.file_tree.heading('size', text='Size', anchor=tk.E)
        self.file_tree.heading('date', text='Modified', anchor=tk.W)
        self.file_tree.heading('attr', text='Attr', anchor=tk.CENTER)
        self.file_tree.column('#0', width=250, minwidth=120)
        self.file_tree.column('size', width=80, minwidth=60, anchor=tk.E)
        self.file_tree.column('date', width=140, minwidth=100)
        self.file_tree.column('attr', width=50, minwidth=40, anchor=tk.CENTER)

        sb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.file_tree.yview)
        self.file_tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.file_tree.pack(fill=tk.BOTH, expand=True)

        self._tree_paths = {}  # item_id -> (ImageInfo, path, FileEntry)

    def _build_hex_tab(self):
        tab = ttk.Frame(self.notebook, padding=4)
        self.notebook.add(tab, text="  Hex Viewer  ")
        self.hex_viewer = HexViewerWidget(tab)
        self.hex_viewer.pack(fill=tk.BOTH, expand=True)

    def _build_info_tab(self):
        tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab, text="  Image Info  ")
        self.detail_text = tk.Text(
            tab, font=("Consolas", 10), wrap=tk.WORD,
            state=tk.DISABLED, padx=8, pady=8
        )
        self.detail_text.pack(fill=tk.BOTH, expand=True)

    # ── Helpers ──────────────────────────────────────────────────────

    def _update_drive_letters(self):
        available = get_available_drive_letters()
        self.drive_combo['values'] = [f"{l}:" for l in available]
        if available:
            self.drive_var.set(f"{available[0]}:")

    def _set_status(self, msg):
        self.status_var.set(msg)
        self.root.update_idletasks()

    def _selected_image(self):
        sel = self.image_list.curselection()
        return self.images[sel[0]] if sel else None

    @staticmethod
    def _format_size(size):
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / (1024 * 1024):.1f} MB"

    # ── Image management ─────────────────────────────────────────────

    def _add_image(self):
        paths = filedialog.askopenfilenames(
            title="Select PC-98 Disk Image(s)",
            filetypes=IMAGE_EXTENSIONS,
        )
        for path in paths:
            self._load_image(path)

    def _load_image(self, path):
        self._set_status(f"Loading {Path(path).name}\u2026")
        try:
            disk = open_image(path)

            fs = None
            try:
                fs = FATFilesystem(disk)
            except Exception as e:
                log.info(f"No FAT filesystem: {e}")

            label = (fs.volume_label if fs and fs.volume_label else None) \
                    or disk.label or Path(path).stem

            info = ImageInfo(path, disk, fs, label)
            self.images.append(info)

            self.image_list.insert(tk.END, Path(path).name)
            self.image_list.selection_clear(0, tk.END)
            self.image_list.selection_set(tk.END)
            self._on_image_select(None)

            total_kb = disk.total_sectors * disk.sector_size // 1024
            fat_str = f"FAT{fs.fat_type}" if fs else "No FAT"
            file_count = sum(1 for _, e in fs.walk() if not e.is_directory) if fs else 0
            status = (
                f"Loaded {Path(path).name}: {fat_str}, "
                f"{disk.total_sectors} sectors \u00d7 {disk.sector_size}B = {total_kb} KB"
            )
            if fs:
                if file_count > 0:
                    status += f", {file_count} files found"
                else:
                    status += " \u2014 WARNING: FAT parsed but 0 files found!"
                    log.warning(
                        f"Image loaded with FAT{fs.fat_type} but root has "
                        f"{len(fs.root.children)} entries. This BPB may be invalid."
                    )
            else:
                status += " (raw access only)"
            self._set_status(status)
        except Exception as e:
            messagebox.showerror("Error", f"Could not load:\n{path}\n\n{e}")
            self._set_status(f"Error: {e}")

    def _remove_image(self):
        sel = self.image_list.curselection()
        if not sel:
            return
        idx = sel[0]
        info = self.images[idx]

        if info.drive_letter and self.mount_mgr.is_mounted(info.drive_letter):
            self.mount_mgr.unmount(info.drive_letter)

        self.images.pop(idx)
        self.image_list.delete(idx)
        self._clear_tree()
        self._set_status("Image removed.")

    # ── Selection / display ──────────────────────────────────────────

    def _on_image_select(self, event):
        info = self._selected_image()
        if not info:
            return
        self._populate_tree(info)
        self.hex_viewer.set_disk(info.disk)
        self._populate_detail(info)

    def _clear_tree(self):
        self.file_tree.delete(*self.file_tree.get_children())
        self._tree_paths.clear()
        self.info_var.set("No image loaded")

    def _populate_tree(self, info):
        self._clear_tree()

        if info.fs is None:
            self.info_var.set(
                f"{info.label} \u2014 No FAT filesystem. "
                f"Use Hex Viewer or mount in flat/sector mode."
            )
            return

        mounted = ""
        if info.drive_letter and self.mount_mgr.is_mounted(info.drive_letter):
            mounted = f"  [mounted at {info.drive_letter}:]"

        total_kb = info.disk.total_sectors * info.disk.sector_size // 1024
        self.info_var.set(
            f"{info.label} \u2014 FAT{info.fs.fat_type}, "
            f"{info.disk.sector_size}B sectors, {total_kb} KB{mounted}"
        )
        self._add_dir_entries(info, info.fs.root, '', '')

    def _add_dir_entries(self, info, dir_entry, parent_item, path_prefix):
        entries = sorted(
            dir_entry.children.values(),
            key=lambda e: (not e.is_directory, e.display_name.upper())
        )
        for entry in entries:
            if entry.name in ('.', '..'):
                continue
            name = entry.display_name
            full_path = f"{path_prefix}/{name}"

            size_str = "<DIR>" if entry.is_directory else self._format_size(entry.size)
            date_str = entry.datetime.strftime("%Y-%m-%d %H:%M")

            attr_parts = []
            if entry.attr & 0x01: attr_parts.append('R')
            if entry.attr & 0x02: attr_parts.append('H')
            if entry.attr & 0x04: attr_parts.append('S')
            if entry.attr & 0x20: attr_parts.append('A')

            icon = "\U0001F4C1 " if entry.is_directory else "\U0001F4C4 "
            item_id = self.file_tree.insert(
                parent_item, tk.END,
                text=f"{icon}{name}",
                values=(size_str, date_str, ''.join(attr_parts)),
                open=False,
            )
            self._tree_paths[item_id] = (info, full_path, entry)

            if entry.is_directory:
                self._add_dir_entries(info, entry, item_id, full_path)

    def _populate_detail(self, info):
        self.detail_text.configure(state=tk.NORMAL)
        self.detail_text.delete('1.0', tk.END)

        lines = [
            f"File:            {info.path}",
            f"File Size:       {os.path.getsize(info.path):,} bytes",
            f"Image Type:      {info.disk.__class__.__name__}",
            f"Label:           {info.label}",
            f"Sector Size:     {info.disk.sector_size} bytes",
            f"Total Sectors:   {info.disk.total_sectors}",
            f"Total Size:      {info.disk.total_sectors * info.disk.sector_size:,} bytes",
            "",
        ]

        if info.fs:
            fs = info.fs
            lines += [
                "\u2500\u2500 FAT Filesystem \u2500" * 3,
                f"FAT Type:        FAT{fs.fat_type}",
                f"Bytes/Sector:    {fs.bytes_per_sector}",
                f"Sects/Cluster:   {fs.sectors_per_cluster}",
                f"Reserved Sects:  {fs.reserved_sectors}",
                f"Number of FATs:  {fs.num_fats}",
                f"FAT Size:        {fs.fat_size_16} sectors",
                f"Root Entries:    {fs.root_entry_count}",
                f"Media Desc:      0x{fs.media_descriptor:02X}",
                f"Total Clusters:  {fs.total_clusters}",
                f"Data Start:      sector {fs.first_data_sector}",
                "",
            ]
            file_count = sum(1 for _, e in fs.walk() if not e.is_directory)
            dir_count = sum(1 for _, e in fs.walk() if e.is_directory)
            total_size = sum(e.size for _, e in fs.walk() if not e.is_directory)
            lines += [
                f"Files:           {file_count}",
                f"Directories:     {dir_count}",
                f"Data Used:       {total_size:,} bytes",
            ]
        else:
            lines += [
                "No FAT filesystem detected.",
                "Use Hex Viewer or mount in flat/sector mode.",
            ]

        lines += ["", "\u2500\u2500 Mount Status \u2500" * 3]
        if info.drive_letter and self.mount_mgr.is_mounted(info.drive_letter):
            lines.append(f"Mounted:         {info.mount_mode} at {info.drive_letter}:")
        else:
            lines.append("Mounted:         No")

        lines += ["", "\u2500\u2500 Mount Strategy \u2500" * 3]
        lines.append(self.mount_mgr.get_strategy_info())

        self.detail_text.insert('1.0', '\n'.join(lines))
        self.detail_text.configure(state=tk.DISABLED)

    # ── Mount / Unmount ──────────────────────────────────────────────

    def _mount_image(self):
        info = self._selected_image()
        if not info:
            messagebox.showinfo("No Image", "Select a disk image first.")
            return

        if info.drive_letter and self.mount_mgr.is_mounted(info.drive_letter):
            messagebox.showinfo("Mounted", f"Already mounted at {info.drive_letter}:")
            return

        drive = self.drive_var.get().rstrip(':')
        if not drive:
            messagebox.showinfo("No Drive", "Select a drive letter.")
            return

        mode = self.mode_var.get()

        if mode == "fat" and info.fs is None:
            messagebox.showwarning(
                "No FAT",
                "No FAT filesystem on this image.\n"
                "Use 'flat' or 'sectors' mode for raw access."
            )
            return

        self._set_status(f"Mounting {Path(info.path).name} at {drive}: ({mode})\u2026")
        try:
            image_size = info.disk.total_sectors * info.disk.sector_size
            self.mount_mgr.mount(
                drive, mode,
                disk_image=info.disk,
                fat_fs=info.fs,
                image_size_bytes=image_size,
            )
            info.drive_letter = drive
            info.mount_mode = mode

            # Update listbox display
            sel = self.image_list.curselection()
            if sel:
                idx = sel[0]
                self.image_list.delete(idx)
                self.image_list.insert(idx, f"[{drive}:] {Path(info.path).name}")
                self.image_list.selection_set(idx)

            self._populate_tree(info)
            self._populate_detail(info)
            self._update_drive_letters()

            mode_desc = {
                "fat": "FAT filesystem (files/folders)",
                "flat": "raw flat file (DISK.IMG)",
                "sectors": f"individual sectors ({info.disk.total_sectors} files)",
            }
            strategy = self.mount_mgr.strategy or ""
            via = f" via {strategy.upper()}" if strategy else ""
            self._set_status(f"Mounted at {drive}:{via} \u2014 {mode_desc.get(mode, mode)}")

            # Warn if FAT extraction found nothing
            if mode == "fat":
                mount_obj = self.mount_mgr.get_mount(drive)
                if mount_obj and hasattr(mount_obj, '_extract_count'):
                    count = mount_obj._extract_count
                    errors = mount_obj._extract_errors
                    if count == 0:
                        messagebox.showwarning(
                            "No Files Extracted",
                            f"The FAT parser found 0 files on this image.\n"
                            f"({errors} extraction errors)\n\n"
                            f"This disk likely has no standard FAT filesystem.\n"
                            f"Try mounting in 'flat' or 'sectors' mode instead,\n"
                            f"or use the Hex Viewer to inspect raw sectors.\n\n"
                            f"Check the console log for diagnostic details."
                        )
                    else:
                        self._set_status(
                            f"Mounted at {drive}:{via} \u2014 {count} files extracted"
                            + (f" ({errors} errors)" if errors else "")
                        )

        except Exception as e:
            messagebox.showerror("Mount Error", f"Failed to mount:\n\n{e}")
            self._set_status(f"Mount failed: {e}")

    def _unmount_image(self):
        info = self._selected_image()
        if not info:
            return

        if not info.drive_letter or not self.mount_mgr.is_mounted(info.drive_letter):
            messagebox.showinfo("Not Mounted", "This image is not mounted.")
            return

        letter = info.drive_letter
        self.mount_mgr.unmount(letter)
        info.drive_letter = None
        info.mount_mode = None

        sel = self.image_list.curselection()
        if sel:
            idx = sel[0]
            self.image_list.delete(idx)
            self.image_list.insert(idx, Path(info.path).name)
            self.image_list.selection_set(idx)

        self._populate_tree(info)
        self._populate_detail(info)
        self._update_drive_letters()
        self._set_status(f"Unmounted {letter}:")

    def _open_explorer(self):
        info = self._selected_image()
        if not info or not info.drive_letter:
            messagebox.showinfo("Not Mounted", "Mount an image first.")
            return
        if sys.platform == 'win32':
            os.startfile(f"{info.drive_letter}:\\")
        else:
            self._set_status("Explorer only available on Windows")

    # ── File extraction ──────────────────────────────────────────────

    def _extract_file(self):
        sel = self.file_tree.selection()
        if not sel or sel[0] not in self._tree_paths:
            messagebox.showinfo("No Selection", "Select a file in the tree.")
            return

        info, path, entry = self._tree_paths[sel[0]]

        if entry.is_directory:
            dest = filedialog.askdirectory(title=f"Extract '{entry.display_name}' to\u2026")
            if dest:
                self._extract_dir(info, entry, dest, entry.display_name)
        else:
            dest = filedialog.asksaveasfilename(
                title="Extract File", initialfile=entry.display_name,
            )
            if dest:
                try:
                    data = info.fs.read_file(entry)
                    with open(dest, 'wb') as f:
                        f.write(data)
                    self._set_status(f"Extracted {entry.display_name} ({len(data)} bytes)")
                except Exception as e:
                    messagebox.showerror("Error", f"Extract failed:\n{e}")

    def _extract_all(self):
        info = self._selected_image()
        if not info or not info.fs:
            messagebox.showinfo("No FAT", "No filesystem to extract.")
            return

        dest = filedialog.askdirectory(title="Extract all files to\u2026")
        if not dest:
            return

        count = 0
        for fpath, entry in info.fs.walk():
            full = os.path.join(dest, fpath.lstrip('/'))
            if entry.is_directory:
                os.makedirs(full, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(full), exist_ok=True)
                try:
                    with open(full, 'wb') as f:
                        f.write(info.fs.read_file(entry))
                    count += 1
                except Exception as e:
                    log.warning(f"Failed: {fpath}: {e}")

        self._set_status(f"Extracted {count} files to {dest}")
        messagebox.showinfo("Done", f"Extracted {count} files to:\n{dest}")

    def _extract_dir(self, info, dir_entry, dest_base, dir_name):
        dest = os.path.join(dest_base, dir_name)
        os.makedirs(dest, exist_ok=True)
        count = 0
        for name, entry in dir_entry.children.items():
            if entry.name in ('.', '..'):
                continue
            if entry.is_directory:
                self._extract_dir(info, entry, dest, entry.display_name)
            else:
                try:
                    with open(os.path.join(dest, entry.display_name), 'wb') as f:
                        f.write(info.fs.read_file(entry))
                    count += 1
                except Exception as e:
                    log.warning(f"Failed: {entry.display_name}: {e}")
        self._set_status(f"Extracted {count} files to {dest}")

    # ── Cleanup ──────────────────────────────────────────────────────

    def on_close(self):
        self.mount_mgr.unmount_all()
        self.root.destroy()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="PC-98 Disk Image Mounter")
    parser.add_argument('images', nargs='*', help='Disk image files to open')
    parser.add_argument('-d', '--drive', help='Drive letter (e.g., P)')
    parser.add_argument('-m', '--mode', choices=['fat', 'flat', 'sectors'],
                        default='fat', help='Mount mode')
    args = parser.parse_args()

    root = tk.Tk()
    app = PC98MountApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)

    for path in args.images:
        if os.path.isfile(path):
            app._load_image(os.path.abspath(path))

    if args.drive and len(app.images) == 1:
        app.drive_var.set(f"{args.drive.upper()}:")
        app.mode_var.set(args.mode)
        app.root.after(200, app._mount_image)

    root.mainloop()


if __name__ == '__main__':
    main()
