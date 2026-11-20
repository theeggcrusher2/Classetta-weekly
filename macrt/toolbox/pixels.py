"""
Pixels in guest memory as host images, and back: a BitMap's 1 bit a
pixel, or a PixMap's 1, 2, 4 or 8 bits of a colour table's index, or 16
or 32 bits of the colour itself (Inside Macintosh V-52).

    16 bits   x, then 5 bits each of red, green and blue
    32 bits   a byte unused, then a byte each of red, green and blue

A table is a list of 0xAARRGGBB colours by pixel value.  Writing a colour
that isn't in the table writes the nearest that is, as Qt finds it.
"""

from __future__ import annotations

import sys
from array import array

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPainter

WHITE_RGB = 0xFFFFFFFF
BLACK_RGB = 0xFF000000
MONO_TABLE = [WHITE_RGB, BLACK_RGB]     # bit 0 white, bit 1 black, as on a Mac
DEPTHS = (1, 2, 4, 8, 16, 32)
_THRESHOLD = Qt.ImageConversionFlag.ThresholdDither


def _places(depth: int, unpack: bool) -> list[bytes]:
    """For each place of a depth-bit pixel in a byte, leftmost first, a
    table for bytes.translate: from a byte, the pixel there (unpack), or
    from a pixel value, its low bits in that place."""
    mask = (1 << depth) - 1
    shifts = range(8 - depth, -1, -depth)
    if unpack:
        return [bytes(b >> s & mask for b in range(256)) for s in shifts]
    return [bytes((v & mask) << s for v in range(256)) for s in shifts]


_PIXELS = {depth: _places(depth, True) for depth in (2, 4)}
_PLACED = {depth: _places(depth, False) for depth in (2, 4)}


def _unpack(data: bytes, depth: int) -> bytes:
    """2- or 4-bit pixels a byte each."""
    n = 8 // depth
    out = bytearray(len(data) * n)
    for i, pixel in enumerate(_PIXELS[depth]):
        out[i::n] = data.translate(pixel)
    return bytes(out)


