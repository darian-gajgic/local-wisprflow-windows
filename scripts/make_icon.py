#!/usr/bin/env python3
"""Generate assets/wisprflow.ico — the tray + shortcut icon.

Written as a generator rather than a committed binary blob so the icon can be regenerated or
restyled without a graphics editor, and reviewed as code. Pure standard library: no Pillow, so
it also runs during a bare-bones install.

The artwork matches the on-screen overlay: a rounded blue square with the same white waveform
bars, drawn with 3x supersampling for antialiased edges.

    python scripts/make_icon.py
"""
from __future__ import annotations

import struct
from pathlib import Path

SIZES = (16, 20, 24, 32, 48, 64, 128, 256)
SS = 3   # supersampling factor per axis

BG_TOP = (0x5B, 0x93, 0xFF)      # accent blue (top of the gradient)
BG_BOT = (0x2A, 0x5D, 0xD6)      # deeper blue (bottom)
BAR = (0xFF, 0xFF, 0xFF)

# Relative bar heights, mirrored around the centre — the same silhouette the overlay animates.
BARS = (0.38, 0.66, 1.00, 0.66, 0.38)


def _rounded_rect_contains(x: float, y: float, x0: float, y0: float,
                           x1: float, y1: float, r: float) -> bool:
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def render(size: int) -> bytearray:
    """Return `size`x`size` BGRA pixels, top row first."""
    n = size * SS
    pad = n * 0.055                      # a little breathing room inside the icon box
    radius = n * 0.22
    x0, y0, x1, y1 = pad, pad, n - pad, n - pad

    # bar geometry, centred
    count = len(BARS)
    bw = n * 0.088
    gap = n * 0.062
    total = count * bw + (count - 1) * gap
    bx0 = (n - total) / 2.0
    cy = n / 2.0
    max_bar_h = n * 0.52
    bar_r = bw / 2.0
    bars = []
    for i, h in enumerate(BARS):
        bh = max_bar_h * h
        left = bx0 + i * (bw + gap)
        bars.append((left, cy - bh / 2.0, left + bw, cy + bh / 2.0))
    # At 16px a five-bar waveform turns to mush; drop to the three tallest.
    if size <= 20:
        bars = bars[1:4]

    out = bytearray(size * size * 4)
    samples = SS * SS
    for py in range(size):
        for px in range(size):
            r_acc = g_acc = b_acc = a_acc = 0
            for sy in range(SS):
                y = py * SS + sy + 0.5
                for sx in range(SS):
                    x = px * SS + sx + 0.5
                    if not _rounded_rect_contains(x, y, x0, y0, x1, y1, radius):
                        continue
                    t = (y - y0) / max(1.0, (y1 - y0))
                    cr = int(BG_TOP[0] + (BG_BOT[0] - BG_TOP[0]) * t)
                    cg = int(BG_TOP[1] + (BG_BOT[1] - BG_TOP[1]) * t)
                    cb = int(BG_TOP[2] + (BG_BOT[2] - BG_TOP[2]) * t)
                    for (bx_0, by_0, bx_1, by_1) in bars:
                        if _rounded_rect_contains(x, y, bx_0, by_0, bx_1, by_1, bar_r):
                            cr, cg, cb = BAR
                            break
                    r_acc += cr
                    g_acc += cg
                    b_acc += cb
                    a_acc += 255
            i = (py * size + px) * 4
            if a_acc:
                cover = a_acc / (255 * samples)          # fraction of the pixel that is covered
                out[i + 0] = int(b_acc / samples / cover)   # B  (un-premultiply)
                out[i + 1] = int(g_acc / samples / cover)   # G
                out[i + 2] = int(r_acc / samples / cover)   # R
                out[i + 3] = int(a_acc / samples)           # A
    return out


def _bmp_image(size: int, bgra: bytearray) -> bytes:
    """One ICO entry as a 32-bit BITMAPINFOHEADER DIB (bottom-up) + an AND mask.

    The AND mask is legacy but not optional: Windows still reads it in some code paths, and
    omitting it makes icons render with black boxes on older shells.
    """
    header = struct.pack("<IiiHHIIiiII",
                         40,            # biSize
                         size,          # biWidth
                         size * 2,      # biHeight — XOR bitmap + AND mask stacked
                         1, 32, 0,      # planes, bit count, compression (BI_RGB)
                         size * size * 4, 0, 0, 0, 0)
    rows = []
    for y in range(size - 1, -1, -1):    # DIBs are stored bottom-up
        off = y * size * 4
        rows.append(bytes(bgra[off:off + size * 4]))
    xor = b"".join(rows)

    stride = ((size + 31) // 32) * 4     # 1bpp rows padded to 4 bytes
    mask_rows = []
    for y in range(size - 1, -1, -1):
        row = bytearray(stride)
        for x in range(size):
            if bgra[(y * size + x) * 4 + 3] == 0:        # fully transparent -> mask bit set
                row[x // 8] |= 0x80 >> (x % 8)
        mask_rows.append(bytes(row))
    return header + xor + b"".join(mask_rows)


def build(path: Path) -> Path:
    images = []
    for s in SIZES:
        images.append((s, _bmp_image(s, render(s))))
        print(f"  rendered {s}x{s}")

    out = bytearray(struct.pack("<HHH", 0, 1, len(images)))   # ICONDIR
    offset = 6 + 16 * len(images)
    for s, data in images:
        out += struct.pack("<BBBBHHII",
                           0 if s == 256 else s, 0 if s == 256 else s,   # 0 means 256
                           0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _s, data in images:
        out += data

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


if __name__ == "__main__":
    target = Path(__file__).resolve().parent.parent / "assets" / "wisprflow.ico"
    print(f"generating {target} ...")
    build(target)
    print(f"wrote {target} ({target.stat().st_size} bytes)")
