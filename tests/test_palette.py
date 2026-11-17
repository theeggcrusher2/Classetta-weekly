"""The Palette Manager (Inside Macintosh V): palettes, the screen colours
a front window's palette gives it, and colour animation."""

import struct

import pytest

from macrt.m68k import glue_palette
from macrt.m68k.glue_palette import (pmAllUpdates, pmAnimated, pmBkUpdates, pmCourteous,
                                     pmExplicit, pmFgUpdates, pmNoUpdates, pmTolerant)
from macrt.resfork import Resource
from macrt.toolbox.color import CT_PALETTE, color_table
from macrt.toolbox.gdevice import GDevice, deref, write_pixmap
from macrt.toolbox.memmgr import noErr
from macrt.toolbox.quickdraw import GrafPort
from macrt.toolbox.types import Rect
from macrt.toolbox.windows import WindowRecord

from guest import Guest, Room

WHITE, BLACK = (0xFFFF, 0xFFFF, 0xFFFF), (0, 0, 0)
RED, BLUE = (0xFFFF, 0, 0), (0, 0, 0xFFFF)
ODD = (0x1234, 0x5678, 0x9ABC)          # none of the standard 8-bit colours
ODDER = (0x9ABC, 0x1234, 0x5678)
ODDEST = (0x5678, 0x9ABC, 0x1234)
pmInhibitC8 = 0x2000


def guest(resources=(), model="ii", **system) -> Guest:
    """A Mac II's trap context, whose windows are on the screen, with no
    views to show what's drawn in them."""
    g = Guest(resources, model=model, **system)
    g.tb.qd.repaint = lambda port: None
    g.system.screen.attach(g.process, lambda w: None)
    return g


@pytest.fixture
def g(qapp):
    return guest()


def rgb(g, c) -> int:
    return g.ptr(struct.pack(">HHH", *c))


def read_rgb(g, p) -> tuple:
    return struct.unpack(">HHH", g.mem.read(p, 6))


def argb(c) -> int:
    r, gr, b = c
    return 0xFF000000 | (r >> 8) << 16 | (gr >> 8) << 8 | b >> 8


def window(g, color: bool = True) -> WindowRecord:
    """A new window, in front, the current port."""
    w = WindowRecord.new(g.system, g.process, g.heap, 0, Rect(40, 40, 100, 200), "Palette",
                         True, 0, False, 0, color=color)
    w.next_window = g.process.windows.head
    g.process.windows.head = w.addr
    g.system.screen.moved()
    g("SetPort", w.addr)
    return w


def palette(g, colors, usage, tolerance=0) -> int:
    return g("NewPalette", len(colors), g.handle(color_table(colors)), usage, tolerance)


def clut(g) -> list[tuple[int, int, int, int]]:
    """The screen's colour table's entries: value, red, green, blue."""
    devices = g.system.devices
    ct = deref(g.mem, devices.pixmap(devices.main).pm_table)
    n = g.mem.r16s(ct + 6) + 1
    return [struct.unpack(">HHHH", g.mem.read(ct + 8 + 8 * i, 8)) for i in range(n)]


def seed(g) -> int:
    devices = g.system.devices
    return g.mem.r32(deref(g.mem, devices.pixmap(devices.main).pm_table))


def low_memory(g) -> bytes:
    """Make what's at address 0 on, where a NIL handle's master pointer
    would be, something other than zeros, as the vectors there would be;
    what's there."""
    g.mem.write(0, bytes(i % 255 + 1 for i in range(0x100)))
    return g.mem.read(0, 0x100)


def screen(g, rect: Rect) -> set[int]:
    """The pixel values in rect on the screen."""
    bits = g.system.screen_bits
    return {g.mem.r8(bits.base + v * bits.row_bytes + h)
            for h in range(rect.left, rect.right) for v in range(rect.top, rect.bottom)}


