"""Offscreen graphics worlds: 32-Bit QuickDraw's GWorlds (Inside Macintosh
VI-21)."""

import itertools
import struct

import pytest

from macrt.m68k.glue_gworld import (GWORLD_VERSION, cDepthErr, clipPix, cNoMemErr, gwFlagErr,
                                    mapPix, newDepth, newRowBytes, noNewDevice, paramErr,
                                    pixelsLocked, pixelsPurgeable, reallocPix, stretchPix,
                                    useTempMem)
from macrt.resfork import Resource
from macrt.toolbox import lowmem
from macrt.toolbox.color import color_table, standard_table
from macrt.toolbox.gdevice import GDevice, PixMap, deref, read_table
from macrt.toolbox.quickdraw import GrafPort
from macrt.toolbox.types import Rect

from guest import Guest, Processor, Room

WHITE, BLACK = (0xFFFF, 0xFFFF, 0xFFFF), (0, 0, 0)
RED, GREEN = (0xFFFF, 0, 0), (0, 0xFFFF, 0)


@pytest.fixture
def g(qapp):
    return Guest(model="ii")


def rgb(g, *c) -> int:
    return g.ptr(struct.pack(">HHH", *c))


def new_gworld(g, depth: int, r: Rect, table: int = 0, flags: int = 0) -> int:
    out = g.ptr(bytes(4))
    assert g("NewGWorld", out, depth, g.rect_ptr(r), table, 0, flags) == 0
    return g.mem.r32(out)


def pixmap(g, gw: int) -> PixMap:
    return PixMap(g.mem, deref(g.mem, g("GetGWorldPixMap", gw)))


def values(g, gw: int) -> list[bytes]:
    """A world's 8-bit pixel values, a row at a time."""
    pm = pixmap(g, gw)
    rb = pm.row_bytes & 0x3FFF
    return [g.mem.read(pm.base_addr + v * rb, pm.bounds.width) for v in range(pm.bounds.height)]


def cport(g) -> GrafPort:
    """A new cGrafPort onto the screen, the current port."""
    p = g.ptr(bytes(GrafPort.SIZE))
    g("OpenCPort", p)
    return g.system.port_at(p)


def screen_colors(g, rect: Rect) -> list[list[int]]:
    bits, table = g.system.screen_bits, g.system.devices.table()
    return [[table[g.mem.r8(bits.base + v * bits.row_bytes + h)]
             for h in range(rect.left, rect.right)] for v in range(rect.top, rect.bottom)]


def current(g) -> tuple[int, int]:
    port, device = g.ptr(bytes(4)), g.ptr(bytes(4))
    g("GetGWorld", port, device)
    return g.mem.r32(port), g.mem.r32(device)


def test_new_gworld_makes_a_colour_port_with_pixels_and_a_device_of_its_own(g):
    before = current(g)
    r = Rect(10, 20, 15, 30)
    gw = new_gworld(g, 8, r)
    assert current(g) == before                 # neither the port nor the device changes
    port = g.system.port_at(gw)
    assert port.is_color and port.port_version & 0xFFFF == GWORLD_VERSION
    assert port.port_rect == r
    pm = pixmap(g, gw)
    assert pm.bounds == r and pm.pixel_size == 8 and pm.row_bytes & 0x3FFF == 12
    assert values(g, gw) == [bytes(10)] * 5     # white
    assert read_table(g.mem, pm.pm_table) == g.system.devices.table()
    device = g("GetGWorldDevice", gw)
    assert device and device != g.system.devices.main
    gd = GDevice(g.mem, deref(g.mem, device))
    assert gd.pmap == port.port_pix_map and gd.rect == r


def test_a_gworld_draws_in_its_own_colours_and_copies_to_the_screen(g):
    screen = cport(g)
    table = g.handle(color_table([WHITE, RED, GREEN, BLACK], seed=7))
    gw = new_gworld(g, 8, Rect(0, 0, 2, 4), table)
    saved = current(g)
    assert saved == (screen.addr, g.system.devices.main)
    g("SetGWorld", gw, 0)
    assert current(g) == (gw, g("GetGWorldDevice", gw))
    g("RGBForeColor", rgb(g, *GREEN))
    assert g.system.port_at(gw).fg_color == 2   # its table's green, not the screen's
    g("PaintRect", g.rect_ptr(Rect(0, 1, 2, 3)))
    assert values(g, gw) == [bytes([0, 2, 2, 0])] * 2
    g("SetGWorld", *saved)
    assert current(g) == saved

    g("CopyBits", deref(g.mem, g("GetGWorldPixMap", gw)), screen.addr + 2,
      g.rect_ptr(Rect(0, 0, 2, 4)), g.rect_ptr(Rect(40, 50, 42, 54)), 0, 0)
    green = g.system.devices.nearest(0xFF00FF00)
    assert screen_colors(g, Rect(40, 50, 42, 54)) == [[0xFFFFFFFF, green, green, 0xFFFFFFFF]] * 2


