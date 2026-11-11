"""
Color QuickDraw's cGrafPorts, pixel maps and pixel patterns, and the
Window Manager's colour windows (Inside Macintosh V-49 to V-79, V-197 to
V-212).

A cGrafPort is a GrafPort whose portBits is a PixMap handle and whose
patterns are PixPat handles (quickdraw has the record).  OpenCPort gives
one a PixMap that's a copy of the current device's, with the device's
colour table, and old-style patterns of its own; PenPat and BackPat set
those, PenPixPat and BackPixPat put an application's pixel pattern in
their place.  A pixel pattern has six handles: the PixPat, its PixMap,
the PixMap's colour table, its pixels, and the expanded pixels and map,
which Color QuickDraw fills in when it draws and macrt leaves empty.

A colour window's port is a cGrafPort.  Its colours are in its
auxiliary record, one of a list from AuxWinHead that ends with the
default record, whose 'wctb' every window without a record of its own
uses.  A window's frame is drawn in its table's colours on a colour Mac
(macrt.ui.chrome says how it reads the parts); a colour window's content
is erased in its content colour because NewCWindow, GetNewCWindow and
SetWinColor make that its background colour.
Controls have auxiliary records the same way, from AuxCtlHead, with
'cctb's, for an application's own CDEF to draw with.
"""

from __future__ import annotations

import struct

from ..memory import BusError
from ..toolbox import cursors, lowmem
from ..toolbox.color import color_table, luminance
from ..toolbox.gdevice import (PIXMAP_SIZE, PixMap, PixPat, RGBPat, deref, dispose_pix_pat,
                               dispose_pixmap, new_pix_pat, new_pixmap, read_table)
from ..toolbox.qdraw import read_poly, read_rgn
from ..toolbox.quickdraw import (BLACK_PAT, DK_GRAY_PAT, GRAY_PAT, LT_GRAY_PAT, WHITE_PAT,
                                 GrafPort, PixPatRef)
from ..toolbox.types import Point, Rect
from ..toolbox.windows import (AUX_CTL_SIZE, AUX_WIN_SIZE, aux_wins, find_aux_win,
                               table_colors, unlink_aux_win)
from . import glue_palette
from .dispatch import trap
from .glue_color import _argb, _qd_error, _set_argb, cNoMemErr
from .glue_common import _heap, _port, _the_port_var
from .glue_quickdraw import init_port, show_cursor
from .glue_windows import _behind

# The parts of a window a 'wctb' gives the colours of (Inside Macintosh V-201).
wContentColor, wFrameColor, wTextColor, wHiliteColor, wTitleBarColor = range(5)
WHITE = (0xFFFF, 0xFFFF, 0xFFFF)
BLACK = (0, 0, 0)
# A window's standard colours, as a black-and-white Mac's are.
STANDARD_WCTB = [WHITE, BLACK, BLACK, BLACK, WHITE]
# And a control's: cFrameColor, cBodyColor, cTextColor and cThumbColor.
STANDARD_CCTB = [BLACK, WHITE, BLACK, WHITE]
# Each list of auxiliary records: its table's type, its records' size and
# the standard colours.
_AUX = {lowmem.AuxWinHead: ("wctb", AUX_WIN_SIZE, STANDARD_WCTB),
        lowmem.AuxCtlHead: ("cctb", AUX_CTL_SIZE, STANDARD_CCTB)}


def _deref(t, h: int) -> int:
    try:
        return deref(t.mem, h)
    except BusError:
        return 0


def _copy_handle(t, src: int, dst: int) -> bool:
    """Put a copy of what's in one handle in another, resizing it."""
    p = _deref(t, src)
    if not p:
        return False
    n = _heap(t, src).handle_size(src)
    heap = _heap(t, dst)
    if not heap.set_handle_size(dst, n):
        return False
    t.mem.write(heap.deref(dst), t.mem.read(_deref(t, src), n))
    return True


