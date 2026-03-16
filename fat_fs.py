"""
FAT12/FAT16 Filesystem Parser for PC-98 disk images.

Key design: the BPB's bytes_per_sector may differ from the disk image's
native sector size (e.g., BPB says 1024 but a D88 image exposes 4096-byte
logical sectors). All filesystem reads go through _read_fs_bytes() which
translates BPB byte offsets into disk sector reads, making it work
regardless of how the disk image is sliced.
"""

import struct
import logging
from datetime import datetime

log = logging.getLogger("pc98mount.fat")

# FAT constants
FAT12_EOC = 0xFF8
FAT16_EOC = 0xFFF8
FAT12_BAD = 0xFF7
FAT16_BAD = 0xFFF7

# Directory entry attributes
ATTR_READ_ONLY = 0x01
ATTR_HIDDEN    = 0x02
ATTR_SYSTEM    = 0x04
ATTR_VOLUME_ID = 0x08
ATTR_DIRECTORY = 0x10
ATTR_ARCHIVE   = 0x20
ATTR_LFN       = 0x0F

# Known PC-98 floppy geometries:
# (image_bytes, bps, spc, reserved, nfats, root_entries, fat_sectors, total_sectors, media)
PC98_KNOWN_GEOMETRIES = [
    (1261568, 1024, 1, 1, 2, 192, 2, 1232, 0xFE),   # 2HD 1.2MB (most common)
    (1228800, 1024, 1, 1, 2, 192, 2, 1200, 0xFE),   # 2HD variant
    (655360,  512,  2, 1, 2, 112, 2, 1280, 0xFD),    # 2DD 640KB
    (737280,  512,  2, 1, 2, 112, 3, 1440, 0xFD),    # 2DD 720KB
    (1474560, 512,  1, 1, 2, 224, 9, 2880, 0xF0),    # 1.44MB
]


class FileEntry:
    """Represents a file or directory in the FAT filesystem."""

    def __init__(self, name, ext, attr, cluster, size, date, time_val, raw_name=None):
        self.raw_name = raw_name or name
        self.name = name.rstrip()
        self.ext = ext.rstrip()
        self.attr = attr
        self.cluster = cluster
        self.size = size
        self.date = date
        self.time = time_val
        self.children = {}

    @property
    def is_directory(self):
        return bool(self.attr & ATTR_DIRECTORY)

    @property
    def is_volume_label(self):
        return bool(self.attr & ATTR_VOLUME_ID)

    @property
    def is_hidden(self):
        return bool(self.attr & ATTR_HIDDEN)

    @property
    def is_lfn(self):
        return (self.attr & ATTR_LFN) == ATTR_LFN

    @property
    def display_name(self):
        if self.is_directory:
            return self.name if self.name in ('.', '..') else self.name
        if self.ext:
            return f"{self.name}.{self.ext}"
        return self.name

    @property
    def datetime(self):
        try:
            day = self.date & 0x1F
            month = (self.date >> 5) & 0x0F
            year = ((self.date >> 9) & 0x7F) + 1980
            sec = (self.time & 0x1F) * 2
            minute = (self.time >> 5) & 0x3F
            hour = (self.time >> 11) & 0x1F
            if day == 0: day = 1
            if month == 0: month = 1
            return datetime(year, month, day, hour, minute, min(sec, 59))
        except (ValueError, OverflowError):
            return datetime(1980, 1, 1)

    def __repr__(self):
        kind = "DIR" if self.is_directory else "FILE"
        return f"<{kind} {self.display_name} size={self.size} cluster={self.cluster}>"


