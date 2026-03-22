"""
Example plugin for pc98mount — NHD (T98-Next Hard Disk) format.

Demonstrates how to add support for a new disk image format, a new
partition detector, and (optionally) a new filesystem prober by
dropping a single ``.py`` file into the ``plugins/`` directory.

To enable this plugin, simply place it (or symlink it) in one of:
  - <app_dir>/plugins/
  - ~/.config/pc98mount/plugins/

It will be picked up automatically on the next launch.

NHD format overview
-------------------
NHD is a hard-disk image container used by the T98-Next emulator.
It has a 512-byte text header starting with "T98HDDIMAGE.R0\\0"
followed by key=value pairs (newline-separated) describing the
geometry, then raw sector data.
"""

import logging

from disk_image import DiskImage
from registry import register_image_format

log = logging.getLogger("pc98mount.plugin.nhd")


# =====================================================================
# 1. Image format: NHD
# =====================================================================

class NHDImage(DiskImage):
    """T98-Next NHD hard-disk image."""

    HEADER_SIZE = 512
    MAGIC = b"T98HDDIMAGE.R0\x00"

    def _parse(self):
        if len(self._data) < self.HEADER_SIZE:
            raise ValueError("File too small for NHD format")

        if self._data[:15] != self.MAGIC[:15]:
            raise ValueError("Not an NHD image (bad magic)")

        # Parse the text header for geometry hints.
        # The header is "T98HDDIMAGE.R0\0" followed by \r\n-separated
        # key=value pairs, then null-padded to 512 bytes.
        header_raw = self._data[:self.HEADER_SIZE]
        # Strip everything from the first run of nulls onward, then
        # skip past the magic line.
        header_str = header_raw.split(b'\x00\x00')[0].decode(
            'ascii', errors='ignore')
        props = {}
        for line in header_str.replace('\r\n', '\n').split('\n'):
            line = line.strip()
            if '=' in line:
                key, _, val = line.partition('=')
                props[key.strip().upper()] = val.strip()

        self._sector_size = int(props.get('SECSIZE', '512'))
        cyls  = int(props.get('CYLINDERS', '0'))
        heads = int(props.get('SURFACES', '0'))
        spt   = int(props.get('SECTORS', '0'))

        raw_len = len(self._data) - self.HEADER_SIZE
        self._total_sectors = raw_len // self._sector_size
        self._raw_offset = self.HEADER_SIZE

        self._spt = spt
        self._heads = heads
        self._label = (
            f"NHD ({cyls}C/{heads}H/{spt}S)"
            if cyls and heads and spt
            else f"NHD ({self._total_sectors} sectors)"
        )
        log.info(f"NHD image: {self._label}")

    def read_sector(self, lba):
        offset = self._raw_offset + lba * self._sector_size
        if offset + self._sector_size > len(self._data):
            return b'\x00' * self._sector_size
        return bytes(self._data[offset:offset + self._sector_size])

    def write_sector(self, lba, data):
        offset = self._raw_offset + lba * self._sector_size
        end = offset + self._sector_size
        if end > len(self._data):
            raise IndexError(f"Sector {lba} out of range")
        self._data[offset:end] = data[:self._sector_size]


# Register the NHD format.  Priority 10 ensures it is tried before
# the generic RawImage fallback (priority 20).
register_image_format(
    extensions=['.nhd'],
    opener=NHDImage,
    label='NHD (T98-Next HDD)',
    group_label='NHD Images',
    priority=10,
)


# =====================================================================
# 2. (Optional) Partition detector example
# =====================================================================
#
# If your new format uses a non-standard partition table you can
# register a detector for it.  This one is a no-op placeholder;
# NHD images typically use the standard PC-98 IPL table which is
# already handled by the built-in detector.
#
# Uncomment the block below to see it in action (and add the
# corresponding imports at the top of the file):
#
# from registry import register_partition_detector
# from partition import PartitionEntry
#
# def detect_nhd_partitions(disk_image):
#     """Detect partitions specific to NHD images."""
#     if not isinstance(disk_image, NHDImage):
#         return []
#     # ... custom detection logic ...
#     return []
#
# register_partition_detector(
#     'NHD-custom', detect_nhd_partitions, priority=5,
# )


# =====================================================================
# 3. (Optional) Filesystem prober example
# =====================================================================
#
# If your images use a non-FAT filesystem you can register a prober.
# The prober should return a filesystem object (duck-type compatible
# with FATFilesystem) or raise an exception so that the next prober
# in line gets a chance.
#
# from registry import register_filesystem_prober
#
# class MyCustomFS:
#     def __init__(self, disk_image):
#         ...
#     # implement root, walk(), read_file(), etc.
#
# def _probe_custom_fs(disk_image):
#     return MyCustomFS(disk_image)
#
# register_filesystem_prober('CustomFS', _probe_custom_fs, priority=50)


log.info("NHD plugin loaded")
