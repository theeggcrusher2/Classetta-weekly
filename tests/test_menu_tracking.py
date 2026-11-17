"""The host's menus as MenuSelect and PopUpMenuSelect follow the mouse:
along the bar's titles, sticky menus, the chosen item's blink, and the
menus an application's own MDEF draws, which the host asks about on the
application's thread (serve, as mdefs.Menus answers it)."""

import threading
from concurrent.futures import Future
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QEvent, QObject, QPoint, Qt
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication, QWidget

from macrt.toolbox import constants as k
from macrt.toolbox.bitfont import Strike
from macrt.toolbox.menus import MENU_PROC, Menu, MenuColors, MenuInfo, mct_entry, parse_menu_items
from macrt.toolbox.qdraw import Region
from macrt.toolbox.types import Point, Rect
from macrt.ui import theme
from macrt.ui.menubar import (DrawnPulldown, MenuBarWidget, Pulldown, _answered, _item_font,
                              _title_icon)

from conftest import Recorder
from test_bitfonts import blocky
from test_display import bar_for
from test_icons import SOLID, guest

TOOLS, SIZES, WIDTH = 140, 150, 100     # menus the program's own MDEF draws
W = 80                                  # as wide as it draws them
ROW = theme.MENU_ITEM_HEIGHT
S = theme.MENU_SHADOW
BLACK, WHITE, BLUE = (0, 0, 0), (0xFFFF, 0xFFFF, 0xFFFF), (0, 0, 0xDDDD)


