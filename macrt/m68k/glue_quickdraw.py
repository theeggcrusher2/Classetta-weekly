"""
QuickDraw traps: setting up ports, the pen and text, shapes, regions,
polygons, bits, mapping, rectangle and point arithmetic, and the cursor.
Each turns the guest's pointers and handles into the Python values the
Toolbox (macrt.toolbox.api) takes, and its results back.  Names and
arguments are as in Inside Macintosh; see dispatch for how they're called.
"""

from __future__ import annotations

import math
import struct
from contextlib import contextmanager

from PyQt6.QtGui import QImage

from ..memory import from_mac
from ..toolbox import constants as k
from ..toolbox import cursors, lowmem
from ..toolbox.gdevice import deref, oldPat
from ..toolbox.memmgr import memFullErr, noErr
from ..toolbox.pict import VERBS
from ..toolbox.qdraw import MemorySurface, Region, read_poly, read_rgn, write_poly, write_rgn
from ..toolbox.quickdraw import (QD_RAND_SEED, WIDE_OPEN, GrafPort, PixPatRef, init_qd_globals,
                                 new_rgn, set_rect_rgn)
from ..toolbox.types import Point, Rect
from ..toolbox.windows import WindowRecord
from .dispatch import trap
from .glue_common import _port, _the_port_var
from .glue_controls import _s16
from .glue_fonts import _swap_port_font


EMPTY = Rect(0, 0, 0, 0)
paramErr = -50
pixmapTooDeepErr = -148


def _custom_proc(t, port: GrafPort, offset: int, key: int) -> int:
    """The port's own procedure at offset in its QDProcs, or 0 if it has
    none there or it's the standard one."""
    procs = port.graf_procs & t.mem.system.addr_mask
    if not procs:
        return 0
    proc = t.mem.r32(procs + offset) & t.mem.system.addr_mask
    return 0 if proc in (0, t.machine.stub_for(key)) else proc


# =========================================================================
# QuickDraw: setting up
# =========================================================================
@trap
def InitGraf(t, globals_ptr):
    init_qd_globals(t.mem, globals_ptr, t.system.screen_of(t.process))
    # at 0(A5), the A5 the program runs with: Fokker Triplane moves its A5
    # world below $70000 before it calls this, and a desk accessory finds
    # the globals there; the rest of QuickDraw looks for them there now too,
    # until a trap comes with A5 back where it was
    a5 = t.cpu.a(5) & t.mem.system.addr_mask
    part = t.process.partition
    if a5 != part.a5 and not part.left_a5:
        part.left_a5 = part.a5
    part.a5 = a5
    t.mem.w32(a5, globals_ptr)
    t.mem.w8(lowmem.QDExist, 0)


@trap
def OpenPort(t, p):
    # on a window's own port, as Moriarty's Revenge calls it after
    # NewWindow, it starts the port afresh and leaves it a window
    port = t.system.port_at(p)
    if not isinstance(port, WindowRecord):
        port = GrafPort(t.mem, p)
    port.open(t.process.heap, t.system.screen_of(t.process))
    t.system.register(port)
    t.mem.w32(_the_port_var(t), p)


@trap
def InitPort(t, p):
    port = t.system.port_at(p) or GrafPort(t.mem, p)
    init_port(t, port, port.open)


def init_port(t, port: GrafPort, init) -> None:
    """InitPort's or InitCPort's work: init, OpenPort's or OpenCPort's,
    but with the regions the port has, if it has them, set as OpenPort
    sets them, and the port the current one."""
    vis, clip = port.vis_rgn, port.clip_rgn
    screen = t.system.screen_of(t.process)
    init(t.process.heap, screen)
    try:
        heaps = (t.system.heap_of(vis), t.system.heap_of(clip)) if vis and clip else None
    except ValueError:          # not handles: a record that was never opened
        heaps = None
    if heaps:                   # keep its regions, set to the screen and wide open (IM I-163)
        t.process.heap.dispose_handle(port.vis_rgn)
        t.process.heap.dispose_handle(port.clip_rgn)
        port.vis_rgn, port.clip_rgn = vis, clip
        set_rect_rgn(heaps[0], vis, screen.bounds)
        set_rect_rgn(heaps[1], clip, WIDE_OPEN)
    t.system.register(port)
    t.mem.w32(_the_port_var(t), port.addr)


