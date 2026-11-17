"""
Color QuickDraw's graphics devices, pixel maps and pixel patterns in
guest memory, and the colour tables they draw with (Inside Macintosh
V, Color QuickDraw).

    PixMap      baseAddr, rowBytes (the top bit set), bounds, pmVersion,
                packType, packSize, hRes, vRes, pixelType, pixelSize,
                cmpCount, cmpSize, planeBytes, pmTable, pmReserved
    GDevice     gdRefNum, gdID, gdType, gdITable, gdResPref,
                gdSearchProc, gdCompProc, gdFlags, gdPMap, gdRefCon,
                gdNextGD, gdRect, gdMode, then the cursor's expanded data
    ColorTable  ctSeed (long), ctFlags, ctSize (entries - 1), then each
                entry's value and red, green, blue
    PixPat      patType, patMap, patData, patXData, patXValid, patXMap,
                pat1Data: a PixMap and its pixels, or an old pattern
    ITab        iTabSeed (long), iTabRes, then for each colour, by the top
                iTabRes bits of its red, green and blue, the nearest
                entry's index

A Macintosh II with Color QuickDraw has one device here, the main
screen, whose pixel map is the screen buffer at ScrnBase, and whose
colour table is the standard one for its depth.

    'scrn'      ScrnCount, then for each screen spDrvrHw, slot,
                dCtlDevBase (long), mode, flagMask, flags, colorTable,
                gammaTable, its global Rect, ctlCount and its control calls
"""

from __future__ import annotations

import struct
from array import array
from collections import Counter
from functools import lru_cache
from typing import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage

from ..memory import INTEGER, LONG, LONGINT, PATTERN, RAM, RECT, Field, Record
from . import lowmem
from .color import CT_DEVICE, CT_PALETTE, color_table, standard_table
from .memmgr import Heap
from .pixels import MONO_TABLE, raw, rgb_value, value_rgb
from .types import Rect

PIXMAP_SIZE = 0x32
GDEVICE_SIZE = 0x3E
PIX_PAT_SIZE = 0x1C
PIXMAP_FLAG = 0x8000            # rowBytes: a PixMap, not a BitMap
ROW_BYTES_MASK = 0x3FFF
FIXED_72 = 72 << 16             # 72 dpi, as a Fixed

# gdType
clutType, fixedType, directType = 0, 1, 2
# gdFlags' bits
gdDevType, ramInit, mainScreen, allInit, screenDevice, noDriver, screenActive = (
    0, 10, 11, 12, 13, 14, 15)
DEFAULT_MODE = 0x80             # a video card's first mode: its 1-bit one
VIDEO_DRIVER = -49              # the main screen's video driver (glue_video), its gdRefNum
VIDEO_SLOT = 9                  # and its card's slot, the Mac II's first
VIDEO_DR_HW = 0                 # and the card's DrHW, a number of macrt's own (glue_slots)
ITAB_RES = 4                    # the inverse table's resolution, gdResPref
MIN_SEED = 1024                 # colour tables' seeds start over the 'clut' IDs
RGB_DIRECT = 16                 # pixelType: a pixel is its colour
# The 'scrn' resource's flagMask, as Inside Macintosh V has it (Graphics
# Device Resources), and the gdFlags bits its flags say: whether the screen
# is colour, the main screen and active.
SCRN_FLAG_MASK = 0x77FE
SCRN_FLAGS = 1 << gdDevType | 1 << mainScreen | 1 << screenActive
# patType
oldPat, pixPat, RGBPat = 0, 1, 2
GRAY_PAT = b"\xAA\x55" * 4


class PixMap(Record):
    SIZE = PIXMAP_SIZE

    base_addr = Field(0x00, LONG)
    row_bytes = Field(0x04, INTEGER)        # with the flags in its top bits
    bounds = Field(0x06, RECT)
    pm_version = Field(0x0E, INTEGER)
    pack_type = Field(0x10, INTEGER)
    pack_size = Field(0x12, LONGINT)
    h_res = Field(0x16, LONGINT)
    v_res = Field(0x1A, LONGINT)
    pixel_type = Field(0x1E, INTEGER)
    pixel_size = Field(0x20, INTEGER)
    cmp_count = Field(0x22, INTEGER)
    cmp_size = Field(0x24, INTEGER)
    plane_bytes = Field(0x26, LONGINT)
    pm_table = Field(0x2A, LONG)
    pm_reserved = Field(0x2E, LONG)