def test_set_gworld_without_a_device_gives_a_port_on_the_screen_the_main_one(g):
    """FloorTiles goes back to its window from a GWorld with SetGWorld(window,
    NIL), then makes a colour dialog, whose PixMap is the current device's."""
    screen = cport(g)
    gw = new_gworld(g, 8, Rect(0, 0, 4, 4))
    g("SetGWorld", gw, 0)
    g("SetGWorld", screen.addr, 0)
    assert current(g) == (screen.addr, g.system.devices.main)
    # A port whose pixels are elsewhere keeps the device there is.
    g("SetGWorld", gw, 0)
    offscreen = g.ptr(bytes(GrafPort.SIZE))
    g("OpenPort", offscreen)
    g("SetPortBits", g.ptr(struct.pack(">LH4h", g.ptr(bytes(8)), 2, 0, 0, 4, 16)))
    g("SetGWorld", offscreen, 0)
    assert current(g) == (offscreen, g("GetGWorldDevice", gw))


@pytest.mark.parametrize("mode, op", [
    (1, lambda d, s: d | s), (2, lambda d, s: d ^ s), (3, lambda d, s: d & ~s),
    (4, lambda d, s: ~s), (5, lambda d, s: d | ~s), (6, lambda d, s: d ^ ~s),
    (7, lambda d, s: d & s)])
def test_copy_bits_in_a_source_mode_combines_pixel_values_bit_by_bit(g, mode, op):
    """In black and white, each mode applies the foreground colour (all 1's)
    and the background colour (all 0's) to a source's 1's and 0's (Inside
    Macintosh V-57): taken bit by bit, the values' Or, Xor, Bic and so on,
    as FloorTiles masks its coloured tiles offscreen."""
    r = Rect(0, 0, 1, 4)
    src, dst = new_gworld(g, 8, r), new_gworld(g, 8, r)
    s, d = bytes([0x0F, 0x3C, 0x00, 0xFF]), bytes([0x5A, 0xF0, 0x81, 0x18])
    g.mem.write(pixmap(g, src).base_addr, s)
    g.mem.write(pixmap(g, dst).base_addr, d)
    saved = current(g)
    g("SetGWorld", dst, 0)
    g("CopyBits", deref(g.mem, g("GetGWorldPixMap", src)),
      deref(g.mem, g("GetGWorldPixMap", dst)), g.rect_ptr(r), g.rect_ptr(r), mode, 0)
    g("SetGWorld", *saved)
    assert values(g, dst) == [bytes(op(a, b) & 0xFF for a, b in zip(d, s))]


def test_the_pixels_state(g):
    gw = new_gworld(g, 8, Rect(0, 0, 4, 4))
    pm = g("GetGWorldPixMap", gw)
    assert g("GetPixelsState", pm) == 0
    assert g("LockPixels", pm) is True
    assert g("GetPixelsState", pm) == pixelsLocked
    g("AllowPurgePixels", pm)
    g("UnlockPixels", pm)
    assert g("GetPixelsState", pm) == pixelsPurgeable
    g("SetPixelsState", pm, pixelsLocked)
    assert g("GetPixelsState", pm) == pixelsLocked
    assert g("GetPixBaseAddr", pm) == pixmap(g, gw).base_addr
    assert g("PixMap32Bit", pm) is False


def test_update_gworld_stretches_or_clips_what_it_has(g):
    table = g.handle(color_table([WHITE, RED, GREEN, BLACK], seed=7))
    gw = new_gworld(g, 8, Rect(0, 0, 2, 2), table)
    saved = current(g)
    g("SetGWorld", gw, 0)
    g("RGBForeColor", rgb(g, *RED))
    g("PaintRect", g.rect_ptr(Rect(0, 0, 1, 1)))
    g("SetGWorld", *saved)
    out = g.ptr(struct.pack(">L", gw))

    def update(r: Rect, flags: int) -> int:
        return g("UpdateGWorld", out, 8, g.rect_ptr(r), 0, 0, flags)

    assert update(Rect(0, 0, 2, 2), 0) == 0     # nothing to change
    assert update(Rect(0, 0, 4, 4), stretchPix) & (reallocPix | stretchPix) == (
        reallocPix | stretchPix)
    assert values(g, gw) == [bytes([1, 1, 0, 0])] * 2 + [bytes(4)] * 2
    done = update(Rect(1, 1, 3, 7), clipPix)
    assert done & (reallocPix | clipPix | newRowBytes) == reallocPix | clipPix | newRowBytes
    assert values(g, gw) == [bytes([1, 0, 0, 0, 0, 0]), bytes([0, 0, 0, 0, 0, 0])]
    port = g.system.port_at(gw)
    assert port.port_rect == pixmap(g, gw).bounds == Rect(1, 1, 3, 7)
    assert read_table(g.mem, pixmap(g, gw).pm_table)[:4] == [
        0xFFFFFFFF, 0xFFFF0000, 0xFF00FF00, 0xFF000000]
    assert update(Rect(0, 0, 2, 6), 0) == 0     # the same size: moved


def test_dispose_gworld(g):
    out, r = g.ptr(bytes(4)), g.rect_ptr(Rect(0, 0, 40, 40))
    saved = current(g)
    for _ in range(2):                          # the second time, from the same free memory
        free = g.heap.free_bytes()
        assert g("NewGWorld", out, 8, r, 0, 0, 0) == 0
        gw = g.mem.r32(out)
        g("SetGWorld", gw, 0)
        g("DisposeGWorld", gw)
        assert g.mem.r32(lowmem.TheGDevice) == g.system.devices.main
        assert g.system.port_at(gw) is None
        g("SetGWorld", *saved)
    assert g.heap.free_bytes() == free


