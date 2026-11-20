"""Icons: PlotIcon, the icon families and suites of the Icon Utilities,
colour icons and SICNs, drawn on a 1-bit port; and colour icons on an
8-bit one."""

import struct
from types import SimpleNamespace

import pytest

from macrt.resfork import Resource
from macrt.toolbox.icons import (LARGE, SMALL, atBottom, atHorizontalCenter, atLeft, atNone,
                                 atRight, atTop, atVerticalCenter, best_size, place, ttDisabled,
                                 ttNone, ttSelected)
from macrt.toolbox.menus import MenuInfo
from macrt.toolbox.pixels import rgb_value
from macrt.toolbox.types import Point, Rect
from macrt.ui.menubar import MenuBarWidget

from guest import Guest


def icon(rows: list[int], width: int = 32) -> bytes:
    """An icon's bits, from a row's bits as an int for each row."""
    return b"".join(r.to_bytes(width // 8, "big") for r in rows)


FRAME = icon([0xFFFFFFFF] + [0x80000001] * 30 + [0xFFFFFFFF])
LEFT_HALF = icon([0xFFFF0000] * 32)
SOLID = icon([0xFFFFFFFF] * 32)
EMPTY = bytes(128)
MIDDLE = icon([0] * 8 + [0x00FFFF00] * 16 + [0] * 8)        # rows 8-23, columns 8-23
SMALL_SOLID = icon([0xFFFF] * 16, 16)


def guest(*resources):
    return Guest([Resource(kind.encode(), rid, data) for kind, rid, data in resources])


def black(rows, top, left, bottom, right) -> bool:
    return all(rows[y][x] == "#" for y in range(top, bottom) for x in range(left, right))


def white(rows, top, left, bottom, right) -> bool:
    return all(rows[y][x] == "." for y in range(top, bottom) for x in range(left, right))


@pytest.fixture
def g(qapp):
    return guest(("ICN#", 128, EMPTY + MIDDLE),
                 ("ICN#", 129, FRAME + SOLID), ("ics#", 129, SMALL_SOLID * 2),
                 ("icl8", 130, bytes([0xFF] * 16 + [0] * 16) * 32),
                 ("ICON", 131, LEFT_HALF))


def fill(g, port):
    g("PaintRect", g.rect_ptr((0, 0, port.port_bits_bounds.bottom, port.port_bits_bounds.right)))


# -- PlotIcon -------------------------------------------------------------------
def test_plot_icon_copies_the_whole_icon(g):
    port, _ = g.port()
    fill(g, port)
    h = g("GetIcon", 131)
    g("PlotIcon", g.rect_ptr((0, 0, 32, 32)), h)
    rows = g.rows(port)
    assert all(r[:32] == "#" * 16 + "." * 16 for r in rows)      # white bits too: no mask
    assert black(rows, 0, 32, 32, 64)


def test_plot_icon_scales_to_the_rect(g):
    port, _ = g.port()
    g("PlotIcon", g.rect_ptr((8, 0, 24, 48)), g("GetIcon", 131))   # half high, wider
    rows = g.rows(port)
    assert all(rows[y][:48] == "#" * 24 + "." * 24 for y in range(8, 24))
    assert white(rows, 0, 0, 8, 64) and white(rows, 24, 0, 32, 64)


def test_plot_icon_takes_a_handle_the_program_made(g):
    """The ROM only dereferences the handle: Leisure Suit Larry passes a
    pointer to a pointer to the bits, all on its stack."""
    port, _ = g.port()
    stack = 0x2800                                  # in no heap
    with pytest.raises(ValueError):
        g.system.heap_of(stack)
    g.mem.write(stack, LEFT_HALF)
    g.mem.w32(stack + 128, stack)
    g("PlotIcon", g.rect_ptr((0, 0, 32, 32)), stack + 128)
    assert all(r[:32] == "#" * 16 + "." * 16 for r in g.rows(port)[:32])


def test_plot_icon_takes_a_master_pointer_moved_into_its_block(qapp):
    """Icon Exchanger points a resource's master pointer at an icon inside
    it, past the block's start, and plots that."""
    g = guest(("ICON", 132, EMPTY + LEFT_HALF))
    port, _ = g.port()
    h = g("GetIcon", 132)
    g.mem.w32(h, g.mem.r32(h) + 128)
    g("PlotIcon", g.rect_ptr((0, 0, 32, 32)), h)
    assert all(r[:32] == "#" * 16 + "." * 16 for r in g.rows(port)[:32])


# -- families -------------------------------------------------------------------
def test_plot_icon_id_draws_through_the_mask(g):
    port, _ = g.port()
    fill(g, port)
    assert g("PlotIconID", g.rect_ptr((0, 0, 32, 32)), atNone, ttNone, 128) == 0
    rows = g.rows(port)
    assert white(rows, 8, 8, 24, 24)
    assert black(rows, 0, 0, 8, 64) and black(rows, 24, 0, 32, 64) and black(rows, 8, 0, 24, 8)
    assert g("PlotIconID", g.rect_ptr((0, 0, 32, 32)), atNone, ttNone, 999) == -192


def test_the_size_drawn_is_the_rects(g):
    port, _ = g.port()
    g("PlotIconID", g.rect_ptr((0, 0, 32, 32)), atNone, ttNone, 129)    # the ICN#: a frame
    g("PlotIconID", g.rect_ptr((0, 40, 16, 56)), atNone, ttNone, 129)   # the ics#: solid
    rows = g.rows(port)
    assert rows[0][:32] == "#" * 32 and rows[1][:32] == "#" + "." * 30 + "#"
    assert black(rows, 0, 40, 16, 56)
    # Only a large icon: shrunk to fit.
    g("PlotIconID", g.rect_ptr((16, 40, 32, 56)), atNone, ttNone, 128)
    assert best_size(Rect(0, 0, 16, 16), lambda size: size == LARGE) == LARGE
    assert best_size(Rect(0, 0, 40, 40), lambda size: size == SMALL) == SMALL
    assert best_size(Rect(0, 0, 40, 40), lambda size: False) is None


def test_alignment():
    r = Rect(0, 0, 32, 64)
    assert place(r, 32, 32, atNone) == r
    assert place(r, 32, 32, atVerticalCenter | atHorizontalCenter) == Rect(0, 16, 32, 48)
    assert place(r, 32, 32, atTop | atLeft) == Rect(0, 0, 32, 32)
    assert place(r, 32, 32, atRight) == Rect(0, 32, 32, 64)
    assert place(Rect(0, 0, 64, 32), 16, 16, atBottom) == Rect(32, 0, 64, 32)


def test_an_aligned_icon_keeps_its_shape(g):
    port, _ = g.port()
    g("PlotIconID", g.rect_ptr((0, 0, 32, 64)), atRight, ttNone, 129)
    rows = g.rows(port)
    assert white(rows, 0, 0, 32, 32) and rows[0][32:] == "#" * 32 and rows[5][32:33] == "#"


def test_transforms(g):
    port, _ = g.port()
    g("PlotIconID", g.rect_ptr((0, 0, 32, 32)), atNone, ttSelected, 129)
    rows = g.rows(port)
    assert rows[0][:32] == "." * 32 and black(rows, 1, 1, 31, 31)      # inverted in its mask
    g("PlotIconID", g.rect_ptr((0, 32, 16, 48)), atNone, ttDisabled, 129)   # solid, greyed
    rows = g.rows(port)
    assert rows[0][32:48] == "#." * 8 and rows[1][32:48] == ".#" * 8


def test_a_colour_only_family_is_made_one_bit(g):
    port, _ = g.port()
    fill(g, port)
    g("PlotIconID", g.rect_ptr((0, 0, 32, 32)), atNone, ttNone, 130)
    rows = g.rows(port)
    assert all(r[:32] == "#" * 16 + "." * 16 for r in rows)      # no mask: all of it


def test_icon_regions_and_hit_testing(g):
    rgn = g("NewRgn")
    r = g.rect_ptr((0, 0, 32, 32))
    assert g("IconIDToRgn", rgn, r, atNone, 128) == 0
    assert g.rect(g.heap.deref(rgn) + 2) == Rect(8, 8, 24, 24)
    assert g("PtInIconID", Point(10, 10), r, atNone, 128)
    assert not g("PtInIconID", Point(2, 2), r, atNone, 128)
    assert g("RectInIconID", g.rect_ptr((0, 0, 9, 9)), r, atNone, 128)
    assert not g("RectInIconID", g.rect_ptr((0, 0, 8, 8)), r, atNone, 128)


# -- suites ---------------------------------------------------------------------
def test_icon_suites(g):
    port, _ = g.port()
    out = g.ptr(bytes(4))
    assert g("GetIconSuite", out, 129, 0xFFFFFFFF) == 0
    suite = g.mem.r32(out)
    g("PlotIconSuite", g.rect_ptr((0, 0, 32, 32)), atNone, ttNone, suite)
    g("PlotIconSuite", g.rect_ptr((0, 40, 16, 56)), atNone, ttNone, suite)
    rows = g.rows(port)
    assert rows[1][:32] == "#" + "." * 30 + "#" and black(rows, 0, 40, 16, 56)

    assert g("GetSuiteLabel", suite) == 0
    g("SetSuiteLabel", suite, 3)
    assert g("GetSuiteLabel", suite) == 3

    assert g("GetIconFromSuite", out, suite, int.from_bytes(b"icl8", "big")) == -50
    assert g.mem.r32(out) == 0
    mine = g.handle(bytes([0xFF] * 1024))
    assert g("AddIconToSuite", mine, suite, int.from_bytes(b"icl8", "big")) == 0
    assert g("GetIconFromSuite", out, suite, int.from_bytes(b"icl8", "big")) == 0
    assert g.mem.r32(out) == mine
    assert g("GetIconFromSuite", out, suite, int.from_bytes(b"ICN#", "big")) == 0
    resource = g.mem.r32(out)

    assert g("DisposeIconSuite", suite, True) == 0
    assert not g.heap.is_handle(mine) and not g.heap.is_handle(suite)
    assert g.contents(resource) == FRAME + SOLID           # still the Resource Manager's


def ostype(kind: bytes) -> int:
    return int.from_bytes(kind, "big")


def test_for_each_icon_do_calls_the_action_for_each_type_the_selector_names(g):
    """More Macintosh Toolbox, Icon Utilities: with the suite's handle for
    each, NIL for a type it hasn't; what the action leaves in its VAR
    theIcon goes in the suite, and a result other than noErr stops it."""
    out = g.ptr(bytes(4))
    g("GetIconSuite", out, 129, 0xFFFFFFFF)
    suite = g.mem.r32(out)
    g("GetIconFromSuite", out, suite, ostype(b"ICN#"))
    icn, mine, action = g.mem.r32(out), g.handle(bytes(1024)), 0xF00000
    seen = []

    def act(kind, var, data):
        seen.append((kind.to_bytes(4, "big"), g.mem.r32(var), data))
        if kind == ostype(b"icl8"):
            g.mem.w32(var, mine)
    g.procs[action] = act
    assert g("ForEachIconDo", suite, 0x107, action, 0x1234) == 0  # the large ones and the ics#
    assert [kind for kind, _, _ in seen] == [b"ICN#", b"icl4", b"icl8", b"ics#"]
    assert seen[0][1:] == (icn, 0x1234) and seen[1][1] == 0 and seen[3][1]
    g("GetIconFromSuite", out, suite, ostype(b"icl8"))
    assert g.mem.r32(out) == mine
    seen.clear()
    g.procs[action] = lambda kind, var, data: seen.append(kind) or -1
    assert g("ForEachIconDo", suite, 0xFFFFFFFF, action, 0) == -1 and len(seen) == 1
    assert g("ForEachIconDo", g.handle(b""), 1, action, 0) == -50 and len(seen) == 1


def test_a_suite_of_only_some_icons(g):
    port, _ = g.port()
    out = g.ptr(bytes(4))
    g("GetIconSuite", out, 129, 0x100)                     # svSmall1Bit: just the ics#
    g("PlotIconSuite", g.rect_ptr((0, 0, 32, 32)), atNone, ttNone, g.mem.r32(out))
    assert black(g.rows(port), 0, 0, 32, 32)               # the small one, stretched
    g("NewIconSuite", out)
    assert g("PlotIconSuite", g.rect_ptr((0, 0, 32, 32)), atNone, ttNone, g.mem.r32(out)) == 0


def test_a_suite_draws_an_icon_purged_from_memory(g):
    """An empty resource handle in a suite is loaded again to be drawn
    (Script Editor's toolbar), and DisposeIconSuite leaves it alone."""
    port, _ = g.port()
    out = g.ptr(bytes(4))
    g("GetIconSuite", out, 129, 0xFFFFFFFF)
    suite = g.mem.r32(out)
    g("GetIconFromSuite", out, suite, int.from_bytes(b"ICN#", "big"))
    resource = g.mem.r32(out)
    g("EmptyHandle", resource)
    assert g.mem.r32(resource) == 0
    assert g.process.resources.is_resource(resource)
    assert not g.process.resources.is_resource(g.handle(b"mine"))
    g("PlotIconSuite", g.rect_ptr((0, 0, 32, 32)), atNone, ttNone, suite)
    assert g.rows(port)[1][:32] == "#" + "." * 30 + "#"
    assert g("DisposeIconSuite", suite, True) == 0
    assert g.process.resources.is_resource(resource)


def test_a_menu_title_can_be_an_icon_suite(qapp):
    """A title of $01 and an icon suite's handle shows the suite's small
    icon (Q&A TB 06): BBEdit 3.1's two menus at the bar's right."""
    g = guest(("ics#", 129, icon([0xFF00] * 16, 16) + SMALL_SOLID))
    out = g.ptr(bytes(4))
    g("GetIconSuite", out, 129, 0x100)
    h = g("NewMenu", 300, g.ptr(b"\x05\x01" + g.mem.read(out, 4)))
    menu = MenuInfo(g.system, h)
    assert menu.title_suite == g.mem.r32(out)
    bar = MenuBarWidget(SimpleNamespace(application_menu_for=lambda bar: None))
    try:
        bar.resize(300, bar.height())
        bar.process = SimpleNamespace(bar_menus=lambda: [menu])
        (_, cell), = bar.cells()
        x = cell.left() + (cell.width() - 16) // 2
        y = cell.top() + (cell.height() - 16) // 2

        def at(img, dx):
            return img.pixel(x + dx, y + 8) & 0xFFFFFF
        img = bar.image()
        assert at(img, 2) == 0 and at(img, 12) == 0xFFFFFF
        bar.hilite_id = 300                                 # inverted within its mask
        img = bar.image()
        assert at(img, 2) == 0xFFFFFF and at(img, 12) == 0
    finally:
        bar.deleteLater()


def test_an_apple_in_a_menu_title_of_text_is_the_apple_menus(qapp):
    """Chicago's $14 is its apple wherever it is in a title: TME's first
    menu is a space and an apple, which looks like the Apple menu's."""
    g = guest()

    def black(title):
        menu = MenuInfo(g.system, g("NewMenu", 300, g.ptr(bytes([len(title)]) + title)))
        bar = MenuBarWidget(SimpleNamespace(application_menu_for=lambda bar: None))
        try:
            bar.resize(300, bar.height())
            bar.process = SimpleNamespace(bar_menus=lambda: [menu])
            (_, cell), = bar.cells()
            img = bar.image()
            dots = {(x, y) for x in range(cell.left(), cell.right() + 1)
                    for y in range(cell.top(), cell.bottom() + 1)
                    if img.pixel(x, y) & 0xFFFFFF == 0}
        finally:
            bar.deleteLater()
        left, top = min(x for x, _ in dots), min(y for _, y in dots)
        return {(x - left, y - top) for x, y in dots}

    assert black(b" \x14") == black(b"\x14")


# -- colour icons and SICNs -----------------------------------------------------
def cicn(with_bitmap: bool) -> bytes:
    """A 16-by-16 cicn at 8 bits: its mask the left 12 columns, its BitMap
    the left 4 columns black, its pixels black in the left 8."""
    bounds = struct.pack(">hhhh", 0, 0, 16, 16)
    pixmap = (struct.pack(">LH", 0, 0x8000 | 16) + bounds
              + struct.pack(">hhlllhhhhlll", 0, 0, 0, 72 << 16, 72 << 16, 0, 8, 1, 8, 0, 0, 0))
    mask = struct.pack(">LH", 0, 2) + bounds
    bmap = struct.pack(">LH", 0, 2 if with_bitmap else 0) + (bounds if with_bitmap else bytes(8))
    ctab = struct.pack(">lHh", 0, 0, 1) + struct.pack(">HHHH", 0, 0xFFFF, 0xFFFF, 0xFFFF) \
        + struct.pack(">HHHH", 1, 0, 0, 0)
    data = pixmap + mask + bmap + bytes(4)
    data += icon([0xFFF0] * 16, 16)
    if with_bitmap:
        data += icon([0xF000] * 16, 16)
    return data + ctab + bytes([1] * 8 + [0] * 8) * 16


def test_colour_icons(qapp):
    g = guest(("cicn", 128, cicn(True)), ("cicn", 129, cicn(False)))
    port, _ = g.port()
    fill(g, port)
    h = g("GetCIcon", 128)
    p = g.heap.deref(h)
    ctab, pixels = g.mem.r32(p + 42), g.mem.r32(p + 78)
    assert g.heap.handle_size(ctab) == 8 + 2 * 8 and g.heap.handle_size(pixels) == 256
    assert g.mem.r32(p + 50) == p + 82                     # the mask's bits, in the record
    g("PlotCIcon", g.rect_ptr((0, 0, 16, 16)), h)
    rows = g.rows(port)
    assert all(r[:16] == "####" + "." * 8 + "####" for r in rows[:16])   # the BitMap, masked
    g("DisposeCIcon", h)
    assert not any(map(g.heap.is_handle, (h, ctab, pixels)))

    # With no BitMap, the pixels, as light or dark.
    h = g("GetCIcon", 129)
    assert g("PlotCIconHandle", g.rect_ptr((16, 0, 32, 32)), atNone, ttNone, h) == 0
    rows = g.rows(port)
    assert rows[20][:32] == "#" * 16 + "." * 8 + "#" * 8          # stretched to 32 wide
    assert g("GetCIcon", 999) == 0


def red_cicn() -> bytes:
    """A 4-by-4 cicn at 8 bits: its pixels red but for a black left
    column; its mask all but the right column; its BitMap all black."""
    bounds = struct.pack(">hhhh", 0, 0, 4, 4)
    pixmap = (struct.pack(">LH", 0, 0x8000 | 4) + bounds
              + struct.pack(">hhlllhhhhlll", 0, 0, 0, 72 << 16, 72 << 16, 0, 8, 1, 8, 0, 0, 0))
    data = pixmap + struct.pack(">LH", 0, 2) + bounds + struct.pack(">LH", 0, 2) + bounds
    data += bytes(4) + icon([0xE000] * 4, 16) + icon([0xF000] * 4, 16)
    ctab = struct.pack(">lHh", 0, 0, 1) + struct.pack(">HHHH", 0, 0xFFFF, 0, 0) \
        + struct.pack(">HHHH", 1, 0, 0, 0)
    return data + ctab + bytes([1, 0, 0, 0]) * 4


def test_colour_icons_draw_their_pixels_on_a_colour_screen(qapp):
    """Into a world whose pixels the program moves between plots, as
    Foobar Versus the DEA makes its sprites; not its BitMap, the screen
    having 8 bits."""
    g = Guest([Resource(b"cicn", 128, red_cicn())], model="ii", depth=8)
    out = g.ptr(bytes(4))
    assert g("NewGWorld", out, 8, g.rect_ptr((0, 0, 4, 8)), 0, 0, 0) == 0
    world = g.mem.r32(out)
    g("SetGWorld", world, 0)
    pm = g.mem.r32(g("GetGWorldPixMap", world))
    elsewhere = g.ptr(bytes(4 * 8))
    g.mem.w32(pm, elsewhere)
    h = g("GetCIcon", 128)
    g("PlotCIcon", g.rect_ptr((0, 0, 4, 4)), h)
    table = g.system.devices.table()
    red, black = rgb_value(0xFFFF0000, 8, table), rgb_value(0xFF000000, 8, table)
    assert [g.mem.read(elsewhere + 8 * v, 8) for v in range(4)] == \
        [bytes([black, red, red, 0, 0, 0, 0, 0])] * 4        # the right column masked off


def test_sicn(g):
    port, _ = g.port()
    fill(g, port)
    h = g.handle(icon([0] * 16, 16) + icon([0xFF00] * 16, 16))
    assert g("PlotSICNHandle", g.rect_ptr((0, 0, 16, 16)), atNone, ttNone, h) == 0
    rows = g.rows(port)
    assert all(r[:16] == "." * 8 + "#" * 8 for r in rows[:16])
    assert g("PlotIconHandle", g.rect_ptr((0, 32, 32, 64)), atNone, ttNone,
             g.handle(EMPTY + MIDDLE)) == 0
    rows = g.rows(port)
    assert white(rows, 8, 40, 24, 56) and rows[0][32:] == "#" * 32


# -- Colour suite icons ---------------------------------------------------------
from macrt.toolbox.color import STANDARD_4BIT, STANDARD_8BIT   # noqa: E402
from macrt.toolbox.icons import color_suite_icon               # noqa: E402

COLOUR_SMALL = {                        # 16 by 16 members of a suite
    "ics#": b"\xcc" * 32 + b"\xff" * 32,
    "ics4": b"\x11" * 128,              # every pixel colour 1
    "ics8": b"\xd7" * 256,              # every pixel colour 215
}
COLOUR_LARGE = {                        # 32 by 32 members
    "ICN#": b"\xf0" * 128 + b"\xff" * 128,
    "icl4": b"\x11" * 512,
    "icl8": b"\xd7" * 1024,
}


def colour_suite(**overrides):
    data = COLOUR_SMALL | COLOUR_LARGE | overrides
    return lambda kind: data.get(kind)


def test_a_colour_icon_needs_more_than_two_bits(qapp):
    assert color_suite_icon(colour_suite(), LARGE, 1) is None
    assert color_suite_icon(colour_suite(), LARGE, 2) is None


def test_a_colour_icon_at_depth_8_is_the_8bit_member(qapp):
    icon = color_suite_icon(colour_suite(), LARGE, 8)
    assert icon is not None
    assert (icon.width, icon.height) == (32, 32)
    px = STANDARD_8BIT[0xd7]
    assert icon.pixels.pixelColor(0, 0).rgb() == px


def test_a_colour_icon_at_depth_8_keeps_the_4bit_member_as_fallback(qapp):
    icon = color_suite_icon(colour_suite(icl8=None), LARGE, 8)
    assert icon is not None
    assert icon.pixels.pixelColor(0, 0).rgb() == STANDARD_4BIT[0x1]


def test_a_colour_icon_at_depth_4_is_the_4bit_member(qapp):
    icon = color_suite_icon(colour_suite(), LARGE, 4)
    assert icon is not None
    assert icon.pixels.pixelColor(0, 0).rgb() == STANDARD_4BIT[0x1]


def test_a_colour_icon_at_depth_4_never_uses_the_8bit_member(qapp):
    icon = color_suite_icon(colour_suite(icl4=None, ics4=None), LARGE, 4)
    assert icon is None
    icon = color_suite_icon(colour_suite(), SMALL, 4)
    assert icon is not None


def test_a_suite_without_colour_members_gives_none(qapp):
    get = colour_suite(icl4=None, icl8=None, ics4=None, ics8=None)
    assert color_suite_icon(get, LARGE, 8) is None
    assert color_suite_icon(get, SMALL, 4) is None


def test_a_short_colour_member_is_not_used(qapp):
    icon = color_suite_icon(colour_suite(icl8=b"\xd7" * 100), LARGE, 8)
    assert icon is not None                     # the 4-bit one takes over
    assert icon.pixels.pixelColor(0, 0).rgb() == STANDARD_4BIT[0x1]


def test_the_masks_clear_bits_become_transparency(qapp):
    # The mask's first row (its first 2 of 32 bytes for a small icon) is clear.
    small = COLOUR_SMALL | {"ics#": b"\xcc" * 32 + b"\x00\x00" + b"\xff" * 30}
    icon = color_suite_icon(lambda kind: small.get(kind), SMALL, 8)
    assert icon.pixels.pixelColor(0, 0).alpha() == 0
    assert icon.pixels.pixelColor(0, 1).alpha() == 255
    large = COLOUR_LARGE | {"ICN#": b"\xf0" * 128 + b"\x00\x00\x00\x00" + b"\xff" * 124}
    icon = color_suite_icon(lambda kind: large.get(kind), LARGE, 8)
    assert icon.pixels.pixelColor(0, 0).alpha() == 0
    assert icon.pixels.pixelColor(0, 1).alpha() == 255


def test_a_suite_without_a_1bit_sibling_keeps_no_alpha(qapp):
    get = colour_suite(**{"ics#": None, "ICN#": None})
    icon = color_suite_icon(get, SMALL, 8)
    assert icon is not None
    assert icon.mask is None
    assert icon.pixels.pixelColor(0, 0).alpha() == 255