def test_a_palette_s_entries(g):
    h = palette(g, [RED, BLUE], pmTolerant, 0x1000)
    out = g.ptr(bytes(6))
    g("GetEntryColor", h, 1, out)
    assert read_rgb(g, out) == BLUE
    usage, tolerance = g.ptr(bytes(2)), g.ptr(bytes(2))
    g("GetEntryUsage", h, 0, usage, tolerance)
    assert (g.mem.r16(usage), g.mem.r16(tolerance)) == (pmTolerant, 0x1000)
    g("SetEntryUsage", h, 0, -1, 0x2000)            # -1: as it was
    g("GetEntryUsage", h, 0, usage, tolerance)
    assert (g.mem.r16(usage), g.mem.r16(tolerance)) == (pmTolerant, 0x2000)
    g("SetEntryColor", h, 0, rgb(g, ODD))
    g("GetEntryColor", h, 0, out)
    assert read_rgb(g, out) == ODD

    # More entries than the table has colours: black.
    longer = g("NewPalette", 3, g.handle(color_table([RED])), pmCourteous, 0)
    g("GetEntryColor", longer, 2, out)
    assert g.mem.r16s(deref(g.mem, longer)) == 3 and read_rgb(g, out) == BLACK

    ct = g.handle(color_table([WHITE]))
    g("Palette2CTab", h, ct)
    data = g.contents(ct)
    assert struct.unpack_from(">h", data, 6)[0] == 1
    assert struct.unpack_from(">4H4H", data, 8) == (0, *ODD, 1, *BLUE)
    g("CTab2Palette", g.handle(color_table([BLUE, RED, WHITE])), h, pmAnimated, 7)
    assert g.mem.r16s(deref(g.mem, h)) == 3
    g("GetEntryColor", h, 2, out)
    g("GetEntryUsage", h, 2, usage, tolerance)
    assert (read_rgb(g, out), g.mem.r16(usage), g.mem.r16(tolerance)) == (WHITE, pmAnimated, 7)

    g("CopyPalette", h, longer, 1, 2, 2)            # entries 2 and 3 of longer
    assert g.mem.r16s(deref(g.mem, longer)) == 4
    g("GetEntryColor", longer, 3, out)
    g("GetEntryUsage", longer, 3, usage, tolerance)
    assert (read_rgb(g, out), g.mem.r16(usage)) == (WHITE, pmAnimated)
    g("GetEntryColor", longer, 0, out)
    assert read_rgb(g, out) == RED


def test_resize_palette_adds_black_courteous_entries_and_lets_go_of_those_it_cuts(g):
    w = window(g)
    h = palette(g, [RED, ODD, ODDER], pmAnimated, 5)
    g("NSetPalette", w.addr, h, pmAllUpdates)
    devices = g.system.devices
    assert sorted(e for _, e in devices.reserved.values()) == [0, 1, 2]
    out, usage, tolerance = g.ptr(bytes(6)), g.ptr(bytes(2)), g.ptr(bytes(2))
    g("ResizePalette", h, 5)
    assert g.mem.r16s(deref(g.mem, h)) == 5
    g("GetEntryColor", h, 1, out)
    g("GetEntryUsage", h, 1, usage, tolerance)
    assert (read_rgb(g, out), g.mem.r16(usage), g.mem.r16(tolerance)) == (ODD, pmAnimated, 5)
    g("GetEntryColor", h, 4, out)
    g("GetEntryUsage", h, 4, usage, tolerance)
    assert (read_rgb(g, out), g.mem.r16(usage), g.mem.r16(tolerance)) == (BLACK, pmCourteous, 0)
    g("ResizePalette", h, 1)
    assert g.mem.r16s(deref(g.mem, h)) == 1
    g("GetEntryColor", h, 0, out)
    assert read_rgb(g, out) == RED
    assert [e for _, e in devices.reserved.values()] == [0]
    assert sum(1 for c in clut(g) if c[0] & 0x4000) == 1
    before = g.contents(h)
    g("ResizePalette", h, -1)
    g("ResizePalette", 0, 3)
    assert g.contents(h) == before


def test_get_new_palette_reads_a_pltt_and_clears_what_s_private(qapp):
    entry = struct.pack(">HHHHH", *ODD, pmTolerant, 0x0100) + b"\xAB" * 6
    pltt = struct.pack(">h", 1) + b"\xCD" * 14 + entry
    g = guest([Resource(b"pltt", 128, pltt), Resource(b"pltt", 130, b"\x01")])
    assert g("GetNewPalette", 129) == g("GetNewPalette", 130) == 0
    h = g("GetNewPalette", 128)
    assert g.contents(h) == struct.pack(">h", 1) + bytes(14) + entry[:10] + bytes(6)


def test_a_tolerant_palette_gives_the_front_window_its_colours(g):
    w = window(g)
    before = clut(g)
    red = next(i for i, c in enumerate(before) if c[1:] == RED)
    h = palette(g, [ODD, RED, ODDER], pmTolerant)
    s = seed(g)
    g("NSetPalette", w.addr, h, pmAllUpdates)
    after = clut(g)
    changed = [i for i in range(len(after)) if after[i] != before[i]]
    assert len(changed) == 2 and 0 not in changed and 255 not in changed
    assert {after[i][1:] for i in changed} == {ODD, ODDER}
    assert after[red][1:] == RED and seed(g) != s     # red was there already
    assert g("GetPalette", w.addr) == h
    # Drawing in its colours draws in those indexes.
    assert g("Color2Index", rgb(g, ODD)) in changed
    # Activating it again changes nothing.
    s = seed(g)
    g("ActivatePalette", w.addr)
    assert clut(g) == after and seed(g) == s