# =========================================================================
# cGrafPorts
# =========================================================================
@trap
def OpenCPort(t, p):
    port = GrafPort(t.mem, p)
    if not port.open_color(t.process.heap, t.system.screen_bits):
        _qd_error(t, cNoMemErr)
        return
    t.system.register(port)
    t.mem.w32(_the_port_var(t), p)


@trap
def InitCPort(t, p):
    port = t.system.port_at(p) or GrafPort(t.mem, p)
    if port.is_color:                   # a GrafPort is left as it is (Inside Macintosh V-67)
        init_port(t, port, port.init_color)


@trap
def CloseCPort(t, p):
    port = t.system.port_at(p)
    if port is not None and port.is_color:
        port.close(t.system.heap_of(port.vis_rgn) if port.vis_rgn else t.process.heap)
        t.system.unregister(port)


@trap
def SetPortPix(t, pm):
    port = _port(t)
    if port is not None and port.is_color:
        port.port_pix_map = pm


# =========================================================================
# Pixel maps
# =========================================================================
@trap
def NewPixMap(t):
    return new_pixmap(t.mem, t.process.heap)


@trap
def DisposePixMap(t, h):
    if h:
        dispose_pixmap(t.mem, _heap(t, h), h)


@trap
def CopyPixMap(t, src, dst):
    """The fields, and a copy of the colour table in the destination's
    own; the pixels stay where they are.  One that can't have a copy
    keeps its own table as it was, never the source's."""
    d = _deref(t, dst)
    if not _deref(t, src) or not d:
        return
    table = PixMap(t.mem, d).pm_table or _heap(t, dst).new_handle(0)
    s = _deref(t, src)
    t.mem.move(s, _deref(t, dst), PIXMAP_SIZE)
    if table:
        _copy_handle(t, PixMap(t.mem, s).pm_table, table)
    PixMap(t.mem, _deref(t, dst)).pm_table = table


# =========================================================================
# Pixel patterns
# =========================================================================
@trap
def NewPixPat(t):
    return new_pix_pat(t.mem, t.process.heap)


@trap
def DisposePixPat(t, h):
    """And no port has it any more: one that did draws with a pattern of
    its own the next time one is set (Inside Macintosh V-74)."""
    if not h or not _deref(t, h):
        return
    with t.system.lock:
        ports = list(t.system.ports.values())
    for port in ports:
        if port.is_color:
            for field in ("bk_pix_pat", "pn_pix_pat", "fill_pix_pat"):
                if (getattr(port, field) ^ h) & t.mem.system.addr_mask == 0:
                    setattr(port, field, 0)
    dispose_pix_pat(t.mem, _heap(t, h), h)


@trap
def CopyPixPat(t, src, dst):
    s, d = _deref(t, src), _deref(t, dst)
    if not s or not d:
        return
    a, b = PixPat(t.mem, s), PixPat(t.mem, d)
    b.pat_type, b.pat1_data, b.pat_x_valid = a.pat_type, a.pat1_data, -1
    _copy_handle(t, a.pat_data, b.pat_data)
    CopyPixMap(t, a.pat_map, b.pat_map)


@trap
def GetPixPat(t, pat_id):
    """A new PixPat from a 'ppat': the PixPat and its PixMap, then the
    pixels and the colour table, all at offsets in the resource (Inside
    Macintosh V-78); 0 if there's none or it doesn't make sense."""
    data = _resource(t, "ppat", pat_id)
    parts = _pix_parts(data)
    pat1 = data[0x14:0x1C]
    if parts is None or len(pat1) < 8:
        return 0
    pixmap, pixel_data, table = parts
    h = new_pix_pat(t.mem, t.process.heap)
    if not h:
        return 0
    heap, pp = t.process.heap, PixPat(t.mem, t.process.heap.deref(h))
    if not _fill_pixmap(t, heap, pp.pat_map, pp.pat_data, parts):
        dispose_pix_pat(t.mem, heap, h)
        return 0
    pp = PixPat(t.mem, heap.deref(h))
    pp.pat_type, pp.pat1_data = struct.unpack_from(">H", data)[0], pat1
    return h


