# pc98mount plugins

Drop `.py` files here to extend pc98mount with new image formats,
partition schemes, or filesystems.  Plugins are loaded automatically
at startup.

You can also install, remove, and reload plugins from the GUI via
**Plugins → Plugin Manager**.

## Quick-start

```python
"""my_format.py — minimal plugin that adds .xyz image support."""

from disk_image import DiskImage
from registry import register_image_format

class XYZImage(DiskImage):
    def _parse(self):
        self._sector_size = 512
        self._total_sectors = len(self._data) // 512
        self._label = "XYZ Image"

    def read_sector(self, lba):
        off = lba * 512
        return bytes(self._data[off:off + 512])

    def write_sector(self, lba, data):
        off = lba * 512
        self._data[off:off + 512] = data[:512]

register_image_format(
    extensions=['.xyz'],
    opener=XYZImage,
    label='XYZ Images',
    priority=10,
)
```

## What you can register

| Registry helper                   | What it does                                    |
|-----------------------------------|-------------------------------------------------|
| `register_image_format()`         | Adds a new disk-image container format          |
| `register_partition_detector()`   | Adds a new partition-table detection scheme      |
| `register_filesystem_prober()`    | Adds a new filesystem type (beyond FAT12/16)    |

All helpers live in `registry.py`.  See `nhd_format.py` in this
directory for a full worked example.
