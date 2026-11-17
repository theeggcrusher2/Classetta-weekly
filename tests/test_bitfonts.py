"""An application's own bitmap fonts, drawn from their strikes; the
font records the Font Manager leaves in low memory; and the System's
pattern list."""

import struct

import machfs

from PyQt6.QtGui import QFontMetrics

from macrt.macfile import MacFile, fonts_beside, load_volume, system_on
from macrt.resfork import Resource, build_fork
from macrt.toolbox import lowmem, qdraw, sysres
from macrt.toolbox.bitfont import Strike
from macrt.toolbox.menus import Menu, parse_menu_items
from macrt.ui import theme
from macrt.ui.menubar import Pulldown, _title_width

from builders import font_rec
from guest import Guest

A = ([".##.",
      "#..#",
      "####",
      "#..#",
      "#..#",
      "...."], 0, 5)
C = ([".###",
      "#...",
      "#...",
      "#...",
      ".###",
      "...."], 0, 5)
BOX = (["###",
        "#.#",
        "#.#",
        "#.#",
        "###",
        "..."], 0, 4)


def blocky(**kw) -> bytes:
    """A and C, B missing; ascent 5, descent 1."""
    return font_rec(65, 67, {65: A, 67: C}, BOX, 5, 1, **kw)


def pixels(img) -> list[str]:
    return ["".join("#" if img.pixelColor(x, y).alpha() else "." for x in range(img.width()))
            for y in range(img.height())]


def test_a_strike_whose_ow_table_stops_at_the_missing_glyph(qapp):
    s = Strike.parse(blocky(final_ow=False))
    assert (s.first, s.last, s.ascent, s.descent, s.wid_max) == (65, 67, 5, 1, 5)
    assert s.width(b"AC") == 10
    assert s.char_width(66) == 4 and s.char_width(32) == 4         # the missing glyph
    assert s.width(b"AC", face=1) == 12                             # bold: a pixel more each
    assert Strike.parse(blocky()[:40]) is None


def test_a_strike_renders_its_glyphs_side_by_side(qapp):
    img, pen, baseline = Strike.parse(blocky()).render(b"AB")
    assert (pen, baseline) == (0, 5)
    assert pixels(img) == [".##..###.",
                           "#..#.#.#.",
                           "####.#.#.",
                           "#..#.#.#.",
                           "#..#.###.",
                           "........."]


def test_kern_max_and_the_glyph_offset_move_it_from_the_pen(qapp):
    data = font_rec(65, 65, {65: (A[0], 2, 5)}, BOX, 5, 1, kern_max=-1)
    img, pen, _ = Strike.parse(data).render(b"A")
    assert pen == 0 and pixels(img)[2] == ".####"                    # at pen - 1 + 2


def drawing(resources, family=200, size=12, v=7):
    g = Guest(resources)
    port, _ = g.port(48, 24)
    g("TextFont", family)
    g("TextSize", size)
    g("MoveTo", 2, v)
    g("DrawString", g.ptr(b"\x02AC"))
    return g, port


def test_drawing_in_an_applications_font(qapp):
    g, port = drawing([Resource(b"FONT", 200 << 7, b"", b"Blocky"),
                       Resource(b"FONT", 200 << 7 | 12, blocky())])
    assert [row[:14] for row in g.rows(port)[2:9]] == ["...##...###...",
                                                       "..#..#.#......",
                                                       "..####.#......",
                                                       "..#..#.#......",
                                                       "..#..#..###...",
                                                       "..............",
                                                       ".............."]
    assert port.pn_loc.h == 12
    assert g("StringWidth", g.ptr(b"\x02AC")) == 10
    info = g.ptr(bytes(8))
    g("GetFontInfo", info)
    assert struct.unpack(">4h", g.mem.read(info, 8)) == (5, 1, 5, 0)


