"""
Pop-up menu controls: the System's CDEF 63 (procIDs 1008 to 1023, in the
Control Manager chapter of Inside Macintosh VI), drawn and tracked in
Python as CDEF 0 and 1 are, so they work with any System file or none.

A pop-up control is a title and a box that shows the item chosen from its
menu, with an arrow pointing down.  Pressing it pops the menu up over the
box, with the chosen item checked, and the item the mouse is released on
becomes the control's value.

When it's made, its fields mean something else: contrlMin is the MENU's
resource ID, contrlMax the title's width, contrlValue the title's style
and justification, and, with popupUseAddResMenu, contrlRefCon a resource
type whose names are added to the menu.  initCntl makes them an ordinary
control's: the value is an item, from 1 to the number of items.

contrlData is a handle to the control's data.  Its first two fields are
public (popupPrivateData): the menu's handle and its ID.  The rest is
macrt's.  The menu is the one in the menu list with that ID if the
application put it there, else the one the control has while it's still
there, else its MENU resource, which a pop-up disposed of releases.
"""

from __future__ import annotations

from contextlib import contextmanager

from PyQt6.QtGui import QPainterPath

from ..toolbox import constants as k
from ..toolbox.controls import Control, Painter
from ..toolbox.qdraw import CHICAGO_SYMBOLS, patXor, rect_path
from ..toolbox.quickdraw import BLACK_PAT, set_rect_rgn
from ..toolbox.types import Point, Rect
from ..ui import theme
from . import dispatch, mdefs

CDEF_ID = 63

# Variations
popupFixedWidth = 1         # the box is as wide as the control, not the menu
popupUseAddResMenu = 4      # contrlRefCon's resource type's names are added
popupUseWFont = 8           # the window's font, not the system font

# contrlValue as made: the title's style, and its justification in the low byte
popupTitleNoStyle = 0x8000
teCenter, teFlushRight = 1, -1

# The data contrlData holds
MENU_HANDLE = 0             # popupPrivateData.mHandle
MENU_ID = 4                 # popupPrivateData.mID
RES_ID = 6                  # the MENU's resource ID, as made
TITLE_WIDTH = 8
TITLE_STYLE = 10
JUSTIFY = 11
DATA_SIZE = 0x3A            # as the System's

BOX_EXTRA = 3               # the box is a line of text and this much high
ARROW_WIDTH = 11            # the down arrow: rows of 11, 9, ... 1 pixels
ARROW_RIGHT = 8             # from the arrow to the box's right edge, inside
ELLIPSIS = "…"


def is_popup(proc_id: int) -> bool:
    return proc_id >> 4 == CDEF_ID


def _data(t, c: Control) -> int:
    h = c.data & t.mem.system.addr_mask
    return t.mem.r32(h) & t.mem.system.addr_mask if h else 0


def _menu(t, c: Control):
    """The control's menu, found again each time: the application may
    have disposed of it, or put it in the menu list.
    The one the control has is kept while it's still that menu, as it may
    no longer be in the current resource file's chain (Text's, filled with
    the System's file current)."""
    d = _data(t, c)
    menu_id = t.mem.r16s(d + MENU_ID)
    m = t.tb.GetMHandle(menu_id)
    if m is None:
        m = _kept(t, t.mem.r32(d + MENU_HANDLE) & t.mem.system.addr_mask, menu_id)
    if m is None:
        rm = t.process.resources
        err = rm.error
        m = t.tb.GetMenu(t.mem.r16s(d + RES_ID))
        rm.error = err
    t.mem.w32(d + MENU_HANDLE, m.handle if m is not None else 0)
    if m is not None:
        t.mem.w16(d + MENU_ID, m.id)
    return m


def menu_of(t, c: Control):
    """The control's menu, None if it hasn't one."""
    return _menu(t, c) if _data(t, c) else None


def _kept(t, h: int, menu_id: int):
    heap = t.system.find_heap(h)
    if heap is None:
        return None
    m = t.menu(h) if heap.is_handle(h) else None
    return m if m is not None and m.id == menu_id else None


