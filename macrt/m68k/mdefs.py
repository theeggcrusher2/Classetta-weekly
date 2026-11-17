"""
Applications' own menu definition procedures.

The host draws the menus the System's MDEF 0 defines.  A menu whose
menuProc is the application's own code, an MDEF resource of its own or a
handle to code it made itself, is that code's to size, draw and track
(Inside Macintosh I-362): PROCEDURE MDEF(message: INTEGER; theMenu:
MenuHandle; VAR menuRect: Rect; hitPt: Point; VAR whichItem: INTEGER).
mSizeMsg sets the menu's menuWidth and menuHeight; mDrawMsg draws its
items in menuRect, in the Window Manager port; mChooseMsg highlights the
item under hitPt, unhighlighting whichItem, and says which it is; and
mPopUpMsg works out where a pop-up menu goes, item whichItem at hitPt
(Inside Macintosh V-241).  For mPopUpMsg, hitPt holds the top in its h
and the left in its v, as the words lie on PopUpMenuSelect's stack.

The host's menu shows an image that the MDEF draws in.  While the mouse
is down, MenuSelect and PopUpMenuSelect run the MDEF on the
application's thread for the host's tracker (Menus, bridge.call's serve),
as the application's MBDF is told of each menu that's pulled down.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from typing import Callable

from PyQt6.QtGui import QColor, QImage

from ..toolbox import constants as k
from ..toolbox import lowmem
from ..toolbox.menus import MENU_HEIGHT, MENU_WIDTH, MenuInfo
from ..toolbox.icons import LARGE, bitmap_icon
from ..toolbox.qdraw import (ImageSurface, Region, patBic, patCopy, poly_path, read_rgn,
                             rect_path, srcCopy, write_rgn)
from ..toolbox.quickdraw import BLACK_PAT, GRAY_PAT
from ..toolbox.system import RTS
from ..toolbox.types import Point, Rect
from ..ui import theme
from . import glue_controls


def entry(t, menu: MenuInfo) -> int:
    """Where menu's own MDEF starts; 0 if it has none to run."""
    if not menu.own_mdef:
        return 0
    h = menu.menu_proc & t.mem.system.addr_mask
    p = t.mem.r32(h) & t.mem.system.addr_mask
    if not p:
        res = t.process.resources
        err = res.error
        res.LoadResource(h)             # purged, as a Mac's Menu Manager would find it
        res.error = err
        p = t.mem.r32(h) & t.mem.system.addr_mask
    return p if p and t.mem.r16(p) != RTS else 0


def wmgr_port(t) -> int:
    """The port menus are drawn in: the Window Manager's colour port with
    Color QuickDraw, its GrafPort without."""
    return (t.mem.r32(lowmem.WMgrCPort) or t.mem.r32(lowmem.WMgrPort)) & t.mem.system.addr_mask


def message(t, menu: MenuInfo, msg: int, rect_p: int, hit: int = 0, item_p: int = 0) -> bool:
    """Send menu's MDEF a message, in the Window Manager port, its code
    locked while it runs.  rect_p and item_p point at menuRect and
    whichItem; hit is hitPt as a long.  False if it has no code to run."""
    at = entry(t, menu)
    if not at:
        return False
    mem = t.mem
    h = menu.menu_proc & t.mem.system.addr_mask
    heap = t.system.find_heap(h)       # None if its master pointer is the application's own
    if heap is not None:
        state = heap.get_state(h)
        heap.lock(h)
    var = mem.r32(t.process.partition.a5) & mem.system.addr_mask      # thePort
    saved = mem.r32(var)
    mem.w32(var, wmgr_port(t))
    try:
        t.call(at, (("h", msg), ("l", menu.handle), ("p", rect_p), ("l", hit), ("p", item_p)))
    finally:
        mem.w32(var, saved)
        if heap is not None:
            heap.set_state(h, state)
    return True


def calc_size(t, menu: MenuInfo) -> None:
    """CalcMenuSize: mSizeMsg."""
    with glue_controls.scratch(t, 10) as p:
        t.mem.w_rect(p, Rect(0, 0, 0, 0))
        t.mem.w16(p + 8, 0)
        message(t, menu, k.mSizeMsg, p, 0, p + 8)


def _long(pt: Point) -> int:
    return (pt.v & 0xFFFF) << 16 | (pt.h & 0xFFFF)


