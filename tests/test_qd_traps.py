"""QuickDraw's traps as a program calls them, with pointers and handles:
the port and the pen, regions, polygons, mapping one rectangle onto
another, and rectangle and point arithmetic (Inside Macintosh I,
QuickDraw)."""

import struct

import pytest
from guest import Guest

from macrt.m68k import glue_quickdraw as qd
from macrt.toolbox import lowmem, qdraw
from macrt.toolbox.qdraw import Region, read_poly, read_rgn
from macrt.toolbox.quickdraw import QD_RAND_SEED
from macrt.toolbox.types import Point, Rect

EMPTY = Rect(0, 0, 0, 0)


@pytest.fixture
def g(qapp):
    return Guest()


def rect(g, r):
    return g.rect_ptr(r)


def rgn(g, *rects):
    """A region handle holding the union of rects."""
    h = g("NewRgn")
    for r in rects:
        part = g("NewRgn")
        g("RectRgn", part, rect(g, r))
        g("UnionRgn", h, part, h)
        g("DisposeRgn", part)
    return h


def test_rectangle_arithmetic(g):
    r = g.ptr(bytes(8))
    g("SetRect", r, 1, 2, 30, 40)                   # left, top, right, bottom
    assert g.mem.r_rect(r) == Rect(2, 1, 40, 30)
    g("OffsetRect", r, 5, -2)
    assert g.mem.r_rect(r) == Rect(0, 6, 38, 35)

    dst = g.ptr(bytes(8))
    assert g("SectRect", rect(g, (0, 0, 10, 10)), rect(g, (5, 5, 20, 20)), dst)
    assert g.mem.r_rect(dst) == Rect(5, 5, 10, 10)
    # Rectangles that don't meet: FALSE, and the empty rectangle.
    assert not g("SectRect", rect(g, (0, 0, 10, 10)), rect(g, (10, 0, 20, 10)), dst)
    assert g.mem.r_rect(dst) == EMPTY
    g("UnionRect", rect(g, (0, 0, 10, 10)), rect(g, (5, 20, 8, 30)), dst)
    assert g.mem.r_rect(dst) == Rect(0, 0, 10, 30)
    g("Pt2Rect", Point(10, 20), Point(5, 30), dst)
    assert g.mem.r_rect(dst) == Rect(5, 20, 10, 30)

    box = rect(g, (0, 0, 10, 10))
    assert g("PtInRect", Point(9, 9), box)
    assert not g("PtInRect", Point(10, 5), box)     # the bottom edge isn't in it
    assert g("EqualRect", box, rect(g, (0, 0, 10, 10)))
    assert not g("EqualRect", box, rect(g, (0, 0, 10, 11)))
    assert g("EmptyRect", rect(g, (5, 5, 5, 10))) and not g("EmptyRect", box)


def test_point_arithmetic(g):
    p = g.ptr(bytes(4))
    g("SetPt", p, 3, 4)                             # h, v
    assert g.mem.r_point(p) == Point(4, 3)
    g("AddPt", Point(1, 2), p)
    assert g.mem.r_point(p) == Point(5, 5)
    g("SubPt", Point(1, 3), p)
    assert g.mem.r_point(p) == Point(4, 2)
    assert g("EqualPt", Point(4, 2), Point(4, 2)) and not g("EqualPt", Point(4, 2), Point(2, 4))
    assert g("DeltaPoint", Point(1, 1), Point(3, 4)) == (-2 & 0xFFFF) << 16 | -3 & 0xFFFF

    box = rect(g, (10, 20, 30, 40))
    assert g("PinRect", box, Point(15, 25)) == 15 << 16 | 25
    assert g("PinRect", box, Point(0, 0)) == 10 << 16 | 20
    assert g("PinRect", box, Point(99, 99)) == 29 << 16 | 39