def test_tolerance_explicit_entries_and_inhibited_ones(g):
    w = window(g)
    before = clut(g)
    # Near red, green and blue will do: each an index of its own.
    near = [(0xFFFF, 0x0800, 0), (0x0800, 0xFFFF, 0), (0, 0x0800, 0xFFFF)]
    h = palette(g, near * 2, pmTolerant, 0x1000)
    g("SetEntryUsage", h, 5, pmTolerant | pmExplicit, 0)             # index 5 is ODD
    g("SetEntryColor", h, 5, rgb(g, ODD))
    g("SetEntryUsage", h, 4, pmTolerant | pmInhibitC8, 0)            # not on an 8-bit screen
    g("SetEntryColor", h, 4, rgb(g, ODDER))
    g("SetEntryUsage", h, 3, pmCourteous, 0)
    g("SetEntryColor", h, 3, rgb(g, ODDER))
    g("NSetPalette", w.addr, h, pmAllUpdates)
    after = clut(g)
    assert [i for i in range(256) if after[i] != before[i]] == [5]
    assert after[5][1:] == ODD


def test_animated_entries_reserve_indexes_that_are_drawn_in_and_animated(g):
    w = window(g)
    h = palette(g, [RED, BLACK, ODD], pmAnimated)
    g("NSetPalette", w.addr, h, pmAllUpdates)
    devices = g.system.devices
    reserved = sorted(devices.reserved)
    assert len(reserved) == 3 and not {0, 255} & set(reserved)
    assert {devices.reserved[i] for i in reserved} == {(h, 0), (h, 1), (h, 2)}
    table = clut(g)
    assert all(table[i][0] & 0x4000 for i in reserved)
    by_entry = {e: i for i, (_, e) in devices.reserved.items()}
    assert [table[by_entry[e]][1:] for e in range(3)] == [RED, BLACK, ODD]
    # Nothing else is drawn in them: not even black, which index 255 is.
    assert g("Color2Index", rgb(g, RED)) not in reserved
    assert g("Color2Index", rgb(g, BLACK)) == 255

    g("PmForeColor", 0)
    r = Rect(10, 50, 14, 60)
    assert w.fg_color == by_entry[0]
    g("PaintRect", g.rect_ptr(r))
    at = Rect(r.top + w.content.top, r.left + w.content.left, r.bottom + w.content.top,
              r.right + w.content.left)
    assert screen(g, at) == {by_entry[0]}
    g("RGBForeColor", rgb(g, BLACK))
    g("PmForeColor", 1)
    assert w.fg_color == by_entry[1]
    # Animating it changes the colour, not the pixels, nor the seed.
    s = seed(g)
    g("AnimateEntry", w.addr, 0, rgb(g, BLUE))
    assert clut(g)[by_entry[0]][1:] == BLUE and seed(g) == s
    # The screen has blue already: it's shown a step from it, to be told apart.
    assert devices.table()[by_entry[0]] == argb(BLUE) - 1
    assert screen(g, at) == {by_entry[0]}
    with w.backing.lock:                                # and the window shows it
        assert w.backing.image.pixel(55, 12) | 0xFF000000 == argb(BLUE) - 1
    out = g.ptr(bytes(6))
    g("GetEntryColor", h, 0, out)
    assert read_rgb(g, out) == BLUE
    g("AnimatePalette", w.addr, g.handle(color_table([WHITE, RED, ODDER])), 1, 1, 5)
    assert [clut(g)[by_entry[e]][1:] for e in range(3)] == [BLUE, RED, ODDER]

    # Entry2Index: an animated entry's reserved index; a courteous one's,
    # the nearest colour's; an explicit one's, its number.
    assert [g("Entry2Index", e) for e in range(3)] == [by_entry[e] for e in range(3)]
    g("SetEntryUsage", h, 2, pmCourteous, 0)
    g("SetEntryColor", h, 2, rgb(g, RED))
    assert g("Entry2Index", 2) == g("Color2Index", rgb(g, RED))
    g("SetEntryUsage", h, 2, pmCourteous | pmExplicit, 0)
    assert g("Entry2Index", 2) == 2 and g("Entry2Index", 3) == 0
    g("SetEntryUsage", h, 2, pmAnimated, 0)

    # Activating it again keeps its indexes; disposing of it gives them up.
    g("ActivatePalette", w.addr)
    assert {i: (h, e) for e, i in by_entry.items()} == devices.reserved
    g("DisposePalette", h)
    assert not devices.reserved and not any(c[0] & 0x4000 for c in clut(g))
    assert g("GetPalette", w.addr) == 0