def test_a_gworld_of_depth_0_is_like_the_deepest_screen(g):
    main = g.system.devices.pixmap(g.system.devices.main)
    entry = deref(g.mem, main.pm_table) + 8 + 8 * 5 + 2      # the screen's colours changed
    g.mem.write(entry, struct.pack(">HHH", 0x1234, 0x5678, 0x9ABC))
    g("CTabChanged", main.pm_table)
    gw = new_gworld(g, 0, Rect(0, 0, 4, 4))
    pm = pixmap(g, gw)
    assert pm.pixel_size == main.pixel_size
    assert read_table(g.mem, pm.pm_table) == g.system.devices.table()
    assert read_table(g.mem, pm.pm_table)[5] & 0xFFFFFF == 0x12569A
    assert pm.pm_table != main.pm_table


def test_given_depth_0_a_world_is_the_size_of_its_global_rectangle_at_0_0(g):
    gw = new_gworld(g, 0, Rect(92, 112, 388, 512))
    assert g.system.port_at(gw).port_rect == Rect(0, 0, 296, 400)
    assert pixmap(g, gw).bounds == Rect(0, 0, 296, 400)
    out = g.ptr(struct.pack(">L", gw))
    g("UpdateGWorld", out, 0, g.rect_ptr(Rect(20, 30, 70, 90)), 0, 0, 0)
    gw = g.mem.r32(out)
    assert pixmap(g, gw).bounds == g.system.port_at(gw).port_rect == Rect(0, 0, 50, 60)
    # Given a depth, the rectangle is the world's own.
    gw = new_gworld(g, 8, Rect(10, 20, 30, 40))
    assert pixmap(g, gw).bounds == Rect(10, 20, 30, 40)


def test_given_depth_0_new_gworld_ignores_its_device_argument(g):
    """Imaging With QuickDraw, Offscreen Graphics Worlds: with a pixel depth
    of 0, aGDevice is ignored even when noNewDevice is set."""
    gw = new_gworld(g, 0, Rect(0, 0, 4, 4), flags=noNewDevice)
    device = g("GetGWorldDevice", gw)
    assert device and device != g.system.devices.main
    out = g.ptr(bytes(4))
    assert g("NewGWorld", out, 8, g.rect_ptr(Rect(0, 0, 4, 4)), 0, 0, noNewDevice) == -50


def test_a_world_made_on_a_programs_device_has_its_depth_and_colour_table(g):
    """Imaging With QuickDraw, NewGWorld: with noNewDevice, the world has
    the device's depth and colour table; given no table, the same handle,
    as OpenCPort gives a cGrafPort the current device's (Inside Macintosh V,
    Color QuickDraw).  So CopyBits copies its values to the screen as they
    are, an index reserved for animation too, which no colour matches."""
    devices = g.system.devices
    screen, main = cport(g), devices.pixmap(devices.main)
    table = main.pm_table
    out, r = g.ptr(bytes(4)), g.rect_ptr(Rect(0, 0, 2, 4))
    assert g("NewGWorld", out, 4, r, 0, devices.main, noNewDevice) == 0
    gw = g.mem.r32(out)
    pm = pixmap(g, gw)
    assert pm.pixel_size == 8 and pm.pm_table == table
    assert g("GetGWorldDevice", gw) == devices.main
    red = g("Color2Index", rgb(g, *RED))
    g("ReserveEntry", red, True)
    g.mem.write(pm.base_addr, bytes([red] * 4))
    g("CopyBits", pm.addr, screen.addr + 2, g.rect_ptr(Rect(0, 0, 1, 4)),
      g.rect_ptr(Rect(40, 50, 41, 54)), 0, 0)
    bits = g.system.screen_bits
    assert g.mem.read(bits.base + 40 * bits.row_bytes + 50, 4) == bytes([red] * 4)
    colors = g.mem.read(deref(g.mem, table), 8 + 8 * 256)
    # Given a table, the world's own; changing it leaves the device's.
    g("UpdateGWorld", out, 8, r, g.handle(color_table([WHITE, RED, GREEN, BLACK])), 0, 0)
    pm = pixmap(g, gw)
    assert pm.pm_table != table and read_table(g.mem, pm.pm_table)[:4] == [
        0xFFFFFFFF, 0xFFFF0000, 0xFF00FF00, 0xFF000000]
    assert g.mem.read(deref(g.mem, table), 8 + 8 * 256) == colors
    g("DisposeGWorld", gw)
    assert main.pm_table == table and deref(g.mem, table)
    assert g("NewGWorld", out, 8, r, g.handle(color_table([WHITE, BLACK])), devices.main,
             noNewDevice) == 0
    assert pixmap(g, g.mem.r32(out)).pm_table != table
    g("DisposeGWorld", g.mem.r32(out))
    assert g.mem.read(deref(g.mem, table), 8 + 8 * 256) == colors


