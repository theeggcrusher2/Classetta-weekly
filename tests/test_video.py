"""The screen's video driver on a Mac II, and changing the screen's depth
while programs run (glue_video)."""

import struct

import pytest

from macrt.m68k import glue_files, glue_video
from macrt.toolbox import lowmem
from macrt.toolbox.color import standard_table
from macrt.toolbox.files import FileError
from macrt.toolbox.gdevice import VIDEO_DRIVER, GDevice, PixMap, deref, gdDevType, read_table
from macrt.toolbox.quickdraw import GrafPort
from macrt.toolbox.system import CLASSIC_SCREEN, RAM_SIZE
from macrt.toolbox.types import Rect

from macrt.demo import parse_args

from guest import Guest

GetMode, GetEntries, GetPages, GetBaseAddr, GetGray, GetGamma = 2, 3, 4, 5, 6, 8
SetMode, SetEntries, SetGamma, GrayPage, SetGray = 2, 3, 4, 5, 6


@pytest.fixture
def g(qapp):
    return Guest(model="ii")


def call(g, control: bool, code: int, record: bytes = bytes(12), refnum: int = VIDEO_DRIVER):
    """A Control or Status call to the driver, through the File Manager:
    its result and the record csParam points to, after."""
    rec = g.ptr(record)
    pb = g.ptr(bytes(50))
    g.mem.write(pb + 24, struct.pack(">hhL", refnum, code, rec))
    try:
        (glue_files._control if control else glue_files._status)(g, pb, False)
        err = 0
    except FileError as e:
        err = e.code
    return err, g.mem.read(rec, len(record))


def page_info(mode: int = 0, page: int = 0) -> bytes:
    return struct.pack(">HlhL", mode, 0, page, 0)


def main(g) -> tuple[GDevice, PixMap]:
    devices = g.system.devices
    gd = GDevice(g.mem, deref(g.mem, devices.main))
    return gd, PixMap(g.mem, deref(g.mem, gd.pmap))


def test_the_main_device_has_the_driver_and_its_modes(g):
    gd, _ = main(g)
    assert gd.ref_num == VIDEO_DRIVER
    err, rec = call(g, False, GetMode, page_info())
    mode, _, page, base = struct.unpack(">HlhL", rec)
    assert (err, mode, page, base) == (0, 0x83, 0, g.system.screen_bits.base)
    for mode in (0x80, 0x81, 0x82, 0x83):
        err, rec = call(g, False, GetPages, page_info(mode))
        assert (err, struct.unpack_from(">h", rec, 6)[0]) == (0, 1)
    assert call(g, False, GetPages, page_info(0x84))[0] == glue_video.statusErr   # no room
    assert call(g, False, GetBaseAddr, page_info(page=1))[0] == glue_video.statusErr


def test_set_mode_changes_the_screens_depth(g):
    port = GrafPort(g.mem, g.ptr(bytes(GrafPort.SIZE)))
    g("OpenCPort", port.addr)
    err, rec = call(g, True, SetMode, page_info(0x82))
    assert err == 0 and struct.unpack_from(">L", rec, 8)[0] == g.system.screen_bits.base
    system = g.system
    assert system.depth == 4 and system.devices.depth == 4
    assert system.screen_bits.row_bytes == 320 and g.mem.r16(lowmem.ScreenRow) == 320
    gd, pm = main(g)
    assert gd.mode == 0x82 and gd.flags >> gdDevType & 1
    assert pm.pixel_size == pm.cmp_size == 4 and pm.row_bytes & 0x3FFF == 320
    assert read_table(g.mem, pm.pm_table) == read_table(g.mem, g.handle(standard_table(4)))
    assert g.mem.r32(deref(g.mem, gd.i_table)) == g.mem.r32(deref(g.mem, pm.pm_table))
    screen = system.screen
    assert (screen.depth, screen.row_bytes, screen.bits.depth) == (4, 320, 4)
    mine = PixMap(g.mem, port.pixmap())                 # a port on the screen goes with it
    assert mine.pixel_size == 4 and mine.row_bytes == pm.row_bytes
    assert (port.fg_color, port.bk_color) == (15, 0)
    assert call(g, False, GetMode, page_info())[1][:2] == b"\x00\x82"

    call(g, True, SetMode, page_info(0x80))
    gd, pm = main(g)
    assert g.system.depth == 1 and not gd.flags >> gdDevType & 1
    assert read_table(g.mem, pm.pm_table) == [0xFFFFFFFF, 0xFF000000]
    assert call(g, True, SetMode, page_info(0x85))[0] == glue_video.controlErr
    assert g.system.depth == 1


