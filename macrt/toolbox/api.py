"""
The Toolbox as a guest application sees it.

Method names, arguments and results follow Inside Macintosh, so the 68k
trap dispatcher can call these directly once it pops arguments off the
guest stack.  Until then, Python guests (see macrt.demo) call them the
same way a Pascal or C Mac program would.

Windows, menus and ports are records in the process's partition of guest
memory (see macrt.toolbox.windows and .menus); these calls read and write
them there.  Resources come from the process's resource chain (see
macrt.toolbox.resources): its application file, files it opened, and the
System file.

Guest code runs on its own thread.  Window and Menu Manager calls hop to
the GUI thread through the display's bridge.  Calls that track the mouse
(MenuSelect, TrackGoAway, DragWindow...) block until the user lets go.
Anything the guest thread reads while the GUI thread may be changing it
(the window list, update regions) is read under the System lock.
"""

from __future__ import annotations

import contextlib
import threading
import time
from functools import partial
from typing import TYPE_CHECKING, Callable

from PyQt6.QtWidgets import QApplication

from ..memory import ADDRESS_MASK
from ..ui import theme
from . import constants as k
from . import lowmem, qdraw
from .bitfont import AppFonts
from .events import EventRecord, ProcessKilled, tick_count
from .resources import ResourceManager, resNotFound
from .menus import (HELP_ITEMS, LAST_FLAGGED_ITEM, MENU_PROC, MenuInfo, mctb_entries,
                    menu_color_table, new_menu_list, parse_menu_items, set_mct_entries,
                    write_mct)
from .qdraw import CHICAGO_SYMBOLS
from .quickdraw import BLACK, WHITE, GrafPort
from .templates import parse_mbar, parse_str, parse_str_list, parse_wind
from .types import Point, Rect
from .windows import WindowRecord

if TYPE_CHECKING:
    from ..process import Process
    from ..ui.display import Display
    from .bitfont import Strike

IN_FRONT = -1   # NewWindow's behind = (WindowPtr)-1
MOUSE_EVENTS = k.event_mask(k.mouseDown) | k.event_mask(k.mouseUp)
MOUSE_UP_WAIT = 0.1             # seconds, for the host to post a release's mouse-up
MENU_SIZES_KEPT = 64            # menus' sizes worked out, kept by their contents