def band(width: int, items: int, item: int) -> QImage:
    """A menu as the program's MDEF draws it: white, the item chosen black."""
    img = QImage(width, items * ROW, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    for y in range((item - 1) * ROW, item * ROW) if item else ():
        for x in range(width):
            img.setPixelColor(x, y, QColor("black"))
    return img


class Drawing(Recorder):
    """A program whose MenuSelect and PopUpMenuSelect answer the host as
    mdefs.Menus does: its own MDEFs draw their menus W wide, an item a
    row, and say which item the mouse is on.  Every question is logged in
    asked, and in answered once it's answered; one whose name is in hold
    waits for that Event first, and one in fail raises."""

    def __init__(self, name="App", **kw):
        super().__init__(name, **kw)
        self.asked, self.answered = [], []
        self.hold, self.fail = {}, set()
        self.host = set()               # menus whose MDEF leaves the drawing to the host
        self.menus, self.rects = {}, {}

    def own(self, menu):
        """menu's menuProc a handle of the program's own code."""
        with self.tb._sys.lock:
            menu.mem.w32(menu.ptr + MENU_PROC, self.tb._p.heap.new_handle(2))
        assert menu.own_mdef
        self.menus[menu.id] = menu
        return menu

    def handle(self, ev):
        if ev.what == k.mouseDown and self.tb.FindWindow(ev.where)[0] == k.inMenuBar:
            try:
                result = self.tb.MenuSelect(ev.where, serve=self.serve)
            except RuntimeError as e:
                self.log.append(("raised", str(e)))
                return False
            return self.menu(result)
        return super().handle(ev)

    def serve(self, what, *args):
        self.asked.append((what, *args))
        held = self.hold.get(what)
        if held is not None:
            held.wait(5)
        try:
            if what in self.fail:
                raise RuntimeError(f"its MDEF failed at {what}")
            return self._answer(what, *args)
        finally:
            self.answered.append(what)

    def _answer(self, what, menu_id, *args):
        if what == "open":
            return None
        if what == "choose":
            pt, _ = args
            r = self.rects[menu_id]
            item = (pt.v - r.top) // ROW + 1 if r.contains(pt) else 0
            return item, band(W, len(self.menus[menu_id].items), item)
        if menu_id in self.host:
            return None
        top, left = args[:2]
        if what == "popup":             # its item at the point
            top -= (args[2] - 1) * ROW
        n = len(self.menus[menu_id].items)
        self.rects[menu_id] = Rect(top, left, top + n * ROW, left + W)
        return self.rects[menu_id], band(W, n, 0)

    def popping(self, menu, top, left, item):
        """PopUpMenuSelect on a thread of its own: a list its answer goes in."""
        picked = []

        def run():
            try:
                picked.append(self.tb.PopUpMenuSelect(menu, top, left, item, serve=self.serve))
            except RuntimeError as e:
                picked.append(e)
        threading.Thread(target=run, daemon=True).start()
        return picked


def drawing(harness, **config):
    """A Drawing with Tools, a menu of its own MDEF's, in its bar, and
    Sizes, one for pop-ups."""
    h = harness(**config)
    app = Drawing()
    proc = h.launch(app)
    tb = app.tb
    tools = tb.NewMenu(TOOLS, "Tools")
    tb.AppendMenu(tools, "Pen;Brush;Spray")
    tb.InsertMenu(app.own(tools), 0)
    sizes = tb.NewMenu(SIZES, "Sizes")
    tb.AppendMenu(sizes, "9;10;12")
    tb.InsertMenu(app.own(sizes), k.hierMenu)
    tb.DrawMenuBar()
    return h, app, bar_for(h, proc)


def pulled_down(h, kind=QWidget):
    h.wait_until(lambda: isinstance(QApplication.activePopupWidget(), kind), what="a menu")
    return QApplication.activePopupWidget()


def at(rect: Rect, item: int) -> tuple[int, int]:
    """A point on a drawn menu's item."""
    return rect.left + 10, rect.top + (item - 1) * ROW + 8


def choices(app):
    return [(a[2], a[3]) for a in app.asked if a[0] == "choose"]


def results(app):
    return [e[1] for e in app.log if e[0] == "MenuSelect"]


def escape(h):
    h.key(Qt.Key.Key_Escape)


# -- drawing ----------------------------------------------------------------
def test_an_items_style_is_its_fonts(qapp):
    plain, slanted, lined = parse_menu_items("Plain;Slanted<I;Lined<U")
    assert not (_item_font(plain).italic() or _item_font(plain).underline())
    assert _item_font(slanted).italic() and not _item_font(slanted).underline()
    assert _item_font(lined).underline() and not _item_font(lined).italic()


def test_a_mark_other_than_the_check_is_drawn_in_the_mark_column(qapp):
    items = parse_menu_items("Bullet;Plain;Checked")
    items[0].mark, items[2].mark = "•", k.checkMark
    pd = Pulldown(Menu(200, "Game", items), None)
    try:
        img = pd.grab().toImage()

        def marked(index):
            r = pd.item_rect(index)
            return {(x, y) for x in range(r.x(), r.x() + theme.MENU_LEFT_PAD)
                    for y in range(r.y(), r.bottom() + 1)
                    if img.pixelColor(x, y).lightness() < 128}
        bullet, check = marked(1), marked(3)
        assert bullet and check and not marked(2)
        assert {(x, y - ROW * 2) for x, y in check} != bullet
    finally:
        pd.deleteLater()


def test_only_the_items_are_selectable(qapp):
    pd = Pulldown(Menu(200, "Game", parse_menu_items("Begin;Quit")), None)
    try:
        assert pd.selectable_at(pd.item_rect(2).center()) == 2
        assert pd.selectable_at(QPoint(0, 0)) == 0                      # the frame
        assert pd.selectable_at(QPoint(5, pd.height() - 1)) == 0        # the shadow
    finally:
        pd.deleteLater()


def _bar(proc):
    bar = MenuBarWidget(SimpleNamespace(application_menu_for=lambda bar: None))
    bar.resize(300, bar.height())
    bar.process = proc
    return bar


def test_a_disabled_menus_title_is_dimmed(qapp):
    game = Menu(200, "Game", parse_menu_items("Begin"))

    def dark():
        bar = _bar(SimpleNamespace(bar_menus=lambda: [game]))
        try:
            img = bar.image()
            (_, cell), = bar.cells()
            return sum(img.pixelColor(x, y).lightness() < 128
                       for x in range(cell.left(), cell.right() + 1)
                       for y in range(cell.top(), cell.bottom() + 1))
        finally:
            bar.deleteLater()
    shown = dark()
    game.enabled = False
    assert 0 < dark() < shown


def test_a_title_in_the_applications_own_font_takes_its_colour(qapp):
    """A title in the application's own Chicago is drawn in the colour the
    menu colour table gives it, as the host's font is."""
    strike = Strike.parse(blocky())
    game = Menu(200, "AC", parse_menu_items("Begin"))

    def first_row(colors):
        bar = _bar(SimpleNamespace(bar_menus=lambda: [game], menu_colors=lambda: colors,
                                   system_strike=strike))
        try:
            img = bar.image()
            (_, cell), = bar.cells()
            x = cell.x() + (cell.width() - strike.width(b"AC")) // 2
            y = cell.y() + (cell.height() - strike.ascent - strike.descent) // 2
            return [img.pixel(x + i, y) & 0xFFFFFF for i in range(10)]
        finally:
            bar.deleteLater()
    black, white, blue = 0, 0xFFFFFF, QColor(0, 0, 0xDD).rgb() & 0xFFFFFF
    assert first_row(None) == [white, black, black, white, white,
                               white, black, black, black, white]       # ".##...###."
    tinted = first_row(MenuColors([mct_entry(200, 0, [WHITE, BLUE, BLACK, WHITE])]))
    assert tinted == [blue, white, white, blue, blue, blue, white, white, white, blue]


def test_a_title_suite_with_no_small_icon_is_a_title_of_text(qapp):
    g = guest(("ICN#", 129, SOLID + SOLID))
    out = g.ptr(bytes(4))
    g("GetIconSuite", out, 129, 0x1)                    # its large icon only
    menu = MenuInfo(g.system, g("NewMenu", 300, g.ptr(b"\x05\x01" + g.mem.read(out, 4))))
    assert menu.title_suite == g.mem.r32(out) and _title_icon(menu) is None


def test_a_drawn_menu_shows_what_its_mdef_last_drew(qapp):
    """Its frame round the image of menuRect; the image the MDEF drew with
    no item highlighted comes back for the chosen item's blink."""
    rect = Rect(40, 100, 40 + 2 * ROW, 100 + W)
    first, lit, other = band(W, 2, 0), band(W, 2, 1), band(W, 2, 2)
    pd = DrawnPulldown(Menu(TOOLS, "Tools", parse_menu_items("Pen;Brush")), None, rect, first)
    try:
        assert (pd.width(), pd.height()) == (W + 2 + S, 2 * ROW + 2 + S)
        pd.chose(1, lit)
        assert (pd.hilite, pd.image, pd.plain) == (1, lit, first)
        pd.set_hilite(0)
        assert pd.image is first
        pd.set_hilite(1)
        assert pd.image is lit
        pd.chose(0, other)
        assert (pd.hilite, pd.image, pd.plain) == (0, other, other)
        pd.chose(2, None)                               # nothing new drawn
        assert (pd.hilite, pd.image) == (2, other)
        img = pd.grab().toImage()
        assert img.pixelColor(0, 0) == QColor("black")                  # the frame
        assert img.pixelColor(5, 1 + 5) == QColor("white")
        assert img.pixelColor(5, 1 + ROW + 5) == QColor("black")        # item 2
    finally:
        pd.deleteLater()


class Paints(QObject):
    """Counts the paint events a widget gets."""

    def __init__(self, widget):
        super().__init__()
        self.n = 0
        widget.installEventFilter(self)

    def eventFilter(self, obj, ev):  # noqa: N802
        self.n += ev.type() == QEvent.Type.Paint
        return False


def test_a_drawn_menu_is_painted_again_with_each_answer_and_blink(harness):
    h = harness()
    rect = Rect(40, 100, 40 + 2 * ROW, 100 + W)
    pd = DrawnPulldown(Menu(TOOLS, "Tools", parse_menu_items("Pen;Brush")), None, rect,
                       band(W, 2, 0))
    try:
        paints = Paints(pd)
        pd.show()
        h.wait_until(lambda: paints.n, what="the menu")
        for change in (lambda: pd.chose(1, band(W, 2, 1)), lambda: pd.set_hilite(0)):
            paints.n = 0
            change()
            h.wait_until(lambda: paints.n, what="it painted again")
    finally:
        pd.hide()
        pd.deleteLater()


def test_the_bar_shows_what_the_screen_buffer_has_over_it(qapp):
    """Where a program has drawn over the bar, there, and only there."""
    over = Region.rect(Rect(0, 10, 10, 30)).union(Region.rect(Rect(0, 50, 10, 70)))

    def image(box):
        img = QImage(box.width, box.height, QImage.Format.Format_RGB32)
        img.fill(QColor("black"))
        return img
    screen = QWidget()
    screen.mirror = SimpleNamespace(bar_shown=lambda: (over, SimpleNamespace(image=image)))
    bar = MenuBarWidget(SimpleNamespace(application_menu_for=lambda bar: None), screen)
    try:
        bar.resize(300, bar.height())
        img = bar.grab().toImage()
        assert [img.pixelColor(x, 5).lightness() < 128 for x in (5, 20, 40, 60, 80)] == [
            False, True, False, True, False]
    finally:
        screen.deleteLater()


# -- following the mouse ----------------------------------------------------
def test_dragging_along_the_bar_pulls_down_the_menu_under_the_mouse(harness, monkeypatch):
    monkeypatch.setattr(theme, "STICKY_CLICK_MS", 0)    # released on the bar, it closes
    h = harness()
    app = Recorder("App")
    bar = bar_for(h, h.launch(app))
    file, edit = h.menu_title_point(bar, "File"), h.menu_title_point(bar, "Edit")
    past = h.mac(bar, QPoint(bar.width() - 40, 5))
    h.press(*file)
    pd = pulled_down(h)
    tracker = h.d.tracker
    h.move(file[0] + 2, file[1])                        # along its title: the same menu
    assert tracker.pulldown is pd
    tracker._open(len(bar.cells()))                     # a title since gone: no change
    assert tracker.pulldown is pd and tracker.menu.title == "File"
    h.move(*h.item_point(1))
    assert pd.hilite == 1
    h.move(*file)                                       # back on the bar: no item
    assert pd.hilite == 0
    h.move(*edit)
    assert tracker.pulldown is not pd and tracker.pulldown.menu.title == "Edit"
    assert bar.hilite_id == Recorder.EDIT
    h.move(*file)                                       # back to the first title
    assert tracker.pulldown.menu.title == "File" and tracker.pulldown.isVisible()
    h.move(*past)                                       # past the titles: still File's
    assert tracker.pulldown.menu.title == "File"
    h.release(*past)
    h.wait_until(lambda: results(app), what="MenuSelect")
    assert results(app) == [0]


def test_a_quick_click_on_a_title_leaves_its_menu_down(harness, monkeypatch):
    """The menu stays down until the next click, following the mouse with
    its button up: on another title that one's pulled down; on the same
    title, the bar past its titles or outside the menu nothing's chosen;
    in the menu its item is."""
    monkeypatch.setattr(theme, "STICKY_CLICK_MS", 60_000)
    h = harness()
    app = Recorder("App")
    proc = h.launch(app)
    bar = bar_for(h, proc)
    file, edit = h.menu_title_point(bar, "File"), h.menu_title_point(bar, "Edit")

    def opened(title):
        h.press(*title)
        pulled_down(h)
        h.release(*title)                               # while it's down: quick
        tracker = h.d.tracker
        assert tracker.sticky and not tracker.done
        return tracker

    tracker = opened(file)
    h.move(*h.item_point(1))
    assert tracker.pulldown.hilite == 1
    h.click(*edit)
    assert tracker.pulldown.menu.title == "Edit" and not tracker.done
    h.click(*edit)
    h.wait_until(lambda: results(app) == [0], what="nothing chosen")
    opened(file)
    h.click(*h.mac(bar, QPoint(bar.width() - 40, 5)))
    h.wait_until(lambda: results(app) == [0, 0], what="nothing chosen")
    tracker = opened(file)
    h.press(600, 400)                                   # the press ends it
    assert tracker.done
    h.release(600, 400)
    h.wait_until(lambda: results(app) == [0, 0, 0], what="nothing chosen")
    opened(file)
    h.click(*h.item_point(2))                           # Close
    h.wait_until(lambda: len(results(app)) == 4 and not proc.windows, what="Close")
    assert results(app)[3] == Recorder.FILE << 16 | 2


def test_menu_choice_gives_the_disabled_item_released_over(harness, qapp):
    h = harness()
    app = Recorder("App")
    proc = h.launch(app)
    bar = bar_for(h, proc)
    h.choose(bar, "Edit", 1)                            # Undo, disabled
    h.wait_until(lambda: results(app) == [0], what="MenuSelect")
    assert proc.menu_choice == Recorder.EDIT << 16 | 1
    app.tb.MenuKey("x")                                 # Cut: MenuKey leaves none
    assert proc.menu_choice == 0
    h.choose(bar, "File", 3)                            # the dividing line
    h.wait_until(lambda: len(results(app)) == 2, what="MenuSelect")
    assert results(app)[1] == 0 and proc.menu_choice == Recorder.FILE << 16 | 3
    h.press(*h.menu_title_point(bar, "Edit"))
    pulled_down(h)
    h.move(600, 400)
    h.release(600, 400)                                 # off the menu: none
    h.wait_until(lambda: len(results(app)) == 3, what="MenuSelect")
    assert results(app)[2] == 0 and proc.menu_choice == 0
    h.choose(bar, "Edit", 1)
    h.wait_until(lambda: len(results(app)) == 4, what="MenuSelect")
    h.choose(bar, "Edit", 3)                            # Copy, chosen: none
    h.wait_until(lambda: len(results(app)) == 5, what="MenuSelect")
    assert results(app)[4] == Recorder.EDIT << 16 | 3 and proc.menu_choice == 0

    g = guest()                                         # and as the trap gives it
    g.process.menu_choice = Recorder.EDIT << 16 | 1
    assert g("MenuChoice") == Recorder.EDIT << 16 | 1


def test_a_submenu_without_room_on_its_right_opens_on_its_left(harness):
    h = harness(screen_size=(512, 342))
    app = Recorder("App")
    proc = h.launch(app)
    sub = app.tb.NewMenu(100, "Volume")
    app.tb.AppendMenu(sub, "Loud;Soft")
    app.tb.InsertMenu(sub, k.hierMenu)
    app.tb.AppendMenu(app.tb.GetMHandle(Recorder.EDIT), "M" * 60 + "/\x1b!\x64")
    edit = h.menu_title_point(bar_for(h, proc), "Edit")
    h.press(*edit)
    pd = pulled_down(h)
    h.move(*h.mac(pd, pd.item_rect(4).topLeft() + QPoint(20, ROW // 2)))
    h.wait_until(lambda: h.d.tracker.sub is not None, what="the submenu")
    sub_pd = h.d.tracker.sub
    left = pd.mapToGlobal(QPoint(0, 0)).x()
    assert sub_pd.mapToGlobal(QPoint(sub_pd.width(), 0)).x() == left + S
    h.move(*edit)                                       # back on the bar, it closes
    assert h.d.tracker.sub is None and not sub_pd.isVisible()
    escape(h)


def test_while_the_item_chosen_blinks_the_mouse_is_left_alone(harness, monkeypatch):
    monkeypatch.setattr(theme, "MENU_BLINK_MS", 100)
    h = harness()
    app = Recorder("App")
    bar = bar_for(h, h.launch(app))
    h.press(*h.menu_title_point(bar, "File"))
    pulled_down(h)
    tracker = h.d.tracker
    h.move(*h.item_point(1))
    h.release(*h.item_point(1))
    assert tracker.blinking
    edit = h.menu_title_point(bar, "Edit")
    h.move(*edit)
    h.press(*edit)
    h.release(*edit)
    assert tracker.menu.title == "File" and not tracker.done
    h.wait_until(lambda: results(app), what="New")
    assert results(app) == [Recorder.FILE << 16 | 1]


def test_an_application_menu_item_cancelled_while_it_blinks_does_nothing(harness, monkeypatch):
    monkeypatch.setattr(theme, "MENU_BLINK_MS", 100)
    h = harness(application_menu="always")
    app = Recorder("App")
    bar = bar_for(h, h.launch(app))
    chosen = []
    monkeypatch.setattr(h.d, "application_menu_choice", chosen.append)
    title = next(c.center() for m, c in bar.cells() if m.id == k.kApplicationMenuID)

    def show_all(then, selects):
        h.press(*h.mac(bar, title))
        pulled_down(h)
        h.move(*h.item_point(3))
        h.release(*h.item_point(3))
        then()
        h.wait_until(lambda: len(results(app)) == selects, what="MenuSelect")

    show_all(lambda: None, 1)
    h.wait_until(lambda: chosen, what="the item chosen")
    assert chosen == [3] and results(app) == [0]
    trackers = []
    show_all(lambda: (trackers.append(h.d.tracker), escape(h)), 2)
    h.settle(4 * theme.MENU_BLINKS * theme.MENU_BLINK_MS)     # for a choice that mustn't come
    assert chosen == [3] and results(app) == [0, 0]
    # Its blink's timer went with its menu; one already due finds it done.
    tracker, = trackers
    tracker._chosen(tracker.menu, 3)
    assert chosen == [3]


def test_a_tracker_already_finished_leaves_the_next_alone(harness):
    h = harness()
    app = Recorder("App")
    bar = bar_for(h, h.launch(app))
    file, edit = h.menu_title_point(bar, "File"), h.menu_title_point(bar, "Edit")
    h.press(*file)
    pulled_down(h)
    first = h.d.tracker
    escape(h)
    h.release(*file)
    h.wait_until(lambda: results(app) == [0], what="nothing chosen")
    h.press(*edit)
    pulled_down(h)
    h.wait_until(lambda: h.d.tracker is not None and h.d.tracker is not first, what="Edit")
    first.cancel()
    assert bar.hilite_id == Recorder.EDIT and h.d.tracker.pulldown.isVisible()
    escape(h)
    h.release(*edit)


# -- pop-up menus ------------------------------------------------------------
def _sizes(app):
    menu = app.tb.NewMenu(200, "Sizes")
    app.tb.AppendMenu(menu, "9 Point;10 Point;12 Point")
    app.tb.InsertMenu(menu, k.hierMenu)
    return menu


def test_a_pop_up_menu_ignores_the_mouse_while_its_item_blinks(harness, monkeypatch):
    monkeypatch.setattr(theme, "MENU_BLINK_MS", 100)
    h = harness()
    app = Recorder("App")
    h.launch(app)
    menu = _sizes(app)
    picked = []
    h.press(10, 300)
    threading.Thread(target=lambda: picked.append(
        app.tb.PopUpMenuSelect(menu, 150, 120, 2)), daemon=True).start()
    pd = pulled_down(h)
    tracker = h.d.tracker
    h.move(*h.mac(pd, pd.item_rect(3).center()))        # followed with its button down
    assert pd.hilite == 3
    h.release(*h.mac(pd, pd.item_rect(3).center()))
    assert tracker.blinking
    h.move(*h.mac(pd, pd.item_rect(1).center()))
    assert pd.hilite != 1
    h.press(10, 300)
    h.release(10, 300)
    h.wait_until(lambda: picked, what="PopUpMenuSelect")
    assert picked == [200 << 16 | 3]


def test_a_pop_up_tracker_already_finished_leaves_the_next_alone(harness):
    """A click since the last pop-up menu closed is still one since."""
    h = harness()
    app = Recorder("App")
    h.launch(app)
    menu = _sizes(app)
    picked = []

    def call():
        threading.Thread(target=lambda: picked.append(
            app.tb.PopUpMenuSelect(menu, 150, 120, 2)), daemon=True).start()
    h.click(10, 300)
    call()
    pulled_down(h)
    first = h.d.tracker
    escape(h)
    h.wait_until(lambda: picked == [0], what="nothing chosen")
    h.click(10, 300)
    first.cancel()
    call()
    pulled_down(h)
    assert h.d.tracker.sticky
    escape(h)
    h.wait_until(lambda: picked == [0, 0], what="nothing chosen")


def test_a_slow_release_outside_a_pop_up_menu_chooses_nothing(harness, monkeypatch):
    monkeypatch.setattr(theme, "STICKY_CLICK_MS", 0)
    h, app, _ = drawing(harness)
    app.host.add(SIZES)                                 # its MDEF leaves it to the host
    h.press(10, 300)
    picked = app.popping(app.menus[SIZES], 150, 120, 2)
    pulled_down(h, Pulldown)
    assert ("popup", SIZES, 150, 120, 2) in app.asked
    h.release(10, 300)
    h.wait_until(lambda: picked, what="PopUpMenuSelect")
    assert picked == [0]


# -- menus the application's own MDEF draws -----------------------------------
def test_a_menu_its_mdef_draws_hangs_from_its_title(harness):
    """Its MDEF draws it inside the frame, which hangs from the title, and
    says which item the mouse is on, drawing it highlighted; the item the
    button's released on is chosen."""
    h, app, bar = drawing(harness)
    h.press(*h.menu_title_point(bar, "Tools"))
    pd = pulled_down(h, DrawnPulldown)
    (_, menu_id, top, left), = [a for a in app.asked if a[0] == "draw"]
    assert menu_id == TOOLS and h.mac(pd, QPoint(1, 1)) == (left, top)
    assert (pd.width(), pd.height()) == (W + 2 + S, 3 * ROW + 2 + S)
    assert bar.hilite_id == TOOLS
    h.wait_until(lambda: not pd.busy, what="the MDEF")
    r = app.rects[TOOLS]
    h.move(*at(r, 2))
    h.wait_until(lambda: pd.hilite == 2, what="item 2")
    assert pd.image.pixelColor(5, ROW + 5) == QColor("black")
    h.move(*at(r, 3))
    h.wait_until(lambda: pd.hilite == 3, what="item 3")
    p2, p3 = (Point(v, hh) for hh, v in (at(r, 2), at(r, 3)))
    assert (p2, 0) in choices(app) and (p3, 2) in choices(app)
    h.release(*at(r, 3))
    h.wait_until(lambda: results(app), what="MenuSelect")
    assert results(app) == [TOOLS << 16 | 3]


def test_a_release_while_the_mdef_is_answering_waits_for_its_answers(harness):
    """One question at a time: the mouse's latest point is asked about once
    the last answer's in, and a release waits for them."""
    h, app, bar = drawing(harness)
    h.press(*h.menu_title_point(bar, "Tools"))
    pd = pulled_down(h, DrawnPulldown)
    h.wait_until(lambda: not pd.busy, what="the MDEF")
    r = app.rects[TOOLS]
    p2, p3 = (Point(v, hh) for hh, v in (at(r, 2), at(r, 3)))
    held = app.hold["choose"] = threading.Event()
    h.move(*at(r, 2))
    h.wait_until(lambda: (p2, 0) in choices(app), what="item 2's question")
    h.move(*at(r, 3))
    assert pd.asking and pd.want == p3
    h.release(*at(r, 3))
    assert pd.then is not None
    h.move(*at(r, 1))                                   # the button up: not followed
    assert pd.want == p3 and not results(app)
    held.set()
    h.wait_until(lambda: results(app), what="MenuSelect")
    assert results(app) == [TOOLS << 16 | 3] and choices(app)[-2:] == [(p2, 0), (p3, 2)]


def test_an_answer_after_the_menu_has_gone_is_dropped(harness):
    h, app, bar = drawing(harness)
    h.press(*h.menu_title_point(bar, "Tools"))
    pd = pulled_down(h, DrawnPulldown)
    h.wait_until(lambda: not pd.busy, what="the MDEF")
    held = app.hold["choose"] = threading.Event()
    h.move(*at(app.rects[TOOLS], 2))
    h.wait_until(lambda: len(app.asked) > len(app.answered), what="item 2's question")
    escape(h)
    h.settle(50)
    held.set()
    h.wait_until(lambda: results(app), what="MenuSelect")
    h.settle(50)
    assert results(app) == [0] and h.d.tracker is None
    h.release(*at(app.rects[TOOLS], 2))


@pytest.mark.parametrize("what", ["draw", "choose"])
def test_an_mdef_that_fails_ends_the_menu(harness, what):
    h, app, bar = drawing(harness)
    app.fail.add(what)
    title = h.menu_title_point(bar, "Tools")
    h.press(*title)
    h.wait_until(lambda: app.last("raised") is not None, what="the failure")
    h.wait_until(lambda: h.d.tracker is None, what="the menu to go")
    assert QApplication.activePopupWidget() is None and bar.hilite_id == 0
    h.release(*title)


def test_a_failing_answer_from_another_menu_select_leaves_this_one(qapp):
    """The MDEF failed: the menu it answered for is ended, if it's still the
    one being followed."""
    failed = Future()
    failed.set_exception(RuntimeError("its MDEF failed"))
    rect = Rect(40, 100, 40 + 2 * ROW, 100 + W)
    pd = DrawnPulldown(Menu(TOOLS, "Tools", parse_menu_items("Pen;Brush")), None, rect,
                       band(W, 2, 0))
    try:
        ended = []
        ask = object()
        display = SimpleNamespace(tracker=SimpleNamespace(ask=object(),
                                                          cancel=lambda: ended.append(1)))
        pd.asking, pd.want = True, Point(50, 110)
        _answered(display, ask, pd, failed)
        assert (pd.asking, pd.want, ended) == (False, None, [])
        display.tracker.ask = ask
        _answered(display, ask, pd, failed)
        assert ended == [1]
        display.tracker = None
        _answered(display, ask, pd, failed)
        assert ended == [1]
    finally:
        pd.deleteLater()


def test_a_menu_measured_for_its_mbdf_leaves_nothing_behind(harness):
    h, app, bar = drawing(harness)
    edit = h.menu_title_point(bar, "Edit")
    h.press(*edit)
    pd = pulled_down(h, Pulldown)
    h.settle(50)
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    assert bar.findChildren(Pulldown) == [pd]
    escape(h)
    h.release(*edit)


def _width_submenu(app):
    """Edit's item 4 opens Width, a submenu of the program's own MDEF's;
    item 5 names a menu there isn't."""
    sub = app.tb.NewMenu(WIDTH, "Width")
    app.tb.AppendMenu(sub, "Thin;Thick")
    app.tb.InsertMenu(app.own(sub), k.hierMenu)
    app.tb.AppendMenu(app.tb.GetMHandle(Recorder.EDIT), "Width/\x1b!\x64;Nowhere/\x1b!\x63")


def test_a_submenu_its_mdef_draws_hangs_from_its_item(harness):
    h, app, bar = drawing(harness)
    _width_submenu(app)
    h.press(*h.menu_title_point(bar, "Edit"))
    pd = pulled_down(h, Pulldown)
    left, top = h.mac(pd, QPoint(0, 0))
    where = Rect(top, left, top + pd.height() - S, left + pd.width() - S)
    assert ("open", Recorder.EDIT, where) in app.asked
    tracker = h.d.tracker
    h.move(*h.mac(pd, pd.item_rect(5).center()))        # no such menu: nothing opens
    h.settle(50)
    assert tracker.sub is None
    ih, iv = h.mac(pd, pd.item_rect(4).center())
    h.move(ih, iv)
    h.wait_until(lambda: isinstance(tracker.sub, DrawnPulldown), what="the submenu")
    r = pd.item_rect(4)
    left, top = h.mac(pd, QPoint(r.right() + 2, r.y()))
    alt = h.mac(pd, QPoint(0, 0))[0] - 1
    assert ("draw", WIDTH, top, left, alt) in app.asked
    sub = tracker.sub
    assert h.mac(sub, QPoint(1, 1)) == (left, top)
    h.move(ih + 4, iv)                                  # along the item, then across
    h.move(*at(app.rects[WIDTH], 2))
    h.wait_until(lambda: sub.hilite == 2, what="Thick")
    h.release(*at(app.rects[WIDTH], 2))
    h.wait_until(lambda: results(app), what="MenuSelect")
    assert results(app) == [WIDTH << 16 | 2]


def test_a_submenu_its_mdef_draws_late_or_fails_at(harness):
    """Drawn once the mouse has left its item, it isn't shown; its MDEF
    failing ends the menu."""
    h, app, bar = drawing(harness)
    _width_submenu(app)
    edit = h.menu_title_point(bar, "Edit")
    h.press(*edit)
    pd = pulled_down(h, Pulldown)
    tracker = h.d.tracker
    held = app.hold["draw"] = threading.Event()
    h.move(*h.mac(pd, pd.item_rect(4).center()))
    h.wait_until(lambda: [a for a in app.asked if a[0] == "draw"], what="the submenu's question")
    h.move(*h.mac(pd, pd.item_rect(2).center()))
    held.set()
    h.wait_until(lambda: "draw" in app.answered, what="the answer")
    h.settle(50)
    assert tracker.sub is None and not tracker.done
    app.fail.add("draw")
    h.move(*h.mac(pd, pd.item_rect(4).center()))
    h.wait_until(lambda: app.last("raised") is not None, what="the failure")
    h.wait_until(lambda: tracker.done, what="the menu to go")
    h.release(*edit)


def test_a_pop_up_menu_its_mdef_draws(harness):
    """Its MDEF says where it goes, its item at the point, and draws it;
    a release while it's answering waits for its answer."""
    h, app, _ = drawing(harness)
    h.press(10, 300)                                    # the press it pops up for
    picked = app.popping(app.menus[SIZES], 150, 120, 2)
    pd = pulled_down(h, DrawnPulldown)
    assert ("popup", SIZES, 150, 120, 2) in app.asked
    assert h.mac(pd, QPoint(1, 1 + ROW)) == (120, 150)
    h.wait_until(lambda: choices(app), what="the mouse's question")
    assert choices(app)[0] == (Point(300, 10), 0)      # where the mouse is, at once
    h.wait_until(lambda: not pd.busy, what="the MDEF")
    r = app.rects[SIZES]
    h.move(*at(r, 1))
    h.wait_until(lambda: pd.hilite == 1, what="item 1")
    held = app.hold["choose"] = threading.Event()
    h.move(*at(r, 3))
    h.release(*at(r, 3))
    assert pd.then is not None and not picked
    held.set()
    h.wait_until(lambda: picked, what="PopUpMenuSelect")
    assert picked == [SIZES << 16 | 3]


def test_a_pop_up_menus_mdef_that_fails_or_answers_too_late(harness):
    h, app, _ = drawing(harness)
    sizes = app.menus[SIZES]
    app.fail.add("popup")
    h.press(10, 300)
    picked = app.popping(sizes, 150, 120, 2)
    h.wait_until(lambda: picked, what="PopUpMenuSelect")
    assert isinstance(picked[0], RuntimeError)
    h.wait_until(lambda: h.d.tracker is None, what="the menu to go")
    assert QApplication.activePopupWidget() is None
    h.release(10, 300)
    app.fail.clear()
    held = app.hold["popup"] = threading.Event()
    h.press(10, 300)
    picked = app.popping(sizes, 150, 120, 2)
    h.wait_until(lambda: len(app.asked) > len(app.answered), what="the question")
    escape(h)                                           # gone before its MDEF answers
    held.set()
    h.wait_until(lambda: picked, what="PopUpMenuSelect")
    h.settle(50)
    assert picked == [0] and QApplication.activePopupWidget() is None
    h.release(10, 300)


def test_a_menu_asked_for_twice_while_its_mdef_answers_is_shown_once(harness):
    """Off its title and back before its MDEF has answered: one menu."""
    h, app, bar = drawing(harness)
    tools, edit = h.menu_title_point(bar, "Tools"), h.menu_title_point(bar, "Edit")
    held = app.hold["draw"] = threading.Event()
    h.press(*tools)
    h.wait_until(lambda: app.asked, what="Tools' question")
    h.move(*edit)
    h.move(*tools)
    held.set()
    h.wait_until(lambda: app.answered.count("draw") == 2 and "open" in app.answered,
                 what="the answers")
    h.settle(50)
    shown = [w for w in QApplication.topLevelWidgets()
             if isinstance(w, DrawnPulldown) and w.isVisible()]
    assert shown == [h.d.tracker.pulldown]
    escape(h)
    h.release(*tools)
