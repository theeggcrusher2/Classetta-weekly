"""A Macintosh II's colour screen: its graphics device, pixel map, colour
table and inverse table, what QuickDraw draws there, and the Color
Manager's calls."""

import random
import struct
import time

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage

from macrt.demo import parse_args
from macrt.m68k.glue_os import (QD32TRAP, SLOT_TRAPS, UNIMPLEMENTED, VIDEO_DEFAULT, Gestalt,
                                SysEnvirons, trap_address)
from macrt.toolbox import gdevice, lowmem, pixels
from macrt.toolbox.color import CT_DEVICE, STANDARD_8BIT, standard_table
from macrt.toolbox.gdevice import (ITAB_RES, PIXMAP_FLAG, GDevice, PixMap, allInit, apart,
                                   deref, gdDevType, mainScreen, make_itable, noDriver,
                                   read_table)
from macrt.toolbox.memmgr import noErr
from macrt.toolbox.types import Rect

from conftest import Recorder
from guest import Guest, Room

cNoMemErr, cRangeErr, cProtectErr = -152, -153, -154
REDCOLOR = 205                  # redColor (Inside Macintosh I-158)
WHITE, BLACK, RED = 0xFFFFFFFF, 0xFF000000, 0xFFFF0000


@pytest.fixture
def g(qapp):
    return Guest(model="ii")


def rgb(g, r, gr, b) -> int:
    return g.ptr(struct.pack(">HHH", r, gr, b))


def read_rgb(g, p) -> tuple:
    return struct.unpack(">HHH", g.mem.read(p, 6))


def specs(g, *entries) -> int:
    """A cSpecArray: (value, r, g, b) each."""
    return g.ptr(b"".join(struct.pack(">HHHH", *e) for e in entries))


def main_device(g):
    gdh = g.mem.r32(lowmem.MainDevice)
    gd = GDevice(g.mem, deref(g.mem, gdh))
    return gdh, gd, PixMap(g.mem, deref(g.mem, gd.pmap))


@pytest.mark.parametrize("depth", pixels.DEPTHS)
def test_pixels_at_each_depth_come_back_as_they_went(qapp, depth):
    if depth <= 8:
        table = read_table_bytes(standard_table({1: 1, 2: 34, 4: 4, 8: 8}[depth]))
        colors = table[:5] if depth > 1 else table
    else:
        table = None
        colors = [WHITE, BLACK, RED, 0xFF00FF00, 0xFF8C8C8C]       # each 5 bits exactly
    img = QImage(13, 3, QImage.Format.Format_RGB32)
    for y in range(3):
        for x in range(13):
            img.setPixel(x, y, colors[(x + y) % len(colors)])
    data, bpl = pixels.encode(img, depth, table)
    back = pixels.decode(data, 13, 3, bpl, depth, table)
    assert [[back.pixel(x, y) for x in range(13)] for y in range(3)] == \
        [[img.pixel(x, y) for x in range(13)] for y in range(3)]
    for c in colors:
        assert pixels.value_rgb(pixels.rgb_value(c, depth, table), depth, table) == c


def read_table_bytes(data: bytes) -> list[int]:
    """A ColorTable's colours by value, from its bytes."""
    size = struct.unpack_from(">h", data, 6)[0] + 1
    out = [0] * size
    for i in range(size):
        value, r, gr, b = struct.unpack_from(">HHHH", data, 8 + 8 * i)
        out[value] = 0xFF000000 | (r >> 8) << 16 | (gr >> 8) << 8 | b >> 8
    return out


def test_packed_pixels_are_each_values_low_bits():
    # the bytes Qt pads an 8-bit image's rows with are whatever was there
    assert pixels._pack(bytes([0x1F, 0x02, 0xFF]), 4) == b"\xF2\xF0"
    assert pixels._pack(bytes([0x07, 0x01, 0x02, 0xFF]), 2) == b"\xDB"