def message(t, c: Control, msg: int, param: int) -> int:
    """A CDEF 63 message, sent in the owner window's port."""
    if msg == k.initCntl:
        _init(t, c)
        return 0
    if not _data(t, c):
        return 0
    menu = _menu(t, c)
    if msg == k.dispCntl:
        _dispose(t, c, menu)
        return 0
    if menu is None:
        return 0
    if msg == k.drawCntl:
        if c.visible:
            _draw(t, c, menu)
    elif msg == k.testCntl:
        pt = Point(_s16(param >> 16), _s16(param))
        if c.visible and c.active and _Geometry(t, c, menu).area.contains(pt):
            return 1
    elif msg in (k.calcCRgns, k.calcCntlRgn, k.calcThumbRgn):
        rgn = param & t.mem.system.addr_mask
        set_rect_rgn(t.system.heap_of(rgn), rgn, _Geometry(t, c, menu).area)
    elif msg == k.autoTrack:
        return _track(t, c, menu)
    return 0


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _init(t, c: Control) -> None:
    h = t.process.heap.new_handle(DATA_SIZE, clear=True)
    c.data = h
    if not h:
        return
    d = _data(t, c)
    mem = t.mem
    mem.w16(d + MENU_ID, c.min)
    mem.w16(d + RES_ID, c.min)
    if c.max > 1:
        mem.w16(d + TITLE_WIDTH, c.max)
    title = c.value & 0xFFFF
    mem.w8(d + JUSTIFY, title & 0xFF)
    style = 0 if title & popupTitleNoStyle else (title & 0x7F00) >> 8
    if c.variation & popupUseWFont:
        port = t.window(c.owner)
        style |= port.tx_face if port is not None else 0
    mem.w8(d + TITLE_STYLE, style)
    menu = _menu(t, c)
    if menu is not None and c.variation & popupUseAddResMenu:
        dispatch.IMPLEMENTATIONS["AppendResMenu"](t, menu.handle, c.ref_con)
    count = menu.count() if menu is not None else 0
    c.min = 1 if count else 0
    c.max = count
    c.value = c.min
    c.action = 0xFFFFFFFF                   # TrackControl's autoTrack: the menu


def _dispose(t, c: Control, menu) -> None:
    """The menu goes too, unless the application put it in the menu list."""
    if menu is not None and t.tb.GetMHandle(menu.id) is None:
        t.tb.DisposeMenu(menu)
    t.process.heap.dispose_handle(c.data & t.mem.system.addr_mask)
    c.data = 0


@contextmanager
def _font(t, c: Control):
    """The control's port in its font: the system font, or with
    popupUseWFont the window's."""
    port = t.window(c.owner)
    saved = (port.tx_font, port.tx_face, port.tx_size, port.tx_mode, port.pn_loc)
    try:
        if not c.variation & popupUseWFont:
            port.tx_font, port.tx_size = 0, 0
        port.tx_face = 0
        port.tx_mode = k.srcOr
        yield port
    finally:
        port.tx_font, port.tx_face, port.tx_size, port.tx_mode, port.pn_loc = saved


class _Geometry:
    """Where a pop-up's parts are: a line of text high, centred in its
    rect, the title at the left, then the box as wide as the menu (or the
    control, popupFixedWidth), with a shadow below and at the right."""

    def __init__(self, t, c: Control, menu):
        d = _data(t, c)
        self.title_width = t.mem.r16s(d + TITLE_WIDTH)
        self.style = t.mem.r8(d + TITLE_STYLE)
        self.justify = t.mem.r8(d + JUSTIFY)
        if self.justify & 0x80:
            self.justify -= 0x100
        r = c.rect
        with _font(t, c) as port:
            asc, desc, wid_max, leading = t.tb.qd.font_info(port)
            menu_width = mdefs._Layout(t, menu, port).width if menu.count() else 0
        self.ascent, self.descent = asc, desc
        high = asc + desc + leading + BOX_EXTRA
        top = max(r.top, r.top + ((r.height - high) >> 1))
        bottom = min(r.bottom, top + high)
        width = wid_max + self.title_width + BOX_EXTRA + menu_width if menu_width else r.width
        width = r.width if c.variation & popupFixedWidth else min(width, r.width)
        left = r.left
        if not self.title_width and self.justify == teCenter:
            left += (r.width - width) >> 1
        self.area = Rect(top, left, bottom, left + width)
        self.title = Rect(top, left, bottom - 1, left + self.title_width)
        self.box = Rect(top, left + self.title_width, bottom - 1, left + width - 1)

    def baseline(self, r: Rect) -> int:
        return r.top + (r.height - self.ascent - self.descent) // 2 + self.ascent


