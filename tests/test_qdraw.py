"""QuickDraw's drawing, checked bit by bit in 1-bit bitmaps in guest
memory, and its regions, checked against QuickDraw's own region format."""

import random
from types import SimpleNamespace

import pytest
from PyQt6.QtGui import QColor, QFont, QImage

from macrt.memory import RAM
from macrt.toolbox import pixels, qdraw
from macrt.toolbox.qdraw import Region, oval_path, rect_path
from macrt.toolbox.quickdraw import BLACK_PAT, GRAY_PAT, Backing, GrafPort
from macrt.toolbox.system import System
from macrt.toolbox.types import Point, Rect

SYSTEM_24 = SimpleNamespace(mmu_32_bit=False, addr_mask=0xFFFFFF)

W, H = 64, 32                   # the offscreen bitmap
ROW_BYTES = W // 8


@pytest.fixture
def qd(qapp):
    system = System()
    return qdraw.QuickDraw(system, qdraw.Fonts(QFont()), lambda port: None)


def offscreen(qd, bounds=Rect(0, 0, H, W)):
    """A port on a fresh, white bitmap; returns it and the bitmap's address."""
    heap = qd.sys.sys_heap
    bits = heap.new_ptr(ROW_BYTES * bounds.height, clear=True)
    port = GrafPort(qd.mem, heap.new_ptr(GrafPort.SIZE))
    port.open(heap, qd.sys.screen_bits)
    port.port_bits_base = bits
    port.port_bits_row_bytes = ROW_BYTES
    port.port_bits_bounds = bounds
    port.port_rect = bounds
    qdraw.write_rgn(heap, port.vis_rgn, Region.rect(bounds))
    qd.sys.register(port)
    return port, bits


def rows(qd, bits, n=H):
    """The bitmap as strings of . and #."""
    out = []
    for y in range(n):
        row = qd.mem.read(bits + y * ROW_BYTES, ROW_BYTES)
        out.append("".join("#" if row[x >> 3] & (0x80 >> (x & 7)) else "." for x in range(W)))
    return out


def ones(qd, bits):
    return sum(r.count("#") for r in rows(qd, bits))


# -- regions -------------------------------------------------------------------
def test_region_data_is_quickdraws():
    # Two overlapping squares, as QuickDraw stores their union: per row
    # where something changes, the h coordinates where it changes, $7FFF.
    rgn = Region.rect(Rect(0, 0, 10, 10)).union(Region.rect(Rect(5, 5, 15, 15)))
    assert rgn.to_data().hex() == (
        "002c" "00000000000f000f"
        "0000" "0000000a" "7fff"
        "0005" "000a000f" "7fff"
        "000a" "00000005" "7fff"
        "000f" "0005000f" "7fff"
        "7fff")
    assert Region.from_data(rgn.to_data()) == rgn
    assert Region.rect(Rect(1, 2, 3, 4)).to_data().hex() == "000a0001000200030004"


def test_an_unpaired_inversion_point_is_left_out():
    # rows 0 to 10: 2, 5 and 9 (the 9 unpaired), then 2 and 5 again
    data = bytes.fromhex("0018" "000000000014000a" "0000" "000200050009" "7fff"
                         "000a" "00020005" "7fff" "7fff")
    rgn = Region.from_data(data)
    assert rgn == Region.rect(Rect(0, 2, 10, 5)) and list(rgn.rects()) == [Rect(0, 2, 10, 5)]


def test_region_operations():
    a, b = Region.rect(Rect(0, 0, 10, 10)), Region.rect(Rect(5, 5, 15, 15))
    assert a.sect(b) == Region.rect(Rect(5, 5, 10, 10))
    assert a.diff(b).bbox == Rect(0, 0, 10, 10)
    assert not a.diff(b).contains(Point(7, 7)) and a.diff(b).contains(Point(7, 2))
    x = a.xor(b)
    assert x.contains(Point(2, 2)) and x.contains(Point(12, 12))
    assert not x.contains(Point(7, 7))
    assert a.union(b).inset(1, 1).bbox == Rect(1, 1, 14, 14)
    assert a.inset(-2, -3).bbox == Rect(-3, -2, 13, 12)
    assert a.inset(5, 5).empty
    assert a.offset(3, 4).bbox == Rect(4, 3, 14, 13)
    assert a.diff(a).empty and Region().union(a) == a