def _resource(t, rtype: str, rid: int) -> bytes:
    res = t.process.resources.GetResource(rtype, rid)
    if not res or not _deref(t, res):
        return b""
    return t.tb.contents(res)


def _pix_parts(data: bytes) -> tuple[bytes, bytes, bytes] | None:
    """A 'ppat' or 'crsr's PixMap, pixels and colour table, at the offsets
    its record's second and third fields give (Inside Macintosh V-78);
    None if they don't make sense."""
    try:
        map_at, data_at = struct.unpack_from(">LL", data, 2)
        pixmap = data[map_at:map_at + PIXMAP_SIZE]
        table_at = struct.unpack_from(">L", pixmap, 0x2A)[0]
        size = struct.unpack_from(">h", data, table_at + 6)[0]
    except struct.error:
        return None
    pixel_data = data[data_at:table_at]
    table = data[table_at:table_at + 8 + 8 * (size + 1)]
    if len(pixmap) < PIXMAP_SIZE or not pixel_data or size < -1:
        return None
    return pixmap, pixel_data, table


def _fill_pixmap(t, heap, pm_h: int, pixels_h: int, parts: tuple[bytes, bytes, bytes]) -> bool:
    """Put _pix_parts' PixMap in pm_h, its colour table in the PixMap's own
    handle and its pixels in pixels_h; False if there isn't the room."""
    pixmap, pixel_data, table = parts
    ct = PixMap(t.mem, heap.deref(pm_h)).pm_table
    if not heap.set_handle_size(ct, len(table)) or not heap.set_handle_size(pixels_h,
                                                                            len(pixel_data)):
        return False
    t.mem.write(heap.deref(pm_h), pixmap)
    pm = PixMap(t.mem, heap.deref(pm_h))
    pm.base_addr, pm.pm_table = 0, ct
    t.mem.write(heap.deref(ct), table)
    t.mem.write(heap.deref(pixels_h), pixel_data)
    return True


# A 1-bit pattern near a colour's lightness, for drawing an RGB pattern
# in black and white, darkest first.
GRAYS = (BLACK_PAT, DK_GRAY_PAT, GRAY_PAT, LT_GRAY_PAT, WHITE_PAT)


