#!/usr/bin/env python3
"""
End-to-end test for the PC-98 disk image toolchain.

Programmatically builds a valid D88 disk image with a FAT12 filesystem,
round-trips it through D88Image / FATFilesystem, performs modifications
via write_back_from_directory, and verifies everything.

Run:  python test_d88_writeback.py
"""

import struct
import os
import sys
import shutil
import tempfile
import hashlib

# ---------------------------------------------------------------------------
# Make sure the modules under test are importable.
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from disk_image import D88Image
from fat_fs import FATFilesystem

# ========================== D88 / FAT12 BUILDER ============================

# Geometry: PC-98 2HD 1.2 MB
CYLS        = 77
HEADS       = 2
SPT         = 8       # sectors per track
SECTOR_SIZE = 1024    # bytes
N_VALUE     = 3       # 128 << 3 = 1024
TOTAL_SECTORS = CYLS * HEADS * SPT   # 1232
NUM_TRACKS    = CYLS * HEADS          # 154

# D88 header sizes
D88_HEADER_SIZE   = 0x2B0             # 688 bytes
SECTOR_HDR_SIZE   = 16
D88_TRACK_ENTRIES = 164

# FAT12 BPB parameters
BPS          = 1024
SPC          = 1
RESERVED     = 1
NFATS        = 2
ROOT_ENTRIES = 192
FAT_SIZE     = 2       # sectors per FAT copy
MEDIA        = 0xFE

# Derived layout
FIRST_FAT_SECTOR   = RESERVED                                  # 1
FIRST_ROOT_SECTOR  = FIRST_FAT_SECTOR + NFATS * FAT_SIZE       # 5
ROOT_DIR_SECTORS   = (ROOT_ENTRIES * 32 + BPS - 1) // BPS      # 6
FIRST_DATA_SECTOR  = FIRST_ROOT_SECTOR + ROOT_DIR_SECTORS       # 11
DATA_CLUSTERS      = TOTAL_SECTORS - FIRST_DATA_SECTOR          # 1221