def test_set_entries_changes_an_animated_entry_whatever_the_devices_gdid(g):
    w = window(g)
    g("NSetPalette", w.addr, palette(g, [RED], pmAnimated), pmAllUpdates)
    (index,) = g.system.devices.reserved
    gd = GDevice(g.mem, deref(g.mem, g("GetMainDevice")))
    gd.id = gd.id + 1
    spec = g.ptr(struct.pack(">HHHH", 0, *BLUE))
    g("SetEntries", index, 0, spec)
    assert g("QDError") == 0 and clut(g)[index][1:] == BLUE


def test_a_pixmap_whose_colour_table_is_of_palette_entries(g):
    """A colour table whose ctFlags have bit 14 set gives each pixel value
    an entry of a palette, \"instead of its rgb values\" (Apple's Out of
    This GWorld sample): what's copied from a PixMap of it is in the
    colours of the entries of the current port's palette.  A port with
    no palette of its own draws on it in pixel values, as any does, and
    what else is there stays as it is."""
    w = window(g)
    h = palette(g, [WHITE, BLACK, RED, BLACK], pmAnimated)
    g("NSetPalette", w.addr, h, pmAllUpdates)
    devices = g.system.devices
    shown = {e: devices.table()[i] for i, (_, e) in devices.reserved.items()}
    assert shown[1] != shown[3]                         # black, told apart
    # Its own colours, all black, aren't what's shown.
    table = g.handle(color_table([BLACK] * 4, flags=CT_PALETTE))
    pm = g.handle(bytes(50))
    base = g.ptr(bytes(range(4)) * 2)
    write_pixmap(g.mem, deref(g.mem, pm), base, 4, Rect(0, 0, 2, 4), 8, table)
    r = g.rect_ptr(Rect(0, 0, 2, 4))

    def copied():
        g("CopyBits", deref(g.mem, pm), w.addr + 2, r, r, 0, 0)
        with w.backing.lock:
            return [w.backing.image.pixel(x, 1) | 0xFF000000 for x in range(4)]

    assert copied() == [shown[e] for e in range(4)]
    p = g.ptr(bytes(GrafPort.SIZE))
    g("OpenCPort", p)
    g("SetPortPix", pm)
    g.system.port_at(p).fg_color = 3                     # as PmForeColor(3) sets it
    g("PaintRect", g.rect_ptr(Rect(0, 1, 1, 2)))
    assert g.mem.read(base, 8) == bytes([0, 3, 2, 3, 0, 1, 2, 3])
    g("SetPort", w.addr)
    assert copied() == [shown[e] for e in range(4)]


def test_a_pixmap_in_the_screens_colours_is_copied_to_a_window_as_its_values(g):
    """A PixMap in the screen's colour table, copied to a window, is
    written in the screen buffer as its values, an animated index's too,
    whatever colours they are when it's copied: a program can copy its
    picture while it's all black and fade it in, as Lemmings does."""
    w = window(g)
    h = palette(g, [BLACK] * 4, pmAnimated | pmExplicit)
    g("NSetPalette", w.addr, h, pmAllUpdates)
    devices = g.system.devices
    assert sorted(devices.reserved) == [1, 2, 3]
    pm = g.handle(bytes(50))
    write_pixmap(g.mem, deref(g.mem, pm), g.ptr(bytes([1, 2, 3, 255]) * 2), 4,
                 Rect(0, 0, 2, 4), 8, devices.pixmap(devices.main).pm_table)
    r = Rect(10, 50, 12, 54)
    g("CopyBits", deref(g.mem, pm), w.addr + 2, g.rect_ptr(Rect(0, 0, 2, 4)), g.rect_ptr(r),
      0, 0)
    at = Rect(r.top + w.content.top, r.left + w.content.left, r.bottom + w.content.top,
              r.right + w.content.left)
    assert screen(g, at) == {1, 2, 3, 255}
    g("AnimatePalette", w.addr, g.handle(color_table([RED, BLUE, ODD])), 0, 1, 3)
    g.system.screen.look()
    shown = devices.table()
    with w.backing.lock:                                # the window shows them
        assert [w.backing.image.pixel(50 + x, 10) | 0xFF000000 for x in range(4)] == \
            [shown[1], shown[2], shown[3], argb(BLACK)]
    for i, c in zip((1, 2, 3), (RED, BLUE, ODD)):       # (each told apart, a step at most)
        assert all(abs((shown[i] >> s & 0xFF) - (argb(c) >> s & 0xFF)) <= 1 for s in (0, 8, 16))