class FATFilesystem:
    """
    Parses a FAT12 or FAT16 filesystem from a disk image.

    Handles sector size mismatches: the BPB may declare 1024-byte sectors
    while the disk image parser reports a different native sector size.
    All reads go through _read_fs_bytes() which works in absolute byte
    offsets and is independent of the disk's sector granularity.
    """

    def __init__(self, disk_image):
        self.disk = disk_image
        self._image_total_bytes = disk_image.total_sectors * disk_image.sector_size
        self._parse_bpb()
        self._load_fat()
        self._build_root()

    # ── Byte-level read layer ────────────────────────────────────────

    def _read_fs_bytes(self, byte_offset, byte_length):
        """
        Read `byte_length` bytes starting at `byte_offset` in the image.
        Translates to disk sector reads regardless of the disk's native
        sector size.
        """
        if byte_length == 0:
            return b''

        ds = self.disk.sector_size
        first_disk_sector = byte_offset // ds
        last_disk_sector = (byte_offset + byte_length - 1) // ds
        count = last_disk_sector - first_disk_sector + 1

        raw = self.disk.read_sectors(first_disk_sector, count)

        local_start = byte_offset % ds
        return raw[local_start:local_start + byte_length]

    def _read_fs_sectors(self, fs_sector, count):
        """
        Read `count` filesystem sectors (at self.bytes_per_sector size)
        starting at filesystem sector number `fs_sector`.
        """
        byte_offset = fs_sector * self.bytes_per_sector
        byte_length = count * self.bytes_per_sector
        return self._read_fs_bytes(byte_offset, byte_length)

    # ── BPB parsing with validation ──────────────────────────────────

    def _bpb_is_sane(self, bps, spc, reserved, nfats, root_ents, fat_sz, total):
        """Check if a set of BPB values makes sense."""
        if bps not in (128, 256, 512, 1024, 2048, 4096):
            return False
        if spc == 0 or spc > 128 or (spc & (spc - 1)) != 0:
            return False
        if reserved == 0 or reserved > 64:
            return False
        if nfats == 0 or nfats > 4:
            return False
        if root_ents == 0 or root_ents > 4096:
            return False
        if fat_sz == 0 or fat_sz > 256:
            return False
        if total == 0:
            return False

        # Layout must fit within image
        root_dir_sects = (root_ents * 32 + bps - 1) // bps
        first_data = reserved + nfats * fat_sz + root_dir_sects
        if first_data >= total:
            return False

        # Compare total BYTES not sector counts (sector sizes may differ)
        bpb_bytes = total * bps
        if bpb_bytes > self._image_total_bytes * 2:
            return False

        return True

    def _parse_bpb(self):
        """Parse BPB with validation, falling back to known geometries."""
        # Read the first 512 or 1024 bytes (boot sector)
        boot = self._read_fs_bytes(0, min(1024, self._image_total_bytes))

        # Read standard BPB fields
        bps = struct.unpack_from('<H', boot, 0x0B)[0]
        spc = boot[0x0D]
        reserved = struct.unpack_from('<H', boot, 0x0E)[0]
        nfats = boot[0x10]
        root_ents = struct.unpack_from('<H', boot, 0x11)[0]
        total16 = struct.unpack_from('<H', boot, 0x13)[0]
        media = boot[0x15]
        fat_sz = struct.unpack_from('<H', boot, 0x16)[0]

        total = total16
        if total == 0 and len(boot) >= 0x24:
            total = struct.unpack_from('<I', boot, 0x20)[0]

        log.info(
            f"BPB raw: bps={bps} spc={spc} reserved={reserved} nfats={nfats} "
            f"root_ents={root_ents} fat_sz={fat_sz} total={total} media=0x{media:02X}"
        )
        log.info(
            f"Image: {self._image_total_bytes} bytes, "
            f"disk reports {self.disk.total_sectors} sectors × {self.disk.sector_size}B"
        )

        if self._bpb_is_sane(bps, spc, reserved, nfats, root_ents, fat_sz, total):
            log.info("BPB valid")
            self.bytes_per_sector = bps
            self.sectors_per_cluster = spc
            self.reserved_sectors = reserved
            self.num_fats = nfats
            self.root_entry_count = root_ents
            self.fat_size_16 = fat_sz
            self.media_descriptor = media
            self.total_sectors = total if total > 0 else (self._image_total_bytes // bps)
        else:
            log.warning("BPB invalid, trying known PC-98 geometries")
            self._apply_geometry_fallback()

        # Extra BPB fields (informational)
        try:
            self.sectors_per_track = struct.unpack_from('<H', boot, 0x18)[0]
            self.num_heads = struct.unpack_from('<H', boot, 0x1A)[0]
            self.hidden_sectors = struct.unpack_from('<H', boot, 0x1C)[0]
        except struct.error:
            self.sectors_per_track = 8
            self.num_heads = 2
            self.hidden_sectors = 0

        # Compute layout (all in BPB sector units)
        self.root_dir_sectors = (
            (self.root_entry_count * 32 + self.bytes_per_sector - 1)
            // self.bytes_per_sector
        )
        self.first_fat_sector = self.reserved_sectors
        self.first_root_sector = self.first_fat_sector + self.num_fats * self.fat_size_16
        self.first_data_sector = self.first_root_sector + self.root_dir_sectors

        # FAT type
        data_sectors = self.total_sectors - self.first_data_sector
        if data_sectors > 0 and self.sectors_per_cluster > 0:
            self.total_clusters = data_sectors // self.sectors_per_cluster
        else:
            self.total_clusters = 0
        self.fat_type = 12 if self.total_clusters < 4085 else 16

        # Volume label
        self.volume_label = ""
        try:
            self.volume_label = boot[0x2B:0x36].decode(
                'shift_jis', errors='replace'
            ).rstrip()
        except Exception:
            pass

        log.info(
            f"Layout: FAT{self.fat_type}, "
            f"root at FS-sector {self.first_root_sector} "
            f"(byte offset 0x{self.first_root_sector * self.bytes_per_sector:X}), "
            f"data at FS-sector {self.first_data_sector} "
            f"(byte offset 0x{self.first_data_sector * self.bytes_per_sector:X}), "
            f"{self.total_clusters} clusters"
        )

        # Validate FAT header
        self._validate_fat_header()

    def _apply_geometry_fallback(self):
        """Apply a known PC-98 geometry based on image size."""
        matched = False
        for (geo_bytes, geo_bps, geo_spc, geo_res, geo_nfats,
             geo_root, geo_fat, geo_total, geo_media) in PC98_KNOWN_GEOMETRIES:
            if abs(self._image_total_bytes - geo_bytes) < 4096:
                log.info(
                    f"Matched geometry: {geo_bytes} bytes, "
                    f"{geo_bps}B sectors, {geo_total} total"
                )
                self.bytes_per_sector = geo_bps
                self.sectors_per_cluster = geo_spc
                self.reserved_sectors = geo_res
                self.num_fats = geo_nfats
                self.root_entry_count = geo_root
                self.fat_size_16 = geo_fat
                self.media_descriptor = geo_media
                self.total_sectors = geo_total
                matched = True
                break

        if not matched:
            log.warning("No geometry match — using default PC-98 2HD layout")
            # Default: assume 1024-byte sectors
            bps = 1024 if self._image_total_bytes % 1024 == 0 else 512
            self.bytes_per_sector = bps
            self.sectors_per_cluster = 1
            self.reserved_sectors = 1
            self.num_fats = 2
            self.root_entry_count = 192
            self.fat_size_16 = 2
            self.media_descriptor = 0xFE
            self.total_sectors = self._image_total_bytes // bps

    def _validate_fat_header(self):
        """Check that the FAT table starts with a valid media descriptor."""
        try:
            fat_data = self._read_fs_sectors(self.first_fat_sector, 1)
            first_byte = fat_data[0]
            valid_media = {0xF0, 0xF8, 0xF9, 0xFA, 0xFB, 0xFC, 0xFD, 0xFE, 0xFF}
            if first_byte not in valid_media:
                log.warning(
                    f"FAT first byte 0x{first_byte:02X} not a valid media "
                    f"descriptor — may not be FAT"
                )
            else:
                log.info(f"FAT header valid, media byte 0x{first_byte:02X}")
        except Exception as e:
            log.warning(f"Could not read FAT header: {e}")

    # ── FAT table ────────────────────────────────────────────────────

    def _load_fat(self):
        self._fat_data = self._read_fs_sectors(
            self.first_fat_sector, self.fat_size_16
        )

    def get_fat_entry(self, cluster):
        if self.fat_type == 12:
            return self._get_fat12_entry(cluster)
        return self._get_fat16_entry(cluster)

    def _get_fat12_entry(self, cluster):
        offset = cluster + (cluster // 2)
        if offset + 1 >= len(self._fat_data):
            return 0xFFF
        word = struct.unpack_from('<H', self._fat_data, offset)[0]
        return (word >> 4) if (cluster & 1) else (word & 0x0FFF)

    def _get_fat16_entry(self, cluster):
        offset = cluster * 2
        if offset + 1 >= len(self._fat_data):
            return 0xFFFF
        return struct.unpack_from('<H', self._fat_data, offset)[0]

    def get_cluster_chain(self, start_cluster):
        if start_cluster < 2:
            return []
        chain = []
        cluster = start_cluster
        eoc = FAT12_EOC if self.fat_type == 12 else FAT16_EOC
        bad = FAT12_BAD if self.fat_type == 12 else FAT16_BAD
        max_cl = self.total_clusters + 2
        visited = set()
        while cluster >= 2 and cluster < eoc and cluster != bad:
            if cluster in visited or cluster >= max_cl:
                break
            visited.add(cluster)
            chain.append(cluster)
            cluster = self.get_fat_entry(cluster)
        return chain

    def cluster_to_fs_sector(self, cluster):
        """Convert cluster number to filesystem sector number."""
        return self.first_data_sector + (cluster - 2) * self.sectors_per_cluster

    def read_cluster(self, cluster):
        return self._read_fs_sectors(
            self.cluster_to_fs_sector(cluster), self.sectors_per_cluster
        )

    def read_file(self, entry):
        if entry.size == 0 and not entry.is_directory:
            return b''
        chain = self.get_cluster_chain(entry.cluster)
        data = bytearray()
        for cl in chain:
            data.extend(self.read_cluster(cl))
        return bytes(data[:entry.size]) if not entry.is_directory else bytes(data)

    # ── Directory parsing ────────────────────────────────────────────

    def _parse_dir_entries(self, raw_data):
        entries = []
        offset = 0
        while offset + 32 <= len(raw_data):
            entry_data = raw_data[offset:offset + 32]
            offset += 32

            first_byte = entry_data[0]
            if first_byte == 0x00:
                break
            if first_byte == 0xE5:
                continue

            attr = entry_data[11]
            if (attr & ATTR_LFN) == ATTR_LFN:
                continue
            if first_byte < 0x20 and first_byte != 0x05:
                continue
            if attr & 0xC0:
                continue

            try:
                name = entry_data[0:8].decode('shift_jis', errors='replace')
                ext = entry_data[8:11].decode('shift_jis', errors='replace')
            except Exception:
                name = entry_data[0:8].decode('ascii', errors='replace')
                ext = entry_data[8:11].decode('ascii', errors='replace')

            if entry_data[0] == 0x05:
                name = chr(0xE5) + name[1:]

            time_val = struct.unpack_from('<H', entry_data, 22)[0]
            date_val = struct.unpack_from('<H', entry_data, 24)[0]
            cluster = struct.unpack_from('<H', entry_data, 26)[0]
            size = struct.unpack_from('<I', entry_data, 28)[0]

            entry = FileEntry(name, ext, attr, cluster, size, date_val, time_val,
                              raw_name=entry_data[0:11])

            if entry.is_volume_label:
                if not self.volume_label:
                    self.volume_label = entry.display_name
                continue

            entries.append(entry)

        return entries

    def _build_root(self):
        log.info(
            f"Reading root dir: FS-sector {self.first_root_sector}, "
            f"{self.root_dir_sectors} FS-sectors, "
            f"byte offset 0x{self.first_root_sector * self.bytes_per_sector:X}"
        )
        root_data = self._read_fs_sectors(
            self.first_root_sector, self.root_dir_sectors
        )

        self.root = FileEntry("", "", ATTR_DIRECTORY, 0, 0, 0, 0)
        root_entries = self._parse_dir_entries(root_data)

        log.info(f"Found {len(root_entries)} root directory entries")
        for e in root_entries:
            log.info(f"  {e}")

        for e in root_entries:
            self.root.children[e.display_name.upper()] = e

        self._parse_subdirs(self.root, depth=0)

    def _parse_subdirs(self, dir_entry, depth=0):
        if depth > 20:
            return
        for name, entry in list(dir_entry.children.items()):
            if entry.is_directory and entry.name not in ('.', '..'):
                if entry.cluster >= 2:
                    try:
                        dir_data = self.read_file(entry)
                        sub_entries = self._parse_dir_entries(dir_data)
                        for se in sub_entries:
                            if se.name not in ('.', '..'):
                                entry.children[se.display_name.upper()] = se
                        self._parse_subdirs(entry, depth + 1)
                    except Exception:
                        pass

    # ── Path resolution and walking ──────────────────────────────────

    def resolve_path(self, path):
        path = path.replace('\\', '/')
        parts = [p for p in path.split('/') if p]
        current = self.root
        for part in parts:
            if not current.is_directory:
                return None
            if part.upper() not in current.children:
                return None
            current = current.children[part.upper()]
        return current

    def list_dir(self, path='/'):
        entry = self.resolve_path(path)
        if entry is None or not entry.is_directory:
            return None
        return list(entry.children.values())

    def walk(self, path='/', prefix=''):
        entries = self.list_dir(path)
        if entries is None:
            return
        for e in entries:
            if e.name in ('.', '..'):
                continue
            full = f"{prefix}/{e.display_name}"
            yield full, e
            if e.is_directory:
                yield from self.walk(full, full)
