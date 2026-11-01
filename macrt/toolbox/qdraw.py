"""
QuickDraw's drawing: lines, shapes, regions, polygons, text and CopyBits.

A port draws onto one of two kinds of surface.  A window's is its
Backing, the image its view on the host shows.  Any other port's is its
portBits: a 1-bit bitmap in guest memory, as on a Mac, so an application
can draw offscreen and CopyBits the result into a window, or read the
bits itself; a cGrafPort's is its PixMap, of as many bits a pixel as
that has.  Either way the drawing is done with a QPainter, clipped to
the port's portRect, visRgn and clipRgn, with QuickDraw's patterns and
transfer modes.  A cGrafPort draws in its RGB colours, as near as the
screen has them, and with its pixel patterns.

Regions are real QuickDraw regions: the region data in guest memory is
read into a Region (rows of spans) and written back, so the region
calls are exact and an application can walk the data itself.

Text is drawn in host fonts standing in for the Mac's, by family and
size, in the port's style and transfer mode.
"""

from __future__ import annotations

import copy
import re
import struct
import sys
from contextlib import contextmanager
from functools import lru_cache
from itertools import accumulate
from typing import TYPE_CHECKING, Callable, Iterator

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontDatabase, QFontMetrics, QImage, QPainter,
                         QPainterPath, QPolygonF, QRegion)

from ..memory import ADDRESS_MASK, RAM
from . import constants as k
from . import lowmem
from .bitfont import AppFonts, style_offsets
from .constants import (blackColor, blueColor, cyanColor, greenColor, magentaColor, redColor,
                        whiteColor, yellowColor)
from . import pixels
from .color import (CLASSIC_RGB, CT_DEVICE, STANDARD_4BIT, STANDARD_8BIT, color_rgb, is_light,
                    luminance, rgb_color)
from .gdevice import (DEPTH_MODES, PIXMAP_FLAG, ROW_BYTES_MASK, GDevice, PixMap, PixPat, RGBPat,
                      deref, pixPat, read_table)
from .memmgr import Heap
from .pixels import BLACK_RGB, MONO_TABLE, WHITE_RGB
from .quickdraw import BLACK_PAT, GrafPort
from .types import Point, Rect

if TYPE_CHECKING:
    from .system import System

# Transfer modes
srcCopy, srcOr, srcXor, srcBic, notSrcCopy, notSrcOr, notSrcXor, notSrcBic = range(8)
patCopy, patOr, patXor, patBic, notPatCopy, notPatOr, notPatXor, notPatBic = range(8, 16)

# Style bits (txFace)
bold, italic, underline, outline, shadow, condense, extend = (1 << i for i in range(7))

MAX_BITMAP_ROWS = 4096
MAX_PATTERN_SIDE = 64
TRANSPARENT = 36            # an arithmetic mode: all but the background colour
DITHER_COPY = 64            # added to a mode: dither the colours
EMPTY = Rect(0, 0, 0, 0)
RGN_END = 0x7FFF

# The classic colour an RGB colour is in a GrafPort with Color QuickDraw:
# the one the top bits of its red, green and blue make (Inside Macintosh
# V-68).
TOP_BITS = {(0, 0, 0): blackColor, (1, 1, 1): whiteColor, (1, 0, 0): redColor,
            (0, 1, 0): greenColor, (0, 0, 1): blueColor, (0, 1, 1): cyanColor,
            (1, 0, 1): magentaColor, (1, 1, 0): yellowColor}


def port_rgb(port: GrafPort | None, fore: bool) -> tuple[int, int, int]:
    """GetForeColor's or GetBackColor's: a cGrafPort's colour, or the RGB
    of a GrafPort's classic one."""
    if port is None:
        return color_rgb(blackColor if fore else whiteColor)
    if port.is_color:
        return port.rgb_fg_color if fore else port.rgb_bk_color
    return color_rgb(port.fg_color if fore else port.bk_color)


# =========================================================================
# Regions
# =========================================================================
def _spans_op(a: tuple, b: tuple, op: Callable[[bool, bool], bool]) -> tuple:
    """Combine two rows of spans, each a sorted tuple of boundaries
    (start, end, start, end...)."""
    if not a and not b:
        return ()
    sa, sb = set(a), set(b)
    out = []
    ina = inb = inside = False
    for x in sorted(sa | sb):
        if x in sa:
            ina = not ina
        if x in sb:
            inb = not inb
        now = op(ina, inb)
        if now != inside:
            out.append(x)
            inside = now
    return tuple(out)


class Region:
    """A region as bands of rows with the same spans:
    [(top, bottom, (left, right, left, right...)), ...], top to bottom,
    no two adjacent bands alike."""

    __slots__ = ("bands",)

    def __init__(self, bands=()):
        self.bands = list(bands)

    @classmethod
    def rect(cls, r: Rect) -> Region:
        return cls() if r.empty else cls([(r.top, r.bottom, (r.left, r.right))])

    @property
    def empty(self) -> bool:
        return not self.bands

    @property
    def bbox(self) -> Rect:
        bands = self.bands
        if not bands:
            return EMPTY
        if len(bands) == 1:
            t, b, xs = bands[0]
            return Rect(t, xs[0], b, xs[-1])
        return Rect(bands[0][0], min(b[2][0] for b in bands),
                    bands[-1][1], max(b[2][-1] for b in bands))

    @property
    def is_rect(self) -> bool:
        return len(self.bands) == 1 and len(self.bands[0][2]) == 2

    def __eq__(self, other) -> bool:
        return isinstance(other, Region) and self.bands == other.bands

    def __repr__(self) -> str:
        return f"Region({self.bands!r})"

    def offset(self, dh: int, dv: int) -> Region:
        return Region([(t + dv, b + dv, tuple(x + dh for x in xs)) for t, b, xs in self.bands])

    def contains(self, pt: Point) -> bool:
        for t, b, xs in self.bands:
            if t <= pt.v < b:
                return any(xs[i] <= pt.h < xs[i + 1] for i in range(0, len(xs), 2))
        return False

    def _combine(self, other: Region, op) -> Region:
        ys = sorted({y for t, b, _ in self.bands for y in (t, b)}
                    | {y for t, b, _ in other.bands for y in (t, b)})
        out: list = []
        ia = ib = 0
        a, b = self.bands, other.bands
        for top, bottom in zip(ys, ys[1:]):
            while ia < len(a) and a[ia][1] <= top:
                ia += 1
            while ib < len(b) and b[ib][1] <= top:
                ib += 1
            xa = a[ia][2] if ia < len(a) and a[ia][0] <= top else ()
            xb = b[ib][2] if ib < len(b) and b[ib][0] <= top else ()
            xs = _spans_op(xa, xb, op)
            if not xs:
                continue
            if out and out[-1][1] == top and out[-1][2] == xs:
                out[-1] = (out[-1][0], bottom, xs)
            else:
                out.append((top, bottom, xs))
        return Region(out)

    def union(self, other: Region) -> Region:
        return self._combine(other, lambda x, y: x or y)

    def sect(self, other: Region) -> Region:
        a, b = self.bands, other.bands
        if len(a) == 1 and len(b) == 1 and len(a[0][2]) == 2 and len(b[0][2]) == 2:
            (t1, b1, (l1, r1)), (t2, b2, (l2, r2)) = a[0], b[0]
            t, bt, l, r = max(t1, t2), min(b1, b2), max(l1, l2), min(r1, r2)
            return Region([(t, bt, (l, r))]) if t < bt and l < r else Region()
        return self._combine(other, lambda x, y: x and y)

    def diff(self, other: Region) -> Region:
        return self._combine(other, lambda x, y: x and not y)

    def xor(self, other: Region) -> Region:
        return self._combine(other, lambda x, y: x != y)

    def inset(self, dh: int, dv: int) -> Region:
        """InsetRgn: shrink by dh and dv (grow, for negative ones)."""
        r = self
        if dh:
            out = []
            for t, b, xs in r.bands:
                row = Region()
                for i in range(0, len(xs), 2):
                    l, rt = (xs[i] + dh, xs[i + 1] - dh)
                    if l < rt:
                        row = row.union(Region([(t, b, (l, rt))]))
                out.extend(row.bands)
            r = Region(_normalize(out))
        for _ in range(abs(dv)):
            up, down = r.offset(0, -1), r.offset(0, 1)
            r = r.sect(up).sect(down) if dv > 0 else r.union(up).union(down)
        return r

    def to_qregion(self) -> QRegion:
        q = QRegion()
        rects = [QRect(xs[i], t, xs[i + 1] - xs[i], b - t)
                 for t, b, xs in self.bands for i in range(0, len(xs), 2)]
        if rects:
            q.setRects(rects)
        return q

    def rects(self) -> Iterator[Rect]:
        for t, b, xs in self.bands:
            for i in range(0, len(xs), 2):
                yield Rect(t, xs[i], b, xs[i + 1])

    # -- QuickDraw's region data ------------------------------------------
    @classmethod
    def from_data(cls, data: bytes) -> Region:
        if len(data) < 10:
            return cls()
        size = struct.unpack_from(">H", data)[0]
        bbox = Rect(*struct.unpack_from(">hhhh", data, 2))
        if size <= 10 or len(data) <= 10:
            return cls.rect(bbox)
        words = struct.unpack_from(f">{(min(size, len(data)) - 10) // 2}h", data, 10)
        bands: list = []
        cur: set[int] = set()
        prev_v = None
        i = 0
        while i < len(words):
            v = words[i]
            i += 1
            if v == RGN_END:
                break
            # an odd number of inversion points leaves the last unpaired:
            # it's left out
            xs = tuple(sorted(cur))[:len(cur) & ~1]
            if prev_v is not None and xs:
                bands.append((prev_v, v, xs))
            while i < len(words) and words[i] != RGN_END:
                cur ^= {words[i]}
                i += 1
            i += 1
            prev_v = v
        return cls(_normalize(bands))

    def to_data(self) -> bytes:
        bbox = self.bbox
        if self.is_rect or self.empty:
            return struct.pack(">Hhhhh", 10, *bbox)
        words: list[int] = []

        def emit(v: int, xs) -> None:
            if xs:
                words.append(v)
                words.extend(sorted(xs))
                words.append(RGN_END)

        prev: tuple = ()
        prev_bottom = None
        for t, b, xs in self.bands:
            if prev_bottom is not None and t != prev_bottom:
                emit(prev_bottom, prev)
                prev = ()
            emit(t, set(prev) ^ set(xs))
            prev, prev_bottom = xs, b
        emit(prev_bottom, prev)
        words.append(RGN_END)
        body = struct.pack(f">{len(words)}h", *words)
        return struct.pack(">Hhhhh", 10 + len(body), *bbox) + body

    # -- from shapes --------------------------------------------------------
    @classmethod
    def from_path(cls, path: QPainterPath) -> Region:
        """The pixels a path fills, as QuickDraw would paint them."""
        box = path.boundingRect().toAlignedRect().adjusted(-1, -1, 1, 1)
        if box.isEmpty() or box.width() > 8192 or box.height() > 8192:
            return cls()
        img = QImage(box.width(), box.height(), QImage.Format.Format_Grayscale8)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.translate(-box.x(), -box.y())
        p.fillPath(path, QColor(255, 255, 255))
        p.end()
        return cls.from_mask(img, box.x(), box.y())

    @classmethod
    def from_mask(cls, img: QImage, left: int, top: int) -> Region:
        """The non-zero pixels of a Grayscale8 image placed at (left, top)."""
        bpl = img.bytesPerLine()
        ptr = img.constBits()
        ptr.setsize(img.sizeInBytes())
        raw = bytes(ptr)
        bands: list = []
        for y in range(img.height()):
            row = raw[y * bpl:y * bpl + img.width()]
            xs: list[int] = []
            for m in re.finditer(rb"[^\x00]+", row):
                xs += (left + m.start(), left + m.end())
            xs_t = tuple(xs)
            v = top + y
            if bands and bands[-1][1] == v and bands[-1][2] == xs_t:
                bands[-1] = (bands[-1][0], v + 1, xs_t)
            elif xs_t:
                bands.append((v, v + 1, xs_t))
        return cls(bands)


def _normalize(bands: list) -> list:
    out: list = []
    for t, b, xs in sorted(bands):
        if not xs or b <= t:
            continue
        if out and out[-1][1] == t and out[-1][2] == xs:
            out[-1] = (out[-1][0], b, xs)
        else:
            out.append((t, b, xs))
    return out


