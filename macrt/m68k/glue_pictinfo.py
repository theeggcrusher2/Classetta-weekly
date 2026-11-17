"""
The Picture Utilities (Pack15; Imaging With QuickDraw, Pictures,
"Collecting Picture Information"): GetPictInfo and GetPixMapInfo for one
picture, pixel map or bitmap, and NewPictInfo, RecordPictInfo,
RecordPixMapInfo, RetrievePictInfo and DisposePictInfo for a survey of
many.  What they count and how they pick colours is
macrt.toolbox.pictutil's.

A PictInfo record's colours come in a new ColorTable, colorsRequested
entries with black for those not found, and a new Palette of the colours
found, none on a machine without Color QuickDraw.  How the palette's
entries are used isn't said: here they're tolerant, with a tolerance of
0, so that a window given the palette shows them exactly.  A
colour-picking method of the program's own ('cpmt', an ID above 127)
isn't run: the popular method picks instead.
"""

from __future__ import annotations

import logging
import struct

from ..toolbox import pict
from ..toolbox.color import color_table
from ..toolbox.gdevice import PIXMAP_FLAG, ROW_BYTES_MASK, PixMap, deref
from ..toolbox.memmgr import memFullErr, noErr
from ..toolbox.pictutil import FIXED_72, Survey, image_colors, table_colors
from ..toolbox.qdraw import MemorySurface
from ..toolbox.types import Rect
from .dispatch import trap
from .glue_color import GetCTSeed
from .glue_palette import _fill, _new_palette, _record, pmTolerant
from .glue_pict import _picture_heap, _picture_reader
from .glue_common import _port
from .glue_fonts import family_name, family_named

log = logging.getLogger("macrt.pictinfo")

returnColorTable, returnPalette, recordComments, recordFontInfo = 1, 2, 4, 8
suppressBlackAndWhite = 16
VERBS = 0x1F
pictInfoVersionErr, pictInfoIDErr, pictInfoVerbErr = -11000, -11001, -11002
cantLoadPickMethodErr, colorsRequestedErr, pictureDataErr = -11003, -11004, -11005
CUSTOM_METHODS = 128        # the lowest ID of a 'cpmt'

# PictInfo: version, uniqueColors, thePalette, theColorTable, hRes, vRes,
# depth, sourceRect, the twelve counts, commentHandle, uniqueFonts,
# fontHandle, fontNamesHandle, reserved1 and reserved2.
PICT_INFO = struct.Struct(">hlLLllh4h12lLlLLll")
FONT_SPEC = struct.Struct(">hh4LhL")
COUNTS = ("text", "line", "rect", "rRect", "oval", "arc", "poly", "region", "bitMap",
          "pixMap")


class Surveyed:
    """A NewPictInfo's survey and how to report it."""

    def __init__(self, verb: int, colors: int, method: int):
        self.survey = Survey()
        self.verb, self.colors, self.method = verb, colors, method


def _check(t, verb: int, colors: int, method: int, version: int) -> int:
    if version != 0:
        return pictInfoVersionErr
    if verb & ~VERBS & 0xFFFF:
        return pictInfoVerbErr
    if verb & (returnColorTable | returnPalette):
        if not 1 <= colors <= 256:
            return colorsRequestedErr
        if method >= CUSTOM_METHODS:
            if not t.process.resources.GetResource("cpmt", method):
                return cantLoadPickMethodErr
            log.warning("cpmt %d: a program's own colour-picking method isn't run", method)
        elif not 0 <= method <= 2:
            return cantLoadPickMethodErr
    return noErr


def _fonts(t, survey: Survey) -> tuple[bytes, bytes]:
    """The FontSpec records and the font names they point into, each a
    Pascal string."""
    specs, names = b"", b""
    for pict_id, use in survey.fonts.items():
        name = use.name if use.name is not None else family_name(t, pict_id)
        family = family_named(t, use.name) if use.name else None
        sizes = [use.sizes >> 32 * k & 0xFFFFFFFF for k in range(4)]
        specs += FONT_SPEC.pack(pict_id, pict_id if family is None else family, *sizes,
                                use.style, len(names))
        encoded = name.encode("mac_roman", "replace")[:255]
        names += bytes([len(encoded)]) + encoded
    return specs, names


