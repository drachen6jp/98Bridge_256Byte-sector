"""
PC-98 Disk Image Format Parsers
Supports: D88/D68, HDM, FDI, HDI, and raw sector images.
Provides uniform sector-level access regardless of container format.
"""

import struct


class DiskImage:
    """Base class providing sector-level access to a disk image."""

    def __init__(self, path):
        self.path = path
        with open(path, 'rb') as f:
            self._data = f.read()
        self._parse()

    def _parse(self):
        raise NotImplementedError

    @property
    def sector_size(self):
        return self._sector_size

    @property
    def total_sectors(self):
        return self._total_sectors

    @property
    def label(self):
        return self._label

    def read_sector(self, lba):
        raise NotImplementedError

    def read_sectors(self, lba, count):
        data = bytearray()
        for i in range(count):
            data.extend(self.read_sector(lba + i))
        return bytes(data)


class RawImage(DiskImage):
    """
    Raw / HDM image — flat sector dump with no header.
    PC-98 2HD: 77 cyl × 2 heads × 8 spt × 1024 bytes/sector = 1,261,568 bytes
    PC-98 2DD: 80 cyl × 2 heads × 9 spt × 512 bytes/sector  = 737,280 bytes
    """

    KNOWN_GEOMETRIES = {
        1261568: (1024, 1232),   # 2HD 1.2MB
        1228800: (1024, 1200),   # 2HD alternate
        737280:  (512, 1440),    # 2DD 720KB
        1474560: (512, 2880),    # 1.44MB (rare on PC-98)
    }

    def __init__(self, path, sector_size=None):
        self._forced_sector_size = sector_size
        super().__init__(path)

    def _parse(self):
        size = len(self._data)
        if self._forced_sector_size:
            self._sector_size = self._forced_sector_size
            self._total_sectors = size // self._sector_size
        elif size in self.KNOWN_GEOMETRIES:
            self._sector_size, self._total_sectors = self.KNOWN_GEOMETRIES[size]
        else:
            # Default: try 1024, fall back to 512
            if size % 1024 == 0:
                self._sector_size = 1024
            else:
                self._sector_size = 512
            self._total_sectors = size // self._sector_size
        self._label = f"RAW ({self._total_sectors} sectors)"

    def read_sector(self, lba):
        offset = lba * self._sector_size
        if offset + self._sector_size > len(self._data):
            return b'\x00' * self._sector_size
        return self._data[offset:offset + self._sector_size]


class FDIImage(DiskImage):
    """
    FDI image format — 4096-byte header followed by raw sector data.
    Header contains geometry information.
    """

    HEADER_SIZE = 4096

    def _parse(self):
        if len(self._data) < self.HEADER_SIZE:
            raise ValueError("File too small for FDI format")

        # FDI header layout (little-endian):
        # 0x00: uint32 - fdd_type
        # 0x04: uint32 - header_size (should be 4096)
        # 0x08: uint32 - sector_size (bytes per sector)
        # 0x0C: uint32 - sectors per cylinder? (varies by implementation)
        # 0x10: uint32 - sectors per track
        # 0x14: uint32 - heads (surfaces)
        # 0x18: uint32 - cylinders (tracks)
        fdd_type, hdr_size, sec_size = struct.unpack_from('<III', self._data, 0)
        spt, heads, cyls = struct.unpack_from('<III', self._data, 0x10)

        if sec_size not in (128, 256, 512, 1024, 2048, 4096):
            sec_size = 1024
        self._sector_size = sec_size

        raw_data = self._data[self.HEADER_SIZE:]
        self._total_sectors = len(raw_data) // self._sector_size
        self._raw_offset = self.HEADER_SIZE
        self._label = f"FDI ({cyls}C/{heads}H/{spt}S)"

    def read_sector(self, lba):
        offset = self._raw_offset + lba * self._sector_size
        if offset + self._sector_size > len(self._data):
            return b'\x00' * self._sector_size
        return self._data[offset:offset + self._sector_size]