def _pack(data: bytes, depth: int) -> bytes:
    """Bytes of pixel values packed depth bits each: each value's low
    bits (a Qt image's rows are padded with bytes it leaves as they were).
    Each place's bits are put in place by a table and the places joined
    as one number, the pixels' bits never overlapping."""
    n = 8 // depth
    if len(data) % n:
        data = bytes(data) + bytes(n - len(data) % n)
    packed = 0
    for i, placed in enumerate(_PLACED[depth]):
        packed |= int.from_bytes(data[i::n].translate(placed), "big")
    return packed.to_bytes(len(data) // n, "big")


def _swapped(data: bytes, size: int) -> bytes:
    """data with each size-byte word's bytes reversed: big-endian pixels
    as the host's (little-endian) formats have them."""
    words = array("H" if size == 2 else "I", data)
    words.byteswap()
    return words.tobytes()


_GREY_READY = (QImage.Format.Format_RGB32, QImage.Format.Format_ARGB32,
               QImage.Format.Format_ARGB32_Premultiplied)


def mono(img: QImage) -> QImage:
    """img as a 1-bit image in MONO_TABLE, each pixel the nearer of white
    (0) and black (1), as Qt matches a colour to a table.  Where every
    pixel is a grey, Qt's own threshold makes the same choice (black below
    128) far more quickly than matching each pixel.  (Qt keeps a 1-bit
    image's values as they are, whatever its table's colours.)"""
    if img.format() not in _GREY_READY and img.colorTable() != MONO_TABLE:
        img = img.convertToFormat(QImage.Format.Format_ARGB32)
    if img.format() in _GREY_READY and img.allGray():
        return img.convertToFormat(QImage.Format.Format_Mono, _THRESHOLD)
    return img.convertToFormat(QImage.Format.Format_Mono, MONO_TABLE, _THRESHOLD)


def decode(data: bytes, width: int, height: int, row_bytes: int, depth: int,
           table: list[int] | None = None) -> QImage:
    """Rows of pixels, row_bytes apart, as an RGB32 image width pixels wide."""
    if depth > 8 and row_bytes % (depth // 8):
        # rows that end part-way into a pixel: each row's whole pixels
        n = width * depth // 8
        data = b"".join(data[y * row_bytes:y * row_bytes + n] for y in range(height))
        row_bytes = n
    if depth == 1:
        img = QImage(data, width, height, row_bytes, QImage.Format.Format_Mono)
        img.setColorTable(table or MONO_TABLE)
    elif depth in (2, 4):
        wide = _unpack(data, depth)
        img = QImage(wide, width, height, row_bytes * (8 // depth), QImage.Format.Format_Indexed8)
        img.setColorTable(_full(table, depth))
    elif depth == 8:
        img = QImage(data, width, height, row_bytes, QImage.Format.Format_Indexed8)
        img.setColorTable(_full(table, depth))
    elif depth == 16:
        img = QImage(_swapped(data, 2), width, height, row_bytes, QImage.Format.Format_RGB555)
    else:
        img = QImage(_swapped(data, 4), width, height, row_bytes, QImage.Format.Format_RGB32)
    if img.format() == QImage.Format.Format_RGB32:
        # already RGB32: convertToFormat shares data's buffer rather than
        # copying, and data doesn't outlive this function
        img = img.copy()
    return img.convertToFormat(QImage.Format.Format_RGB32)


def _full(table: list[int] | None, depth: int) -> list[int]:
    """table, with as many entries as depth has values: black for any it lacks."""
    n = 1 << depth
    table = list(table or MONO_TABLE)[:n]
    return table + [BLACK_RGB] * (n - len(table))


def encode(img: QImage, depth: int, table: list[int] | None = None,
           match: list[int] | None = None) -> tuple[bytes, int]:
    """img's pixels at depth: the bytes, and how many there are a row
    (rows padded as Qt pads them, to 4 bytes; a 2- or 4-bit image's rows
    are packed from its 8-bit ones, so as long as they need be).  Given
    match, table's colours with those of values no colour is matched to
    made another's (Devices.matching), a pixel is written as one of those
    values only if it's exactly that value's colour in table, and if not
    as the nearest of match's (see _matched)."""
    if depth == 1:
        if table is None or table == MONO_TABLE:
            out = mono(img)
        else:
            out = img.convertToFormat(QImage.Format.Format_Mono, table, _THRESHOLD)
        return raw(out), out.bytesPerLine()
    if depth <= 8:
        full = _full(table, depth)
        out = img.convertToFormat(QImage.Format.Format_Indexed8, full, _THRESHOLD)
        data, bpl = raw(out), out.bytesPerLine()
        if match is not None and match is not table:
            data = _matched(img, data, bpl, full, _full(match, depth))
        if depth == 8:
            return data, bpl
        return _pack(data, depth), bpl * depth // 8
    if depth == 16:
        out = img.convertToFormat(QImage.Format.Format_RGB555)
        return _swapped(raw(out), 2), out.bytesPerLine()
    out = img.convertToFormat(QImage.Format.Format_RGB32)
    return _swapped(raw(out), 4), out.bytesPerLine()


_NONZERO = bytes([0]) + bytes([1]) * 255       # each byte, 1 if it isn't 0
_ONES = bytes([0, 0xFF]) + bytes(254)          # 1, all ones
_RGB = 0 if sys.byteorder == "little" else 1   # where an RGB32 pixel's colour bytes start
_EXACT = (0xFF000000).to_bytes(4, sys.byteorder)


def _matched(img: QImage, data: bytes, bpl: int, table: list[int], match: list[int]) -> bytes:
    """data, img's pixels as the values of table's colours nearest theirs
    (8 bits each, bpl a row), with each that is a value match leaves out
    (whose colour isn't table's) but isn't exactly that colour written as
    the nearest of match's instead.  The screen's entries reserved for
    animation are kept as colours unlike any other (gdevice.apart): a
    pixel drawn in one is that colour, and has to be written as its
    value, to change as it does; a colour copied from a PixMap of other
    colours is matched, as Color QuickDraw matches it, to the others."""
    kept = bytes(a != b for a, b in zip(table, match)).ljust(256, b"\0")
    hit = data.translate(kept)
    if 1 not in hit:
        return data
    w, h = img.width(), img.height()
    shown = QImage(data, w, h, bpl, QImage.Format.Format_Indexed8)
    shown.setColorTable(table)
    x = shown.convertToFormat(QImage.Format.Format_RGB32)
    p = QPainter(x)
    p.setCompositionMode(QPainter.CompositionMode.RasterOp_SourceXorDestination)
    p.drawImage(0, 0, img)
    p.end()
    x = raw(x)                  # a pixel's colour 0 where it's exactly what it's written as
    rb, exact = 4 * w, _EXACT * w
    need = [y for y in range(h)
            if x[y * rb:y * rb + rb] != exact and 1 in hit[y * bpl:y * bpl + w]]
    if not need:
        return data
    top, bottom = need[0], need[-1] + 1
    rows, lo, hi = bottom - top, top * bpl, bottom * bpl
    x = x[top * rb:bottom * rb].translate(_NONZERO)
    differ = (int.from_bytes(x[_RGB::4], "big") | int.from_bytes(x[_RGB + 1::4], "big")
              | int.from_bytes(x[_RGB + 2::4], "big")).to_bytes(rows * w, "big")
    if bpl != w:
        pad = bytes(bpl - w)
        differ = b"".join([differ[y * w:y * w + w] + pad for y in range(rows)])
    redo = int.from_bytes(hit[lo:hi], "big") & int.from_bytes(differ, "big")
    if not redo:
        return data
    mask = int.from_bytes(redo.to_bytes(hi - lo, "big").translate(_ONES), "big")
    band = img.copy(0, top, w, rows)
    other = raw(band.convertToFormat(QImage.Format.Format_Indexed8, match, _THRESHOLD))
    part = int.from_bytes(data[lo:hi], "big") & ~mask | int.from_bytes(other, "big") & mask
    return data[:lo] + part.to_bytes(hi - lo, "big") + data[hi:]


def raw(img: QImage) -> bytes:
    """An image's bytes, as Qt keeps them."""
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    return bytes(ptr)


def value_rgb(value: int, depth: int, table: list[int] | None = None) -> int:
    """The colour (0xAARRGGBB) a pixel value is."""
    if depth <= 8:
        return _full(table, depth)[value & ((1 << depth) - 1)]
    if depth == 16:
        r, g, b = (value >> 10) & 31, (value >> 5) & 31, value & 31
        return 0xFF000000 | (r << 19 | (r >> 2) << 16) | (g << 11 | (g >> 2) << 8) | b << 3 | b >> 2
    return 0xFF000000 | (value & 0xFFFFFF)


def rgb_value(rgb: int, depth: int, table: list[int] | None = None) -> int:
    """The pixel value nearest a colour (0xAARRGGBB)."""
    if depth <= 8:
        img = QImage(1, 1, QImage.Format.Format_RGB32)
        img.setPixel(0, 0, rgb)
        fmt = QImage.Format.Format_Mono if depth == 1 else QImage.Format.Format_Indexed8
        return img.convertToFormat(fmt, _full(table, depth), _THRESHOLD).pixelIndex(0, 0)
    if depth == 16:
        return ((rgb >> 19 & 31) << 10) | ((rgb >> 11 & 31) << 5) | (rgb >> 3 & 31)
    return rgb & 0xFFFFFF
