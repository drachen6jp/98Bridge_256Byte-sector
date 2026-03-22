"""
Generate blank NHD (T98-Next) hard disk images for testing.

The NHD format is a 512-byte ASCII text header starting with
"T98HDDIMAGE.R0\\0", followed by key=value geometry fields,
then raw sector data.

Usage:
    python make_nhd.py                      # 40 MB default
    python make_nhd.py test_20mb.nhd 20     # 20 MB
    python make_nhd.py big.nhd 128          # 128 MB
"""

import struct
import sys


def make_nhd(path, size_mb=40):
    """Create a blank NHD image at *path* with the given size in MB."""

    # Standard PC-98 HDD geometry
    sector_size = 512
    spt = 17
    heads = 8
    sectors_per_cyl = spt * heads
    total_sectors = (size_mb * 1024 * 1024) // sector_size
    cyls = total_sectors // sectors_per_cyl

    # Clamp so total_sectors is exact
    total_sectors = cyls * sectors_per_cyl
    image_bytes = total_sectors * sector_size

    # ── Build the 512-byte text header ───────────────────────
    lines = [
        "T98HDDIMAGE.R0\x00",
        f"COMMENT=Blank {size_mb}MB test image",
        f"HEADERSIZE=512",
        f"CYLINDERS={cyls}",
        f"SURFACES={heads}",
        f"SECTORS={spt}",
        f"SECSIZE={sector_size}",
    ]
    header_text = "\r\n".join(lines).encode("ascii")
    header = bytearray(512)
    header[:len(header_text)] = header_text

    # ── Write the file ───────────────────────────────────────
    with open(path, "wb") as f:
        f.write(header)
        # Write the raw sector area in 64 KB chunks
        remaining = image_bytes
        chunk = b'\x00' * 65536
        while remaining > 0:
            n = min(remaining, len(chunk))
            f.write(chunk[:n])
            remaining -= n

    actual_mb = image_bytes / (1024 * 1024)
    print(f"Created: {path}")
    print(f"  {cyls} cyl × {heads} heads × {spt} spt × {sector_size} B")
    print(f"  {total_sectors:,} sectors, {actual_mb:.1f} MB")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "blank_40mb.nhd"
    size = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    make_nhd(path, size)
