"""
Icons, as the Toolbox draws them on a black-and-white screen (Inside
Macintosh I-473, V-71; More Macintosh Toolbox, chapter 5).

    ICON        32 by 32, 4 bytes a row: 128 bytes
    ICN#        an ICON and its mask (a list of ICONs, really)
    ics#, icm#  the same at 16 by 16 and 16 by 12, 2 bytes a row
    SICN        16 by 16 icons, one after another
    icl4, icl8  32 by 32 at 4 and 8 bits a pixel, in the standard
                colours; ics4, ics8, icm4, icm8 likewise smaller
    cicn        a CIcon: PixMap, mask BitMap, BitMap, then the mask's
                bits, the BitMap's bits, a colour table and the pixels

An icon here is its 1-bit image and mask as rows of bits, a row of
(width + 7) // 8 bytes.  Colour ones become 1-bit by how light their
colours are, when there's no 1-bit icon of the size to use instead.
"""

from __future__ import annotations

import struct

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QImage

from .color import STANDARD_4BIT, STANDARD_8BIT
from .pict import PictError, Reader, pixel_image, read_ctab, to_mono
from .qdraw import MONO_TABLE
from .types import Rect

# Transforms (IconTransformType): the low bits are one of these, and
# ttSelected can be added.  Labels (0x100-0x700) are colours, which
# don't show here.
ttNone, ttDisabled, ttOffline, ttOpen = 0, 1, 2, 3
ttSelected = 0x4000

# Alignments (IconAlignmentType): vertical in the low two bits,
# horizontal in the next two.
atNone = 0
atVerticalCenter, atTop, atBottom = 1, 2, 3
atHorizontalCenter, atLeft, atRight = 4, 8, 12

LARGE, SMALL, MINI = (32, 32), (16, 16), (16, 12)
# Each size's types, deepest last, and its IconSelectorValue bits.
SIZES = {LARGE: ("ICN#", "icl4", "icl8"), SMALL: ("ics#", "ics4", "ics8"),
         MINI: ("icm#", "icm4", "icm8")}
SELECTORS = {"ICN#": 0x1, "icl4": 0x2, "icl8": 0x4, "ics#": 0x100, "ics4": 0x200,
             "ics8": 0x400, "icm#": 0x10000, "icm4": 0x20000, "icm8": 0x40000}
ICON_TYPES = tuple(SELECTORS)
GRAY = (b"\xAA", b"\x55")


class Icon:
    """A 1-bit icon: rows of its image and of its mask (None if all of
    it shows); and, for a colour icon, its pixels in their colours."""

    def __init__(self, width: int, height: int, rows: list[bytes], mask: list[bytes] | None,
                 pixels: QImage | None = None):
        self.width = width
        self.height = height
        self.rows = rows
        self.mask = mask
        self.pixels = pixels

    @property
    def row_bytes(self) -> int:
        return (self.width + 7) // 8

    def transformed(self, transform: int) -> Icon:
        """ttSelected inverts it within its mask; ttDisabled greys it,
        leaving a 1 bit only where the grey pattern has one.  Either is
        drawn in black and white."""
        if not transform & (ttSelected | 0xFF):
            return self
        rows = self.rows
        mask = self.mask or [b"\xFF" * self.row_bytes] * self.height
        if transform & ttSelected:
            rows = [bytes(a ^ b for a, b in zip(r, m)) for r, m in zip(rows, mask)]
        if transform & 0xFF in (ttDisabled, ttOffline):
            rows = [bytes(a & GRAY[y & 1][0] for a in r) for y, r in enumerate(rows)]
        return Icon(self.width, self.height, rows, self.mask)

    def image(self, rows: list[bytes] | None = None) -> QImage:
        """The icon (or its mask) as an image, 1 bits black."""
        rb = self.row_bytes
        data = b"".join(r[:rb].ljust(rb, b"\0") for r in (rows or self.rows))
        img = QImage(data, self.width, self.height, rb, QImage.Format.Format_Mono)
        img.setColorTable(MONO_TABLE)
        return img.copy()

    def mask_image(self) -> QImage | None:
        return self.image(self.mask) if self.mask is not None else None


def _rows(data: bytes, row_bytes: int, height: int, at: int = 0) -> list[bytes]:
    return [data[at + y * row_bytes:at + (y + 1) * row_bytes].ljust(row_bytes, b"\0")
            for y in range(height)]