@pytest.mark.parametrize("near", [False, True])
def test_a_colour_copied_from_a_pixmap_of_its_own_colours_is_no_animated_entry(g, near):
    """CopyBits from a PixMap with a colour table of its own matches its
    colours to the screen's as Color2Index does: never to an entry
    reserved for animation, whether the colour is near the entry's or
    the same, so what's copied doesn't change as it's animated.  What's
    drawn in the entry does."""
    w = window(g)
    blue = (0x1000, 0x2000, 0xE000)
    h = palette(g, [blue], pmAnimated)
    g("NSetPalette", w.addr, h, pmAllUpdates)
    devices = g.system.devices
    (index, _), = devices.reserved.items()
    colour = (0x1100, 0x2200, 0xDD00) if near else blue
    value = g("Color2Index", rgb(g, colour))
    assert value != index
    pm = g.handle(bytes(50))
    write_pixmap(g.mem, deref(g.mem, pm), g.ptr(bytes([1]) * 8), 4, Rect(0, 0, 2, 4), 8,
                 g.handle(color_table([WHITE, colour])))
    r = Rect(10, 50, 12, 54)
    g("CopyBits", deref(g.mem, pm), w.addr + 2, g.rect_ptr(Rect(0, 0, 2, 4)), g.rect_ptr(r),
      0, 0)
    g("PmForeColor", 0)
    g("PaintRect", g.rect_ptr(Rect(20, 50, 22, 54)))
    at = r.offset(w.content.left, w.content.top)
    drawn = Rect(20, 50, 22, 54).offset(w.content.left, w.content.top)
    assert screen(g, at) == {value} and screen(g, drawn) == {index}
    g("AnimateEntry", w.addr, 0, rgb(g, RED))
    g.system.screen.look()
    with w.backing.lock:
        assert w.backing.image.pixel(51, 10) | 0xFF000000 == devices.table()[value]
        assert abs((w.backing.image.pixel(51, 20) >> 16 & 0xFF) - 0xFF) <= 1


@pytest.mark.parametrize("color_port", [False, True])
def test_a_colour_copied_to_the_screen_s_own_bits_is_no_animated_entry(g, color_port):
    """The same, copied to a BitMap of the screen's own pixels, in a
    colour window's port (drawn in pixel values) or an old GrafPort."""
    w = window(g)
    blue = (0x1000, 0x2000, 0xE000)
    g("NSetPalette", w.addr, palette(g, [blue], pmAnimated), pmAllUpdates)
    (index, _), = g.system.devices.reserved.items()
    if not color_port:
        port = g.ptr(bytes(GrafPort.SIZE))
        g("OpenPort", port)
    near = (0x1100, 0x2200, 0xDD00)
    pm = g.handle(bytes(50))
    write_pixmap(g.mem, deref(g.mem, pm), g.ptr(bytes([1, 2]) * 4), 4, Rect(0, 0, 2, 4), 8,
                 g.handle(color_table([WHITE, near, blue])))
    bits = g.system.screen_bits
    bm = g.ptr(struct.pack(">LH4h", bits.base, bits.row_bytes, *bits.bounds))
    r = Rect(300, 300, 302, 304)
    g("CopyBits", deref(g.mem, pm), bm, g.rect_ptr(Rect(0, 0, 2, 4)), g.rect_ptr(r), 0, 0)
    values = {g("Color2Index", rgb(g, c)) for c in (near, blue)}
    assert index not in values and screen(g, r) == values


def test_an_animated_entry_back_in_its_index_s_old_colour_is_still_no_match(g):
    """An entry animated to the colour its index had before it was
    reserved: a colour copied in that colour is matched to the screen's
    colours as they are now, with the entry left out, not as they were."""
    w = window(g)
    before = clut(g)
    g("NSetPalette", w.addr, palette(g, [BLUE], pmAnimated), pmAllUpdates)
    (index, _), = g.system.devices.reserved.items()
    was = before[index][1:]
    g("AnimateEntry", w.addr, 0, rgb(g, was))
    value = g("Color2Index", rgb(g, was))
    assert value != index
    pm = g.handle(bytes(50))
    write_pixmap(g.mem, deref(g.mem, pm), g.ptr(bytes([1]) * 8), 4, Rect(0, 0, 2, 4), 8,
                 g.handle(color_table([WHITE, was])))
    r = Rect(10, 50, 12, 54)
    g("CopyBits", deref(g.mem, pm), w.addr + 2, g.rect_ptr(Rect(0, 0, 2, 4)), g.rect_ptr(r),
      0, 0)
    assert screen(g, r.offset(w.content.left, w.content.top)) == {value}


