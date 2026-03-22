"""
Built-in FAT12/FAT16 filesystem prober.

Registers:
  Filesystem prober : FAT12/FAT16

Ships with pc98mount and is loaded automatically at startup.
"""

from fat_fs import FATFilesystem
from registry import register_filesystem_prober


def _probe_fat(disk_image):
    """Filesystem prober for FAT12/FAT16.

    Returns a ``FATFilesystem`` instance or raises on failure.
    """
    return FATFilesystem(disk_image)


register_filesystem_prober('FAT12/FAT16', _probe_fat, priority=10)