def read_rgn(mem: RAM, h: int) -> Region:
    """The region a RgnHandle holds; empty for NIL or a purged handle."""
    if not h:
        return Region()
    p = mem.r32(h) & ADDRESS_MASK
    if not p:
        return Region()
    size = mem.r16(p)
    if size <= 10:
        return Region.rect(mem.r_rect(p + 2))
    return Region.from_data(mem.read(p, min(size, 0x10000)))


def write_rgn(heap: Heap, h: int, rgn: Region) -> bool:
    data = rgn.to_data()
    if heap.handle_size(h) != len(data) and not heap.set_handle_size(h, len(data)):
        return False
    heap.mem.write(heap.deref(h), data)
    return True


# =========================================================================
# Shapes as paths
# =========================================================================
def rect_path(r: Rect) -> QPainterPath:
    path = QPainterPath()
    path.addRect(QRectF(r.left, r.top, r.width, r.height))
    return path


def oval_path(r: Rect) -> QPainterPath:
    path = QPainterPath()
    path.addEllipse(QRectF(r.left, r.top, r.width, r.height))
    return path


def round_rect_path(r: Rect, oval_w: int, oval_h: int) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(QRectF(r.left, r.top, r.width, r.height),
                        max(min(oval_w, r.width), 0) / 2, max(min(oval_h, r.height), 0) / 2)
    return path


def arc_path(r: Rect, start: int, arc: int) -> QPainterPath:
    """A wedge of the oval in r.  QuickDraw's angles go clockwise from
    12 o'clock; Qt's anticlockwise from 3."""
    path = QPainterPath()
    box = QRectF(r.left, r.top, r.width, r.height)
    path.moveTo(box.center())
    path.arcTo(box, 90 - start, -arc)
    path.closeSubpath()
    return path


def poly_path(points: list[Point]) -> QPainterPath:
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.OddEvenFill)
    if len(points) >= 2:
        path.addPolygon(QPolygonF([QPointF(pt.h, pt.v) for pt in points]))
        path.closeSubpath()
    return path


def frame_path(shape: Callable[[Rect], QPainterPath], r: Rect, pen: Point) -> QPainterPath:
    """The outline of a shape, pen.h wide and pen.v high, inside r."""
    outer = shape(r)
    inner_r = r.inset(pen.h, pen.v)
    if inner_r.empty:
        return outer
    return outer.subtracted(shape(inner_r))


def line_path(a: Point, b: Point, pen: Point) -> QPainterPath:
    """What the pen covers going from a to b: its rectangle at every point."""
    w, h = pen.h, pen.v
    path = QPainterPath()
    if w <= 0 or h <= 0:
        return path
    if a.h == b.h or a.v == b.v:
        top, left = min(a.v, b.v), min(a.h, b.h)
        path.addRect(QRectF(left, top, abs(b.h - a.h) + w, abs(b.v - a.v) + h))
        return path
    corners = [(a.h, a.v), (a.h + w, a.v), (a.h + w, a.v + h), (a.h, a.v + h),
               (b.h, b.v), (b.h + w, b.v), (b.h + w, b.v + h), (b.h, b.v + h)]
    hull = _convex_hull(corners)
    path.addPolygon(QPolygonF([QPointF(x, y) for x, y in hull]))
    path.closeSubpath()
    return path


def _convex_hull(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    pts = sorted(set(points))

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def qrect(r: Rect) -> QRect:
    return QRect(r.left, r.top, r.width, r.height)


# =========================================================================
# Patterns and transfer modes
# =========================================================================
CLEAR = 0x00000000          # leaves the pixel alone (drawn over)
KEEP = 0xFF000000           # leaves the pixel alone (XORed with)
INVERT = 0xFFFFFFFF         # inverts the pixel (XORed with)
XOR = QPainter.CompositionMode.RasterOp_SourceXorDestination
OVER = QPainter.CompositionMode.CompositionMode_SourceOver
_C = QPainter.CompositionMode
# The source modes, drawn in pixel values in a black foreground (all 1's)
# and a white background (all 0's): applying them to the 1's and 0's of
# a source that isn't just black and white (Inside Macintosh V-57) bit by
# bit, Or is d OR s, Bic d AND NOT s, NotBic d AND s and so on, as these
# combine VALUE_TABLES' colours, in which each value's bits are inverted.
# Programs mask colour with them, building a tile or sprite offscreen with
# NotBic and Or (FloorTiles).
VALUE_OPS = {srcOr: _C.RasterOp_SourceAndDestination,
             srcXor: _C.RasterOp_NotSourceXorDestination,
             srcBic: _C.RasterOp_NotSourceOrDestination,
             notSrcCopy: _C.RasterOp_NotSource,
             notSrcOr: _C.RasterOp_NotSourceAndDestination,
             notSrcXor: _C.RasterOp_SourceXorDestination,
             notSrcBic: _C.RasterOp_SourceOrDestination}

# Color QuickDraw's arithmetic modes (Inside Macintosh V, The Color Graphics
# Port), which combine the RGB components of a source's pixels and the
# destination's, and the classic modes they revert to on a 1-bit one.
BLEND, ADD_PIN, ADD_OVER, SUB_PIN, ADD_MAX, SUB_OVER, AD_MIN = 32, 33, 34, 35, 37, 38, 39
ONE_BIT_MODES = {BLEND: srcCopy, ADD_PIN: srcBic, ADD_OVER: srcXor, SUB_PIN: srcOr,
                 ADD_MAX: srcBic, SUB_OVER: srcXor, AD_MIN: srcOr}
# An old port's addPin ceiling, subPin floor and blend weights
OLD_OP_COLORS = {ADD_PIN: (0xFFFF,) * 3, SUB_PIN: (0, 0, 0), BLEND: (0x8000,) * 3}


def arithmetic(src: QImage, dst: QImage, mode: int,
               op: tuple[int, int, int] | None = None) -> QImage:
    """dst with src, the same size, drawn on it in an arithmetic mode: each
    component of each pixel the two combined.  addMax and adMin take the
    greater and the lesser; addPin the sum, no more than op; subPin the
    destination's less the source's, no less than op; addOver and subOver
    the same, wrapping around; blend src weighted by op and dst by what's
    left.  op is a cGrafPort's OpColor; None, an old port's, for which
    addPin's is white, subPin's black and blend's 50% grey."""
    src = src.convertToFormat(QImage.Format.Format_RGB32)
    out = dst.convertToFormat(QImage.Format.Format_RGB32)
    if mode in (ADD_OVER, SUB_OVER):
        return _wrapped(src, out, mode == SUB_OVER)
    if op is None:
        op = OLD_OP_COLORS.get(mode, (0, 0, 0))
    color = QColor(op[0] >> 8, op[1] >> 8, op[2] >> 8)
    if mode == SUB_PIN:             # d - s, pinned at 0, is NOT (NOT d + s), pinned at white
        out.invertPixels()
    elif mode == BLEND:
        src = _times(src, color)
        out = _times(out, QColor(255 - color.red(), 255 - color.green(), 255 - color.blue()))
    comp = {AD_MIN: _C.CompositionMode_Darken, ADD_MAX: _C.CompositionMode_Lighten}.get(
        mode, _C.CompositionMode_Plus)
    p = QPainter(out)
    p.setCompositionMode(comp)
    p.drawImage(0, 0, src)
    p.end()
    if mode == SUB_PIN:
        out.invertPixels()
    if mode in (ADD_PIN, SUB_PIN):
        p = QPainter(out)
        p.setCompositionMode(_C.CompositionMode_Darken if mode == ADD_PIN
                             else _C.CompositionMode_Lighten)
        p.fillRect(out.rect(), color)
        p.end()
    return out


def _times(img: QImage, color: QColor) -> QImage:
    """img, each component scaled by color's."""
    p = QPainter(img)
    p.setCompositionMode(_C.CompositionMode_Multiply)
    p.fillRect(img.rect(), color)
    p.end()
    return img


def _wrapped(src: QImage, dst: QImage, sub: bool) -> QImage:
    """dst plus src (or less it), byte by byte modulo 256: the low seven bits
    of each added (or subtracted) so that no carry (or borrow) leaves a
    byte, and its top bit from theirs and that one's."""
    n = dst.sizeInBytes()
    a = int.from_bytes(pixels.raw(dst), "big")
    b = int.from_bytes(pixels.raw(src), "big")
    top = int.from_bytes(b"\x80" * n, "big")
    if sub:
        r = ((a | top) - (b & ~top)) ^ ((a ^ ~b) & top)
    else:
        r = ((a & ~top) + (b & ~top)) ^ ((a ^ b) & top)
    alpha = QImage(dst.size(), QImage.Format.Format_RGB32)
    alpha.fill(QColor(0, 0, 0))     # 0xFF000000: the bytes that are alpha, all set
    data = (r | int.from_bytes(pixels.raw(alpha), "big")).to_bytes(n, "big")
    return QImage(data, dst.width(), dst.height(), dst.bytesPerLine(),
                  QImage.Format.Format_RGB32).copy()


def arithmetic_mode(mode: int) -> int:
    """A transfer mode as macrt draws it: an arithmetic one with the
    pattern bit (patCopy's, 8) set, as the arithmetic mode itself."""
    return mode & ~8 if mode & 32 else mode


def mode_table(mode: int, fg: int = BLACK_RGB,
               bk: int = WHITE_RGB) -> tuple[list[int], QPainter.CompositionMode]:
    """The colours a 1-bit image's 0 and 1 bits become in a transfer mode
    (the not- modes swap them), and how they're combined with what's there.
    1 bits draw in the foreground colour and 0 bits, where they draw at
    all, in the background colour; XOR ignores both.  An arithmetic mode
    draws as the classic one it reverts to on a 1-bit destination, but
    transparent, which leaves out what's in the background colour: the 0
    bits, and the 1 bits too if the foreground colour is the same.  With
    the pattern bit (40 to 47), an arithmetic mode is drawn as it is
    without it."""
    mode = arithmetic_mode(mode)
    if mode == TRANSPARENT:
        return [CLEAR, CLEAR if fg == bk else fg], OVER
    mode = ONE_BIT_MODES.get(mode, mode)
    zero, one = {srcCopy: (bk, fg), srcOr: (CLEAR, fg), srcXor: (KEEP, INVERT),
                 srcBic: (CLEAR, bk)}[mode & 3]
    if mode & 4:
        zero, one = one, zero
    return [zero, one], (XOR if mode & 3 == srcXor else OVER)


def port_colors(port) -> tuple[int, int]:
    """The foreground and background colours a port draws in on a 1-bit
    screen.  Of the eight classic colours (ForeColor, BackColor) only
    whiteColor has bit 0, the plane a black-and-white screen shows, clear,
    so it draws white and the rest black.  A port no colour was ever set
    in draws black on white."""
    if port is None or port is _NoPort:
        return BLACK_RGB, WHITE_RGB
    return (WHITE_RGB if port.fg_color == whiteColor else BLACK_RGB,
            BLACK_RGB if port.bk_color & 1 else WHITE_RGB)


def classic_argb(color: int, default: int) -> int:
    """A classic colour (ForeColor's, BackColor's) as Color QuickDraw
    draws it, 0xAARRGGBB; default for a value that isn't one of the eight."""
    rgb = CLASSIC_RGB.get(color)
    if rgb is None:
        return default
    r, g, b = rgb
    return 0xFF000000 | (r >> 8) << 16 | (g >> 8) << 8 | b >> 8


def rgb_argb(rgb: tuple[int, int, int]) -> int:
    """An RGBColor as 0xAARRGGBB."""
    r, g, b = rgb
    return 0xFF000000 | (r >> 8) << 16 | (g >> 8) << 8 | b >> 8


def mono_argb(argb: int) -> int:
    """The colour, black or white, a 1-bit pixel shows for one: the nearer."""
    light = luminance((argb >> 16 & 0xFF) * 0x101, (argb >> 8 & 0xFF) * 0x101,
                      (argb & 0xFF) * 0x101) >= 0x8000
    return WHITE_RGB if light else BLACK_RGB


def _blend(a: int, b: int, f: float) -> int:
    return 0xFF000000 | sum(round((a >> s & 0xFF) * (1 - f) + (b >> s & 0xFF) * f) << s
                            for s in (16, 8, 0))


@lru_cache(maxsize=64)
def relative_table(data: bytes, depth: int, fg: int, bk: int) -> tuple[int, ...]:
    """A pixel pattern's colours (Inside Macintosh V-56), from its
    ColorTable's ctFlags on (none, if empty): each value the table
    doesn't give a colour is given one between the foreground colour,
    the largest such value's, and the background colour, the smallest's."""
    n, given = 1 << depth, {}
    if len(data) >= 4:
        flags, size = struct.unpack_from(">Hh", data)
        for i in range(max(0, min(size + 1, (len(data) - 4) // 8))):
            value, r, g, b = struct.unpack_from(">HHHH", data, 4 + 8 * i)
            value = i if flags & CT_DEVICE else value & 0xFF
            if value < n:
                given[value] = rgb_argb((r, g, b))
    free = [v for v in range(n) if v not in given]
    for i, v in enumerate(free):
        given[v] = _blend(bk, fg, i / (len(free) - 1)) if len(free) > 1 else fg
    return tuple(given[v] for v in range(n))


def _pattern_brush(img: QImage, mode: int,
                   bk: int) -> tuple[QBrush, QPainter.CompositionMode]:
    """A brush of a colour pattern's image in a pattern mode (the not-
    modes aside), and how it combines: copied; with white left out (Or);
    inverting where it's dark (Xor); the background colour where it
    isn't white (Bic); or with the background colour left out
    (transparent).  The port's colours don't change its own."""
    if arithmetic_mode(mode) == TRANSPARENT:
        mask = img.createMaskFromColor(bk, Qt.MaskMode.MaskOutColor)
        img = img.convertToFormat(QImage.Format.Format_ARGB32)
        img.setAlphaChannel(mask)
        return QBrush(img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)), OVER
    mode &= 3
    if mode == srcXor:
        img = img.copy()
        img.invertPixels()
        return QBrush(img), XOR
    if mode == srcCopy:
        return QBrush(img), OVER
    mask = img.createMaskFromColor(WHITE_RGB, Qt.MaskMode.MaskOutColor)
    if mode == srcBic:
        img = QImage(img.size(), QImage.Format.Format_ARGB32)
        img.fill(QColor.fromRgba(bk))
    else:
        img = img.convertToFormat(QImage.Format.Format_ARGB32)
    img.setAlphaChannel(mask)
    return QBrush(img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)), OVER


@lru_cache(maxsize=128)
def pix_pat_brush(data: bytes, width: int, height: int, row_bytes: int, depth: int,
                  table: tuple[int, ...], screen: tuple[int, ...], mode: int,
                  bk: int, shown: tuple[int, ...] = ()) -> tuple[QBrush, QPainter.CompositionMode]:
    """A pixel pattern's brush: its pixels in its colours, as near as the
    screen's (a table of them; empty, any) has them; or, given the colours
    to show the screen's values in (shown), the values nearest, in those."""
    img = pixels.decode(data, width, height, row_bytes, depth, list(table) or None)
    if screen:
        img = img.convertToFormat(QImage.Format.Format_Indexed8, list(screen),
                                  Qt.ImageConversionFlag.ThresholdDither)
        if shown:
            img.setColorTable(list(shown))
        img = img.convertToFormat(QImage.Format.Format_RGB32)
    return _pattern_brush(img, mode, bk)


@lru_cache(maxsize=64)
def solid_brush(argb: int, mode: int, bk: int) -> tuple[QBrush, QPainter.CompositionMode]:
    """An RGB pattern's brush (MakeRGBPat's): its colour, as the screen has it."""
    img = QImage(8, 8, QImage.Format.Format_RGB32)
    img.fill(QColor.fromRgba(argb))
    return _pattern_brush(img, mode, bk)


def color_source(src: QImage, mode: int, fg: int = BLACK_RGB,
                 bk: int = WHITE_RGB) -> tuple[QImage, QPainter.CompositionMode] | None:
    """src, in colour, ready to draw in a source mode (a colour CopyBits),
    and how it combines; None for a mode that draws it as black and
    white (source_image).  Copy (and the arithmetic modes, but for
    transparent) copies its colours; Or leaves its white out; Bic draws
    the background colour where it isn't white; Xor inverts where it's
    dark; NotCopy inverts it; transparent leaves out the background
    colour."""
    mode &= ~DITHER_COPY
    if mode >= 32 and mode != TRANSPARENT:
        mode = srcCopy
    if mode in (srcCopy, notSrcCopy, srcXor):
        img = src.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        if mode != srcCopy:
            img.invertPixels()
        return img, XOR if mode == srcXor else OVER
    if mode not in (srcOr, srcBic, TRANSPARENT):
        return None
    mask = src.createMaskFromColor(bk if mode == TRANSPARENT else WHITE_RGB,
                                   Qt.MaskMode.MaskOutColor)
    if mode == srcBic:
        img = QImage(src.size(), QImage.Format.Format_ARGB32)
        img.fill(QColor.fromRgba(bk))
    else:
        img = src.convertToFormat(QImage.Format.Format_ARGB32)
    img.setAlphaChannel(mask)
    return img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied), OVER


# For each depth, a colour for each pixel value, none the same, white 0's
# and black the last's: what CopyBits copies pixel values through when it
# copies them as they are (MemorySurface.same_values).
VALUE_TABLES = {d: [0xFF000000 | (255 - i * 255 // ((1 << d) - 1)) * 0x010101
                    for i in range(1 << d)] for d in (1, 2, 4, 8)}


@lru_cache(maxsize=64)          # a screen's 480 rows of three: some 56K each
def _rows(row: str, n: int) -> struct.Struct:
    """n rows laid out as row (a struct format) is, one after another."""
    return struct.Struct(">" + row * n)


def _mono(data: bytes, width: int, height: int, bpl: int) -> QImage:
    img = QImage(data, width, height, bpl, QImage.Format.Format_Mono)
    img.setColorTable(MONO_TABLE)
    return img.copy()           # its own copy of the bits, which data doesn't outlive


@lru_cache(maxsize=256)
def ink(pat: bytes, mode: int, fg: int = BLACK_RGB,
        bk: int = WHITE_RGB) -> tuple[QBrush, QPainter.CompositionMode]:
    """A brush that paints pat in a pattern mode, and how it combines with
    what's there."""
    rows = (bytes(pat) + bytes(8))[:8]
    table, comp = mode_table(mode, fg, bk)
    if rows in (bytes(8), b"\xFF" * 8):
        # one colour: a plain brush paints the same, far more quickly
        return QBrush(QColor.fromRgba(table[rows[0] & 1])), comp
    img = _mono(b"".join(bytes([r, 0, 0, 0]) for r in rows), 8, 8, 4)
    img.setColorTable(table)
    return QBrush(img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)), comp


def source_image(src: QImage, mode: int, fg: int = BLACK_RGB,
                 bk: int = WHITE_RGB) -> tuple[QImage, QPainter.CompositionMode]:
    """src, black and white, ready to draw in a source mode (CopyBits,
    text): black is a 1 bit, white a 0."""
    mono = pixels.mono(src)
    table, comp = mode_table(mode, fg, bk)
    mono.setColorTable(table)
    return mono.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied), comp