@trap
def ClosePort(t, p):
    port = t.system.port_at(p)
    if port is not None and not isinstance(port, WindowRecord):
        port.close(t.system.heap_of(port.vis_rgn) if port.vis_rgn else t.process.heap)
        t.system.unregister(port)


@trap
def SetPort(t, p):
    t.mem.w32(_the_port_var(t), p)


@trap
def GetPort(t, pp):
    t.w32(pp, t.mem.r32(_the_port_var(t)))


@trap
def GetWMgrPort(t, pp):
    t.w32(pp, t.mem.r32(lowmem.WMgrPort))


@trap
def SetOrigin(t, h, v):
    port = _port(t)
    if port is None:
        return
    r = port.port_rect
    dh, dv = h - r.left, v - r.top
    port.port_rect = r.offset(dh, dv)
    port.port_bits_bounds = port.port_bits_bounds.offset(dh, dv)
    if port.vis_rgn:
        _set_rgn(t, port.vis_rgn, _rgn(t, port.vis_rgn).offset(dh, dv))


@trap
def PortSize(t, width, height):
    port = _port(t)
    if port is not None:
        r = port.port_rect
        port.port_rect = Rect(r.top, r.left, r.top + height, r.left + width)


@trap
def MovePortTo(t, left, top):
    t.tb.MovePortTo(left, top)


@trap
def GlobalToLocal(t, p):
    port = _port(t)
    if port is not None:
        t.mem.w_point(p, port.global_to_local(t.mem.r_point(p)))


@trap
def LocalToGlobal(t, p):
    port = _port(t)
    if port is not None:
        t.mem.w_point(p, port.local_to_global(t.mem.r_point(p)))


# =========================================================================
# QuickDraw: the pen and text settings, which live in the port
# =========================================================================
def _set(t, field: str, value) -> None:
    port = _port(t)
    if port is not None:
        setattr(port, field, value)


@trap
def MoveTo(t, h, v):
    t.tb.MoveTo(h, v)


@trap
def Move(t, dh, dv):
    t.tb.Move(dh, dv)


@trap
def LineTo(t, h, v):
    t.tb.LineTo(h, v)


@trap
def Line(t, dh, dv):
    t.tb.Line(dh, dv)


@trap
def GetPen(t, p):
    port = _port(t)
    if port is not None:
        t.mem.w_point(p, port.pn_loc)


@trap
def PenSize(t, width, height):
    _set(t, "pn_size", Point(height, width))


@trap
def PenMode(t, mode):
    _set(t, "pn_mode", mode)


@trap
def PenPat(t, p):
    _set(t, "pn_pat", t.mem.read(p, 8))


@trap
def BackPat(t, p):
    _set(t, "bk_pat", t.mem.read(p, 8))


@trap
def PenNormal(t):
    port = _port(t)
    if port is not None:
        port.pn_size = Point(1, 1)
        port.pn_mode = k.patCopy
        port.pn_pat = b"\xFF" * 8


@trap
def HidePen(t):
    port = _port(t)
    if port is not None:
        port.pn_vis = port.pn_vis - 1


@trap
def ShowPen(t):
    port = _port(t)
    if port is not None:
        port.pn_vis = port.pn_vis + 1


PEN_STATE = 18      # pnLoc, pnSize, pnMode, pnPat


# In a cGrafPort the pen's pattern is a PixPat handle.  GetPenState saves
# an old-style one's pattern, and a pixel pattern's handle with this
# after it, macrt's own mark, so SetPenState can put either back.
PEN_PIX_PAT = b"ppat"