def test_init_gdevice_sets_the_mode_through_the_driver(g):
    devices = g.system.devices
    g("InitGDevice", VIDEO_DRIVER, 0x81, devices.main)
    gd, pm = main(g)
    assert g.system.depth == 2 and gd.mode == 0x81 and pm.pixel_size == 2
    assert len(read_table(g.mem, pm.pm_table)) == 4


def test_what_a_program_writes_shows_at_the_new_depth(g):
    call(g, True, SetMode, page_info(0x82))
    screen, bits = g.system.screen, g.system.screen_bits
    g.mem.w8(bits.base + 100 * bits.row_bytes + 10, 0x3F)  # red, then black: pixels 20, 21
    screen.look()
    img = screen.bits.image(screen.bounds)
    table = g.system.devices.table()
    assert img.pixel(20, 100) & 0xFFFFFF == table[3] & 0xFFFFFF
    assert img.pixel(21, 100) & 0xFFFFFF == 0


def test_set_entries_and_get_entries(g):
    devices = g.system.devices
    seed = g.mem.r32(deref(g.mem, devices.pixmap(devices.main).pm_table))
    specs = g.ptr(struct.pack(">HHHH", 0, 0x1234, 0x5678, 0x9ABC))
    record = struct.pack(">Lhh", specs, 5, 0)
    assert call(g, True, SetEntries, record)[0] == 0
    assert devices.table()[5] == 0xFF12569A
    pm = devices.pixmap(devices.main)
    assert g.mem.r32(deref(g.mem, pm.pm_table)) == seed     # the seed stays
    out = g.ptr(bytes(8))
    assert call(g, False, GetEntries, struct.pack(">Lhh", out, 5, 0))[0] == 0
    assert g.mem.read(out, 8) == struct.pack(">HHHH", 5, 0x1234, 0x5678, 0x9ABC)

    assert call(g, True, SetGray, b"\x01")[0] == 0
    assert call(g, False, GetGray, bytes(1))[1] == b"\x01"
    call(g, True, SetEntries, record)
    r = devices.table()[5] >> 16 & 0xFF
    assert devices.table()[5] == 0xFF000000 | r << 16 | r << 8 | r
    assert call(g, True, SetEntries, struct.pack(">Lhh", specs, 300, 0))[0] == glue_video.paramErr