class D88Image(DiskImage):
    """
    D88/D68 image format — used by many Japanese emulators.
    Has a header with disk name, media type, and a 164-entry track offset table.
    Each track's sectors have individual headers with C/H/R/N fields.
    """

    def _parse(self):
        if len(self._data) < 0x2B0:
            raise ValueError("File too small for D88 format")

        # D88 header
        name_raw = self._data[0:17].split(b'\x00')[0]
        try:
            self._label = name_raw.decode('shift_jis', errors='replace')
        except Exception:
            self._label = "D88 Image"

        self._write_protect = self._data[0x1A]
        self._media_type = self._data[0x1B]
        self._disk_size = struct.unpack_from('<I', self._data, 0x1C)[0]

        # Track offset table: 164 entries at offset 0x20
        self._track_offsets = []
        for i in range(164):
            off = struct.unpack_from('<I', self._data, 0x20 + i * 4)[0]
            self._track_offsets.append(off)

        # Build a flat sector table by walking all tracks
        self._sectors = []  # list of (offset_in_file, size) for each logical sector
        self._sector_size = 0

        for track_off in self._track_offsets:
            if track_off == 0:
                continue
            pos = track_off
            if pos >= len(self._data):
                continue

            # Read sectors in this track
            while pos < len(self._data) - 16:
                # D88 sector header: C(1) H(1) R(1) N(1) num_sectors(2)
                # density(1) deleted(1) status(1) reserved(5) data_size(2)
                c, h, r, n = struct.unpack_from('BBBB', self._data, pos)
                num_sects = struct.unpack_from('<H', self._data, pos + 4)[0]
                data_size = struct.unpack_from('<H', self._data, pos + 14)[0]

                if data_size == 0 or num_sects == 0:
                    break

                sec_size = 128 << n
                if self._sector_size == 0:
                    self._sector_size = sec_size

                data_offset = pos + 16
                self._sectors.append((data_offset, data_size))

                pos = data_offset + data_size

                # Stop if we've read all sectors for this track
                if len(self._sectors) % num_sects == 0 and num_sects > 0:
                    # Check if next sector header belongs to a different track
                    if pos + 16 <= len(self._data):
                        next_c = self._data[pos]
                        next_h = self._data[pos + 1]
                        if next_c != c or next_h != h:
                            break

        if self._sector_size == 0:
            self._sector_size = 1024
        self._total_sectors = len(self._sectors)
        if not self._label or self._label.strip() == '':
            self._label = f"D88 ({self._total_sectors} sectors)"

    def read_sector(self, lba):
        if lba < 0 or lba >= len(self._sectors):
            return b'\x00' * self._sector_size
        offset, size = self._sectors[lba]
        data = self._data[offset:offset + size]
        # Pad or truncate to uniform sector size
        if len(data) < self._sector_size:
            data = data + b'\x00' * (self._sector_size - len(data))
        return data[:self._sector_size]


class HDIImage(DiskImage):
    """
    HDI image format — hard disk image with a small header.
    Header: 4096 bytes (commonly), followed by raw sector data.
    Used by Anex86 and some other emulators.
    """

    def _parse(self):
        if len(self._data) < 4096:
            raise ValueError("File too small for HDI format")

        # HDI header (Anex86 format):
        # 0x00: uint32 - reserved/padding flag
        # 0x04: uint32 - header size
        # 0x08: uint32 - total data size
        # 0x0C: uint32 - sector size
        # 0x10: uint32 - sectors
        # 0x14: uint32 - heads
        # 0x18: uint32 - cylinders
        hdr_size = struct.unpack_from('<I', self._data, 0x04)[0]
        data_size = struct.unpack_from('<I', self._data, 0x08)[0]
        sec_size = struct.unpack_from('<I', self._data, 0x0C)[0]
        spt = struct.unpack_from('<I', self._data, 0x10)[0]
        heads = struct.unpack_from('<I', self._data, 0x14)[0]
        cyls = struct.unpack_from('<I', self._data, 0x18)[0]

        if sec_size not in (128, 256, 512, 1024, 2048, 4096):
            sec_size = 512  # HDI often uses 512-byte sectors for HDD
        if hdr_size == 0 or hdr_size > 0x10000:
            hdr_size = 4096

        self._sector_size = sec_size
        self._raw_offset = hdr_size
        raw_len = len(self._data) - hdr_size
        self._total_sectors = raw_len // sec_size
        self._label = f"HDI ({cyls}C/{heads}H/{spt}S)"

    def read_sector(self, lba):
        offset = self._raw_offset + lba * self._sector_size
        if offset + self._sector_size > len(self._data):
            return b'\x00' * self._sector_size
        return self._data[offset:offset + self._sector_size]


def open_image(path):
    """Auto-detect image format and return appropriate DiskImage instance."""
    ext = path.lower()
    size = os.path.getsize(path) if isinstance(path, str) else 0

    if ext.endswith('.d88') or ext.endswith('.d68') or ext.endswith('.d77'):
        return D88Image(path)
    elif ext.endswith('.fdi'):
        return FDIImage(path)
    elif ext.endswith('.hdi'):
        return HDIImage(path)
    elif ext.endswith('.hdm') or ext.endswith('.tfd'):
        return RawImage(path, sector_size=1024)
    elif ext.endswith('.img') or ext.endswith('.ima'):
        # Heuristic: check size
        return RawImage(path)
    else:
        # Try raw with auto-detect
        return RawImage(path)


import os