# =========================================================================
# Fonts
# =========================================================================
SERIF, SANS, MONO = "serif", "sans", "mono"
FAMILIES = {        # Mac font family -> host stand-ins
    2: SERIF, 3: SANS, 4: MONO, 5: SERIF, 6: SERIF, 7: SANS, 8: SANS, 9: SANS,
    11: SANS, 12: SANS, 13: SERIF, 20: SERIF, 21: SANS, 22: MONO, 23: SERIF, 24: SERIF,
}
HOST_FAMILIES = {
    SERIF: ["New York", "Times", "Times New Roman", "Liberation Serif", "DejaVu Serif"],
    SANS: ["Geneva", "Helvetica", "Arial", "Liberation Sans", "DejaVu Sans"],
    MONO: ["Monaco", "Menlo", "Courier New", "Liberation Mono", "DejaVu Sans Mono"],
}
HINTS = {SERIF: QFont.StyleHint.Serif, SANS: QFont.StyleHint.SansSerif,
         MONO: QFont.StyleHint.TypeWriter}
FONT_NAMES = {0: "Chicago", 1: "Geneva", 2: "New York", 3: "Geneva", 4: "Monaco", 5: "Venice",
              6: "London", 7: "Athens", 8: "San Francisco", 9: "Toronto", 11: "Cairo",
              12: "Los Angeles", 13: "Zapf Dingbats", 20: "Times", 21: "Helvetica",
              22: "Courier", 23: "Symbol", 24: "Taliesin"}
FONT_NUMBERS = {name.lower(): num for num, name in FONT_NAMES.items() if num}
FONT_NUMBERS["chicago"] = 0
# What host fonts stand in for, always: the families macrt's System has.
# Venice, Cairo and the rest are drawn from their bitmaps when a font file
# or the System brings them (Font Mover shows Cairo's pictures in it), and
# only otherwise from the nearest host font.
STANDARD_FAMILIES = {0, 1, 2, 3, 4, 20, 21, 22}

# Characters Chicago draws as symbols, which host fonts know by their Unicode
# names.  Text comes in with them as those; a bitmap font wants the codes back.
CHICAGO_SYMBOLS = str.maketrans({k.commandMark: "\u2318", k.checkMark: "\u2713",
                                 k.diamondMark: "\u25c6"})
_TO_CODES = {ord(v): chr(c) for c, v in CHICAGO_SYMBOLS.items()}


# What Chicago, Geneva and Monaco have no width for and draw nothing for,
# whatever the host font standing in for them would draw.
BLANKS = "\x00\r"


# The largest size a host font is made in: taller than any screen, so that
# a program that sets an absurd txSize draws big rather than stopping.
MAX_TEXT_SIZE = 1024


def text_size(size: int) -> int:
    """The size a host font is made in for a port's txSize: 0, the
    default, is 12."""
    return min(max(size, 1), MAX_TEXT_SIZE) if size else 12


def char_codes(text: str) -> bytes:
    return text.translate(_TO_CODES).encode("mac_roman", "replace")