@trap
def MakeRGBPat(t, h, p):
    """An RGB pattern: an 8-by-8 PixMap of 2 bits a pixel whose colour
    table has the colour in its last entry (Inside Macintosh V-73); macrt
    draws it as the nearest colour the screen has, not dithered."""
    pp_p = _deref(t, h)
    if not pp_p:
        return
    pp = PixPat(t.mem, pp_p)
    pm_p = _deref(t, pp.pat_map)
    if not pm_p:
        return
    pm = PixMap(t.mem, pm_p)
    ct = pm.pm_table or _heap(t, pp.pat_map).new_handle(0)
    rgb = tuple(t.mem.r16(p + 2 * i) for i in range(3))
    data = color_table([BLACK] * 4 + [rgb], seed=0)
    heap = _heap(t, ct)
    if not ct or not heap.set_handle_size(ct, len(data)):
        return
    t.mem.write(heap.deref(ct), data)
    pm = PixMap(t.mem, _deref(t, pp.pat_map))
    pm.pm_table, pm.row_bytes, pm.pixel_size, pm.cmp_size = ct, 0x8002, 2, 2
    pm.bounds = Rect(0, 0, 8, 8)
    pp = PixPat(t.mem, _deref(t, h))
    pp.pat_type, pp.pat_x_valid = RGBPat, -1
    pp.pat1_data = GRAYS[min(4, luminance(*rgb) * 5 // 0x10000)]


def _pix_pat(t, port: GrafPort | None, h: int) -> bytes:
    """The pattern a PixPat is to draw with in a port: in a cGrafPort, all
    of it; in a GrafPort, its 1-bit pattern (Inside Macintosh V-73)."""
    p = _deref(t, h)
    pat = t.mem.read(p + 0x14, 8) if p else BLACK_PAT
    return PixPatRef(pat, h) if p and port is not None and port.is_color else pat


@trap
def PenPixPat(t, h):
    port = _port(t)
    if port is not None:
        port.pn_pat = _pix_pat(t, port, h)


@trap
def BackPixPat(t, h):
    port = _port(t)
    if port is not None:
        port.bk_pat = _pix_pat(t, port, h)


# =========================================================================
# Colour cursors (Inside Macintosh V-62, V-75)
# =========================================================================
CCRSR_SIZE = 96
oldCrsr, newCrsr = 0x8000, 0x8001


class CCrsr:
    """A colour cursor's record: crsrType, crsrMap, crsrData, crsrXData,
    crsrXValid, crsrXHandle, then a Cursor's data, mask and hot spot."""

    def __init__(self, mem, addr: int):
        self.mem, self.addr = mem, addr

    def handles(self) -> tuple[int, int, int]:
        """crsrMap, crsrData and crsrXData."""
        return struct.unpack(">LLL", self.mem.read(self.addr + 2, 12))

    def cursor(self) -> bytes:
        """crsr1Data, crsrMask and crsrHotSpot: an old Cursor's record."""
        return self.mem.read(self.addr + 0x14, 68)


@trap
def GetCCursor(t, crsr_id):
    """A new CCrsr from a 'crsr', laid out as a 'ppat' is; 0 if there's
    none or it doesn't make sense."""
    data = _resource(t, "crsr", crsr_id)
    parts = _pix_parts(data)
    if parts is None or len(data) < CCRSR_SIZE:
        return 0
    heap = t.process.heap
    made = [heap.new_handle(CCRSR_SIZE), new_pixmap(t.mem, heap), heap.new_handle(0),
            heap.new_handle(0)]
    h, pm_h, pixels_h, x_h = made
    if not all(made) or not _fill_pixmap(t, heap, pm_h, pixels_h, parts):
        if pm_h:
            dispose_pixmap(t.mem, heap, pm_h)
        for x in (h, pixels_h, x_h):
            if x:
                heap.dispose_handle(x)
        return 0
    p = heap.deref(h)
    t.mem.write(p, data[:CCRSR_SIZE])
    t.mem.write(p + 2, struct.pack(">LLLhL", pm_h, pixels_h, x_h, 0, 0))
    return h


@trap
def SetCCursor(t, h):
    """Its picture, on a screen of more than 2 bits a pixel; else, or for
    an old cursor's record, its 1-bit data."""
    p = _deref(t, h)
    if not p:
        return
    rec = CCrsr(t.mem, p)
    t.process.cursor = cursors.mono(rec.cursor())
    pm_h, pixels_h, _ = rec.handles()
    pm_p, pixels_p = _deref(t, pm_h), _deref(t, pixels_h)
    if t.mem.r16(p) == newCrsr and t.system.depth > 2 and pm_p and pixels_p:
        pm = PixMap(t.mem, pm_p)
        size = pm.pixel_size
        if size in (1, 2, 4, 8):
            pixels = t.mem.read(pixels_p, t.system.heap_of(pixels_h).handle_size(pixels_h))
            table = tuple(read_table(t.mem, pm.pm_table))
            mask, hot = rec.cursor()[32:64], t.mem.r_point(p + 0x54)
            t.process.cursor = cursors.color(pixels, pm.row_bytes & 0x3FFF, size, table,
                                             mask, hot)
    show_cursor(t)


@trap
def DisposeCCursor(t, h):
    if not h or not _deref(t, h):
        return
    heap = _heap(t, h)
    pm_h, pixels_h, x_h = CCrsr(t.mem, _deref(t, h)).handles()
    if pm_h and _deref(t, pm_h):
        dispose_pixmap(t.mem, heap, pm_h)
    for x in (pixels_h, x_h):
        if x and _deref(t, x):
            heap.dispose_handle(x)
    heap.dispose_handle(h)


@trap
def AllocCursor(t):
    """Nothing to do: the host's pointer takes any depth."""


# =========================================================================
# Drawing in colour
# =========================================================================
def _fill_c(kind: str, args: int):
    def fill(t, r, *rest):
        t.tb.shape("Fill", kind, t.rect(r), rest[:args], _pix_pat(t, _port(t), rest[args]))
    fill.__name__ = "FillC" + kind
    return fill


for _kind, _args in (("Rect", 0), ("Oval", 0), ("RoundRect", 2), ("Arc", 2)):
    trap(_fill_c(_kind, _args))


@trap
def FillCRgn(t, rgn, h):
    port = _port(t)
    t.tb.qd.region(port, "Fill", read_rgn(t.mem, rgn), _pix_pat(t, port, h))


@trap
def FillCPoly(t, poly, h):
    port = _port(t)
    t.tb.qd.poly(port, "Fill", read_poly(t.mem, poly), _pix_pat(t, port, h))


@trap
def GetCPixel(t, h, v, p):
    _set_argb(t, p, t.tb.qd.get_rgb(_port(t), Point(v, h)))


@trap
def SetCPixel(t, h, v, p):
    t.tb.qd.set_rgb(_port(t), Point(v, h), _argb(t, p))


# =========================================================================
# Colour windows
# =========================================================================
@trap
def NewCWindow(t, storage, bounds, title, visible, proc_id, behind, go_away, ref_con):
    w = t.tb.NewWindow(t.rect(bounds), t.str255(title), bool(visible), proc_id,
                       _behind(t, behind), bool(go_away), ref_con, storage, color=True)
    _default_background(t, w)
    glue_palette.new_window(t, w)
    return w


@trap
def GetNewCWindow(t, window_id, storage, behind):
    """And the 'wctb' of the same ID, if there is one, its colours, and the
    'pltt' its palette (Inside Macintosh V-161)."""
    def colored(w):
        ct = t.process.resources.GetResource("wctb", window_id)
        if ct and _deref(t, ct):
            _set_win_color(t, w.addr, ct)
        else:
            _default_background(t, w)
    w = t.tb.GetNewWindow(window_id, storage, _behind(t, behind), color=True, colored=colored)
    glue_palette.new_window(t, w, window_id)
    return w


def _default_aux(t, head: int = lowmem.AuxWinHead) -> int:
    """The default auxiliary window (or control) record, made at the end
    of the list the first time it's wanted, with the 'wctb' (or 'cctb') 0
    the application or System has, or the standard colours."""
    h = find_aux_win(t.mem, 0, head)
    if h:
        return h
    kind, size, standard = _AUX[head]
    heap = t.process.heap
    h = heap.new_handle(size, clear=True)
    if not h:
        return 0
    ct = t.process.resources.GetResource(kind, 0)
    if not ct or not _deref(t, ct):
        data = color_table(standard)
        ct = heap.new_handle_with(data)
        if not ct:
            heap.dispose_handle(h)
            return 0
    t.mem.w32(heap.deref(h) + 8, ct)
    last = 0
    for last in aux_wins(t.mem, head):
        pass
    t.mem.w32(_deref(t, last) if last else head, h)
    return h


def default_aux_records(t) -> None:
    """InitWindows with Color QuickDraw: the default auxiliary window and
    control records (Inside Macintosh V, the Window and Control Managers)."""
    rm = t.process.resources
    error = rm.error                    # whether there's a 'wctb' or 'cctb' 0 isn't the
    for head in _AUX:                   # application's to hear of
        _default_aux(t, head)
    rm.error = error


def _table_colors(t, ct: int) -> dict[int, tuple[int, int, int]]:
    try:
        return table_colors(t.mem, ct)
    except BusError:
        return {}


def _set_aux_color(t, owner: int, ct: int, head: int = lowmem.AuxWinHead) -> None:
    """Give a window (or control) the colour table ct: with owner 0, the
    default record.  One with the default's colours has no record."""
    default = _default_aux(t, head)
    if not owner:
        if default:
            t.mem.w32(_deref(t, default) + 8, ct)
        return
    aux = find_aux_win(t.mem, owner, head)
    default_ct = t.mem.r32(_deref(t, default) + 8) if default else 0
    if default_ct and _table_colors(t, ct) == _table_colors(t, default_ct):
        if aux:
            unlink_aux_win(t.mem, aux, head)
            _heap(t, aux).dispose_handle(aux)
    elif aux:
        t.mem.w32(_deref(t, aux) + 8, ct)
    else:
        aux = t.process.heap.new_handle(_AUX[head][1], clear=True)
        if not aux:
            return
        p = t.process.heap.deref(aux)
        t.mem.w32(p, t.mem.r32(head))
        t.mem.w32(p + 4, owner)
        t.mem.w32(p + 8, ct)
        t.mem.w32(head, aux)


def _set_win_color(t, window: int, ct: int) -> None:
    """SetWinColor's work: the window's frame is drawn again in its new
    colours (every window's, with the default's), and a colour window's
    background is its content colour."""
    _set_aux_color(t, window, ct)
    w = t.window(window) if window else None
    if w is not None:
        _set_background(t, w, ct)
    if w is not None or not window:
        t.tb.redraw_frames(w)


def _set_background(t, w, ct: int) -> None:
    """A colour window's background colour is the content colour of its
    table, ct (Inside Macintosh V, the Window Manager)."""
    content = _content_color(t, ct)
    if not w.is_color or content is None:
        return
    qd = t.tb.qd
    qd.set_rgb_color(w, False, content)
    if w.backing is not None:
        w.backing.paper = qd.colors(w, qd.depth(w), False)[1]


def _default_background(t, w) -> None:
    """A new colour window without a table of its own has the default
    record's content colour as its background (a new port's is white)."""
    default = find_aux_win(t.mem, 0)
    p = _deref(t, default) if default else 0
    ct = t.mem.r32(p + 8) if p else 0
    if w is not None and _content_color(t, ct) not in (None, WHITE):
        _set_background(t, w, ct)


def _content_color(t, ct: int) -> tuple[int, int, int] | None:
    """A table's content colour: its first, if it has none."""
    colors = _table_colors(t, ct)
    return colors.get(wContentColor, next(iter(colors.values()))) if colors else None


def _get_aux(t, owner: int, pp: int, head: int = lowmem.AuxWinHead) -> bool:
    """The record and True; the default and False for one without a
    record of its own, the default and True for none."""
    default = _default_aux(t, head)
    aux = find_aux_win(t.mem, owner, head) if owner & t.mem.system.addr_mask else 0
    t.mem.w32(pp, aux or default)
    return bool(aux) or not owner & t.mem.system.addr_mask


@trap
def SetWinColor(t, window, ct):
    _set_win_color(t, window & t.mem.system.addr_mask, ct)


@trap
def GetAuxWin(t, window, pp):
    return _get_aux(t, window, pp)


@trap
def SetControlColor(t, control, ct):
    """SetCtlColor.  macrt draws the standard controls in black and white
    whatever their table says, but an application's own CDEF can use it."""
    _set_aux_color(t, control & t.mem.system.addr_mask, ct, lowmem.AuxCtlHead)


@trap
def GetAuxiliaryControlRecord(t, control, pp):
    """GetAuxCtl, as GetAuxWin."""
    return _get_aux(t, control, pp, lowmem.AuxCtlHead)


@trap
def GetCWMgrPort(t, pp):
    t.mem.w32(pp, t.mem.r32(lowmem.WMgrCPort) or t.mem.r32(lowmem.WMgrPort))