def test_measure_text_and_text_box_go_by_the_applications_strike(qapp):
    # The Oregon Trail breaks its messages into lines with MeasureText and
    # labels its thermometer with TETextBox, both in its own font.
    g, port = drawing([Resource(b"FONT", 200 << 7, b"", b"Blocky"),
                       Resource(b"FONT", 200 << 7 | 12, blocky())])
    locs = g.ptr(bytes(8))
    g("MeasureText", 3, g.ptr(b"ACA"), locs)
    assert struct.unpack(">4h", g.mem.read(locs, 8)) == (0, 5, 10, 15)
    g("TextFace", 1)
    g("MeasureText", 2, g.ptr(b"AC"), locs)
    assert struct.unpack(">3h", g.mem.read(locs, 6)) == (0, 6, 12)
    g("TextFace", 0)
    g("TETextBox", g.ptr(b"AC AC"), 5, g.ptr(struct.pack(">4h", 0, 0, 24, 12)), 0)
    rows = [row[:12] for row in g.rows(port)]
    assert rows[0][:9] == rows[6][:9] == ".##...###"    # a line each, 6 pixels apart
    assert rows[12] == "." * 12


def test_an_nfnt_a_fond_lists(qapp):
    g, port = drawing([Resource(b"FOND", 300, sysres.fond(300, (9,), ids=(3000,)), b"Blocky"),
                       Resource(b"NFNT", 3000, blocky())], family=300, size=9)
    assert g.rows(port)[4][:8] == "..####.#"
    assert port.pn_loc.h == 12


def test_an_nfnt_a_fond_lists_only_in_another_style(qapp):
    """A size the FOND has only in a style is drawn from that strike: the
    Font Manager gives the one closest to the style asked for (Inside
    Macintosh IV, The Font Manager).  Treasure MathStorm!'s Futura Book
    is bold and nothing else."""
    fond = bytearray(sysres.fond(300, (9,), ids=(3000,)))
    struct.pack_into(">h", fond, 56, 1)                 # bold
    g, port = drawing([Resource(b"FOND", 300, bytes(fond), b"Blocky"),
                       Resource(b"NFNT", 3000, blocky())], family=300, size=9)
    assert g.rows(port)[4][:8] == "..####.#"
    assert port.pn_loc.h == 12


def test_a_size_it_hasnt_got_is_scaled_from_one_it_has(qapp):
    g, port = drawing([Resource(b"FONT", 200 << 7 | 9, blocky())], size=18, v=12)
    assert g("StringWidth", g.ptr(b"\x02AC")) == 20
    rows = g.rows(port)
    assert rows[2][:20] == "....####......######" and rows[3] == rows[2]   # doubled
    assert port.pn_loc.h == 22


def test_the_standard_families_stay_host_fonts(qapp):
    """but are measured with a FONT of theirs if there is one: a real
    System file's Chicago 12, whose digits the Alarm Clock adds up."""
    g = Guest()
    rm = g.process.resources
    rm.UseResFile(0)
    rm.AddResource(g.handle(blocky()), "FONT", 3 << 7 | 12)
    port, _ = g.port(48, 24)
    g("TextFont", 3)
    g("TextSize", 12)
    g("MoveTo", 2, 7)
    g("DrawString", g.ptr(b"\x02AC"))
    assert g.tb.qd.strike(port) is None
    assert port.pn_loc.h == 2 + 10
    assert g("StringWidth", g.ptr(b"\x02AC")) == 10
    assert g("CharWidth", ord("B")) == 4            # the missing glyph's
    info = g.ptr(bytes(8))
    g("GetFontInfo", info)
    assert struct.unpack(">4h", g.mem.read(info, 8)) == (5, 1, 5, 0)
    assert output(g)["handle"] == rm.GetResource("FONT", 3 << 7 | 12)
    g("TextFace", 1)
    assert g("StringWidth", g.ptr(b"\x02AC")) == 12  # bold: a pixel more each
    g("TextFace", 0)
    g("TextFont", 1)                                # the application font: Geneva
    assert g("StringWidth", g.ptr(b"\x02AC")) == 10
    g("TextSize", 9)                                # a size it hasn't: the host font's
    assert g("StringWidth", g.ptr(b"\x02AC")) == sum(g.tb.qd.fonts.advances(3, 9, 0, "AC"))


