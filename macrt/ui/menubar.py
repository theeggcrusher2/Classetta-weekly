"""
macrt.ui.menubar - The menu bar, pull-down menus and MenuSelect tracking.

A MenuBarWidget shows one process's menus.  Where it lives depends on the
display's `menu_bar` setting: one shared bar for the front app ("global"),
or one bar under each window's title bar ("window").  Neither the bar nor
the pull-downs act on a click.  The guest gets a mouseDown, calls FindWindow,
then MenuSelect, and MenuTracker answers that call.
"""

from __future__ import annotations

import time
from concurrent.futures import Future
from typing import TYPE_CHECKING, Callable

from PyQt6 import sip
from PyQt6.QtCore import QPoint, QRect, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPen, QRegion
from PyQt6.QtWidgets import QWidget

from ..toolbox.constants import APPLE_MENU_TITLE, checkMark, kApplicationMenuID, kHMHelpMenuID
from ..toolbox.icons import MINI, SMALL, suite_entries, suite_icon
from ..toolbox.menus import RGB, Menu, MenuBase, MenuColors, MenuItem
from ..toolbox.qdraw import CHICAGO_SYMBOLS, char_codes
from ..toolbox.types import Point, Rect
from . import theme
from .chrome import paint_dither
from .theme import BLACK, WHITE

if TYPE_CHECKING:
    from ..process import Process
    from ..toolbox.bitfont import Strike
    from ..toolbox.screen import Screen
    from .display import Display


def _item_font(item: MenuItem) -> QFont:
    font = QFont(theme.menu_item_font())
    if item.style & theme.STYLE_ITALIC:
        font.setItalic(True)
    if item.style & theme.STYLE_UNDERLINE:
        font.setUnderline(True)
    return font


def _menu_colors(proc: Process | None) -> MenuColors | None:
    """The colours the process's menu colour table gives its menus, if any."""
    get = getattr(proc, "menu_colors", None)
    return (get() or None) if get is not None else None


def _qcolor(rgb: RGB) -> QColor:
    return QColor(rgb[0] >> 8, rgb[1] >> 8, rgb[2] >> 8)


# The menus at the bar's right, in order from the left, and their titles' icons.
RIGHT_MENUS = (kHMHelpMenuID, kApplicationMenuID)
_TITLE_ICONS = {kHMHelpMenuID: theme.HELP_ICON, kApplicationMenuID: theme.GENERIC_APP_ICON}


def _bitmap(rows: list[bytes], width: int) -> list[str]:
    return ["".join("#" if row[x >> 3] & 0x80 >> (x & 7) else "." for x in range(width))
            for row in rows]


def _title_icon(menu: Menu) -> tuple[list[str], list[str]] | None:
    """The image and mask, as bitmaps, of the small icon of a title that's
    an icon suite's; None for a title of text."""
    suite = menu.title_suite
    icons = suite_entries(menu.contents(suite)) if suite else None
    if icons is None:
        return None

    def get(kind: str) -> bytes | None:
        return menu.contents(icons.get(kind, 0))
    icon = suite_icon(get, SMALL) or suite_icon(get, MINI)
    if icon is None:
        return None
    mask = icon.mask or [b"\xFF" * icon.row_bytes] * icon.height
    return _bitmap(icon.rows, icon.width), _bitmap(mask, icon.width)


def _title_width(menu: Menu, metrics: QFontMetrics, strike: Strike | None = None) -> int:
    if menu.is_apple:
        return theme.bitmap_size(theme.APPLE_ICON)[0]
    if menu.id in _TITLE_ICONS:
        return theme.bitmap_size(_TITLE_ICONS[menu.id])[0]
    icon = _title_icon(menu)
    if icon is not None:
        return theme.bitmap_size(icon[0])[0]
    if strike is not None:
        return strike.width(char_codes(menu.title))
    return sum(w for _, w in _title_runs(menu.title, metrics))


def _title_runs(title: str, metrics: QFontMetrics) -> list[tuple[str | None, int]]:
    """A title of text in the host's font, as runs of text and their widths,
    None for each apple in it: Chicago's $14, which the host's font hasn't
    got, so the Apple menu's own icon stands in for it (TME's title is a
    space and an apple)."""
    apple = theme.bitmap_size(theme.APPLE_ICON)[0]
    runs: list[tuple[str | None, int]] = []
    for i, text in enumerate(title.split(APPLE_MENU_TITLE)):
        if i:
            runs.append((None, apple))
        if text:
            text = text.translate(CHICAGO_SYMBOLS)
            runs.append((text, metrics.horizontalAdvance(text)))
    return runs