@pytest.mark.parametrize("depth", [2, 4])
def test_pixels_are_packed_and_unpacked_leftmost_first(depth):
    """_pack and _unpack against reading and writing each pixel's bits
    one at a time: the leftmost pixel in a byte's high bits, a value's
    bits above depth dropped, a part-filled last byte filled with 0s."""
    n, mask, rnd = 8 // depth, (1 << depth) - 1, random.Random(depth)
    for size in (0, 1, 2, 3, 5, 64, 1001):
        data = bytes(rnd.randrange(256) for _ in range(size))
        wide = bytes(b >> 8 - depth * (i + 1) & mask for b in data for i in range(n))
        assert pixels._unpack(data, depth) == wide
        assert pixels._pack(wide, depth) == data
        values = bytes(rnd.randrange(256) for _ in range(size))
        want = bytearray((len(values) + n - 1) // n)
        for i, v in enumerate(values):
            want[i // n] |= (v & mask) << 8 - depth * (i % n + 1)
        assert pixels._pack(values, depth) == pixels._pack(bytearray(values), depth) == want


@pytest.mark.parametrize("fmt", ["Format_RGB32", "Format_ARGB32", "Format_ARGB32_Premultiplied"])
def test_a_1_bit_image_has_each_pixel_the_nearer_of_white_and_black(qapp, fmt):
    """pixels.mono, for each grey, at any alpha, and for a colour Qt's own
    threshold would make the other: what matching each pixel to
    MONO_TABLE gives.  The rows are 13 pixels, not a whole number of bytes."""
    img = QImage(13, 20, getattr(QImage.Format, fmt))
    for i in range(13 * 20):
        v, a = i % 256, (255, 128, 0)[i % 3] if fmt != "Format_RGB32" else 255
        img.setPixel(i % 13, i // 13, a << 24 | v * 0x010101)
    matched = img.convertToFormat(QImage.Format.Format_Mono, pixels.MONO_TABLE,
                                  Qt.ImageConversionFlag.ThresholdDither)

    def bits(out):
        assert out.colorTable() == pixels.MONO_TABLE
        return [[out.pixelIndex(x, y) for x in range(13)] for y in range(20)]
    assert bits(pixels.mono(img)) == bits(matched)
    if fmt == "Format_RGB32":
        assert bits(pixels.mono(img))[9][10:13] == [1, 0, 0]        # 127, 128, 129
    img.setPixel(0, 0, 0xFF5A5AFF)          # nearer white, though its grey is 115
    assert bits(pixels.mono(img))[0][0] == 0
    # An indexed image's pixels are matched by their colours.
    inverse = img.convertToFormat(QImage.Format.Format_Mono, pixels.MONO_TABLE)
    inverse.invertPixels()
    inverse.setColorTable([BLACK, WHITE])
    assert bits(pixels.mono(inverse)) == bits(pixels.mono(img))
    # A 1-bit pixel map's pixels are these, unless its table says otherwise.

    def encoded(table):
        data, bpl = pixels.encode(img, 1, table)
        return bits(pixels.decode(data, 13, 20, bpl, 1).convertToFormat(
            QImage.Format.Format_Mono, pixels.MONO_TABLE))
    assert encoded(None) == encoded(pixels.MONO_TABLE) == bits(pixels.mono(img))
    assert [[1 - b for b in row] for row in encoded([BLACK, WHITE])] == bits(pixels.mono(img))


def test_direct_pixels_are_their_colours():
    assert pixels.rgb_value(0xFF123456, 32) == 0x123456
    assert pixels.rgb_value(RED, 16) == 0x7C00 and pixels.value_rgb(0x7C00, 16) == RED
    assert pixels.value_rgb(0x03E0, 16) == 0xFF00FF00


def test_a_mac_ii_has_the_main_screen_as_its_device(g):
    gdh, gd, pm = main_device(g)
    assert gdh and g.mem.r32(lowmem.DeviceList) == gdh and g.mem.r32(lowmem.TheGDevice) == gdh
    assert g("GetMainDevice") == g("GetDeviceList") == g("GetGDevice") == gdh
    assert g("GetNextDevice", gdh) == 0
    assert gd.flags & 0xFFFF == 0xBC01 and gd.mode == 0x83 and gd.res_pref == 4
    assert gd.rect == Rect(0, 0, 480, 640)
    bits = g.system.screen_bits
    assert pm.base_addr == bits.base == g.mem.r32(lowmem.ScrnBase)
    assert pm.row_bytes & 0xFFFF == PIXMAP_FLAG | 640 == PIXMAP_FLAG | bits.row_bytes
    assert bits.base + 640 * 480 <= 8 << 20
    assert (pm.pixel_size, pm.cmp_count, pm.cmp_size, pm.h_res) == (8, 1, 8, 72 << 16)
    assert pm.bounds == gd.rect

    ct = deref(g.mem, pm.pm_table)
    assert g.mem.r16(ct + 4) == CT_DEVICE and g.mem.r16(ct + 6) == 255
    assert read_table(g.mem, pm.pm_table) == STANDARD_8BIT
    itab = deref(g.mem, gd.i_table)
    assert g.mem.r32(itab) == g.mem.r32(ct) and g.mem.r16(itab + 4) == 4
    assert g.mem.r8(itab + 6) == 255 and g.mem.r8(itab + 6 + 4095) == 0   # black, white
    itab = make_itable(STANDARD_8BIT, 7, 3)                     # 3 bits a component
    assert itab[:6] == struct.pack(">lH", 7, 3) and len(itab) == 6 + 512
    assert (itab[6 + 0o777], itab[6 + 0o000], itab[6 + 0o700]) == (0, 255, STANDARD_8BIT.index(RED))


def test_the_application_starts_out_drawing_on_the_main_screen(harness):
    from test_m68k_apps import launch_hello
    h = harness(model="ii")
    proc, _ = launch_hello(h)
    t = proc.runner
    with proc.system.lock:
        assert t.mem.r16(lowmem.ROM85) == 0x3FFF
        assert t.mem.r32(lowmem.TheGDevice) == t.mem.r32(lowmem.MainDevice) != 0


def test_32_bit_quickdraw_s_trap_is_there_only_on_a_colour_machine(harness):
    """Pico looks for 32-Bit QuickDraw by asking whether QD32Trap is
    implemented."""
    from test_m68k_apps import launch_hello
    for model, there in (("ii", True), ("plus", False)):
        proc, _ = launch_hello(harness(model=model))
        t = proc.runner
        with proc.system.lock:
            assert (trap_address(t, QD32TRAP) != trap_address(t, UNIMPLEMENTED)) == there


def test_only_a_mac_ii_has_slots_for_slot_vbl_tasks_and_a_default_video_card(harness):
    """NetTrek asks whether SlotVInstall is implemented, and if it is,
    calls GetVideoDefault for the slot to give it."""
    from test_m68k_apps import launch_hello
    for model, there in (("ii", True), ("plus", False)):
        proc, _ = launch_hello(harness(model=model))
        t = proc.runner
        with proc.system.lock:
            for word in SLOT_TRAPS:
                assert (trap_address(t, word) != trap_address(t, UNIMPLEMENTED)) == there


def test_the_default_video_card_is_the_screen_s_until_another_is_set(g):
    rec = g.ptr(bytes(2))
    g("GetVideoDefault", rec)
    assert g.mem.read(rec, 2) == bytes(VIDEO_DEFAULT)
    g.mem.write(rec, b"\x0a\x81")
    g("SetVideoDefault", rec)
    g.mem.write(rec, bytes(2))
    g("GetVideoDefault", rec)
    assert g.mem.read(rec, 2) == b"\x0a\x81"


def test_a_mac_plus_has_no_devices(qapp):
    g = Guest()
    assert g.system.devices is None
    assert g("GetMainDevice") == g("GetGDevice") == g("GetDeviceList") == 0
    assert g("NewGDevice", 0, 0x83) == 0
    assert g("GetMaxDevice", g.rect_ptr((0, 0, 10, 10))) == 0
    p = rgb(g, 0xFFFF, 0, 0x1234)
    g("InvertColor", p)
    assert read_rgb(g, p) == (0xFFFF, 0, 0x1234)
    g("SetEntries", 0, 0, specs(g, (0, 0, 0, 0)))               # no colour table to change
    assert g("QDError") == cRangeErr
    g("ReserveEntry", 0, True)
    assert g("QDError") == cRangeErr
    g("MakeITable", 0, 0, 0)
    assert g("QDError") == cNoMemErr


def test_gestalt_and_sysenvirons_say_whether_there_is_color_quickdraw(g, qapp):
    # 'qd  ', gestalt32BitQD13: System 7's Color QuickDraw
    assert Gestalt(g, 0x71642020) == (noErr, 0x230)
    assert Gestalt(g, 0x71647277) == (noErr, 7)                # 'qdrw'
    p = g.ptr(bytes(16))
    assert SysEnvirons(g, 2, p) == noErr
    assert g.mem.r8(p + 9) == 1                                 # hasColorQD
    plus = Guest()
    assert Gestalt(plus, 0x71642020) == (noErr, 0)
    assert Gestalt(plus, 0x71647277) == (noErr, 0)
    p = plus.ptr(bytes(16))
    SysEnvirons(plus, 2, p)
    assert plus.mem.r8(p + 9) == 0


def test_device_loop_calls_its_procedure_for_the_screen(g):
    """BMAPEdit draws its alert's default button through DeviceLoop: the
    procedure is called once, for the main screen at its depth, unless
    the region is empty and allDevices isn't set; on a Plus, for a
    1-bit screen with no device."""
    gdh, gd, pm = main_device(g)
    seen = []
    g.procs[0x50000] = lambda *args: seen.append(args)
    rgn = g("NewRgn")
    g("DeviceLoop", rgn, 0x50000, 1234, 0)
    g("DeviceLoop", rgn, 0, 1234, 4)
    assert seen == []
    g("DeviceLoop", rgn, 0x50000, 1234, 4)                    # allDevices
    g("SetRectRgn", rgn, 0, 0, 10, 10)
    g("DeviceLoop", rgn, 0x50000, 99, 0)
    assert seen == [(pm.pixel_size, gd.flags, gdh, 1234), (pm.pixel_size, gd.flags, gdh, 99)]
    plus = Guest()
    plus.procs[0x50000] = lambda *args: seen.append(args)
    plus("DeviceLoop", 0, 0x50000, 7, 0)
    assert seen[-1] == (1, 0, 0, 7)


def test_the_device_calls(g):
    gdh, gd, _ = main_device(g)
    assert g("TestDeviceAttribute", gdh, mainScreen) and not g("TestDeviceAttribute", gdh, noDriver)
    g("SetDeviceAttribute", gdh, noDriver, True)
    assert g("TestDeviceAttribute", gdh, noDriver)
    g("SetDeviceAttribute", gdh, noDriver, False)
    assert gd.flags & 0xFFFF == 0xBC01

    assert g("GetMaxDevice", g.rect_ptr((100, 100, 200, 200))) == gdh
    assert g("GetMaxDevice", g.rect_ptr((500, 0, 600, 100))) == 0

    other = g("NewGDevice", 7, 0x82)                            # 4 bits a pixel
    o = GDevice(g.mem, deref(g.mem, other))
    pm = PixMap(g.mem, deref(g.mem, o.pmap))
    assert (o.ref_num, o.mode, o.flags) == (7, 0x82, 1 << gdDevType)
    assert pm.pixel_size == 4 and len(read_table(g.mem, pm.pm_table)) == 16
    assert g("GetNextDevice", gdh) == 0                         # not in the list
    g("SetGDevice", other)
    assert g("GetGDevice") == other
    assert g("Color2Index", rgb(g, 0, 0, 0)) == 15
    g("InitGDevice", 7, 0x83, other)
    assert o.mode == 0x83
    g("SetGDevice", gdh)
    handles = (other, o.pmap, o.i_table, pm.pm_table)
    g("DisposeGDevice", other)
    assert not any(g.system.sys_heap.is_handle(h) for h in handles)
    g("DisposeGDevice", gdh)                                    # never the screen's
    assert g.system.sys_heap.is_handle(gdh)
    assert not GDevice(g.mem, deref(g.mem, g("NewGDevice", 0, -1))).flags & (1 << allInit)


def test_colours_and_indexes_are_the_devices(g):
    assert g("Color2Index", rgb(g, 0xFFFF, 0xFFFF, 0xFFFF)) == 0
    assert g("Color2Index", rgb(g, 0, 0, 0)) == 255
    assert g("Color2Index", rgb(g, 0xFFFF, 0, 0)) == STANDARD_8BIT.index(RED)
    out = g.ptr(bytes(6))
    g("Index2Color", 35, out)
    assert read_rgb(g, out) == (0xFFFF, 0, 0)
    assert g("RealColor", rgb(g, 0xFFFF, 0, 0))
    assert not g("RealColor", rgb(g, 0x1234, 0x5678, 0x9ABC))
    for mode in (0x84, 0x85):                                   # 16 and 32 bits: every colour
        g("SetGDevice", g("NewGDevice", 0, mode))
        assert g("RealColor", rgb(g, 0x1234, 0x5678, 0x9ABC))
    g("SetGDevice", g("GetMainDevice"))
    p = rgb(g, 0xFFFF, 0, 0x1234)
    g("InvertColor", p)
    assert read_rgb(g, p) == (0, 0xFFFF, 0xEDCB)


def test_search_procedures_are_asked_a_colour_first_until_they_are_deleted(g):
    """The latest added is asked first, and one that finds nothing passes
    the colour on, to the next and then to the inverse table."""
    asked = []

    def greys(p, pos):
        asked.append("greys")
        r, gr, b = read_rgb(g, p)
        if r == gr == b:
            g.mem.w32(pos, 42)
            return True
        return False

    def nothing(p, pos):
        asked.append("nothing")
        return False

    g.procs = {0x1000: greys, 0x2000: nothing}
    g("AddSearch", 0x1000)
    g("AddSearch", 0x2000)
    assert g("Color2Index", rgb(g, 0x8000, 0x8000, 0x8000)) == 42
    assert asked == ["nothing", "greys"]
    g("SetGDevice", g("NewGDevice", 0, 0x83))                    # another device's: none
    assert g("Color2Index", rgb(g, 0x8000, 0x8000, 0x8000)) != 42 and len(asked) == 2
    assert g("Color2Index", rgb(g, 0xFFFF, 0, 0)) == STANDARD_8BIT.index(RED)
    g("SetGDevice", g("GetMainDevice"))
    assert g("Color2Index", rgb(g, 0xFFFF, 0, 0)) == STANDARD_8BIT.index(RED)
    for proc in (0x1000, 0x2000):
        g("DelSearch", proc)
    asked.clear()
    assert g("Color2Index", rgb(g, 0x8000, 0x8000, 0x8000)) != 42
    assert not asked and g.tb.qd.search is None


def test_set_entries_changes_the_screens_colours(g):
    gdh, gd, pm = main_device(g)
    ct = deref(g.mem, pm.pm_table)
    seed = g.mem.r32(ct)
    assert g("GetCTSeed") > seed
    blue = STANDARD_8BIT.index(0xFF0000FF)
    assert g.system.devices.nearest(0xFF0000F8) == 0xFF0000FF

    g("SetEntries", 1, 0, specs(g, (0, 0, 0, 0xF800)))          # entry 1 a blue, by place
    assert g("QDError") == noErr
    assert read_table(g.mem, pm.pm_table)[1] == 0xFF0000F8
    assert g.mem.r32(ct) > seed and g.mem.r32(deref(g.mem, gd.i_table)) == g.mem.r32(ct)
    assert g.system.devices.nearest(0xFF0000F8) == 0xFF0000F8
    g("SetEntries", -1, 0, specs(g, (blue, 0x1111, 0x1111, 0x1111)))    # by value
    assert read_table(g.mem, pm.pm_table)[blue] == 0xFF111111
    g("SetEntries", 255, 1, specs(g, (0, 0, 0, 0), (0, 0, 0, 0)))     # past the end
    assert g("QDError") == cRangeErr

    saved = g.handle(bytes(8))
    sel = g.ptr(struct.pack(">hhh", 1, 1, blue))                # two entries: 1 and blue
    g("SaveEntries", 0, saved, sel)
    assert g("QDError") == noErr
    sp = deref(g.mem, saved)
    assert g.mem.r16s(sp + 6) == 1
    assert struct.unpack(">HHHH", g.mem.read(sp + 8, 8)) == (1, 0, 0, 0xF800)
    g("SetEntries", 1, 0, specs(g, (0, 0xFFFF, 0, 0)))
    seed = g.mem.r32(ct)
    g("RestoreEntries", saved, 0, sel)
    assert read_table(g.mem, pm.pm_table)[1] == 0xFF0000F8 and g.mem.r32(ct) == seed
    assert g.system.devices.nearest(0xFF0000F8) == 0xFF0000F8


def test_save_entries_into_what_is_not_a_handle_saves_nothing(g):
    """Giffer 1.0 passes the address of its handle variable as resultTable:
    SaveEntries can't make room there, and writes nothing over it."""
    gdh, gd, pm = main_device(g)
    table = g.handle(bytes(8 + 8 * 256))
    var = g.process.partition.a5 - 8               # one of its globals
    g.mem.w32(var, table)
    before = g.mem.read(table & 0xFFFFFF, 16)
    sel = g.ptr(struct.pack(">hhh", 1, 0, 1))
    g("SaveEntries", 0, var, sel)
    assert g("QDError") == cNoMemErr
    assert g.mem.r32(var) == table and g.mem.read(table & 0xFFFFFF, 16) == before


def test_save_and_restore_entries_say_which_they_could_not(g, monkeypatch):
    """Of the entries selection names, one not in the table is left out,
    and -1 put in its place in selection; the others are copied."""
    gdh, gd, pm = main_device(g)
    table = read_table(g.mem, pm.pm_table)
    saved = g.handle(bytes(8))
    sel = g.ptr(struct.pack(">hhhh", 2, 1, 256, 3))
    g("SaveEntries", 0, saved, sel)
    assert g("QDError") == cRangeErr
    assert struct.unpack(">hhhh", g.mem.read(sel, 8)) == (2, 1, -1, 3)
    sp = deref(g.mem, saved)
    for i, at in ((0, 1), (2, 3)):
        value, *color = struct.unpack(">HHHH", g.mem.read(sp + 8 + 8 * i, 8))
        assert value == at and argb_of(color) == table[at]
    full = g.handle(b"\xAA" * 32)                     # the place of one it left out is kept
    g("SaveEntries", 0, full, sel)
    assert g("QDError") == cRangeErr
    kept, got = g.contents(full), g.contents(saved)
    assert kept[16:24] == b"\xAA" * 8 and kept[24:] == got[24:]

    g("SetEntries", 1, 2, specs(g, *[(0, 0x1111, 0x2222, 0x3333)] * 3))
    sel = g.ptr(struct.pack(">hhhh", 2, 1, -5, 3))
    g("RestoreEntries", saved, 0, sel)
    assert g("QDError") == cRangeErr
    assert struct.unpack(">hhhh", g.mem.read(sel, 8)) == (2, 1, -1, 3)
    now = read_table(g.mem, pm.pm_table)
    assert now[1] == table[1] and now[3] == table[3] and now[2] == 0xFF112233

    empty = g.handle(b"")
    g("EmptyHandle", empty)
    sel = g.ptr(struct.pack(">hh", 0, 1))
    for call in (("SaveEntries", empty, saved, sel), ("SaveEntries", 0, empty, sel),
                 ("RestoreEntries", empty, 0, sel), ("RestoreEntries", saved, empty, sel)):
        kept = g.contents(saved)
        g(*call)
        assert g("QDError") == cRangeErr
        assert g.mem.read(sel, 4) == struct.pack(">hh", 0, 1) and g.contents(saved) == kept
    small = g.handle(bytes(8))
    Room(monkeypatch, g).limit(0)
    g("SaveEntries", 0, small, sel)
    assert g("QDError") == cNoMemErr and g.contents(small) == bytes(8)


def argb_of(color) -> int:
    r, gr, b = color
    return 0xFF000000 | (r >> 8) << 16 | (gr >> 8) << 8 | b >> 8


def test_a_protected_entry_is_kept_from_set_entries(g):
    gdh, gd, pm = main_device(g)
    g("ProtectEntry", 3, True)
    assert g("QDError") == noErr
    g("ProtectEntry", 3, True)
    assert g("QDError") == cProtectErr                  # already
    was = read_table(g.mem, pm.pm_table)
    g("SetEntries", 2, 1, specs(g, (0, 0, 0, 0xFFFF), (0, 0, 0, 0xFFFF)))
    assert g("QDError") == cProtectErr and read_table(g.mem, pm.pm_table) == was
    g("ProtectEntry", 3, False)
    g("SetEntries", 2, 1, specs(g, (0, 0, 0, 0xFFFF), (0, 0, 0, 0xFFFF)))
    assert g("QDError") == noErr and read_table(g.mem, pm.pm_table)[3] == 0xFF0000FF
    g("ProtectEntry", 256, True)
    assert g("QDError") == cRangeErr


def test_a_reserved_entry_is_no_match_for_a_colour(g):
    gdh, gd, pm = main_device(g)
    red = rgb(g, 0xFFFF, 0, 0)
    index = g("Color2Index", red)
    assert read_table(g.mem, pm.pm_table)[index] == RED
    ct = deref(g.mem, pm.pm_table)
    seed = g.mem.r32(ct)
    g("SetClientID", 7)
    assert gd.id == 7
    g("ReserveEntry", index, True)
    assert g("QDError") == noErr and g.mem.r32(ct) != seed
    assert g.mem.r16(ct + 8 + 8 * index) & 0x40FF == 0x4007
    assert g("Color2Index", red) != index
    g("ReserveEntry", index, True)
    assert g("QDError") == cProtectErr                  # already
    g("SetEntries", index, 0, specs(g, (0, 0xFFFF, 0, 0)))      # its own gdID's change
    assert g("QDError") == noErr
    g("SetClientID", 8)
    g("SetEntries", index, 0, specs(g, (0, 0, 0, 0)))           # another's doesn't
    assert g("QDError") == cProtectErr
    assert read_table(g.mem, pm.pm_table)[index] == RED
    g("ReserveEntry", index, False)
    assert g("Color2Index", red) == index and not g.mem.r16(ct + 8 + 8 * index) & 0x4000


def test_set_client_id_sets_the_current_devices_gdid_only(g):
    gdh, gd, pm = main_device(g)
    other = g("NewGDevice", 0, 0x83)
    g("SetGDevice", other)
    g("SetClientID", 9)
    assert GDevice(g.mem, deref(g.mem, other)).id == 9 and gd.id != 9


def test_reserving_entry_after_entry_shows_the_screen_once_a_tick(g, monkeypatch):
    """Each reservation is a new seed and a new inverse table, but the same
    colours: the table's indexes are made once.  The screen is shown in
    its new colours at the next tick, not at each one."""
    gdh, gd, pm = main_device(g)
    screen, looked = g.system.screen, []
    monkeypatch.setattr(type(screen), "look", lambda self: looked.append(1))
    monkeypatch.setattr(screen, "_last", time.monotonic())
    made = gdevice._itable_indexes.cache_info().misses
    seeds = set()
    for index in range(1, 40):
        g("ReserveEntry", index, True)
        seeds.add(g.mem.r32(deref(g.mem, gd.i_table)))
    assert len(seeds) == 39 and gdevice._itable_indexes.cache_info().misses <= made + 1
    assert len(looked) <= 1
    monkeypatch.setattr(screen, "_last", time.monotonic() - 1)
    g("ReserveEntry", 40, True)
    assert looked


def test_a_window_draws_in_colours_not_yet_shown(harness):
    """QuickDraw matches what it draws in a window to the screen's colours
    as they are, shown yet or not: an entry reserved a moment before is no
    match."""
    h = harness(model="ii", depth=8)
    rec = Recorder("Colour")
    h.launch(rec)
    system = h.d.system
    bits, devices = system.screen_bits, system.devices
    w = rec.tb.FrontWindow()
    c = w.content
    at = bits.base + (c.top + 12) * bits.row_bytes + c.left
    with system.lock:
        rec.tb.SetPort(w)
        w.fg_color = REDCOLOR
        rec.tb.PaintRect(Rect(10, 20, 14, 36))
        red = system.mem.r8(at + 25)
        devices.held = devices.held | {red}
        devices.changed()
        rec.tb.PaintRect(Rect(10, 40, 14, 56))
        assert system.mem.r8(at + 45) != red
    devices.held = devices.held - {red}


def test_entries_alike_are_told_apart():
    """Of entries alike, the first is kept as it is and each other is
    made unlike any, by as little as it takes; one reserved for
    animation is the one changed, and is changed even if none is like it."""
    black, white = 0xFF000000, 0xFFFFFFFF
    assert apart([white, black], set()) == [white, black]
    out = apart([black] * 4, set())
    assert out[0] == black and len(set(out)) == 4
    assert all(c >> s & 0xFF <= 1 for c in out for s in (0, 8, 16))    # a step at most
    out = apart([black, black, white, 0xFF000001], {0})
    assert out[1:] == [black, white, 0xFF000001] and out[0] not in out[1:]
    assert len(set(apart([black] * 256, set()))) == 256
    out = apart([white, 0xFF102030, black], {1, 7})
    assert out[0::2] == [white, black] and out[1] not in (0xFF102030, white, black)
    assert all(abs((out[1] >> s & 0xFF) - (0x102030 >> s & 0xFF)) <= 1 for s in (0, 8, 16))


def _values(encoded: tuple[bytes, int], depth: int, img: QImage) -> list[list[int]]:
    """Each row's pixel values, from pixels.encode's bytes for img."""
    data, bpl = encoded
    return [[data[y * bpl + x * depth // 8] >> (4 - x % 2 * 4 if depth == 4 else 0)
             & (1 << depth) - 1 for x in range(img.width())] for y in range(img.height())]


def test_a_colour_is_written_as_a_reserved_value_only_if_it_is_that_colour(qapp):
    """pixels.encode with a match table: a pixel exactly the colour of a
    value the match table leaves out is written as that value, and one
    that's only near it as the nearest of match's (rows padded or not,
    at 8 bits and packed at 4, and rows of neither, or of only the
    exact, above, between and below).  Only the pixels are compared: Qt
    leaves a row's padding as it was."""
    white, black, blue, near = 0xFFFFFFFF, 0xFF000000, 0xFF1020E0, 0xFF1121DD
    table = [white, 0xFF0000FF, blue, black]
    match = [white, 0xFF0000FF, white, black]
    for width in (4, 5):
        row = [blue, near, white, black, blue][:width]
        rows = [[white, black] * 3, row, [blue] * 5, row[::-1], [blue] * 5]
        img = QImage(width, len(rows), QImage.Format.Format_RGB32)
        for y, cs in enumerate(rows):
            for x in range(width):
                img.setPixel(x, y, cs[x])
        want = [[0, 3, 0, 3, 0][:width], [2, 1, 0, 3, 2][:width], [2] * width]
        want[3:] = [want[1][::-1], [2] * width]
        for depth in (8, 4):
            assert _values(pixels.encode(img, depth, table, match), depth, img) == want
            assert (_values(pixels.encode(img, depth, table, table), depth, img)
                    == _values(pixels.encode(img, depth, table), depth, img))
        plain, bpl = pixels.encode(img, 8, table)
        assert plain[bpl + 1] == 2      # with none, near is blue's
        # An RGB32 image's fourth byte isn't its colour's (one read from a 32-bit PixMap's
        # pixels has whatever the program left in it).
        zeroed = bytes(b if i % 4 != 3 else 0 for i, b in enumerate(pixels.raw(img)))
        bare = QImage(zeroed, width, len(rows), 4 * width, QImage.Format.Format_RGB32)
        assert _values(pixels.encode(bare, 8, table, match), 8, img) == want


def test_make_itable(g, monkeypatch):
    gdh, gd, pm = main_device(g)
    g("MakeITable", 0, 0, 5)
    assert g("QDError") == noErr
    itab = deref(g.mem, gd.i_table)
    assert g.mem.r16(itab + 4) == 5 and g.system.sys_heap.handle_size(gd.i_table) == 6 + 32768
    g("MakeITable", 0, 0, 7)
    assert g("QDError") == cRangeErr
    for pref, res in ((3, 3), (0, ITAB_RES)):                   # res 0: the device's
        gd.res_pref = pref
        g("MakeITable", 0, 0, 0)
        assert g("QDError") == noErr and g.mem.r16(deref(g.mem, gd.i_table) + 4) == res

    empty, it_h = g.handle(b""), g.handle(b"")
    g("EmptyHandle", empty)
    g("MakeITable", empty, 0, 4)                                # no colour table
    assert g("QDError") == cNoMemErr
    Room(monkeypatch, g).limit(0)
    g("MakeITable", 0, it_h, 4)                                 # no room for the table
    assert g("QDError") == cNoMemErr and g.heap.handle_size(it_h) == 0


def test_quickdraw_draws_the_nearest_colour_in_the_screen_buffer(harness):
    h = harness(model="ii", depth=8)
    rec = Recorder("Colour")
    h.launch(rec)
    system = h.d.system
    bits, table = system.screen_bits, system.devices.table()
    w = rec.tb.FrontWindow()
    with system.lock:
        rec.tb.SetPort(w)
        w.fg_color = REDCOLOR
        rec.tb.PaintRect(Rect(10, 20, 14, 36))
    c = w.content
    red = system.devices.nearest(0xFFDD0806)
    assert red not in (WHITE, BLACK)
    at = bits.base + (c.top + 12) * bits.row_bytes
    assert all(table[system.mem.r8(at + c.left + x)] == red for x in range(20, 36))
    assert table[system.mem.r8(at + c.left + 19)] == WHITE
    with w.backing.lock:
        assert w.backing.image.pixel(25, 12) | 0xFF000000 == red

    # A program's pixel shows in the window as the colour table has it,
    # and again, as the table has it then, when its colours change.
    at = bits.base + (c.top + 30) * bits.row_bytes + c.left + 40
    green = STANDARD_8BIT.index(0xFF00FF00)
    with system.lock:
        system.mem.w8(at, green)
        system.screen.look()
    with w.backing.lock:
        assert w.backing.image.pixel(40, 30) | 0xFF000000 == 0xFF00FF00
    with system.lock:
        pm = system.devices.pixmap(system.devices.main)
        ct = deref(system.mem, pm.pm_table)
        system.mem.write(ct + 8 + 8 * green + 2, struct.pack(">HHH", 0x8888, 0, 0x8888))
        system.mem.w32(ct, system.devices.next_seed())
        system.screen.look()
    with w.backing.lock:
        assert w.backing.image.pixel(40, 30) | 0xFF000000 == 0xFF880088


def test_depth_needs_a_colour_model(capsys):
    assert parse_args(["--model", "ii", "--depth", "4", "x"]).depth == 4
    assert parse_args(["--depth", "1", "x"]).depth == 1
    with pytest.raises(SystemExit):
        parse_args(["--depth", "8", "x"])
    assert "depth needs a model with a colour screen" in capsys.readouterr().err