def test_lines_are_as_far_apart_as_the_disks_system_sets_them(qapp):
    """A host font stands in for the Chicago 12 of the System on the
    application's disk, at its own widths, but GetFontInfo gives the
    strike's ascent, descent and leading, which programs space lines by:
    The Venice Project's "Enter the word ... in the box above" goes under
    the box.  A FOND there can list the strike."""
    disk = MacFile("System", type=b"ZSYS", rsrc=build_fork([
        Resource(b"FONT", 12, blocky(leading=2)),               # Chicago 12
        Resource(b"FOND", 3, sysres.fond(3, (9,), (1000,)), b"Geneva"),
        Resource(b"NFNT", 1000, blocky(leading=1)),             # Geneva 9
    ]))
    g = Guest(disk_system=disk)
    port, _ = g.port(48, 24)
    fonts = g.tb.qd.fonts
    info = g.ptr(bytes(8))

    def font_info():
        g("GetFontInfo", info)
        return struct.unpack(">4h", g.mem.read(info, 8))

    g("TextFont", 0)
    assert g.tb.qd.strike(port) is None
    assert font_info() == (5, 1, QFontMetrics(fonts.font(0, 12, 0)).maxWidth(), 2)
    assert g("StringWidth", g.ptr(b"\x02AC")) == sum(fonts.advances(0, 12, 0, "AC"))
    g("TextFont", 1)                                # the application font: Geneva
    g("TextSize", 9)
    ascent, descent, _, leading = font_info()
    assert (ascent, descent, leading) == (5, 1, 1)
    g("TextSize", 10)                               # a size it hasn't: the host font's
    fm = QFontMetrics(fonts.font(3, 10, 0))
    assert font_info() == (fm.ascent(), fm.descent(), fm.maxWidth(), max(fm.leading(), 0))


def test_a_suitcase_in_the_fonts_folder_has_the_strike(qapp):
    """System 7.5's Chicago 12 is in a suitcase in the Fonts folder, the
    FOND that lists it in the System file: lines of it are 16 pixels
    apart, and Netscape Navigator 2.02's own MDEF spaces its items so."""
    v = machfs.Volume()
    v.name = "Disk"
    v["System Folder"] = machfs.Folder()
    v["System Folder"]["Fonts"] = machfs.Folder()
    system, chicago, other = machfs.File(), machfs.File(), machfs.File()
    system.type, system.creator = b"zsys", b"MACS"
    system.rsrc = build_fork([Resource(b"FOND", 0, sysres.fond(0, (12,), (1025,)), b"Chicago")])
    chicago.type, chicago.creator = b"FFIL", b"DMOV"
    chicago.rsrc = build_fork([Resource(b"FOND", 0, sysres.fond(0, (12,), (1025,)), b"Chicago"),
                               Resource(b"NFNT", 1025, blocky(leading=3))])
    other.type, other.rsrc = b"TEXT", build_fork([Resource(b"NFNT", 1025, blocky())])
    v["System Folder"]["System"] = system
    v["System Folder"]["Fonts"]["Chicago"] = chicago
    v["System Folder"]["Fonts"]["Read Me"] = other
    disk = system_on(load_volume(v.write(800 * 1024, desktopdb=False, bootable=False)))
    suitcases = fonts_beside(disk)
    assert [f.name for f in suitcases] == ["Chicago"]
    assert fonts_beside(MacFile("System", type=b"zsys")) == []
    g = Guest(disk_system=disk, disk_fonts=suitcases)
    assert g.system.font_lines[0, 12] == (5, 1, 3)
    g.port(48, 24)
    info = g.ptr(bytes(8))
    g("TextFont", 0)
    g("GetFontInfo", info)
    ascent, descent, _, leading = struct.unpack(">4h", g.mem.read(info, 8))
    assert (ascent, descent, leading) == (5, 1, 3)