def _write(t, p: int, survey: Survey, verb: int, colors: int, method: int,
           pixels_only: bool = False) -> int:
    """Fill in the PictInfo record at p.  With no room for one of its
    handles, it's memFullErr, the record as it was and the handles already
    made disposed of."""
    heap = t.process.heap
    made: list[int] = []

    def kept(h: int) -> int:
        if h:
            made.append(h)
        return h

    def no_room() -> int:
        for h in made:
            heap.dispose_handle(h)
        return memFullErr
    palette = ctab = comments_h = fonts_h = names_h = 0
    color_qd = t.system.devices is not None
    if color_qd and verb & (returnColorTable | returnPalette):
        picked = survey.pick(colors, method, not verb & suppressBlackAndWhite)
        if verb & returnColorTable:
            padded = picked + [(0, 0, 0)] * (colors - len(picked))
            ctab = kept(heap.new_handle_with(color_table(padded, GetCTSeed(t))))
            if not ctab:
                return no_room()
        if verb & returnPalette:
            palette = kept(_new_palette(t, len(picked)))
            if not palette:
                return no_room()
            _fill(t, _record(t, palette), picked, pmTolerant, 0)
    counts = dict(survey.counts)
    unique_comments = unique_fonts = 0
    if pixels_only or not verb & recordComments:
        counts["comment"] = 0
    else:
        counts["comment"] = sum(survey.comments.values())
        unique_comments = len(survey.comments)
        comments_h = kept(heap.new_handle_with(b"".join(
            struct.pack(">hh", min(n, 0x7FFF), kind) for kind, n in survey.comments.items())))
        if not comments_h:
            return no_room()
    if not pixels_only and verb & recordFontInfo:
        specs, names = _fonts(t, survey)
        unique_fonts = len(survey.fonts)
        fonts_h, names_h = (kept(heap.new_handle_with(x)) for x in (specs, names))
        if not fonts_h or not names_h:
            return no_room()
    width, height = survey.size
    t.mem.write(p, PICT_INFO.pack(
        0, len(survey.colors), palette, ctab, survey.h_res, survey.v_res, max(survey.depth, 1),
        0, 0, height, width, *(counts[k] for k in COUNTS), counts["comment"], unique_comments,
        comments_h, unique_fonts, fonts_h, names_h, 0, 0))
    return noErr


def _add_picture(t, survey: Survey, h: int) -> int:
    """A picture, read as macrt's DrawPicture reads it: a spooled one,
    whose handle holds only its size and frame, through the current
    port's getPicProc (Jigsaw's)."""
    p = deref(t.mem, h)
    if not p:
        return pictureDataErr
    heap, size = _picture_heap(t, h, p)
    frame = pict.picture_frame(t.mem.read(p, min(size, 10)))
    reader, buf = _picture_reader(t, _port(t), heap, p, size)
    if reader is None:
        return memFullErr
    item = Survey()
    try:
        item.add_picture(frame, reader)
    except pict.PictError as e:
        log.info("GetPictInfo: %s", e)
        return pictureDataErr
    finally:
        if buf:
            heap.dispose_ptr(buf)
    survey.merge(item)
    return noErr