@trap
def GetPenState(t, p):
    port = _port(t)
    if port is None:
        return
    head = t.mem.read(port.addr + 48, 10)           # pnLoc, pnSize, pnMode
    pat = port.pn_pat
    h = getattr(pat, "handle", 0)
    pp = deref(t.mem, h) if h else 0
    if pp and t.mem.r16(pp) != oldPat:
        t.mem.write(p, head + struct.pack(">L", h) + PEN_PIX_PAT)
    else:
        t.mem.write(p, head + bytes(pat))


@trap
def SetPenState(t, p):
    port = _port(t)
    if port is None:
        return
    if not port.is_color:
        t.mem.write(port.addr + 48, t.mem.read(p, PEN_STATE))
        return
    t.mem.write(port.addr + 48, t.mem.read(p, 10))
    pat = t.mem.read(p + 10, 8)
    h = struct.unpack(">L", pat[:4])[0]
    pp = deref(t.mem, h) if pat[4:] == PEN_PIX_PAT else 0
    port.pn_pat = PixPatRef(t.mem.read(pp + 0x14, 8), h) if pp else pat


@trap
def TextFont(t, font):
    _set(t, "tx_font", font)


@trap
def TextFace(t, face):
    _set(t, "tx_face", face)


@trap
def TextMode(t, mode):
    _set(t, "tx_mode", mode)


@trap
def TextSize(t, size):
    _set(t, "tx_size", size)


@trap
def SpaceExtra(t, extra):
    _set(t, "sp_extra", extra)


# =========================================================================
# QuickDraw: text
# =========================================================================
# Each leaves the Font Manager's output record in low memory, as the
# FMSwapFont QuickDraw calls first does.  A port with its own textProc or
# txMeasProc has its text drawn or measured by it, as QuickDraw calls
# them in place of StdText and StdTxMeas: Macintosh Pascal's draw its
# program's formatting bytes as bold keywords and indents.
TEXT_PROC, TX_MEAS_PROC = 0, 40         # in QDProcs
StdText, StdTxMeas = 0xA882, 0xA8ED
UNITY = 0x00010001                      # the Point (1, 1): numer and denom, not scaled


def text_procs(t, port: GrafPort | None) -> tuple[int, int]:
    """The port's own textProc and txMeasProc, 0 for the standard ones."""
    if port is None:
        return 0, 0
    return _custom_proc(t, port, TEXT_PROC, StdText), _custom_proc(t, port, TX_MEAS_PROC, StdTxMeas)


def draw_with(t, proc: int, p: int, count: int) -> None:
    """A textProc draws count characters at p at the pen."""
    t.call(proc, (("h", count), ("p", p), ("l", UNITY), ("l", UNITY)))


def measure_with(t, proc: int, p: int, count: int) -> int:
    """What a txMeasProc measures count characters at p as: the width it
    returns, scaled by the numer and denom it leaves.  They and the
    FontInfo it fills in are on the stack."""
    cpu = t.cpu
    ctx = cpu.context()
    cpu.sp = var = cpu.sp - 16
    t.mem.write(var, struct.pack(">LL8x", UNITY, UNITY))
    width = _s16(t.call(proc, (("h", count), ("p", p), ("p", var), ("p", var + 4),
                               ("p", var + 8)), "h"))
    numer, denom = t.mem.r16(var + 2), t.mem.r16(var + 6)
    cpu.restore(ctx)
    return width * numer // denom if numer and denom and numer != denom else width


def _draw(t, text: bytes, at: int | None = None) -> None:
    """text drawn, by the port's textProc if it has one; at is where it is
    in memory, None for DrawChar's character, which goes on the stack."""
    _swap_port_font(t)
    proc = text_procs(t, _port(t))[0]
    if proc:
        with _in_memory(t, text, at) as p:
            draw_with(t, proc, p, len(text))
    elif text:
        t.tb.DrawString(from_mac(text))