class Fonts:
    """Host fonts for Mac font families.  system is what stands in for
    Chicago, the system font.  measured, if set, gives the strike a family
    is measured with at a size and how much to scale it by: the one it's
    drawn from, an application's own, or the one a host font standing in
    for it is measured with (bitfont.AppFonts.widths).  lines, the
    ascent, descent and leading of a family at a size where a host font
    stands in for a strike the System has (System.font_lines), not the
    host font's own.  app_font says which family family 1, the
    application font, is: ApFontID's."""

    def __init__(self, system: QFont):
        self.system = QFont(system)
        self._families: dict[str, str | None] = {}
        self.measured: Callable[[int, int], tuple | None] | None = None
        self.lines: dict[tuple[int, int], tuple[int, int, int]] = {}
        self.app_font: Callable[[], int] = lambda: 3        # Geneva
        self._widths: dict[tuple[int, int, int], tuple[object, float, dict[str, int]]] = {}

    def _strike(self, family: int, size: int) -> tuple[object, float]:
        found = self.measured(family, size) if self.measured is not None else None
        return found if found is not None else (None, 1.0)

    def advances(self, family: int, size: int, face: int, text: str,
                 space_extra: int = 0) -> list[int]:
        """How far the pen moves for each character of text: a whole number
        of pixels, its own width, whatever comes before or after it, with
        a pixel more for outline and shadow, and space_extra more for a
        space.  Programs that measure text a character at a time and draw
        it a piece at a time, as word processors do, need it to add up."""
        family = self.app_font() if family == 1 else family
        strike, scale = self._strike(family, size)
        key = (family, size, face)
        known = self._widths.get(key)
        if known is None or known[0] is not strike or known[1] != scale:
            known = self._widths[key] = (strike, scale, {})
        table = known[2]
        new = set(text).difference(table)
        if new:
            style = (1 if face & bold and strike is not None else 0) + (
                1 if face & (outline | shadow) else 0)
            fm = QFontMetrics(self.font(family, size, face)) if strike is None else None
            for ch in new:
                table[ch] = style + (round(strike.char_width(char_codes(ch)[0]) * scale)
                                     if strike is not None else
                                     0 if ch in BLANKS else fm.horizontalAdvance(ch))
        widths = [table[ch] for ch in text]
        if space_extra:
            widths = [w + space_extra if ch == " " else w for w, ch in zip(widths, text)]
        return widths

    def blanks(self, family: int, size: int, text: str) -> set[str]:
        """The characters of text the font draws nothing for: glyphs of no
        width, as Chicago's NUL and return are."""
        strike, _ = self._strike(family, size)
        if strike is None:
            return set(text) & set(BLANKS)
        return {ch for ch in set(text) if not strike.char_width(char_codes(ch)[0])}

    def info(self, family: int, size: int, face: int) -> tuple[int, int, int, int]:
        """ascent, descent, widMax, leading"""
        strike, scale = self._strike(family, size)
        if strike is not None:
            return tuple(round(n * scale) for n in (strike.ascent, strike.descent,
                                                    strike.wid_max, strike.leading))
        fm = QFontMetrics(self.font(family, size, face))
        family = self.app_font() if family == 1 else family
        ascent, descent, leading = self.lines.get(
            (family, text_size(size)), (fm.ascent(), fm.descent(), max(fm.leading(), 0)))
        return ascent, descent, fm.maxWidth(), leading

    def _family(self, kind: str) -> str | None:
        if kind not in self._families:
            have = set(QFontDatabase.families())
            self._families[kind] = next((f for f in HOST_FAMILIES[kind] if f in have), None)
        return self._families[kind]

    def font(self, family: int, size: int, face: int) -> QFont:
        return self._font(self.app_font() if family == 1 else family, size, face)

    @lru_cache(maxsize=128)
    def _font(self, family: int, size: int, face: int) -> QFont:
        if family == 0:
            f = QFont(self.system)
            f.setPixelSize(text_size(size))
        else:
            kind = FAMILIES.get(family, SANS)
            name = self._family(kind)
            f = QFont(name) if name else QFont()
            f.setStyleHint(HINTS[kind])
            f.setPixelSize(text_size(size))
            f.setBold(bool(face & bold))
        if family == 0 and face & bold:
            f.setBold(True)
        f.setItalic(bool(face & italic))
        f.setUnderline(bool(face & underline))
        if face & condense:
            f.setStretch(QFont.Stretch.SemiCondensed.value)
        elif face & extend:
            f.setStretch(QFont.Stretch.SemiExpanded.value)
        f.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
        f.setKerning(False)
        return f

    def for_port(self, port: GrafPort) -> QFont:
        return self.font(port.tx_font, port.tx_size, port.tx_face)


# =========================================================================
# Surfaces: where a port's pixels are
# =========================================================================
class WindowSurface:
    """A window's Backing, whose area is relative to the top left of the
    content area where the Window Manager placed it (backing_rect)."""

    def __init__(self, port: GrafPort):
        self.port = port
        self.backing = port.backing

    def _origin(self) -> tuple[int, int]:
        """Where the Backing's first pixel is in the port's coordinates:
        portBits.bounds maps them to the screen, as QuickDraw maps them,
        wherever the portRect is."""
        at, b, a = self.port.backing_at, self.port.port_bits_bounds, self.backing.area
        return at.h + b.left + a.left, at.v + b.top + a.top

    @contextmanager
    def painter(self, area: Rect | None = None):
        with self.backing.lock:
            p = QPainter(self.backing.image)
            h, v = self._origin()
            p.translate(-h, -v)
            try:
                yield p
            finally:
                p.end()

    def image(self, r: Rect) -> QImage:
        """The pixels in r (local), white where there are none."""
        h, v = self._origin()
        out = QImage(max(r.width, 1), max(r.height, 1), QImage.Format.Format_RGB32)
        out.fill(QColor(255, 255, 255))
        with self.backing.lock:
            p = QPainter(out)
            p.drawImage(QPoint(0, 0), self.backing.image,
                        QRect(r.left - h, r.top - v, r.width, r.height))
            p.end()
        return out

    def part(self, r: Rect, need: Rect) -> QImage:
        """The pixels in r (local).  Those of need, inside r, are as
        image() has them; the rest may be anything, if they're beyond the
        Backing."""
        h, v = self._origin()
        with self.backing.lock:
            img = self.backing.image
            if img.rect().contains(QRect(need.left - h, need.top - v, need.width, need.height)):
                return img.copy(QRect(r.left - h, r.top - v, r.width, r.height))
        return self.image(r)

    def mono(self, r: Rect, need: Rect) -> QImage:
        """part(), as a 1-bit image, black 1 as on a Mac."""
        return pixels.mono(self.part(r, need))

    def pixel(self, pt: Point) -> bool:
        """Whether the pixel at pt isn't white."""
        rgb = self.rgb(pt)
        return rgb is not None and rgb & 0xFFFFFF != 0xFFFFFF

    def rgb(self, pt: Point) -> int | None:
        """The pixel at pt's colour, 0xAARRGGBB; None where there's none."""
        h, v = self._origin()
        with self.backing.lock:
            img = self.backing.image
            x, y = pt.h - h, pt.v - v
            if 0 <= x < img.width() and 0 <= y < img.height():
                return img.pixel(x, y) | 0xFF000000
        return None