def test_a_real_systems_fonts_folder_has_its_fonts(qapp):
    """System 7.5's System file has Geneva 9 and 12, and the Geneva in its
    Fonts folder the other sizes and the FOND that lists them all; Courier
    and Helvetica are only there.  HyperCard Player's stacks ask for them."""
    def suitcase(*resources):
        f = machfs.File()
        f.type, f.creator, f.rsrc = b"FFIL", b"DMOV", build_fork(list(resources))
        return f

    v = machfs.Volume()
    v.name = "Disk"
    v["System Folder"] = machfs.Folder()
    v["System Folder"]["Fonts"] = fonts = machfs.Folder()
    system = machfs.File()
    system.type, system.creator = b"zsys", b"MACS"
    system.rsrc = build_fork([Resource(b"FOND", 3, sysres.fond(3, (12,)), b"Geneva"),
                              Resource(b"FONT", 3 << 7 | 12, blocky())])
    v["System Folder"]["System"] = system
    fonts["Geneva"] = suitcase(
        Resource(b"FOND", 3, sysres.fond(3, (10, 12), (2000, 2001)), b"Geneva"),
        Resource(b"NFNT", 2000, blocky(leading=3)), Resource(b"NFNT", 2001, blocky()),
        Resource(b"sfnt", 2002, bytes(64)))
    fonts["Courier"] = suitcase(
        Resource(b"FOND", 22, sysres.fond(22, (0,), (2003,)), b"Courier"),
        Resource(b"sfnt", 2003, bytes(64)))
    fonts["Cairo"] = suitcase(
        Resource(b"FOND", 11, sysres.fond(11, (18,), (2004,)), b"Cairo"),
        Resource(b"NFNT", 2004, blocky()), Resource(b"NFNT", 2005, blocky()))
    real = system_on(load_volume(v.write(800 * 1024, desktopdb=False, bootable=False)))
    g = Guest(system_file=real)
    m = g.system.sys_map
    assert m.find(b"sfnt", 2002) is None and m.find(b"NFNT", 2005) is None

    def number(name: bytes) -> int:
        num = g.ptr(bytes(2))
        g("GetFNum", g.ptr(bytes([len(name)]) + name), num)
        return g.mem.r16(num)

    assert (number(b"Courier"), number(b"Cairo"), number(b"Helvetica")) == (22, 11, 0)
    assert g("RealFont", 3, 10) and g("RealFont", 3, 12) and not g("RealFont", 3, 14)
    assert g("RealFont", 22, 14)                    # an outline font: any size
    g.port(48, 24)
    info = g.ptr(bytes(8))
    g("TextFont", 3)
    g("TextSize", 10)                               # measured with the suitcase's strike
    g("GetFontInfo", info)
    ascent, descent, _, leading = struct.unpack(">4h", g.mem.read(info, 8))
    assert (ascent, descent, leading) == (5, 1, 3)
    port, _ = g.port(48, 24)
    g("TextFont", 11)                               # drawn from its strike
    g("TextSize", 18)
    g("MoveTo", 2, 7)
    g("DrawString", g.ptr(b"\x02AC"))
    assert g.rows(port)[4][:8] == "..####.#"