def _width(t, text: bytes, at: int | None = None) -> int:
    _swap_port_font(t)
    proc = text_procs(t, _port(t))[1]
    if proc:
        with _in_memory(t, text, at) as p:
            return measure_with(t, proc, p, len(text))
    return t.tb.StringWidth(from_mac(text)) if text else 0


@contextmanager
def _in_memory(t, text: bytes, at: int | None):
    """Where text is for a port's own procs: at, or on the stack."""
    if at is not None:
        yield at
        return
    cpu = t.cpu
    ctx = cpu.context()
    cpu.sp = cpu.sp - 2
    t.mem.write(cpu.sp, text)
    try:
        yield cpu.sp
    finally:
        cpu.restore(ctx)


def _text(t, p: int, count: int) -> bytes:
    return t.mem.read(p, count) if p and count > 0 else b""


@trap
def DrawString(t, p):
    """A NIL string is the one at address 0, where NumToString puts the
    number it's given NIL for (see NumToString)."""
    _draw(t, _text(t, p + 1, t.mem.r8(p)), p + 1)


@trap
def DrawChar(t, ch):
    _draw(t, bytes([ch & 0xFF]))


@trap
def DrawText(t, p, first, count):
    _draw(t, _text(t, p + first, count), p + first)


@trap
def StringWidth(t, p):
    return _width(t, _text(t, p + 1, t.mem.r8(p)), p + 1)


@trap
def CharWidth(t, ch):
    return _width(t, bytes([ch & 0xFF]))


@trap
def TextWidth(t, p, first, count):
    return _width(t, _text(t, p + first, count), p + first)


@trap
def MeasureText(t, count, p, locs):
    """Where the pen is after each of count characters at p, as TextWidth
    measures them: count + 1 integers at locs, 0 first.  The Oregon Trail
    breaks its messages into lines with it."""
    if count < 0:
        return
    _swap_port_font(t)
    at = t.tb.MeasureText(from_mac(t.mem.read(p, count))) if count else [0]
    t.mem.write(locs, struct.pack(f">{len(at)}H", *(x & 0xFFFF for x in at)))


@trap
def GetFontInfo(t, p):
    _swap_port_font(t)
    t.mem.write(p, struct.pack(">hhhh", *t.tb.GetFontInfo()))


# =========================================================================
# QuickDraw: shapes
# =========================================================================
# A port with its own rectProc, rRectProc, ovalProc, arcProc, polyProc or
# rgnProc draws that kind of shape: QuickDraw calls it with the verb in
# place of StdRect and the rest, a Fill's pattern put in the port's
# fillPat first.  Treasure MathStorm!'s rectProc draws nothing, so that
# TextEdit's erasing leaves its panels' colour behind its text.
SHAPE_PROCS = {"Rect": (8, 0xA8A0), "RoundRect": (12, 0xA8AF), "Oval": (16, 0xA8B6),
               "Arc": (20, 0xA8BD), "Poly": (24, 0xA8C5), "Rgn": (28, 0xA8D1)}


def shape_proc(t, port: GrafPort | None, kind: str) -> int:
    """The port's own procedure for the kind of shape, 0 for QuickDraw's."""
    return _custom_proc(t, port, *SHAPE_PROCS[kind]) if port is not None else 0


def _by_shape_proc(t, verb: str, kind: str, args: tuple, pat: bytes | None) -> bool:
    """Whether thePort's own procedure drew the shape, called with the verb
    and args."""
    port = _port(t)
    proc = shape_proc(t, port, kind)
    if not proc:
        return False
    if pat is not None:
        port.fill_pat = pat
    t.call(proc, (("b", VERBS.index(verb)),) + args)
    return True