def test_shape_regions():
    oval = Region.from_path(oval_path(Rect(0, 0, 8, 8)))
    assert oval.bbox == Rect(0, 0, 8, 8)
    assert not oval.contains(Point(0, 0)) and oval.contains(Point(4, 4))


# -- drawing in a bitmap -----------------------------------------------------
def test_paint_and_frame_rect(qd):
    port, bits = offscreen(qd)
    qd.shape(port, "Paint", rect_path, Rect(1, 2, 3, 6))
    qd.shape(port, "Frame", rect_path, Rect(5, 0, 9, 8))
    assert rows(qd, bits, 10) == [
        "." * W,
        "..####" + "." * (W - 6),
        "..####" + "." * (W - 6),
        "." * W,
        "." * W,
        "########" + "." * (W - 8),
        "#......#" + "." * (W - 8),
        "#......#" + "." * (W - 8),
        "########" + "." * (W - 8),
        "." * W,
    ]


def test_an_empty_rectangle_draws_nothing(qd):
    # MacGammon frames one whose top is below its bottom.
    port, bits = offscreen(qd)
    for verb in ("Frame", "Paint", "Fill", "Invert"):
        qd.shape(port, verb, rect_path, Rect(30, 4, 10, 40))
        qd.shape(port, verb, oval_path, Rect(2, 40, 20, 30))
    assert ones(qd, bits) == 0


def test_pen_size_and_lines(qd):
    port, bits = offscreen(qd)
    port.pn_size = Point(2, 3)                      # 3 wide, 2 high
    qd.move_to(port, 4, 1)
    qd.line_to(port, 10, 1)
    r = rows(qd, bits, 4)
    assert r[1][4:13] == "#########" and r[2][4:13] == "#########"
    assert r[1][3] == "." and r[1][13] == "." and r[3].count("#") == 0
    assert port.pn_loc == Point(1, 10)


def test_patterns_line_up_with_the_bitmap(qd):
    port, bits = offscreen(qd)
    port.pn_pat = GRAY_PAT
    qd.shape(port, "Paint", rect_path, Rect(0, 0, 2, 8))
    qd.shape(port, "Paint", rect_path, Rect(0, 9, 2, 17))  # same pattern phase, a pixel along
    r = rows(qd, bits, 2)
    assert r[0][:8] == "#.#.#.#." and r[1][:8] == ".#.#.#.#"
    assert r[0][9:17] == ".#.#.#.#"


@pytest.mark.parametrize("mode, expect", [
    (qdraw.patCopy, "####...."),
    (qdraw.patOr, "####.#.#"),
    (qdraw.patXor, ".....#.#"),
    (qdraw.patBic, ".....#.#"),
    (qdraw.notPatCopy, "....####"),
    (qdraw.notPatOr, "########"),
    (qdraw.notPatXor, "#####.#."),
    (qdraw.notPatBic, "####...."),
])
def test_pattern_transfer_modes(qd, mode, expect):
    port, bits = offscreen(qd)
    qd.mem.w8(bits, 0b11110101)                     # what's there
    port.pn_pat = bytes([0xF0] * 8)                 # what's painted
    port.pn_mode = mode
    qd.shape(port, "Paint", rect_path, Rect(0, 0, 1, 8))
    assert rows(qd, bits, 1)[0][:8] == expect


@pytest.mark.parametrize("mode, black, white", [
    (qdraw.patCopy, "########", "........"),
    (qdraw.patOr, "########", "####.#.#"),
    (qdraw.patXor, "....#.#.", "####.#.#"),
    (qdraw.patBic, "........", "####.#.#"),
    (qdraw.notPatCopy, "........", "########"),
    (qdraw.notPatOr, "####.#.#", "########"),
    (qdraw.notPatXor, "####.#.#", "....#.#."),
    (qdraw.notPatBic, "####.#.#", "........"),
])
def test_patterns_of_one_colour_in_each_mode(qd, mode, black, white):
    """Black and white patterns (painted with a plain brush, not a pattern's)."""
    for pat, expect in ((BLACK_PAT, black), (bytes(8), white)):
        port, bits = offscreen(qd)
        qd.mem.w8(bits, 0b11110101)
        port.pn_pat = pat
        port.pn_mode = mode
        qd.shape(port, "Paint", rect_path, Rect(0, 0, 1, 8))
        assert rows(qd, bits, 1)[0][:8] == expect


