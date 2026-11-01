"""
Colours, as both kinds of Mac have them.  On a black-and-white one
there's no Color QuickDraw (ROM85 says so), but programs written for both
ask about colours anyway: an RGB colour becomes the classic colour it
would draw as on a 1-bit screen, black or white by how light it is.  On a
Mac II, colour tables are the screen's and the standard ones for each
depth (gdevice has the devices that use them).

    ColorTable  ctSeed (long), ctFlags, ctSize (entries - 1), then each
                entry's value and red, green, blue (Inside Macintosh V, the Color Manager)
"""

from __future__ import annotations

import struct

from .constants import (blackColor, blueColor, cyanColor, greenColor, magentaColor,
                        redColor, whiteColor, yellowColor)

WHITE = (0xFFFF, 0xFFFF, 0xFFFF)
BLACK = (0, 0, 0)

# The eight classic colours (Inside Macintosh I-158) as Color QuickDraw
# gives them in RGB.
CLASSIC_RGB = {
    blackColor: BLACK, whiteColor: WHITE,
    redColor: (0xDD6B, 0x08C2, 0x06A2),
    greenColor: (0x0000, 0x8000, 0x11B0),
    blueColor: (0x0000, 0x0000, 0xD400),
    cyanColor: (0x0241, 0xAB54, 0xEAFF),
    magentaColor: (0xF2D7, 0x0856, 0x84EC),
    yellowColor: (0xFC00, 0xF37D, 0x052F),
}

CT_DEVICE = 0x8000          # ctFlags: entries are in pixel value order, values ignored
# ctFlags: entries are in pixel value order, each value an entry of the
# current port's palette, whose colour the pixel value is.
CT_PALETTE = 0x4000


def luminance(r: int, g: int, b: int) -> int:
    """How light an RGB colour is, 0 to $FFFF, by the NTSC weights."""
    return (299 * (r & 0xFFFF) + 587 * (g & 0xFFFF) + 114 * (b & 0xFFFF)) // 1000


def is_light(r: int, g: int, b: int) -> bool:
    return luminance(r, g, b) >= 0x8000


def rgb_color(r: int, g: int, b: int) -> int:
    """The classic colour an RGB colour draws as on a 1-bit screen."""
    return whiteColor if is_light(r, g, b) else blackColor


def color_rgb(color: int) -> tuple[int, int, int]:
    """The RGB of a classic colour (GetForeColor in a plain GrafPort)."""
    rgb = CLASSIC_RGB.get(color)
    if rgb is None:
        rgb = BLACK if color & 1 else WHITE
    return rgb


def color_table(colors: list[tuple[int, int, int]], seed: int = 0, flags: int = 0) -> bytes:
    """A ColorTable record for colors, by pixel value."""
    out = struct.pack(">lHh", seed, flags, len(colors) - 1)
    for value, (r, g, b) in enumerate(colors):
        out += struct.pack(">HHHH", value, r, g, b)
    return out


def standard_table(ct_id: int) -> bytes | None:
    """The standard colour tables GetCTable knows without a clut: 1, 4
    and 8 bits of the standard colours and 33, 34, 36 and 40, the greys
    (32 + depth).  A table's seed is its ID.  None for any other."""
    if ct_id in (1, 33):
        return color_table([WHITE, BLACK], ct_id)
    if ct_id in (34, 36, 40):
        n = 1 << (ct_id - 32)
        grays = [0xFFFF - i * 0xFFFF // (n - 1) for i in range(n)]
        return color_table([(v, v, v) for v in grays], ct_id)
    if ct_id in (4, 8):
        return color_table([((c >> 16 & 0xFF) * 0x101, (c >> 8 & 0xFF) * 0x101,
                             (c & 0xFF) * 0x101)
                            for c in (STANDARD_4BIT if ct_id == 4 else STANDARD_8BIT)], ct_id)
    return None


def highlight_table(ct_id: int, hilite: tuple[int, int, int]) -> bytes | None:
    """GetCTable's 66, 68 and 72 (64 + depth), the standard colours with the
    highlight colour added (Inside Macintosh: Imaging With QuickDraw's
    GetCTable; Advanced Color Imaging's default colour tables): of 2 bits,
    white, 50% grey, the highlight colour and black; of 4 and 8, the
    standard colours, with the one closest to the highlight colour, between
    white and black, averaged with it if it's more than $3000 off in any
    component.  Which is closest is macrt's own: the one whose furthest-off
    component is nearest.  None for any other ID."""
    if ct_id == 66:
        return color_table([WHITE, (0x7FFF,) * 3, hilite, BLACK], ct_id)
    if ct_id not in (68, 72):
        return None
    data = bytearray(standard_table(ct_id - 64))
    struct.pack_into(">l", data, 0, ct_id)
    count = struct.unpack_from(">h", data, 6)[0] + 1
    colors = [struct.unpack_from(">HHH", data, 10 + 8 * i) for i in range(count)]

    def off(i: int) -> int:
        return max(abs(a - b) for a, b in zip(colors[i], hilite))
    near = min(range(1, count - 1), key=off)
    if off(near) > 0x3000:
        struct.pack_into(">HHH", data, 10 + 8 * near,
                         *((a + b) // 2 for a, b in zip(colors[near], hilite)))
    return bytes(data)


def _standard_8bit() -> list[int]:
    """The Mac's standard 256 colours, as 0xAARRGGBB: a 6x6x6 cube from
    white, then ramps of red, green, blue and grey without the cube's
    levels, then black."""
    cube = (0xFF, 0xCC, 0x99, 0x66, 0x33, 0x00)
    ramp = (0xEE, 0xDD, 0xBB, 0xAA, 0x88, 0x77, 0x55, 0x44, 0x22, 0x11)
    out = [(r << 16) | (g << 8) | b for r in cube for g in cube for b in cube][:215]
    out += [v << 16 for v in ramp] + [v << 8 for v in ramp] + list(ramp)
    out += [(v << 16) | (v << 8) | v for v in ramp] + [0]
    return [0xFF000000 | c for c in out]


# The standard 16 colours: white, yellow, orange, red, magenta, purple,
# blue, cyan, green, dark green, brown, tan, light, medium and dark grey,
# black.
STANDARD_4BIT = [0xFF000000 | c for c in (
    0xFFFFFF, 0xFCF305, 0xFF6402, 0xDD0806, 0xF20884, 0x4600A5, 0x0000D4, 0x02ABEA,
    0x1FB714, 0x006411, 0x562C05, 0x90713A, 0xC0C0C0, 0x808080, 0x404040, 0x000000)]
STANDARD_8BIT = _standard_8bit()