def _shape(verb: str, kind: str):
    sizes = ("h", "h") if kind in ("RoundRect", "Arc") else ()   # the ovals or angles

    def draw(t, r, *args):
        pat = t.mem.read(args[-1], 8) if verb == "Fill" else None
        if not _by_shape_proc(t, verb, kind, (("p", r),) + tuple(zip(sizes, args)), pat):
            t.tb.shape(verb, kind, t.rect(r), args[:2], pat)
    draw.__name__ = verb + kind
    return draw


for _verb in ("Frame", "Paint", "Erase", "Invert", "Fill"):
    for _kind in ("Rect", "Oval", "RoundRect", "Arc"):
        trap(_shape(_verb, _kind))


# =========================================================================
# QuickDraw: regions
# =========================================================================
def _rgn(t, h: int) -> Region:
    return read_rgn(t.mem, h)


def _set_rgn(t, h: int, rgn: Region | Rect) -> None:
    """The region h becomes rgn; one whose handle isn't in a heap is left
    as it is: MacTriv passes RectRgn the address of its rectangle for the
    region, and the region for the rectangle."""
    heap = t.system.find_heap(h) if h else None
    if heap is not None:
        write_rgn(heap, h, rgn if isinstance(rgn, Region) else Region.rect(rgn))


@trap
def NewRgn(t):
    """A region handle from the Memory Manager, with MemErr set by the
    allocation: The Prince tests MemErr after NewRgn, following a PurgeMem
    that left it at memFullErr."""
    h = new_rgn(t.process.heap)
    t.set_mem_err(noErr if h else memFullErr)
    return h


@trap
def DisposeRgn(t, h):
    if h:
        t.system.heap_of(h).dispose_handle(h)


@trap
def OpenRgn(t):
    port = _port(t)
    if port is not None:
        t.tb.qd.open_rgn(port)


@trap
def CloseRgn(t, h):
    port = _port(t)
    _set_rgn(t, h, t.tb.qd.close_rgn(port) if port is not None else Region())


@trap
def SetEmptyRgn(t, h):
    _set_rgn(t, h, Region())


@trap
def RectRgn(t, h, r):
    _set_rgn(t, h, t.rect(r))


@trap
def SetRectRgn(t, h, left, top, right, bottom):
    _set_rgn(t, h, Rect(top, left, bottom, right))


@trap
def CopyRgn(t, src, dst):
    _set_rgn(t, dst, _rgn(t, src))


@trap
def OffsetRgn(t, h, dh, dv):
    _set_rgn(t, h, _rgn(t, h).offset(dh, dv))


@trap
def InsetRgn(t, h, dh, dv):
    _set_rgn(t, h, _rgn(t, h).inset(dh, dv))


@trap
def SectRgn(t, a, b, dst):
    _set_rgn(t, dst, _rgn(t, a).sect(_rgn(t, b)))


@trap
def UnionRgn(t, a, b, dst):
    _set_rgn(t, dst, _rgn(t, a).union(_rgn(t, b)))


@trap
def DiffRgn(t, a, b, dst):
    _set_rgn(t, dst, _rgn(t, a).diff(_rgn(t, b)))


@trap
def XorRgn(t, a, b, dst):
    _set_rgn(t, dst, _rgn(t, a).xor(_rgn(t, b)))


@trap
def PtInRgn(t, pt, h):
    return _rgn(t, h).contains(pt)


@trap
def RectInRgn(t, r, h):
    return not _rgn(t, h).sect(Region.rect(t.rect(r))).empty


@trap
def EqualRgn(t, a, b):
    return _rgn(t, a) == _rgn(t, b)


@trap
def EmptyRgn(t, h):
    return _rgn(t, h).empty


def _draw_rgn(verb: str):
    def draw(t, h, *pat):
        pat = t.mem.read(pat[0], 8) if pat else None
        if not _by_shape_proc(t, verb, "Rgn", (("p", h),), pat):
            t.tb.qd.region(_port(t), verb, _rgn(t, h), pat)
    draw.__name__ = verb + "Rgn"
    return draw