def bitmap_icon(data: bytes, size: tuple[int, int] = LARGE, masked: bool = True) -> Icon | None:
    """An ICON, an ICN#-style icon and mask, or a SICN's first icon (with
    the second as its mask, when there is a second)."""
    width, height = size
    rb = (width + 7) // 8
    n = rb * height
    if len(data) == 128 and size != LARGE:
        large_icon = bitmap_icon(data, LARGE, masked=False)
        if large_icon is None:
            return None
        img = large_icon.image().scaled(width, height, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
        return Icon(width, height, _mono_rows(img), None)
    if len(data) < n:
        return None
    mask = _rows(data, rb, height, n) if masked and len(data) >= 2 * n else None
    return Icon(width, height, _rows(data, rb, height), mask)


def _mono_rows(img: QImage) -> list[bytes]:
    mono = to_mono(img)
    rb = (mono.width() + 7) // 8
    out = []
    for y in range(mono.height()):
        line = mono.constScanLine(y)
        line.setsize(rb)
        out.append(bytes(line))
    return out


def color_icon(data: bytes, size: tuple[int, int], depth: int,
               mask: list[bytes] | None) -> Icon | None:
    """A 4- or 8-bit icon in the standard colours, made 1-bit."""
    width, height = size
    rb = width * depth // 8
    if len(data) < rb * height:
        return None
    img = pixel_image(_rows(data, rb, height), width, height, depth,
                      STANDARD_8BIT if depth == 8 else STANDARD_4BIT + [0xFF000000] * 240)
    return Icon(width, height, _mono_rows(img), mask,
                _mask_to_alpha(img, mask, size))


def _mask_to_alpha(img: QImage, mask: list[bytes] | None,
                 size: tuple[int, int]) -> QImage:
    """img as ARGB32, the bits where mask is clear made transparent;
    without a mask, img as ARGB32 with every pixel kept."""
    out = img.convertToFormat(QImage.Format.Format_ARGB32)
    if not mask:
        return out
    transparent = QColor(0, 0, 0, 0)
    width, height = size
    for y in range(min(height, len(mask))):
        mrow = mask[y]
        for x in range(width):
            if not (mrow[x >> 3] & (0x80 >> (x & 7))):
                out.setPixelColor(x, y, transparent)
    return out


def color_suite_icon(get, size: tuple[int, int], depth: int) -> Icon | None:
    """The deepest colour icon of a size in a suite, its pixels as an
    ARGB32 image with the 1-bit sibling's mask as its alpha; None when
    depth can't show colour or the suite has no colour member for it.
    At depth 8 and deeper the 8-bit member wins over the 4-bit one; at
    depth 4 the 4-bit member is the deepest one usable."""
    if depth <= 2:
        return None
    bw, four, eight = SIZES[size]
    candidates = ((eight, 8), (four, 4)) if depth >= 8 else ((four, 4),)
    one = get(bw)
    if not one and size == LARGE:
        one = get("ICON")
    masked = len(one) >= 2 * size[0] * size[1] // 8 if one else False
    masked_icon = bitmap_icon(one, size, masked=masked) if one else None
    mask = masked_icon.mask if masked_icon else None
    for kind, bits in candidates:
        data = get(kind)
        if not data:
            continue
        row_bytes = size[0] * bits // 8
        if len(data) < row_bytes * size[1]:
            continue
        table = STANDARD_8BIT if bits == 8 else STANDARD_4BIT + [0xFF000000] * 240
        img = pixel_image(_rows(data, row_bytes, size[1]), size[0], size[1],
                          bits, table)
        return Icon(size[0], size[1], _mono_rows(img), mask,
                    pixels=_mask_to_alpha(img, mask, size))
    return None


def suite_icon(get, size: tuple[int, int]) -> Icon | None:
    """The 1-bit icon of a size from a suite or a resource ID: get(type)
    gives a type's data, or None.  The 1-bit one is drawn if it's there,
    since that's what a black-and-white screen shows, else the deepest
    one, all of it (the mask is the 1-bit one's)."""
    bw, four, eight = SIZES[size]
    one = get(bw)
    if not one and size == LARGE:
        one = get("ICON")
    masked = len(one) >= 2 * size[0] * size[1] // 8 if one else False
    icon = bitmap_icon(one, size, masked=masked) if one else None
    if icon is not None:
        return icon
    for kind, depth in ((eight, 8), (four, 4)):
        data = get(kind)
        if data:
            return color_icon(data, size, depth, None)
    return None


# An icon suite is a handle of macrt's own making, as the real one is
# private: its label, how many icons, then each icon's type and handle.
SUITE_HEADER = 4            # label, count
SUITE_ENTRY = 8             # type, handle


def suite_entries(data: bytes | None) -> dict[str, int] | None:
    """A suite's icons, type -> handle, from what its handle holds; None
    if that's too short to be one."""
    if data is None or len(data) < SUITE_HEADER:
        return None
    count = struct.unpack_from(">H", data, 2)[0]
    out = {}
    for i in range(min(count, (len(data) - SUITE_HEADER) // SUITE_ENTRY)):
        kind, handle = struct.unpack_from(">4sL", data, SUITE_HEADER + SUITE_ENTRY * i)
        out[kind.decode("mac_roman")] = handle
    return out


def best_size(r: Rect, have) -> tuple[int, int] | None:
    """The size of icon to draw in r: the largest no bigger than r, of
    those have(size) says there are, else the closest there is."""
    side = min(r.width, r.height)
    wanted = LARGE if side >= 32 else SMALL if side >= 16 else MINI
    order = {LARGE: (LARGE, SMALL, MINI), SMALL: (SMALL, LARGE, MINI),
             MINI: (MINI, SMALL, LARGE)}[wanted]
    for size in order:
        if have(size):
            return size
    return None


def place(r: Rect, width: int, height: int, align: int) -> Rect:
    """Where an icon goes in r.  With no alignment it's stretched to r;
    with one it's scaled to fit, keeping its shape, and put at the side
    or centre of r the alignment says (centred on an axis it says
    nothing about)."""
    if align & 0xF == atNone or not width or not height:
        return r
    scale = min(r.width / width, r.height / height)
    w, h = max(round(width * scale), 1), max(round(height * scale), 1)
    v, hz = align & 3, align & 12
    top = (r.top if v == atTop else r.bottom - h if v == atBottom
           else r.top + (r.height - h) // 2)
    left = (r.left if hz == atLeft else r.right - w if hz == atRight
            else r.left + (r.width - w) // 2)
    return Rect(top, left, top + h, left + w)


# =========================================================================
# Colour icons (cicn)
# =========================================================================
CICON_HEADER = 82       # iconPMap, iconMask, iconBMap, iconData


class CIcon:
    """A cicn resource's parts: where each is in it, and its icon."""

    def __init__(self, data: bytes):
        if len(data) < CICON_HEADER:
            raise PictError("a cicn too short for its header")
        self.pm_row_bytes = struct.unpack_from(">H", data, 4)[0] & 0x3FFF
        self.bounds = Rect(*struct.unpack_from(">hhhh", data, 6))
        self.pixel_size = struct.unpack_from(">h", data, 32)[0]
        self.mask_row_bytes = struct.unpack_from(">H", data, 54)[0] & 0x3FFF
        self.mask_bounds = Rect(*struct.unpack_from(">hhhh", data, 56))
        self.bmap_row_bytes = struct.unpack_from(">H", data, 68)[0] & 0x3FFF
        self.bmap_bounds = Rect(*struct.unpack_from(">hhhh", data, 70))
        height = max(self.bounds.height, 0)
        self.mask_at = CICON_HEADER
        self.bmap_at = self.mask_at + self.mask_row_bytes * max(self.mask_bounds.height, 0)
        self.ctab_at = self.bmap_at + self.bmap_row_bytes * max(self.bmap_bounds.height, 0)
        r = Reader.of(data, self.ctab_at)
        self.ctab = read_ctab(r)
        self.pixels_at = r.pos
        self.pixels_end = self.pixels_at + self.pm_row_bytes * height
        self.data = data

    @property
    def mask(self) -> list[bytes]:
        return _rows(self.data, self.mask_row_bytes, self.mask_bounds.height, self.mask_at)

    def icon(self) -> Icon | None:
        """Its 1-bit BitMap through its mask, or, with no BitMap, its pixels
        made 1-bit; with its pixels as they are, if they're 1 to 8 bits."""
        width, height = self.bounds.width, self.bounds.height
        if width <= 0 or height <= 0 or width > 1024 or height > 1024:
            return None
        mask = self.mask if self.mask_row_bytes else None
        if mask is not None and (self.mask_bounds.width != width
                                 or self.mask_bounds.height != height):
            mask = None
        img = None
        if self.pixel_size in (1, 2, 4, 8) and self.pm_row_bytes:
            img = pixel_image(_rows(self.data, self.pm_row_bytes, height, self.pixels_at), width,
                              height, self.pixel_size, self.ctab)
        if self.bmap_row_bytes and self.bmap_bounds == self.bounds:
            return Icon(width, height, [r[:(width + 7) // 8].ljust((width + 7) // 8, b"\0")
                                        for r in _rows(self.data, self.bmap_row_bytes, height,
                                                       self.bmap_at)], mask, img)
        if img is None:
            return None
        return Icon(width, height, _mono_rows(img), mask, img)
