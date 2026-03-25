"""
Example plugin for IPL1前パラメータ型BIOS用 format.

Demonstrates how to add support for a new disk image format, a new
partition detector, and (optionally) a new filesystem prober by
dropping a single ``.py`` file into the ``plugins/`` directory.

To enable this plugin, simply place it (or symlink it) in one of:
  - <app_dir>/plugins/
  - ~/.config/pc98mount/plugins/

It will be picked up automatically on the next launch.

IPL1前パラメータ型BIOS用 format overview
-------------------
べたイメージのちょうど空いてる場所を間借りしてジオメトリをディスク側に持たせよう
というやり方を提案しています。

"""

import logging

from disk_image import DiskImage
from registry import register_image_format

log = logging.getLogger("pc98mount.plugin.nhd")


# =====================================================================
# 1. Image format: Raw(IPL1)
# =====================================================================

class IPL1Image(DiskImage):
    """IPL1前パラメータ型BIOS用 image."""
    def _parse(self):
        self._sector_size = 512
        heads = self._data[0x3]
        spt   = self._data[0x2]

        raw_len = len(self._data)
        if raw_len < 0x110000000 and (heads == 0 or spt == 0) or (heads == 0x90 and spt == 0x90):
            heads = 8
            spt = 17
        if raw_len >= 0x110000000 and (heads == 0 or spt == 0) or (heads == 0x90 and spt == 0x90):
            heads = 16
            spt = 63
        cyls  = raw_len // (heads * spt * self._sector_size)
        self._total_sectors = raw_len // self._sector_size
        self._raw_offset = 0

        self._spt = spt
        self._heads = heads
        self._label = (
            f"Raw(IPL1) ({cyls}C/{heads}H/{spt}S)"
            if cyls and heads and spt
            else f"Raw(IPL1) ({self._total_sectors} sectors)"
        )
        log.info(f"Raw(IPL1) image: {self._label}")

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

register_image_format(
    extensions=['.vhd'],
    opener=IPL1Image,
    label='Raw (IPL1type HDD)',
    group_label='Raw(IPL1) Images',
    priority=10,
)


log.info("IPL1 plugin loaded")
