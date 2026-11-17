"""QuickDraw pictures: PackBits, hand-built version 1 and 2 pictures
played (scaled and clipped), pictures recorded with OpenPicture and
played back, and the masked copies (CopyMask, SeedFill, CalcMask)."""

import struct

import pytest
from PyQt6.QtGui import QImage

from macrt.resfork import Resource
from macrt.toolbox import pict
from macrt.toolbox.qdraw import srcCopy, srcXor
from macrt.toolbox.quickdraw import BLACK_PAT, GRAY_PAT, GrafPort
from macrt.toolbox.memmgr import memWZErr, noErr
from macrt.toolbox.types import Point, Rect

from guest import Guest, Processor, Room

FRAME = (0, 0, 32, 64)
BLANK = ["." * 64] * 32


@pytest.fixture
def g(qapp):
    return Guest()


def rect(*r) -> bytes:
    return struct.pack(">hhhh", *r)


def v1(frame, body: bytes) -> bytes:
    """A version 1 picture: size, frame, $1101, the opcodes, $FF."""
    data = b"\x11\x01" + body + b"\xff"
    return struct.pack(">H", 10 + len(data)) + rect(*frame) + data


def v2(frame, *ops) -> bytes:
    """A version 2 picture: each (opcode, data) on a word boundary."""
    body = struct.pack(">HHH", 0x0011, 0x02FF, 0x0C00) + struct.pack(">l", -1) + bytes(20)
    for opcode, data in ops + ((0x00FF, b""),):
        body += struct.pack(">H", opcode) + data
        if len(body) & 1:
            body += b"\0"
    return struct.pack(">H", 10 + len(body)) + rect(*frame) + body


def play(g, port, data, dst=None, comment=None):
    frame = pict.picture_frame(data)
    pict.draw_picture(g.tb.qd, port, frame, pict.Reader.of(data, 10), Rect(*(dst or frame)),
                      comment)


def black(rows, top, left, bottom, right) -> bool:
    """Is every pixel of the rectangle black?"""
    return all(rows[y][x] == "#" for y in range(top, bottom) for x in range(left, right))


def ones(rows) -> int:
    return sum(r.count("#") for r in rows)


# -- PackBits -------------------------------------------------------------------
def test_pack_bits_is_inside_macintoshs_example():
    unpacked = bytes.fromhex("AAAAAA80002AAAAAAAAA80002A22" + "AA" * 10)
    packed = bytes.fromhex("FEAA0280002AFDAA0380002A22F7AA")
    assert pict.pack_bits(unpacked) == packed
    assert pict.unpack_bits(packed, len(unpacked)) == unpacked


def test_pack_bits_round_trips_long_runs_and_literals():
    data = bytes(300) + bytes(range(256)) * 2 + b"\x55" * 129 + b"\x01\x02"
    assert pict.unpack_bits(pict.pack_bits(data), len(data)) == data
    # Words, for 16-bit pixel maps: two literal words, then one word 3 times.
    assert pict.unpack_bits(bytes.fromhex("01 1234 5678 FE ABCD"), 10, 2) == bytes.fromhex(
        "12345678ABCDABCDABCD")


def test_the_traps_move_the_pointers_past_what_they_took_and_gave(g):
    """A program packs and unpacks a bitmap a row at a time, each row from
    where the last left the pointers (IM I-470)."""
    rows = [bytes.fromhex("AAAAAA80002AAAAAAAAA80002A22" + "AA" * 10), bytes(24)]
    src, packed, out = g.ptr(b"".join(rows)), g.ptr(bytes(64)), g.ptr(bytes(48))
    vars_ = g.ptr(bytes(8))
    g.mem.w32(vars_, src)
    g.mem.w32(vars_ + 4, packed)
    for row in rows:
        g("PackBits", vars_, vars_ + 4, len(row))
    assert g.mem.r32(vars_) == src + 48
    assert g.mem.r32(vars_ + 4) == packed + 15 + 2
    assert g.mem.read(packed, 17) == bytes.fromhex("FEAA0280002AFDAA0380002A22F7AA E900")
    g.mem.w32(vars_, packed)
    g.mem.w32(vars_ + 4, out)
    for row in rows:
        g("UnpackBits", vars_, vars_ + 4, len(row))
    assert (g.mem.r32(vars_), g.mem.r32(vars_ + 4)) == (packed + 17, out + 48)
    assert g.mem.read(out, 48) == b"".join(rows)


# -- playing pictures -----------------------------------------------------------
def test_version_1_shapes_lines_and_bits(g):
    port, _ = g.port()
    bits = bytes.fromhex("FFFF 8001 8001 FFFF")         # a 16-by-4 box
    data = v1(FRAME, b"".join([
        b"\x31" + rect(2, 2, 10, 20),                   # PaintRect
        b"\x07" + struct.pack(">hh", 2, 2),             # PnSize 2, 2
        b"\x20" + struct.pack(">hhhh", 20, 2, 20, 30),  # Line (20, 2) to (20, 30), as (v, h)
        b"\x90" + struct.pack(">H", 2) + rect(0, 0, 4, 16) + rect(0, 0, 4, 16)
        + rect(24, 40, 28, 56) + struct.pack(">h", srcCopy) + bits,     # BitsRect
        b"\x0B" + struct.pack(">hh", 4, 4) + b"\x41" + rect(2, 40, 12, 60),  # PaintRoundRect
    ]))
    play(g, port, data)
    rows = g.rows(port)
    assert black(rows, 2, 2, 10, 20) and rows[1][2] == rows[2][20] == "."
    assert black(rows, 20, 2, 22, 31) and rows[22][10] == "."
    assert rows[24][40:56] == "#" * 16 and rows[25][40:56] == "#" + "." * 14 + "#"
    assert rows[2][40] == "." and rows[6][40] == "#"     # a rounded corner, a straight side
    # The port's settings are back as they were.
    assert port.pn_size == (1, 1)


def test_version_2_with_packbits_comments_and_reserved_opcodes(g):
    port, _ = g.port()
    rows8 = [b"\xff" * 8, bytes(8)] * 4                 # 64 wide, 8 high: stripes
    packed = b"".join(bytes([len(p)]) + p for p in map(pict.pack_bits, rows8))
    seen = []
    data = v2(
        FRAME,
        (0x001E, b""),                                  # DefHilite
        (0x00A0, struct.pack(">h", 100)),               # ShortComment
        (0x00A1, struct.pack(">hH", 200, 3) + b"abc"),  # LongComment, odd: padded
        (0x0024, struct.pack(">H", 3) + b"xyz"),        # reserved: a length and that much
        (0x0100, b"\0\0"),                              # reserved: 2 bytes
        (0x8100, struct.pack(">L", 5) + b"12345"),      # reserved: a long length
        (0x00B0, b""),                                  # reserved: nothing
        (0x001A, struct.pack(">HHH", 0, 0, 0)),         # RGBFgCol black
        (0x0098, struct.pack(">H", 8) + rect(0, 0, 8, 64) + rect(0, 0, 8, 64)
         + rect(4, 0, 12, 64) + struct.pack(">h", srcCopy) + packed),   # PackBitsRect
    )
    play(g, port, data, comment=lambda kind, d: seen.append((kind, d)))
    assert seen == [(100, None), (200, b"abc")]
    rows = g.rows(port)
    assert [rows[y] for y in range(4, 12)] == ["#" * 64, "." * 64] * 4
    assert ones(rows) == 4 * 64


def test_a_pixel_map_with_a_colour_table_is_thresholded(g):
    port, _ = g.port()
    ctab = struct.pack(">lHh", 0, 0, 1) + struct.pack(">HHHH", 0, 0xFFFF, 0xFFFF, 0xFFFF) \
        + struct.pack(">HHHH", 1, 0x8000, 0, 0)          # white, and a dark red
    pixmap = struct.pack(">hhlllhhhhlll", 0, 0, 0, 72 << 16, 72 << 16, 0, 8, 1, 8, 0, 0, 0)
    row = bytes([1] * 8 + [0] * 8)                       # 16 pixels at 8 bits: packed
    packed = b"".join(bytes([len(p)]) + p for p in [pict.pack_bits(row)] * 4)
    data = v2(FRAME, (0x0098, struct.pack(">H", 0x8000 | 16) + rect(0, 0, 4, 16) + pixmap
                      + ctab + rect(0, 0, 4, 16) + rect(0, 0, 4, 16)
                      + struct.pack(">h", srcCopy) + packed))
    play(g, port, data)
    rows = g.rows(port)
    assert [r[:16] for r in rows[:4]] == ["#" * 8 + "." * 8] * 4
    assert ones(rows) == 32