class GDevice(Record):
    SIZE = GDEVICE_SIZE

    ref_num = Field(0x00, INTEGER)
    id = Field(0x02, INTEGER)
    type = Field(0x04, INTEGER)
    i_table = Field(0x06, LONG)
    res_pref = Field(0x0A, INTEGER)
    search_proc = Field(0x0C, LONG)
    comp_proc = Field(0x10, LONG)
    flags = Field(0x14, INTEGER)
    pmap = Field(0x16, LONG)
    ref_con = Field(0x1A, LONGINT)
    next_gd = Field(0x1E, LONG)
    rect = Field(0x22, RECT)
    mode = Field(0x2A, LONGINT)


class PixPat(Record):
    SIZE = PIX_PAT_SIZE

    pat_type = Field(0x00, INTEGER)
    pat_map = Field(0x02, LONG)             # a PixMap handle
    pat_data = Field(0x06, LONG)            # its pixels' handle
    pat_x_data = Field(0x0A, LONG)
    pat_x_valid = Field(0x0E, INTEGER)
    pat_x_map = Field(0x10, LONG)
    pat1_data = Field(0x14, PATTERN)        # drawn where there's one bit a pixel


_PMAP, _PM_TABLE = GDevice.pmap.offset, PixMap.pm_table.offset


def deref(mem: RAM, h: int) -> int:
    """Where a handle's block is (0 for no handle, or one purged)."""
    h &= mem.system.addr_mask
    return mem.r32(h) & mem.system.addr_mask if h else 0


def read_table(mem: RAM, h: int,
               palette: Callable[[], list[int] | None] | None = None) -> list[int]:
    """A ColorTable handle's colours, 0xAARRGGBB by pixel value: in order
    for a device's table, else by each entry's value.  A table whose
    values are a palette's entries (CT_PALETTE, as Palette Manager
    programs give their offscreen pixel maps) is in order too, each
    colour its entry's, by palette() if given (the colours of the current
    port's palette's entries), else the table's own."""
    p = deref(mem, h)
    if not p:
        return list(MONO_TABLE)
    size = max(0, min(mem.r16s(p + 6) + 1, 256))
    data = mem.read(p + 4, 4 + 8 * size)
    colors = palette() if palette is not None and data[0] & CT_PALETTE >> 8 else None
    return list(_parse_table(data, tuple(colors) if colors else ()))


@lru_cache(maxsize=64)
def _parse_table(data: bytes, palette: tuple[int, ...] = ()) -> tuple[int, ...]:
    """A ColorTable's colours from its ctFlags on."""
    flags, size = struct.unpack_from(">Hh", data)
    size = max(0, min(size + 1, 256))
    out = [0xFF000000] * size
    for i in range(size):
        value, r, g, b = struct.unpack_from(">HHHH", data, 4 + 8 * i)
        if flags & (CT_DEVICE | CT_PALETTE):
            entry, value = value, i
            if flags & CT_PALETTE and entry < len(palette):
                out[value] = palette[entry]
                continue
        value &= 0xFF
        if value >= len(out):
            out += [0xFF000000] * (value + 1 - len(out))
        out[value] = 0xFF000000 | (r >> 8) << 16 | (g >> 8) << 8 | b >> 8
    return tuple(out)


def table_rgb(rgb: int) -> tuple[int, int, int]:
    """0xAARRGGBB as a ColorTable entry's 16-bit red, green and blue."""
    return (rgb >> 16 & 0xFF) * 0x101, (rgb >> 8 & 0xFF) * 0x101, (rgb & 0xFF) * 0x101


def make_itable(table: list[int], seed: int, res: int = ITAB_RES) -> bytes:
    """An inverse table for table: for each colour of res bits a component,
    the index of the nearest of the table's colours (as Qt finds it)."""
    return struct.pack(">LH", seed & 0xFFFFFFFF, res) + _itable_indexes(tuple(table), res)


@lru_cache(maxsize=16)
def _itable_indexes(table: tuple[int, ...], res: int) -> bytes:
    # Kept by the colours alone: a program that reserves or protects entry
    # after entry gets a new seed each time but the same colours.
    n = 1 << res
    levels = [round(i * 255 / (n - 1)) for i in range(n)]
    cube = array("I", [0xFF000000 | r << 16 | g << 8 | b
                       for r in levels for g in levels for b in levels])
    img = QImage(cube.tobytes(), n * n, n, QImage.Format.Format_RGB32)
    idx = img.convertToFormat(QImage.Format.Format_Indexed8, list(table) or MONO_TABLE,
                              Qt.ImageConversionFlag.ThresholdDither)
    return raw(idx)[:n ** 3]