class Menus:
    """What the host's tracker asks of the application's MDEFs during one
    MenuSelect or PopUpMenuSelect: ask(what, ...) runs the method of that
    name on the application's thread, holding the machine.

    opening(menu ID, where) tells the application's MBDF of a menu about to
    be pulled down; popup is PopUpMenuSelect's menu, which needn't be in
    the menu list."""

    def __init__(self, t, opening: Callable[[int, Rect], None] | None = None,
                 popup: MenuInfo | None = None):
        self.t = t
        self.opening = opening
        self.popup_menu = popup
        self.shown: dict[int, tuple[Rect, QImage]] = {}     # menu ID -> menuRect, its image

    def __call__(self, what: str, *args):
        t = self.t
        with nullcontext() if t.holding else t.resumed():
            return getattr(self, what)(*args)

    def _menu(self, menu_id: int) -> MenuInfo | None:
        m = self.popup_menu
        if m is not None and m.id == menu_id:
            return m
        return self.t.process.menus.get(menu_id)

    # -- what the tracker asks ----------------------------------------------
    def open(self, menu_id: int, where: Rect) -> None:
        """A menu the host draws is about to be pulled down at where, its
        frame."""
        if self.opening is not None:
            self.opening(menu_id, where)

    def draw(self, menu_id: int, top: int, left: int, alt_right: int | None = None
             ) -> tuple[Rect, QImage] | None:
        """Pull down a menu its MDEF draws, its top left inside the frame at
        (top, left), or as far left as keeps it on the screen; a submenu
        (alt_right set) goes up to keep its bottom on the screen, or
        left of its menu, its right at alt_right.  Its menuRect and what
        the MDEF drew there; None if it has no code."""
        menu = self._menu(menu_id)
        if menu is None or not entry(self.t, menu):
            return None
        calc_size(self.t, menu)
        w, h = menu.size
        rect = self._place(Rect(top, left, top + max(h, 0), left + max(w, 0)), alt_right,
                           alt_right is not None)
        if alt_right is None:
            self.open(menu_id, Rect(rect.top - 1, rect.left - 1, rect.bottom + 1, rect.right + 1))
        return self._draw(menu, rect)

    def choose(self, menu_id: int, pt: Point, item: int) -> tuple[int, QImage | None]:
        """mChooseMsg for the mouse at pt with item highlighted: the item
        now highlighted, and the menu as it is now."""
        shown = self.shown.get(menu_id)
        menu = self._menu(menu_id)
        if shown is None or menu is None:
            return 0, None
        rect, img = shown
        t = self.t
        with glue_controls.scratch(t, 10) as p:
            t.mem.w_rect(p, rect)
            t.mem.w16(p + 8, item)
            with self._drawing(img, rect):
                message(t, menu, k.mChooseMsg, p, _long(pt), p + 8)
            item = t.mem.r16s(p + 8)
        return max(item, 0), img.copy()

    def popup(self, menu_id: int, top: int, left: int, item: int
              ) -> tuple[Rect, QImage] | None:
        """mPopUpMsg, then mDrawMsg: where the MDEF puts the pop-up menu
        to have item at (top, left), and what it drew there."""
        menu = self._menu(menu_id)
        t = self.t
        if menu is None or not entry(t, menu):
            return None
        calc_size(t, menu)
        w, h = menu.size
        t.mem.w16(lowmem.TopMenuItem, top)
        with glue_controls.scratch(t, 10) as p:
            t.mem.w_rect(p, Rect(top, left, top, left))
            t.mem.w16(p + 8, item)
            message(t, menu, k.mPopUpMsg, p, (left & 0xFFFF) << 16 | (top & 0xFFFF), p + 8)
            rect = t.mem.r_rect(p)
        if rect.empty:                      # an MDEF from before pop-up menus
            rect = self._place(Rect(top, left, top + max(h, 0), left + max(w, 0)), None, True)
        return self._draw(menu, rect, popup=True)

    # -- drawing --------------------------------------------------------------
    def _place(self, r: Rect, alt_right: int | None, fit_bottom: bool) -> Rect:
        screen = self.t.system.screen_bits.bounds
        edge = 1 + theme.MENU_SHADOW            # the frame and shadow round the rect
        dh = dv = 0
        if r.right + edge > screen.right:
            if alt_right is not None and alt_right - r.width - 1 >= screen.left:
                dh = alt_right - r.right
            else:
                dh = max(screen.right - edge - r.right, screen.left + 1 - r.left)
        if fit_bottom and r.bottom + edge > screen.bottom:
            dv = max(screen.bottom - edge - r.bottom, screen.top + 1 - r.top)
        return r.offset(dh, dv) if dh or dv else r

    @contextmanager
    def _drawing(self, img: QImage, rect: Rect):
        """The Window Manager port drawing in img, menuRect's image, and
        clipped to menuRect."""
        t = self.t
        mem = t.mem
        port_addr = wmgr_port(t)
        port = t.system.port_at(port_addr)
        surfaces = t.tb.qd.surfaces
        before = surfaces.get(port_addr)
        clip = port.clip_rgn if port is not None else 0
        kept = read_rgn(mem, clip) if clip else None
        if clip:
            write_rgn(t.system.heap_of(clip), clip, Region.rect(rect))
        surfaces[port_addr] = ImageSurface(img, rect)
        try:
            yield
        finally:
            if before is None:
                surfaces.pop(port_addr, None)
            else:
                surfaces[port_addr] = before
            if kept is not None:
                write_rgn(t.system.heap_of(clip), clip, kept)

    def _draw(self, menu: MenuInfo, rect: Rect, popup: bool = False) -> tuple[Rect, QImage]:
        t = self.t
        img = QImage(max(rect.width, 1), max(rect.height, 1), QImage.Format.Format_RGB32)
        img.fill(QColor(255, 255, 255))
        if not popup:
            t.mem.w16(lowmem.TopMenuItem, rect.top)
        t.mem.w16(lowmem.AtMenuBottom, rect.bottom)
        with glue_controls.scratch(t, 10) as p:
            t.mem.w_rect(p, rect)
            t.mem.w16(p + 8, 0)
            with self._drawing(img, rect):
                message(t, menu, k.mDrawMsg, p, 0, p + 8)
        self.shown[menu.id] = (rect, img)
        return rect, img.copy()