def _build_fat12_bytes(entries, total_bytes):
    """Serialise a list of 12-bit FAT entries into *total_bytes* of data."""
    buf = bytearray(total_bytes)
    for i, val in enumerate(entries):
        offset = i + (i // 2)
        if offset + 1 >= len(buf):
            break
        word = struct.unpack_from('<H', buf, offset)[0]
        if i & 1:
            word = (word & 0x000F) | ((val & 0x0FFF) << 4)
        else:
            word = (word & 0xF000) | (val & 0x0FFF)
        struct.pack_into('<H', buf, offset, word)
    return bytes(buf)


def _make_dir_entry(name8, ext3, attr, cluster, size):
    """Build one 32-byte FAT directory entry (date/time zeroed)."""
    e = bytearray(32)
    e[0:8] = name8.ljust(8)[:8]
    e[8:11] = ext3.ljust(3)[:3]
    e[11] = attr
    # time = 0, date = 0x0021 (1980-01-01)
    struct.pack_into('<H', e, 22, 0)
    struct.pack_into('<H', e, 24, 0x0021)
    struct.pack_into('<H', e, 26, cluster)
    struct.pack_into('<I', e, 28, size)
    return bytes(e)


def _make_boot_sector():
    """Create a 1024-byte boot sector with a valid FAT12 BPB."""
    boot = bytearray(BPS)
    boot[0:3] = b'\xEB\x3C\x90'                           # JMP short
    boot[3:11] = b'PC98TEST'                                # OEM name
    struct.pack_into('<H', boot, 0x0B, BPS)                 # bytes/sector
    boot[0x0D] = SPC                                        # sectors/cluster
    struct.pack_into('<H', boot, 0x0E, RESERVED)            # reserved sectors
    boot[0x10] = NFATS                                      # number of FATs
    struct.pack_into('<H', boot, 0x11, ROOT_ENTRIES)        # root entries
    struct.pack_into('<H', boot, 0x13, TOTAL_SECTORS)       # total sectors 16
    boot[0x15] = MEDIA                                      # media descriptor
    struct.pack_into('<H', boot, 0x16, FAT_SIZE)            # FAT size 16
    struct.pack_into('<H', boot, 0x18, SPT)                 # sectors/track
    struct.pack_into('<H', boot, 0x1A, HEADS)               # heads
    struct.pack_into('<H', boot, 0x1C, 0)                   # hidden sectors
    # Volume label at 0x2B (11 bytes)
    boot[0x2B:0x36] = b'TESTDISK   '
    boot[BPS - 2:BPS] = b'\x55\xAA'                        # signature
    return bytes(boot)


# Test file contents
HELLO_CONTENT = b"Hello, PC-98!\r\nThis is a test file.\r\n"
DATA_CONTENT  = bytes(range(256)) * 4   # exactly 1024 bytes


def build_d88_image():
    """Return the raw bytes of a complete D88 disk image containing a
    FAT12 filesystem with two test files: HELLO.TXT and DATA.BIN."""

    # --- Step 1: create flat sector data (1232 × 1024 bytes) -------------
    flat = bytearray(TOTAL_SECTORS * SECTOR_SIZE)

    # Sector 0: boot sector / BPB
    boot = _make_boot_sector()
    flat[0:BPS] = boot

    # Sectors 1-2: FAT #1,  Sectors 3-4: FAT #2
    #   Cluster 0 = media marker, Cluster 1 = 0xFFF,
    #   Cluster 2 = HELLO.TXT (1 cluster, EOC), Cluster 3 = DATA.BIN (1 cluster, EOC)
    fat_entries = [0] * (DATA_CLUSTERS + 2)
    fat_entries[0] = 0xF00 | MEDIA        # 0xFFE
    fat_entries[1] = 0xFFF
    fat_entries[2] = 0xFFF                # EOC for HELLO.TXT
    fat_entries[3] = 0xFFF                # EOC for DATA.BIN
    fat_bytes = _build_fat12_bytes(fat_entries, FAT_SIZE * BPS)
    # Write both FAT copies
    flat[FIRST_FAT_SECTOR * BPS : FIRST_FAT_SECTOR * BPS + len(fat_bytes)] = fat_bytes
    fat2_start = (FIRST_FAT_SECTOR + FAT_SIZE) * BPS
    flat[fat2_start : fat2_start + len(fat_bytes)] = fat_bytes

    # Root directory (starts at sector 5)
    root_offset = FIRST_ROOT_SECTOR * BPS
    # Volume label entry
    vol_entry = _make_dir_entry(b'TESTDISK', b'   ', 0x08, 0, 0)
    # HELLO.TXT → cluster 2
    hello_entry = _make_dir_entry(b'HELLO   ', b'TXT', 0x20, 2, len(HELLO_CONTENT))
    # DATA.BIN → cluster 3
    data_entry = _make_dir_entry(b'DATA    ', b'BIN', 0x20, 3, len(DATA_CONTENT))

    flat[root_offset:root_offset + 32] = vol_entry
    flat[root_offset + 32:root_offset + 64] = hello_entry
    flat[root_offset + 64:root_offset + 96] = data_entry

    # Data area: cluster 2 → sector 11, cluster 3 → sector 12
    cluster2_off = FIRST_DATA_SECTOR * BPS
    flat[cluster2_off:cluster2_off + len(HELLO_CONTENT)] = HELLO_CONTENT
    cluster3_off = (FIRST_DATA_SECTOR + 1) * BPS
    flat[cluster3_off:cluster3_off + len(DATA_CONTENT)] = DATA_CONTENT

    # --- Step 2: wrap the flat sectors in D88 container ------------------
    d88 = bytearray()

    # D88 header (0x2B0 bytes)
    header = bytearray(D88_HEADER_SIZE)
    # Disk name (null-terminated, up to 17 bytes)
    name = b'TestDisk\x00'
    header[0:len(name)] = name
    # Write-protect: 0 = not protected
    header[0x1A] = 0x00
    # Media type: 0x00 = 2HD
    header[0x1B] = 0x00

    # We'll fill in total disk size after building tracks.
    # Track offsets: 164 uint32 entries starting at 0x20
    # We have 154 used tracks (77 cyl × 2 heads), rest are 0.
    track_data_size = SPT * (SECTOR_HDR_SIZE + SECTOR_SIZE)  # 8 × 1040 = 8320
    for t in range(D88_TRACK_ENTRIES):
        if t < NUM_TRACKS:
            offset = D88_HEADER_SIZE + t * track_data_size
            struct.pack_into('<I', header, 0x20 + t * 4, offset)
        else:
            struct.pack_into('<I', header, 0x20 + t * 4, 0)

    # Total disk size
    total_d88_size = D88_HEADER_SIZE + NUM_TRACKS * track_data_size
    struct.pack_into('<I', header, 0x1C, total_d88_size)

    d88.extend(header)

    # Build each track's sector headers + data
    sector_lba = 0
    for t in range(NUM_TRACKS):
        cyl = t // HEADS
        head = t % HEADS
        for s in range(SPT):
            # 16-byte sector header
            sec_hdr = bytearray(SECTOR_HDR_SIZE)
            sec_hdr[0] = cyl          # C
            sec_hdr[1] = head         # H
            sec_hdr[2] = s + 1        # R (1-based sector number)
            sec_hdr[3] = N_VALUE      # N (size code: 128 << N)
            struct.pack_into('<H', sec_hdr, 4, SPT)   # num_sectors in track
            sec_hdr[6] = 0x00         # density (0 = double density)
            sec_hdr[7] = 0x00         # deleted mark
            sec_hdr[8] = 0x00         # status
            # bytes 9-13: reserved (zero)
            struct.pack_into('<H', sec_hdr, 14, SECTOR_SIZE)  # data_size
            d88.extend(sec_hdr)

            # Sector data from flat image
            d88.extend(flat[sector_lba * SECTOR_SIZE:(sector_lba + 1) * SECTOR_SIZE])
            sector_lba += 1

    assert len(d88) == total_d88_size, (
        f"Built {len(d88)} bytes but expected {total_d88_size}"
    )
    return bytes(d88)


# ============================== THE TEST ===================================

def run_test():
    tmpdir = tempfile.mkdtemp(prefix='pc98test_')
    print(f"Working in {tmpdir}")

    original_path = os.path.join(tmpdir, 'original.d88')
    modified_path = os.path.join(tmpdir, 'modified.d88')
    extract_dir   = os.path.join(tmpdir, 'extracted')

    try:
        # ------------------------------------------------------------------
        # 1.  Build and save the D88 image
        # ------------------------------------------------------------------
        print("=== Step 1: Build D88 image ===")
        raw = build_d88_image()
        with open(original_path, 'wb') as f:
            f.write(raw)
        original_hash = hashlib.sha256(raw).hexdigest()
        print(f"  Written {len(raw):,} bytes, SHA-256={original_hash[:16]}…")

        # ------------------------------------------------------------------
        # 2.  Open with D88Image and verify basic geometry
        # ------------------------------------------------------------------
        print("=== Step 2: Open with D88Image ===")
        disk = D88Image(original_path)
        assert disk.sector_size == SECTOR_SIZE, (
            f"sector_size={disk.sector_size}, expected {SECTOR_SIZE}"
        )
        assert disk.total_sectors == TOTAL_SECTORS, (
            f"total_sectors={disk.total_sectors}, expected {TOTAL_SECTORS}"
        )
        print(f"  D88 OK: {disk.total_sectors} sectors × {disk.sector_size}B, "
              f"label={disk.label!r}")

        # ------------------------------------------------------------------
        # 3.  Open with FATFilesystem and verify original files
        # ------------------------------------------------------------------
        print("=== Step 3: Open FATFilesystem, verify original files ===")
        fs = FATFilesystem(disk)
        assert fs.fat_type == 12, f"Expected FAT12, got FAT{fs.fat_type}"
        assert fs.bytes_per_sector == BPS
        assert fs.sectors_per_cluster == SPC
        assert fs.first_data_sector == FIRST_DATA_SECTOR
        assert fs.total_clusters == DATA_CLUSTERS
        print(f"  FAT12 OK: {fs.total_clusters} data clusters, "
              f"root at FS-sector {fs.first_root_sector}")

        # Check volume label
        assert 'TESTDISK' in fs.volume_label.upper(), (
            f"Volume label={fs.volume_label!r}"
        )

        # Check that both files are visible
        hello = fs.resolve_path('/HELLO.TXT')
        assert hello is not None, "HELLO.TXT not found"
        assert not hello.is_directory
        assert hello.size == len(HELLO_CONTENT)
        hello_data = fs.read_file(hello)
        assert hello_data == HELLO_CONTENT, (
            f"HELLO.TXT content mismatch: {hello_data!r}"
        )
        print(f"  HELLO.TXT OK ({hello.size} bytes)")

        data_entry = fs.resolve_path('/DATA.BIN')
        assert data_entry is not None, "DATA.BIN not found"
        assert data_entry.size == len(DATA_CONTENT)
        data_data = fs.read_file(data_entry)
        assert data_data == DATA_CONTENT, "DATA.BIN content mismatch"
        print(f"  DATA.BIN OK ({data_entry.size} bytes)")

        # walk() should list both files
        walked = {name: e for name, e in fs.walk()}
        assert '/HELLO.TXT' in walked, f"walk() missing HELLO.TXT: {list(walked)}"
        assert '/DATA.BIN'  in walked, f"walk() missing DATA.BIN:  {list(walked)}"
        print(f"  walk() OK: {sorted(walked.keys())}")

        # ------------------------------------------------------------------
        # 4.  Extract to host directory, then modify
        # ------------------------------------------------------------------
        print("=== Step 4: Extract and modify ===")
        os.makedirs(extract_dir, exist_ok=True)
        for rel_path, entry in fs.walk():
            host_path = os.path.join(extract_dir, rel_path.lstrip('/'))
            if entry.is_directory:
                os.makedirs(host_path, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(host_path), exist_ok=True)
                with open(host_path, 'wb') as f:
                    f.write(fs.read_file(entry))

        # Verify extracted files exist
        assert os.path.isfile(os.path.join(extract_dir, 'HELLO.TXT'))
        assert os.path.isfile(os.path.join(extract_dir, 'DATA.BIN'))

        # Modification A: edit HELLO.TXT
        new_hello_content = b"Modified PC-98 greeting!\r\nLine two.\r\nLine three.\r\n"
        with open(os.path.join(extract_dir, 'HELLO.TXT'), 'wb') as f:
            f.write(new_hello_content)
        print(f"  Modified HELLO.TXT ({len(new_hello_content)} bytes)")

        # Modification B: delete DATA.BIN
        os.remove(os.path.join(extract_dir, 'DATA.BIN'))
        print("  Deleted DATA.BIN")

        # Modification C: add a new file
        new_file_content = b"Brand new file on the PC-98!\r\n" * 50   # 1500 bytes, spans 2 clusters
        with open(os.path.join(extract_dir, 'NEWFILE.TXT'), 'wb') as f:
            f.write(new_file_content)
        print(f"  Added NEWFILE.TXT ({len(new_file_content)} bytes)")

        # Modification D: add a subdirectory with a file inside
        subdir_path = os.path.join(extract_dir, 'SUBDIR')
        os.makedirs(subdir_path)
        sub_file_content = b"File inside SUBDIR\r\n"
        with open(os.path.join(subdir_path, 'INNER.TXT'), 'wb') as f:
            f.write(sub_file_content)
        print(f"  Added SUBDIR/INNER.TXT ({len(sub_file_content)} bytes)")

        # ------------------------------------------------------------------
        # 5.  Write back to a NEW image file
        # ------------------------------------------------------------------
        print("=== Step 5: write_back_from_directory ===")
        files_written, dirs_written = fs.write_back_from_directory(
            extract_dir, save_path=modified_path
        )
        print(f"  Written {files_written} files, {dirs_written} dirs → {modified_path}")
        assert files_written >= 2, f"Expected ≥2 files, got {files_written}"
        assert dirs_written >= 1, f"Expected ≥1 dirs, got {dirs_written}"
        assert os.path.isfile(modified_path), "Modified image not created"

        # ------------------------------------------------------------------
        # 6.  Re-open the modified image and verify every change
        # ------------------------------------------------------------------
        print("=== Step 6: Verify modified image ===")
        disk2 = D88Image(modified_path)
        assert disk2.sector_size == SECTOR_SIZE
        assert disk2.total_sectors == TOTAL_SECTORS
        fs2 = FATFilesystem(disk2)
        assert fs2.fat_type == 12

        # 6a. HELLO.TXT should have new content
        hello2 = fs2.resolve_path('/HELLO.TXT')
        assert hello2 is not None, "HELLO.TXT missing in modified image"
        hello2_data = fs2.read_file(hello2)
        assert hello2_data == new_hello_content, (
            f"HELLO.TXT content wrong: {hello2_data!r}"
        )
        assert hello2.size == len(new_hello_content)
        print(f"  HELLO.TXT modified OK ({hello2.size} bytes)")

        # 6b. DATA.BIN should be gone
        gone = fs2.resolve_path('/DATA.BIN')
        assert gone is None, "DATA.BIN should have been deleted"
        print("  DATA.BIN deleted OK")

        # 6c. NEWFILE.TXT should exist with correct content
        nf = fs2.resolve_path('/NEWFILE.TXT')
        assert nf is not None, "NEWFILE.TXT missing"
        nf_data = fs2.read_file(nf)
        assert nf_data == new_file_content, (
            f"NEWFILE.TXT content mismatch ({len(nf_data)} vs {len(new_file_content)})"
        )
        print(f"  NEWFILE.TXT OK ({nf.size} bytes)")

        # 6d. SUBDIR should exist and contain INNER.TXT
        sd = fs2.resolve_path('/SUBDIR')
        assert sd is not None, "SUBDIR missing"
        assert sd.is_directory, "SUBDIR is not a directory"
        inner = fs2.resolve_path('/SUBDIR/INNER.TXT')
        assert inner is not None, "SUBDIR/INNER.TXT missing"
        inner_data = fs2.read_file(inner)
        assert inner_data == sub_file_content, (
            f"INNER.TXT content mismatch: {inner_data!r}"
        )
        print(f"  SUBDIR/INNER.TXT OK ({inner.size} bytes)")

        # 6e. walk() over modified image should show exactly the right set
        walked2 = {name: e for name, e in fs2.walk()}
        expected_paths = {'/HELLO.TXT', '/NEWFILE.TXT', '/SUBDIR', '/SUBDIR/INNER.TXT'}
        assert '/DATA.BIN' not in walked2, "DATA.BIN still in walk()"
        for p in expected_paths:
            assert p in walked2, f"Missing {p} in walk(): {sorted(walked2.keys())}"
        print(f"  walk() OK: {sorted(walked2.keys())}")

        # 6f. Verify the D88 container structure is intact
        assert disk2.path == modified_path
        # Read boot sector and check BPB is preserved
        boot_sector = disk2.read_sector(0)
        bps_check = struct.unpack_from('<H', boot_sector, 0x0B)[0]
        assert bps_check == BPS, f"BPB bytes_per_sector = {bps_check}"
        media_check = boot_sector[0x15]
        assert media_check == MEDIA, f"BPB media = 0x{media_check:02X}"
        print("  BPB preserved OK")

        # ------------------------------------------------------------------
        # 7.  Verify the ORIGINAL image is untouched
        # ------------------------------------------------------------------
        print("=== Step 7: Original image integrity ===")
        with open(original_path, 'rb') as f:
            check_hash = hashlib.sha256(f.read()).hexdigest()
        assert check_hash == original_hash, (
            f"Original image modified!  {check_hash} != {original_hash}"
        )
        print(f"  Original untouched: SHA-256={check_hash[:16]}…")

        # Also verify original still has the old content when re-opened
        disk_orig = D88Image(original_path)
        fs_orig = FATFilesystem(disk_orig)
        orig_hello = fs_orig.resolve_path('/HELLO.TXT')
        assert orig_hello is not None
        assert fs_orig.read_file(orig_hello) == HELLO_CONTENT
        orig_data = fs_orig.resolve_path('/DATA.BIN')
        assert orig_data is not None
        assert fs_orig.read_file(orig_data) == DATA_CONTENT
        print("  Original content verified")

        # ------------------------------------------------------------------
        # 8.  Cleanup
        # ------------------------------------------------------------------
        print("=== Step 8: Cleanup ===")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        print(f"  Removed {tmpdir}")

    print()
    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == '__main__':
    run_test()