def _fitted(qd, port, text: str, width: int) -> str:
    """text, shortened with an ellipsis to fit width."""
    if qd.text_width(port, text) <= width:
        return text
    while text and qd.text_width(port, text + ELLIPSIS) > width:
        text = text[:-1]
    return text + ELLIPSIS


def _draw(t, c: Control, menu) -> None:
    g = _Geometry(t, c, menu)
    qd = t.tb.qd
    with _font(t, c) as port:
        p = Painter(qd, port)
        p.erase(rect_path(c.rect))
        title, box = g.title, g.box
        if g.title_width:
            port.tx_face = g.style
            text = _fitted(qd, port, c.title.translate(CHICAGO_SYMBOLS), title.width)
            width = qd.text_width(port, text)
            h = title.left
            if g.justify == teCenter:
                h += (title.width - width) // 2
            elif g.justify == teFlushRight:
                h = title.right - width
            port.pn_loc = Point(g.baseline(title), h)
            qd.draw_text(port, text)
            port.tx_face = 0
        p.frame(rect_path, box)
        p.fill(rect_path(Rect(box.bottom, box.left + 2, box.bottom + 1, box.right + 1)))
        p.fill(rect_path(Rect(box.top + 2, box.right, box.bottom + 1, box.right + 1)))
        arrow = box.right - 1 - ARROW_RIGHT - ARROW_WIDTH
        text_left = box.left + 1 + theme.MENU_LEFT_PAD    # where the menu's text pops up
        it = menu.item(c.value)
        if it is not None:
            port.pn_loc = Point(g.baseline(box), text_left)
            text = _fitted(qd, port, it.shown.translate(CHICAGO_SYMBOLS), arrow - 2 - text_left)
            qd.draw_text(port, text)
        rows = (ARROW_WIDTH + 1) // 2
        top = box.top + (box.height - rows) // 2
        path = QPainterPath()
        for i in range(rows):
            path.addRect(arrow + i, top + i, ARROW_WIDTH - 2 * i, 1)
        p.fill(path)
        if not c.active:
            p.dim(title)
            p.dim(box.inset(1, 1))
        elif c.hilite and g.title_width:
            p.fill(rect_path(title), BLACK_PAT, patXor)


def _track(t, c: Control, menu) -> int:
    """autoTrack: pop the menu up with its item over the box's, checked,
    and make the item chosen the value.  1 if one was."""
    w = t.window(c.owner)
    box = _Geometry(t, c, menu).box
    at = w.local_to_global(Point(box.top + 1, box.left + 1))
    item = c.value
    it = menu.item(item)
    marked = it is not None and not it.submenu
    if marked:
        t.tb.SetItemMark(menu, item, k.checkMark)
    try:
        chosen = dispatch.IMPLEMENTATIONS["PopUpMenuSelect"](t, menu.handle, at.v, at.h, item)
    finally:
        if marked:
            t.tb.CheckItem(menu, item, False)
    menu_id, item = _s16(chosen >> 16), chosen & 0xFFFF
    if not menu_id:
        return 0
    if menu_id != menu.id:                  # from a submenu: its item in this menu
        item = next((i for i, it in enumerate(menu.items, 1) if it.submenu == menu_id), 0)
    c.value = item
    return 1