DEPTH_MODES = {1: 0, 2: 1, 4: 2, 8: 3, 16: 4, 32: 5}   # gdMode, from DEFAULT_MODE


def device_pixmap(mem: RAM) -> int:
    """Where the current device's (TheGDevice's) PixMap is; 0 if there's none."""
    gd = deref(mem, mem.r32(lowmem.TheGDevice))
    return deref(mem, GDevice(mem, gd).pmap) if gd else 0


def new_pixmap(mem: RAM, heap: Heap) -> int:
    """NewPixMap: a PixMap handle, a copy of the current device's PixMap
    but for its colour table, a new handle with no colours in it yet."""
    ct = heap.new_handle(8, clear=True)
    h = heap.new_handle(PIXMAP_SIZE, clear=True)
    if not h or not ct:
        for x in (h, ct):
            if x:
                heap.dispose_handle(x)
        return 0
    mem.w16(heap.deref(ct) + 6, 0xFFFF)     # ctSize: none
    src, p = device_pixmap(mem), heap.deref(h)
    if src:
        mem.move(src, p, PIXMAP_SIZE)
    PixMap(mem, p).pm_table = ct
    return h


def dispose_pixmap(mem: RAM, heap: Heap, h: int) -> None:
    """DisposPixMap: the PixMap and its colour table."""
    p = deref(mem, h)
    if p:
        heap.dispose_handle(PixMap(mem, p).pm_table)
    heap.dispose_handle(h)


def new_pix_pat(mem: RAM, heap: Heap, pat: bytes | None = None) -> int:
    """NewPixPat: a PixPat handle, a pixel pattern with a new PixMap and
    new handles for its pixels, but none yet, and a grey pat1Data; or,
    given pat, an old pattern of it, as PenPat makes in a cGrafPort."""
    pm = new_pixmap(mem, heap)
    rest = [heap.new_handle(0) for _ in range(3)]       # patData, patXData, patXMap
    h = heap.new_handle(PIX_PAT_SIZE, clear=True)
    if not h or not pm or not all(rest):
        for x in [h] + rest:
            if x:
                heap.dispose_handle(x)
        if pm:
            dispose_pixmap(mem, heap, pm)
        return 0
    pp = PixPat(mem, heap.deref(h))
    pp.pat_type = pixPat if pat is None else oldPat
    pp.pat_map = pm
    pp.pat_data, pp.pat_x_data, pp.pat_x_map = rest
    pp.pat_x_valid = -1
    pp.pat1_data = GRAY_PAT if pat is None else pat
    return h


def set_pix_pat(mem: RAM, heap: Heap, h: int, pat_type: int, pat1: bytes,
                pixmap: bytes = b"", table: bytes = b"", data: bytes = b"") -> bool:
    """Make NewPixPat's h a pattern of pat_type: an old one of pat1, or a
    pixel or RGB one with the PixMap record pixmap (its pmTable aside),
    the colour table table and the pixels data, pat1 its 1-bit pattern;
    False, h as it was, if there isn't the room."""
    p = deref(mem, h)
    if not p:
        return False
    pp = PixPat(mem, p)
    if pat_type != oldPat:
        pm_h = pp.pat_map
        pm_p = deref(mem, pm_h)
        if not pm_p or not heap.set_handle_size(pp.pat_data, len(data)):
            return False
        ct = PixMap(mem, pm_p).pm_table
        if not ct or not heap.set_handle_size(ct, len(table)):
            return False
        mem.write(deref(mem, pm_h), pixmap[:PIXMAP_SIZE].ljust(PIXMAP_SIZE, b"\0"))
        PixMap(mem, deref(mem, pm_h)).pm_table = ct
        mem.write(deref(mem, ct), table)
        mem.write(deref(mem, pp.pat_data), data)
        pp = PixPat(mem, deref(mem, h))
    pp.pat_type, pp.pat1_data, pp.pat_x_valid = pat_type, pat1[:8], -1
    return True