def test_a_screen_buffer_is_a_pixmap_like_the_screens(g):
    gdh, pm_p = g.ptr(bytes(4)), g.ptr(bytes(4))
    assert g("NewScreenBuffer", g.rect_ptr(Rect(0, 0, 3, 5)), False, gdh, pm_p) == 0
    devices = g.system.devices
    assert g.mem.r32(gdh) == devices.main
    pm = PixMap(g.mem, deref(g.mem, g.mem.r32(pm_p)))
    main = devices.pixmap(devices.main)
    assert pm.pixel_size == main.pixel_size and pm.pm_table == main.pm_table
    assert pm.bounds == Rect(0, 0, 3, 5) and pm.base_addr
    g("DisposeScreenBuffer", g.mem.r32(pm_p))
    assert deref(g.mem, main.pm_table)          # the screen's table is left


def test_newgworld_without_the_size_of_its_arguments_in_the_selector(qapp):
    """32-Bit QuickDraw's first interfaces call it with MOVEQ #0,D0: its
    dispatcher looks only at D0's low word (Pico)."""
    m = Processor(model="ii")
    out, bounds = m.ptr(bytes(4)), m.rect_ptr((0, 0, 8, 16))
    m.run(bytes.fromhex("4267"                          # CLR.W -(SP): the result
                        f"2F3C{out:08X}"                # MOVE.L #out,-(SP)
                        "3F3C0008"                      # MOVE.W #8,-(SP): the depth
                        f"2F3C{bounds:08X}"             # MOVE.L #bounds,-(SP)
                        + "42A7" * 3                    # CLR.L -(SP): no table, device, flags
                        + "7000AB1D"))                  # MOVEQ #0,D0; _QDExtensions
    assert m.missing == [] and m.cpu.sp == m.stack_top - 2
    assert m.mem.r16(m.cpu.sp) == 0 and m.mem.r32(out) != 0


# -- basic QuickDraw ----------------------------------------------------------
@pytest.fixture
def plus(qapp):
    return Guest()


def test_on_basic_quickdraw_a_gworld_is_a_grafport_onto_1_bit_pixels(plus):
    """Imaging With QuickDraw, Offscreen Graphics Worlds: with only basic
    QuickDraw, NewGWorld makes a 1-bit pixel map, which GetGWorldPixMap
    gives for LockPixels and the rest, and no GDevice; the depth may be 0
    or 1 (Rescue! makes its worlds so on a Plus)."""
    g = plus
    r = Rect(0, 0, 5, 20)
    gw = new_gworld(g, 0, r)
    port = g.system.port_at(gw)
    assert not port.is_color and port.port_rect == port.port_bits_bounds == r
    assert port.port_bits_row_bytes == 4
    pm_h = g("GetGWorldPixMap", gw)
    pm = PixMap(g.mem, deref(g.mem, pm_h))
    assert pm.pixel_size == 1 and pm.bounds == r and pm.row_bytes & 0x3FFF == 4
    assert g("GetPixBaseAddr", pm_h) == pm.base_addr == port.port_bits_base
    assert g.mem.read(pm.base_addr, 4 * 5) == bytes(20)            # white
    assert g("LockPixels", pm_h)
    assert g("GetGWorldDevice", gw) == g.mem.r32(lowmem.TheGDevice)
    out = g.ptr(bytes(4))
    assert g("NewGWorld", out, 8, g.rect_ptr(r), 0, 0, 0) == -157  # cDepthErr
    assert g("NewGWorld", out, 1, g.rect_ptr(r), 0, 0, 0) == 0


def test_on_basic_quickdraw_what_is_drawn_in_a_gworld_copies_to_a_port(plus):
    g = plus
    gw = new_gworld(g, 1, Rect(0, 0, 2, 8))
    dst, _ = g.port(16, 3)
    g("SetGWorld", gw, 0)
    assert current(g)[0] == gw
    g("PaintRect", g.rect_ptr(Rect(0, 2, 2, 4)))
    pm_h = g("GetGWorldPixMap", gw)
    base = g("GetPixBaseAddr", pm_h)
    assert [g.mem.r8(base + v * 4) for v in range(2)] == [0x30, 0x30]
    g("SetGWorld", dst.addr, 0)
    g("CopyBits", deref(g.mem, pm_h), dst.addr + 2, g.rect_ptr(Rect(0, 0, 2, 8)),
      g.rect_ptr(Rect(1, 4, 3, 12)), 0, 0)
    assert g.rows(dst) == ["." * 16] + ["......##........"] * 2


def test_on_basic_quickdraw_update_gworld_keeps_what_it_has(plus):
    g = plus
    gw = new_gworld(g, 0, Rect(0, 0, 2, 8))
    g("SetGWorld", gw, 0)
    g("PaintRect", g.rect_ptr(Rect(0, 0, 1, 2)))
    out = g.ptr(struct.pack(">L", gw))
    done = g("UpdateGWorld", out, 0, g.rect_ptr(Rect(0, 0, 3, 40)), 0, 0, clipPix)
    assert done & (reallocPix | clipPix | newRowBytes) == reallocPix | clipPix | newRowBytes
    port = g.system.port_at(g.mem.r32(out))
    pm = PixMap(g.mem, deref(g.mem, g("GetGWorldPixMap", port.addr)))
    assert port.port_rect == port.port_bits_bounds == pm.bounds == Rect(0, 0, 3, 40)
    assert port.port_bits_row_bytes == pm.row_bytes & 0x3FFF == 8
    assert port.port_bits_base == pm.base_addr
    assert g.mem.read(pm.base_addr, 24) == b"\xC0" + bytes(23)
    assert g("UpdateGWorld", out, 8, g.rect_ptr(Rect(0, 0, 3, 40)), 0, 0, 0) & 1 << 31