class Toolbox:
    def __init__(self, display: Display, process: Process):
        self._d = display
        self._p = process
        self._sys = process.system
        self._mem = process.system.mem
        self._grown: WindowRecord | None = None     # GrowWindow's preview, until SizeWindow
        self.qd = qdraw.QuickDraw(self._sys, qdraw.Fonts(theme.menu_item_font()), self._repaint)
        self.qd.app_fonts = AppFonts(process.resources, qdraw.STANDARD_FAMILIES)
        self.qd.fonts.measured = self.qd.app_fonts.widths
        self.qd.fonts.lines = self._sys.font_lines
        # Family 1 is ApFontID's, which a program may set: Gemstone Warrior
        # makes it Chicago, and brings a Chicago of its own.
        self.qd.fonts.app_font = self.qd.app_fonts.app_font = \
            lambda: self._mem.r16(lowmem.ApFontID)
        self._sys.screen.attach(process, self._repaint)
        self._dirty: set[WindowRecord] = set()      # windows drawn in since their views showed it
        self._dirty_lock = threading.Lock()
        self._bars_due = False                      # a refresh_bars posted, not yet run
        # The application's own WDEFs, for a 68k application: its runtime's
        # macrt.m68k.wdefs.WindowDefinitions.
        self.wdefs = None
        # size_menu's sizes by the menus' contents, with the strike they're
        # in: The Secret Island of Dr. Quandary sets its items' marks and
        # text again on each pass of its event loop, changing nothing.
        self._menu_sizes: dict[tuple, tuple] = {}

    def _gui(self, fn, *args):
        return self._d.bridge.call(fn, *args)

    def _track(self, fn, *args, serve: Callable | None = None):
        """_gui for a call that follows the mouse, which the application
        being stopped ends there and then."""
        return self._d.bridge.call(fn, *args, serve=serve, killed=lambda: self._p.events.killed)

    def contents(self, h: int) -> bytes:
        """What a handle holds (68k code would just dereference it)."""
        with self._sys.lock:
            heap = self._sys.heap_of(h)
            return self._mem.read(heap.deref(h), heap.handle_size(h))

    @property
    def _the_port(self) -> int:
        """Address of the QuickDraw global thePort; 0(A5) points at it."""
        return self._mem.r32(self._p.partition.a5) & ADDRESS_MASK

    @property
    def _port(self) -> GrafPort | None:
        return self._sys.port_at(self._mem.r32(self._the_port))

    # =====================================================================
    # Event Manager
    # =====================================================================
    def WaitNextEvent(self, mask: int = k.everyEvent, sleep: int = 0,
                      mouse_rgn: qdraw.Region | None = None) -> tuple[bool, EventRecord]:
        """With mouse_rgn, a region in global coordinates, a mouse-moved
        event whenever the mouse is out of it (Technical Note OV17: "In
        System 6.0 and later, _WaitNextEvent returns a mouse-moved event
        whenever the cursor is outside the mouse region"), an empty one
        too.  Inside Macintosh: Macintosh Toolbox Essentials has it that
        an empty one gives none; but Olive Wars! passes one that it never
        changes, draws its frames only when it's given an event, and
        passes over a mouse-moved event whose mouse hasn't moved."""
        self._end_grow_preview()
        if not mask & k.event_mask(k.osEvt):
            mouse_rgn = None
        return self._p.events.get(mask, sleep, self._next_update, mouse_rgn=mouse_rgn)

    def GetNextEvent(self, mask: int = k.everyEvent) -> tuple[bool, EventRecord]:
        self._end_grow_preview()
        return self._p.events.get(mask, 0, self._next_update)

    def _end_grow_preview(self) -> None:
        """Done with the event that grew a window, and it wasn't resized
        after all: it goes back to its size."""
        w, self._grown = self._grown, None
        if w is not None:
            self._gui(self._d.end_grow_preview, w)

    def _next_update(self, picture: bool = False) -> WindowRecord | None:
        """The frontmost window with some of its update region to be seen:
        one the windows in front of it cover wholly is updated once it's
        uncovered, as on a Mac, where it has nothing that can be seen to
        draw.  King's Quest makes its status window behind its picture,
        and shows it when it's updated.  One with a picture of its own
        (SetWindowPic) isn't sent an update event: picture asks for one of
        those, for the Window Manager to draw it (Inside Macintosh I,
        Window Manager, CheckUpdate)."""
        with self._sys.lock:
            covered, in_front = None, []
            for w in self._p.windows:
                if not w.visible:
                    continue
                if w.update_pending and bool(w.window_pic) == picture:
                    for f in in_front:      # what covers it, only once there's one to update
                        covered = f.covers() if covered is None else covered.union(f.covers())
                    in_front.clear()
                    if covered is None or not w.update.diff(covered).empty:
                        return w
                in_front.append(w)
        return None

    def FlushEvents(self, mask: int = k.everyEvent, stop_mask: int = 0) -> None:
        self._p.events.flush(mask, stop_mask)

    def Button(self) -> bool:
        self._p.events.asked()
        return self._d.input.down

    def StillDown(self) -> bool:
        """Inside Macintosh I, Toolbox Event Manager: the button is down
        and there are no more mouse events pending in the queue."""
        self._p.events.asked()
        return self._d.input.down and not self._p.events.peek(MOUSE_EVENTS, self._next_update)[0]

    def WaitMouseUp(self) -> bool:
        """StillDown, except that if the button isn't still down from the
        press, the mouse-up event is taken from the queue."""
        if self.StillDown():
            return True
        self.take_mouse_up()
        return False

    def take_mouse_up(self) -> None:
        """The mouse-up that ended a press, out of the queue, as the
        Toolbox's tracking calls take it.  The host posts it just after the
        button comes up: a release it hasn't finished with is waited for."""
        events, state = self._p.events, self._d.input
        deadline = time.monotonic() + MOUSE_UP_WAIT
        while not events.get(k.event_mask(k.mouseUp), 0, self._next_update)[0]:
            if state.releases == state.presses or time.monotonic() > deadline:
                return
            time.sleep(0.002)

    def GetMouse(self) -> Point:
        """Mouse location, local to the current port."""
        pt = self._d.input.mac_pos
        port = self._port
        return pt if port is None else port.global_to_local(pt)

    def TickCount(self) -> int:
        return tick_count()

    def SysBeep(self, duration: int = 1) -> None:
        self._d.bridge.post(QApplication.beep)

    # =====================================================================
    # Window Manager
    # =====================================================================
    def GetMBarHeight(self) -> int:
        return self._mem.r16s(lowmem.MBarHeight)

    def ScreenBounds(self) -> Rect:
        """qd.screenBits.bounds"""
        return self._sys.screen_bits.bounds

    def NewWindow(self, bounds: Rect, title: str, visible: bool = True,
                  proc_id: int = k.documentProc, behind=IN_FRONT,
                  go_away: bool = True, ref_con: int = 0,
                  storage: int = 0, color: bool = False) -> WindowRecord | None:
        """NewWindow, or with color NewCWindow, a window that's a cGrafPort."""
        def gui():
            p, system = self._p, self._sys
            with system.lock:
                rec = WindowRecord.new(system, p, p.heap, storage, bounds, title, visible,
                                       proc_id, go_away, ref_con, color)
                if rec is None:
                    return None
                rec.title_width = self._title_width(title)
                if rec.definition.zoom:
                    rec.std_state = self._d.standard_state(rec)
                    rec.user_state = bounds
                prev = p.front_window()
                if behind == IN_FRONT:
                    p.windows.insert_front(rec)
                elif isinstance(behind, WindowRecord) and behind in p.windows:
                    p.windows.insert_behind(rec, behind)
                else:
                    p.windows.insert_back(rec)
            self._d.create_view(rec)
            self._rehilite(prev)
            p.events.wake()
            return rec
        rec = self._gui(gui)
        if rec is not None and self.wdefs is not None and rec.own_wdef:
            self.wdefs.created(rec)
        return rec

    def GetNewWindow(self, window_id: int, storage: int = 0, behind=IN_FRONT,
                     color: bool = False, colored=None) -> WindowRecord | None:
        """NewWindow (NewCWindow with color) from a WIND resource; with
        colored, what's done to the window before it's shown, if its
        template says it's visible, in the colours that gives it."""
        h = self._p.resources.GetResource("WIND", window_id)
        if not h:
            return None
        t = parse_wind(self.contents(h))
        w = self.NewWindow(self.placed(t.bounds, t.position, t.proc_id), t.title,
                           t.visible and colored is None, t.proc_id, behind, t.go_away,
                           t.ref_con, storage, color)
        if w is not None and colored is not None:
            colored(w)
            if t.visible:
                self.ShowWindow(w)
        return w

    def placed(self, bounds: Rect, position: int, proc_id: int) -> Rect:
        """Where a template's positioning word puts a window of this kind."""
        return self._gui(self._d.place_window, bounds, position, proc_id)

    def _close_window(self, w: WindowRecord, dispose: bool) -> None:
        if self.wdefs is not None and w.own_wdef and w in self._p.windows:
            self.wdefs.disposing(w)

        def gui():
            p = self._p
            if w not in p.windows:
                return
            prev = p.front_window()
            was_active = p.events.active is w
            with self._sys.lock:
                p.windows.remove(w)
            p.events.forget(w)
            self._d.destroy_view(w)
            self._rehilite(None if prev is w else prev)
            cur = p.front_window()
            if was_active and cur is not None and p.events.active is None and self._d.is_front(p):
                # Closing the active window activates the one now in front,
                # though the program highlighted it already: HyperCard 2
                # hides an alert with ShowHide and highlights the card
                # window before it disposes of the alert, and takes clicks
                # in the card again only once that window is activated.
                p.events.post_activate(cur, True)
            with self._sys.lock:
                if (self._mem.r32(self._the_port) ^ w.addr) & ADDRESS_MASK == 0:
                    self._mem.w32(self._the_port, 0)
                if dispose:
                    w.dispose()
                else:
                    w.close()
        self._gui(gui)

    def CloseWindow(self, w: WindowRecord) -> None:
        self._close_window(w, dispose=False)

    def DisposeWindow(self, w: WindowRecord) -> None:
        self._close_window(w, dispose=True)

    def FrontWindow(self) -> WindowRecord | None:
        return self._p.front_window()

    def FindWindow(self, pt: Point) -> tuple[int, WindowRecord | None]:
        part, w = self._gui(self._d.find_window, self._p, pt)
        if part == k.ASK_WDEF:
            code = self.wdefs.hit(w, pt) if self.wdefs is not None else k.wInContent
            if code == k.wNoHit:
                return k.inDesk, None
            part = k.WHIT_TO_PART.get(code, k.inContent)
        return part, w

    def SelectWindow(self, w: WindowRecord) -> None:
        """Bring w to the front and activate it: and the window that was in
        front deactivated, whether or not either was highlighted, as the
        Window Manager's CurDeactive and CurActivate have it.  Multiplan
        keeps its formula bar's windows in front of the sheet, which stays
        the highlighted one, and selects the sheet when it's clicked in:
        it goes on with the click once it's had the events."""
        def gui():
            p = self._p
            prev = p.front_window()
            if prev is None or prev is w or w not in p.windows or not self._d.is_front(p):
                if w in p.windows:
                    with self._sys.lock:
                        p.windows.bring_to_front(w)
                self._rehilite(prev)
                return
            with self._sys.lock:
                p.windows.bring_to_front(w)
            for x, on in ((prev, False), (w, True)):
                if x.hilited != on:
                    x.hilited = on
                    self._d.sync(x)
                p.events.post_activate(x, on)
            self._d.restack()
            self._d.focus(w)
        self._gui(gui)

    def BringToFront(self, w: WindowRecord) -> None:
        def gui():
            if w in self._p.windows:
                with self._sys.lock:
                    self._p.windows.bring_to_front(w)
            self._d.restack()
        self._gui(gui)

    def SendBehind(self, w: WindowRecord, behind: WindowRecord | None) -> None:
        """Move w behind behind, or behind all the others for None.  If w
        was the front window the next one becomes active (Inside
        Macintosh I-286)."""
        def gui():
            p = self._p
            if w is behind or w not in p.windows:
                return
            prev = p.front_window()
            with self._sys.lock:
                p.windows.remove(w)
                if behind is not None and behind in p.windows:
                    p.windows.insert_behind(w, behind)
                else:
                    p.windows.insert_back(w)
            self._rehilite(prev)
        self._gui(gui)

    def ShowWindow(self, w: WindowRecord) -> None:
        """Show w, its content erased and to be updated, as PaintOne
        leaves it: nothing drawn in it while it was hidden is kept, and
        SimCity draws its copy protection's question in the window its
        scenario's story was in."""
        def gui():
            if w.visible:
                return
            prev = self._p.front_window()
            self._shown(w)
            self._d.sync(w)
            self._rehilite(prev)
            self._p.events.wake()
        self._gui(gui)

    def _shown(self, w: WindowRecord) -> None:
        """Make w visible, its content erased and to be updated, and its
        regions worked out again from its portRect: the Window Manager
        has them worked out only while the window is visible (Inside
        Macintosh I, the Window Manager), and Ultimine sets portRect
        itself while its window's hidden, to measure the frame around
        it.  GUI thread only."""
        with self._sys.lock:
            w.visible = True
            w.update_pending = True
            w.calc_regions()
        if w.backing is not None:
            w.fit_backing()
            w.backing.clear()

    def HideWindow(self, w: WindowRecord) -> None:
        def gui():
            if not w.visible:
                return
            prev = self._p.front_window()
            w.visible = False
            self._d.sync(w)
            self._rehilite(prev)
        self._gui(gui)

    def ShowHide(self, w: WindowRecord, show: bool) -> None:
        """Show or hide w without changing which windows are hilited or
        their order, and without activate events (Inside Macintosh I-438):
        programs keep floating windows so."""
        def gui():
            if w.visible == show:
                return
            if show:
                self._shown(w)
            else:
                w.visible = False
            self._d.sync(w)
            self._d.restack()
            self._d.focus(self._p.front_window())
            if show:
                self._p.events.wake()
        self._gui(gui)

    def HiliteWindow(self, w: WindowRecord, hilite: bool) -> None:
        def gui():
            w.hilited = hilite
            self._d.sync(w)
        self._gui(gui)

    @contextlib.contextmanager
    def host_dialog(self):
        """While a dialog of macrt's own (Standard File's, the Printing
        Manager's) is up.  On a Mac it is the front window: the window it
        covers is deactivated, the dialog's ModalDialog takes that event,
        and when the dialog goes the application gets the window's
        activate event.  MacWrite 4.5 hides its scroll bar and dims its
        menus itself before SFPutFile, and shows them again then."""
        def up():
            w = self._p.front_window()
            if w is not None and w.hilited:
                w.hilited = False
                self._d.sync(w)
        self._gui(up)
        try:
            yield
        finally:
            if not self._d.bridge.closed:
                self._gui(self._rehilite, None)

    def _rehilite(self, prev: WindowRecord | None) -> None:
        """After the window list changed: deactivate the old front window,
        activate the new one, and restack.  GUI thread only."""
        p = self._p
        cur = p.front_window()
        if prev is not None and prev is not cur and prev.hilited:
            prev.hilited = False
            self._d.sync(prev)
            p.events.post_activate(prev, False)
        if cur is not None and self._d.is_front(p) and not cur.hilited:
            cur.hilited = True
            self._d.sync(cur)
            p.events.post_activate(cur, True)
        self._d.restack()
        self._d.focus(cur)

    def SetWTitle(self, w: WindowRecord, title: str) -> None:
        def gui():
            w.title = title
            w.title_width = self._title_width(title)
            self._d.sync(w)
        self._gui(gui)

    def _title_width(self, title: str) -> int:
        """titleWidth: the title's width in the system font and size, which
        the System's WDEF 0 centres it by (Inside Macintosh I, The Window
        Manager): HyperCard 2.0's card window."""
        return self.qd.text_width(None, title.translate(CHICAGO_SYMBOLS))

    def redraw_frames(self, w: WindowRecord | None) -> None:
        """Draw a window's frame again (with None, each of the
        application's): its colours have changed.  Without a display
        there's none to draw."""
        if self._d is None:
            return
        windows = [w] if w is not None else list(self._p.windows)
        self._d.bridge.post(self._sync_all, windows)

    def _sync_all(self, windows: list[WindowRecord]) -> None:
        for w in windows:
            self._d.sync(w)

    def GetWTitle(self, w: WindowRecord) -> str:
        return w.title

    def SetWRefCon(self, w: WindowRecord, ref_con: int) -> None:
        w.ref_con = ref_con

    def GetWRefCon(self, w: WindowRecord) -> int:
        return w.ref_con

    def _refit(self, w: WindowRecord, moved: bool = False) -> None:
        """The window's Backing once it has moved or changed size."""
        if w.fit_backing(moved):
            with self._sys.lock:
                w.update_pending = True
            self._p.events.wake()

    def MoveWindow(self, w: WindowRecord, h: int, v: int, front: bool = False) -> None:
        def gui():
            with self._sys.lock:
                w.content = w.content.moved_to(h, v)
            self._refit(w, moved=True)
            self._d.sync(w)
        self._gui(gui)
        if front:
            self.SelectWindow(w)

    def SizeWindow(self, w: WindowRecord, width: int, height: int, update: bool = True) -> None:
        if self._grown is w:
            self._grown = None

        def gui():
            with self._sys.lock:
                w.content = w.content.sized(width, height)
                if update:
                    w.update_pending = True
            self._refit(w)
            view = self._d.views.get(w)
            if view is not None:
                view.preview_size = None
            if update:
                self._p.events.wake()
            self._d.sync(w)
        self._gui(gui)

    def DragWindow(self, w: WindowRecord, start: Point, bounds: Rect | None = None) -> None:
        if not self._d.input.modifiers & k.cmdKey:
            self.SelectWindow(w)
        moved = self._track(self._d.track_drag, w, bounds, start)
        if moved is not None:
            def gui():
                view = self._d.views.get(w)
                if view is None:
                    return
                l, t = view.content_offset()
                with self._sys.lock:
                    w.content = w.content.moved_to(moved.h + l, moved.v + t)
                self._refit(w, moved=True)
                self._d.sync(w)
            self._gui(gui)

    def TrackGoAway(self, w: WindowRecord, pt: Point) -> bool:
        return self._track_box(w, k.wInGoAway)

    def TrackBox(self, w: WindowRecord, pt: Point, part: int) -> bool:
        return self._track_box(w, k.wInZoomIn if part == k.inZoomIn else k.wInZoomOut)

    def _track_box(self, w: WindowRecord, whit: int) -> bool:
        if w.wdef_shaped and self.wdefs is not None:
            return self.wdefs.track(w, whit)        # its WDEF draws the box
        return bool(self._track(self._d.track_box, w, whit))

    def ZoomWindow(self, w: WindowRecord, part: int, front: bool = False) -> None:
        def gui():
            if part == k.inZoomOut:
                w.user_state = w.content
                target = w.std_state
            else:
                target = w.user_state
            if target is None or target == w.content:
                return
            with self._sys.lock:
                w.content = target
                w.update_pending = True
            self._refit(w, moved=True)
            self._d.sync(w)
            self._p.events.wake()
        self._gui(gui)
        if front:
            self.SelectWindow(w)

    def GrowWindow(self, w: WindowRecord, pt: Point, size_rect: Rect) -> int:
        size = int(self._track(self._d.track_grow, w, size_rect, pt) or 0)
        if size:
            self._grown = w
        return size

    def BeginUpdate(self, w: WindowRecord) -> None:
        """Until EndUpdate, draw only what needs it: the visRgn is what of
        it is in the update region, which is emptied.  A program that
        validated the rest draws only that, however much it draws."""
        with self._sys.lock:
            vis = qdraw.read_rgn(self._mem, w.vis_rgn)
            w.saved_vis = (vis, w.port_rect)
            b = w.port_bits_bounds
            qdraw.write_rgn(self._sys.heap_of(w.vis_rgn), w.vis_rgn,
                            vis.sect(w.update.offset(b.left, b.top)))
            w.update_pending = False
            w.updates_begun += 1

    def EndUpdate(self, w: WindowRecord) -> None:
        """The visRgn as it was, moved with any SetOrigin since."""
        with self._sys.lock:
            if w.saved_vis is None:
                return
            (vis, then), now = w.saved_vis, w.port_rect
            w.saved_vis = None
            qdraw.write_rgn(self._sys.heap_of(w.vis_rgn), w.vis_rgn,
                            vis.offset(now.left - then.left, now.top - then.top))

    def InvalRect(self, r: Rect | None = None) -> None:
        """Add r (local to the current port; None for all of it) to the
        window's update region."""
        if r is None:
            port = self._port
            if isinstance(port, WindowRecord):
                with self._sys.lock:
                    port.update_pending = True
                self._p.events.wake()
            return
        self.InvalRgn(qdraw.Region.rect(r))

    def InvalRgn(self, rgn: qdraw.Region) -> None:
        self._update_rgn(rgn, True)

    def ValidRect(self, r: Rect) -> None:
        """Take r (local to the current port) out of the window's update
        region: it's drawn already."""
        self._update_rgn(qdraw.Region.rect(r), False)

    def ValidRgn(self, rgn: qdraw.Region) -> None:
        self._update_rgn(rgn, False)

    def _update_rgn(self, rgn: qdraw.Region, add: bool) -> None:
        port = self._port
        if not isinstance(port, WindowRecord):
            return
        with self._sys.lock:
            b = port.port_bits_bounds
            (port.invalidate if add else port.validate)(rgn.offset(-b.left, -b.top))
        if add:
            self._p.events.wake()

    # =====================================================================
    # Menu Manager
    # =====================================================================
    def NewMenu(self, menu_id: int, title: str) -> MenuInfo | None:
        """MDEF 0's menu: an application's own MDEF 0 if it has one."""
        menu = MenuInfo.new(self._sys, self._p.heap, menu_id, title,
                            self._p.resources.defproc("MDEF", 0))
        if menu is not None:
            self.size_menu(menu)
        return menu

    def _system_strike(self) -> Strike | None:
        """The application's own system font, Chicago 12, if it has one."""
        found = self.qd.app_fonts.find(0, 12)
        return found[0] if found is not None else None

    def size_menu(self, menu: MenuInfo) -> None:
        """The Menu Manager works out a menu's size after every routine that
        changes it (IM I-361, CalcMenuSize).  A menu of the System's MDEF 0
        is the size the host pulls it down at; an application's own MDEF
        works out its own menus' (CalcMenuSize's mSizeMsg)."""
        if not menu.own_mdef:
            from ..ui.menubar import menu_size     # which imports this module
            strike, key = self._system_strike(), menu.contents_key()
            found = self._menu_sizes.get(key)
            if found is None or found[0] is not strike:
                if len(self._menu_sizes) >= MENU_SIZES_KEPT:
                    self._menu_sizes.clear()
                found = self._menu_sizes[key] = (strike, menu_size(menu, strike))
            menu.size = found[1]

    def GetMenu(self, menu_id: int) -> MenuInfo | None:
        """A menu from a MENU resource.  The resource is the menu: its
        MDEF ID is replaced by a handle to the MDEF, and it's made
        unpurgeable so it stays in memory while it's in the menu bar."""
        rm = self._p.resources
        with self._sys.lock:
            h = rm.GetResource("MENU", menu_id)
            if not h:
                return None
            self._sys.heap_of(h).set_purgeable(h, False)
            menu = MenuInfo(self._sys, h)
            proc = self._mem.r32(menu.ptr + MENU_PROC)
            if rm.GetResInfo(proc)[1] != b"MDEF":         # not patched yet
                mdef_id = self._mem.r16s(menu.ptr + MENU_PROC)
                self._mem.w32(menu.ptr + MENU_PROC, rm.defproc("MDEF", mdef_id))
            self._add_mctb(menu_id)
            rm.error = 0
        self.size_menu(menu)
        return menu

    def _add_mctb(self, rid: int) -> None:
        """With Color QuickDraw, the Menu Manager adds the entries of the
        'mctb' with this ID to the menu colour table."""
        if self._sys.model.color:
            ct = self._p.resources.GetResource("mctb", rid)
            if ct:
                set_mct_entries(self._sys, self._p.world, self._p.heap,
                                mctb_entries(self.contents(ct)))

    def _clear_menu_colors(self) -> None:
        """With Color QuickDraw, ClearMenuBar empties the menu colour table."""
        h = menu_color_table(self._sys, self._p.world) if self._sys.model.color else 0
        if h:
            write_mct(self._sys, h, [])

    def GetNewMBar(self, mbar_id: int) -> int:
        """A menu list (not yet the menu bar) of the menus in an MBAR."""
        h = self._p.resources.GetResource("MBAR", mbar_id)
        if not h:
            return 0
        # It clears the menu colour table, as ClearMenuBar does, for its
        # menus' colours, and leaves it so (Inside Macintosh V).
        self._clear_menu_colors()
        menus = [self.GetMenu(i) for i in parse_mbar(self.contents(h))]
        with self._sys.lock:
            return new_menu_list(self._sys, self._p.heap, [m.handle for m in menus if m],
                                 self._p.menus.mbdf_id)

    def SetMenuBar(self, menu_list: int) -> None:
        self._gui(self._p.menus.set_from, menu_list)

    def GetMenuBar(self) -> int:
        with self._sys.lock:
            return self._p.menus.copy(self._p.heap)

    def DisposeMenu(self, menu: MenuInfo) -> None:
        """A menu that's a resource is released, so that GetMenu reads it
        afresh: the MacPaint demo deletes, disposes of and gets again its
        Goodies menu."""
        rm = self._p.resources
        err = rm.error
        rm.ReleaseResource(menu.handle)
        if rm.error == resNotFound:
            self._gui(menu.dispose)
        rm.error = err

    def AppendMenu(self, menu: MenuInfo, data: str) -> None:
        def gui():
            menu.append(parse_menu_items(data))
            self._d.refresh_bars()
        self._gui(gui)
        self.size_menu(menu)

    def InsertMenu(self, menu: MenuInfo, before_id: int = 0) -> None:
        # A Mac draws the menus in the system font, Chicago 12, and an
        # application's own comes first: The Fool's Errand's has its tarot
        # suits for its menus' titles.
        self._p.system_strike = self._system_strike()
        self._gui(self._p.menus.insert, menu, before_id)

    def DeleteMenu(self, menu_id: int) -> None:
        self._gui(self._p.menus.delete, menu_id)

    def InitMenus(self) -> None:
        """An empty menu bar.  With Color QuickDraw, a menu colour table
        too, made the first time, with the entries of the 'mctb' 0 (the
        application's, else the System's) added (Inside Macintosh V)."""
        self.ClearMenuBar()
        if self._sys.model.color and menu_color_table(self._sys, self._p.world, self._p.heap):
            self._add_mctb(0)
            self._p.resources.error = 0

    def ClearMenuBar(self) -> None:
        """The menu list emptied, and the menu colour table."""
        self._gui(self._p.menus.clear)
        self._clear_menu_colors()

    def GetMHandle(self, menu_id: int) -> MenuInfo | None:
        return self._p.menus.get(menu_id)

    def HMGetHelpMenuHandle(self) -> MenuInfo | None:
        """The application's own Help menu, made the first time it asks:
        the standard items, to append its own to.  It's at the right of the
        menu bar once it has some, and MenuSelect returns kHMHelpMenuID
        with one of them."""
        p = self._p
        if p.help_menu is None:
            menu = MenuInfo.new(self._sys, p.heap, k.kHMHelpMenuID, "")
            if menu is None:
                return None
            menu.append(parse_menu_items(HELP_ITEMS))
            p.help_menu = menu
            with self._sys.lock:
                p.menus.add_system(menu, first=True)
        return p.help_menu

    def DrawMenuBar(self) -> None:
        def gui():
            self._d.refresh_bars()
            return self._d.bar_image(self._p)
        self._sys.screen.bar_drawn(self._gui(gui))

    def menus_changed(self, menu: MenuInfo | None = None) -> None:
        """Show a menu's new items, and work out its new size.  Unlike
        DrawMenuBar, this leaves what a program has drawn over the bar."""
        if menu is not None:
            self.size_menu(menu)
        self._gui(self._d.refresh_bars)

    def MenuSelect(self, pt: Point, serve: Callable | None = None) -> int:
        """serve(what, ...) answers the tracker on this thread while it
        follows the mouse: an application's MBDF and MDEFs (mdefs.Menus).
        If it chooses nothing, MenuChoice gives the disabled item the mouse
        was released over, if it was."""
        self._p.menu_choice = 0
        return int(self._track(self._d.track_menu, self._p, pt, serve=serve) or 0)

    def PopUpMenuSelect(self, menu: MenuInfo, top: int, left: int, item: int,
                        serve: Callable | None = None) -> int:
        """A pop-up menu, its item `item` at (top, left) in global
        coordinates: the menu's ID and the item chosen, as MenuSelect
        returns them; 0 for none."""
        args = (self._d.track_popup, self._p, menu, Point(top, left), item)
        return int(self._track(*args, serve=serve) or 0)

    def MenuKey(self, ch: str | int) -> int:
        self._p.menu_choice = 0
        if isinstance(ch, int):
            ch = bytes([ch & 0xFF]).decode("mac_roman")

        def gui():
            bar = self._p.bar_menus()
            for menu in bar + self._p.menus.submenus():
                for i, item in enumerate(menu.items, start=1):
                    if item.key and item.key.lower() == ch.lower() and menu.selectable(i):
                        title = menu.id
                        if menu not in bar:     # a submenu: the menu it hangs from
                            title = next((m.id for m in bar if any(
                                it.submenu == menu.id for it in m.items)), 0)
                        self._d.hilite_menu(self._p, title)
                        return (menu.id & 0xFFFF) << 16 | i
            return 0
        return self._gui(gui)

    def HiliteMenu(self, menu_id: int) -> None:
        self._gui(self._d.hilite_menu, self._p, menu_id)

    def _change_item(self, menu: MenuInfo, item: int, **changes) -> None:
        """A change to an item that leaves it as it is doesn't wait for the
        GUI thread: The Secret Island of Dr. Quandary checks its menus'
        items and sets their text again on each pass of its event loop, over
        a hundred calls a second, and nearly all of them change nothing:
        waiting about 5 ms each, they took three quarters of its time."""
        if item:
            with self._sys.lock:
                changes_it = menu.changes_item(item, **changes)
            if not changes_it:
                self.size_menu(menu)
                return

        def gui():
            if item == 0 and "enabled" in changes:
                menu.enabled = changes["enabled"]
            elif not menu.change_item(item, **changes):
                return
            self._d.refresh_bars()
        self._gui(gui)
        if item:
            self.size_menu(menu)

    def _set_enabled(self, menu: MenuInfo, item: int, on: bool) -> None:
        """Enabling or disabling an item or a menu sets or clears its bit
        of enableFlags, nothing more, so the guest doesn't wait for the
        menu bar to show it: Netscape Navigator 2.02 enables and disables
        some sixty items for each key typed, and at a wait for the GUI
        thread each, lost keys typed at a typist's pace.  Only a menu's own
        bit is drawn in the bar, so only a change to it has the bar drawn
        again: The Secret Island of Dr. Quandary disables and enables items
        on each pass of its event loop, and the bar was drawn fifty times a
        second.  A menu that's pulled down reads its items' bits as it's
        drawn."""
        with self._sys.lock:
            if not 0 <= item <= min(menu.count(), LAST_FLAGGED_ITEM):
                return
            flags = menu.enable_flags
            menu.enable_flags = (flags | 1 << item) if on else (flags & ~(1 << item))
            if item or menu.enable_flags == flags:
                return
        with self._dirty_lock:
            due, self._bars_due = self._bars_due, True
        if not due:
            self._d.bridge.post(self._refresh_bars)

    def _refresh_bars(self) -> None:
        with self._dirty_lock:
            self._bars_due = False
        self._d.refresh_bars()

    def EnableItem(self, menu: MenuInfo, item: int) -> None:
        self._set_enabled(menu, item, True)

    def DisableItem(self, menu: MenuInfo, item: int) -> None:
        self._set_enabled(menu, item, False)

    def CheckItem(self, menu: MenuInfo, item: int, checked: bool) -> None:
        self._change_item(menu, item, mark=k.checkMark if checked else k.noMark)

    def SetItemMark(self, menu: MenuInfo, item: int, mark: str) -> None:
        self._change_item(menu, item, mark=mark)

    def SetItem(self, menu: MenuInfo, item: int, text: str) -> None:
        self._change_item(menu, item, text=text)

    def GetItem(self, menu: MenuInfo, item: int) -> str:
        it = menu.item(item)
        return it.text if it else ""

    def CountMItems(self, menu: MenuInfo) -> int:
        return menu.count()

    # =====================================================================
    # QuickDraw (see macrt.toolbox.qdraw).  The current port and its pen
    # are in guest memory.  Coordinates are local to the current port.
    # =====================================================================
    def SetPort(self, port: GrafPort | None) -> None:
        self._mem.w32(self._the_port, port.addr if port is not None else 0)

    def GetPort(self) -> GrafPort | None:
        return self._port

    def MovePortTo(self, left: int, top: int) -> None:
        """Where the port is on the screen: a window's goes with it, and
        what's on the screen stays where it was (as Multiplan has it, moving
        its formula bar's window and then sizing it)."""
        port = self._port
        if port is None:
            return
        b, r = port.port_bits_bounds, port.port_rect
        port.port_bits_bounds = b.offset(r.left - left - b.left, r.top - top - b.top)
        if isinstance(port, WindowRecord):
            def gui():
                self._refit(port)
                self._d.sync(port)
            self._gui(gui)

    def _repaint(self, port: GrafPort) -> None:
        """Have a window's view show what's been drawn, once however many
        calls drew in it since it last did."""
        if not isinstance(port, WindowRecord):
            return
        with self._dirty_lock:
            if port in self._dirty:
                return
            first = not self._dirty
            self._dirty.add(port)
        if first:
            self._d.bridge.post(self._show_dirty)

    def _show_dirty(self) -> None:
        with self._dirty_lock:
            dirty, self._dirty = self._dirty, set()
        for w in dirty:
            self._d.repaint_content(w)

    def MoveTo(self, h: int, v: int) -> None:
        port = self._port
        if port is not None:
            self.qd.move_to(port, h, v)

    def Move(self, dh: int, dv: int) -> None:
        port = self._port
        if port is not None:
            pen = port.pn_loc
            self.qd.move_to(port, pen.h + dh, pen.v + dv)

    def LineTo(self, h: int, v: int) -> None:
        port = self._port
        if port is not None:
            self.qd.line_to(port, h, v)

    def Line(self, dh: int, dv: int) -> None:
        port = self._port
        if port is not None:
            pen = port.pn_loc
            self.qd.line_to(port, pen.h + dh, pen.v + dv)

    def shape(self, verb: str, kind: str, r: Rect, args: tuple = (),
              pat: bytes | None = None) -> None:
        """verb is Frame, Paint, Erase, Invert or Fill; kind Rect, Oval,
        RoundRect (args: ovalWidth, ovalHeight) or Arc (startAngle,
        arcAngle)."""
        if kind == "Rect":
            path_of = qdraw.rect_path
        elif kind == "Oval":
            path_of = qdraw.oval_path
        elif kind == "RoundRect":       # partials, so a picture can tell what they draw
            path_of = partial(qdraw.round_rect_path, oval_w=args[0], oval_h=args[1])
        else:
            path_of = partial(qdraw.arc_path, start=args[0], arc=args[1])
        self.qd.shape(self._port, verb, path_of, r, pat)

    def FrameRect(self, r: Rect) -> None:
        self.shape("Frame", "Rect", r)

    def PaintRect(self, r: Rect) -> None:
        self.shape("Paint", "Rect", r)

    def EraseRect(self, r: Rect) -> None:
        self.shape("Erase", "Rect", r)

    def InvertRect(self, r: Rect) -> None:
        self.shape("Invert", "Rect", r)

    def FillRect(self, r: Rect, pat: bytes) -> None:
        self.shape("Fill", "Rect", r, pat=pat)

    def FrameOval(self, r: Rect) -> None:
        self.shape("Frame", "Oval", r)

    def PaintOval(self, r: Rect) -> None:
        self.shape("Paint", "Oval", r)

    def FrameRoundRect(self, r: Rect, oval_width: int, oval_height: int) -> None:
        self.shape("Frame", "RoundRect", r, (oval_width, oval_height))

    def ScrollRect(self, r: Rect, dh: int, dv: int) -> qdraw.Region:
        """Returns the area left behind, which ScrollRect's caller passes
        an updateRgn for."""
        return self.qd.scroll_rect(self._port, r, dh, dv)

    def DrawString(self, s: str) -> None:
        self.qd.draw_text(self._port, s.translate(CHICAGO_SYMBOLS))

    def StringWidth(self, s: str) -> int:
        return self.qd.text_width(self._port, s.translate(CHICAGO_SYMBOLS))

    def MeasureText(self, s: str) -> list[int]:
        """Where the pen is after each character of s, from 0 before it."""
        return self.qd.text_positions(self._port, s.translate(CHICAGO_SYMBOLS))

    def GetFontInfo(self) -> tuple[int, int, int, int]:
        """(ascent, descent, widMax, leading) of the port's font."""
        return self.qd.font_info(self._port)

    def TextBox(self, text: str, r: Rect, just: int = 0) -> None:
        self.qd.text_box(self._port, text.translate(CHICAGO_SYMBOLS), r, just)

    def DrawGrowIcon(self, w: WindowRecord) -> None:
        """Scroll bar outlines plus the size box; an empty box when inactive.
        They're drawn in the window's port, so its clipRgn clips them:
        Shadowgate clips to the size box's column to leave out the line
        above the bottom of its text."""
        if not w.definition.grow or w.backing is None:
            return
        # A 16-pixel scroll bar overlaps the window's frame by one, so its
        # edge, and the size box's, is 15 pixels in from the port's.
        r = w.port_rect
        g = theme.GROW_BOX
        x, y = r.right - g, r.bottom - g
        with self.qd.painting(w) as p:
            if p is None:
                return
            p.fillRect(x, r.top, 1, r.height, BLACK)
            p.fillRect(r.left, y, r.width, 1, BLACK)
            p.fillRect(x + 1, y + 1, g - 1, g - 1, WHITE)
            if w.hilited:
                for bx, by, bs in ((x + 3, y + 3, 6), (x + 5, y + 5, 8)):
                    p.fillRect(bx, by, bs, bs, WHITE)
                    p.fillRect(bx, by, bs, 1, BLACK)
                    p.fillRect(bx, by + bs - 1, bs, 1, BLACK)
                    p.fillRect(bx, by, 1, bs, BLACK)
                    p.fillRect(bx + bs - 1, by, 1, bs, BLACK)

    # =====================================================================
    # Toolbox Utilities
    # =====================================================================
    def GetString(self, string_id: int) -> int:
        """A handle to a 'STR ' resource."""
        return self._p.resources.GetResource("STR ", string_id)

    def GetIndString(self, list_id: int, index: int) -> str:
        """The index'th string (from 1) in a 'STR#', "" if there isn't one."""
        h = self._p.resources.GetResource("STR#", list_id)
        strings = parse_str_list(self.contents(h)) if h else []
        return strings[index - 1] if 1 <= index <= len(strings) else ""

    def string_of(self, h: int) -> str:
        """The Pascal string a handle holds, as from GetString."""
        return parse_str(self.contents(h)) if h else ""

    # =====================================================================
    # Process Manager / Segment Loader
    # =====================================================================
    def ExitToShell(self) -> None:
        raise ProcessKilled()


# Resource Manager calls go straight to the process's ResourceManager.
RESOURCE_CALLS = (
    "OpenResFile", "CreateResFile", "CloseResFile", "CurResFile", "UseResFile",
    "HomeResFile", "ResError", "SetResLoad", "UpdateResFile",
    "GetResFileAttrs", "SetResFileAttrs",
    "GetResource", "Get1Resource", "GetNamedResource", "Get1NamedResource",
    "CountResources", "Count1Resources", "GetIndResource", "Get1IndResource",
    "CountTypes", "Count1Types", "GetIndType", "Get1IndType", "UniqueID", "Unique1ID",
    "LoadResource", "ReleaseResource", "DetachResource",
    "GetResInfo", "SetResInfo", "GetResAttrs", "SetResAttrs", "SizeResource", "MaxSizeRsrc",
    "AddResource", "RemoveResource", "RmveResource", "ChangedResource", "WriteResource",
)


def _resource_call(name: str):
    def call(self: Toolbox, *args):
        return getattr(self._p.resources, name)(*args)
    call.__name__ = call.__qualname__ = name
    call.__doc__ = getattr(ResourceManager, name).__doc__
    return call


for _name in RESOURCE_CALLS:
    setattr(Toolbox, _name, _resource_call(_name))