def rgb_pat_map(rgb: tuple[int, int, int]) -> tuple[bytes, bytes]:
    """An RGB pattern's PixMap record and colour table: 8 by 8 pixels of 2
    bits, the colour in the table's fifth entry, as MakeRGBPat makes it
    (Inside Macintosh V-73)."""
    pixmap = struct.pack(">LH4hhhlllhhhhlll", 0, PIXMAP_FLAG | 2, 0, 0, 8, 8, 0, 0, 0,
                         FIXED_72, FIXED_72, 0, 2, 1, 2, 0, 0, 0)
    return pixmap, color_table([(0, 0, 0)] * 4 + [rgb], seed=0)


def dispose_pix_pat(mem: RAM, heap: Heap, h: int) -> None:
    """DisposPixPat: everything NewPixPat allocated."""
    p = deref(mem, h)
    if p:
        pp = PixPat(mem, p)
        dispose_pixmap(mem, heap, pp.pat_map)
        for x in (pp.pat_data, pp.pat_x_data, pp.pat_x_map):
            if x:
                heap.dispose_handle(x)
    heap.dispose_handle(h)


class Devices:
    """The System's graphics devices: the main screen's, in the System heap,
    its handle in DeviceList, MainDevice and, to start with, each
    application's TheGDevice."""

    def __init__(self, mem: RAM, heap: Heap, screen: Rect, base: int, row_bytes: int,
                 depth: int):
        self.mem = mem
        self.heap = heap
        self.depth = depth
        self._seed = MIN_SEED
        self._table: tuple[int, list[int]] = (-1, [])      # the main device's, by its seed
        self._raw: list[int] = []                           # as its colour table has them
        self._index: dict[int, int] | None = None           # colour -> index, in _table
        self._nearest: dict[int, int] = {}
        self._matching: list[int] | None = None
        # The main device's indexes the Palette Manager has reserved for
        # animation: the palette and entry each is for.
        self.reserved: dict[int, tuple[int, int]] = {}
        # And those a program has reserved with the Color Manager's
        # ReserveEntry.
        self.held: set[int] = set()
        self.main = self.new_device(screen, base, row_bytes, depth)
        gd = GDevice(mem, deref(mem, self.main))
        gd.flags = (1 << gdDevType if 1 < depth <= 8 else 0) | (
            1 << ramInit | 1 << mainScreen | 1 << allInit | 1 << screenDevice | 1 << screenActive)
        gd.mode = DEFAULT_MODE + DEPTH_MODES[depth]
        gd.ref_num = VIDEO_DRIVER
        mem.w32(lowmem.DeviceList, self.main)
        mem.w32(lowmem.MainDevice, self.main)
        mem.w32(lowmem.TheGDevice, self.main)

    def next_seed(self) -> int:
        """A seed no colour table has had: GetCTSeed's."""
        self._seed += 1
        return self._seed

    def _device_table(self, depth: int, gray: bool = False) -> bytes:
        """A device's colour table for a depth, with a new seed: the
        standard colours, or the greys (there are no standard 2-bit
        colours), or on a direct device none."""
        data = bytearray(standard_table(32 + depth if gray or depth == 2 else depth)
                         if depth <= 8 else color_table([]))
        struct.pack_into(">lH", data, 0, self.next_seed(), CT_DEVICE)
        return bytes(data)

    def new_device(self, rect: Rect, base: int, row_bytes: int, depth: int) -> int:
        """A GDevice handle for a device whose pixels are at base, with
        the standard colour table for its depth and an inverse table for
        that (NewGDevice and InitGDevice's, as a video card's driver would
        have them set it up)."""
        mem, heap = self.mem, self.heap
        ct = heap.new_handle_with(self._device_table(depth))
        pm = heap.new_handle(PIXMAP_SIZE, clear=True)
        write_pixmap(mem, heap.deref(pm), base, row_bytes, rect, depth, ct)
        gdh = heap.new_handle(GDEVICE_SIZE, clear=True)
        gd = GDevice(mem, heap.deref(gdh))
        gd.type = clutType if depth <= 8 else directType
        gd.res_pref = ITAB_RES
        gd.pmap = pm
        gd.rect = rect
        gd.mode = -1
        if depth <= 8:
            gd.i_table = heap.new_handle_with(make_itable(read_table(mem, ct),
                                                          mem.r32(heap.deref(ct))))
        return gdh

    def set_depth(self, depth: int, row_bytes: int, gray: bool = False) -> bool:
        """The main screen is depth bits a pixel now, row_bytes a row, as
        InitGDevice sets a device up for a video card's new mode: its
        PixMap, its colour table (the standard one for the depth, or with
        gray the greys), its inverse table, its mode, and whether it's a
        colour device.  What was reserved is let go.
        False if there isn't the memory for the tables."""
        mem, heap = self.mem, self.heap
        gd = GDevice(mem, deref(mem, self.main))
        ct = PixMap(mem, deref(mem, gd.pmap)).pm_table
        data = self._device_table(depth, gray)
        if heap.handle_size(ct) != len(data) and not heap.set_handle_size(ct, len(data)):
            return False
        if depth <= 8:
            itab = make_itable(list(_parse_table(data[4:])), struct.unpack_from(">l", data)[0],
                               gd.res_pref or ITAB_RES)
            ih = gd.i_table
            if not ih:
                ih = heap.new_handle(len(itab))
            elif heap.handle_size(ih) != len(itab) and not heap.set_handle_size(ih, len(itab)):
                ih = 0
            if not ih:
                return False
            mem.write(heap.deref(ih), itab)
        mem.write(heap.deref(ct), data)
        gd = GDevice(mem, deref(mem, self.main))
        pm = PixMap(mem, deref(mem, gd.pmap))
        write_pixmap(mem, pm.addr, pm.base_addr, row_bytes, pm.bounds, depth, ct)
        if depth <= 8:
            gd.i_table = ih
        gd.type = clutType if depth <= 8 else directType
        colour = 1 < depth <= 8 and not gray
        gd.flags = gd.flags & ~(1 << gdDevType) | colour << gdDevType
        gd.mode = DEFAULT_MODE + DEPTH_MODES[depth]
        self.depth = depth
        self.reserved = {}
        self.held = set()
        self.changed()
        return True

    def scrn(self) -> bytes:
        """A 'scrn' resource for the screen as it is (Inside Macintosh V,
        Graphics Device Resources), as the Control Panel writes one to the
        System file when the user sets the screen's depth, and some
        programs read to see how many colours it shows: JuliaSet 5.1
        looks for the main screen's mode in it.  The one card, in
        VIDEO_SLOT, whose memory starts with the screen buffer; its mode,
        flags and rectangle, as its GDevice has them; the standard colour
        table for the depth, the 'clut' of that ID; no gamma table, and
        no control calls.  Its spDrvrHw is the DrHW the card's declaration
        ROM gives (glue_slots)."""
        gd = GDevice(self.mem, deref(self.mem, self.main))
        r = gd.rect
        return struct.pack(">HhhLhHHhh4hH", 1, VIDEO_DR_HW, VIDEO_SLOT,
                           self.pixmap(self.main).base_addr,
                           gd.mode, SCRN_FLAG_MASK, gd.flags & SCRN_FLAGS, self.depth, -1,
                           r.top, r.left, r.bottom, r.right, 0)

    def pixmap(self, gdh: int) -> PixMap | None:
        p = deref(self.mem, GDevice(self.mem, deref(self.mem, gdh)).pmap) if gdh else 0
        return PixMap(self.mem, p) if p else None

    def table(self) -> list[int]:
        """The main screen's colours, by pixel value, as its colour table
        has them now (each unlike any other: see apart)."""
        mem = self.mem                  # read as pixmap() would, without the views
        gd = deref(mem, self.main)
        pm = deref(mem, mem.r32(gd + _PMAP)) if gd else 0
        h = mem.r32(pm + _PM_TABLE) if pm else 0
        p = deref(mem, h)
        seed = mem.r32(p) if p else 0
        if seed != self._table[0]:
            table = read_table(self.mem, h) if self.depth <= 8 else []
            self._raw = table
            self._table = (seed, apart(table, self.reserved.keys() | self.held))
            self._index = None
            self._nearest = {}
            self._matching = None
        return self._table[1]

    def alike(self, value: int, color: int) -> bool:
        """Whether the main screen's entry value has the same colour, in
        its colour table, as the entry shown in color: another of entries
        alike, each shown unlike the rest (see apart)."""
        table = self.table()
        if self._index is None:
            self._index = {c: i for i, c in enumerate(table)}
        i = self._index.get(color)
        return i is not None and 0 <= value < len(table) and self._raw[value] == self._raw[i]

    def changed(self) -> None:
        """The main screen's colours have changed, whether or not its
        table's seed has (RestoreEntries keeps it), or which of them are
        reserved has."""
        self._table = (-1, [])

    def matching(self) -> list[int]:
        """The main screen's colours as a colour to draw in is matched to
        them: an index reserved, for animation or by ReserveEntry, is no
        match for anything, as the Color Manager leaves it out of the
        inverse table.  (It stands in for index 0's colour, which is
        matched first.)"""
        table = self.table()
        if self._matching is None:
            kept = self.reserved.keys() | self.held
            self._matching = [table[0] if i in kept else c
                              for i, c in enumerate(table)] if kept else table
        return self._matching

    def nearest(self, rgb: int) -> int:
        """The colour (0xAARRGGBB) the main screen shows for rgb: the
        nearest of its colour table's, or on a direct device as many of
        its bits as a pixel keeps."""
        table = self.table()
        got = self._nearest.get(rgb)
        if got is None:
            got = self._nearest[rgb] = value_rgb(rgb_value(rgb, self.depth, self.matching()),
                                                 self.depth, table)
        return got