def _add_pixmap(t, survey: Survey, h: int) -> int:
    """A PixMap's or BitMap's pixels: from memory, or for a port that's
    drawn elsewhere, as its pixels look."""
    mem = t.mem
    p = deref(mem, h)
    if not p:
        return noErr
    row_bytes = mem.r16(p + 4)
    item = Survey()
    surface, port = t.tb.qd.bitmap(p, _port(t))
    if port is not None and not isinstance(surface, MemorySurface):
        bounds = port.port_rect
        img = surface.image(bounds)
        item.add_image_colors(image_colors(img), bounds.width, bounds.height,
                              t.tb.qd.depth(port), bool(row_bytes & PIXMAP_FLAG))
    elif row_bytes & PIXMAP_FLAG:
        pm = PixMap(mem, p)
        bounds, depth = pm.bounds, pm.pixel_size
        table = None
        ct = deref(mem, pm.pm_table)
        if ct and depth <= 8:
            n = max(0, min(mem.r16s(ct + 6) + 1, 256))
            table = table_colors(mem.read(ct, 8 + 8 * n))
        _add_rows(t, item, pm.base_addr, pm.row_bytes & ROW_BYTES_MASK, bounds, depth, table,
                  True, pm.h_res or FIXED_72, pm.v_res or FIXED_72)
    else:
        _add_rows(t, item, mem.r32(p), row_bytes & ROW_BYTES_MASK, mem.r_rect(p + 6), 1, None,
                  False)
    survey.merge(item)
    return noErr


def _add_rows(t, survey: Survey, base: int, row_bytes: int, bounds: Rect, depth: int,
              table, pixmap: bool, h_res: int = FIXED_72, v_res: int = FIXED_72) -> None:
    base &= t.mem.system.addr_mask
    width, height = max(bounds.width, 0), max(bounds.height, 0)
    if (depth not in (1, 2, 4, 8, 16, 32) or not base or row_bytes * 8 < width * depth
            or base + row_bytes * height > t.mem.size):
        rows = []
        height = 0
    else:
        data = t.mem.read(base, row_bytes * height)
        rows = [data[i:i + row_bytes] for i in range(0, len(data), row_bytes)]
    survey.add_pixmap(rows, width, height, depth, table, pixmap, h_res, v_res)


# =========================================================================
# One picture, pixel map or bitmap
# =========================================================================
@trap
def GetPictInfo(t, h, info, verb, colors, method, version):
    err = _check(t, verb, colors, method, version)
    if err:
        return err
    survey = Survey()
    err = _add_picture(t, survey, h)
    return err or _write(t, info, survey, verb, colors, method)


@trap
def GetPixMapInfo(t, h, info, verb, colors, method, version):
    err = _check(t, verb, colors, method, version)
    if err:
        return err
    survey = Survey()
    err = _add_pixmap(t, survey, h)
    return err or _write(t, info, survey, verb, colors, method, pixels_only=True)


# =========================================================================
# Surveys
# =========================================================================
@trap
def NewPictInfo(t, id_p, verb, colors, method, version):
    err = _check(t, verb, colors, method, version)
    if err:
        return err
    surveys = t.process.pict_infos
    pict_id = max(surveys, default=0) + 1
    surveys[pict_id] = Surveyed(verb, colors, method)
    t.mem.w32(id_p, pict_id)
    return noErr


def _surveyed(t, pict_id: int) -> Surveyed | None:
    return t.process.pict_infos.get(pict_id)


@trap
def RecordPictInfo(t, pict_id, h):
    s = _surveyed(t, pict_id)
    return _add_picture(t, s.survey, h) if s is not None else pictInfoIDErr


@trap
def RecordPixMapInfo(t, pict_id, h):
    s = _surveyed(t, pict_id)
    return _add_pixmap(t, s.survey, h) if s is not None else pictInfoIDErr


@trap
def RetrievePictInfo(t, pict_id, info, colors):
    s = _surveyed(t, pict_id)
    if s is None:
        return pictInfoIDErr
    if s.verb & (returnColorTable | returnPalette) and not 1 <= colors <= s.colors:
        return colorsRequestedErr
    return _write(t, info, s.survey, s.verb, colors, s.method)


@trap
def DisposePictInfo(t, pict_id):
    return noErr if t.process.pict_infos.pop(pict_id, None) is not None else pictInfoIDErr
