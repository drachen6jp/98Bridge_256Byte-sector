"""
Built-in PC-98 image formats and partition detectors.

Registers:
  Image formats : D88/D68/D77, FDI, HDI, HDM/TFD, Raw IMG/IMA
  Partition detectors : IBM-PC MBR, PC-98 IPL

These ship with pc98mount and are loaded automatically at startup.
"""

from disk_image import D88Image, FDIImage, HDIImage, RawImage
from partition import detect_mbr, detect_pc98
from registry import register_image_format, register_partition_detector, set_fallback_opener

# ── Image formats ───────────────────────────────────────────────

register_image_format(
    extensions=['.d88', '.d68', '.d77'],
    opener=D88Image,
    label='D88/D68/D77',
    group_label='D88 Images',
    priority=10,
)

register_image_format(
    extensions=['.fdi'],
    opener=FDIImage,
    label='FDI',
    group_label='FDI Images',
    priority=10,
)

register_image_format(
    extensions=['.hdi'],
    opener=HDIImage,
    label='HDI',
    group_label='HDI Images',
    priority=10,
)

register_image_format(
    extensions=['.hdm', '.tfd'],
    opener=lambda path: RawImage(path, sector_size=1024),
    label='HDM/TFD',
    group_label='HDM Images',
    priority=10,
)

register_image_format(
    extensions=['.img', '.ima'],
    opener=RawImage,
    label='Raw (IMG/IMA)',
    group_label='Raw Images',
    priority=20,
)

set_fallback_opener(RawImage)

# ── Partition detectors ─────────────────────────────────────────

register_partition_detector('MBR',   detect_mbr,  priority=10)
register_partition_detector('PC-98', detect_pc98, priority=20)