def test_drawing_while_restore_entries_has_the_screen_all_black(g):
    """RestoreEntries sets the screen's entries without a new seed, and a
    program can set them all black, draw, and set them back to fade in
    what it drew, as Zoop does: what's drawn, in a port's colour and
    copied from a PixMap in the screen's colour table, is written in the
    screen buffer as the values it was drawn in, though they were all
    black, and the window shows their colours again once they're back."""
    w = window(g)
    g("RGBForeColor", rgb(g, BLACK))
    assert w.fg_color == 255
    devices = g.system.devices
    sel = g.ptr(struct.pack(">h", 255) + b"".join(struct.pack(">h", i) for i in range(256)))
    saved = g.handle(color_table([BLACK] * 256))
    g("SaveEntries", 0, saved, sel)
    g("RestoreEntries", g.handle(color_table([BLACK] * 256)), 0, sel)
    assert {c[1:] for c in clut(g)} == {BLACK}
    shown = devices.table()
    assert len(set(shown)) == 256 and shown[0] == argb(BLACK)   # told apart

    def at(r: Rect) -> Rect:
        return r.offset(w.content.left, w.content.top)

    painted = Rect(10, 50, 14, 60)
    g("PaintRect", g.rect_ptr(painted))
    assert screen(g, at(painted)) == {255}
    pm = g.handle(bytes(50))
    write_pixmap(g.mem, deref(g.mem, pm), g.ptr(bytes([0, 255, 5, 0]) * 2), 4,
                 Rect(0, 0, 2, 4), 8, devices.pixmap(devices.main).pm_table)
    copied = Rect(20, 50, 22, 54)
    g("CopyBits", deref(g.mem, pm), w.addr + 2, g.rect_ptr(Rect(0, 0, 2, 4)),
      g.rect_ptr(copied), 0, 0)
    assert [g.mem.r8(g.system.screen_bits.base + at(copied).top * g.system.screen_bits.row_bytes
                     + at(copied).left + x) for x in range(4)] == [0, 255, 5, 0]

    g("RestoreEntries", saved, 0, sel)
    g.system.screen.look()
    shown = devices.table()
    assert shown[0] == argb(WHITE) and shown[255] == argb(BLACK)
    with w.backing.lock:
        assert w.backing.image.pixel(55, 12) | 0xFF000000 == argb(BLACK)
        assert [w.backing.image.pixel(50 + x, 20) | 0xFF000000 for x in range(4)] == \
            [shown[0], shown[255], shown[5], shown[0]]


def test_palettes_of_windows_not_in_front_and_the_application_s(g):
    back = window(g)
    front = window(g)
    mine = palette(g, [ODD], pmTolerant)
    before = clut(g)
    g("NSetPalette", back.addr, mine, pmAllUpdates)                # not in front: nothing yet
    assert clut(g) == before
    g("ActivatePalette", back.addr)
    assert clut(g) == before
    # The application's default is the front window's, which has none.
    default = palette(g, [ODDER], pmTolerant)
    g("NSetPalette", -1, default, pmNoUpdates)
    assert g("GetPalette", -1) == default and g("GetPalette", front.addr) == 0
    assert any(c[1:] == ODDER for c in clut(g))
    plain = window(g, color=False)
    g("NSetPalette", plain.addr, mine, pmAllUpdates)
    assert g("GetPalette", plain.addr) == 0             # not a colour window's


def test_the_application_has_a_default_palette_it_hasn_t_set(qapp):
    # No 'pltt' 0: one of two entries, white and black, the same each time.
    g = guest()
    h = g("GetPalette", -1)
    assert h and g("GetPalette", -1) == h
    assert g.contents(h)[:2] == struct.pack(">h", 2)
    assert [g.contents(h)[16 + 16 * i:22 + 16 * i] for i in range(2)] == \
        [struct.pack(">HHH", *c) for c in (WHITE, BLACK)]
    g("DisposePalette", h)                              # made again
    h = g("GetPalette", -1)
    assert h and g.contents(h)[:2] == struct.pack(">h", 2)
    # 'pltt' 0: that, until the application sets its own.
    entry = struct.pack(">HHHHH6x", *ODD, pmCourteous, 0)
    g = guest([Resource(b"pltt", 0, struct.pack(">h", 1) + bytes(14) + entry)])
    h = g("GetPalette", -1)
    assert h and g.contents(h)[16:22] == struct.pack(">HHH", *ODD)
    mine = palette(g, [ODDER], pmCourteous)
    g("NSetPalette", -1, mine, pmNoUpdates)
    assert g("GetPalette", -1) == mine


def test_a_colour_window_from_its_resources_gets_the_pltt_of_its_id(qapp):
    entry = struct.pack(">HHHHH6x", *ODD, pmTolerant, 0)
    g = guest([Resource(b"pltt", 128, struct.pack(">h", 1) + bytes(14) + entry)])
    w = window(g)
    glue_palette.new_window(g, w, 128)                  # GetNewCWindow's, for WIND 128
    h = g("GetPalette", w.addr)
    assert h and g.contents(h)[16:22] == struct.pack(">HHH", *ODD)
    assert any(c[1:] == ODD for c in clut(g))