def test_the_disks_outline_fonts_are_real_at_any_size(qapp):
    """With no System file given, the standard families the disk has as
    outline fonts, in its System file or a suitcase beside it, are real
    at almost any size, as they are beside a real System file: ATPM's
    DOCMaker issues ask after Geneva 11 and Helvetica 43."""
    def suitcase(*resources):
        f = machfs.File()
        f.type, f.creator, f.rsrc = b"FFIL", b"DMOV", build_fork(list(resources))
        return f

    v = machfs.Volume()
    v.name = "Disk"
    v["System Folder"] = machfs.Folder()
    v["System Folder"]["Fonts"] = fonts = machfs.Folder()
    system = machfs.File()
    system.type, system.creator = b"zsys", b"MACS"
    system.rsrc = build_fork([Resource(b"FOND", 3, sysres.fond(3, (0, 12), (2002, 3 << 7 | 12)),
                                       b"Geneva"),
                              Resource(b"sfnt", 2002, bytes(64))])
    v["System Folder"]["System"] = system
    fonts["Helvetica"] = suitcase(
        Resource(b"FOND", 21, sysres.fond(21, (0, 10), (2003, 2004)), b"Helvetica"),
        Resource(b"sfnt", 2003, bytes(64)), Resource(b"NFNT", 2004, blocky()))
    disk = system_on(load_volume(v.write(800 * 1024, desktopdb=False, bootable=False)))
    g = Guest(disk_system=disk, disk_fonts=fonts_beside(disk))
    assert g("RealFont", 3, 11) and g("RealFont", 21, 43) and g("RealFont", 3, 9)
    assert not g("RealFont", 2, 11) and g("RealFont", 2, 12)    # New York: bitmaps only
    assert not g("RealFont", 3, 200)
    assert g.system.sys_map.find(b"sfnt", 2002) is None


def test_what_the_font_has_no_width_for_draws_nothing(qapp):
    """NUL and return are blank glyphs of no width in Chicago, Geneva and
    Monaco, where the host font standing in would draw boxes: Social
    Climber pads its scores with NULs."""
    def drawn(text, strike=None):
        g = Guest()
        if strike is not None:
            rm = g.process.resources
            rm.UseResFile(0)
            rm.AddResource(g.handle(strike), "FONT", 3 << 7 | 12)
        port, _ = g.port(64, 24)
        g("TextFont", 3)
        g("TextSize", 12)
        g("MoveTo", 2, 14)
        g("DrawString", g.ptr(bytes([len(text)]) + text))
        return g.rows(port), port.pn_loc.h

    assert drawn(b"A\x00B\rC") == drawn(b"ABC")
    blank = ([""] * 6, 0, 0)
    strike = font_rec(0, 67, {0: blank, 13: blank, 65: A, 67: C}, BOX, 5, 1)
    assert drawn(b"A\x00C\r", strike) == drawn(b"AC", strike)


def test_a_standard_family_the_application_has_its_own_strike_of(qapp):
    """is drawn from it, as on a Mac, where the application's file is
    looked in first: Balance of Power traces its New York 36 title's
    letters from where they are in its own strike."""
    g, port = drawing([Resource(b"FONT", 2 << 7 | 12, blocky())], family=2)
    rm = g.process.resources
    assert g.rows(port)[4][:8] == "..####.#"
    assert port.pn_loc.h == 12
    assert output(g)["handle"] == rm.GetResource("FONT", 2 << 7 | 12)
    g("TextSize", 9)                                # a size it hasn't: the host font
    assert g.tb.qd.strike(port) is None
    g, port = drawing([Resource(b"FONT", 12, blocky())], family=0)
    assert g.rows(port)[4][:8] == "..####.#"        # the system font's too
    assert port.pn_loc.h == 12


def test_the_application_font_is_the_family_apfontid_says(qapp):
    """Gemstone Warrior sets ApFontID to 0 and DlgFont to 1: its dialogs
    are in the Chicago 12 it brings, whose $B9 is a double arrow."""
    g = Guest([Resource(b"FONT", 12, blocky())])
    port, _ = g.port(48, 24)
    g("TextFont", 1)
    assert g.tb.qd.strike(port) is None             # Geneva, a host font
    g.mem.w16(lowmem.ApFontID, 0)
    g("MoveTo", 2, 7)
    g("DrawString", g.ptr(b"\x02AC"))
    assert g.rows(port)[4][:8] == "..####.#"
    assert g("StringWidth", g.ptr(b"\x02AC")) == 10