def test_on_basic_quickdraw_dispose_gworld(plus):
    g = plus
    out, r = g.ptr(bytes(4)), g.rect_ptr(Rect(0, 0, 40, 40))
    for _ in range(2):                          # the second time, from the same free memory
        free = g.heap.free_bytes()
        assert g("NewGWorld", out, 0, r, 0, 0, 0) == 0
        gw = g.mem.r32(out)
        g("DisposeGWorld", gw)
        assert g.system.port_at(gw) is None
    assert g.heap.free_bytes() == free


# -- what goes wrong ----------------------------------------------------------
def short_of_room(g, room: Room, call) -> int:
    """call() with room for 0, 1, 2... more blocks, until it has room
    enough: each time short of it, the call gives cNoMemErr, and
    QDError does, and it gives back what it took.  How many it needs."""
    for n in itertools.count():
        room.limit(n)
        err = call()
        if err == 0:
            room.limit(None)
            return n
        assert err == cNoMemErr and g("QDError") == cNoMemErr, n
        assert room.given_back(), n


@pytest.mark.parametrize("depth, flags", [(8, 0), (8, useTempMem), (8, noNewDevice), (32, 0)])
def test_new_gworld_short_of_room_makes_no_part_of_a_world(g, monkeypatch, depth, flags):
    room = Room(monkeypatch, g)
    out, r = g.ptr(bytes(4)), g.rect_ptr(Rect(0, 0, 8, 8))
    device = g.system.devices.main if flags & noNewDevice else 0
    port_p, device_p = g.ptr(bytes(4)), g.ptr(bytes(4))

    def now() -> tuple[int, int]:
        g("GetGWorld", port_p, device_p)
        return g.mem.r32(port_p), g.mem.r32(device_p)
    before = now()

    def new() -> int:
        assert g.mem.r32(out) == 0 and now() == before
        return g("NewGWorld", out, depth, r, 0, device, flags)

    # The pixels, table (unless the device given has the one it uses),
    # PixMap, port and cGrafPort's own blocks, and a device and (for colour
    # tables) its inverse table, unless given one.
    assert short_of_room(g, room, new) >= 4 + (0 if device else 1 if depth > 8 else 2)
    port = g.system.port_at(g.mem.r32(out))
    assert port is not None and current(g) == before and g("QDError") == 0
    assert (g("GetGWorldDevice", port.addr) == g.system.devices.main) == bool(device)
    pixels = pixmap(g, port.addr).base_addr
    assert g.system.heap_of(pixels) is (g.system.sys_heap if flags & useTempMem else g.heap)


def test_on_basic_quickdraw_new_gworld_short_of_room_makes_no_part_of_a_world(plus,
                                                                              monkeypatch):
    g = plus
    room = Room(monkeypatch, g)
    out, r = g.ptr(bytes(4)), g.rect_ptr(Rect(0, 0, 8, 8))
    assert short_of_room(g, room, lambda: g("NewGWorld", out, 1, r, 0, 0, 0)) >= 3
    assert g.system.port_at(g.mem.r32(out)) is not None
    assert g("NewGWorld", out, 1, g.rect_ptr(Rect(0, 0, 8, 0)), 0, 0, 0) == paramErr