def test_random_and_stuff_hex(g):
    seed = (qd._the_port_var(g) + QD_RAND_SEED) & g.mem.system.addr_mask
    g.mem.w32(seed, 1)
    assert g("Random") == 16807
    assert g.mem.r32(seed) == 16807
    assert g("Random") == 16807 * 16807 % 0x7FFFFFFF & 0xFFFF

    p = g.ptr(bytes(4))
    g("StuffHex", p, g.ptr(b"\x08DEADBEEF"))
    assert g.mem.read(p, 4) == b"\xDE\xAD\xBE\xEF"
    g("StuffHex", p, g.ptr(b"\x02zz"))              # not hex: left as it was
    assert g.mem.read(p, 4) == b"\xDE\xAD\xBE\xEF"


def test_mapping_one_rectangle_onto_another(g):
    src, dst = rect(g, (0, 0, 10, 10)), rect(g, (10, 20, 30, 60))
    p = g.ptr(bytes(4))
    g.mem.w_point(p, Point(5, 5))
    g("MapPt", p, src, dst)
    assert g.mem.r_point(p) == Point(20, 40)
    r = rect(g, (1, 2, 3, 4))
    g("MapRect", r, src, dst)
    assert g.mem.r_rect(r) == Rect(12, 28, 16, 36)

    # ScalePt scales a distance, and never to less than 1.
    g.mem.w_point(p, Point(3, 2))
    g("ScalePt", p, src, dst)
    assert g.mem.r_point(p) == Point(6, 8)
    g.mem.w_point(p, Point(0, 0))
    g("ScalePt", p, src, dst)
    assert g.mem.r_point(p) == Point(1, 1)

    h = rgn(g, (0, 0, 5, 5))
    g("MapRgn", h, src, dst)
    assert read_rgn(g.mem, h) == Region.rect(Rect(10, 20, 20, 40))

    g.port()
    poly = g("OpenPoly")
    g("MoveTo", 0, 0)
    g("LineTo", 5, 0)
    g("LineTo", 5, 5)
    g("ClosePoly")
    g("MapPoly", poly, src, dst)
    assert read_poly(g.mem, poly) == [Point(10, 20), Point(10, 40), Point(20, 40)]
    g("OffsetPoly", poly, 1, 2)
    assert read_poly(g.mem, poly) == [Point(12, 21), Point(12, 41), Point(22, 41)]
    g("KillPoly", poly)
    assert not g.heap.is_handle(poly)
    g("KillPoly", 0)
    g("OffsetPoly", 0, 1, 1)
    g("MapPoly", 0, src, dst)


def test_region_arithmetic(g):
    a, b = rgn(g, (0, 0, 10, 10)), rgn(g, (5, 5, 15, 15))
    dst = g("NewRgn")
    g("SectRgn", a, b, dst)
    assert read_rgn(g.mem, dst) == Region.rect(Rect(5, 5, 10, 10))
    g("UnionRgn", a, b, dst)
    assert read_rgn(g.mem, dst).bbox == Rect(0, 0, 15, 15)
    assert g("PtInRgn", Point(12, 12), dst) and not g("PtInRgn", Point(12, 2), dst)
    g("XorRgn", a, b, dst)
    assert not g("PtInRgn", Point(7, 7), dst) and g("PtInRgn", Point(2, 2), dst)
    assert g("RectInRgn", rect(g, (0, 0, 3, 3)), dst)
    assert not g("RectInRgn", rect(g, (6, 6, 9, 9)), dst)

    g("CopyRgn", a, dst)
    assert g("EqualRgn", a, dst) and not g("EqualRgn", a, b)
    g("OffsetRgn", dst, 5, 5)
    assert g("EqualRgn", dst, b)
    g("InsetRgn", dst, 2, 3)
    assert read_rgn(g.mem, dst) == Region.rect(Rect(8, 7, 12, 13))
    g("SetRectRgn", dst, 1, 2, 3, 4)                # left, top, right, bottom
    assert read_rgn(g.mem, dst) == Region.rect(Rect(2, 1, 4, 3))
    assert not g("EmptyRgn", dst)
    g("SetEmptyRgn", dst)
    assert g("EmptyRgn", dst)
    for h in (a, b, dst):
        g("DisposeRgn", h)
    g("DisposeRgn", 0)