def test_a_pixel_map_draws_in_its_colours_in_a_colour_port(qapp):
    g = Guest(model="ii")
    g("OpenCPort", g.ptr(bytes(GrafPort.SIZE)))
    colors = [(0xFFFF, 0xFFFF, 0xFFFF), (0xFFFF, 0, 0), (0, 0, 0xFFFF)]
    ctab = struct.pack(">lHh", 0, 0, len(colors) - 1) + b"".join(
        struct.pack(">HHHH", i, *c) for i, c in enumerate(colors))
    pixmap = struct.pack(">hhlllhhhhlll", 0, 0, 0, 72 << 16, 72 << 16, 0, 8, 1, 8, 0, 0, 0)
    row = bytes([1] * 8 + [2] * 8)                       # red, then blue
    packed = b"".join(bytes([len(p)]) + p for p in [pict.pack_bits(row)] * 4)
    data = v2((0, 0, 4, 16), (0x0098, struct.pack(">H", 0x8000 | 16) + rect(0, 0, 4, 16)
                              + pixmap + ctab + rect(0, 0, 4, 16) + rect(0, 0, 4, 16)
                              + struct.pack(">h", srcCopy) + packed))
    g("DrawPicture", g.handle(data), g.rect_ptr((20, 30, 24, 46)))
    devices, bits = g.system.devices, g.system.screen_bits
    red, blue = (devices.nearest(0xFF000000 | (r >> 8) << 16 | (gr >> 8) << 8 | b >> 8)
                 for r, gr, b in colors[1:])
    table = devices.table()
    for v in range(20, 24):
        at = bits.base + v * bits.row_bytes
        assert [table[g.mem.r8(at + h)] for h in range(30, 46)] == [red] * 8 + [blue] * 8


def test_a_pictures_pixel_patterns_draw_in_their_colours_in_a_colour_port(qapp):
    """FillPixPat's pixel pattern, PnPixPat's RGB one, and then PnPat's
    old one; the port's own patterns are as they were after, and nothing
    is left in the heap."""
    g = Guest(model="ii")
    p = g.ptr(bytes(GrafPort.SIZE))
    g("OpenCPort", p)
    port = g.system.port_at(p)
    patterns = (port.bk_pix_pat, port.pn_pix_pat, port.fill_pix_pat)
    white, red, blue = (0xFFFF, 0xFFFF, 0xFFFF), (0xFFFF, 0, 0), (0, 0, 0xFFFF)
    ctab = struct.pack(">lHh", 0, 0, 2) + b"".join(
        struct.pack(">HHHH", i, *c) for i, c in enumerate((white, red, blue)))
    pixmap = struct.pack(">hhlllhhhhlll", 0, 0, 0, 72 << 16, 72 << 16, 0, 2, 1, 2, 0, 0, 0)
    stripes = (b"\x55\x55" + b"\xAA\xAA") * 4         # rows of values 1 and 2: red, blue
    fill = (struct.pack(">H", 1) + GRAY_PAT + struct.pack(">H", 0x8002) + rect(0, 0, 8, 8)
            + pixmap + ctab + stripes)
    pen = struct.pack(">H", 2) + BLACK_PAT + struct.pack(">HHH", *blue)
    data = v2((0, 0, 8, 16), (0x0014, fill), (0x0034, rect(0, 0, 4, 8)),
              (0x0013, pen), (0x0031, rect(4, 8, 8, 16)),
              (0x0009, BLACK_PAT), (0x0031, rect(0, 8, 4, 16)))
    pic, dst = g.handle(data), g.rect_ptr((20, 30, 28, 46))
    g("DrawPicture", pic, dst)
    devices, bits = g.system.devices, g.system.screen_bits
    table = devices.table()
    on = [devices.nearest(0xFF000000 | (r >> 8) << 16 | (gr >> 8) << 8 | b >> 8)
          for r, gr, b in ((0, 0, 0), red, blue)]

    def row(v, left, right):
        at = bits.base + v * bits.row_bytes
        return [table[g.mem.r8(at + h)] for h in range(left, right)]
    assert [row(v, 30, 38) for v in range(20, 24)] == [[on[1]] * 8, [on[2]] * 8] * 2
    assert [row(v, 38, 46) for v in range(24, 28)] == [[on[2]] * 8] * 4
    assert [row(v, 38, 46) for v in range(20, 24)] == [[on[0]] * 8] * 4
    assert (port.bk_pix_pat, port.pn_pix_pat, port.fill_pix_pat) == patterns
    assert port.pn_pat == BLACK_PAT
    free = g.process.heap.free_bytes()          # once there are master pointers enough
    g("DrawPicture", pic, dst)
    assert g.process.heap.free_bytes() == free


def test_direct_bits_in_32_bits(g):
    port, _ = g.port()
    pixmap = struct.pack(">hhlllhhhhlll", 0, 4, 0, 72 << 16, 72 << 16, 16, 32, 3, 8, 0, 0, 0)
    width = 8
    # packType 4: each row's red, green and blue planes in turn, packed.
    plane = bytes([0] * 4 + [255] * 4)                   # black then white
    row = pict.pack_bits(plane * 3)
    data = v2(FRAME, (0x009A, struct.pack(">L", 0xFF) + struct.pack(">H", 0x8000 | 4 * width)
                      + rect(0, 0, 2, width) + pixmap + rect(0, 0, 2, width)
                      + rect(0, 0, 2, width) + struct.pack(">h", srcCopy)
                      + (bytes([len(row)]) + row) * 2))
    play(g, port, data)
    rows = g.rows(port)
    assert rows[0][:8] == rows[1][:8] == "####...."
    assert ones(rows) == 8


@pytest.mark.parametrize("count, op, depth",
                         [(2, 0x98, 1), (5, 0x98, 4), (40, 0x98, 8), (300, 0x9A, 32)])