def test_the_menus_are_drawn_in_the_applications_own_chicago(qapp):
    """A Mac draws the menus in the system font, and the application's own
    Chicago 12 comes first: The Fool's Errand's has Roman numerals and an
    eye where its menus' titles have $80 to $84 and a tilde."""
    g = Guest([Resource(b"FONT", 12, blocky())])
    g.tb._gui = lambda fn, *args: fn(*args)
    g.tb.InsertMenu(g.tb.NewMenu(5, "AC"), 0)
    strike = g.process.system_strike
    assert strike is not None and strike.width(b"AC") == 10
    menu = Menu(5, "AC", parse_menu_items("CA"))
    assert _title_width(menu, QFontMetrics(theme.menu_bar_font()), strike) == 10
    pd = Pulldown(menu, None, strike)
    try:
        img = pd.grab().toImage()
    finally:
        pd.deleteLater()
    x, top = 1 + theme.MENU_LEFT_PAD, 1 + (theme.MENU_ITEM_HEIGHT - 6) // 2
    rows = ["".join("#" if img.pixelColor(x + i, top + j).lightness() < 128 else "."
                    for i in range(10)) for j in range(5)]
    assert rows == [
        ".###..##..",
        "#....#..#.",
        "#....####.",
        "#....#..#.",
        ".###.#..#.",
    ]
    g = Guest()                                     # with none of its own: the host's
    g.tb._gui = lambda fn, *args: fn(*args)
    g.tb.InsertMenu(g.tb.NewMenu(5, "AC"), 0)
    assert g.process.system_strike is None


def test_a_menu_title_with_the_command_mark_shows_it_in_a_host_font(qapp):
    """RoboSport's menu titles are "Pause \u2318P" and "Quit \u2318Q", with
    Chicago's $11 in them: a host font has the mark as Unicode's."""
    metrics = QFontMetrics(theme.menu_bar_font())
    menu = Menu(5, "Pause \x11P", parse_menu_items("Go"))
    assert _title_width(menu, metrics) == metrics.horizontalAdvance("Pause \u2318P")


def test_outline_is_every_pixel_round_the_glyph(qapp):
    """QuickDraw ORs the glyph one pixel each way, corners too, and takes
    the glyph out; shadow reaches a pixel further down and right."""
    dot = (["...", ".#.", "...", "..."], 0, 3)
    s = Strike.parse(font_rec(65, 65, {65: dot}, dot, 3, 1))
    img, _, _ = s.render(b"A", face=8)
    assert pixels(img)[1:4] == [".###..", ".#.#..", ".###.."]
    img, _, _ = s.render(b"A", face=16)
    assert pixels(img)[1:5] == [".####..", ".#.##..", ".####..", ".####.."]


def test_a_family_with_no_host_font_is_drawn_from_its_bitmap(qapp):
    """Cairo is pictures, not letters: Font Mover shows a sample of it with
    the FONT from the Fonts file it's opened."""
    g, port = drawing([Resource(b"FONT", 11 << 7 | 12, blocky())], family=11)
    assert g.rows(port)[4][:8] == "..####.#"
    g("TextFont", 9)                                # Toronto, which it hasn't
    assert g.tb.qd.strike(port) is None


def output(g) -> dict:
    """The Font Manager's output record, FMOutput, at FMgrOutRec."""
    fields = struct.unpack(">hIBBBBBBbBBBbBhhhh", g.mem.read(lowmem.FMgrOutRec, 26))
    names = ("err handle bold italic ul_offset ul_shadow ul_thick shadow extra ascent descent "
             "wid_max leading unused numer_v numer_h denom_v denom_h").split()
    return dict(zip(names, fields))


