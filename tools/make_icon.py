"""Draws the app icon — two folders inside a cycle of arrows — and writes the PNGs.

Run it from the repository root when the icon needs to change:

    python tools/make_icon.py

Everything is stdlib: the shapes are point-in-shape tests over a supersampled
canvas, and the PNG is assembled by hand from zlib-compressed scanlines. The
result lands in ``dirsynk/ui/resources/`` and is committed, so nobody needs to
run this to run the app.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

DESIGN = 512.0  # the coordinates below are all in this square, y downwards
RENDER = 2048  # rendered here, then box-averaged down: that is the anti-aliasing
SIZES = (512, 256, 128, 64, 32)
OUT_DIR = Path(__file__).resolve().parent.parent / "dirsynk" / "ui" / "resources"

TOP_BLUE = (0x4C, 0x9A, 0xFF)
BOTTOM_BLUE = (0x1B, 0x4B, 0xC8)
FOLDER = (0xFF, 0xFF, 0xFF)
FOLDER_FRONT = (0xC7, 0xDD, 0xF8)
ARROW = (0xFF, 0xC9, 0x47)

CORNER = 112.0
RING = (256.0, 256.0, 204.0, 170.0, 16.0)  # cx, cy, rx, ry, half-thickness
HEAD_LENGTH, HEAD_HALF_WIDTH = 44.0, 34.0
FOLDERS = ((88.0, 198.0), (276.0, 198.0))  # top-left corner of each
FOLDER_W, FOLDER_H, TAB_H = 148.0, 116.0, 22.0


def in_rounded_rect(x, y, x0, y0, x1, y1, r):
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    dx = max(x0 + r - x, 0.0, x - (x1 - r))
    dy = max(y0 + r - y, 0.0, y - (y1 - r))
    return dx * dx + dy * dy <= r * r


def in_triangle(x, y, a, b, c):
    def side(p, q):
        return (q[0] - p[0]) * (y - p[1]) - (q[1] - p[1]) * (x - p[0])

    s1, s2, s3 = side(a, b), side(b, c), side(c, a)
    return not ((s1 < 0 or s2 < 0 or s3 < 0) and (s1 > 0 or s2 > 0 or s3 > 0))


def arc_point(theta_deg):
    cx, cy, rx, ry, _ = RING
    t = math.radians(theta_deg)
    return cx + rx * math.cos(t), cy + ry * math.sin(t)


def arc_tangent(theta_deg):
    """Unit vector in the direction of travel as theta increases."""
    _, _, rx, ry, _ = RING
    t = math.radians(theta_deg)
    tx, ty = -rx * math.sin(t), ry * math.cos(t)
    length = math.hypot(tx, ty)
    return tx / length, ty / length


def arrow_head(theta_deg):
    """The three corners of the head that closes an arc at ``theta_deg``."""
    px, py = arc_point(theta_deg)
    tx, ty = arc_tangent(theta_deg)
    nx, ny = -ty, tx
    tip = (px + tx * HEAD_LENGTH, py + ty * HEAD_LENGTH)
    return (
        tip,
        (px + nx * HEAD_HALF_WIDTH, py + ny * HEAD_HALF_WIDTH),
        (px - nx * HEAD_HALF_WIDTH, py - ny * HEAD_HALF_WIDTH),
    )


ARCS = ((190.0, 350.0), (10.0, 170.0))  # over the top to the right, under to the left
HEADS = (arrow_head(350.0), arrow_head(170.0))


def in_arcs(x, y):
    cx, cy, rx, ry, half = RING
    ux, uy = (x - cx) / rx, (y - cy) / ry
    f = ux * ux + uy * uy - 1.0
    gx, gy = 2.0 * ux / rx, 2.0 * uy / ry
    gradient = math.hypot(gx, gy)
    if gradient == 0.0 or abs(f) / gradient > half:
        return False
    theta = math.degrees(math.atan2(uy, ux)) % 360.0
    return any(start <= theta <= end for start, end in ARCS)


def in_folder(x, y, x0, y0):
    x1, y1 = x0 + FOLDER_W, y0 + FOLDER_H
    body = in_rounded_rect(x, y, x0, y0 + TAB_H, x1, y1, 15.0)
    tab = in_rounded_rect(x, y, x0, y0, x0 + FOLDER_W * 0.5, y0 + TAB_H + 15.0, 9.0)
    return body or tab


def in_folder_front(x, y, x0, y0):
    return in_rounded_rect(x, y, x0, y0 + TAB_H + 22.0, x0 + FOLDER_W, y0 + FOLDER_H, 15.0)


def colour_at(x, y):
    """The design, sampled at one point: (r, g, b, a)."""
    if not in_rounded_rect(x, y, 0.0, 0.0, DESIGN, DESIGN, CORNER):
        return (0, 0, 0, 0)
    for x0, y0 in FOLDERS:
        if in_folder_front(x, y, x0, y0):
            return (*FOLDER_FRONT, 255)
        if in_folder(x, y, x0, y0):
            return (*FOLDER, 255)
    if in_arcs(x, y) or any(in_triangle(x, y, *head) for head in HEADS):
        return (*ARROW, 255)
    t = y / DESIGN
    return (*(round(a + (b - a) * t) for a, b in zip(TOP_BLUE, BOTTOM_BLUE, strict=True)), 255)


def render():
    """The whole design at RENDER x RENDER, as a flat list of RGBA tuples."""
    step = DESIGN / RENDER
    offset = step / 2.0
    pixels = []
    for row in range(RENDER):
        y = row * step + offset
        pixels.extend(colour_at(col * step + offset, y) for col in range(RENDER))
    return pixels


def downsample(pixels, size):
    """Box-average the render down to ``size``; premultiplied so edges stay clean."""
    block = RENDER // size
    area = block * block
    out = bytearray()
    for row in range(size):
        for col in range(size):
            r = g = b = a = 0
            for dy in range(block):
                base = (row * block + dy) * RENDER + col * block
                for dx in range(block):
                    pr, pg, pb, pa = pixels[base + dx]
                    r += pr * pa
                    g += pg * pa
                    b += pb * pa
                    a += pa
            if a:
                out += bytes((round(r / a), round(g / a), round(b / a), round(a / area)))
            else:
                out += b"\0\0\0\0"
    return bytes(out)


def write_png(path, size, rgba):
    raw = b"".join(b"\0" + rgba[row * size * 4 : (row + 1) * size * 4] for row in range(size))

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pixels = render()
    for size in SIZES:
        path = OUT_DIR / f"icon-{size}.png"
        write_png(path, size, downsample(pixels, size))
        print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
