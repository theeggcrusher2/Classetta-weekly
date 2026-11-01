"""The screen buffer at ScrnBase mirrors what the windows show: QuickDraw's
drawing is written through to it, and what a program writes there itself
shows in the windows it lands on, or on the desk."""

import time

from macrt.toolbox import lowmem
from macrt.toolbox.qdraw import Region, WindowSurface, ink, patCopy, read_rgn, write_rgn
from macrt.toolbox.screen import DESK, TICK
from macrt.toolbox.types import Point, Rect
from macrt.ui import theme

from conftest import Recorder

STRIPES = bytes([0x0F]) * 8


def black(system, h, v) -> bool:
    bits = system.screen_bits
    return bool(system.mem.r8(bits.base + v * bits.row_bytes + h // 8) & (0x80 >> (h % 8)))


def poke(system, h, v, byte=0xFF) -> None:
    """Write a byte of the screen buffer, as a program would: h a multiple of 8."""
    bits = system.screen_bits
    system.mem.w8(bits.base + v * bits.row_bytes + h // 8, byte)


def gray(system, h, v) -> bool:
    """Whether the buffer's byte at (h, v) is the desk's gray."""
    bits = system.screen_bits
    return system.mem.r8(bits.base + v * bits.row_bytes + h // 8) == DESK[v % 8]


def on_desk(harness, h, v) -> bool:
    """Whether the emulated screen shows a black pixel at (h, v)."""
    return harness.d.screen_widget.grab().toImage().pixelColor(h, v).black() == 255


def shows(w, h, v) -> bool:
    """Whether w's window shows a black pixel at global (h, v)."""
    c, r = w.content, w.port_rect
    return WindowSurface(w).pixel(Point(v - c.top + r.top, h - c.left + r.left))


def launch(harness, windows=1):
    h = harness()
    g = Recorder("Mirror", windows=windows)
    h.launch(g)
    return h, g, h.d.system


def test_what_quickdraw_draws_in_a_window_is_in_the_buffer(harness):
    h, g, system = launch(harness)
    w = g.tb.FrontWindow()
    g.tb.SetPort(w)
    g.tb.PaintRect(Rect(10, 20, 14, 36))
    c = w.content
    assert all(black(system, c.left + x, c.top + y) for x in range(20, 36) for y in range(10, 14))
    assert not black(system, c.left + 19, c.top + 10) and not black(system, c.left + 20, c.top + 14)
    g.tb.EraseRect(Rect(10, 20, 14, 36))
    assert not black(system, c.left + 25, c.top + 12)


def test_what_a_program_writes_in_the_buffer_shows_in_the_window(harness):
    h, g, system = launch(harness)
    w = g.tb.FrontWindow()
    c = w.content
    x = (c.left + 40) // 8 * 8
    for v in range(c.top + 30, c.top + 33):
        poke(system, x, v)
    assert not shows(w, x, c.top + 30)
    system.screen.look()
    assert all(shows(w, x + i, c.top + 30 + j) for i in range(8) for j in range(3))
    assert not shows(w, x + 8, c.top + 30) and not shows(w, x, c.top + 33)


def test_the_front_window_has_the_pixels_where_two_overlap(harness):
    h, g, system = launch(harness, windows=2)
    front, back = g.tb.FrontWindow(), g.tb._p.windows[1]
    f, b = front.content, back.content
    assert f.left > b.left and f.top > b.top                # front overlaps back's lower right
    x, v = (f.left + 15) // 8 * 8, f.top + 15
    bx, bv = (b.left + 10) // 8 * 8, b.top + 5              # back's, uncovered
    poke(system, x, v)
    poke(system, bx, bv)
    system.screen.look()
    assert shows(front, x, v) and not shows(back, x, v)
    assert shows(back, bx, bv)


def test_get_pixel_reads_what_another_window_drew_outside_the_port(harness):
    """GetPixel reads the screen: at a point local to the back window,
    outside it, what the front window drew there (IM I's GetPixel)."""
    h, g, system = launch(harness, windows=2)
    front, back = g.tb.FrontWindow(), g.tb._p.windows[1]
    f, b = front.content, back.content
    x, v = b.right + 5, b.bottom + 5                        # front's, outside back
    at = Point(v - b.top + back.port_rect.top, x - b.left + back.port_rect.left)
    assert not g.tb.qd.get_pixel(back, at)
    g.tb.SetPort(front)
    g.tb.PaintRect(Rect(v - f.top - 2, x - f.left - 2, v - f.top + 2, x - f.left + 2))
    assert g.tb.qd.get_pixel(back, at)


def test_a_moved_window_is_written_where_it_is_now(harness):
    h, g, system = launch(harness)
    w = g.tb.FrontWindow()
    g.tb.SetPort(w)
    g.tb.PaintRect(Rect(0, 0, 4, 16))
    g.tb.MoveWindow(w, 300, 250)
    system.screen.look()
    c = w.content
    assert (c.left, c.top) == (300, 250)
    assert black(system, c.left + 8, c.top + 2) and not black(system, c.left + 8, c.top + 5)


def test_a_window_whose_port_rect_moves_without_set_origin_stays_where_it_is(harness):
    """QuickDraw puts a window's pixels where portBits.bounds says,
    wherever the program has set the portRect: Ricochet sets it to the half
    of its window it copies to next, without SetOrigin.  The window stays
    where the Window Manager put it, with both halves."""
    h, g, system = launch(harness)
    w = g.tb.FrontWindow()
    g.tb.SetPort(w)
    r, c = w.port_rect, w.content
    mid = r.height // 2
    w.port_rect = Rect(mid, 0, r.bottom, r.right)
    g.tb.PaintRect(Rect(mid + 2, 8, mid + 6, 24))
    system.screen.look()
    w.port_rect = Rect(0, 0, mid, r.right)
    g.tb.PaintRect(Rect(2, 40, 6, 56))
    system.screen.look()
    w.port_rect = r
    for x, v in ((c.left + 10, c.top + mid + 3), (c.left + 42, c.top + 3)):
        assert black(system, x, v) and shows(w, x, v)
    assert not shows(w, c.left + 10, c.top + 3)


def test_the_desk_a_window_leaves_is_gray_again_at_once(harness):
    """As the Window Manager paints it: before the program goes on, so
    what it draws there next is kept."""
    h, g, system = launch(harness)
    w = g.tb.FrontWindow()
    g.tb.SetPort(w)
    g.tb.PaintRect(Rect(0, 0, 4, 16))
    c = w.content
    assert black(system, c.left + 8, c.top + 2)
    g.tb.MoveWindow(w, 500, 400)
    assert all(gray(system, c.left + 8, c.top + v) for v in range(4))
    poke(system, c.left + 8, c.top + 2)
    system.screen.look()
    assert black(system, c.left + 8, c.top + 2)


def test_a_window_shown_covers_what_was_written_under_it(harness):
    """What a program wrote on the desk before ShowWindow was under the
    window, not in it: SnapShot zooms frames in the screen port, drawing
    and undoing each where its window is about to come."""
    h, g, system = launch(harness)
    w = g.tb.FrontWindow()
    c = w.content
    x, v = (c.left + 40) // 8 * 8, c.top + 30
    g.tb.HideWindow(w)
    system.screen.look()
    poke(system, x, v)
    system.screen.look()
    poke(system, x, v, DESK[v % 8])         # undone: gray, as the desk was
    g.tb.ShowWindow(w)
    assert not any(shows(w, x + i, v) for i in range(8))
    assert not black(system, x, v) and not black(system, x + 1, v)


def test_a_program_that_calls_no_traps_is_shown_from_the_clock(harness):
    h, g, system = launch(harness)
    w = g.tb.FrontWindow()
    c = w.content
    x, v = (c.left + 40) // 8 * 8, c.top + 30
    system.screen.look()
    poke(system, x, v)
    time.sleep(TICK)
    system.screen.glance()              # what the clock does each tick
    assert shows(w, x, v)


def test_the_clock_stops_looking_once_the_display_is_gone(harness):
    h, g, system = launch(harness)
    w = g.tb.FrontWindow()
    c = w.content
    x, v = (c.left + 40) // 8 * 8, c.top + 30
    system.screen.look()
    system.screen.close()
    poke(system, x, v)
    time.sleep(TICK)
    system.screen.glance()
    assert not shows(w, x, v)


def test_what_a_program_writes_on_the_desk_shows_there(harness):
    h, g, system = launch(harness)
    w = g.tb.FrontWindow()
    system.screen.look()
    assert not w.content.contains(Point(500, 600)) and gray(system, 600, 500)
    assert not system.screen.desk_drawn                     # the desk is only gray
    poke(system, 600, 500)
    system.screen.look()
    assert black(system, 600, 500) and system.screen.desk_drawn
    assert all(on_desk(h, 600 + i, 500) for i in range(8))
    assert on_desk(h, 608, 500) and not on_desk(h, 609, 500)   # the gray goes on
    assert not on_desk(h, 600, 501)


def test_the_desk_shows_the_pattern_it_is_painted_in(harness):
    """PaintOne with no window, or SetDeskCPat: the windows uncover the
    desk in it from then on."""
    h, g, system = launch(harness)
    w = g.tb.FrontWindow()
    system.screen.paint_desk(Region.rect(Rect(496, 600, 504, 616)), ink(STRIPES, patCopy)[0])
    assert [on_desk(h, x, 500) for x in range(600, 608)] == [False] * 4 + [True] * 4
    assert on_desk(h, 600, 504) == black(system, 600, 504) == bool(DESK[0] & 0x80)
    c = w.content
    x = (c.left + 8) // 8 * 8
    g.tb.HideWindow(w)
    system.screen.look()
    assert [black(system, x + i, c.top + 8) for i in range(8)] == [False] * 4 + [True] * 4


def test_what_a_program_writes_over_the_menu_bar_shows_till_it_draws_the_bar(harness):
    """A Mac's menu bar is only pixels in the buffer: games draw over it."""
    h, g, system = launch(harness)
    system.screen.look()
    assert not on_desk(h, 400, 5)                           # the bar, white past its titles
    poke(system, 400, 5)
    system.screen.look()
    assert all(on_desk(h, 400 + i, 5) for i in range(8)) and not on_desk(h, 408, 5)
    g.tb.EnableItem(next(iter(g.tb._p.menus)), 1)           # changes no title: still there
    assert on_desk(h, 400, 5)
    g.tb.DrawMenuBar()
    assert not on_desk(h, 400, 5) and not black(system, 400, 5)    # the bar's back, there too


def test_the_menu_bar_is_in_the_buffer_for_a_program_to_draw_over(harness):
    """MacLanding fills the bar white, its line too, to draw its radar there."""
    h, g, system = launch(harness)
    system.screen.look()
    bar = theme.MENU_BAR_HEIGHT - 1
    assert black(system, 400, bar) and on_desk(h, 400, bar)     # the bar's line
    assert black(system, 0, 0) and not black(system, 7, 0)      # a rounded corner
    w = system.screen_bits.row_bytes
    for v in range(theme.MENU_BAR_HEIGHT):
        system.mem.write(system.screen_bits.base + v * w, bytes(w))
    system.screen.look()
    assert not any(on_desk(h, x, bar) for x in (0, 400, 600))
    assert not on_desk(h, 0, 0)


def test_a_window_draws_over_the_menu_bar_only_if_its_visrgn_says_to(harness):
    """A window's visRgn leaves out the bar, as the desk does, so what it
    draws there isn't seen: unless the program puts the bar back in its
    visRgn, as Indiana Jones and the Last Crusade does to cover the bar
    with its window, till the bar is drawn again."""
    h, g, system = launch(harness)
    w = g.tb.FrontWindow()
    g.tb.MoveWindow(w, 360, 0)                              # over the bar, past its titles
    r = w.port_rect
    assert read_rgn(system.mem, w.vis_rgn) == Region.rect(
        Rect(r.top + theme.MENU_BAR_HEIGHT, r.left, r.bottom, r.right))
    g.tb.SetPort(w)
    g.tb.PaintRect(r)
    assert black(system, 400, theme.MENU_BAR_HEIGHT)
    assert not black(system, 400, 5) and not on_desk(h, 400, 5)
    write_rgn(system.heap_of(w.vis_rgn), w.vis_rgn, Region.rect(r))
    g.tb.PaintRect(r)
    assert black(system, 400, 5) and on_desk(h, 400, 5)
    g.tb.DrawMenuBar()
    assert not black(system, 400, 5) and not on_desk(h, 400, 5)


def test_what_shows_of_a_window_is_written_to_the_pixel(harness):
    """The part of a window that shows needn't be a rectangle, nor begin on
    a byte of the buffer: what's beside it there is kept as it was."""
    h, g, system = launch(harness, windows=2)
    front, back = g.tb.FrontWindow(), g.tb._p.windows[1]
    f, b = front.content, back.content
    g.tb.SetPort(front)
    g.tb.PaintRect(front.port_rect)
    title = (f.left // 8 * 8 + 16, f.top - 4)               # under front's title bar
    poke(system, *title)
    left = (b.left + 8) // 8 * 8 + 3                        # inside a byte
    top, right, bottom = f.top - 12, f.left + 30, f.top + 10    # from front's title bar
    assert b.left < left - 8 and left < f.left - 2 and b.top < top
    assert right < b.right and bottom < b.bottom

    def local(r):
        return r.offset(back.port_rect.left - b.left, back.port_rect.top - b.top)

    g.tb.SetPort(back)
    g.tb.PaintRect(local(Rect(top, left - 8, bottom, right)))
    g.tb.EraseRect(local(Rect(top, left, bottom, right)))
    for v in (top, f.top - 1, f.top, bottom - 1):
        assert black(system, left - 1, v) and not black(system, left, v)
        assert not black(system, f.left - 2, v) or v >= f.top - 1
    assert all(black(system, f.left + x, f.top + y) for x in range(30) for y in range(10))
    assert system.mem.r8(system.screen_bits.base + title[1] * system.screen_bits.row_bytes
                         + title[0] // 8) == 0xFF
    assert not shows(back, left, top) and shows(back, left - 1, top)


def test_the_desk_and_bar_show_the_alternate_buffer_while_the_via_says_to(harness):
    """Dark Castle draws in one buffer while the other is shown, and flips."""
    h = harness(screen_size=(512, 342))
    g = Recorder("Mirror", windows=0)
    h.launch(g)
    system = h.d.system
    alt, rb = system.alternate_screen, system.screen_bits.row_bytes

    def black_byte(v):          # (400, v) to (407, v): the gray desk is only half black
        return all(on_desk(h, 400 + i, v) for i in range(8))
    system.screen.look()
    system.mem.w8(alt + 200 * rb + 50, 0xFF)             # (400, 200) in the alternate
    system.mem.w8(alt + 5 * rb + 50, 0xFF)               # and over the bar
    poke(system, 400, 220)                              # (400, 220) in the main one
    system.screen.look()
    assert black_byte(220) and not black_byte(200) and not black_byte(5)
    system.screen.show_page(True)
    system.screen.look()
    assert black_byte(200) and black_byte(5) and not black_byte(220)
    system.mem.w8(alt + 210 * rb + 50, 0xFF)             # drawn while it's shown
    system.screen.look()
    assert black_byte(210)
    system.screen.show_page(False)
    system.screen.look()
    assert black_byte(220) and not black_byte(200) and not black_byte(5)


def test_a_program_that_puts_the_bar_in_gray_rgn_has_the_desk_painted_there(harness):
    """HyperCard's hide menuBar adds the bar to GrayRgn and paints the desk
    there (PaintOne with no window): that shows until the bar is drawn."""
    h, g, system = launch(harness)
    system.screen.look()
    gray_rgn = system.mem.r32(lowmem.GrayRgn)
    stripes = ink(STRIPES, patCopy)[0]
    system.screen.paint_desk(None, stripes)
    assert not on_desk(h, 400, 5)                           # the bar isn't desk yet
    b = system.screen_bits.bounds
    write_rgn(system.sys_heap, gray_rgn, Region.rect(b))
    system.screen.paint_desk(read_rgn(system.mem, gray_rgn), stripes)
    assert [on_desk(h, x, 5) for x in range(400, 408)] == [False] * 4 + [True] * 4
    assert [black(system, x, 5) for x in range(400, 408)] == [False] * 4 + [True] * 4
    g.tb.DrawMenuBar()
    assert not on_desk(h, 404, 5)