for _verb in ("Frame", "Paint", "Erase", "Invert", "Fill"):
    trap(_draw_rgn(_verb))


@trap
def ClipRect(t, r):
    port = _port(t)
    if port is not None:
        _set_rgn(t, port.clip_rgn, t.rect(r))


@trap
def GetClip(t, h):
    port = _port(t)
    if port is not None:
        _set_rgn(t, h, _rgn(t, port.clip_rgn))


@trap
def SetClip(t, h):
    port = _port(t)
    if port is not None:
        _set_rgn(t, port.clip_rgn, _rgn(t, h))


@trap
def BitMapToRegion(t, h, bm):
    """The 1 bits of a BitMap, as a region in its coordinates."""
    base, row_bytes, bounds = t.mem.r32(bm), t.mem.r16(bm + 4), t.mem.r_rect(bm + 6)
    surface = MemorySurface.make(t.mem, base, row_bytes, bounds)
    if surface is None:
        return pixmapTooDeepErr if row_bytes & 0x8000 else paramErr
    img = surface.image(bounds).convertToFormat(QImage.Format.Format_Grayscale8)
    img.invertPixels()                                  # black, the 1 bits, non-zero
    _set_rgn(t, h, Region.from_mask(img, bounds.left, bounds.top))
    return noErr


# =========================================================================
# QuickDraw: polygons
# =========================================================================
@trap
def OpenPoly(t):
    port = _port(t)
    h = t.process.heap.new_handle(10, clear=True)
    if h and port is not None:
        t.mem.w16(t.process.heap.deref(h), 10)
        t.tb.qd.open_poly(port, h)
    return h


@trap
def ClosePoly(t):
    port = _port(t)
    if port is not None:
        t.tb.qd.close_poly(port)


@trap
def KillPoly(t, h):
    if h:
        t.system.heap_of(h).dispose_handle(h)


@trap
def OffsetPoly(t, h, dh, dv):
    if h:
        write_poly(t.system.heap_of(h), h, [pt.offset(dh, dv) for pt in read_poly(t.mem, h)])


@trap
def FramePoly(t, h):
    if not _by_shape_proc(t, "Frame", "Poly", (("p", h),), None):
        t.tb.qd.frame_poly(_port(t), read_poly(t.mem, h))


def _fill_poly(verb: str):
    def fill(t, h, *pat):
        pat = t.mem.read(pat[0], 8) if pat else None
        if not _by_shape_proc(t, verb, "Poly", (("p", h),), pat):
            t.tb.qd.poly(_port(t), verb, read_poly(t.mem, h), pat)
    fill.__name__ = verb + "Poly"
    return fill


for _verb in ("Paint", "Erase", "Invert", "Fill"):
    trap(_fill_poly(_verb))


# =========================================================================
# QuickDraw: bits
# =========================================================================
@trap
def SetPortBits(t, bm):
    port = _port(t)
    if port is not None and not port.is_color:  # a cGrafPort's are SetPortPix's (IM V-75)
        t.mem.write(port.addr + 2, t.mem.read(bm, 14))


BITS_PROC, StdBits = 32, 0xA8EB         # in QDProcs


@trap
def CopyBits(t, src, dst, src_r, dst_r, mode, mask_rgn):
    port = _port(t)
    # into thePort's own portBits it's the port's bitsProc that copies, as
    # HyperCard's AddColor has it put its colour pictures over the card's
    proc = (_custom_proc(t, port, BITS_PROC, StdBits)
            if port is not None and dst & t.mem.system.addr_mask == port.addr + 2 else 0)
    if proc:
        t.call(proc, (("p", src), ("p", src_r), ("p", dst_r), ("h", mode), ("p", mask_rgn)))
        return
    mask = _rgn(t, mask_rgn) if mask_rgn else None
    t.tb.qd.copy_bits(src, dst, t.rect(src_r), t.rect(dst_r), mode, mask, port)