def test_a_host_fonts_record_has_the_widths_it_draws_with(qapp):
    """Word 1.0 measures text from the FONT whose handle the Font Manager
    leaves in low memory, not with CharWidth: for a family macrt draws with
    a host font, that's a FONT with the host font's widths."""
    g = Guest()
    g.port()
    g("TextFont", 2)
    g("TextSize", 12)
    widths = {c: g("CharWidth", c) for c in b"aWi .\x11"}
    out = output(g)
    font = Strike.parse(g.contents(out["handle"]))
    assert (font.first, font.last) == (0, 255)
    assert {c: font.char_width(c) for c in widths} == widths
    info = g.ptr(bytes(8))
    g("GetFontInfo", info)
    assert (out["ascent"], out["descent"]) == (font.ascent, font.descent) == struct.unpack(
        ">2h", g.mem.read(info, 4))
    assert (out["extra"], out["numer_h"], out["denom_h"]) == (0, 1, 1)
    g("TextFace", 1)                                # bold: its own record, its own widths
    width = g("CharWidth", ord("W"))
    bold = output(g)
    assert bold["handle"] != out["handle"] and bold["bold"] == 1 and bold["extra"] == 0
    assert Strike.parse(g.contents(bold["handle"])).char_width(ord("W")) == width
    g("TextFace", 0)
    g("CharWidth", ord("W"))
    assert output(g)["handle"] == out["handle"]     # made once


def test_an_absurd_text_size_is_measured_at_a_size_a_host_font_can_be(qapp):
    """BMAPEdit's About box once measured text in a port whose txSize was
    two letters of a string, $6773: a host font is made no bigger than
    qdraw.MAX_TEXT_SIZE, and a size below 1 is 1."""
    g = Guest()
    g.port()
    g("TextFont", 3)
    g("TextSize", 0x6773)
    assert g("StringWidth", g.ptr(b"\x02Hi")) > 0
    huge = output(g)
    height = struct.unpack_from(">h", g.contents(huge["handle"]), 14)[0]   # fRectHeight
    assert 0 < height <= qdraw.MAX_TEXT_SIZE * 2
    g("TextSize", qdraw.MAX_TEXT_SIZE)
    g("StringWidth", g.ptr(b"\x02Hi"))
    assert output(g)["handle"] == huge["handle"]    # the same record
    g("TextSize", -12)
    assert g("StringWidth", g.ptr(b"\x02Hi")) >= 0


def test_an_applications_font_is_its_own_record(qapp):
    g, port = drawing([Resource(b"FONT", 200 << 7 | 9, blocky())], size=9)
    rm = g.process.resources
    assert output(g)["handle"] == rm.GetResource("FONT", 200 << 7 | 9)
    g("TextFace", 1)
    g("CharWidth", 65)
    assert output(g)["extra"] == 1                  # bold's pixel, which the widths haven't got
    # FMSwapFont itself, scaling the 9-point font to 18.
    fm_input = g.ptr(struct.pack(">hhBBhII", 200, 18, 0, 0, 0, 0x10001, 0x10001))
    assert g("FMSwapFont", fm_input) == lowmem.FMgrOutRec
    out = output(g)
    assert out["handle"] == rm.GetResource("FONT", 200 << 7 | 9)
    assert (out["numer_h"], out["denom_h"], out["ascent"], out["extra"]) == (18, 9, 10, 0)


def width_table(g) -> tuple[list[int], dict]:
    """The global width table (Inside Macintosh IV, "The Font Manager"):
    its widths, and the rest."""
    h = g.mem.r32(lowmem.WidthTabHandle)
    data = g.contents(h)
    assert g.mem.r32(lowmem.WidthPtr) == g.mem.r32(h)
    names = ("font s_extra style fid fsize face device numer denom afid fhand used_fam aface "
             "v_output h_output v_factor h_factor asize size").split()
    rest = struct.unpack_from(">IllhhhhIIhIBBhhhhhh", data, 1024)
    return list(struct.unpack_from(">256l", data)), dict(zip(names, rest))