def test_windows_that_want_colour_updates_get_them_when_the_colours_change(g):
    back, front = window(g), window(g)

    def updates() -> tuple[bool, bool]:
        """Which windows have been given updates, the back one's and the
        front one's; and none have, after."""
        got = back.update_pending, front.update_pending
        back.update_pending = front.update_pending = False
        return got

    updates()
    g("NSetPalette", back.addr, palette(g, [RED], pmCourteous), pmBkUpdates)
    assert updates() == (False, False)                  # no colours changed
    h = palette(g, [ODD], pmTolerant)
    g("NSetPalette", front.addr, h, pmNoUpdates)
    assert any(c[1:] == ODD for c in clut(g))
    assert back.update.bbox == back.content             # all of what's on the screen
    assert updates() == (True, False)
    g("NSetPalette", front.addr, h, pmFgUpdates)
    assert updates() == (False, False)                  # the screen has its colours already
    g("SetEntryColor", h, 0, rgb(g, ODDER))
    assert updates() == (False, False)                  # not till it's activated
    g("ActivatePalette", front.addr)
    assert updates() == (True, True)

    # SetPalette's Boolean is in the word's high byte.
    g("NSetPalette", back.addr, 0, 0x00C0)              # FALSE
    g("SetEntryColor", h, 0, rgb(g, ODDEST))
    g("NSetPalette", front.addr, h, 0x01C0)             # TRUE
    assert updates() == (False, True)

    # Animating isn't a change.
    animated = palette(g, [ODD], pmAnimated)
    g("NSetPalette", front.addr, animated, pmAllUpdates)
    updates()
    g("AnimateEntry", front.addr, 0, rgb(g, BLUE))
    assert updates() == (False, False)


def test_what_isnt_a_palette_a_table_or_an_entry_is_passed_over(g):
    bogus = g.mem.size - 2                              # not a handle
    w = window(g)
    g("InitPalettes")
    h = palette(g, [RED, BLUE], pmCourteous)
    other = palette(g, [ODD], pmCourteous)
    ct = g.handle(color_table([WHITE]))
    g("NSetPalette", w.addr, h, pmAllUpdates)
    g("RGBForeColor", rgb(g, ODDER))
    before = g.contents(h), g.contents(other), g.contents(ct), w.rgb_fg_color
    low = low_memory(g)
    out = g.ptr(bytes(6))
    g("GetEntryColor", g("NewPalette", 1, bogus, pmCourteous, 0), 0, out)
    assert read_rgb(g, out) == BLACK                    # no table: black
    g("DisposePalette", 0)
    assert g.heap.error == noErr
    g("SetEntryUsage", h, 2, pmAnimated, 0)
    g("AnimateEntry", w.addr, 2, rgb(g, ODD))
    g("PmForeColor", 2)
    for table, pal in ((0, h), (bogus, h), (ct, 0), (ct, bogus)):
        g("CTab2Palette", table, pal, pmAnimated, 0)
        g("Palette2CTab", pal, table)
    for src, dst, src_entry, dst_entry, n in ((0, other, 0, 0, 1), (h, 0, 0, 0, 1),
                                              (bogus, other, 0, 0, 1), (h, bogus, 0, 0, 1),
                                              (h, other, 0, 0, 0), (h, other, 0, 0, -1),
                                              (h, other, -1, 0, 3), (h, other, 0, -1, 2),
                                              (h, other, 2, 5, 1)):
        g("CopyPalette", src, dst, src_entry, dst_entry, n)
    assert (g.contents(h), g.contents(other), g.contents(ct), w.rgb_fg_color) == before
    assert g.mem.read(0, 0x100) == low
    g("SetPort", 0)
    g("PmForeColor", 0)
    assert g("Entry2Index", 0) == 0


def test_with_no_room_a_palette_or_table_is_left_as_it_was(g, monkeypatch):
    room = Room(monkeypatch, g)
    h = palette(g, [RED], pmCourteous)
    longer = palette(g, [RED, BLUE, ODD], pmTolerant)
    ct, three = g.handle(color_table([WHITE])), g.handle(color_table([BLUE, RED, WHITE]))
    before = g.contents(h), g.contents(ct)
    room.limit(0)
    g("CTab2Palette", three, h, pmCourteous, 0)
    g("Palette2CTab", longer, ct)
    g("CopyPalette", longer, h, 0, 0, 3)
    assert (g.contents(h), g.contents(ct)) == before