def test_regions_from_drawing_and_bits(g):
    port, _ = g.port(32, 16)
    g("OpenRgn")
    g("FrameRect", rect(g, (2, 3, 6, 9)))
    h = g("NewRgn")
    g("CloseRgn", h)
    assert read_rgn(g.mem, h) == Region.rect(Rect(2, 3, 6, 9))

    g("ClipRect", rect(g, (0, 0, 4, 4)))
    clip = g("NewRgn")
    g("GetClip", clip)
    assert read_rgn(g.mem, clip) == Region.rect(Rect(0, 0, 4, 4))
    g("SetClip", h)
    assert read_rgn(g.mem, port.clip_rgn) == Region.rect(Rect(2, 3, 6, 9))

    # The 1 bits of a bitmap, in its own coordinates.
    bits = g.ptr(b"\x00\x00\x3C\x00\x3C\x00\x00\x00")       # rowBytes 2, 4 rows
    bm = g.ptr(struct.pack(">IH4h", bits, 2, 10, 20, 14, 36))
    assert g("BitMapToRegion", h, bm) == 0
    assert read_rgn(g.mem, h) == Region.rect(Rect(11, 22, 13, 26))
    deep = g.ptr(struct.pack(">IH4h", bits, 0x8000 | 2, 0, 0, 4, 16))
    assert g("BitMapToRegion", h, deep) == qd.pixmapTooDeepErr


def test_the_port_its_pen_and_its_text_settings(g):
    port, _ = g.port(64, 32)
    pp = g.ptr(bytes(4))
    g("GetPort", pp)
    assert g.mem.r32(pp) == port.addr
    g("GetWMgrPort", pp)
    assert g.mem.r32(pp) == g.mem.r32(lowmem.WMgrPort)

    g("MoveTo", 3, 4)
    g("Move", 2, 1)
    p = g.ptr(bytes(4))
    g("GetPen", p)
    assert g.mem.r_point(p) == Point(5, 5)
    g("PenMode", qdraw.patXor)
    g("BackPat", g.ptr(b"\xAA\x55" * 4))
    g("TextMode", qdraw.srcBic)
    g("SpaceExtra", 0x18000)
    assert (port.pn_mode, bytes(port.bk_pat), port.tx_mode, port.sp_extra) == (
        qdraw.patXor, b"\xAA\x55" * 4, qdraw.srcBic, 0x18000)
    g("HidePen")
    g("HidePen")
    g("ShowPen")
    assert port.pn_vis == -1
    g("ShowPen")

    state = g.ptr(bytes(qd.PEN_STATE))
    g("GetPenState", state)
    g("PenNormal")
    assert port.pn_mode == qdraw.patCopy
    g("SetPenState", state)
    assert (port.pn_mode, port.pn_loc) == (qdraw.patXor, Point(5, 5))

    # SetOrigin moves the port's coordinates, its visRgn with them.
    g("SetOrigin", 10, 20)
    assert port.port_rect == Rect(20, 10, 52, 74)
    assert port.port_bits_bounds == Rect(20, 10, 52, 74)
    assert read_rgn(g.mem, port.vis_rgn) == Region.rect(Rect(20, 10, 52, 74))
    g.mem.w_point(p, Point(20, 10))
    g("LocalToGlobal", p)
    assert g.mem.r_point(p) == Point(0, 0)
    g("GlobalToLocal", p)
    assert g.mem.r_point(p) == Point(20, 10)
    g("PortSize", 30, 20)
    assert port.port_rect == Rect(20, 10, 40, 40)
    g("MovePortTo", 100, 50)
    assert port.port_bits_bounds.top_left == Point(20 - 50, 10 - 100)