def test_drawing_is_clipped_to_the_clip_region(qd):
    port, bits = offscreen(qd)
    clip = Region.rect(Rect(0, 0, 4, 4)).union(Region.rect(Rect(4, 4, 8, 8)))
    qdraw.write_rgn(qd.sys.sys_heap, port.clip_rgn, clip)
    qd.shape(port, "Paint", rect_path, Rect(0, 0, H, W))
    r = rows(qd, bits, 9)
    assert r[0][:9] == "####....." and r[5][:9] == "....####."
    assert ones(qd, bits) == 32


def test_hidden_pen_draws_nothing(qd):
    """Whatever the verb, and text too, which still moves the pen:
    Evocation erases with TEInsert between HidePen and ShowPen."""
    port, bits = offscreen(qd)
    qd.shape(port, "Paint", rect_path, Rect(0, 0, 8, 8))
    port.pn_vis = -1
    qd.shape(port, "Paint", rect_path, Rect(8, 8, 16, 16))
    qd.shape(port, "Frame", rect_path, Rect(8, 8, 16, 16))
    qd.shape(port, "Erase", rect_path, Rect(0, 0, 8, 8))
    qd.shape(port, "Invert", rect_path, Rect(0, 0, 2, 2))
    qd.shape(port, "Fill", rect_path, Rect(8, 8, 16, 16), BLACK_PAT)
    qd.region(port, "Erase", Region.rect(Rect(0, 0, 4, 4)))
    qd.poly(port, "Invert", [Point(0, 0), Point(0, 8), Point(8, 8)])
    qd.move_to(port, 20, 20)
    qd.draw_text(port, "Hi")
    assert port.pn_loc == Point(20, 20 + qd.text_width(port, "Hi"))
    assert ones(qd, bits) == 64
    port.pn_vis = 0
    qd.shape(port, "Erase", rect_path, Rect(0, 0, 8, 8))
    assert ones(qd, bits) == 0


def test_local_coordinates_follow_the_origin(qd):
    port, bits = offscreen(qd, Rect(100, 200, 100 + H, 200 + W))
    qd.shape(port, "Paint", rect_path, Rect(100, 200, 101, 202))
    assert rows(qd, bits, 1)[0][:3] == "##."


def test_text_moves_the_pen(qd):
    port, bits = offscreen(qd)
    qd.move_to(port, 2, 20)
    width = qd.text_width(port, "Hi")
    qd.draw_text(port, "Hi")
    assert port.pn_loc == Point(20, 2 + width)
    assert ones(qd, bits) > 10


def test_text_a_character_at_a_time_is_the_same_text(qd):
    # As word processors measure and draw it: every character its own width.
    text = "Wavy, jolly text."
    port, whole = offscreen(qd)
    qd.move_to(port, 2, 20)
    qd.draw_text(port, text)
    assert qd.text_width(port, text) == sum(qd.text_width(port, ch) for ch in text)
    port2, pieces = offscreen(qd)
    qd.move_to(port2, 2, 20)
    for ch in text:
        qd.draw_text(port2, ch)
    assert port2.pn_loc == port.pn_loc
    assert rows(qd, pieces) == rows(qd, whole)