def test_get_gamma_and_set_gamma(g):
    """The driver's gamma table: linear to start with, a copy of the one
    SetGamma is given, and linear again for NIL."""
    err, rec = call(g, False, GetGamma, bytes(4))
    first = struct.unpack(">L", rec)[0]
    assert err == 0 and first
    assert g.mem.read(first, 12 + 256) == glue_video.LINEAR_GAMMA
    assert call(g, False, GetGamma, bytes(4))[1] == rec            # the same table again

    dim = struct.pack(">6h", 0, 0, 0, 1, 256, 8) + bytes(i // 2 for i in range(256))
    mine = g.ptr(dim)
    assert call(g, True, SetGamma, struct.pack(">L", mine))[0] == 0
    table = struct.unpack(">L", call(g, False, GetGamma, bytes(4))[1])[0]
    assert table != mine and g.mem.read(table, len(dim)) == dim    # a copy, the driver's own
    g.mem.write(mine, bytes(len(dim)))
    assert g.mem.read(table, len(dim)) == dim

    three = struct.pack(">6h", 0, 0, 2, 3, 4, 8) + b"\x01\x02" + bytes(range(12))
    assert call(g, True, SetGamma, struct.pack(">L", g.ptr(three)))[0] == 0
    table = struct.unpack(">L", call(g, False, GetGamma, bytes(4))[1])[0]
    assert g.mem.read(table, len(three)) == three

    for bad in ((1, 0, 0, 1, 256, 8), (0, 0, 0, 2, 256, 8), (0, 0, 0, 1, 0, 8),
                (0, 0, 0, 1, 256, 16)):
        record = struct.pack(">L", g.ptr(struct.pack(">6h", *bad) + bytes(512)))
        assert call(g, True, SetGamma, record)[0] == glue_video.controlErr
    assert call(g, True, SetGamma, bytes(4))[0] == 0
    table = struct.unpack(">L", call(g, False, GetGamma, bytes(4))[1])[0]
    assert g.mem.read(table, 12 + 256) == glue_video.LINEAR_GAMMA


def test_gray_page(g):
    call(g, True, SetMode, page_info(0x80))
    assert call(g, True, GrayPage, page_info())[0] == 0
    bits = g.system.screen_bits
    assert g.mem.read(bits.base, 2) == b"\x55\x55"
    assert g.mem.read(bits.base + bits.row_bytes, 2) == b"\xAA\xAA"


def test_has_depth_and_set_depth(g):
    main_gd = g.system.devices.main
    assert g("HasDepth", main_gd, 4, 0, 0) == 0x82
    assert g("HasDepth", main_gd, 16, 0, 0) == 0
    assert g("HasDepth", main_gd, 1, 1, 1) == 0     # not in colour
    assert g("SetDepth", main_gd, 4, 1, 0) == 0     # in grey
    _, pm = main(g)
    assert g.system.depth == 4
    assert read_table(g.mem, pm.pm_table) == read_table(g.mem, g.handle(standard_table(36)))
    assert g("SetDepth", main_gd, 32, 0, 0) == glue_video.cDepthErr


def test_restore_device_clut_puts_back_the_standard_colours(g):
    call(g, True, SetMode, page_info(0x82))
    devices = g.system.devices
    specs = g.ptr(struct.pack(">HHHH", 0, 0x1234, 0x5678, 0x9ABC))
    call(g, True, SetEntries, struct.pack(">Lhh", specs, 5, 0))
    g("RestoreDeviceClut", 0)
    _, pm = main(g)
    assert g.system.depth == 4 and devices.table()[5] != 0xFF12569A
    assert read_table(g.mem, pm.pm_table) == read_table(g.mem, g.handle(standard_table(4)))
    call(g, True, SetEntries, struct.pack(">Lhh", specs, 5, 0))
    g("RestoreDeviceClut", devices.main)
    assert read_table(g.mem, pm.pm_table) == read_table(g.mem, g.handle(standard_table(4)))


def test_a_plus_has_no_video_driver(qapp):
    g = Guest()
    assert call(g, False, GetMode, page_info())[0] == glue_files.unitEmptyErr


def test_the_system_file_says_how_the_screen_is_set_up(qapp):
    """'scrn' 0, as the Control Panel leaves it: the one screen, the main
    one and active, its mode, whether it's colour and its rectangle.
    JuliaSet 5.1 looks for the main screen's mode there."""
    for depth, mode, colour in ((8, 0x83, 1), (4, 0x82, 1), (1, 0x80, 0)):
        g = Guest(model="ii", depth=depth)
        data = g.contents(g.process.resources.GetResource("scrn", 0))
        assert struct.unpack(">HhhLhHHhh4hH", data) == (
            1, 0, 9, g.system.screen_bits.base, mode, 0x77FE, 0x8800 | colour, depth, -1,
            0, 0, 480, 640, 0)
    assert Guest().process.resources.GetResource("scrn", 0) == 0     # no Color QuickDraw


def test_the_standard_colour_tables_are_resources(qapp):
    """'clut' 1, 4 and 8, which RGetResource finds among a colour Mac's ROM
    resources: Easy Color Paint sets the screen's colours to 'clut' 4."""
    g = Guest(model="ii", depth=4)
    for n in (1, 4, 8):
        assert g.contents(g("RGetResource", b"clut", n)) == standard_table(n)
    assert Guest()("RGetResource", b"clut", 4) == 0                  # no Color QuickDraw


def test_an_old_port_on_the_screen_draws_at_its_depth(g):
    call(g, True, SetMode, page_info(0x82))
    port = GrafPort(g.mem, g.ptr(bytes(GrafPort.SIZE)))
    g("OpenPort", port.addr)
    bits = g.system.screen_bits
    below = g.mem.read(bits.base + 101 * bits.row_bytes, bits.row_bytes)
    g("PaintRect", g.rect_ptr(Rect(100, 0, 101, bits.bounds.width)))
    assert g.mem.read(bits.base + 100 * bits.row_bytes, bits.row_bytes) == b"\xFF" * 320
    assert g.mem.read(bits.base + 101 * bits.row_bytes, bits.row_bytes) == below


def test_a_screen_whose_rows_are_further_apart_than_they_need(qapp):
    g = Guest(model="ii", depth=8, row_bytes=1024)
    bits = g.system.screen_bits
    assert bits.row_bytes == 1024 and g.mem.r16(lowmem.ScreenRow) == 1024
    assert bits.base == g.mem.r32(lowmem.ScrnBase) == RAM_SIZE - 1024 * 480
    gd, pm = main(g)
    assert pm.row_bytes & 0x3FFF == 1024 and pm.base_addr == bits.base
    port = GrafPort(g.mem, g.ptr(bytes(GrafPort.SIZE)))
    g("OpenCPort", port.addr)
    g("PaintRect", g.rect_ptr(Rect(100, 0, 101, 640)))
    assert g.mem.read(bits.base + 100 * 1024, 1024) == b"\xFF" * 640 + bytes(384)

    # At another depth they're as far apart as the same pixels' bits are,
    # and a depth whose rows need more room than the screen has doesn't fit.
    assert call(g, False, GetPages, page_info(0x83))[0] == 0
    assert call(g, False, GetPages, page_info(0x84))[0] == glue_video.statusErr
    for mode, row_bytes in ((0x80, 128), (0x81, 256), (0x82, 512)):
        call(g, True, SetMode, page_info(mode))
        assert g.system.screen_bits.row_bytes == g.mem.r16(lowmem.ScreenRow) == row_bytes
        assert main(g)[1].row_bytes & 0x3FFF == g.system.screen.row_bytes == row_bytes
    assert call(g, True, SetMode, page_info(0x84))[0] == glue_video.controlErr


def test_a_screens_rows_are_as_far_apart_as_a_row_needs_at_least(qapp):
    system = Guest(model="ii", depth=8, row_bytes=650).system
    assert system.screen_bits.row_bytes == 650
    assert [system.row_bytes_for(d) for d in (1, 2, 4)] == [82, 164, 326]
    system = Guest(model="ii", depth=8, row_bytes=512).system
    assert system.screen_bits.row_bytes == 640 and system.row_bytes_for(1) == 80
    plus = Guest(screen_size=(512, 342), row_bytes=64).system
    assert plus.screen_bits.base == CLASSIC_SCREEN and plus.alternate_screen
    plus = Guest(screen_size=(512, 342), row_bytes=128).system
    assert plus.screen_bits.row_bytes == 128 and plus.alternate_screen is None
    for row_bytes in (0, 1023, 0x4000):
        with pytest.raises(ValueError):
            Guest(model="ii", row_bytes=row_bytes)


def test_row_bytes_option(capsys):
    assert parse_args(["--model", "ii", "--row-bytes", "1024", "x"]).row_bytes == 1024
    for bad in ("1023", "0", "16384"):
        with pytest.raises(SystemExit):
            parse_args(["--row-bytes", bad, "x"])
    assert "--row-bytes is an even number" in capsys.readouterr().err