class MemorySurface:
    """A BitMap in guest memory, or a PixMap of depth bits a pixel by a
    table of colours (see pixels), read into an image to draw on and
    written back after.  Only the rows a call touches are read."""

    def __init__(self, mem: RAM, base: int, row_bytes: int, bounds: Rect, depth: int = 1,
                 table: list[int] | None = None):
        self.mem = mem
        self.base = base
        self.row_bytes = row_bytes
        self.bounds = bounds
        self.depth = depth
        self.table = table
        # The screen's: the colours a colour that isn't exactly table's is
        # matched to, with none for an entry reserved for animation
        # (Devices.matching; see pixels.encode).
        self.match: list[int] | None = None
        self.ct = 0                 # a PixMap's, the ColorTable handle table is from
        self.own: list[int] | None = None   # table, while it's drawn on in values

    @classmethod
    def make(cls, mem: RAM, base: int, row_bytes: int, bounds: Rect, depth: int = 1,
             table: list[int] | None = None) -> MemorySurface | None:
        """None if it isn't a BitMap (or, given its depth, the pixels of a
        PixMap) QuickDraw can draw in: one whose rowBytes has a PixMap's
        flags, or that doesn't fit in memory.  Bounds wider than rowBytes
        hold are drawn in as far as a row goes (Tetris's tiles: 208
        pixels' bounds on rows of 170 bytes, drawn in the first 169)."""
        base &= ADDRESS_MASK
        if row_bytes & 0x8000 or row_bytes <= 0 or depth not in pixels.DEPTHS:
            return None
        if bounds.width * depth > row_bytes * 8:
            bounds = Rect(bounds.top, bounds.left, bounds.bottom,
                          bounds.left + row_bytes * 8 // depth)
        if (bounds.empty or bounds.height > MAX_BITMAP_ROWS
                or not base or base + row_bytes * bounds.height > mem.size):
            return None
        return cls(mem, base, row_bytes, bounds, depth, table)

    @classmethod
    def of_pixmap(cls, mem: RAM, p: int, palette: Callable[[], list[int] | None] | None = None,
                  tableless: Callable[[int], list[int]] | None = None) -> MemorySurface | None:
        """The pixels of the PixMap at p (in the colours of palette()'s
        entries, if its table's values are a palette's: see read_table;
        of 2 to 8 bits with no colour table, or one with no colours,
        tableless(depth)'s)."""
        pm = PixMap(mem, p)
        depth = pm.pixel_size
        ct = deref(mem, pm.pm_table)
        if 1 < depth <= 8 and tableless is not None and (not ct or mem.r16s(ct + 6) < 0):
            table = tableless(depth)
        else:
            table = read_table(mem, pm.pm_table, palette) if depth <= 8 else None
        got = cls.make(mem, pm.base_addr, pm.row_bytes & ROW_BYTES_MASK, pm.bounds, depth,
                       table)
        if got is not None and table is not None:
            got.ct = pm.pm_table & ADDRESS_MASK
        return got

    def same_values(self, other) -> bool:
        """Whether other's pixel values are this one's colours: both are
        PixMaps of a depth with a colour table, the same one, or ones
        whose seeds are the same, so that CopyBits copies the values as
        they are (as Color QuickDraw does, with no colours to match)."""
        if (not isinstance(other, MemorySurface) or self.depth != other.depth
                or self.depth > 8 or not self.ct or not other.ct):
            return False
        if self.ct == other.ct:
            return True
        a, b = deref(self.mem, self.ct), deref(self.mem, other.ct)
        return bool(a and b) and self.mem.r32(a) == self.mem.r32(b)

    def in_values(self) -> MemorySurface:
        """This PixMap, to be drawn on in its pixel values as they are, each
        VALUE_TABLES' colour for it, as Color QuickDraw draws on one (in a
        port's fgColor and bkColor, which are pixel values): none is lost
        to another the same colour.  Its own colours (own) are what a
        colour drawn on it is matched to (values)."""
        if self.own is None and self.depth <= 8:
            self.own, self.table = self.table, VALUE_TABLES[self.depth]
        return self

    def values(self, img: QImage) -> QImage:
        """img's colours as this PixMap's nearest pixel values show while
        it's drawn on in them (in_values)."""
        if self.own is None:
            return img
        if img.format() in (QImage.Format.Format_Indexed8, QImage.Format.Format_Mono,
                            QImage.Format.Format_MonoLSB):
            # an indexed image's indices would be kept, not its colours matched
            img = img.convertToFormat(QImage.Format.Format_ARGB32)
        raw, bpl = pixels.encode(img, self.depth, self.own, self.match)
        return pixels.decode(raw, img.width(), img.height(), bpl, self.depth, self.table)

    def _rows(self, area: Rect | None) -> tuple[int, int]:
        b = self.bounds
        if area is None:
            return b.top, b.bottom
        return max(b.top, area.top), min(b.bottom, area.bottom)

    def _decode(self, top: int, bottom: int) -> QImage:
        rb, depth = self.row_bytes, self.depth
        data = self.mem.read(self.base + (top - self.bounds.top) * rb, rb * (bottom - top))
        if depth == 1:
            return _mono(data, rb * 8, bottom - top, rb).convertToFormat(
                QImage.Format.Format_RGB32)
        return pixels.decode(data, rb * 8 // depth, bottom - top, rb, depth, self.table)

    def _match(self) -> list[int] | None:
        return self.match if self.own is None else None     # in values, they're as they are

    def _encode(self, img: QImage, top: int) -> None:
        raw, bpl = pixels.encode(img, self.depth, self.table, self._match())
        rb, h = self.row_bytes, img.height()
        at = self.base + (top - self.bounds.top) * rb
        if bpl >= rb:
            rows = b"".join(_rows(f"{rb}s{bpl - rb}x", h).unpack_from(raw))
        else:
            # rows that end part-way into a pixel: what's after the last as it was
            rest = _rows(f"{bpl}x{rb - bpl}s", h).unpack(self.mem.read(at, rb * h))
            rows = b"".join(raw[y * bpl:(y + 1) * bpl] + rest[y] for y in range(h))
        self.mem.write(at, rows)

    def put(self, img: QImage, left: int, r: Region) -> None:
        """Write img's pixels where r is, img's top left at r's top and
        left, which is on a byte of the bitmap; on a 1-bit one img is a
        1-bit image (WindowSurface.mono's).  r is inside bounds.  The rows
        are written whole, with what's beside r as it was: one write, with
        no painting to do, as a call that draws a little is drawn."""
        b, rb, d = self.bounds, self.row_bytes, self.depth
        top, bottom = r.bands[0][0], r.bands[-1][1]
        first = ((left - b.left) * d) >> 3
        nb = min((img.width() * d + 7) >> 3, rb - first)
        if d == 1:
            data, bpl = pixels.raw(img), img.bytesPerLine()
        else:
            data, bpl = pixels.encode(img, d, self.table, self._match())
        rows = bottom - top
        at = self.base + (top - b.top) * rb
        # Each row in three: before first, nb bytes from first, and after.
        parts = list(_rows(f"{first}s{nb}s{rb - first - nb}s", rows).unpack(
            self.mem.read(at, rows * rb)))
        src = b"".join(_rows(f"{nb}s{bpl - nb}x", rows).unpack_from(data))
        bits, y, masks = nb * 8, top, []
        for t, bt, xs in r.bands:
            if t > y:
                masks.append(bytes(nb * (t - y)))
            m = 0
            for i in range(0, len(xs), 2):
                x0, x1 = (xs[i] - left) * d, (xs[i + 1] - left) * d
                m |= ((1 << (x1 - x0)) - 1) << (bits - x1)
            masks.append(m.to_bytes(nb, "big") * (bt - t))
            y = bt
        if len(r.bands) > 1 or m != (1 << bits) - 1:        # not all of each row's nb
            mask = int.from_bytes(b"".join(masks), "big")
            old = int.from_bytes(b"".join(parts[1::3]), "big")
            src = (old & ~mask | int.from_bytes(src, "big") & mask).to_bytes(rows * nb, "big")
        parts[1::3] = _rows(f"{nb}s", rows).unpack(src)
        self.mem.write(at, b"".join(parts))

    @contextmanager
    def painter(self, area: Rect | None = None):
        top, bottom = self._rows(area)
        if bottom <= top:
            yield None
            return
        img = self._decode(top, bottom)
        p = QPainter(img)
        p.translate(-self.bounds.left, -top)
        try:
            yield p
        finally:
            p.end()
            self._encode(img, top)

    def image(self, r: Rect) -> QImage:
        top, bottom = self._rows(r)
        out = QImage(max(r.width, 1), max(r.height, 1), QImage.Format.Format_RGB32)
        out.fill(QColor(255, 255, 255))
        if bottom > top:
            p = QPainter(out)
            p.drawImage(QPoint(0, top - r.top), self._decode(top, bottom),
                        QRect(r.left - self.bounds.left, 0, r.width, bottom - top))
            p.end()
        return out

    def pixel(self, pt: Point) -> bool:
        """Whether the pixel at pt isn't white: on a 1-bit bitmap, whether it's set."""
        if self.depth == 1:
            value = self.value(pt)
            return bool(value)
        rgb = self.rgb(pt)
        return rgb is not None and rgb != WHITE_RGB

    def value(self, pt: Point) -> int | None:
        """The pixel value at pt; None outside the bounds."""
        b, d = self.bounds, self.depth
        if not b.contains(pt):
            return None
        x, y = pt.h - b.left, pt.v - b.top
        at = self.base + y * self.row_bytes + (x * d >> 3)
        if d < 8:
            return self.mem.r8(at) >> (8 - d - (x * d & 7)) & ((1 << d) - 1)
        return {8: self.mem.r8, 16: self.mem.r16}.get(d, self.mem.r32)(at)

    def rgb(self, pt: Point) -> int | None:
        value = self.value(pt)
        if value is None:
            return None
        return pixels.value_rgb(value, self.depth, self.own or self.table or pixels.MONO_TABLE)


class ImageSurface:
    """A host image a port draws in, its top left at the top left of
    bounds: a page being printed."""

    def __init__(self, img: QImage, bounds: Rect):
        self.img = img
        self.bounds = bounds

    @contextmanager
    def painter(self, area: Rect | None = None):
        p = QPainter(self.img)
        p.translate(-self.bounds.left, -self.bounds.top)
        try:
            yield p
        finally:
            p.end()

    def image(self, r: Rect) -> QImage:
        out = QImage(max(r.width, 1), max(r.height, 1), QImage.Format.Format_RGB32)
        out.fill(QColor(255, 255, 255))
        p = QPainter(out)
        p.drawImage(QPoint(0, 0), self.img, QRect(r.left - self.bounds.left,
                                                  r.top - self.bounds.top, r.width, r.height))
        p.end()
        return out

    def pixel(self, pt: Point) -> bool:
        x, y = pt.h - self.bounds.left, pt.v - self.bounds.top
        if 0 <= x < self.img.width() and 0 <= y < self.img.height():
            return (self.img.pixel(x, y) & 0xFFFFFF) == 0
        return False

    def rgb(self, pt: Point) -> int | None:
        x, y = pt.h - self.bounds.left, pt.v - self.bounds.top
        if 0 <= x < self.img.width() and 0 <= y < self.img.height():
            return self.img.pixel(x, y) | 0xFF000000
        return None


# =========================================================================
# QuickDraw
# =========================================================================
class QuickDraw:
    """The drawing calls for one process.  Coordinates are local to the
    port drawn in.  repaint(window) is called, on the guest's thread, when
    a window's pixels have changed."""

    def __init__(self, system: System, fonts: Fonts, repaint: Callable[[GrafPort], None]):
        self.sys = system
        self.mem = system.mem
        self.fonts = fonts
        self.repaint = repaint
        self.app_fonts: AppFonts | None = None      # the process's own bitmap fonts
        self.polys: dict[int, list] = {}            # port -> [PolyHandle, points]
        self.rgns: dict[int, list] = {}             # port -> [Region so far, lines]
        self.pictures: dict[int, object] = {}       # port -> the pict.Recorder it's recording
        self.surfaces: dict[int, ImageSurface] = {}  # port -> the host image it draws in
        # The colours of the entries of the palette a port draws in, as the
        # screen shows them: the Palette Manager's, when there is one.
        self.palette_colors: Callable[[GrafPort], list[int] | None] | None = None
        # The pixel value the current device's search procedures give a
        # colour (AddSearch), or None for the inverse table's; None when
        # there are none.
        self.search: Callable[[tuple[int, int, int]], int | None] | None = None

    # -- colours ---------------------------------------------------------------
    def device_colors(self, matching: bool = False) -> tuple[int, list[int]]:
        """The current device's depth and colours, by pixel value: on a
        black-and-white Mac, a 1-bit device's.  Or, matching, the colours
        a colour to draw in is matched to (Devices.matching)."""
        devices = self.sys.devices
        if devices is None:
            return 1, [0xFFFFFFFF, 0xFF000000]
        gdh = self.mem.r32(lowmem.TheGDevice)
        main = devices.matching() if matching else devices.table()
        if gdh & ADDRESS_MASK == devices.main & ADDRESS_MASK:
            return devices.depth, main
        p = deref(self.mem, gdh)
        pm = deref(self.mem, GDevice(self.mem, p).pmap) if p else 0
        if not pm:
            return devices.depth, main
        pm = PixMap(self.mem, pm)
        depth = pm.pixel_size if pm.pixel_size in DEPTH_MODES else 1
        return depth, read_table(self.mem, pm.pm_table) if depth <= 8 else []

    def tableless(self, depth: int) -> list[int]:
        """The colours of a PixMap of 2 to 8 bits with no colour table:
        the current device's, if it's of that depth, else the standard
        ones (the greys, for 2 bits).  The Discovery Demo draws its
        pictures in one, and copies them out through another PixMap of
        the same pixels with the screen's table."""
        got, colors = self.device_colors()
        if got == depth:
            return list(colors)
        if depth == 2:
            return [0xFF000000 | v * 0x010101 for v in (0xFF, 0xAA, 0x55, 0)]
        return list(STANDARD_4BIT if depth == 4 else STANDARD_8BIT)

    def color_index(self, rgb: tuple[int, int, int]) -> int:
        """Color2Index: the current device's pixel value nearest a colour."""
        if self.sys.devices is None:
            return 0 if is_light(*rgb) else 1
        if self.search is not None:
            got = self.search(rgb)
            if got is not None:
                return got
        r, g, b = rgb
        return pixels.rgb_value(0xFF000000 | (r >> 8) << 16 | (g >> 8) << 8 | b >> 8,
                                *self.device_colors(matching=True))

    def set_rgb_color(self, port: GrafPort, fore: bool, rgb: tuple[int, int, int]) -> None:
        """RGBForeColor or RGBBackColor: in a cGrafPort, the colour and the
        pixel value nearest it; in a GrafPort, the classic colour it is (on
        a black-and-white Mac, black or white, as it would draw)."""
        rgb = tuple(c & 0xFFFF for c in rgb)
        if port.is_color:
            if fore:
                port.rgb_fg_color, port.fg_color = rgb, self.color_index(rgb)
            else:
                port.rgb_bk_color, port.bk_color = rgb, self.color_index(rgb)
            return
        if self.sys.devices is None:
            color = rgb_color(*rgb)
        else:
            color = TOP_BITS[tuple(c >> 15 for c in rgb)]
        if fore:
            port.fg_color = color
        else:
            port.bk_color = color

    def set_classic_color(self, port: GrafPort, fore: bool, color: int) -> None:
        """ForeColor or BackColor: in a cGrafPort, the classic colour's RGB."""
        if port.is_color:
            self.set_rgb_color(port, fore, color_rgb(color))
        elif fore:
            port.fg_color = color
        else:
            port.bk_color = color

    # -- where the pixels go -------------------------------------------------
    def surface(self, port: GrafPort, current: GrafPort | None = None):
        """A window draws in its Backing while its portBits are the screen;
        anything else (SetPortBits to an offscreen BitMap, SetPortPix to
        a PixMap) draws there: a PixMap in the colours of the current
        port's palette (the port's own, unless another's current), if its
        table says so; an old port's portBits on a colour screen, at its
        depth (on_screen)."""
        own = self.surfaces.get(port.addr)
        if own is not None:
            return own
        base = port.port_bits_base & ADDRESS_MASK
        if port.backing is not None and self.mem.physical(base) == self.sys.screen_bits.base:
            self.sys.screen.look()      # what the program wrote in the screen buffer first
            return WindowSurface(port)
        if port.is_color:
            pm = port.pixmap()
            return MemorySurface.of_pixmap(self.mem, pm, self._palette(current or port),
                                           self.tableless) if pm else None
        bounds = port.port_bits_bounds
        return (self.on_screen(base, bounds)
                or MemorySurface.make(self.mem, base, port.port_bits_row_bytes, bounds))

    def on_screen(self, base: int, bounds: Rect) -> MemorySurface | None:
        """A BitMap whose baseAddr is a colour screen's, as Color QuickDraw
        draws on one, an old port's portBits or screenBits: the main
        device's PixMap, at its depth, in its colours and with its
        rowBytes, in the BitMap's coordinates.  None for any other."""
        devices = self.sys.devices
        if (devices is None or self.sys.depth == 1
                or self.mem.physical(base) != self.sys.screen_bits.base):
            return None
        pm = devices.pixmap(devices.main)
        if pm is None:
            return None
        depth = pm.pixel_size
        got = MemorySurface.make(self.mem, base, pm.row_bytes & ROW_BYTES_MASK, bounds, depth,
                                 devices.table() if depth <= 8 else None)
        if got is not None and depth <= 8:
            got.ct = pm.pm_table & ADDRESS_MASK
            got.match = devices.matching()
        return got

    def _palette(self, port: GrafPort | None) -> Callable[[], list[int] | None] | None:
        get = self.palette_colors
        if get is None or port is None or port is _NoPort:
            return None
        return lambda: get(port)

    def depth(self, port: GrafPort | None) -> int:
        """How many bits a pixel the pixels port draws on have: the
        screen's, for a window or a BitMap on it; a PixMap's own; 1 for
        any other BitMap."""
        if port is None or port is _NoPort or port.addr in self.surfaces:
            return 1
        if self._window_on_screen(port):
            return self.sys.depth
        if port.is_color:
            pm = port.pixmap()
            return PixMap(self.mem, pm).pixel_size if pm else 1
        surface = self.on_screen(port.port_bits_base, port.port_bits_bounds)
        return surface.depth if surface is not None else 1

    def _window_on_screen(self, port: GrafPort) -> bool:
        """Whether port is a window's whose portBits are the screen (and
        so draws in its Backing)."""
        return (port.backing is not None
                and self.mem.physical(port.port_bits_base) == self.sys.screen_bits.base)

    def _values(self, port: GrafPort | None) -> bool:
        """Whether port draws in pixel values as they are (in_values): a
        cGrafPort whose pixels are a PixMap of 2 to 8 bits, not a window's
        on the screen."""
        return (port is not None and port is not _NoPort and port.is_color
                and port.addr not in self.surfaces and 1 < self.depth(port) <= 8
                and not self._window_on_screen(port))

    def colors(self, port: GrafPort | None, depth: int = 1,
               values: bool | None = None) -> tuple[int, int]:
        """The foreground and background colours port draws in on pixels
        of depth bits: black and white on a bitmap (port_colors); on a
        colour screen, a cGrafPort's pixel values, as _device_color has
        them (a window's on the screen, the colours the screen has for
        them now).  At 1 bit
        a cGrafPort's are black or white, whichever is nearer; if both
        would be the same and they aren't, the foreground is the other
        (Inside Macintosh V-58).  Drawn in pixel values (values, by default
        whether the port draws in them: _values), they're its fgColor's and
        bkColor's (VALUE_TABLES)."""
        if port is None or port is _NoPort:
            return BLACK_RGB, WHITE_RGB
        devices = self.sys.devices
        if port.is_color:
            if values is None:
                values = self._values(port)
            if values and 1 < depth <= 8:
                shown, last = VALUE_TABLES[depth], (1 << depth) - 1
                return shown[port.fg_color & last], shown[port.bk_color & last]
            fg, bk = rgb_argb(port.rgb_fg_color), rgb_argb(port.rgb_bk_color)
            if depth > 1 and devices is not None:
                as_is = self._window_on_screen(port)
                return (self._device_color(port.fg_color, fg, as_is),
                        self._device_color(port.bk_color, bk, as_is))
            mono_fg, mono_bk = mono_argb(fg), mono_argb(bk)
            if mono_fg == mono_bk and fg != bk:
                mono_fg ^= 0xFFFFFF
            return mono_fg, mono_bk
        fg, bk = port_colors(port)
        if depth == 1 or devices is None:
            return fg, bk
        return (devices.nearest(classic_argb(port.fg_color, fg)),
                devices.nearest(classic_argb(port.bk_color, bk)))

    def _search_colors(self, port: GrafPort | None, depth: int, values: bool,
                       colors: tuple[int, int]) -> tuple[int, int]:
        """A bitmap's black and white as CopyBits maps them to pixel values
        in a cGrafPort, asking the search procedures (colour_index): the
        port's colours, black and white unless it colours what's copied."""
        if not getattr(port, "is_color", False):
            return colors
        if values:
            table = VALUE_TABLES[depth]
        else:
            table = self.device_colors()[1]
        got = []
        for rgb, color in zip((port.rgb_fg_color, port.rgb_bk_color), colors):
            value = self.search(rgb)
            got.append(table[value] if value is not None and 0 <= value < len(table) else color)
        return got[0], got[1]

    def _device_color(self, value: int, argb: int, as_is: bool = False) -> int:
        """The colour a cGrafPort's pixel value and RGB colour draw in on
        the screen.  A window's on the screen (as_is) draws in its pixel
        value, in whatever colour the screen has for it now: fgColor and
        bkColor are the pixel values of the colours drawn in, and with one
        screen QuickDraw needs no other (Inside Macintosh V, Color
        QuickDraw); a program may write them itself (Rainbow).  Otherwise,
        the nearest to the RGB colour, unless the value is reserved for
        animation, or is an entry of that same colour (one of several
        alike, as when a program fades the screen to black).  Either way
        it's written as the value."""
        devices = self.sys.devices
        table = devices.table()
        if (as_is or value in devices.reserved) and 0 <= value < len(table):
            return table[value]
        got = devices.nearest(argb)
        if devices.depth <= 8 and devices.alike(value, got):
            return table[value]
        return got

    def brush(self, port: GrafPort | None, pat: bytes, mode: int, depth: int,
              values: bool | None = None) -> tuple[QBrush, QPainter.CompositionMode]:
        """What pat paints with in a mode on pixels of depth bits, and how
        it combines: in the port's colours, or a cGrafPort's pixel
        pattern (a PixPatRef's) in its own, as near as the screen has them
        (or, drawn in pixel values, the current device's values nearest)."""
        if values is None:
            values = self._values(port)
        fg, bk = self.colors(port, depth, values)
        h = getattr(pat, "handle", 0)
        if h and depth > 1 and (not mode & 4 or arithmetic_mode(mode) == TRANSPARENT):
            shown = VALUE_TABLES[depth] if values and depth <= 8 else ()
            got = self._pix_pat_brush(h, mode, *(self.colors(port, depth, False) if shown
                                                 else (fg, bk)), shown, bk)
            if got is not None:
                return got
        return ink(bytes(pat), mode, fg, bk)

    def desk_brush(self, pat: bytes, h: int = 0) -> QBrush:
        """What the desk is painted with: PixPat h, on a screen with
        colour, or the binary pattern pat."""
        if h and self.sys.depth > 1:
            got = self._pix_pat_brush(h, patCopy, BLACK_RGB, WHITE_RGB)
            if got is not None:
                return got[0]
        return ink(bytes(pat), patCopy)[0]

    def _pix_pat_brush(self, h: int, mode: int, fg: int, bk: int,
                       shown: tuple[int, ...] = (), shown_bk: int = WHITE_RGB):
        """The brush of a pixel pattern or an RGB one; None for an old-style
        one or one there's nothing to draw of, drawn as its 1-bit pattern.
        Its colours are matched to the screen's, or, given the colours
        pixel values are shown in (shown), the current device's values,
        shown in them, with the background shown_bk."""
        mem = self.mem
        p = deref(mem, h)
        pp = PixPat(mem, p) if p else None
        if pp is None or pp.pat_type not in (pixPat, RGBPat):
            return None
        pm_p = deref(mem, pp.pat_map)
        if not pm_p:
            return None
        pm = PixMap(mem, pm_p)
        devices = self.sys.devices
        ct = deref(mem, pm.pm_table)
        if pp.pat_type == RGBPat:
            if not ct:
                return None
            rgb = (mem.r16(ct + 0x2A), mem.r16(ct + 0x2C), mem.r16(ct + 0x2E))
            if shown:
                return solid_brush(shown[self.color_index(rgb) % len(shown)], mode,
                                   shown_bk)
            argb = rgb_argb(rgb)
            return solid_brush(devices.nearest(argb) if devices is not None else argb,
                               mode, bk)
        depth, b = pm.pixel_size, pm.bounds
        row_bytes, data_p = pm.row_bytes & ROW_BYTES_MASK, deref(mem, pp.pat_data)
        width, height = b.width, b.height
        if (depth not in pixels.DEPTHS or not data_p or not 0 < width <= MAX_PATTERN_SIDE
                or not 0 < height <= MAX_PATTERN_SIDE or width * depth > row_bytes * 8):
            return None
        data = mem.read(data_p, row_bytes * height)
        table = ()
        if depth <= 8:
            size = mem.r16s(ct + 6) + 1 if ct else 0
            table = relative_table(mem.read(ct + 4, 4 + 8 * max(0, min(size, 256))) if ct
                                   else b"", depth, fg, bk)
        if shown:
            _, matching = self.device_colors(matching=True)
            screen = tuple(matching)
            shown = tuple(shown[i % len(shown)] for i in range(len(screen)))
            bk = shown_bk
        else:
            screen = (tuple(devices.matching()) if devices is not None and devices.depth <= 8
                      else ())
        return pix_pat_brush(data, width, height, row_bytes, depth, table, screen, mode & 3,
                             bk, tuple(shown))

    def clip_region(self, port: GrafPort) -> Region:
        """Where drawing in port can go: its visRgn and clipRgn, and its
        bitmap's own edges, past which there's nothing to draw on.  Inside
        Macintosh I (QuickDraw) has it within the portRect as well; macrt
        leaves that out, as Excel 4.0 needs: it draws its toolbar's buttons
        in a port whose portRect it copies from a window it made with no
        size, having set the regions to the buttons' pixel maps.  A
        window's visRgn is within its portRect anyway.  ScrollRect, which
        IM I names the portRect for itself, keeps to it (scroll_rect)."""
        clip = None
        for h in (port.vis_rgn, port.clip_rgn):
            if h:
                rgn = read_rgn(self.mem, h)
                clip = rgn if clip is None else clip.sect(rgn)
        return Region.rect(port.port_rect) if clip is None else clip

    @contextmanager
    def _on(self, port: GrafPort, surface, area: Rect | None, clip: Region | None):
        """A painter on surface, clipped to clip, with patterns lined up
        with port's bitmap as QuickDraw lines them up."""
        if surface is None or (clip is not None and clip.empty):
            yield None
            return
        if clip is not None and area is not None:
            box = clip.bbox
            area = Rect(max(area.top, box.top), max(area.left, box.left),
                        min(area.bottom, box.bottom), min(area.right, box.right))
            if area.empty:
                yield None
                return
        with surface.painter(area) as p:
            if p is not None:
                p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
                if clip is not None:
                    if clip.is_rect:
                        p.setClipRect(qrect(clip.bbox))
                    else:
                        p.setClipRegion(clip.to_qregion())
                b = port.port_bits_bounds
                p.setBrushOrigin(b.left, b.top)
            yield p
        if isinstance(surface, WindowSurface):
            self.sys.screen.drawn(port, area, clip)
            self.repaint(port)

    def painting(self, port: GrafPort | None, area: Rect | None = None,
                 mask: Region | None = None, surface=None):
        """A QPainter on port's pixels (surface, if it's known), clipped as
        macrt clips (to its visRgn and clipRgn, and mask);
        None if there's nothing to draw on."""
        if port is None:
            return self._on(port, None, area, None)
        clip = self.clip_region(port)
        if mask is not None:
            clip = clip.sect(mask)
        if self.sys.outside_port_rect is not None:
            self._outside_port_rect(port, area, clip)
        surface = surface or self.surface(port)
        if isinstance(surface, MemorySurface) and self._values(port):
            surface.in_values()
        return self._on(port, surface, area, clip)

    def _outside_port_rect(self, port: GrafPort, area: Rect | None, clip: Region) -> None:
        """Note, in the System's outside_port_rect, drawing in port that
        can land outside its portRect, which a clip to the portRect would
        cut.  One entry a port, portRect, bit map's bounds and QuickDraw
        routine that drew (painting's caller), with how many times and the
        box round where, in the port's coordinates."""
        rect, bounds = port.port_rect, port.port_bits_bounds
        extra = clip.diff(Region.rect(rect)).sect(Region.rect(bounds))
        if area is not None:
            extra = extra.sect(Region.rect(area))
        if extra.empty:
            return
        via = sys._getframe(2).f_code.co_name
        key = (port.addr, rect, bounds, via)
        seen = self.sys.outside_port_rect
        note = seen.get(key)
        if note is None:
            note = seen[key] = {"port": f"${port.addr:06X}", "port_rect": list(rect),
                                "bounds": list(bounds), "via": via, "box": extra.bbox,
                                "times": 0}
        note["box"] = note["box"].union(extra.bbox)
        note["times"] += 1

    def fill(self, port: GrafPort | None, path: QPainterPath, pat: bytes, mode: int) -> None:
        if port is None or path.isEmpty():
            return
        box = path.boundingRect().toAlignedRect()
        area = Rect(box.top(), box.left(), box.bottom() + 1, box.right() + 1)
        brush, comp = self.brush(port, pat, mode, self.depth(port))
        with self.painting(port, area) as p:
            if p is not None:
                p.setCompositionMode(comp)
                p.fillPath(path, brush)

    def _picture(self, port: GrafPort, call: str, *args) -> bool:
        """Record a drawing call in the picture port is recording, if it
        is; True if the call shouldn't draw as well: OpenPicture hides the
        pen, and while it's hidden nothing draws.  A program can stop the
        recording for a while by clearing picSave."""
        rec = self.pictures.get(port.addr)
        if rec is None or not port.pic_save:
            return False
        getattr(rec, call)(port, *args)
        return port.pn_vis < 0

    # -- the pen -------------------------------------------------------------
    def move_to(self, port: GrafPort, h: int, v: int) -> None:
        port.pn_loc = Point(v, h)
        rec = self.rgns.get(port.addr)
        if rec is not None:
            rec[1].append([Point(v, h)])

    def line_to(self, port: GrafPort, h: int, v: int) -> None:
        start, end = port.pn_loc, Point(v, h)
        port.pn_loc = end
        if (self._record_line(port, start, end) or self._picture(port, "line", start, end)
                or port.pn_vis < 0):
            return
        self.fill(port, line_path(start, end, port.pn_size), port.pn_pat, port.pn_mode)

    # -- shapes --------------------------------------------------------------
    def shape(self, port: GrafPort | None, verb: str, path_of: Callable[[Rect], QPainterPath],
              r: Rect, pat: bytes | None = None) -> None:
        """Frame, Paint, Erase, Invert or Fill a shape; path_of makes the
        shape's path for a rectangle.  QuickDraw draws nothing in an empty
        rectangle, as MacGammon's, bottom above top, is; Qt would draw the
        one its corners make."""
        if port is None or (verb == "Frame" and not r.empty
                            and self._record_shape(port, path_of(r))):
            return
        if self._picture(port, "shape", verb, path_of, r, pat) or r.empty or port.pn_vis < 0:
            return
        if verb == "Frame":
            self.fill(port, frame_path(path_of, r, port.pn_size), port.pn_pat, port.pn_mode)
        elif verb == "Paint":
            self.fill(port, path_of(r), port.pn_pat, port.pn_mode)
        elif verb == "Erase":
            self.fill(port, path_of(r), port.bk_pat, patCopy)
        elif verb == "Invert":
            self.fill(port, path_of(r), BLACK_PAT, patXor)
        elif verb == "Fill":
            self.fill(port, path_of(r), pat if pat is not None else BLACK_PAT, patCopy)

    def region(self, port: GrafPort | None, verb: str, rgn: Region,
               pat: bytes | None = None) -> None:
        """Frame, Paint, Erase, Invert or Fill a region."""
        if port is None or rgn.empty:
            return
        if verb == "Frame":
            rec = self.rgns.get(port.addr)
            if rec is not None:
                rec[0] = rec[0].xor(rgn)
                return
        if not self._picture(port, "region", verb, rgn, pat):
            self._paint_region(port, verb, rgn, pat)

    def erase_content(self, w: GrafPort, rgn: Region) -> None:
        """The Window Manager erases rgn (local) of a window's content:
        white, or a colour window's content colour (PaintOne)."""
        if w.backing is None or rgn.empty:
            return
        with self._on(w, WindowSurface(w), rgn.bbox, rgn) as p:
            if p is not None:
                p.fillRect(qrect(rgn.bbox), w.backing.paper)

    def poly(self, port: GrafPort | None, verb: str, points: list[Point],
             pat: bytes | None = None) -> None:
        """Paint, Erase, Invert or Fill a polygon (frame_poly frames one)."""
        if port is None or self._picture(port, "poly", verb, points, pat):
            return
        rgn = Region.from_path(poly_path(points))
        if not rgn.empty:
            self._paint_region(port, verb, rgn, pat)

    def _paint_region(self, port: GrafPort, verb: str, rgn: Region, pat: bytes | None) -> None:
        # A hidden pen draws nothing, whatever the verb: "whenever pnVis is
        # negative, the pen doesn't draw on the screen" (Inside Macintosh I,
        # QuickDraw, HidePen).  Evocation erases with TEInsert between
        # HidePen and ShowPen, its box then drawn by TEUpdate.
        if port.pn_vis < 0:
            return
        if verb == "Frame":
            pen = port.pn_size
            rgn = rgn.diff(rgn.inset(pen.h, pen.v))
        if verb in ("Frame", "Paint"):
            pat, mode = port.pn_pat, port.pn_mode
        elif verb == "Erase":
            pat, mode = port.bk_pat, patCopy
        elif verb == "Invert":
            pat, mode = BLACK_PAT, patXor
        else:
            pat, mode = (pat if pat is not None else BLACK_PAT), patCopy
        brush, comp = self.brush(port, pat, mode, self.depth(port))
        with self.painting(port, rgn.bbox, rgn) as p:
            if p is not None:
                p.setCompositionMode(comp)
                p.fillRect(qrect(rgn.bbox), brush)

    # -- recording regions and polygons ------------------------------------
    def open_rgn(self, port: GrafPort) -> None:
        self.rgns[port.addr] = [Region(), [[port.pn_loc]]]
        port.pn_vis = port.pn_vis - 1

    def close_rgn(self, port: GrafPort) -> Region:
        """What was drawn since OpenRgn: the shapes' outlines and closed
        loops of lines, each XORed in, as QuickDraw records them."""
        rec = self.rgns.pop(port.addr, None)
        if rec is None:
            return Region()
        port.pn_vis = port.pn_vis + 1
        rgn = rec[0]
        for line in rec[1]:
            if len(line) >= 3:
                rgn = rgn.xor(Region.from_path(poly_path(line)))
        return rgn

    def _record_shape(self, port: GrafPort, path: QPainterPath) -> bool:
        rec = self.rgns.get(port.addr)
        if rec is None:
            return False
        rec[0] = rec[0].xor(Region.from_path(path))
        return True

    def _record_line(self, port: GrafPort, a: Point, b: Point) -> bool:
        recorded = False
        poly = self.polys.get(port.addr)
        if poly is not None:
            if not poly[1]:
                poly[1].append(a)
            poly[1].append(b)
            recorded = True
        rec = self.rgns.get(port.addr)
        if rec is not None:
            if not rec[1] or rec[1][-1][-1] != a:
                rec[1].append([a])
            rec[1][-1].append(b)
            recorded = True
        return recorded

    def open_poly(self, port: GrafPort, h: int) -> None:
        self.polys[port.addr] = [h, []]
        port.pn_vis = port.pn_vis - 1

    def close_poly(self, port: GrafPort) -> None:
        rec = self.polys.pop(port.addr, None)
        if rec is not None:
            port.pn_vis = port.pn_vis + 1
            write_poly(self.sys.heap_of(rec[0]), rec[0], rec[1])

    def frame_poly(self, port: GrafPort | None, points: list[Point]) -> None:
        """The lines from point to point; closed only if the last point is
        the first."""
        if port is None or len(points) < 2:
            return
        port.pn_loc = points[-1]
        if self._picture(port, "poly", "Frame", points, None) or port.pn_vis < 0:
            return
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.WindingFill)
        for a, b in zip(points, points[1:]):
            path.addPath(line_path(a, b, port.pn_size))
        self.fill(port, path, port.pn_pat, port.pn_mode)

    # -- text ----------------------------------------------------------------
    def font(self, port: GrafPort | None) -> QFont:
        return self.fonts.for_port(port) if port is not None else self.fonts.font(0, 12, 0)

    def strike(self, port: GrafPort | None):
        """The bitmap font the port's text is drawn in, and its scale, if
        it's a family of the application's own rather than a standard one,
        or a standard one it has its own strike of (AppFonts.source)."""
        if port is None or self.app_fonts is None:
            return None
        return self.app_fonts.find(port.tx_font, port.tx_size)

    def _advances(self, port: GrafPort | None, text: str) -> list[int]:
        """fonts.advances in the port's host font."""
        if port is None:
            return self.fonts.advances(0, 12, 0, text)
        return self.fonts.advances(port.tx_font, port.tx_size, port.tx_face, text,
                                   port.sp_extra >> 16)

    def text_width(self, port: GrafPort | None, text: str) -> int:
        found = self.strike(port)
        if found is not None:
            strike, scale = found
            w = strike.width(char_codes(text), port.tx_face, port.sp_extra >> 16)
            return round(w * scale)
        return sum(self._advances(port, text))

    def text_positions(self, port: GrafPort | None, text: str) -> list[int]:
        """MeasureText: where the pen is after each character of text, from
        0 before the first."""
        return list(accumulate(self._advances(port, text), initial=0))

    def host_widths(self, family: int, size: int, face: int, text: str) -> list[int]:
        """How far the pen moves for each character of text in the host font
        standing in for a standard family, as text_width adds them up."""
        return self.fonts.advances(family, size, face, text)

    def host_font_info(self, family: int, size: int, face: int) -> tuple[int, int, int, int]:
        return self.fonts.info(family, size, face)

    def font_info(self, port: GrafPort | None) -> tuple[int, int, int, int]:
        """ascent, descent, widMax, leading"""
        found = self.strike(port)
        if found is not None:
            st, scale = found
            return tuple(round(n * scale) for n in (st.ascent, st.descent, st.wid_max, st.leading))
        if port is None:
            return self.fonts.info(0, 12, 0)
        return self.fonts.info(port.tx_font, port.tx_size, port.tx_face)

    def draw_text(self, port: GrafPort | None, text: str, mask: Region | None = None) -> None:
        """Draw text at the pen in the port's font, style and text mode, and
        move the pen past it.  mask clips it further (TextEdit's viewRect)."""
        if port is None or not text:
            return
        found = self.strike(port)
        if found is not None:
            self._draw_strike(port, text, *found, mask)
            return
        font = self.font(port)
        fm = QFontMetrics(font)
        pen = port.pn_loc
        advances = self._advances(port, text)
        width = sum(advances)
        hidden = self._picture(port, "text", text, lambda s: self.text_width(port, s))
        port.pn_loc = pen.offset(width, 0)
        if hidden or port.pn_vis < 0:
            return
        face = port.tx_face
        extra = 2 if face & (outline | shadow) else 0
        if port.tx_mode & 7 in (srcOr, srcXor, srcBic) or port.tx_mode == TRANSPARENT:
            # White leaves what's there: a pixel more all round, for what
            # the host's glyphs draw past their advances.
            area = Rect(pen.v - fm.ascent() - 1, pen.h - 1, pen.v + fm.descent() + extra + 1,
                        pen.h + width + extra + 1)
        else:
            # The text's own box, which the mode fills: none of the character
            # before it (Mac Pro Football draws a character at a time, in
            # srcCopy).
            ascent, descent, _, _ = self.font_info(port)
            area = Rect(pen.v - ascent, pen.h, pen.v + descent + extra, pen.h + width + extra)
        # The text black on white, then drawn in the text mode.
        img = QImage(max(area.width, 1), max(area.height, 1), QImage.Format.Format_RGB32)
        img.fill(QColor(255, 255, 255))
        p = QPainter(img)
        p.setFont(font)
        p.setPen(QColor(0, 0, 0))
        x, y = pen.h - area.left, pen.v - area.top
        # Where the font has nothing to draw, the host font would draw a box.
        blanks = self.fonts.blanks(port.tx_font, port.tx_size, text)
        drawn = [(px, ch) for px, ch in zip(accumulate(advances, initial=0), text)
                 if ch not in blanks]
        if face & (outline | shadow):
            for dx, dy in style_offsets(bool(face & shadow)):
                for px, ch in drawn:
                    p.drawText(x + px + dx, y + dy, ch)
            p.setPen(QColor(255, 255, 255))
        for px, ch in drawn:
            p.drawText(x + px, y, ch)
        p.end()
        src, comp = source_image(img, port.tx_mode, *self.colors(port, self.depth(port)))
        with self.painting(port, area, mask) as dst:
            if dst is not None:
                dst.setCompositionMode(comp)
                dst.drawImage(QPoint(area.left, area.top), src)

    def _draw_strike(self, port: GrafPort, text: str, strike, scale: float,
                     mask: Region | None) -> None:
        """draw_text in a bitmap font."""
        pen = port.pn_loc
        width = self.text_width(port, text)
        hidden = self._picture(port, "text", text, lambda s: self.text_width(port, s))
        port.pn_loc = pen.offset(width, 0)
        if hidden or port.pn_vis < 0:
            return
        glyphs, left, baseline = strike.render(char_codes(text), port.tx_face,
                                               port.sp_extra >> 16)
        if scale != 1.0:
            glyphs = glyphs.scaled(max(round(glyphs.width() * scale), 1),
                                   max(round(glyphs.height() * scale), 1),
                                   Qt.AspectRatioMode.IgnoreAspectRatio,
                                   Qt.TransformationMode.FastTransformation)
            left, baseline = round(left * scale), round(baseline * scale)
        area = Rect(pen.v - baseline, pen.h - left, pen.v - baseline + glyphs.height(),
                    pen.h - left + glyphs.width())
        # Black on white, then drawn in the text mode, as a host font is.
        img = QImage(glyphs.size(), QImage.Format.Format_RGB32)
        img.fill(QColor(255, 255, 255))
        p = QPainter(img)
        p.drawImage(QPoint(0, 0), glyphs)
        p.end()
        src, comp = source_image(img, port.tx_mode, *self.colors(port, self.depth(port)))
        with self.painting(port, area, mask) as dst:
            if dst is not None:
                dst.setCompositionMode(comp)
                dst.drawImage(QPoint(area.left, area.top), src)

    def text_box(self, port: GrafPort | None, text: str, r: Rect, just: int) -> None:
        """TETextBox: erase r and draw the text in it, wrapped at words."""
        if port is None:
            return
        self.fill(port, rect_path(r), port.bk_pat, patCopy)
        align = {0: Qt.AlignmentFlag.AlignLeft, 1: Qt.AlignmentFlag.AlignHCenter,
                 -1: Qt.AlignmentFlag.AlignRight}.get(just, Qt.AlignmentFlag.AlignLeft)
        with self.painting(port, r) as p:
            if p is not None:
                p.setFont(self.font(port))
                p.setPen(QColor(0, 0, 0))
                p.drawText(qrect(r), align | Qt.TextFlag.TextWordWrap, text.replace("\r", "\n"))

    # -- bits ----------------------------------------------------------------
    def bitmap(self, bm: int, current: GrafPort | None = None) -> tuple[object, GrafPort | None]:
        """The surface a BitMap pointer is, and the port whose portBits it
        is, if it's one's.  It may be a cGrafPort's portBits, whose
        baseAddr is a PixMap handle (rowBytes' top two bits set), or a
        PixMap (only the top one), perhaps a port's.  A PixMap is in the
        colours of the current port's palette, if its table says so.  A
        BitMap on a colour screen is the screen's PixMap (on_screen).  A
        copy of the current port's portBits, or of its PixMap's first 14
        bytes, is its bits too, as Executor finds (qBit.cpp): Legends of
        the Lost Realm copies its title through one, into a window
        shorter than the picture."""
        port = self.sys.port_at(bm - 2)
        if port is None and current is not None and self._copies_bits(bm, current):
            port = current
        if port is not None:
            return self.surface(port, current), port
        mem = self.mem
        row_bytes = mem.r16(bm + 4)
        if row_bytes & PIXMAP_FLAG:
            pm = deref(mem, mem.r32(bm)) if row_bytes & 0x4000 else bm & ADDRESS_MASK
            port = self.sys.port_with_pixmap(pm) if pm else None
            if port is not None:
                return self.surface(port, current), port
            return (MemorySurface.of_pixmap(mem, pm, self._palette(current), self.tableless)
                    if pm else None), None
        base, bounds = mem.r32(bm), mem.r_rect(bm + 6)
        return (self.on_screen(base, bounds)
                or MemorySurface.make(mem, base, row_bytes, bounds)), None

    def _copies_bits(self, bm: int, port: GrafPort) -> bool:
        bits = self.mem.read(bm, 14)
        if bits == self.mem.read(port.addr + 2, 14):
            return True
        pm = port.pixmap() if port.is_color else 0
        return bool(pm) and bits == self.mem.read(pm, 14)

    def bits_depth(self, surface) -> int:
        return self.sys.depth if isinstance(surface, WindowSurface) else getattr(surface,
                                                                                 "depth", 1)

    def copy_bits(self, src_bm: int, dst_bm: int, src_r: Rect, dst_r: Rect, mode: int,
                  mask: Region | None, current: GrafPort | None) -> None:
        """CopyBits: src_r of one bitmap to dst_r of another, scaled to fit.
        Clipped to the current port's regions when drawing in its bits.
        From a PixMap, or a window on a colour screen, it copies colours;
        from a PixMap to one whose pixel values are the same colours
        (same_values), in srcCopy, the values as they are, and in
        transparent, with a cGrafPort current, those that aren't its
        bkColor.  TerraTopia gives its offscreen PixMap the screen's seed,
        not its colours, and draws its sprites in it in transparent."""
        if src_r.empty or dst_r.empty:
            return
        src, _ = self.bitmap(src_bm, current)
        if src is None:
            return
        dst, dst_port = target = self.bitmap(dst_bm, current)
        as_is = mode & ~DITHER_COPY == srcCopy or (
            mode & ~DITHER_COPY == TRANSPARENT and getattr(current, "is_color", False))
        values = (as_is and isinstance(src, MemorySurface) and src.same_values(dst)
                  and (dst_port is None or dst_port.addr not in self.pictures))
        if values:
            src.in_values()
            dst.in_values()
        elif isinstance(src, MemorySurface) and isinstance(dst, WindowSurface):
            src = self._as_shown(src)
        self.draw_bits(dst_bm, dst_r, src.image(src_r), mode, mask, current,
                       color=self.bits_depth(src) > 1, target=target, values=values)

    def _as_shown(self, src: MemorySurface) -> MemorySurface:
        """src, to be copied to a window: if its pixel values are the main
        screen's (its colour table, or one with the same seed), in the
        colours the screen shows for them, each index reserved for
        animation in one unlike any other's, so that what's copied is
        written in the screen buffer as the same values, and changes
        colour as they're animated (see gdevice.apart).  Lemmings fades
        its title in that way."""
        screen = self.on_screen(self.sys.screen_bits.base, self.sys.screen_bits.bounds)
        if screen is None or not src.same_values(screen) or src.table == screen.table:
            return src
        shown = copy.copy(src)
        shown.table = screen.table
        return shown

    def draw_bits(self, dst_bm: int, dst_r: Rect, img: QImage, mode: int, mask: Region | None,
                  current: GrafPort | None, color: bool = False,
                  target: tuple[object, GrafPort | None] | None = None,
                  values: bool = False) -> None:
        """Draw an image into dst_r of a BitMap as CopyBits draws bits:
        black and white, scaled, in a source mode and the current port's
        colours, clipped to the port's regions when they're its bits; or,
        if it's in colour and so are they, in its own colours
        (color_source).  target is the BitMap's bitmap(), if known.  On a
        PixMap of 2 to 8 bits, with a cGrafPort current, it's drawn in
        pixel values (in_values): img's colours the nearest the PixMap
        has, unless they're its values already (values); in a black
        foreground and a white background, in a source mode but Copy,
        combined with the PixMap's bit by bit (VALUE_OPS)."""
        if dst_r.empty:
            return
        dst, dst_port = target or self.bitmap(dst_bm, current)
        on_port = dst_port is not None and dst_port is current
        if on_port and self._picture(dst_port, "bits", img, dst_r, mode, mask, color):
            return
        if dst is None:
            return
        depth = self.bits_depth(dst)
        if mode & ~DITHER_COPY in ONE_BIT_MODES and depth > 1:
            if (img.width(), img.height()) != (dst_r.width, dst_r.height):
                img = img.scaled(dst_r.width, dst_r.height)
            img = arithmetic(img, self._colors_of(dst, dst_r), mode & ~DITHER_COPY,
                             self.op_color(current))
            mode, color, values = srcCopy, True, False
        in_values = isinstance(dst, MemorySurface) and depth <= 8 and (
            values or depth > 1 and getattr(current, "is_color", False))
        if in_values:
            dst.in_values()
            if color and not values:
                img = dst.values(img)
        colors = self.colors(current, depth, in_values)
        if self.search is not None and not color and depth > 1:
            colors = self._search_colors(current, depth, in_values, colors)
        op = (VALUE_OPS.get(mode & ~DITHER_COPY)
              if in_values and color and colors == (BLACK_RGB, WHITE_RGB) else None)
        got = color_source(img, mode, *colors) if color and depth > 1 and op is None else None
        if op is not None:
            img, comp = img.convertToFormat(QImage.Format.Format_RGB32), op
        elif got is not None:
            img, comp = got
        else:
            img, comp = source_image(img, mode & ~DITHER_COPY, *colors)
        if on_port:
            ctx = self.painting(dst_port, dst_r, mask, dst)
        else:
            ctx = self._on(dst_port or current or _NoPort, dst, dst_r, mask)
        with ctx as p:
            if p is not None:
                p.setCompositionMode(comp)
                p.drawImage(qrect(dst_r), img)

    @staticmethod
    def _colors_of(surface, r: Rect) -> QImage:
        """The pixels in r of what's drawn on, in their colours, though it's
        drawn on in pixel values (MemorySurface.in_values)."""
        own = getattr(surface, "own", None)
        if own is None:
            return surface.image(r)
        values, surface.table = surface.table, own
        try:
            return surface.image(r)
        finally:
            surface.table = values

    def op_color(self, port: GrafPort | None) -> tuple[int, int, int] | None:
        """A cGrafPort's OpColor, its GrafVars' rgbOpColor; None for an old
        port, which has none."""
        if port is None or not getattr(port, "is_color", False):
            return None
        gv = deref(self.mem, port.graf_vars)
        return (self.mem.r16(gv), self.mem.r16(gv + 2), self.mem.r16(gv + 4)) if gv else None

    def scroll_rect(self, port: GrafPort | None, r: Rect, dh: int, dv: int) -> Region:
        """ScrollRect: move the bits in r by dh, dv, erase what's left
        behind, and return that.  IM I's ScrollRect moves only the bits
        within the port's portRect, visRgn, clipRgn and bitmap."""
        if port is None:
            return Region()
        surface = self.surface(port)
        clip = self.clip_region(port).sect(Region.rect(r.sect(port.port_rect)))
        if surface is None or clip.empty:
            return Region()
        if isinstance(surface, MemorySurface) and self._values(port):
            surface.in_values()
        box = clip.bbox
        img = surface.image(box)
        moved = clip.offset(dh, dv).sect(clip)
        vacated = clip.diff(moved)
        with self._on(port, surface, box, moved) as p:
            if p is not None:
                p.drawImage(QPoint(box.left + dh, box.top + dv), img)
        brush, _ = self.brush(port, port.bk_pat, patCopy, self.depth(port))
        with self._on(port, surface, box, vacated) as p:
            if p is not None:
                p.fillRect(qrect(box), brush)
        return vacated

    def _reading(self, port: GrafPort | None, pt: Point):
        """Where GetPixel and GetCPixel read port's pixel at pt, and the
        point there.  A window's Backing, where the window has a pixel;
        elsewhere the screen buffer at that point.  The pixel needn't be
        the port's, IM I's GetPixel says: another port may have drawn it.
        MonsterPaint picks colours from its palette window at a point
        local to its tools window."""
        surface = self.surface(port) if port is not None else None
        if isinstance(surface, WindowSurface) and surface.rgb(pt) is None:
            b = port.port_bits_bounds
            return self.sys.screen.bits, Point(pt.v - b.top, pt.h - b.left)
        return surface, pt

    def get_pixel(self, port: GrafPort | None, pt: Point) -> bool:
        surface, at = self._reading(port, pt)
        return surface.pixel(at) if surface is not None else False

    def get_rgb(self, port: GrafPort | None, pt: Point) -> int:
        """GetCPixel: the colour of the pixel at pt, 0xAARRGGBB; white
        where there's none."""
        surface, at = self._reading(port, pt)
        rgb = surface.rgb(at) if surface is not None else None
        return WHITE_RGB if rgb is None else rgb

    def set_rgb(self, port: GrafPort | None, pt: Point, argb: int) -> None:
        """SetCPixel: the pixel at pt the colour nearest argb the port's
        pixels have, clipped as drawing is."""
        if port is None:
            return
        depth = self.depth(port)
        devices = self.sys.devices
        if depth == 1 or devices is None:
            argb = mono_argb(argb)
        elif self._values(port):
            shown = VALUE_TABLES[depth]
            rgb = tuple((argb >> shift & 0xFF) * 0x101 for shift in (16, 8, 0))
            argb = shown[self.color_index(rgb) % len(shown)]
        else:
            argb = devices.nearest(argb)
        with self.painting(port, Rect(pt.v, pt.h, pt.v + 1, pt.h + 1)) as p:
            if p is not None:
                p.fillRect(QRect(pt.h, pt.v, 1, 1), QColor.fromRgba(argb))


class _NoPort:
    """For CopyBits into a bare BitMap: patterns line up at its (0, 0)."""
    port_bits_bounds = Rect(0, 0, 0, 0)
    is_color = False


# =========================================================================
# Polygons: polySize, polyBBox, then the points
# =========================================================================
def read_poly(mem: RAM, h: int) -> list[Point]:
    p = mem.r32(h) & ADDRESS_MASK if h else 0
    if not p:
        return []
    size = mem.r16(p)
    n = max(size - 10, 0) // 4
    return [mem.r_point(p + 10 + 4 * i) for i in range(n)]


def write_poly(heap: Heap, h: int, points: list[Point]) -> None:
    size = 10 + 4 * len(points)
    if heap.handle_size(h) != size and not heap.set_handle_size(h, size):
        return
    p = heap.deref(h)
    mem = heap.mem
    mem.w16(p, size)
    if points:
        bbox = Rect(min(pt.v for pt in points), min(pt.h for pt in points),
                    max(pt.v for pt in points), max(pt.h for pt in points))
    else:
        bbox = EMPTY
    mem.w_rect(p + 2, bbox)
    for i, pt in enumerate(points):
        mem.w_point(p + 10 + 4 * i, pt)