def test_text_in_src_copy_fills_its_own_box(qd):
    """from the pen to its width and from the font's ascent to its descent,
    and none of the character before it: Mac Pro Football draws its coin
    toss a character at a time, in srcCopy, onto black."""
    port, bits = offscreen(qd)
    qd.shape(port, "Paint", rect_path, Rect(0, 0, H, W))
    port.tx_font, port.tx_size, port.tx_mode = 3, 9, qdraw.srcCopy
    ascent, descent, _, _ = qd.font_info(port)
    qd.move_to(port, 10, 20)
    qd.draw_text(port, "N")
    n = rows(qd, bits)
    left = port.pn_loc.h
    qd.draw_text(port, " ")
    right = port.pn_loc.h
    after = rows(qd, bits)
    assert [r[:left] for r in after] == [r[:left] for r in n]
    assert all(r[9] == "#" for r in after)          # nothing left of the pen
    for y, r in enumerate(after):
        inside = 20 - ascent <= y < 20 + descent
        assert r[left:right] == ("." if inside else "#") * (right - left), y
        assert r[right] == "#"


# -- bits ---------------------------------------------------------------------
def bitmap(qd, base, bounds):
    bm = qd.sys.sys_heap.new_ptr(14)
    qd.mem.w32(bm, base)
    qd.mem.w16(bm + 4, ROW_BYTES)
    qd.mem.w_rect(bm + 6, bounds)
    return bm


@pytest.mark.parametrize("mode, expect", [
    (qdraw.srcCopy, "##..##.."),
    (qdraw.srcOr, "###.##.#"),
    (qdraw.srcXor, ".##.#..#"),
    (qdraw.srcBic, "..#....#"),
])
def test_copy_bits_modes(qd, mode, expect):
    src_port, src = offscreen(qd)
    dst_port, dst = offscreen(qd)
    qd.mem.w8(src, 0b11001100)
    qd.mem.w8(dst, 0b10100101)
    full = Rect(0, 0, H, W)
    qd.copy_bits(bitmap(qd, src, full), bitmap(qd, dst, full), Rect(0, 0, 1, 8),
                 Rect(0, 0, 1, 8), mode, None, None)
    assert rows(qd, dst, 1)[0][:8] == expect


@pytest.mark.parametrize("mode, op, expect", [
    (qdraw.AD_MIN, None, 0x640A1E),
    (qdraw.ADD_MAX, None, 0xC83264),
    (qdraw.ADD_PIN, None, 0xFF3C82),
    (qdraw.ADD_PIN, (0x8000, 0xFFFF, 0x1000), 0x803C10),
    (qdraw.SUB_PIN, None, 0x002800),
    (qdraw.SUB_PIN, (0x4000, 0x4000, 0x4000), 0x404040),
    (qdraw.ADD_OVER, None, 0x2C3C82),
    (qdraw.SUB_OVER, None, 0x9C28BA),
    (qdraw.BLEND, None, 0x961E41),
])
def test_arithmetic_modes_combine_each_component(qapp, mode, op, expect):
    """A source pixel of (200, 10, 100) on a destination one of (100, 50, 30)."""
    src, dst = (QImage(2, 1, QImage.Format.Format_RGB32) for _ in range(2))
    src.fill(QColor(200, 10, 100))
    dst.fill(QColor(100, 50, 30))
    out = qdraw.arithmetic(src, dst, mode, op)
    assert [out.pixel(x, 0) & 0xFFFFFF for x in range(2)] == [expect] * 2


def test_arithmetic_modes_revert_to_classic_ones_in_one_bit():
    for mode, classic in ((qdraw.ADD_MAX, qdraw.srcBic), (qdraw.AD_MIN, qdraw.srcOr),
                          (qdraw.BLEND, qdraw.srcCopy), (qdraw.SUB_OVER, qdraw.srcXor)):
        assert qdraw.mode_table(mode) == qdraw.mode_table(classic)


def test_transparent_leaves_out_what_is_in_the_background_colour():
    red, blue = 0xFFFF0000, 0xFF0000FF
    assert qdraw.mode_table(qdraw.TRANSPARENT, red, blue) == ([qdraw.CLEAR, red], qdraw.OVER)
    assert qdraw.mode_table(qdraw.TRANSPARENT, blue, blue) == ([qdraw.CLEAR] * 2, qdraw.OVER)


def test_arithmetic_modes_with_the_pattern_bit_draw_as_without_it():
    for mode in (qdraw.BLEND, qdraw.ADD_PIN, qdraw.TRANSPARENT, qdraw.AD_MIN):
        assert qdraw.mode_table(mode + 8) == qdraw.mode_table(mode)


