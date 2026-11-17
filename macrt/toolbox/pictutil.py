"""
The Picture Utilities' surveys (Imaging With QuickDraw, Pictures,
"Collecting Picture Information"): the colours, shapes, comments and fonts
in a picture, a pixel map or a bitmap, or in all of a number of them, and
the colours picked from them to show them in.

Colours are counted in a colour bank: exactly, until there are more than
256 of them or a 32-bit pixel map is added, and from then on to 5 bits a
component, as the Picture Utilities' 5-5-5 histogram counts them.  A 5-bit
component comes back as 16 bits by repeating it, as a 16-bit pixel's do.
A picture's colours are its bitmaps' and pixel maps' pixels and the
colours its RGBFgCol, RGBBkCol, HiliteColor and pixel pattern opcodes set,
one pixel each, as GetPictInfo counts them.

The popular method picks the colours used most.  The median method, "a
weighted distribution of colors", cuts the colours into boxes, each time
the box with the widest range of a component at the weighted median of
that component, and picks each box's average, the biggest boxes first.
"""

from __future__ import annotations

import struct
from collections import Counter
from dataclasses import dataclass

from PyQt6.QtGui import QImage

from . import pict
from .gdevice import ROW_BYTES_MASK, RGBPat, pixPat
from .masks import EXPAND
from .types import Rect

RGB = tuple[int, int, int]
BLACK: RGB = (0, 0, 0)
WHITE: RGB = (0xFFFF, 0xFFFF, 0xFFFF)
EXACT = 256                 # the most colours a bank counts exactly
FIXED_72 = 72 << 16
popularMethod, medianMethod = 1, 2
COUNTS = ("text", "line", "rect", "rRect", "oval", "arc", "poly", "region", "bitMap",
          "pixMap")
SHAPES = {0x30: "rect", 0x40: "rRect", 0x50: "oval", 0x60: "arc"}
WIDEN = [v << 11 | v << 6 | v << 1 | v >> 4 for v in range(32)]
SYSTEM_SIZE = 12            # what a text size of 0 is


@dataclass
class FontUse:
    """A font as a picture's text uses it: its name, if the picture gives
    one, the sizes (bit n set for n points, bit 0 for any above 127) and
    the styles."""
    name: str | None = None
    sizes: int = 0
    style: int = 0


def reduced(colors: Counter) -> Counter:
    """The colours to 5 bits a component."""
    out: Counter = Counter()
    for (r, g, b), n in colors.items():
        out[WIDEN[r >> 11], WIDEN[g >> 11], WIDEN[b >> 11]] += n
    return out