@trap
def ScrollRect(t, r, dh, dv, update_rgn):
    _set_rgn(t, update_rgn, t.tb.ScrollRect(t.rect(r), dh, dv))


@trap
def GetPixel(t, h, v):
    port = _port(t)
    return t.tb.qd.get_pixel(port, Point(v, h)) if port is not None else False


# =========================================================================
# QuickDraw: mapping from one rectangle to another
# =========================================================================
def _map(n: int, a0: int, a1: int, b0: int, b1: int) -> int:
    return b0 + (n - a0) * (b1 - b0) // (a1 - a0) if a1 != a0 else b0


def _map_pt(pt: Point, a: Rect, b: Rect) -> Point:
    return Point(_map(pt.v, a.top, a.bottom, b.top, b.bottom),
                 _map(pt.h, a.left, a.right, b.left, b.right))


@trap
def MapPt(t, p, src, dst):
    t.set_point(p, _map_pt(t.point(p), t.rect(src), t.rect(dst)))


@trap
def MapRect(t, r, src, dst):
    a, b = t.rect(src), t.rect(dst)
    x = t.rect(r)
    tl = _map_pt(Point(x.top, x.left), a, b)
    br = _map_pt(Point(x.bottom, x.right), a, b)
    t.set_rect(r, Rect(tl.v, tl.h, br.v, br.h))


@trap
def ScalePt(t, p, src, dst):
    a, b = t.rect(src), t.rect(dst)
    pt = t.point(p)
    h = pt.h * b.width // a.width if a.width else pt.h
    v = pt.v * b.height // a.height if a.height else pt.v
    t.set_point(p, Point(max(v, 1), max(h, 1)))


@trap
def MapRgn(t, h, src, dst):
    a, b = t.rect(src), t.rect(dst)
    bands = []
    for top, bottom, xs in _rgn(t, h).bands:
        top, bottom = (_map(y, a.top, a.bottom, b.top, b.bottom) for y in (top, bottom))
        bands.append((top, bottom, tuple(_map(x, a.left, a.right, b.left, b.right) for x in xs)))
    _set_rgn(t, h, Region([band for band in bands if band[1] > band[0]]))


@trap
def MapPoly(t, h, src, dst):
    if h:
        a, b = t.rect(src), t.rect(dst)
        write_poly(t.system.heap_of(h), h, [_map_pt(pt, a, b) for pt in read_poly(t.mem, h)])


# =========================================================================
# QuickDraw: rectangle and point arithmetic
# =========================================================================
def _sect(a: Rect, b: Rect) -> Rect | None:
    r = Rect(max(a.top, b.top), max(a.left, b.left), min(a.bottom, b.bottom),
             min(a.right, b.right))
    return None if r.empty else r


def _union(a: Rect, b: Rect) -> Rect:
    if a.empty:
        return b
    if b.empty:
        return a
    return Rect(min(a.top, b.top), min(a.left, b.left), max(a.bottom, b.bottom),
                max(a.right, b.right))


@trap
def SetRect(t, r, left, top, right, bottom):
    t.set_rect(r, Rect(top, left, bottom, right))


@trap
def OffsetRect(t, r, dh, dv):
    t.set_rect(r, t.rect(r).offset(dh, dv))


@trap
def InsetRect(t, r, dh, dv):
    # Only the sides move: the ROM leaves a rectangle inset to nothing as
    # it is, not (0,0,0,0) as Inside Macintosh says, and PhrazeCraze
    # insets one to nothing and out again to turn over a letter.
    t.set_rect(r, t.rect(r).inset(dh, dv))


@trap
def SectRect(t, a, b, dst):
    r = _sect(t.rect(a), t.rect(b))
    t.set_rect(dst, r or EMPTY)
    return r is not None


@trap
def UnionRect(t, a, b, dst):
    t.set_rect(dst, _union(t.rect(a), t.rect(b)))