@pytest.mark.parametrize("model, depth", [("ii", 8), ("plus", 1)])
def test_a_world_too_big_for_memory_makes_no_part_of_a_world(qapp, monkeypatch, model, depth):
    """Pixels too many for either heap, though there's room for the rest."""
    g = Guest(model=model)
    out, r = g.ptr(bytes(4)), g.rect_ptr(Rect(0, 0, 3000, 3000 * 8 // depth))
    room = Room(monkeypatch, g)
    assert g("NewGWorld", out, depth, r, 0, 0, 0) == cNoMemErr == g("QDError")
    assert g.mem.r32(out) == 0 and len(room.made) >= 2 and room.given_back()


def test_new_gworld_given_what_it_cannot_make(g):
    out = g.ptr(bytes(4))
    r = g.rect_ptr(Rect(0, 0, 8, 8))
    assert g("NewGWorld", out, 3, r, 0, 0, 0) == cDepthErr == g("QDError")
    assert g("NewGWorld", out, 8, g.rect_ptr(Rect(0, 0, 0, 8)), 0, 0, 0) == paramErr
    assert g("QDError") == paramErr and g.mem.r32(out) == 0
    # Given what isn't a colour table, the world has the standard one.
    assert g("NewGWorld", out, 8, r, g.mem.size - 2, 0, 0) == 0
    assert read_table(g.mem, pixmap(g, g.mem.r32(out)).pm_table) == g.system.devices.table()


def test_a_world_of_2_bits_has_its_clut_or_the_greys(qapp):
    """A world of 2 bits given no table has the 'clut' of ID 2, or, with
    none, the four greys (GetCTable's 34)."""
    g = Guest(model="ii")
    gw = new_gworld(g, 2, Rect(0, 0, 4, 4))
    assert read_table(g.mem, pixmap(g, gw).pm_table) == [
        0xFF000000 | v * 0x10101 for v in (0xFF, 0xAA, 0x55, 0)]
    g = Guest([Resource(b"clut", 2, color_table([WHITE, RED, GREEN, BLACK]))], model="ii")
    gw = new_gworld(g, 2, Rect(0, 0, 4, 4))
    assert read_table(g.mem, pixmap(g, gw).pm_table) == [
        0xFFFFFFFF, 0xFFFF0000, 0xFF00FF00, 0xFF000000]


def test_a_world_of_direct_pixels_has_no_colour_table_to_match(g):
    gw = new_gworld(g, 32, Rect(0, 0, 2, 2))
    pm = pixmap(g, gw)
    assert pm.pixel_size == 32 and read_table(g.mem, pm.pm_table) == []
    gd = GDevice(g.mem, deref(g.mem, g("GetGWorldDevice", gw)))
    assert gd.type == 2 and gd.i_table == 0                     # directType
    saved = current(g)
    g("SetGWorld", gw, 0)
    g("RGBForeColor", rgb(g, *RED))
    g("PaintRect", g.rect_ptr(Rect(0, 0, 1, 2)))
    g("SetGWorld", *saved)
    assert [g.mem.r32(pm.base_addr + 4 * i) & 0xFFFFFF for i in range(2)] == [0xFF0000] * 2


def test_update_gworld_given_what_it_cannot_do(g, monkeypatch):
    gw = new_gworld(g, 4, Rect(0, 0, 8, 8))
    out, r = g.ptr(struct.pack(">L", gw)), g.rect_ptr(Rect(0, 0, 8, 8))
    pm = pixmap(g, gw)
    was = (pm.base_addr, pm.pixel_size, pm.bounds, read_table(g.mem, pm.pm_table))
    screen = g.ptr(struct.pack(">L", cport(g).addr))
    assert g("UpdateGWorld", screen, 8, r, 0, 0, 0) == gwFlagErr   # not a world
    assert g("QDError") == paramErr
    assert g("UpdateGWorld", out, 3, r, 0, 0, 0) == gwFlagErr and g("QDError") == cDepthErr
    assert g("UpdateGWorld", out, 8, g.rect_ptr(Rect(0, 0, 8, -1)), 0, 0, 0) == gwFlagErr
    assert g("QDError") == paramErr
    # Short of room for the new pixels, or for its table to grow to 8
    # bits' colours, the world is as it was.
    room = Room(monkeypatch, g)
    for n in range(2):
        assert g("UpdateGWorld", out, 4, r, 0, 0, 0) == 0 == g("QDError")     # nothing to do
        room.limit(n)
        assert g("UpdateGWorld", out, 8, r, 0, 0, clipPix) == gwFlagErr
        assert g("QDError") == cNoMemErr and room.given_back()
        pm = pixmap(g, gw)
        assert (pm.base_addr, pm.pixel_size, pm.bounds, read_table(g.mem, pm.pm_table)) == was
    room.limit(None)
    assert g("UpdateGWorld", out, 8, r, 0, 0, clipPix) & newDepth


def test_update_gworld_to_another_depth_keeps_the_picture_in_its_colours(g):
    gw = new_gworld(g, 4, Rect(0, 0, 2, 8))
    saved = current(g)
    g("SetGWorld", gw, 0)
    g("RGBForeColor", rgb(g, *RED))
    g("PaintRect", g.rect_ptr(Rect(0, 0, 2, 4)))
    g("SetGWorld", *saved)
    pm = pixmap(g, gw)
    assert read_table(g.mem, pm.pm_table)[g.mem.r8(pm.base_addr) >> 4] == 0xFFDD0806
    out = g.ptr(struct.pack(">L", gw))
    done = g("UpdateGWorld", out, 8, g.rect_ptr(Rect(0, 0, 2, 8)), 0, 0, clipPix)
    assert done == reallocPix | newDepth | newRowBytes | clipPix
    pm = pixmap(g, gw)
    assert pm.pixel_size == 8 and pm.row_bytes & 0x3FFF == 8
    table = read_table(g.mem, pm.pm_table)
    assert table == read_table(g.mem, g.handle(standard_table(8)))
    near = table.index(0xFFDD0000)                              # 4 bits' red, in 8 bits
    assert values(g, gw) == [bytes([near] * 4 + [0] * 4)] * 2
    red = table.index(0xFFFF0000)
    gd = GDevice(g.mem, deref(g.mem, g("GetGWorldDevice", gw)))
    assert gd.type == 0 and gd.flags & 1 << 0                   # clutType, gdDevType
    itable = g.contents(gd.i_table)
    assert itable[:4] == g.contents(pm.pm_table)[:4]            # for this table
    assert itable[6 + (15 << 8)] == red                         # red: res 4, its top bits
    # To direct pixels, and the device is one for them.
    assert g("UpdateGWorld", out, 32, g.rect_ptr(Rect(0, 0, 2, 8)), 0, 0, clipPix) & newDepth
    assert g.mem.r32(pixmap(g, gw).base_addr) & 0xFFFFFF == 0xDD0000
    assert GDevice(g.mem, deref(g.mem, g("GetGWorldDevice", gw))).type == 2


def test_update_gworld_to_another_colour_table_maps_the_pixels_to_it(g):
    table = g.handle(color_table([WHITE, RED, GREEN, BLACK]))
    gw = new_gworld(g, 8, Rect(0, 0, 1, 2), table)
    g.mem.write(pixmap(g, gw).base_addr, bytes([1, 2]))       # red, green
    out = g.ptr(struct.pack(">L", gw))
    swapped = g.handle(color_table([WHITE, GREEN, RED, BLACK]))
    done = g("UpdateGWorld", out, 8, g.rect_ptr(Rect(0, 0, 1, 2)), swapped, 0, 0)
    assert done == reallocPix | mapPix | clipPix
    assert values(g, gw) == [bytes([2, 1])]
    assert read_table(g.mem, pixmap(g, gw).pm_table)[1:3] == [0xFF00FF00, 0xFFFF0000]


def test_update_gworld_given_a_device_takes_its_depth_and_colour_table(g):
    """Imaging With QuickDraw, UpdateGWorld: given a device, the world takes
    its pixel depth and colour table, whatever pixelDepth and cTable are.
    A world made on that device with noNewDevice has the device's own table
    then, as one made with no table has, so its pixel values stay as they
    are when the colours of the table a program passes change."""
    devices = g.system.devices
    table = devices.pixmap(devices.main).pm_table
    out, r = g.ptr(bytes(4)), g.rect_ptr(Rect(0, 0, 1, 2))
    mine = g.handle(color_table([WHITE, RED, GREEN, BLACK]))
    assert g("NewGWorld", out, 8, r, mine, devices.main, noNewDevice) == 0
    gw = g.mem.r32(out)
    assert pixmap(g, gw).pm_table != table
    g.mem.write(pixmap(g, gw).base_addr, bytes([1, 2]))       # red, green
    done = g("UpdateGWorld", out, 4, r, mine, devices.main, 0)
    pm = pixmap(g, gw)
    assert pm.pixel_size == 8 and pm.pm_table == table
    assert done == reallocPix | mapPix | clipPix
    red, green = g("Color2Index", rgb(g, *RED)), g("Color2Index", rgb(g, *GREEN))
    assert values(g, gw) == [bytes([red, green])]
    g.mem.write(deref(g.mem, mine) + 8 + 8 + 2, struct.pack(">HHH", 0, 0, 0xFFFF))
    assert g("UpdateGWorld", out, 8, r, mine, devices.main, clipPix) == 0
    assert values(g, gw) == [bytes([red, green])] and pixmap(g, gw).pm_table == table
    g("DisposeGWorld", gw)
    assert deref(g.mem, table)


def test_update_gworld_given_a_device_copies_its_table_for_a_world_of_its_own(g):
    devices = g.system.devices
    main = devices.pixmap(devices.main)
    gw = new_gworld(g, 4, Rect(0, 0, 1, 2))
    out = g.ptr(struct.pack(">L", gw))
    done = g("UpdateGWorld", out, 1, g.rect_ptr(Rect(0, 0, 1, 2)), 0, devices.main, clipPix)
    pm = pixmap(g, gw)
    assert pm.pixel_size == 8 and done & newDepth
    assert pm.pm_table != main.pm_table
    assert read_table(g.mem, pm.pm_table) == read_table(g.mem, main.pm_table)
    g("DisposeGWorld", gw)
    assert deref(g.mem, main.pm_table)


def test_ctab_changed(g):
    """A program that changes a world's colours, then calls CTabChanged,
    has a new seed for its table, and an inverse table made for it; for
    the screen's table, the screen is shown in its new colours."""
    gw = new_gworld(g, 8, Rect(0, 0, 2, 2), g.handle(color_table([WHITE, RED, GREEN, BLACK])))
    pm = pixmap(g, gw)
    ct = deref(g.mem, pm.pm_table)
    seed = g.mem.r32(ct)
    g.mem.write(ct + 8 + 2 * 8 + 2, struct.pack(">HHH", 0, 0, 0xFFFF))  # green to blue
    g("CTabChanged", pm.pm_table)
    assert g.mem.r32(ct) != seed and read_table(g.mem, pm.pm_table)[2] == 0xFF0000FF
    itable = g.contents(GDevice(g.mem, deref(g.mem, g("GetGWorldDevice", gw))).i_table)
    assert itable[:4] == g.mem.read(ct, 4) and itable[6 + 15] == 2   # blue
    # A table no world has: a new seed, nothing more.
    other = g.handle(color_table([WHITE, BLACK]))
    g("CTabChanged", other)
    assert g.mem.r32(deref(g.mem, other)) != 0
    # The screen's.
    devices = g.system.devices
    main_ct = devices.pixmap(devices.main).pm_table
    seed = g.mem.r32(deref(g.mem, main_ct))
    entry = deref(g.mem, main_ct) + 8 + 8 * 5 + 2
    g.mem.write(entry, struct.pack(">HHH", 0x1234, 0x5678, 0x9ABC))
    g("CTabChanged", main_ct)
    assert g.mem.r32(deref(g.mem, main_ct)) != seed
    assert devices.table()[5] & 0xFFFFFF == 0x12569A
    g("CTabChanged", 0)                                       # NIL: nothing


def test_on_basic_quickdraw_ctab_changed_and_screen_buffers_do_nothing(plus):
    g = plus
    table = g.handle(color_table([WHITE, BLACK], seed=7))
    g("CTabChanged", table)
    assert g.mem.r32(deref(g.mem, table)) == 7
    gdh, pm_p = g.ptr(bytes(4)), g.ptr(bytes(4))
    assert g("NewScreenBuffer", g.rect_ptr(Rect(0, 0, 3, 5)), False, gdh, pm_p) == paramErr
    assert g("QDError") == paramErr and g.mem.r32(pm_p) == 0


def test_screen_buffers_short_of_room_or_given_no_rectangle(g, monkeypatch):
    gdh, pm_p = g.ptr(bytes(4)), g.ptr(bytes(4))
    assert g("NewScreenBuffer", g.rect_ptr(Rect(0, 0, 0, 5)), False, gdh, pm_p) == paramErr
    assert g("QDError") == paramErr
    room = Room(monkeypatch, g)
    r = g.rect_ptr(Rect(0, 0, 3, 5))
    assert short_of_room(g, room, lambda: g("NewScreenBuffer", r, False, gdh, pm_p)) == 2
    assert short_of_room(g, room, lambda: g("NewTempScreenBuffer", r, False, gdh, pm_p)) == 2
    base = PixMap(g.mem, deref(g.mem, g.mem.r32(pm_p))).base_addr
    assert g.system.heap_of(base) is g.system.sys_heap         # temporary memory
    g("DisposeScreenBuffer", g.mem.r32(pm_p))
    assert not g.system.sys_heap.is_handle(g.system.sys_heap.recover_handle(base))


def test_a_world_whose_pixels_a_program_has_moved(g):
    """A program that points a world's baseAddr at pixels of its own keeps
    them when it disposes the world, whose own pixels go."""
    gw = new_gworld(g, 8, Rect(0, 0, 4, 4))
    pm = pixmap(g, gw)
    own = pm.base_addr
    mine = g.handle(bytes(16))
    g.heap.lock(mine)
    pm.base_addr = deref(g.mem, mine)
    g.mem.write(pm.base_addr, bytes([3]) * 16)
    out = g.ptr(struct.pack(">L", gw))
    # UpdateGWorld takes what's there now into its new pixels.
    g("UpdateGWorld", out, 8, g.rect_ptr(Rect(0, 0, 4, 8)), 0, 0, clipPix)
    assert values(g, gw) == [bytes([3] * 4 + [0] * 4)] * 4
    assert g.heap.is_handle(mine) and not g.heap.is_handle(g.heap.recover_handle(own))
    pm = pixmap(g, gw)
    own = g.heap.recover_handle(pm.base_addr)
    pm.base_addr = deref(g.mem, mine)
    g("DisposeGWorld", gw)
    assert g.heap.is_handle(mine) and g.contents(mine) == bytes([3]) * 16
    assert not g.heap.is_handle(own)
    # A screen buffer pointed at the screen leaves the screen alone.
    gdh, pm_p = g.ptr(bytes(4)), g.ptr(bytes(4))
    g("NewScreenBuffer", g.rect_ptr(Rect(0, 0, 3, 5)), False, gdh, pm_p)
    buffer = PixMap(g.mem, deref(g.mem, g.mem.r32(pm_p)))
    buffer.base_addr = g.system.screen_bits.base
    g("DisposeScreenBuffer", g.mem.r32(pm_p))
    assert not g.heap.is_handle(g.mem.r32(pm_p))


def test_what_is_and_is_not_a_world(g):
    screen = cport(g)
    g("DisposeGWorld", screen.addr)                           # not a world: left as it is
    assert g.system.port_at(screen.addr) is screen
    assert g("GetGWorldPixMap", 0) == 0
    # A colour port of a program's own making, and an old port.
    record = g.ptr(bytes(GrafPort.SIZE))
    g.mem.write(record + 2, struct.pack(">LH", 0x1234, 0xC000))
    assert g("GetGWorldPixMap", record) == 0x1234
    g.mem.w16(record + 6, 0)
    assert g("GetGWorldPixMap", record) == 0
    assert g("GetPixBaseAddr", g.mem.size - 2) == 0           # not a handle


def test_the_calls_that_change_or_ask_nothing(g):
    gw = new_gworld(g, 8, Rect(0, 0, 4, 4))
    pm = g("GetGWorldPixMap", gw)
    g("AllowPurgePixels", pm)
    g("NoPurgePixels", pm)
    assert g("GetPixelsState", pm) == 0
    assert g("QDDone", gw) is True
    assert g("OffscreenVersion") >= 0x100       # 32-Bit QuickDraw's offscreen routines
    port = g.system.port_at(gw)
    before = g.mem.read(gw, GrafPort.SIZE)
    for name, arg in (("PortChanged", gw), ("PixPatChanged", port.pn_pix_pat),
                      ("GDeviceChanged", g("GetGWorldDevice", gw))):
        assert g(name, arg) is None
    assert g.mem.read(gw, GrafPort.SIZE) == before