def table_colors(data: bytes) -> dict[int, RGB]:
    """A ColorTable record's colours by pixel value."""
    _seed, flags, size = struct.unpack_from(">lHh", data)
    n = max(0, min(size + 1, 256, (len(data) - 8) // 8))
    out = {}
    for i in range(n):
        value, r, g, b = struct.unpack_from(">HHHH", data, 8 + 8 * i)
        out[i if flags & 0x8000 else value & 0xFF] = (r, g, b)
    return out


def _values(row: bytes, depth: int, width: int) -> bytes:
    """A row's pixel values, a byte each."""
    if depth == 8:
        return row[:width]
    if depth == 1:
        return b"".join(EXPAND[b] for b in row[:(width + 7) // 8])[:width]
    per = 8 // depth
    out = bytearray(len(row) * per)
    for k, table in enumerate(pict.SPLIT[depth]):
        out[k::per] = row.translate(table)
    return bytes(out[:width])


def pixel_colors(rows: list[bytes], width: int, height: int, depth: int,
                 table: dict[int, RGB] | None, pack_type: int = 1,
                 cmp_count: int = 3) -> Counter:
    """How many pixels of each colour rows hold: 1- to 8-bit pixels by the
    table (a bitmap's, with none, white and black), 16- and 32-bit ones
    by their components, to 5 bits."""
    if depth <= 8:
        values: Counter = Counter()
        for row in rows[:height]:
            values.update(_values(row, depth, width))
        if table is None:
            table = {0: WHITE, 1: BLACK}
        out: Counter = Counter()
        for v, n in values.items():
            out[table.get(v, BLACK)] += n
        return out
    return reduced(image_colors(pict.pixel_image(rows, width, height, depth, None, pack_type,
                                                 cmp_count)))


def image_colors(img: QImage) -> Counter:
    """How many pixels of each colour an image has."""
    img = img.convertToFormat(QImage.Format.Format_RGB32)
    words = memoryview(img.constBits().asstring(img.sizeInBytes())).cast("I")
    per, width = img.bytesPerLine() // 4, img.width()
    argb: Counter = Counter()
    for y in range(img.height()):
        argb.update(words[y * per:y * per + width])
    out: Counter = Counter()
    for v, n in argb.items():
        out[(v >> 16 & 0xFF) * 0x101, (v >> 8 & 0xFF) * 0x101, (v & 0xFF) * 0x101] += n
    return out


def median_cut(colors: Counter, n: int) -> list[RGB]:
    """n colours spread over the colours as they're used."""
    def spread(box: list) -> tuple[int, int]:
        if len(box) < 2:
            return -1, 0
        return max((max(c[k] for c, _ in box) - min(c[k] for c, _ in box), k)
                   for k in range(3))

    boxes = [list(colors.items())]
    spreads = [spread(boxes[0])]
    while len(boxes) < n:
        i = max(range(len(boxes)), key=lambda j: spreads[j][0])
        width, k = spreads[i]
        if width < 0:
            break
        box = sorted(boxes[i], key=lambda e: e[0][k])
        total, run, cut = sum(w for _, w in box), 0, 0
        while cut < len(box) - 1:
            run += box[cut][1]
            cut += 1
            if 2 * run >= total:
                break
        halves = [box[:cut], box[cut:]]
        boxes[i:i + 1] = halves
        spreads[i:i + 1] = [spread(h) for h in halves]
    picked = []
    for box in sorted(boxes, key=lambda b: -sum(w for _, w in b)):
        total = sum(w for _, w in box)
        picked.append(tuple((sum(c[k] * w for c, w in box) + total // 2) // total
                            for k in range(3)))
    return picked


class Survey:
    """What's been seen in the pictures, pixel maps and bitmaps added."""

    def __init__(self) -> None:
        self.colors: Counter = Counter()
        self.banked = False                 # counted to 5 bits a component
        self.counts = dict.fromkeys(COUNTS, 0)
        self.comments: Counter = Counter()  # by kind, in the order first seen
        self.fonts: dict[int, FontUse] = {}
        self.h_res = self.v_res = 0
        self.depth = 0
        self.size = (0, 0)                  # the source rectangle's width and height

    # -- colours --------------------------------------------------------
    def add_colors(self, colors: Counter, deep: bool = False) -> None:
        if deep and not self.banked:
            self._bank()
        self.colors.update(reduced(colors) if self.banked else colors)
        if not self.banked and len(self.colors) > EXACT:
            self._bank()

    def _bank(self) -> None:
        self.banked = True
        self.colors = reduced(self.colors)

    def pick(self, n: int, method: int, keep_black_and_white: bool = True) -> list[RGB]:
        """Up to n of the colours, by the method: the popular method's
        (and so the system's) unless it's the median one."""
        colors = Counter(self.colors)
        if not keep_black_and_white:
            colors.pop(BLACK, None)
            colors.pop(WHITE, None)
        if method == medianMethod and len(colors) > n:
            return median_cut(colors, n)
        return [c for c, _ in colors.most_common(n)]

    # -- what's added -----------------------------------------------------
    def merge(self, other: Survey) -> None:
        self.add_colors(other.colors, other.banked)
        for k, v in other.counts.items():
            self.counts[k] += v
        self.comments.update(other.comments)
        for fid, use in other.fonts.items():
            mine = self.fonts.setdefault(fid, FontUse(use.name))
            mine.name = mine.name or use.name
            mine.sizes |= use.sizes
            mine.style |= use.style
        self.h_res, self.v_res = max(self.h_res, other.h_res), max(self.v_res, other.v_res)
        self.depth = max(self.depth, other.depth)
        self.size = (max(self.size[0], other.size[0]), max(self.size[1], other.size[1]))

    def _place(self, h_res: int, v_res: int, width: int, height: int, depth: int) -> None:
        self.h_res, self.v_res = max(self.h_res, h_res), max(self.v_res, v_res)
        self.size = (max(self.size[0], width), max(self.size[1], height))
        self.depth = max(self.depth, depth)

    def add_pixels(self, rows: list[bytes], width: int, height: int, depth: int,
                   table: dict[int, RGB] | None, pixmap: bool, pack_type: int = 1,
                   cmp_count: int = 3) -> None:
        """A bitmap's or pixel map's pixels."""
        self.counts["pixMap" if pixmap else "bitMap"] += 1
        self.depth = max(self.depth, depth)
        self.add_colors(pixel_colors(rows, width, height, depth, table, pack_type, cmp_count),
                        depth == 32)

    def add_pixmap(self, rows: list[bytes], width: int, height: int, depth: int,
                   table: dict[int, RGB] | None, pixmap: bool, h_res: int = FIXED_72,
                   v_res: int = FIXED_72) -> None:
        """A pixel map or bitmap in memory, its pixels unpacked."""
        self._place(h_res, v_res, width, height, depth)
        self.add_pixels(rows, width, height, depth, table, pixmap)

    def add_image_colors(self, colors: Counter, width: int, height: int, depth: int,
                         pixmap: bool) -> None:
        """The colours of a pixel map or bitmap as they're shown."""
        self._place(FIXED_72, FIXED_72, width, height, depth)
        self.counts["pixMap" if pixmap else "bitMap"] += 1
        self.depth = max(self.depth, depth)
        self.add_colors(colors, depth == 32)

    def add_picture(self, frame: Rect, r: pict.Reader) -> None:
        """A picture with this frame, its opcodes read from r.  PictError
        if it can't be read."""
        walk = _Walk(self)
        walk.walk(r)
        width, height = frame.width, frame.height
        h_res = v_res = FIXED_72
        if walk.header is not None:
            h_res, v_res, src = walk.header
            width, height = src.width, src.height
        self._place(h_res, v_res, max(width, 0), max(height, 0), max(walk.depth, 1))


class _Walk:
    """A picture's opcodes, read as DrawPicture reads them, for what they
    add to a survey."""

    def __init__(self, survey: Survey):
        self.survey = survey
        self.v2 = False
        self.font = self.size = self.face = 0
        self.names: dict[int, str] = {}
        self.header: tuple | None = None
        self.depth = 0

    def walk(self, r: pict.Reader) -> None:
        while True:
            if self.v2:
                r.align()
                op = r.u16()
            else:
                op = r.u8()
            if op == pict.END:
                return
            self.op(op, r)

    def op(self, op: int, r: pict.Reader) -> None:
        counts = self.survey.counts
        if op == 0x01:                      # ClipRgn
            _sized(r)
        elif 0x70 <= op <= 0x8F:            # a polygon or region, or the same again
            if not op & 8:
                _sized(r)
            if op & 7 < 5:
                counts["poly" if op < 0x80 else "region"] += 1
        elif op == 0x03:
            self.font = r.s16()
        elif op == 0x04:
            self.face = r.u8()
        elif op == 0x0D:
            self.size = r.s16()
        elif op in (0x02, 0x09, 0x0A, 0x10):
            r.read(8)
        elif op in (0x05, 0x08, 0x15, 0x16):
            r.read(2)
        elif op in (0x06, 0x07, 0x0B, 0x0C, 0x0E, 0x0F):
            r.read(4)
        elif op == 0x11:
            if r.u8() == 2:                 # $0011 $02FF
                r.u8()
                self.v2 = True
        elif 0x12 <= op <= 0x14:
            self._pix_pat(r)
        elif op in (0x1A, 0x1B, 0x1D):      # RGBFgCol, RGBBkCol, HiliteColor
            self.survey.add_colors(Counter({r.unpack(">HHH"): 1}))
        elif op == 0x1F:
            r.read(6)
        elif 0x20 <= op <= 0x23:
            r.read((8, 4, 6, 2)[op - 0x20])
            counts["line"] += 1
        elif 0x28 <= op <= 0x2B:
            r.read((4, 1, 1, 2)[op - 0x28])
            r.read(r.u8())
            self._text()
        elif op == 0x2C:                    # FontName: an ID and the font's name
            data = r.read(r.u16())
            if len(data) >= 3:
                self.names[struct.unpack_from(">h", data)[0]] = (
                    data[3:3 + data[2]].decode("mac_roman"))
        elif 0x30 <= op <= 0x6F:
            if not op & 8:
                r.read(8)
            if op >= 0x60:
                r.read(4)
            if op & 7 < 5:
                counts[SHAPES[op & 0xF0]] += 1
        elif op in pict.BITS_OPS:
            self._bits(op, r)
        elif op == 0xA0:
            self.survey.comments[r.u16()] += 1
        elif op == 0xA1:
            kind, size = r.unpack(">hH")
            r.read(size)
            self.survey.comments[kind] += 1
        elif op == 0x0C00:
            self._header(r.read(24))
        else:
            pict.skip(op, r)

    def _text(self) -> None:
        self.survey.counts["text"] += 1
        use = self.survey.fonts.setdefault(self.font, FontUse())
        use.name = use.name or self.names.get(self.font)
        size = self.size or SYSTEM_SIZE
        use.sizes |= 1 << (size if 0 < size < 128 else 0)
        use.style |= self.face

    def _header(self, data: bytes) -> None:
        """An extended version 2 header (-2): the resolution and the
        rectangle the picture was drawn at."""
        version, = struct.unpack_from(">h", data)
        if version == -2:
            h_res, v_res = struct.unpack_from(">ll", data, 4)
            src = Rect(*struct.unpack_from(">hhhh", data, 12))
            self.header = (h_res, v_res, src)

    def _pix_pat(self, r: pict.Reader) -> None:
        """A pixel pattern: its colour, or each of its pixel map's colours,
        once."""
        kind = r.u16()
        r.read(8)
        if kind == RGBPat:
            self.survey.add_colors(Counter({r.unpack(">HHH"): 1}))
        elif kind == pixPat:
            row_bytes = r.u16() & ROW_BYTES_MASK
            bounds = r.rect()
            pack_type, depth, cmp_count = pict.read_pixmap(
                pict.Reader.of(r.read(pict.PIXMAP_REST)))
            table = table_colors(pict.read_ctab_record(r))
            width, height = bounds.width, bounds.height
            pict.check_size(width, height, depth)
            rows = pict.read_pixels(r, row_bytes, height, True, pack_type, width, depth,
                                    cmp_count)
            colors = pixel_colors(rows, width, height, depth, table, pack_type, cmp_count)
            self.survey.add_colors(Counter(dict.fromkeys(colors, 1)), depth == 32)
            self.depth = max(self.depth, depth)

    def _bits(self, op: int, r: pict.Reader) -> None:
        direct = op in (0x9A, 0x9B)
        if direct:
            r.read(4)
        row_bytes = r.u16()
        bounds = r.rect()
        table = None
        pack_type, depth, cmp_count = 0, 1, 1
        pixmap = direct or bool(row_bytes & 0x8000)
        if pixmap:
            pack_type, depth, cmp_count = pict.read_pixmap(r)
            if not direct:
                table = table_colors(pict.read_ctab_record(r))
        row_bytes &= 0x3FFF
        r.read(18)                          # srcRect, dstRect, mode
        if op & 1:
            _sized(r)                       # the mask region
        width, height = bounds.width, bounds.height
        pict.check_size(width, height, depth)
        rows = pict.read_pixels(r, row_bytes, height, op >= 0x98, pack_type, width, depth,
                                cmp_count)
        if pixmap:
            self.depth = max(self.depth, depth)
        self.survey.add_pixels(rows, width, height, depth, table, pixmap, pack_type, cmp_count)


def _sized(r: pict.Reader) -> None:
    """Pass over a region or polygon: its size, then the rest of it."""
    size = r.u16()
    if size < 10:
        raise pict.PictError(f"a region or polygon of {size} bytes")
    r.read(size - 2)