@trap
def Pt2Rect(t, a, b, dst):
    t.set_rect(dst, Rect(min(a.v, b.v), min(a.h, b.h), max(a.v, b.v), max(a.h, b.h)))


@trap
def PtInRect(t, pt, r):
    return t.rect(r).contains(pt)


@trap
def EqualRect(t, a, b):
    return t.rect(a) == t.rect(b)


@trap
def EmptyRect(t, r):
    return t.rect(r).empty


@trap
def SetPt(t, p, h, v):
    t.set_point(p, Point(v, h))


@trap
def AddPt(t, src, dst):
    d = t.point(dst)
    t.set_point(dst, Point(d.v + src.v, d.h + src.h))


@trap
def SubPt(t, src, dst):
    d = t.point(dst)
    t.set_point(dst, Point(d.v - src.v, d.h - src.h))


@trap
def DeltaPoint(t, a, b):
    """a - b, as a Point in a long: vertical in the high word."""
    return (((a.v - b.v) & 0xFFFF) << 16) | ((a.h - b.h) & 0xFFFF)


@trap
def EqualPt(t, a, b):
    return a == b


@trap
def PtToAngle(t, r, pt, angle_p):
    """Degrees clockwise from 12 o'clock to the line from r's centre to
    pt, as r's shape has them: the line through its top right corner is
    at 45 whatever its shape."""
    b = t.rect(r)
    dh = (2 * pt.h - b.left - b.right) / max(b.width, 1)
    dv = (2 * pt.v - b.top - b.bottom) / max(b.height, 1)
    t.w16(angle_p, round(math.degrees(math.atan2(dh, 0.0 - dv))) % 360)


@trap
def PinRect(t, r, pt):
    b = t.rect(r)
    v = min(max(pt.v, b.top), b.bottom - 1)
    h = min(max(pt.h, b.left), b.right - 1)
    return ((v & 0xFFFF) << 16) | (h & 0xFFFF)


@trap
def Random(t):
    """QuickDraw's: randSeed times 7^5 mod 2^31-1, low word, never -32768."""
    p = (_the_port_var(t) + QD_RAND_SEED) & t.mem.system.addr_mask
    seed = (t.mem.r32(p) * 16807) % 0x7FFFFFFF
    t.mem.w32(p, seed)
    r = seed & 0xFFFF
    return 0 if r == 0x8000 else r


@trap
def StuffHex(t, p, s):
    try:
        t.mem.write(p, bytes.fromhex(t.str255(s)))
    except ValueError:
        pass


# =========================================================================
# Cursors: the host's pointer, in the program's shape (toolbox.cursors)
# =========================================================================
def show_cursor(t) -> None:
    """Have the host's pointer show the process's cursor, or none while
    it's hidden; the display is told only of a change."""
    proc = t.process
    shape = cursors.HIDDEN if t.system.input.hidden else proc.cursor
    if shape != proc.cursor_shown:
        proc.cursor_shown = shape
        display = getattr(t.tb, "_d", None)
        if display is not None:
            display.bridge.post(display.set_cursor, proc, shape)


@trap
def InitCursor(t):
    """The arrow, and shown."""
    t.system.input.cursor(None)
    t.process.cursor = None
    show_cursor(t)


@trap
def HideCursor(t):
    t.system.input.cursor(-1)
    show_cursor(t)


@trap
def ShowCursor(t):
    t.system.input.cursor(1)
    show_cursor(t)


@trap
def ObscureCursor(t):
    """Hidden until the mouse next moves."""
    proc = t.process
    display = getattr(t.tb, "_d", None)
    if display is not None and not proc.cursor_obscured:
        proc.cursor_obscured = True
        display.bridge.post(display.obscure_cursor, proc)


@trap
def SetCursor(t, p):
    if p:
        t.process.cursor = cursors.mono(t.mem.read(p, cursors.CURSOR_SIZE))
        show_cursor(t)


@trap
def ShieldCursor(t, r, offset):
    pass