# How a colour is changed, least first, to be unlike the others in a table.
_NUDGES = [d for step in range(1, 256)
           for d in ((0, 0, step), (0, 0, -step), (0, step, 0), (0, -step, 0),
                     (step, 0, 0), (-step, 0, 0))]


def apart(table: list[int], reserved) -> list[int]:
    """table, with each entry made unlike any other by as little as it
    takes: one whose index is in reserved, and of others alike each but
    the first.  A window's pixels are kept as colours, and written in
    the screen buffer as the index of their colour: a pixel drawn in an
    entry's colour has to go on being written as that entry's index, so
    that it changes as the entry does.  One reserved for animation does;
    so does any, when a program sets the entries alike, draws, and sets
    them back (Zoop fades its screen to black with RestoreEntries, draws
    its next picture and fades that in).  And black text mustn't change,
    when an animated colour is black to start with.  One reserved is
    changed whether or not another is like it, so that a colour copied
    from a PixMap of its own colours, the same as it, isn't written as
    its index: Color QuickDraw matches no colour to it (pixels.encode)."""
    reserved = {i for i in reserved if 0 <= i < len(table)}
    counts = Counter(table)
    if len(counts) == len(table) and not reserved:
        return table
    out = list(table)
    kept: set[int] = set()
    tried: dict[int, int] = {}      # colour -> how many of _NUDGES its alikes have tried
    for i in sorted(reserved) + [i for i in range(len(out)) if i not in reserved]:
        c = out[i]
        if i not in reserved and (counts[c] == 1 or c not in kept):
            kept.add(c)
            continue
        r, g, b = c >> 16 & 0xFF, c >> 8 & 0xFF, c & 0xFF
        for k in range(tried.get(c, 0), len(_NUDGES)):
            dr, dg, db = _NUDGES[k]
            if 0 <= r + dr <= 255 and 0 <= g + dg <= 255 and 0 <= b + db <= 255:
                n = 0xFF000000 | (r + dr) << 16 | (g + dg) << 8 | (b + db)
                if n not in counts:
                    tried[c] = k + 1
                    counts[c] -= 1
                    counts[n] = 1
                    out[i] = n
                    break
    return out


def write_pixmap(mem: RAM, p: int, base: int, row_bytes: int, bounds: Rect, depth: int,
                 table: int) -> None:
    """Fill in the PixMap at p: depth bits a pixel, by table's colours (a
    ColorTable handle) or, 16 and 32 bits, by RGB."""
    pm = PixMap(mem, p)
    pm.base_addr = base
    pm.row_bytes = (row_bytes & ROW_BYTES_MASK) | PIXMAP_FLAG
    pm.bounds = bounds
    pm.pm_version = 0
    pm.pack_type = 0
    pm.pack_size = 0
    pm.h_res = pm.v_res = FIXED_72
    direct = depth > 8
    pm.pixel_type = RGB_DIRECT if direct else 0     # or chunky: a pixel's bits together
    pm.pixel_size = depth
    pm.cmp_count = 3 if direct else 1
    pm.cmp_size = (5 if depth == 16 else 8) if direct else depth
    pm.plane_bytes = 0
    pm.pm_table = table
    pm.pm_reserved = 0