def test_a_colour_image_as_a_pixel_map_reads_back(qapp, count, op, depth):
    """pixmap_bits: the fewest bits a pixel for the image's colours, or
    32-bit direct pixels for more than 256 of them, read back as they
    were drawn."""
    width, height = 25, 12
    img = QImage(width, height, QImage.Format.Format_RGB32)
    for i in range(width * height):
        n = i % count
        img.setPixel(i % width, i // width, 0xFF000000 | n * 7 << 16 | n * 13 & 0xFF00 | n)
    opcode, head, rows, row_bytes = pict.pixmap_bits(img)
    r = pict.Reader.of(head + struct.pack(">hhhhh", 0, 0, height, width, srcCopy)
                       + pict.pixel_rows(rows, row_bytes))
    if opcode == 0x9A:
        r.read(4)
    assert opcode == op and r.u16() & 0x3FFF == row_bytes and r.rect() == Rect(0, 0, height, width)
    pack_type, pixel_size, cmp_count = pict.read_pixmap(r)
    ctab = pict.read_ctab(r) if opcode == 0x98 else None
    assert pixel_size == depth
    r.rect(), r.rect(), r.u16()
    back = pict.pixel_image(pict.read_pixels(r, row_bytes, height, True, pack_type, width,
                                             pixel_size, cmp_count),
                            width, height, pixel_size, ctab, pack_type, cmp_count)
    assert all(back.pixel(x, y) | 0xFF000000 == img.pixel(x, y)
               for y in range(height) for x in range(width))


def test_a_picture_scales_to_the_destination(g):
    port, _ = g.port()
    data = v1((0, 0, 8, 16), b"\x31" + rect(0, 0, 4, 8) + b"\x51" + rect(4, 8, 8, 16))
    play(g, port, data, dst=(0, 0, 16, 32))
    rows = g.rows(port)
    assert black(rows, 0, 0, 8, 16) and rows[8][0] == rows[0][16] == "."
    assert black(rows, 11, 19, 13, 29)                  # the oval's middle, doubled
    assert rows[8][16] == rows[15][31] == "."           # not its corners


def test_space_extra_scales_with_the_picture(g):
    """SpExtra is pixels in the picture's coordinates: a picture drawn at
    half its size adds half as many to each space, as its text positions
    and size are halved (the MayaQuest ReadMe's 2304-dpi pages)."""
    port, _ = g.port()
    data = v1((0, 0, 64, 128), b"\x0D\x00\x18\x06" + struct.pack(">l", 16 << 16)
              + b"\x28" + struct.pack(">hh", 40, 4) + b"\x03i i")
    play(g, port, data, dst=FRAME)
    other, _ = g.port()
    g.set_port(other)
    g("TextSize", 12)
    g("SpaceExtra", 8 << 16)
    g("MoveTo", 2, 20)
    g("DrawString", g.ptr(b"\x03i i"))
    assert ones(g.rows(port)) and g.rows(port) == g.rows(other)


def test_a_picture_is_clipped_to_its_clip_and_the_ports(g):
    port, _ = g.port()
    g("ClipRect", g.rect_ptr((0, 0, 4, 64)))
    data = v1(FRAME, b"\x01" + struct.pack(">H", 10) + rect(0, 0, 8, 8)
              + b"\x31" + rect(0, 0, 32, 64))
    play(g, port, data)
    rows = g.rows(port)
    assert black(rows, 0, 0, 4, 8) and ones(rows) == 32
    assert g.rect(g.heap.deref(port.clip_rgn) + 2) == Rect(0, 0, 4, 64)   # put back


def test_init_port_sets_the_regions_it_keeps_afresh(g):
    """Dark Castle draws a picture in a port it has set up again with
    InitPort, after narrowing its visRgn for a smaller bitmap."""
    port, _ = g.port()
    vis, clip = port.vis_rgn, port.clip_rgn
    g("ClipRect", g.rect_ptr((0, 0, 4, 64)))
    g("RectRgn", vis, g.rect_ptr((0, 0, 8, 8)))
    g("InitPort", port.addr)
    assert (port.vis_rgn, port.clip_rgn) == (vis, clip)
    assert g.rect(g.heap.deref(vis) + 2) == g.system.screen_bits.bounds
    assert g.rect(g.heap.deref(clip) + 2) == Rect(-32767, -32767, 32767, 32767)


def test_a_bad_picture_draws_what_it_can(g):
    port, _ = g.port()
    data = v1(FRAME, b"\x31" + rect(0, 0, 2, 2) + b"\x90" + struct.pack(">H", 2))  # cut short
    play(g, port, data)
    assert ones(g.rows(port)) == 4


def direct_bits(bounds, src, dst, cmp_count=3) -> tuple[int, bytes]:
    """A DirectBitsRect of black 32-bit pixels, packType 4: its opcode and
    data."""
    width = bounds[3] - bounds[1]
    pixmap = struct.pack(">hhlllhhhhlll", 0, 4, 0, 72 << 16, 72 << 16, 16, 32, cmp_count, 8,
                         0, 0, 0)
    row = pict.pack_bits(bytes(3 * width))
    return 0x009A, (struct.pack(">L", 0xFF) + struct.pack(">H", 0x8000 | 4 * width)
                    + rect(*bounds) + pixmap + rect(*src) + rect(*dst)
                    + struct.pack(">h", srcCopy)
                    + (bytes([len(row)]) + row) * (bounds[2] - bounds[0]))


@pytest.mark.parametrize("cmp_count", [0, 32767])
def test_direct_bits_packed_in_components_it_cannot_have_end_the_picture(g, cmp_count):
    """packType 4 packs 3 components, or 4: a picture that says it packs
    none, or thousands, draws what came before it and no more."""
    port, _ = g.port()
    data = v2(FRAME, (0x0031, rect(0, 0, 2, 2)),
              direct_bits((0, 0, 2, 8), (0, 0, 2, 8), (0, 0, 2, 8), cmp_count),
              (0x0031, rect(4, 4, 6, 6)))
    play(g, port, data)
    rows = g.rows(port)
    assert black(rows, 0, 0, 2, 2) and ones(rows) == 4


@pytest.mark.parametrize("cmp_count", [0, 2])
def test_unpacked_direct_pixels_of_too_few_components_are_read_as_xrgb(qapp, cmp_count):
    """Rows too narrow to be packed are read as they are: 4 bytes a pixel,
    padded, whatever the components."""
    img = pict.pixel_image([b"\x10\x20"] * 3, 1, 3, 32, None, 4, cmp_count)
    assert (img.width(), img.height()) == (1, 3)
    assert {img.pixel(0, y) & 0xFFFFFF for y in range(3)} == {0x200000}


def tall_pix_pat(rows: int) -> tuple[int, bytes]:
    """A FillPixPat of a white 1-bit pixel pattern 8 wide: its opcode and
    data."""
    pixmap = struct.pack(">hhlllhhhhlll", 0, 0, 0, 72 << 16, 72 << 16, 0, 1, 1, 1, 0, 0, 0)
    ctab = struct.pack(">lHh", 0, 0, 0) + struct.pack(">HHHH", 0, 0xFFFF, 0xFFFF, 0xFFFF)
    return 0x0014, (struct.pack(">H", 1) + GRAY_PAT + struct.pack(">H", 0x8002)
                    + rect(0, 0, rows, 8) + pixmap + ctab + bytes(2 * rows))


def test_a_pixel_pattern_bigger_than_a_picture_may_hold_ends_it(g):
    port, _ = g.port()
    data = v2(FRAME, (0x0031, rect(0, 0, 2, 2)), tall_pix_pat(pict.MAX_SIDE + 1),
              (0x0031, rect(4, 4, 6, 6)))
    play(g, port, data)
    rows = g.rows(port)
    assert black(rows, 0, 0, 2, 2) and ones(rows) == 4


def test_a_source_rectangle_past_the_bitmap_draws_only_the_bitmap(g):
    """Only the part of srcRect inside the bitmap's bounds has pixels: it
    goes to the same part of dstRect, and the rest is left as it was."""
    port, _ = g.port()
    white = (0x0090, struct.pack(">H", 2) + rect(0, 0, 2, 2) + rect(0, 0, 4, 4)
             + rect(0, 0, 20, 20) + struct.pack(">h", srcCopy) + bytes(4))
    play(g, port, v2(FRAME, (0x0031, rect(*FRAME)), white))
    rows = g.rows(port)
    assert ones(rows) == 32 * 64 - 100 and not any("#" in r[:10] for r in rows[:10])
    port, _ = g.port()
    play(g, port, v2(FRAME, direct_bits((2, 2, 4, 4), (0, 0, 4, 4), (0, 0, 20, 20))))
    rows = g.rows(port)
    assert ones(rows) == 100 and black(rows, 10, 10, 20, 20)


# -- recording ------------------------------------------------------------------
def scene(g):
    """Some of everything a picture records."""
    g("ClipRect", g.rect_ptr((0, 0, 30, 64)))
    g("PenSize", 2, 1)
    g("FrameRect", g.rect_ptr((1, 1, 12, 20)))
    g("PaintOval", g.rect_ptr((3, 3, 10, 18)))
    g("PenNormal")
    g("FillRoundRect", g.rect_ptr((14, 1, 30, 20)), 8, 8, g.ptr(GRAY_PAT))
    g("InvertArc", g.rect_ptr((14, 1, 30, 20)), 0, 90)
    g("PenPat", g.ptr(GRAY_PAT))
    g("MoveTo", 22, 2)
    g("LineTo", 40, 12)
    g("Line", 10, 0)
    g("PenPat", g.ptr(BLACK_PAT))
    poly = g("OpenPoly")
    g("MoveTo", 44, 14)
    g("LineTo", 62, 14)
    g("LineTo", 53, 28)
    g("LineTo", 44, 14)
    g("ClosePoly")
    g("PaintPoly", poly)
    g("FramePoly", poly)
    rgn = g("NewRgn")
    g("RectRgn", rgn, g.rect_ptr((20, 22, 28, 40)))
    g("InvertRgn", rgn)
    g("TextFace", 1)
    g("MoveTo", 22, 26)
    g("DrawString", g.ptr(b"\x02Hi"))
    src_bits = g.ptr(bytes.fromhex("F0F0 0F0F") * 4)
    bitmap = g.ptr(struct.pack(">LH", src_bits, 2) + rect(0, 0, 8, 16))
    port = g.tb.qd.sys.port_at(g.mem.r32(g.mem.r32(g.process.partition.a5)))
    g("CopyBits", bitmap, port.addr + 2, g.rect_ptr((0, 0, 8, 16)), g.rect_ptr((2, 44, 10, 60)),
      srcCopy, 0)
    g("PicComment", 150, 0, 0)


def test_a_recorded_picture_plays_back_as_it_was_drawn(g):
    direct, _ = g.port()
    scene(g)
    expected = g.rows(direct)
    assert ones(expected) > 300

    recording, _ = g.port()
    pic = g("OpenPicture", g.rect_ptr(FRAME))
    assert recording.pic_save and recording.pn_vis == -1
    scene(g)
    g("ClosePicture")
    assert recording.pic_save == 0 and recording.pn_vis == 0
    assert g.rows(recording) == BLANK                   # nothing drawn while it recorded
    assert g.heap.handle_size(pic) == struct.unpack(">H", g.contents(pic)[:2])[0]

    played, _ = g.port()
    g("DrawPicture", pic, g.rect_ptr(FRAME))
    assert g.rows(played) == expected


def test_the_opcodes_a_recording_makes(g):
    g.port()
    pic = g("OpenPicture", g.rect_ptr(FRAME))
    g("ClipRect", g.rect_ptr(FRAME))
    g("PaintRect", g.rect_ptr((1, 2, 3, 4)))
    g("PaintRect", g.rect_ptr((1, 2, 3, 4)))
    g("PicComment", 100, 0, 0)
    g("PicComment", 101, 2, g.handle(b"ok"))
    g("ClosePicture")
    assert g.contents(pic).hex() == (
        "002c" "000000000020" "0040"                    # size, frame
        "1101"                                          # version 1
        "01" "000a" "0000000000200040"                  # clip
        "31" "0001000200030004"                         # paintRect
        "39"                                            # paintSameRect
        "a0" "0064"                                     # shortComment
        "a1" "0065" "0002" "6f6b"                       # longComment
        "ff")


def test_open_picture_with_no_port_or_no_room_records_nothing(g, monkeypatch):
    port, _ = g.port()
    room = Room(monkeypatch, g)
    room.limit(0)
    assert g("OpenPicture", g.rect_ptr(FRAME)) == 0
    assert port.pic_save == 0 and port.pn_vis == 0 and not g.tb.qd.pictures
    room.limit(None)
    g("SetPort", 0)
    assert g("OpenPicture", g.rect_ptr(FRAME)) == 0
    assert not g.tb.qd.pictures
    g("ClosePicture")                                   # no port: nothing to close
    g.set_port(port)
    g("ClosePicture")                                   # nothing being recorded
    assert port.pn_vis == 0


def record(g, comment: int) -> int:
    """A picture of a rectangle and a long comment of its data."""
    pic = g("OpenPicture", g.rect_ptr(FRAME))
    g("PaintRect", g.rect_ptr((0, 0, 2, 2)))
    g("PicComment", 200, g.heap.handle_size(comment), comment)
    g("ClosePicture")
    return pic


def test_close_picture_spools_through_the_put_pic_proc(g, monkeypatch):
    """A port's putPicProc is given the picture a buffer's worth at a
    time, the handle keeping its size and frame; with no room for the
    buffer, it's given nothing."""
    comment = g.handle(bytes(range(256)) * 20)
    port, _ = g.port()
    expected = g.contents(record(g, comment))
    assert len(expected) > 4096
    procs_of(g, port, _48=0x1234)
    spooled, sizes = bytearray(), []

    def put_pic(buf, n):
        spooled.extend(g.mem.read(buf, n))
        sizes.append(n)
    g.procs[0x1234] = put_pic
    pic = record(g, comment)
    assert bytes(spooled) == expected and sizes == [4096, len(expected) - 4096]
    assert g.contents(pic) == expected[:10]
    assert not g.heap.is_ptr(g.calls[0][1][0][1])       # the buffer, given back
    assert port.pic_save == 0 and port.pn_vis == 0
    room = Room(monkeypatch, g)
    sizes.clear()
    pic = g("OpenPicture", g.rect_ptr(FRAME))
    g("PaintRect", g.rect_ptr((0, 0, 2, 2)))
    room.limit(0)
    g("ClosePicture")
    assert sizes == [] and g.contents(pic)[2:] == rect(*FRAME)
    assert port.pic_save == 0 and port.pn_vis == 0


def test_with_no_room_for_the_picture_it_is_an_empty_one(g, monkeypatch):
    port, _ = g.port()
    room = Room(monkeypatch, g)
    pic = g("OpenPicture", g.rect_ptr(FRAME))
    g("PaintRect", g.rect_ptr((0, 0, 2, 2)))
    room.limit(0)
    g("ClosePicture")
    assert g.contents(pic) == struct.pack(">H", 10) + rect(*FRAME)
    assert port.pic_save == 0 and port.pn_vis == 0
    room.limit(None)
    g("DrawPicture", pic, g.rect_ptr(FRAME))
    assert ones(g.rows(port)) == 0


def test_a_picture_disposed_of_while_it_was_recorded_is_left_alone(g):
    """ClosePicture of a picture whose handle the program has disposed of
    or purged writes nothing where its master pointer would take it: to
    0, or, once disposed of, to the next free master pointer."""
    port, _ = g.port()
    low = g.mem.read(0, 0x40)
    for h in (g.handle(b"a"), g.handle(b"b")):
        g("DisposeHandle", h)                           # free master pointers, linked
    for gone in ("DisposeHandle", "EmptyHandle"):
        pic = g("OpenPicture", g.rect_ptr(FRAME))
        g("PaintRect", g.rect_ptr((0, 0, 2, 2)))
        g(gone, pic)
        link = g.mem.r32(pic)                           # the next free one's, once disposed
        assert bool(link) == (gone == "DisposeHandle")
        there = g.mem.read(link, 16)
        g("ClosePicture")
        assert g.mem.read(0, 0x40) == low and g.mem.read(link, 16) == there
        assert port.pic_save == 0 and port.pn_vis == 0


def test_a_colour_picture_is_version_2(g):
    port, _ = g.port()
    params = g.ptr(rect(*FRAME) + struct.pack(">llhhl", 72 << 16, 72 << 16, -2, 0, 0))
    pic = g("OpenCPicture", params)
    g("PaintRect", g.rect_ptr((0, 0, 4, 4)))
    g("ClosePicture")
    data = g.contents(pic)
    assert data[10:16].hex() == "001102ff0c00" and data[16:18] == b"\xff\xfe"
    assert data[-2:] == b"\x00\xff" and len(data) % 2 == 0
    g("DrawPicture", pic, g.rect_ptr(FRAME))
    assert ones(g.rows(port)) == 16


def test_get_picture_and_kill_picture(qapp):
    g = Guest([Resource(b"PICT", 128, v1(FRAME, b"\x31" + rect(0, 0, 2, 2)))])
    port, _ = g.port()
    h = g("GetPicture", 128)
    assert h
    g("DrawPicture", h, g.rect_ptr((0, 0, 64, 128)))  # twice the size
    assert ones(g.rows(port)) == 16
    g("KillPicture", h)                                 # a resource: released, not disposed
    h2 = g("GetPicture", 128)
    assert h2 and g.contents(h2)[:2] == struct.pack(">H", 10 + 2 + 9 + 1)


def test_kill_picture(g):
    """KillPicture disposes of a picture, one being recorded too, which is
    then recorded into no more; NIL, or a handle in no heap, it passes
    over, with MemErr as DisposeHandle sets it."""
    port, _ = g.port()
    g.set_mem_err(memWZErr)
    g("KillPicture", 0)
    assert g.mem_err == memWZErr
    pic = g("OpenPicture", g.rect_ptr(FRAME))
    other, _ = g.port()
    kept = g("OpenPicture", g.rect_ptr(FRAME))
    g.set_port(port)
    g.set_mem_err(memWZErr)
    g("KillPicture", pic)
    assert not g.heap.is_handle(pic) and list(g.tb.qd.pictures) == [other.addr]
    assert g.mem_err == noErr
    g("PicComment", 100, 0, 0)                          # not recorded anywhere
    g.set_port(other)
    g("ClosePicture")
    assert g.contents(kept) == v1(FRAME, b"")
    master = g.process.partition.a5 - 0x100             # a master pointer of its own
    g.mem.w32(master, g.ptr(v1(FRAME, b"")))
    g("KillPicture", master)
    assert g.mem_err == memWZErr


def test_drawing_no_picture_or_in_no_port_draws_nothing(g):
    """DrawPicture of NIL, of a handle that's been emptied, or with no
    current port, draws nothing."""
    port, _ = g.port()
    data = v1(FRAME, b"\x31" + rect(0, 0, 2, 2))
    g.mem.write(0, data)                                # where a NIL master pointer leads
    g.mem.write(0x100, struct.pack(">L", 0))
    empty = g.handle(data)
    g("EmptyHandle", empty)
    for h in (0, empty, 0x100):
        g("DrawPicture", h, g.rect_ptr(FRAME))
    g.mem.w32(0, g.ptr(data))                           # NIL is no handle, whatever's at 0
    g("DrawPicture", 0, g.rect_ptr(FRAME))
    g("SetPort", 0)
    g("DrawPicture", g.handle(data), g.rect_ptr(FRAME))
    assert ones(g.rows(port)) == 0


def procs_of(g, port, **offsets) -> int:
    """The standard procs for port, with these (an offset: an address)
    in place of some."""
    procs = g.ptr(bytes(52))
    g("SetStdProcs", procs)
    for at, addr in offsets.items():
        g.mem.w32(procs + int(at[1:]), addr)
    port.graf_procs = procs
    return procs


def test_a_comment_procs_long_comments_are_in_a_handle_while_it_runs(g, monkeypatch):
    """A long comment's data is in a handle the commentProc is given,
    disposed of after; with no room for one, the proc is given none."""
    port, _ = g.port()
    procs_of(g, port, _36=0x5678)
    seen, handles = [], []

    def comment(kind, size, h):
        seen.append((kind, size, g.contents(h) if h else None))
        handles.append(h)
    g.procs[0x5678] = comment
    pic = g.handle(v1(FRAME, b"\xa1" + struct.pack(">hh", 7, 3) + b"abc" + b"\xa0\x00\x08"))
    g("DrawPicture", pic, g.rect_ptr(FRAME))
    assert seen == [(7, 3, b"abc"), (8, 0, None)]
    assert handles[0] and not g.heap.is_handle(handles[0])
    seen.clear()
    Room(monkeypatch, g).limit(0)
    g("DrawPicture", pic, g.rect_ptr(FRAME))
    assert seen == [(7, 0, None), (8, 0, None)]


def test_a_spooled_picture_with_no_room_for_its_buffer_draws_nothing(g, monkeypatch):
    port, _ = g.port()
    procs_of(g, port, _44=0x1234)
    header = g.handle(v1(FRAME, b"\x31" + rect(0, 0, 2, 2))[:10])
    Room(monkeypatch, g).limit(0)
    g("DrawPicture", header, g.rect_ptr(FRAME))
    assert g.calls == [] and ones(g.rows(port)) == 0


def test_spooled_pictures_and_comment_procs(g):
    """A port's getPicProc gives the opcodes after the size and frame,
    and its commentProc sees the comments."""
    port, _ = g.port()
    data = v1(FRAME, b"\xa0\x00\x07" + b"\x31" + rect(0, 0, 2, 2))
    stream = bytearray(data[10:])
    procs = g.ptr(bytes(52))
    g("SetStdProcs", procs)
    assert g.mem.r32(procs + 44) == g.machine.stub_for(0xA8EE)
    g.mem.w32(procs + 44, 0x1234)                       # getPicProc
    g.mem.w32(procs + 36, 0x5678)                       # commentProc
    port.graf_procs = procs
    comments = []

    def get_pic(buf, n):
        g.mem.write(buf, bytes(stream[:n]))
        del stream[:n]
    g.procs[0x1234] = get_pic
    g.procs[0x5678] = lambda *args: comments.append(args)
    header = g.handle(data[:10])
    g("DrawPicture", header, g.rect_ptr(FRAME))
    assert ones(g.rows(port)) == 4 and comments == [(7, 0, 0)]


def test_a_spooled_picture_whose_handle_moves(g):
    """The frame is the handle's as DrawPicture is called: the buffer it
    reads the spooled opcodes into can move an unlocked handle (Winter
    Games' event pictures)."""
    port, _ = g.port()
    data = v1(FRAME, b"\x31" + rect(0, 0, 2, 2))
    stream = bytearray(data[10:])
    procs = g.ptr(bytes(52))
    g("SetStdProcs", procs)
    g.mem.w32(procs + 44, 0x1234)                       # getPicProc
    port.graf_procs = procs

    def get_pic(buf, n):
        g.mem.write(buf, bytes(stream[:n]))
        del stream[:n]
    g.procs[0x1234] = get_pic
    header = g.handle(data[:10])
    heap = g.system.heap_of(header)
    new_ptr = heap.new_ptr

    def moving(size, clear=False):
        was = g.mem.r32(header)
        heap.move_high(header)
        g.mem.write(was & 0xFFFFFF, bytes(10))          # where it was, now something else
        return new_ptr(size, clear)
    heap.new_ptr = moving
    g("DrawPicture", header, g.rect_ptr(FRAME))
    assert ones(g.rows(port)) == 4


def test_text_goes_through_the_ports_text_procs(qapp):
    """A port's own textProc draws, and its txMeasProc measures, the text
    the drawing and width calls are given, at the scale it leaves in numer
    and denom: Macintosh Pascal's interpret the formatting bytes in its
    program text."""
    g = Processor()
    port, _ = g.port()
    procs = g.ptr(bytes(52))
    g("SetStdProcs", procs)
    g.mem.w32(procs + 0, 0x1234)                        # textProc
    g.mem.w32(procs + 40, 0x5678)                       # txMeasProc
    port.graf_procs = procs
    drawn = []

    def measure(count, p, numer, denom, info):
        g.mem.w16(numer + 2, 2)                         # twice as wide
        return 5 * count
    g.procs[0x1234] = lambda count, p, numer, denom: drawn.append(g.mem.read(p, count))
    g.procs[0x5678] = measure
    text = g.ptr(b"xhello")
    g("DrawText", text, 1, 5)
    g("DrawChar", ord("!"))
    g("DrawString", g.ptr(b"\x02hi"))
    assert drawn == [b"hello", b"!", b"hi"]
    assert {tuple(v for _, v in args[2:]) for at, args in g.calls if at == 0x1234} \
        == {(0x10001, 0x10001)}                         # numer and denom: 1:1
    assert g("TextWidth", text, 1, 5) == 50
    assert g("CharWidth", ord("a")) == 10
    assert g("StringWidth", g.ptr(b"\x02hi")) == 20
    assert ones(g.rows(port)) == 0                      # nothing drawn the standard way


def test_copy_bits_into_the_port_goes_through_its_bits_proc(g):
    """CopyBits into thePort's portBits is the port's bitsProc's to do, as
    HyperCard's AddColor puts its colour over the card that way; into any
    other bitmap it copies itself."""
    port, _ = g.port()
    procs = g.ptr(bytes(52))
    g("SetStdProcs", procs)
    assert g.mem.r32(procs + 32) == g.machine.stub_for(0xA8EB)
    g.mem.w32(procs + 32, 0x1234)                       # bitsProc
    port.graf_procs = procs
    g.procs[0x1234] = lambda *args: None
    src = g.ptr(struct.pack(">LH4h", g.ptr(b"\xFF\xFF" * 2), 2, 0, 0, 2, 16))
    r, mask = g.rect_ptr((0, 0, 2, 16)), g.handle(bytes(10))
    g("CopyBits", src, port.addr + 2, r, r, srcXor, mask)
    assert g.calls == [(0x1234, (("p", src), ("p", r), ("p", r), ("h", srcXor), ("p", mask)))]
    assert ones(g.rows(port)) == 0                      # the proc didn't draw it
    other, bits = g.port()
    g.set_port(port)
    g("CopyBits", src, other.addr + 2, r, r, srcCopy, 0)
    assert len(g.calls) == 1 and ones(g.rows(other)) == 32


def test_shapes_go_through_the_ports_shape_procs(g):
    """A port's own rectProc, rRectProc, ovalProc, arcProc, polyProc and
    rgnProc draw its shapes, called with the verb in place of StdRect and
    the rest, a Fill's pattern put in fillPat first: Treasure MathStorm!'s
    rectProc does nothing, so that TextEdit's erasing leaves its panels'
    colour."""
    port, _ = g.port()
    procs = g.ptr(bytes(52))
    g("SetStdProcs", procs)
    assert g.mem.r32(procs + 8) == g.machine.stub_for(0xA8A0)
    g("EraseRect", g.rect_ptr((0, 0, 1, 1)))            # the standard ones: no calls
    assert g.calls == []
    for offset in range(8, 32, 4):                      # rectProc to rgnProc
        g.mem.w32(procs + offset, 0x1000 + offset)
    port.graf_procs = procs
    r, pat = g.rect_ptr((0, 0, 8, 16)), g.ptr(GRAY_PAT)
    poly = g.handle(struct.pack(">h4h2h2h2h", 22, 0, 0, 4, 4, 0, 0, 4, 4, 0, 0))
    rgn = g("NewRgn")
    g("EraseRect", r)
    g("FillRect", r, pat)
    g("FrameRoundRect", r, 4, 6)
    g("PaintOval", r)
    g("InvertArc", r, 90, 45)
    g("FramePoly", poly)
    g("FillRgn", rgn, g.ptr(BLACK_PAT))
    assert g.calls == [(0x1008, (("b", 2), ("p", r))), (0x1008, (("b", 4), ("p", r))),
                       (0x100C, (("b", 0), ("p", r), ("h", 4), ("h", 6))),
                       (0x1010, (("b", 1), ("p", r))),
                       (0x1014, (("b", 3), ("p", r), ("h", 90), ("h", 45))),
                       (0x1018, (("b", 0), ("p", poly))), (0x101C, (("b", 4), ("p", rgn)))]
    assert port.fill_pat == BLACK_PAT
    assert ones(g.rows(port)) == 0                      # nothing drawn the standard way


def test_std_tx_meas_measures_and_gives_the_font_info(g):
    port, _ = g.port()
    text = g.ptr(b"hello")
    info, numer, denom = g.ptr(bytes(8)), g.ptr(bytes(4)), g.ptr(bytes(4))
    width = g("StdTxMeas", 5, text, numer, denom, info)
    assert width == g("TextWidth", text, 0, 5) > 0
    fi = g.ptr(bytes(8))
    g("GetFontInfo", fi)
    assert g.mem.read(info, 8) == g.mem.read(fi, 8) != bytes(8)
    assert g("StdTxMeas", 0, text, numer, denom, info) == 0


def drawn(g, draw) -> list[str]:
    """What draw(port) draws in a fresh port, the pen at (2, 20)."""
    port, _ = g.port()
    g("MoveTo", 2, 20)
    draw(port)
    return g.rows(port)


def test_the_standard_procs_draw_as_the_calls_they_stand_for(g):
    """StdRect and the rest, called themselves, draw as their verb says
    (Fill in the port's fillPat), StdLine, StdText and StdBits as LineTo,
    DrawText and CopyBits do."""
    g.port()
    r = g.rect_ptr((2, 3, 12, 30))
    poly = g("OpenPoly")
    g("MoveTo", 4, 2)
    g("LineTo", 40, 4)
    g("LineTo", 20, 28)
    g("ClosePoly")
    rgn, small = g("NewRgn"), g("NewRgn")
    g("RectRgn", rgn, g.rect_ptr((4, 6, 20, 50)))
    g("RectRgn", small, g.rect_ptr((6, 10, 12, 30)))
    text = g.ptr(b"Hi")
    one = Point(1, 1)
    src = bitmap(g, [b"\xf0\x0f"] * 8, 16)
    sr, dr = g.rect_ptr((0, 0, 8, 16)), g.rect_ptr((4, 8, 20, 40))
    gray = g.ptr(GRAY_PAT)

    def filled(std):
        def draw(port):
            port.fill_pat = GRAY_PAT
            std()
        return draw
    for std, same in (
            (lambda p: g("StdRect", 1, r), lambda p: g("PaintRect", r)),
            (filled(lambda: g("StdRect", 4, r)), lambda p: g("FillRect", r, gray)),
            (lambda p: g("StdOval", 0, r), lambda p: g("FrameOval", r)),
            (lambda p: g("StdRRect", 3, r, 20, 4), lambda p: g("InvertRoundRect", r, 20, 4)),
            (lambda p: g("StdArc", 1, r, 90, 135), lambda p: g("PaintArc", r, 90, 135)),
            (lambda p: g("StdPoly", 0, poly), lambda p: g("FramePoly", poly)),
            (lambda p: g("StdPoly", 1, poly), lambda p: g("PaintPoly", poly)),
            (filled(lambda: g("StdPoly", 4, poly)), lambda p: g("FillPoly", poly, gray)),
            (lambda p: g("StdRgn", 3, rgn), lambda p: g("InvertRgn", rgn)),
            (lambda p: g("StdLine", Point(4, 40)), lambda p: g("LineTo", 40, 4)),
            (lambda p: g("StdText", 2, text, one, one), lambda p: g("DrawText", text, 0, 2)),
            (lambda p: g("StdText", 1, text, one, one), lambda p: g("DrawText", text, 0, 1)),
            (lambda p: g("StdBits", src, sr, dr, srcCopy, 0),
             lambda p: g("CopyBits", src, p.addr + 2, sr, dr, srcCopy, 0)),
            (lambda p: g("StdBits", src, sr, dr, srcCopy, small),
             lambda p: g("CopyBits", src, p.addr + 2, sr, dr, srcCopy, small))):
        assert drawn(g, std) == drawn(g, same) != BLANK


def test_the_standard_procs_draw_nothing_with_no_verb_or_no_port(g):
    """A verb past Fill, or no current port, and the shape procs draw
    nothing, as the rest don't with no port; StdTxMeas measures nothing."""
    g.port()
    r = g.rect_ptr((2, 3, 12, 30))
    poly = g("OpenPoly")
    g("MoveTo", 4, 2)
    g("LineTo", 40, 4)
    g("LineTo", 20, 28)
    g("ClosePoly")
    rgn = g("NewRgn")
    g("RectRgn", rgn, g.rect_ptr((4, 6, 20, 50)))
    text, one, info = g.ptr(b"Hi"), Point(1, 1), g.ptr(b"\xaa" * 8)
    src = bitmap(g, [b"\xff\xff"] * 8, 16)
    sr = g.rect_ptr((0, 0, 8, 16))

    def none_drawn(port):
        for verb in (5, -1):
            g("StdRect", verb, r)
            g("StdOval", verb, r)
            g("StdRRect", verb, r, 8, 6)
            g("StdArc", verb, r, 0, 90)
            g("StdPoly", verb, poly)
            g("StdRgn", verb, rgn)
        g("SetPort", 0)
        for verb in range(5):
            g("StdRect", verb, r)
            g("StdPoly", verb, poly)
            g("StdRgn", verb, rgn)
        g("StdLine", Point(4, 40))
        g("StdText", 2, text, one, one)
        g("StdBits", src, sr, sr, srcCopy, 0)
        assert g("StdTxMeas", 2, text, one, one, info) == 0
    assert drawn(g, none_drawn) == BLANK
    assert g.mem.read(info, 8) == b"\xaa" * 8


def test_std_comment_is_pic_comment(g):
    g.port()
    pic = g("OpenPicture", g.rect_ptr(FRAME))
    g("StdComment", 100, 0, 0)
    g("StdComment", 101, 2, g.handle(b"ok"))
    g("ClosePicture")
    assert g.contents(pic) == v1(FRAME, b"\xa0\x00\x64" b"\xa1\x00\x65\x00\x02ok")


def test_draw_picture_puts_the_ports_state_back(g):
    port, _ = g.port()
    g("PenSize", 3, 3)
    g("TextSize", 18)
    before = g.mem.read(port.addr, 108)
    data = v1(FRAME, b"\x07" + struct.pack(">hh", 5, 5) + b"\x0d\x00\x09"
              + b"\x09" + GRAY_PAT + b"\x31" + rect(0, 0, 1, 1))
    g("DrawPicture", g.handle(data), g.rect_ptr(FRAME))
    assert g.mem.read(port.addr, 108) == before


def test_a_picture_through_a_master_pointer_of_the_programs_own_making(g):
    """Microsoft BASIC's run-time reads a picture in and makes its master
    pointer on the stack, outside any heap: the picture is drawn, as long
    as its block's, or failing that as its picSize says."""
    port, _ = g.port()
    data = v1(FRAME, b"\x31" + rect(2, 2, 10, 20))
    for p in (g.ptr(data), g.process.partition.a5 - 0x200):
        g.mem.write(p, data)
        master = g.process.partition.a5 - 0x100
        g.mem.w32(master, p)
        g("EraseRect", g.rect_ptr(FRAME))
        assert ones(g.rows(port)) == 0
        g("DrawPicture", master, g.rect_ptr(FRAME))
        rows = g.rows(port)
        assert black(rows, 2, 2, 10, 20) and ones(rows) == 8 * 18


# -- masks ----------------------------------------------------------------------
def bitmap(g, rows: list[bytes], width: int) -> int:
    rb = len(rows[0])
    bits = g.ptr(b"".join(rows))
    return g.ptr(struct.pack(">LH", bits, rb) + rect(0, 0, len(rows), width))


def test_copy_mask_copies_only_where_the_mask_is_set(g):
    port, _ = g.port()
    g("PaintRect", g.rect_ptr((0, 0, 8, 16)))           # what's there: black
    src = bitmap(g, [b"\xff\x00"] * 8, 16)              # left half black, right white
    mask = bitmap(g, [b"\x0f\xf0"] * 8, 16)             # the middle 8 columns
    r = g.rect_ptr((0, 0, 8, 16))
    g("CopyMask", src, mask, port.addr + 2, r, r, r)
    assert g.rows(port)[0][:16] == "#" * 8 + "...." + "####"
    # Scaled: mask and source map onto the destination together.
    g("CopyMask", src, mask, port.addr + 2, r, r, g.rect_ptr((10, 0, 26, 32)))
    assert g.rows(port)[12][:32] == "." * 8 + "#" * 8 + "." * 8 + "." * 8


def test_copy_deep_mask_has_a_mode_and_a_mask_region(g):
    port, _ = g.port()
    src = bitmap(g, [b"\xff\xff"] * 8, 16)
    mask = bitmap(g, [b"\xff\xff"] * 8, 16)
    rgn = g("NewRgn")
    g("RectRgn", rgn, g.rect_ptr((0, 0, 8, 4)))
    r = g.rect_ptr((0, 0, 8, 16))
    g("PaintRect", g.rect_ptr((0, 0, 8, 2)))
    g("CopyDeepMask", src, mask, port.addr + 2, r, r, r, srcXor, rgn)
    assert g.rows(port)[0][:16] == "..##" + "." * 12


def test_seed_fill_and_calc_mask(g):
    box = [b"\x00\x00", b"\x7e\x00", b"\x42\x00", b"\x42\x00", b"\x7e\x00", b"\x00\x00",
           b"\x00\x00", b"\x00\x00"]                      # a hollow square, open space around
    src = g.ptr(b"".join(box))
    dst = g.ptr(bytes(16))
    g("SeedFill", src, dst, 2, 2, 8, 1, 3, 2)            # from inside the square
    assert g.mem.read(dst, 16) == bytes.fromhex("0000 0000 3c00 3c00 0000 0000 0000 0000")
    g("SeedFill", src, dst, 2, 2, 8, 1, 10, 6)           # from outside it
    assert g.mem.read(dst, 4) == bytes.fromhex("ffff 81ff")
    g("CalcMask", src, dst, 2, 2, 8, 1)
    assert g.mem.read(dst, 16) == bytes.fromhex("0000 7e00 7e00 7e00 7e00 0000 0000 0000")


def _box_pixmap(g) -> int:
    """The hollow square of test_seed_fill_and_calc_mask, in blue on red,
    as an 8-bit PixMap."""
    colors = [(0xFFFF, 0xFFFF, 0xFFFF), (0xFFFF, 0, 0), (0, 0, 0xFFFF)]
    ctab = g.handle(struct.pack(">lHh", 0, 0, len(colors) - 1) + b"".join(
        struct.pack(">HHHH", i, *c) for i, c in enumerate(colors)))
    box = ["", "0111111", "01....1", "01....1", "0111111", "", "", ""]
    pixels = b"".join(bytes(2 if c == "1" else 1 for c in row.ljust(16, ".")) for row in box)
    return g.ptr(struct.pack(">LH", g.ptr(pixels), 0x8000 | 16) + rect(0, 0, 8, 16)
                 + struct.pack(">hhlllhhhhllL", 0, 0, 0, 72 << 16, 72 << 16, 0, 8, 1, 8, 0,
                               ctab, 0))


def test_seed_c_fill_and_calc_c_mask(qapp):
    """The same masks from colours: CalcCMask's walls are seedRGB's,
    SeedCFill fills the seed pixel's colour."""
    g = Guest(model="ii")
    src, dst = _box_pixmap(g), bitmap(g, [bytes(2)] * 8, 16)
    r = g.rect_ptr((0, 0, 8, 16))
    g("SeedCFill", src, dst, r, r, 3, 2, 0, 0)          # from inside the square
    assert g.mem.read(g.mem.r32(dst), 8) == bytes.fromhex("0000 0000 3c00 3c00")
    g("SeedCFill", src, dst, r, r, 10, 6, 0, 0)         # from outside it
    assert g.mem.read(g.mem.r32(dst), 4) == bytes.fromhex("ffff 81ff")
    blue = g.ptr(struct.pack(">HHH", 0, 0, 0xFFFF))
    g("CalcCMask", src, dst, r, r, blue, 0, 0)
    assert g.mem.read(g.mem.r32(dst), 12) == bytes.fromhex("0000 7e00 7e00 7e00 7e00 0000")
    # Into a mask wider than srcRect, and not on a byte: the rest is kept.
    wide = bitmap(g, [b"\xff\xff\xff\xff"] * 8, 32)
    g("CalcCMask", src, wide, r, g.rect_ptr((0, 3, 8, 19)), blue, 0, 0)
    assert g.mem.read(g.mem.r32(wide) + 4, 4) == bytes.fromhex("ef c0 1f ff")


@pytest.mark.parametrize("model", ["ii", "plus"])
def test_calc_c_mask_asks_a_match_proc(qapp, model):
    """A matchProc decides the walls, given each colour, with the
    device's GDRefCon, where there's a device, pointing to a MatchRec of
    seedRGB and matchData while it runs; for a colour it returns false
    for, the device decides, dark colours being walls."""
    g = Guest(model=model)
    src, dst = _box_pixmap(g), bitmap(g, [b"\xaa\xaa"] * 8, 16)
    r = g.rect_ptr((0, 0, 8, 16))
    seed = g.ptr(struct.pack(">HHH", 0x1212, 0x3434, 0x5656))
    gdh = g.mem.r32(0xCC8) if model == "ii" else 0
    ref_con = (lambda: g.mem.r32(g.mem.r32(gdh) + 0x1A)) if gdh else (lambda: 0)
    before = ref_con()
    asked, answers = [], {}

    def match(rgb, result):
        rec = ref_con()
        asked.append((g.mem.read(rgb, 6), g.mem.read(rec, 10) if rec else None))
        colour = g.mem.read(rgb, 6)
        if colour not in answers:
            return False
        if answers[colour] is not None:                 # None: true, with no result
            g.mem.w32(result, answers[colour])
        return True
    g.procs[0x1234] = match
    g("CalcCMask", src, dst, r, r, seed, 0x1234, 0xCAFE)
    assert g.mem.read(g.mem.r32(dst), 16) == b"\xff" * 16        # red and blue: dark
    rec = struct.pack(">HHHL", 0x1212, 0x3434, 0x5656, 0xCAFE)
    assert {c for c, _ in asked} >= {b"\xff\xff\0\0\0\0", b"\0\0\0\0\xff\xff"}
    assert {m for _, m in asked} == {rec if gdh else None} and ref_con() == before
    answers.update({b"\xff\xff\0\0\0\0": 0, b"\0\0\0\0\xff\xff": 1})
    g("CalcCMask", src, dst, r, r, seed, 0x1234, 0xCAFE)
    assert g.mem.read(g.mem.r32(dst), 12) == bytes.fromhex("0000 7e00 7e00 7e00 7e00 0000")
    answers.update({b"\xff\xff\0\0\0\0": None, b"\0\0\0\0\xff\xff": 0x100})  # bit 0 counts
    g("CalcCMask", src, dst, r, r, seed, 0x1234, 0xCAFE)
    assert g.mem.read(g.mem.r32(dst), 16) == bytes(16)


def test_masked_copies_of_nothing_or_through_nothing_copy_nothing(g):
    """CopyMask and CopyDeepMask with an empty srcRect or dstRect, a mask
    of no 1s or an empty mask region copy nothing; CopyDeepMask with no
    mask copies through its mask region alone, or with neither, all."""
    port, _ = g.port()
    src = bitmap(g, [b"\xff\xff"] * 8, 16)
    mask = bitmap(g, [b"\xff\xff"] * 8, 16)
    none = bitmap(g, [b"\x00\x00"] * 8, 16)
    r, empty = g.rect_ptr((0, 0, 8, 16)), g.rect_ptr((4, 4, 4, 12))
    dst = port.addr + 2
    g("CopyMask", src, mask, dst, empty, r, r)
    g("CopyMask", src, mask, dst, r, r, empty)
    g("CopyMask", src, none, dst, r, r, r)
    rgn = g("NewRgn")
    g("CopyDeepMask", src, 0, dst, r, r, r, srcCopy, rgn)
    g("CopyDeepMask", src, mask, dst, r, r, r, srcCopy, rgn)
    assert ones(g.rows(port)) == 0
    g("RectRgn", rgn, g.rect_ptr((0, 0, 8, 4)))
    g("CopyDeepMask", src, 0, dst, r, r, r, srcCopy, rgn)
    rows = g.rows(port)
    assert black(rows, 0, 0, 8, 4) and ones(rows) == 32
    g("CopyDeepMask", src, 0, dst, r, r, r, srcCopy, 0)
    assert ones(g.rows(port)) == 128


def test_a_mask_quickdraw_cannot_read_masks_nothing_out(g):
    """A mask BitMap with no bits (a NIL baseAddr), or an empty maskRect,
    is no mask: CopyMask copies as CopyBits would."""
    port, _ = g.port()
    src = bitmap(g, [b"\xff\xff"] * 8, 16)
    nil = g.ptr(struct.pack(">LH", 0, 2) + rect(0, 0, 8, 16))
    none = bitmap(g, [b"\x00\x00"] * 8, 16)
    r = g.rect_ptr((0, 0, 8, 16))
    for mask, mr in ((nil, r), (none, g.rect_ptr((2, 2, 2, 8)))):
        g("EraseRect", r)
        g("CopyMask", src, mask, port.addr + 2, r, mr, r)
        assert ones(g.rows(port)) == 128


def test_seed_fill_and_calc_mask_of_nothing_change_nothing(g):
    src, dst = g.ptr(bytes(8)), g.ptr(b"\xaa" * 8)
    for height, words in ((0, 1), (4, 0), (-1, 1), (4, -1)):
        g("SeedFill", src, dst, 2, 2, height, words, 0, 0)
        g("CalcMask", src, dst, 2, 2, height, words)
    assert g.mem.read(dst, 8) == b"\xaa" * 8


def test_colour_masks_of_nothing_change_nothing(qapp):
    """SeedCFill and CalcCMask of an empty srcRect, or of a BitMap with no
    bits, leave the mask as it was."""
    g = Guest(model="ii")
    src = _box_pixmap(g)
    nil = g.ptr(struct.pack(">LH", 0, 2) + rect(0, 0, 8, 16))
    dst = bitmap(g, [b"\xaa\xaa"] * 8, 16)
    r, empty = g.rect_ptr((0, 0, 8, 16)), g.rect_ptr((2, 2, 2, 8))
    blue = g.ptr(struct.pack(">HHH", 0, 0, 0xFFFF))
    for bits, sr in ((src, empty), (nil, r)):
        g("SeedCFill", bits, dst, sr, r, 3, 2, 0, 0)
        g("CalcCMask", bits, dst, sr, r, blue, 0, 0)
    assert g.mem.read(g.mem.r32(dst), 16) == b"\xaa" * 16


def test_a_colour_mask_goes_only_into_its_bitmaps_bounds(qapp):
    """The rows and columns of dstRect off the mask BitMap's bounds are
    left out, and a BitMap with no bits or no rowBytes is left alone."""
    g = Guest(model="ii")
    src = _box_pixmap(g)
    r = g.rect_ptr((0, 0, 8, 16))
    blue = g.ptr(struct.pack(">HHH", 0, 0, 0xFFFF))
    guard = b"\x55" * 16
    block = g.ptr(guard + bytes(16) + guard)            # the BitMap's bits between guards
    base = block + 16
    dst = g.ptr(struct.pack(">LH", base, 2) + rect(0, 0, 8, 16))
    for dr in ((0, 16, 8, 32), (0, -16, 8, 0), (0, 40, 8, 56), (0, -40, 8, -24)):
        g("CalcCMask", src, dst, r, g.rect_ptr(dr), blue, 0, 0)
    assert g.mem.read(base, 16) == bytes(16)
    g("CalcCMask", src, dst, r, g.rect_ptr((4, 0, 12, 16)), blue, 0, 0)
    assert g.mem.read(base, 16) == bytes.fromhex("0000 0000 0000 0000 0000 7e00 7e00 7e00")
    g("CalcCMask", src, dst, r, g.rect_ptr((-4, 0, 4, 16)), blue, 0, 0)
    assert g.mem.read(base, 8) == bytes.fromhex("7e00 0000 0000 0000")
    assert g.mem.read(block, 16) == g.mem.read(base + 16, 16) == guard
    low = bytes(i % 255 + 1 for i in range(0x40))
    g.mem.write(0, low)
    for bits, row_bytes in ((0, 2), (g.ptr(bytes(16)), 0)):
        g("CalcCMask", src, g.ptr(struct.pack(">LH", bits, row_bytes) + rect(0, 0, 8, 16)),
          r, r, blue, 0, 0)
        assert bits == 0 or g.mem.read(bits, 16) == bytes(16)
    assert g.mem.read(0, 0x40) == low


def test_mask_regions_follow_the_mask(g):
    from macrt.toolbox.masks import calc_mask, seed_fill
    assert calc_mask([b"\x80"], 8) == [b"\x80"]
    assert seed_fill([b"\x10"], 8, 0, 0) == [b"\xe0"]


def test_set_std_c_procs(g):
    """A CQDProcs is a QDProcs and seven more, opcodeProc and six reserved,
    which are NIL."""
    procs = g.ptr(b"\xff" * 80)
    g("SetStdCProcs", procs)
    assert g.mem.r32(procs + 44) == g.machine.stub_for(0xA8EE)
    assert g.mem.read(procs + 52, 28) == bytes(28)