# =========================================================================
# The System's MDEF 0, for an application's MDEF that calls it
# =========================================================================
# An application's MDEF may leave the menu to MDEF 0's code, with the port
# set up as it likes: Theldrow's sets its own font, then jumps to it.  So
# this MDEF 0 draws with QuickDraw, in the current port's font and in its
# image of the menu, rather than as the host draws the menus it shows.
ICON_SIZE = 32
ICON_GAP = 4


def _current_port(t):
    var = t.mem.r32(t.process.partition.a5) & t.mem.system.addr_mask
    return t.system.port_at(t.mem.r32(var) & t.mem.system.addr_mask)


class _Layout:
    """Where MDEF 0 puts menu's items in the current port's font: rows,
    (top offset, height) per item, and the menu's width and height."""

    def __init__(self, t, menu: MenuInfo, port):
        tb = t.tb
        self.mem = t.mem
        self.items = menu.items
        self.ascent, self.descent, _, leading = tb.GetFontInfo()
        line = self.ascent + self.descent + leading
        self.rows: list[tuple[int, int]] = []
        face = port.tx_face
        width = key_w = 0
        icons = any(it.icon for it in self.items)
        y = 0
        try:
            for it in self.items:
                h = max(line, ICON_SIZE + ICON_GAP) if it.icon else line
                self.rows.append((y, h))
                y += h
                if it.is_separator:
                    continue
                port.tx_face = it.style & 0x7F
                width = max(width, tb.StringWidth(it.shown))
                port.tx_face = 0
                if it.submenu:
                    key_w = max(key_w, line // 2)
                elif it.key:
                    key_w = max(key_w, tb.StringWidth(k.commandMark + it.key))
        finally:
            port.tx_face = face
        self.text_left = theme.MENU_LEFT_PAD + (ICON_SIZE + ICON_GAP if icons else 0)
        self.width = self.text_left + width + theme.MENU_RIGHT_PAD
        if key_w:
            self.width += theme.MENU_KEY_GAP + key_w
        self.height = y

    def top(self, rect: Rect) -> int:
        """Where item 1 is: TopMenuItem, unless that's not this menu's."""
        top = self.mem.r16s(lowmem.TopMenuItem)
        return top if rect.top - self.height <= top <= rect.top else rect.top

    def item_rect(self, rect: Rect, top: int, index: int) -> Rect | None:
        if not 1 <= index <= len(self.rows):
            return None
        y, h = self.rows[index - 1]
        return Rect(top + y, rect.left, top + y + h, rect.right)

    def item_at(self, rect: Rect, top: int, pt: Point) -> int:
        if not rect.contains(pt):
            return 0
        for i, (y, h) in enumerate(self.rows, 1):
            if top + y <= pt.v < top + y + h:
                return i
        return 0


def system_mdef(t, msg: int, h: int, rect_p: int, hit: Point, item_p: int) -> None:
    """MDEF 0's code: size, draw, choose and pop up h's menu in the current
    port, as the Menu Manager's own does (Inside Macintosh I-362, V-241)."""
    port = _current_port(t)
    if port is None or not h:
        return
    menu = t.menu(h & t.mem.system.addr_mask)
    mem = t.mem
    layout = _Layout(t, menu, port)
    if msg == k.mSizeMsg:
        mem.w16(menu.ptr + MENU_WIDTH, layout.width)
        mem.w16(menu.ptr + MENU_HEIGHT, layout.height)
        return
    rect = mem.r_rect(rect_p)
    if msg == k.mDrawMsg:
        _draw_items(t, menu, layout, port, rect, layout.top(rect))
    elif msg == k.mChooseMsg:
        top = layout.top(rect)
        old = mem.r16s(item_p)
        new = layout.item_at(rect, top, hit)
        if new and not menu.selectable(new):
            new = 0
        if new != old:
            for i in (old, new):
                r = layout.item_rect(rect, top, i)
                if r is not None:
                    t.tb.InvertRect(r)
            mem.w16(item_p, new)
    elif msg == k.mPopUpMsg:
        top, left = hit.h, hit.v            # as PopUpMenuSelect's words lie
        index = mem.r16s(item_p)
        y = layout.rows[index - 1][0] if 1 <= index <= len(layout.rows) else 0
        r = Rect(top - y, left, top - y + layout.height, left + layout.width)
        screen = t.system.screen_bits.bounds
        edge = 1 + theme.MENU_SHADOW
        least = screen.top + mem.r16s(lowmem.MBarHeight) + 1
        dv = dh = 0
        if r.bottom + edge > screen.bottom:
            dv = screen.bottom - edge - r.bottom
        if r.top + dv < least:
            dv = least - r.top
        if r.right + edge > screen.right:
            dh = max(screen.right - edge - r.right, screen.left + 1 - r.left)
        r = r.offset(dh, dv)
        mem.w_rect(rect_p, r)
        mem.w16(lowmem.TopMenuItem, r.top)


def _draw_items(t, menu: MenuInfo, layout: _Layout, port, rect: Rect, top: int) -> None:
    tb = t.tb
    qd = tb.qd
    tb.EraseRect(rect)
    face = port.tx_face
    asc, desc = layout.ascent, layout.descent
    try:
        for i, it in enumerate(layout.items, 1):
            r = layout.item_rect(rect, top, i)
            if r.bottom <= rect.top or r.top >= rect.bottom:
                continue
            if it.is_separator:
                mid = r.top + r.height // 2
                qd.fill(port, rect_path(Rect(mid, r.left, mid + 1, r.right)), GRAY_PAT, patCopy)
                continue
            base = r.top + (r.height - asc - desc) // 2 + asc
            if it.mark and not it.submenu:
                port.tx_face = 0
                tb.MoveTo(r.left + 2, base)
                tb.DrawString(it.mark)
            if it.icon:
                at = Point(r.top + ICON_GAP // 2, r.left + theme.MENU_LEFT_PAD)
                _draw_icon(t, port, it.icon,
                           Rect(at.v, at.h, at.v + ICON_SIZE, at.h + ICON_SIZE))
            port.tx_face = it.style & 0x7F
            tb.MoveTo(r.left + layout.text_left, base)
            tb.DrawString(it.shown)
            port.tx_face = 0
            right = r.right - theme.MENU_RIGHT_PAD
            if it.submenu:
                mid, s = r.top + r.height // 2, max(asc // 3, 2)
                qd.fill(port, poly_path([Point(mid - s, right - s), Point(mid, right),
                                         Point(mid + s, right - s), Point(mid - s, right - s)]),
                        BLACK_PAT, patCopy)
            elif it.key:
                s = k.commandMark + it.key
                tb.MoveTo(right - tb.StringWidth(s), base)
                tb.DrawString(s)
            if not (menu.enabled and it.enabled):
                qd.fill(port, rect_path(r), GRAY_PAT, patBic)
    finally:
        port.tx_face = face


def _draw_icon(t, port, number: int, r: Rect) -> None:
    res = t.process.resources
    err = res.error
    h = res.GetResource("ICON", 256 + number)
    res.error = err
    if not h:
        return
    data = t.tb.contents(h)
    icon = bitmap_icon(data, LARGE, masked=False) if data else None
    if icon is not None:
        t.tb.qd.draw_bits(port.addr + 2, r, icon.image(), srcCopy, None, port)