def _draw_strike(p: QPainter, strike: Strike, text: str, r: QRect, color: QColor,
                 face: int = 0, centered: bool = False) -> None:
    """Text in an application's own system font, centred in r or from its
    left, with its baseline where the Menu Manager puts it."""
    codes = char_codes(text)
    img, pen, baseline = strike.render(codes, face)
    if color != BLACK:
        tint = QPainter(img)
        tint.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        tint.fillRect(img.rect(), color)
        tint.end()
    x = r.x() + ((r.width() - strike.width(codes, face)) // 2 if centered else 0)
    y = r.y() + (r.height() - strike.ascent - strike.descent) // 2 + strike.ascent
    p.drawImage(x - pen, y - baseline, img)


class MenuBarWidget(QWidget):
    def __init__(self, display: Display, parent: QWidget | None = None, *,
                 top_level: bool = False, rounded: bool = False):
        flags = Qt.WindowType.Widget
        if top_level:
            flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                     | Qt.WindowType.WindowStaysOnTopHint
                     | Qt.WindowType.WindowDoesNotAcceptFocus
                     | Qt.WindowType.NoDropShadowWindowHint)
        super().__init__(parent, flags)
        self.display = display
        self._process: Process | None = None
        self.hilite_id = 0
        self.rounded = rounded
        self.setProperty("macrt_role", "menubar")
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)     # whatever the program's is
        self.setFixedHeight(theme.MENU_BAR_HEIGHT)
        if top_level:
            self.setWindowTitle("Menu Bar")

    @property
    def process(self) -> Process | None:
        """Whose menus the bar shows."""
        return self._process

    @process.setter
    def process(self, proc: Process | None) -> None:
        if proc is not self._process:
            self._process = proc
            self.hilite_id = 0          # a highlighted title was the last app's
            mirror = self._mirror()
            if mirror is not None:
                mirror.bar_drawn()      # the next app's bar is drawn afresh

    def system_strike(self) -> Strike | None:
        """The application's own Chicago 12, to draw its menus in."""
        return getattr(self.process, "system_strike", None)

    # -- layout -----------------------------------------------------------
    def menus(self) -> list[Menu]:
        if self.process is None:
            return []
        menus = self.process.bar_menus()
        app_menu = self.display.application_menu_for(self)
        if app_menu is not None:
            menus.append(app_menu)
        return menus

    def cells(self) -> list[tuple[Menu, QRect]]:
        metrics = QFontMetrics(theme.menu_bar_font())
        pad = theme.MENU_TITLE_PADDING
        h = theme.MENU_BAR_HEIGHT - 1
        x = theme.MENU_BAR_LEFT
        strike = self.system_strike()
        out, right = [], []
        for menu in self.menus():
            w = _title_width(menu, metrics, strike) + 2 * pad
            if menu.id in RIGHT_MENUS:
                right.append((menu, w))
            else:
                out.append((menu, QRect(x, 0, w, h)))
                x += w
        x = self.width() - theme.MENU_BAR_LEFT - sum(w for _, w in right)
        for menu, w in right:
            out.append((menu, QRect(x, 0, w, h)))
            x += w
        return out

    def natural_width(self) -> int:
        cells = self.cells()
        right = max((c.right() for m, c in cells if m.id not in RIGHT_MENUS), default=0)
        return right + theme.MENU_BAR_LEFT

    def cell_at(self, pos: QPoint) -> int:
        for i, (_, cell) in enumerate(self.cells()):
            if cell.contains(pos):
                return i
        return -1

    def set_hilite(self, menu_id: int) -> None:
        if menu_id != self.hilite_id:
            self.hilite_id = menu_id
            self.update()

    # -- painting ---------------------------------------------------------
    def _mirror(self) -> Screen | None:
        """The screen buffer, for the bar on an emulated screen."""
        return getattr(self.parentWidget(), "mirror", None)

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        self._paint_bar(p)
        mirror = self._mirror()
        over, bits = mirror.bar_shown() if mirror is not None else (None, None)
        if over is not None and not over.empty:
            # What the program drew over the bar (the bar is at the screen's top left).
            box = over.bbox
            p.setClipRegion(over.to_qregion())
            p.drawImage(QPoint(box.left, box.top), bits.image(box))

    def image(self) -> QImage:
        """The bar as DrawMenuBar draws it in the screen buffer: without
        what a program has drawn over it."""
        img = QImage(self.width(), self.height(), QImage.Format.Format_RGB32)
        p = QPainter(img)
        self._paint_bar(p, mono=True)
        p.end()
        return img

    def _paint_bar(self, p: QPainter, mono: bool = False) -> None:
        w, h = self.width(), self.height()
        colors = _menu_colors(self.process)
        p.fillRect(0, 0, w, h, _qcolor(colors.bar()) if colors else WHITE)
        p.fillRect(0, h - 1, w, 1, BLACK)
        font = theme.menu_bar_font()
        p.setFont(font)
        strike = self.system_strike()
        for menu, cell in self.cells():
            inverted = menu.id == self.hilite_id
            icon = _title_icon(menu)
            fg, bg = map(_qcolor, colors.title(menu.id)) if colors else (BLACK, WHITE)
            if inverted:
                fg, bg = bg, fg
            if inverted or colors:
                p.fillRect(cell, bg)
            if menu.is_apple:
                rainbow = (theme.APPLE_RAINBOW if theme.RAINBOW_APPLE and not (inverted or mono)
                           else None)
                self._draw_icon(p, cell, theme.APPLE_ICON, fg, rainbow)
            elif menu.id in _TITLE_ICONS:
                self._draw_icon(p, cell, _TITLE_ICONS[menu.id], fg)
            elif icon is not None:
                # Its mask in the title's background, then the icon over it.
                self._draw_icon(p, cell, icon[1], bg)
                self._draw_icon(p, cell, icon[0], fg)
            elif strike is not None:
                _draw_strike(p, strike, menu.title, cell, fg, centered=True)
            elif APPLE_MENU_TITLE in menu.title:
                self._draw_runs(p, cell, _title_runs(menu.title, QFontMetrics(font)), fg)
            else:
                p.setPen(QPen(fg))
                p.drawText(cell, Qt.AlignmentFlag.AlignCenter,
                           menu.title.translate(CHICAGO_SYMBOLS))
            if not menu.enabled:
                paint_dither(p, cell.adjusted(1, 1, -1, -1), bg)
        if self.rounded:
            self._draw_screen_corners(p, w)

    @staticmethod
    def _draw_runs(p, cell, runs, color):
        """Runs of text and apples, centred together in cell."""
        x = cell.x() + (cell.width() - sum(w for _, w in runs)) // 2
        p.setPen(QPen(color))
        for text, w in runs:
            if text is None:
                h = theme.bitmap_size(theme.APPLE_ICON)[1]
                y = cell.y() + (cell.height() - h) // 2
                theme.draw_bitmap(p, x, y, theme.APPLE_ICON, color)
            else:
                p.drawText(QRect(x, cell.y(), w, cell.height()),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
            x += w

    @staticmethod
    def _draw_icon(p, cell, bitmap, color, rows=None):
        bw, bh = theme.bitmap_size(bitmap)
        theme.draw_bitmap(p, cell.x() + (cell.width() - bw) // 2,
                          cell.y() + (cell.height() - bh) // 2, bitmap, color, rows)

    @staticmethod
    def _draw_screen_corners(p, w):
        left = theme.SCREEN_CORNER
        right = [row[::-1] for row in left]
        theme.draw_bitmap(p, 0, 0, left)
        theme.draw_bitmap(p, w - len(right[0]), 0, right)


def _menu_window(widget: QWidget, w: int, h: int) -> None:
    """A menu's window: a Qt popup so it can hang below the bar, w by h
    with its frame, and its shadow."""
    widget.setProperty("macrt_role", "pulldown")
    widget.setMouseTracking(True)
    s = theme.MENU_SHADOW
    widget.setFixedSize(w + s, h + s)
    if s:
        widget.setMask(QRegion(0, 0, w, h).united(QRegion(s, h, w, s))
                       .united(QRegion(w, s, s, h)))


_MENU_FLAGS = (Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
               | Qt.WindowType.NoDropShadowWindowHint)


def _paint_frame(p: QPainter, w: int, h: int, back: QColor = WHITE) -> None:
    s = theme.MENU_SHADOW
    p.fillRect(0, 0, w, h, back)
    p.fillRect(0, 0, w, 1, BLACK)
    p.fillRect(0, h - 1, w, 1, BLACK)
    p.fillRect(0, 0, 1, h, BLACK)
    p.fillRect(w - 1, 0, 1, h, BLACK)
    if s:
        p.fillRect(w, s, s, h, BLACK)
        p.fillRect(s, h, w, s, BLACK)


def _item_text_width(item: MenuItem, text: str, strike: Strike | None) -> int:
    if strike is not None:
        return strike.width(char_codes(text), item.style)
    return QFontMetrics(_item_font(item)).horizontalAdvance(text.translate(CHICAGO_SYMBOLS))


def _cmd_width(item: MenuItem, strike: Strike | None) -> int:
    if item.submenu:
        return theme.bitmap_size(theme.SUBMENU_GLYPH)[0]
    if not item.key:
        return 0
    gw = theme.bitmap_size(theme.COMMAND_GLYPH)[0]
    return gw + 2 + _item_text_width(item, item.key.upper(), strike)


def menu_size(menu: MenuBase, strike: Strike | None = None) -> tuple[int, int]:
    """The width and height inside its frame that a menu of the System's
    MDEF 0 is pulled down at, in the application's own system font if it
    has one: the menuWidth and menuHeight CalcMenuSize gives it."""
    items = menu.items
    text_w = max((_item_text_width(i, i.shown, strike)
                  for i in items if not i.is_separator), default=40)
    cmd_w = max((_cmd_width(i, strike) for i in items), default=0)
    w = theme.MENU_LEFT_PAD + text_w + theme.MENU_RIGHT_PAD
    if cmd_w:
        w += theme.MENU_KEY_GAP + cmd_w
    return w, len(items) * theme.MENU_ITEM_HEIGHT


class Pulldown(QWidget):
    """One pulled-down menu, as the System's MDEF 0 draws it, in the
    colours of its application's menu colour table if it has any."""

    def __init__(self, menu: Menu, parent: QWidget | None, strike: Strike | None = None,
                 colors: MenuColors | None = None):
        super().__init__(parent, _MENU_FLAGS)
        self.menu = menu
        self.strike = strike            # the application's own Chicago 12, if it has one
        self.colors = colors
        self.hilite = 0
        self._inner_w = self._measure()
        _menu_window(self, self._inner_w + 2,
                     max(len(menu.items), 0) * theme.MENU_ITEM_HEIGHT + 2)

    def _text_width(self, item: MenuItem, text: str) -> int:
        return _item_text_width(item, text, self.strike)

    def _measure(self) -> int:
        return menu_size(self.menu, self.strike)[0]

    def item_rect(self, index: int) -> QRect:
        return QRect(1, 1 + (index - 1) * theme.MENU_ITEM_HEIGHT,
                     self._inner_w, theme.MENU_ITEM_HEIGHT)

    def item_at(self, pos: QPoint) -> int:
        """The item at pos, enabled or not; 0 off the items."""
        items = QRect(1, 1, self._inner_w, len(self.menu.items) * theme.MENU_ITEM_HEIGHT)
        if not items.contains(pos):
            return 0
        return (pos.y() - 1) // theme.MENU_ITEM_HEIGHT + 1

    def selectable_at(self, pos: QPoint) -> int:
        index = self.item_at(pos)
        return index if self.menu.selectable(index) else 0

    def set_hilite(self, index: int) -> None:
        if index != self.hilite:
            self.hilite = index
            self.update()

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        back = _qcolor(self.colors.background(self.menu.id)) if self.colors else WHITE
        _paint_frame(p, self._inner_w + 2, self.height() - theme.MENU_SHADOW, back)
        for i, item in enumerate(self.menu.items, start=1):
            self._paint_item(p, i, item)

    def _paint_item(self, p: QPainter, index: int, item: MenuItem) -> None:
        r = self.item_rect(index)
        if self.colors:
            mark_c, fg, key_c, back = map(_qcolor, self.colors.item(self.menu.id, index))
        else:
            mark_c = fg = key_c = BLACK
            back = WHITE
        if item.is_separator:
            y = r.y() + r.height() // 2
            p.fillRect(r.x(), y, r.width(), 1, BLACK)
            paint_dither(p, QRect(r.x(), y, r.width(), 1), back)
            return
        if index == self.hilite:                # inverted: the text's colour behind
            back, mark_c, fg, key_c = fg, back, back, back
            p.fillRect(r, back)
        elif self.colors:
            p.fillRect(r, back)
        font = _item_font(item)
        p.setFont(font)
        p.setPen(QPen(fg))
        mark = "" if item.submenu else item.mark      # a hierarchical item's is a menu ID
        if mark == checkMark:
            cw, ch = theme.bitmap_size(theme.CHECK_GLYPH)
            theme.draw_bitmap(p, r.x() + 3, r.y() + (r.height() - ch) // 2, theme.CHECK_GLYPH,
                              mark_c)
        elif mark:
            self._draw_text(p, item, mark, QRect(r.x(), r.y(), theme.MENU_LEFT_PAD, r.height()),
                            mark_c, centered=True)
        text_r = r.adjusted(theme.MENU_LEFT_PAD, 0, -theme.MENU_RIGHT_PAD, 0)
        self._draw_text(p, item, item.shown, text_r, fg)
        if item.submenu:
            gw, gh = theme.bitmap_size(theme.SUBMENU_GLYPH)
            theme.draw_bitmap(p, text_r.right() - gw + 1, r.y() + (r.height() - gh) // 2,
                              theme.SUBMENU_GLYPH, key_c)
        elif item.key:
            key = item.key.upper()
            kw = self._text_width(item, key)
            self._draw_text(p, item, key, QRect(text_r.right() - kw + 1, r.y(), kw, r.height()),
                            key_c, centered=True)
            gw, gh = theme.bitmap_size(theme.COMMAND_GLYPH)
            theme.draw_bitmap(p, text_r.right() - kw - 2 - gw + 1,
                              r.y() + (r.height() - gh) // 2, theme.COMMAND_GLYPH, key_c)
        if not (item.enabled and self.menu.enabled):
            paint_dither(p, r, back)

    def _draw_text(self, p: QPainter, item: MenuItem, text: str, r: QRect, fg: QColor,
                   centered: bool = False) -> None:
        if self.strike is not None:
            _draw_strike(p, self.strike, text, r, fg, item.style, centered)
        else:
            align = Qt.AlignmentFlag.AlignCenter if centered else (
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            p.setPen(QPen(fg))
            p.drawText(r, align, text.translate(CHICAGO_SYMBOLS))


class DrawnPulldown(QWidget):
    """A menu its application's own MDEF draws: the image of menuRect it
    drew in, in the frame and shadow the Menu Manager puts round a menu.

    The MDEF highlights items itself, on the application's thread
    (mdefs.Menus), and answers one point at a time: asking says one is
    being answered, want is the point to ask about next, and then is
    what to do once there's nothing more to ask."""

    def __init__(self, menu: Menu, parent: QWidget | None, rect: Rect, image: QImage):
        super().__init__(parent, _MENU_FLAGS)
        self.menu = menu
        self.rect_ = rect               # menuRect, global
        self.image = self.plain = self.lit = image
        self.hilite = 0
        self.asking = False
        self.want: Point | None = None
        self.then: Callable[[], None] | None = None
        _menu_window(self, rect.width + 2, rect.height + 2)

    @property
    def busy(self) -> bool:
        return self.asking or self.want is not None

    def chose(self, item: int, image: QImage | None) -> None:
        """What the MDEF answered."""
        self.hilite = item
        if image is not None:
            self.image = self.lit = image
            if not item:
                self.plain = image
        self.update()

    def set_hilite(self, index: int) -> None:
        """For the blink of the item chosen."""
        self.image = self.lit if index else self.plain
        self.update()

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        _paint_frame(p, self.rect_.width + 2, self.rect_.height + 2)
        p.drawImage(QPoint(1, 1), self.image)


def _follow(display: Display, ask: Callable, pd: DrawnPulldown, gpos: QPoint) -> None:
    """Ask pd's MDEF about the mouse at gpos: mChooseMsg.  One question at
    a time; the latest point waits for the answer to the last."""
    pd.want = display.mac_from_global(gpos)
    if not pd.asking:
        _ask_next(display, ask, pd)


def _ask_next(display: Display, ask: Callable, pd: DrawnPulldown) -> None:
    pt, pd.want = pd.want, None
    pd.asking = True
    ask("choose", pd.menu.id, pt, pd.hilite).add_done_callback(
        lambda f: display.bridge.post(_answered, display, ask, pd, f))


def _answered(display: Display, ask: Callable, pd: DrawnPulldown, f: Future) -> None:
    pd.asking = False
    if f.exception() is not None:
        pd.want = None
        tracker = display.tracker
        if tracker is not None and getattr(tracker, "ask", None) is ask:
            tracker.cancel()                # the application has stopped
        return
    if sip.isdeleted(pd) or not pd.isVisible():     # closed while its MDEF answered
        return
    pd.chose(*f.result())
    if pd.want is not None:
        _ask_next(display, ask, pd)
    elif pd.then is not None:
        then, pd.then = pd.then, None
        then()


def _blink(pd: QWidget, item: int, then: Callable[[], None]) -> None:
    """The item chosen blinks, MENU_BLINKS times, then then()."""
    steps = theme.MENU_BLINKS * 2
    timer = QTimer(pd)
    state = {"n": 0}

    def tick():
        state["n"] += 1
        if state["n"] > steps or not pd.isVisible():
            timer.stop()
            then()
            return
        pd.set_hilite(0 if state["n"] % 2 else item)

    timer.timeout.connect(tick)
    timer.start(theme.MENU_BLINK_MS)


def _drop(widget: QWidget | None) -> None:
    if widget is not None:
        widget.hide()
        widget.deleteLater()


class MenuTracker:
    """Answers one MenuSelect call: follow the mouse until it's released.

    Classic menus are press-drag-release.  With `sticky` set, a quick click
    on a title leaves the menu open until the next click (Mac OS 8 style).
    That also covers the guest being slow to call MenuSelect after a click.

    With ask, the application answers questions on its own thread:
    ask(what, ...) returns a Future that's done when it has (mdefs.Menus).
    Its MBDF hears about each menu before it's pulled down, and may change
    its items (ask("open", menu ID, where it goes)); and its own MDEFs draw
    their menus and say which item the mouse is on (ask("draw", ...),
    ask("choose", ...)).

    A hierarchical item opens its submenu beside it, which stays open as
    the mouse leaves the menu for it; choosing one of its items gives the
    submenu's ID.
    """

    owner: Process | None = None        # the application waiting for it

    def __init__(self, display: Display, bar: MenuBarWidget, future: Future, sticky: bool,
                 ask: Callable[..., Future] | None = None):
        self.display = display
        self.bar = bar
        self.future = future
        self.sticky_allowed = sticky
        self.ask = ask
        self.sticky = False
        self.index = -1
        self.menu: Menu | None = None
        self.pulldown: Pulldown | DrawnPulldown | None = None
        self.sub: Pulldown | DrawnPulldown | None = None    # the submenu open from an item
        self.sub_item = 0                                   # that item
        self.blinking = False
        self.done = False
        self.disabled = 0               # the disabled item released over (MenuChoice)

    # -- tracker protocol (called by Display's input router) ---------------
    def start(self) -> None:
        pos = self.display.input.global_pos
        idx = self.bar.cell_at(self.bar.mapFromGlobal(pos))
        if idx >= 0:
            self._open(idx)
        if not self.display.input.down:
            if self.sticky_allowed and self.menu is not None:
                self.sticky = True
            else:
                self._finish(0)

    def move(self, gpos: QPoint) -> None:
        if self.blinking or (not self.display.input.down and not self.sticky):
            return
        local = self.bar.mapFromGlobal(gpos)
        if self.bar.rect().contains(local):
            idx = self.bar.cell_at(local)
            if idx >= 0:
                self._open(idx)
            if self.pulldown is not None:
                self._hilite_at(self.pulldown, gpos)
                self._close_sub()
        elif self.pulldown is not None:
            self._track(gpos)

    def _hilite_at(self, pd: Pulldown | DrawnPulldown, gpos: QPoint) -> int:
        """Highlight the item of pd at gpos, if any; for a menu the host
        draws, which that is."""
        if isinstance(pd, DrawnPulldown):
            _follow(self.display, self.ask, pd, gpos)
            return 0
        item = pd.selectable_at(pd.mapFromGlobal(gpos)) if pd.geometry().contains(gpos) else 0
        pd.set_hilite(item)
        return item

    def _track(self, gpos: QPoint) -> None:
        pd, sub = self.pulldown, self.sub
        if sub is not None:
            self._hilite_at(sub, gpos)
            if sub.geometry().contains(gpos) or not pd.geometry().contains(gpos):
                return                  # in the submenu, on its way there, or off both
        item = self._hilite_at(pd, gpos)
        if isinstance(pd, DrawnPulldown):
            return
        if item != self.sub_item:
            self._close_sub()
            it = pd.menu.item(item)
            if it is not None and it.submenu:
                self._open_sub(item, it.submenu)

    def _open_sub(self, item: int, menu_id: int) -> None:
        proc = self.bar.process
        menu = proc.menus.get(menu_id) if proc is not None else None
        if menu is None:
            return
        pd = self.pulldown
        r = pd.item_rect(item)
        screen = self.display.screen_bounds
        right_limit = self.display.global_from_mac(Point(0, screen.right)).x()
        self.sub_item = item
        if menu.own_mdef and self.ask is not None:
            # Inside the frame, which hangs from the item's top right.
            at = self.display.mac_from_global(pd.mapToGlobal(QPoint(r.right() + 2, r.y())))
            alt = self.display.mac_from_global(pd.mapToGlobal(QPoint(0, 0))).h - 1
            self.ask("draw", menu.id, at.v, at.h, alt).add_done_callback(
                lambda f: self.display.bridge.post(self._sub_drawn, f, pd, item, menu))
            return
        sub = self._pulldown(menu)
        top_left = pd.mapToGlobal(QPoint(r.right() + 1, r.y() - 1))
        if top_left.x() + sub.width() > right_limit:      # then on the menu's left
            top_left.setX(pd.mapToGlobal(QPoint(0, 0)).x() - sub.width() + theme.MENU_SHADOW)
        sub.move(top_left)
        sub.show()
        self.sub = sub

    def _sub_drawn(self, f: Future, pd: Pulldown, item: int, menu: Menu) -> None:
        if f.exception() is not None:
            self._finish(0)
            return
        if self.done or self.pulldown is not pd or self.sub_item != item or self.sub is not None:
            return
        drawn = f.result()
        if drawn is not None:
            self.sub = self._drawn_menu(menu, *drawn)

    def _pulldown(self, menu: Menu) -> Pulldown:
        return Pulldown(menu, self.bar, self.bar.system_strike(), _menu_colors(self.bar.process))

    def _drawn_menu(self, menu: Menu, rect: Rect, image: QImage) -> DrawnPulldown:
        pd = DrawnPulldown(menu, self.bar, rect, image)
        pd.move(self.display.global_from_mac(Point(rect.top - 1, rect.left - 1)))
        pd.show()
        return pd

    def _close_sub(self) -> None:
        _drop(self.sub)
        self.sub, self.sub_item = None, 0

    def press(self, gpos: QPoint) -> None:
        if self.blinking:
            return
        local = self.bar.mapFromGlobal(gpos)
        if self.bar.rect().contains(local):
            idx = self.bar.cell_at(local)
            if idx < 0 or idx == self.index:
                self._finish(0)
            else:
                self._open(idx)
        elif self.pulldown is not None and (self.pulldown.geometry().contains(gpos) or (
                self.sub is not None and self.sub.geometry().contains(gpos))):
            self.move(gpos)
        else:
            self._finish(0)

    def release(self, gpos: QPoint) -> None:
        if self.blinking or self.done:
            return
        for pd in (self.sub, self.pulldown):
            if isinstance(pd, DrawnPulldown) and pd.busy:
                pd.then = lambda: self.release(gpos)    # once the MDEF has answered
                return
        if self.sub is not None and self.sub.hilite:
            self._choose(self.sub, self.sub.hilite)
            return
        item = self.pulldown.hilite if self.pulldown is not None else 0
        if item and item != self.sub_item:
            self._choose(self.pulldown, item)
            return
        over_bar = self.bar.rect().contains(self.bar.mapFromGlobal(gpos))
        quick = (time.monotonic() - self.display.input.press_time) * 1000 < theme.STICKY_CLICK_MS
        if (over_bar and self.sticky_allowed and self.menu is not None
                and (self.sticky or quick)):
            self.sticky = True
            return
        self.disabled = self._disabled_at(gpos)
        self._finish(0)

    def cancel(self) -> None:
        self._finish(0)

    def _disabled_at(self, gpos: QPoint) -> int:
        """The menu ID and number of the disabled item at gpos in a menu
        the host draws, as MenuChoice gives them; 0 if there's none."""
        for pd in (self.sub, self.pulldown):
            if isinstance(pd, Pulldown) and pd.geometry().contains(gpos):
                item = pd.item_at(pd.mapFromGlobal(gpos))
                if item and not pd.menu.selectable(item):
                    return (pd.menu.id & 0xFFFF) << 16 | item
                return 0
        return 0

    # -- internals ----------------------------------------------------------
    def _open(self, idx: int) -> None:
        if idx == self.index:
            return
        cells = self.bar.cells()
        if idx >= len(cells):
            return
        menu, cell = cells[idx]
        self.index, self.menu = idx, menu
        self.bar.set_hilite(menu.id)
        self._close_sub()
        old = self.pulldown
        self.pulldown = None
        _drop(old)
        if self.ask is None:
            self._show(menu, cell)
            return
        if menu.own_mdef:
            # Its MDEF draws it inside the frame, which hangs from the title.
            at = self.display.mac_from_global(
                self.bar.mapToGlobal(QPoint(cell.x() + 1, self.bar.height())))
            self.ask("draw", menu.id, at.v, at.h).add_done_callback(
                lambda f: self.display.bridge.post(self._asked, f, idx, menu, cell))
            return
        pd = self._place(self._pulldown(menu), cell)
        g = pd.geometry()
        top_left = self.display.mac_from_global(g.topLeft())
        where = Rect(top_left.v, top_left.h, top_left.v + pd.height() - theme.MENU_SHADOW,
                     top_left.h + pd.width() - theme.MENU_SHADOW)
        pd.deleteLater()
        self.ask("open", menu.id, where).add_done_callback(
            lambda f: self.display.bridge.post(self._asked, f, idx, menu, cell))

    def _asked(self, f: Future, idx: int, menu: Menu, cell: QRect) -> None:
        if f.exception() is not None:             # the application has stopped
            self._finish(0)
        elif not self.done and self.index == idx and self.pulldown is None:
            drawn = f.result()          # (once: off its title and back asks again)
            if drawn is None:
                self._show(menu, cell)
                return
            self.pulldown = self._drawn_menu(menu, *drawn)
            if self.display.input.down or self.sticky:
                self._track(self.display.input.global_pos)

    def _place(self, pd: Pulldown, cell: QRect) -> Pulldown:
        top_left = self.bar.mapToGlobal(QPoint(cell.x(), self.bar.height() - 1))
        # Menus may hang past a window's edge, but not off the Mac screen.
        screen = self.display.screen_bounds
        left_limit = self.display.global_from_mac(Point(0, screen.left)).x()
        right_limit = self.display.global_from_mac(Point(0, screen.right)).x()
        if top_left.x() + pd.width() > right_limit:
            top_left.setX(max(right_limit - pd.width(), left_limit))
        pd.move(top_left)
        return pd

    def _show(self, menu: Menu, cell: QRect) -> None:
        self.pulldown = self._place(self._pulldown(menu), cell)
        self.pulldown.show()

    def _choose(self, pd: Pulldown | DrawnPulldown, item: int) -> None:
        self.blinking = True
        _blink(pd, item, lambda: self._chosen(pd.menu, item))

    def _chosen(self, menu: Menu, item: int) -> None:
        if self.done:
            return
        if menu is not None and menu.id == kApplicationMenuID:
            self._finish(0)
            self.display.application_menu_choice(item)
            return
        self._finish((menu.id & 0xFFFF) << 16 | item if menu else 0)

    def _finish(self, result: int) -> None:
        if self.done:
            return
        self.done = True
        self._close_sub()
        _drop(self.pulldown)
        self.pulldown = None
        if result == 0:
            self.bar.set_hilite(0)
        self.display.tracker_finished(self)
        if self.owner is not None:
            self.owner.menu_choice = self.disabled
        if not self.future.done():
            self.future.set_result(result)


class PopUpTracker:
    """Answers one PopUpMenuSelect call: the menu shown with its item
    `item` at `where` (global), followed like a pulled-down menu until the
    mouse is released, or with `sticky` until the next click if it was
    quick.  With ask, the menu's own MDEF places and draws it
    (ask("popup", ...)), as MenuTracker's do theirs.

    A call with the button up and no click since the last pop-up menu
    closed chooses nothing at once, as the Menu Manager follows the mouse
    only while its button is down: Beyond Cyberpunk's CyberDeck stack
    calls PopUpMenuSelect as it opens."""

    owner: Process | None = None        # the application waiting for it

    def __init__(self, display: Display, proc: Process, menu: Menu, where: Point, item: int,
                 future: Future, sticky: bool, ask: Callable[..., Future] | None = None):
        self.display = display
        self.proc = proc
        self.menu = menu
        self.where = where
        self.item = item
        self.future = future
        self.sticky_allowed = sticky
        self.ask = ask
        self.sticky = False
        self.pulldown: Pulldown | DrawnPulldown | None = None
        self.blinking = False
        self.done = False

    def _parent(self) -> QWidget | None:
        return self.display.default_bar(self.proc)

    def start(self) -> None:
        if self.ask is not None and self.menu.own_mdef:
            self.ask("popup", self.menu.id, self.where.v, self.where.h, self.item
                     ).add_done_callback(lambda f: self.display.bridge.post(self._drawn, f))
        else:
            self._show()
        state = self.display.input
        if not state.down:
            if self.sticky_allowed and state.presses != state.popup_presses:
                self.sticky = True
            else:
                self._finish(0)

    def _show(self) -> None:
        pd = Pulldown(self.menu, self._parent(), getattr(self.proc, "system_strike", None),
                      _menu_colors(self.proc))
        n = len(self.menu.items)
        r = pd.item_rect(self.item if 1 <= self.item <= n else 1)
        top_left = self.display.global_from_mac(self.where) - r.topLeft()
        # Kept on the Mac screen, below the menu bar.
        screen = self.display.screen_bounds
        lo = self.display.global_from_mac(Point(screen.top + theme.MENU_BAR_HEIGHT, screen.left))
        hi = self.display.global_from_mac(Point(screen.bottom, screen.right))
        top_left.setX(max(min(top_left.x(), hi.x() - pd.width()), lo.x()))
        top_left.setY(max(min(top_left.y(), hi.y() - pd.height()), lo.y()))
        pd.move(top_left)
        pd.show()
        self.pulldown = pd
        self._track(self.display.input.global_pos)

    def _drawn(self, f: Future) -> None:
        if f.exception() is not None:
            self._finish(0)
            return
        if self.done:
            return
        drawn = f.result()
        if drawn is None:
            self._show()
            return
        rect, image = drawn
        pd = DrawnPulldown(self.menu, self._parent(), rect, image)
        pd.move(self.display.global_from_mac(Point(rect.top - 1, rect.left - 1)))
        pd.show()
        self.pulldown = pd
        self._track(self.display.input.global_pos)

    def _track(self, gpos: QPoint) -> None:
        pd = self.pulldown
        if isinstance(pd, DrawnPulldown):
            _follow(self.display, self.ask, pd, gpos)
        elif pd is not None:
            pd.set_hilite(pd.selectable_at(pd.mapFromGlobal(gpos))
                          if pd.geometry().contains(gpos) else 0)

    # -- tracker protocol ---------------------------------------------------
    def move(self, gpos: QPoint) -> None:
        if self.blinking or (not self.display.input.down and not self.sticky):
            return
        self._track(gpos)

    def press(self, gpos: QPoint) -> None:
        if self.blinking:
            return
        if self.pulldown is not None and self.pulldown.geometry().contains(gpos):
            self._track(gpos)
        else:
            self._finish(0)

    def release(self, gpos: QPoint) -> None:
        if self.blinking or self.done:
            return
        pd = self.pulldown
        if isinstance(pd, DrawnPulldown) and pd.busy:
            pd.then = lambda: self.release(gpos)
            return
        item = pd.hilite if pd is not None else 0
        if item:
            self.blinking = True
            _blink(pd, item, lambda: self._finish((self.menu.id & 0xFFFF) << 16 | item))
            return
        quick = (time.monotonic() - self.display.input.press_time) * 1000 < theme.STICKY_CLICK_MS
        if self.sticky_allowed and (self.sticky or quick):
            self.sticky = True
            return
        self._finish(0)

    def cancel(self) -> None:
        self._finish(0)

    def _finish(self, result: int) -> None:
        if self.done:
            return
        self.done = True
        _drop(self.pulldown)
        self.pulldown = None
        self.display.input.popup_presses = self.display.input.presses
        self.display.tracker_finished(self)
        if not self.future.done():
            self.future.set_result(result)