def test_with_no_port_the_port_calls_do_nothing(g):
    g("SetPort", 0)
    p, h = g.ptr(bytes(20)), g("NewRgn")
    for name, args in [("SetOrigin", (1, 1)), ("PortSize", (1, 1)), ("GlobalToLocal", (p,)),
                       ("LocalToGlobal", (p,)), ("GetPen", (p,)), ("PenMode", (1,)),
                       ("PenNormal", ()), ("HidePen", ()), ("ShowPen", ()),
                       ("GetPenState", (p,)), ("SetPenState", (p,)), ("OpenRgn", ()),
                       ("ClipRect", (p,)), ("GetClip", (h,)), ("SetClip", (h,)),
                       ("ClosePoly", ()), ("SetPortBits", (p,))]:
        g(name, *args)
    g("CloseRgn", h)
    assert g("EmptyRgn", h)
    assert g("GetPixel", 0, 0) is False
    assert g.mem.read(p, 20) == bytes(20)
    # Text is measured with the standard procs.
    assert g("TextWidth", g.ptr(b"ab"), 0, 2) > 0


def test_opening_initialising_and_closing_a_port(g):
    p = g.ptr(bytes(qd.GrafPort.SIZE))
    g("OpenPort", p)
    port = g.system.port_at(p)
    assert port is not None and g.mem.r32(qd._the_port_var(g)) == p
    vis, clip = port.vis_rgn, port.clip_rgn
    g("ClipRect", rect(g, (0, 0, 5, 5)))
    g("PenMode", qdraw.patXor)
    # InitPort starts it afresh, in the regions it has.
    g("InitPort", p)
    assert (port.vis_rgn, port.clip_rgn, port.pn_mode) == (vis, clip, qdraw.patCopy)
    assert read_rgn(g.mem, clip).bbox == qd.WIDE_OPEN
    g("ClosePort", p)
    assert g.system.port_at(p) is None
    assert not g.heap.is_handle(vis) and not g.heap.is_handle(clip)


def test_scrolling_measuring_and_angles(g):
    port, _ = g.port(16, 8)
    g("PaintRect", rect(g, (2, 2, 4, 4)))
    # The bits move right by 4; what is left behind is the update region.
    upd = g("NewRgn")
    g("ScrollRect", rect(g, (0, 0, 8, 16)), 4, 0, upd)
    assert g.rows(port)[2] == "......##........"
    assert read_rgn(g.mem, upd).bbox == Rect(0, 0, 8, 4)
    # InsetRect moves only the sides.
    r = rect(g, (0, 0, 10, 20))
    g("InsetRect", r, 2, 3)
    assert g.rect(r) == Rect(3, 2, 7, 18)
    # MeasureText: count + 1 positions, 0 first; a negative count writes nothing.
    locs = g.ptr(b"\xFF" * 8)
    g("MeasureText", -1, g.ptr(b"ab"), locs)
    assert g.mem.read(locs, 8) == b"\xFF" * 8
    g("MeasureText", 0, g.ptr(b"ab"), locs)
    assert g.mem.read(locs, 4) == b"\x00\x00\xFF\xFF"
    g("MeasureText", 2, g.ptr(b"ab"), locs)
    at = struct.unpack(">3h", g.mem.read(locs, 6))
    assert at[0] == 0 and at[2] == g("TextWidth", g.ptr(b"ab"), 0, 2) and at[1] < at[2]
    # PtToAngle goes clockwise from 12 o'clock, as the rectangle's shape has it.
    angle = g.ptr(bytes(2))
    for pt, deg in [((10, 0), 0), ((20, 0), 45), ((20, 5), 90), ((10, 10), 180),
                    ((0, 5), 270)]:
        g("PtToAngle", rect(g, (0, 0, 10, 20)), Point(pt[1], pt[0]), angle)
        assert g.mem.r16(angle) == deg
    g("ShieldCursor", rect(g, (0, 0, 1, 1)), Point(0, 0))


def test_initport_on_a_record_that_was_never_opened(g):
    p = g.ptr(b"\x55" * qd.GrafPort.SIZE)
    g("InitPort", p)
    port = g.system.port_at(p)
    assert g.mem.r32(qd._the_port_var(g)) == p
    assert g.heap.is_handle(port.vis_rgn) and g.heap.is_handle(port.clip_rgn)
    assert read_rgn(g.mem, port.clip_rgn).bbox == qd.WIDE_OPEN