def test_pm_fore_and_back_colour_by_an_entry_s_usage(g):
    w = window(g)
    h = palette(g, [ODD, RED, ODDER], pmCourteous)
    g("SetEntryUsage", h, 1, pmAnimated, 0)
    g("SetEntryUsage", h, 2, pmCourteous | pmExplicit, 0)
    g("NSetPalette", w.addr, h, pmAllUpdates)
    (index,) = g.system.devices.reserved
    g("PmForeColor", 0)                                 # courteous: as RGBForeColor sets it
    assert (w.rgb_fg_color, w.fg_color) == (ODD, g("Color2Index", rgb(g, ODD)))
    g("PmBackColor", 1)                                 # animated: its index
    assert (w.rgb_bk_color, w.bk_color) == (RED, index)
    g("PmForeColor", 2)                                 # explicit: index 2, in its colour
    assert (w.rgb_fg_color, w.fg_color) == (clut(g)[2][1:], 2)


@pytest.mark.parametrize("model, depth", [("plus", None), ("ii", 32)])
def test_with_no_screen_table_to_change_an_entry_is_just_its_colour(qapp, model, depth):
    """On a Plus, which has no devices, and on a screen of direct colours,
    which has no colour table, a palette changes none of the screen's
    colours; drawing in an entry draws in its colour."""
    entry = struct.pack(">HHHHH6x", *ODD, pmTolerant, 0)
    g = guest([Resource(b"pltt", 128, struct.pack(">h", 1) + bytes(14) + entry)],
              model=model, depth=depth)
    w = window(g, color=model == "ii")
    glue_palette.new_window(g, None, 128)
    glue_palette.new_window(g, w, 128)                  # a Plus has no 'pltt' for it
    assert (w.addr & 0xFFFFFF in g.process.palettes) == (model == "ii")
    h = palette(g, [ODD, RED], pmAnimated)
    g("NSetPalette", w.addr, h, pmAllUpdates)
    g("ActivatePalette", w.addr)
    g("AnimateEntry", w.addr, 0, rgb(g, BLUE))
    out = g.ptr(bytes(6))
    g("GetEntryColor", h, 0, out)
    assert read_rgb(g, out) == BLUE
    g("RGBForeColor", rgb(g, RED))
    red = w.fg_color
    g("RGBForeColor", rgb(g, BLUE))
    g("PmForeColor", 1)
    assert w.fg_color == red
    assert g("Entry2Index", 1) == g("Color2Index", rgb(g, RED))
    g("SetEntryUsage", h, 1, pmCourteous | pmExplicit, 0)
    assert g("Entry2Index", 1) == g("Color2Index", rgb(g, RED))
    if g.system.devices is not None:
        assert not g.system.devices.reserved


def test_the_palettes_of_windows_that_have_gone_are_forgotten(g):
    back, w = window(g), window(g)
    h = palette(g, [ODD], pmTolerant)
    g("NSetPalette", w.addr, h, pmAllUpdates)
    assert w.addr & 0xFFFFFF in g.process.palettes
    g.process.windows.remove(w)                         # as DisposeWindow takes it out
    g("NSetPalette", back.addr, palette(g, [ODDER], pmTolerant), pmAllUpdates)
    assert w.addr & 0xFFFFFF not in g.process.palettes and g.heap.is_handle(h)


def test_when_the_screen_has_no_index_left_an_entry_goes_without(g):
    """255 animated entries, one more than there are indexes between white
    and black, then tolerant white, black and an odd colour."""
    w = window(g)
    h = palette(g, [ODDEST] * 255 + [WHITE, BLACK, ODD], pmAnimated)
    for e in range(255, 258):
        g("SetEntryUsage", h, e, pmTolerant, 0)
    g("NSetPalette", w.addr, h, pmAllUpdates)
    reserved = g.system.devices.reserved
    assert sorted(reserved) == list(range(1, 255))
    assert sorted(e for _, e in reserved.values()) == list(range(254))
    table = clut(g)
    assert table[0][1:] == WHITE and table[255][1:] == BLACK
    assert ODD not in [c[1:] for c in table]


def test_a_screen_whose_colour_table_isnt_there_keeps_its_colours(g):
    devices = g.system.devices
    pm = devices.pixmap(devices.main)
    ct, w = pm.pm_table, window(g)
    h = palette(g, [ODD], pmTolerant)
    low = low_memory(g)
    pm.pm_table = g.mem.size - 2
    try:
        g("NSetPalette", w.addr, h, pmAllUpdates)
        g("SetEntryUsage", h, 0, pmAnimated, 0)
        g("AnimateEntry", w.addr, 0, rgb(g, ODDER))
        assert not devices.reserved and g.mem.read(0, 0x100) == low
    finally:
        pm.pm_table = ct
    assert ODD not in [c[1:] for c in clut(g)]