def test_copy_bits_scales_and_masks(qd):
    src_port, src = offscreen(qd)
    dst_port, dst = offscreen(qd)
    qd.mem.w8(src, 0b10000000)                      # one black pixel
    qd.copy_bits(src_port.addr + 2, dst_port.addr + 2, Rect(0, 0, 1, 2), Rect(0, 0, 4, 8),
                 qdraw.srcCopy, None, dst_port)
    assert rows(qd, dst, 5)[:5] == ["####" + "." * (W - 4)] * 4 + ["." * W]
    qd.copy_bits(src_port.addr + 2, dst_port.addr + 2, Rect(0, 0, 1, 1), Rect(10, 10, 12, 12),
                 qdraw.srcCopy, Region.rect(Rect(10, 10, 11, 11)), dst_port)
    assert rows(qd, dst, 12)[10][9:13] == ".#.." and rows(qd, dst, 12)[11][9:13] == "...."


def test_copy_bits_to_a_copy_of_the_ports_bits_clips_to_the_port(qd):
    # Legends of the Lost Realm copies its title through a copy of its
    # window's portBits: drawing in those bits, it's clipped to the window.
    src_port, src = offscreen(qd)
    dst_port, dst = offscreen(qd)
    qdraw.write_rgn(qd.sys.sys_heap, dst_port.vis_rgn, Region.rect(Rect(0, 0, 4, W)))
    qd.mem.write(src, b"\xff" * ROW_BYTES * H)
    full = Rect(0, 0, H, W)
    qd.copy_bits(src_port.addr + 2, bitmap(qd, dst, full), full, full, qdraw.srcCopy, None,
                 dst_port)
    assert ones(qd, dst) == 4 * W
    other = bitmap(qd, dst, Rect(0, 0, H, W // 2))  # not the port's: not clipped to it
    qd.copy_bits(src_port.addr + 2, other, full, Rect(0, 0, 8, 8), qdraw.srcCopy, None, dst_port)
    assert rows(qd, dst, 8)[7][:9] == "########."


def test_scroll_rect(qd):
    port, bits = offscreen(qd)
    qd.shape(port, "Paint", rect_path, Rect(0, 0, 2, 2))
    vacated = qd.scroll_rect(port, Rect(0, 0, 8, 8), 3, 1)
    r = rows(qd, bits, 4)
    assert r[0][:8] == "........" and r[1][:8] == "...##..." and r[2][:8] == "...##..."
    assert vacated == Region.rect(Rect(0, 0, 8, 8)).diff(Region.rect(Rect(1, 3, 8, 8)))


def test_scroll_rect_keeps_to_the_port_rect(qd):
    """Drawing isn't kept to the portRect, but ScrollRect is: IM I has it
    move only the bits in the portRect, visRgn, clipRgn and bitmap."""
    port, bits = offscreen(qd)
    port.port_rect = Rect(0, 0, 8, 4)
    qd.shape(port, "Paint", rect_path, Rect(0, 0, 2, 8))
    vacated = qd.scroll_rect(port, Rect(0, 0, 8, 8), 0, 1)
    assert rows(qd, bits, 3) == [s + "." * (W - 8) for s in ("....####", "########", "####....")]
    assert vacated == Region.rect(Rect(0, 0, 1, 4))


def test_drawing_outside_the_port_rect_is_noted_while_its_watched(qd):
    """A survey's outside_port_rect: drawing that a clip to the portRect
    would cut, one note a port, portRect, bit map and routine."""
    port, bits = offscreen(qd)
    port.port_rect = Rect(0, 0, 8, 4)
    qd.shape(port, "Paint", rect_path, Rect(0, 0, 2, 8))
    assert qd.sys.outside_port_rect is None
    qd.sys.outside_port_rect = {}
    qd.shape(port, "Paint", rect_path, Rect(0, 0, 2, 2))
    assert qd.sys.outside_port_rect == {}
    qd.shape(port, "Paint", rect_path, Rect(0, 0, 2, 8))
    qd.shape(port, "Paint", rect_path, Rect(4, 2, 6, 10))
    assert list(qd.sys.outside_port_rect.values()) == [
        {"port": f"${port.addr:06X}", "port_rect": [0, 0, 8, 4], "bounds": [0, 0, H, W],
         "via": "fill", "box": Rect(0, 4, 6, 10), "times": 2}]


def test_get_pixel(qd):
    port, bits = offscreen(qd)
    qd.mem.w8(bits + ROW_BYTES, 0b01000000)
    assert qd.get_pixel(port, Point(1, 1)) and not qd.get_pixel(port, Point(1, 0))


def value(data, s, x, y):
    """The value of the pixel at (x, y) of s, a MemorySurface, in data, the
    memory it's in."""
    bit = (x - s.bounds.left) * s.depth
    byte = data[s.base + (y - s.bounds.top) * s.row_bytes + (bit >> 3)]
    return byte >> (8 - s.depth - (bit & 7)) & ((1 << s.depth) - 1)


def inside(r, x, y):
    return any(t <= y < b and any(xs[i] <= x < xs[i + 1] for i in range(0, len(xs), 2))
               for t, b, xs in r.bands)


@pytest.mark.parametrize("depth, bounds, rb, left, bands, width", [
    # two bands with rows between, two spans in one, ends in the middle of
    # bytes, and an image whose rows end in the middle of one
    (1, Rect(10, 16, 30, 80), 10, 24, [(12, 15, (27, 33, 40, 51)), (17, 20, (25, 60))], 37),
    (2, Rect(4, 0, 12, 30), 8, 4, [(5, 6, (5, 6)), (8, 11, (4, 13, 15, 28))], 28),
    (4, Rect(0, 3, 9, 21), 10, 5, [(2, 4, (6, 9)), (4, 7, (5, 7, 12, 19))], 16),
    (8, Rect(0, 0, 6, 12), 14, 2, [(1, 5, (2, 10))], 8),        # all of each row's bytes
    (8, Rect(0, 0, 6, 12), 14, 2, [(1, 5, (2, 10))], 20),       # an image wider than the rows
    (8, Rect(0, 0, 6, 12), 14, 2, [(1, 2, (4, 6)), (3, 5, (2, 10))], 8),   # the last band all
])
def test_put_writes_the_images_pixels_where_the_region_is(qapp, depth, bounds, rb, left,
                                                          bands, width):
    """MemorySurface.put, as the screen's mirror is written: each pixel in
    the region is the image's, and every other, in its rows, beside them in
    rowBytes, or in the rows above and below, is as it was."""
    rng = random.Random(depth * 100 + width)
    before = bytes(rng.randrange(256) for _ in range(0x200))
    mem = RAM(SYSTEM_24, 0x200)
    mem.write(0, before)
    table = qdraw.VALUE_TABLES[depth]
    s = qdraw.MemorySurface(mem, 0x40, rb, bounds, depth, table)
    r, top = Region(bands), bands[0][0]
    values = [[rng.randrange(1 << depth) for _ in range(width)]
              for _ in range(bands[-1][1] - top)]
    img = QImage(width, len(values), QImage.Format.Format_RGB32)
    for y, row in enumerate(values):
        for x, v in enumerate(row):
            img.setPixel(x, y, table[v])
    s.put(pixels.mono(img) if depth == 1 else img, left, r)
    after = mem.read(0, 0x200)
    end = s.base + rb * bounds.height
    assert after[:s.base] == before[:s.base] and after[end:] == before[end:]
    got = [[value(after, s, x, y) for x in range(bounds.left, bounds.left + rb * 8 // depth)]
           for y in range(bounds.top, bounds.bottom)]
    assert got == [[values[y - top][x - left] if inside(r, x, y) else value(before, s, x, y)
                    for x in range(bounds.left, bounds.left + rb * 8 // depth)]
                   for y in range(bounds.top, bounds.bottom)]


def test_drawing_on_rows_longer_than_their_pixels_leaves_the_rest_of_each_row(qapp):
    """32 bits a pixel on rows of 10 bytes: two pixels a row, each where
    it is in its row, and the last two bytes of each as they were."""
    mem = RAM(SYSTEM_24, 0x100)
    mem.write(0x10, bytes(range(1, 31)))
    s = qdraw.MemorySurface.make(mem, 0x10, 10, Rect(0, 0, 3, 4), 32)
    assert s.bounds == Rect(0, 0, 3, 2)
    with s.painter() as p:
        p.fillRect(0, 0, 1, 3, QColor(0x11, 0x22, 0x33))
    for y in range(3):
        row = mem.read(0x10 + y * 10, 10)
        assert row[1:4] == b"\x11\x22\x33"                         # its colour
        assert row[5:] == bytes(range(y * 10 + 6, y * 10 + 11))     # the rest as it was


# -- recording -------------------------------------------------------------------
def test_open_rgn_records_frames_and_closed_lines(qd):
    port, bits = offscreen(qd)
    qd.open_rgn(port)
    qd.shape(port, "Frame", rect_path, Rect(0, 0, 10, 10))
    qd.shape(port, "Frame", rect_path, Rect(5, 5, 15, 15))
    rgn = qd.close_rgn(port)
    assert ones(qd, bits) == 0                      # nothing drawn meanwhile
    assert rgn == Region.rect(Rect(0, 0, 10, 10)).xor(Region.rect(Rect(5, 5, 15, 15)))
    qd.open_rgn(port)
    qd.move_to(port, 0, 0)
    for h, v in ((20, 0), (20, 10), (0, 10), (0, 0)):
        qd.line_to(port, h, v)
    assert qd.close_rgn(port) == Region.rect(Rect(0, 0, 10, 20))
    assert port.pn_vis == 0


def test_polygons(qd):
    port, bits = offscreen(qd)
    heap = qd.sys.sys_heap
    h = heap.new_handle(10, clear=True)
    qd.open_poly(port, h)
    qd.move_to(port, 0, 0)
    qd.line_to(port, 8, 0)
    qd.line_to(port, 8, 4)
    qd.line_to(port, 0, 0)
    qd.close_poly(port)
    assert ones(qd, bits) == 0
    assert qdraw.read_poly(qd.mem, h) == [Point(0, 0), Point(0, 8), Point(4, 8), Point(0, 0)]
    p = heap.deref(h)
    assert qd.mem.r16(p) == 26 and qd.mem.r_rect(p + 2) == Rect(0, 0, 4, 8)
    qd.frame_poly(port, qdraw.read_poly(qd.mem, h))
    assert ones(qd, bits) > 0 and port.pn_loc == Point(0, 0)


def test_erase_uses_the_background_pattern(qd):
    port, bits = offscreen(qd)
    qd.shape(port, "Fill", rect_path, Rect(0, 0, 1, 8), BLACK_PAT)
    port.bk_pat = bytes([0x0F] * 8)
    qd.shape(port, "Erase", rect_path, Rect(0, 0, 1, 8))
    assert rows(qd, bits, 1)[0][:8] == "....####"


def test_a_backing_keeps_only_its_area(qapp):
    port = Rect(0, 0, 100, 30000)
    b = Backing(Rect(0, 0, 100, 1000), port)
    assert (b.image.width(), b.image.height()) == (1000, 100)
    with b.painter() as p:
        p.fillRect(990, 10, 5, 5, QColor("#000000"))
    assert not b.fit(Rect(0, 500, 100, 1000), port)         # less of it: all still there
    assert b.image.width() == 500
    assert b.image.pixelColor(490, 10) == QColor("#000000")
    assert b.fit(Rect(0, 500, 100, 2000), port)             # 1000 to 2000 wasn't kept
    assert b.image.pixelColor(490, 10) == QColor("#000000")
    with b.painter() as p:                                  # (0, 0) is still the port's
        p.fillRect(1500, 20, 1, 1, QColor("#000000"))
    assert b.image.pixelColor(1000, 20) == QColor("#000000")


def test_a_backing_that_grows_with_its_port_has_nothing_to_redraw(qapp):
    b = Backing(Rect(0, 0, 100, 200))
    assert not b.fit(Rect(0, 0, 150, 300), Rect(0, 0, 150, 300))  # the new part is the app's
    assert (b.image.width(), b.image.height()) == (300, 150)