def test_the_global_width_table_has_the_widths_text_is_drawn_with(qapp):
    """Microsoft Word 4 adds up character widths from the global width
    table, whose handle FMSwapFont leaves in WidthTabHandle."""
    g = Guest()
    g.port()
    g("TextFont", 2)
    g("TextSize", 12)
    widths = {c: g("CharWidth", c) for c in b"aWi ."}
    table, rest = width_table(g)
    assert {c: table[c] for c in widths} == {c: w << 16 for c, w in widths.items()}
    assert rest["font"] == output(g)["handle"]
    assert [rest[k] for k in "fid fsize face afid asize".split()] == [2, 12, 0, 2, 12]
    assert [rest[k] for k in "h_output v_output h_factor v_factor".split()] == [0x100] * 4
    assert rest["size"] == len(g.contents(g.mem.r32(lowmem.WidthTabHandle))) == 0x430
    h = g.mem.r32(lowmem.WidthTabHandle)
    assert struct.unpack(">II", g.contents(g.mem.r32(lowmem.WidthListHand))) == (h, 0)
    g("TextFace", 1)
    bold = g("CharWidth", ord("W"))
    assert width_table(g)[0][ord("W")] == bold << 16 and width_table(g)[1]["face"] == 1
    assert g.mem.r32(lowmem.WidthTabHandle) == h    # the one table, filled in again
    metrics = g.ptr(bytes(20))
    g("FontMetrics", metrics)
    ascent, *_, handle = struct.unpack(">4lI", g.mem.read(metrics, 20))
    assert handle == h and ascent == output(g)["ascent"] << 16


def test_an_applications_font_in_the_width_table(qapp):
    g, port = drawing([Resource(b"FONT", 200 << 7 | 9, blocky())], size=9)
    g("TextFace", 1)
    a = g("CharWidth", 65)
    table, rest = width_table(g)
    assert table[65] == a << 16 and rest["style"] == 1 << 16
    fm_input = g.ptr(struct.pack(">hhBBhII", 200, 18, 0, 0, 0, 0x10001, 0x10001))
    g("FMSwapFont", fm_input)                       # scaled from 9 points to 18
    table, rest = width_table(g)
    assert table[65] == 2 * (a - 1) << 16 and (rest["fsize"], rest["asize"]) == (18, 9)


def test_the_font_managers_flags_are_kept_in_low_memory(qapp):
    g = Guest()
    g("SetFScaleDisable", True)
    g("SetFractEnable", True)
    assert (g.mem.r8(lowmem.FScaleDisable), g.mem.r8(lowmem.FractEnable)) == (0xFF, 0xFF)
    g("SetFScaleDisable", False)
    assert g.mem.r8(lowmem.FScaleDisable) == 0


def test_the_system_pattern_list(qapp):
    g = Guest()
    h = g.process.resources.GetResource("PAT#", 0)
    data = g.contents(h)
    count = struct.unpack(">H", data[:2])[0]
    patterns = [data[2 + 8 * i:10 + 8 * i] for i in range(count)]
    assert count == 38 and len(data) == 2 + 8 * count
    assert patterns[0] == b"\xff" * 8 and patterns[-1] == bytes(8)
    assert len(set(patterns)) == count


def test_the_systems_desktop_patterns(qapp):
    """'PAT ' 16, the desktop's gray, and 17, light gray: Jazz looks for
    the System's at startup, with its other resources."""
    g = Guest()
    rm = g.process.resources
    assert g.contents(rm.GetResource("PAT ", 16)) == b"\xaa\x55" * 4
    assert g.contents(rm.GetResource("PAT ", 17)) == b"\x88\x22" * 4
