"""
Control panels: a 'cdev' file run as System 7's Finder runs one, in a
window of its own, with the panel's code called for whatever happens
there (Inside Macintosh: More Macintosh Toolbox, Control Panels).

A control panel's resources have IDs from -4064 to -4033.  With -4064:
its code, 'cdev'; its window's items, a 'DITL'; 'nrct', a count and that
many rectangles, the areas the window is made of, the first with its top
left at (-1, 87); and 'mach', two words, which as $0000 $FFFF say to ask
the code, with the macDev message, whether it can run on this Macintosh.
With -4049, a 'finf', a count and that many fonts, each a font number,
style and size, says what the window's text is in, the first of them;
without one it's the application font in 9 points ("Specifying the Font
of Text in a Control Panel").

The code starts at its first byte.  It's a Pascal function

    cdev(message, item, numItems, CPrivateValue: INTEGER;
         VAR theEvent: EventRecord; cdevStorageValue: LONGINT;
         CPDialog: DialogPtr): LONGINT

that returns what it wants as cdevStorageValue next time, usually a
handle to its own data, or an error: cdevGenErr closes the panel, and
cdevMemErr and cdevResErr close it saying there wasn't the memory or a
resource it needs.  The first message, initDev, comes with cdevUnset.
numItems is 0, and item an item's own number, as with System 7.
CPrivateValue is reserved, but Startup Disk makes its list of disks with
it as the list definition's ID, for cells of an icon handle, a font, a
face, a size and a name.  It's 19, macrt's LDEF that draws those (see
macrt.m68k.glue_lists).

macrt is the host, where on a Mac the Finder is.  The window is a dialog
of the 'DITL''s items, as big as the rectangles' bounds less the outline
round them, with its origin set so that the items fall where the panel
put them.  What the rectangles don't cover is grey, and each is outlined.
The window, and the dialog's items but the user items, are drawn before
initDev.  Key events go to the panel first, with keyEvtDev, then, unless
it made the event a null one, to DialogSelect: the panel deals with
Return and Enter.  A Command key's Edit menu item, like a pick from the
menu, is undoDev, cutDev, copyDev, pasteDev or clearDev.  An update's
updateDev comes once the items are drawn; activDev and deActivDev with
activate events, nulDev with null ones.
"""

from __future__ import annotations

import logging
import struct
from collections import deque

from ..toolbox import constants as k
from ..toolbox import lowmem
from ..toolbox.quickdraw import QD_GLOBALS_SIZE, QD_GRAY
from ..toolbox.system import APP_PARAMS
from ..toolbox.types import Rect
from . import dispatch, glue_desk, glue_dialogs, glue_os
from .runtime import SystemError68k
from .segload import ENTRY, Layout

log = logging.getLogger(__name__)

CDEV_ID = -4064
FONT_ID = -4049             # the 'finf' of the window's font
APPL_FONT = (1, 0, 9)       # without one: the application font, 9 points

(initDev, hitDev, closeDev, nulDev, updateDev, activDev, deActivDev, keyEvtDev, macDev,
 undoDev, cutDev, copyDev, pasteDev, clearDev) = range(14)
cdevUnset = 3
cdevGenErr, cdevMemErr, cdevResErr = -1, 0, 1
ICON_LIST = 19                  # CPrivateValue: the LDEF for a cell of an icon and a name

# A control panel has no CODE 0: its A5 world holds the application
# parameters and QuickDraw's globals, which the host's InitGraf fills,
# and a jump table of one entry that's never used.
LAYOUT = Layout(APP_PARAMS + ENTRY, QD_GLOBALS_SIZE, APP_PARAMS, bytes(ENTRY))

APPLE, FILE, EDIT = 128, 129, 130
CLOSE, QUIT = 1, 3
EDITS = {1: undoDev, 3: cutDev, 4: copyDev, 5: pasteDev, 6: clearDev}
SLEEP = 10                  # ticks between null events, for TextEdit's caret and nulDev
EVENT, WINDOW, ITEM, RECT, TEXT = 0, 16, 20, 24, 32      # the host's own block
BLOCK = TEXT + 256


def run(t) -> None:
    """The process's main loop: open the panel, then hand it events till
    it's closed."""
    host = Host(t)
    try:
        host.run()
    except SystemError68k as e:
        if not host.reopen(e):
            raise


class Host:
    def __init__(self, t):
        self.t = t
        self.mem = t.mem
        self.block = t.process.heap.new_ptr(BLOCK, clear=True)
        if not self.block:
            t.system_error(25, "out of memory")         # dsMemFullErr
        self.event = self.block + EVENT
        self.code = 0
        self.dialog = 0
        self.storage = cdevUnset
        self.rects: list[Rect] = []
        self.content = Rect(0, 0, 0, 0)
        # With its 'INIT' resources run: the CPU and the System's Gestalt
        # functions as they were before them, where the code they left is,
        # and whether it has opened since.
        self.before_startup: tuple | None = None
        self.extension: list[tuple[int, int]] = []
        self.opened = False
        self.quit = False

    def trap(self, name: str, *args):
        """Call a trap as the panel's code would."""
        return dispatch.IMPLEMENTATIONS[name](self.t, *args)

    def run(self) -> None:
        self._startup()
        self.trap("InitGraf", self.t.process.partition.a5 - 4)
        for name in ("InitFonts", "InitWindows", "InitMenus", "TEInit"):
            self.trap(name)
        self.trap("InitDialogs", 0)
        self.trap("InitCursor")
        self._menus()
        if self._open():
            while self.dialog:
                self._event()
                self.opened = True
        self._go_on()

    def reopen(self, e) -> bool:
        """A panel that stops with its 'INIT' resources run, as it opens or
        in the code they left, is opened again without them, as on a Mac
        started with its extension off; an extension of the System's own
        may need what only that System has.  What they added to the
        System's Gestalt goes, and so do their patches.  False if it ran
        without them, or stopped in code of its own."""
        if self.before_startup is None:
            return False
        if self.opened and not any(lo <= (e.pc or 0) < hi for lo, hi in self.extension):
            return False
        t = self.t
        app = t.process.app
        ctx, gestalt, patches = self.before_startup
        log.warning("%s stopped, its INIT run (%s); opening it again without",
                    t.process.name, e)
        t.cpu.restore(ctx)
        t.system.gestalt_procs.clear()
        t.system.gestalt_procs.update(gestalt)
        t.system.init_patches = patches
        t.system.extensions_off.add(app.key or app.name)
        launch = getattr(t.tb._d, "launch_application", None) if t.tb is not None else None
        return launch is not None and not isinstance(launch(app), str)

    # =====================================================================
    # Opening and closing
    # =====================================================================
    def _menus(self) -> None:
        def menu(menu_id: int, title: bytes, items: str = "") -> int:
            h = self.trap("NewMenu", menu_id, self._pstr(title))
            if items:
                self.trap("AppendMenu", h, self._pstr(items))
            self.trap("InsertMenu", h, 0)
            return h
        self.apple = menu(APPLE, b"\x14")
        self.trap("AppendResMenu", self.apple, int.from_bytes(b"DRVR", "big"))
        menu(FILE, b"File", "Close/W;(-;Quit/Q")
        menu(EDIT, b"Edit", "Undo/Z;(-;Cut/X;Copy/C;Paste/V;Clear")
        self.trap("DrawMenuBar")

    def _open(self) -> bool:
        t = self.t
        rm = t.process.resources
        name = t.process.name
        code = rm.Get1Resource("cdev", CDEV_ID)
        self.rects = self._rectangles()
        items = glue_dialogs._copy_resource(t, "DITL", CDEV_ID)
        if not code or not self.rects or not items:
            self._tell(f"The control panel “{name}” can't be opened, because "
                       "some of its resources are missing.")
            return False
        heap = t.system.heap_of(code)
        heap.lock(code)
        heap.set_purgeable(code, False)
        self.code = code
        top = min(r.top for r in self.rects)
        left = min(r.left for r in self.rects)
        bottom = max(r.bottom for r in self.rects)
        right = max(r.right for r in self.rects)
        self.content = Rect(top + 1, left + 1, bottom - 1, right - 1)
        width, height = right - left - 2, bottom - top - 2
        screen = t.system.screen_bits.bounds
        bar = t.mem.r16s(lowmem.MBarHeight)
        h = max(screen.left + 4, (screen.left + screen.right - width) // 2)
        v = max(screen.top + bar + 22, min(screen.top + bar + 40, screen.bottom - height - 4))
        self.mem.w_rect(self.block + RECT, Rect(v, h, v + height, h + width))
        w = self.trap("NewColorDialog", 0, self.block + RECT, self._pstr(name), False,
                      k.noGrowDocProc, 0xFFFFFFFF, True, 0, items)
        if not w:
            self._tell(f"There isn't enough memory to open “{name}”.")
            return False
        self.dialog = w.addr
        glue_dialogs.set_font(t, w, *(self._font() or APPL_FONT))
        self.trap("SetPort", self.dialog)
        self.trap("SetOrigin", self.content.left, self.content.top)
        if not self._runs_here():
            self.trap("DisposeDialog", self.dialog)
            self.dialog = 0
            self._tell(f"The control panel “{name}” can't be used with this "
                       "Macintosh.")
            return False
        self.trap("ShowWindow", self.dialog)
        self._backdrop()
        self.trap("DrawDialog", self.dialog)
        return self._message(initDev)

    def _startup(self) -> None:
        """What INIT 31 does with the panel's file as the Mac starts up
        (IM IV, The System Resource File, "Initialization Resources"):
        call each of its 'INIT' resources, its handle in A0, the registers
        kept.  An INIT that draws sets up QuickDraw globals in an A5 world
        of its own, gone when it returns, so the panel's is kept; and the
        icon it may show at the bottom of the screen goes as the desk is
        painted again.  One that stops with a system error, needing what
        isn't here, is left as if it hadn't been loaded: the traps it
        patched are put back, what it added to Gestalt goes, and the panel
        opens without it."""
        t = self.t
        app = t.process.app
        rm = t.process.resources
        a5 = t.process.partition.a5
        count = rm.Count1Resources("INIT")
        if not count or (app.key or app.name) in t.system.extensions_off:
            return
        self.before_startup = (t.cpu.context(), dict(t.system.gestalt_procs),
                               dict(t.system.init_patches))
        heaps = (t.system.sys_heap, t.process.heap)
        before = [heap.blocks() for heap in heaps]
        for i in range(1, count + 1):
            h = rm.Get1IndResource("INIT", i)
            if not h:
                continue
            heap = t.system.heap_of(h)
            heap.lock(h)
            self.extension.append((heap.deref(h), heap.deref(h) + heap.handle_size(h)))
            ctx, patches, current_a5 = t.cpu.context(), dict(t.patches), t.mem.r32(lowmem.CurrentA5)
            gestalt = dict(t.system.gestalt_procs)
            requests = set(t.process.notifications), list(t.process.responses_due)
            try:
                t.call_for(heap.deref(h), (), a0=h)
            except SystemError68k as e:
                log.warning("%s: its INIT %d stopped (%s); going on without it",
                            t.process.name, i, e)
                t.cpu.restore(ctx)
                t.patches.clear()
                t.patches.update(patches)
                t.system.gestalt_procs.clear()
                t.system.gestalt_procs.update(gestalt)
                t.mem.w32(lowmem.CurrentA5, current_a5)
                t.process.notifications = requests[0]
                t.process.responses_due = deque(requests[1])
            t.process.partition.a5, t.process.partition.left_a5 = a5, 0
        # What they allocated, their patches perhaps.  The traps they patched
        # stay patched for the applications that run after, but for patches
        # in the panel's own partition, which goes when it closes.
        self.extension += [(p, p + n) for heap, was in zip(heaps, before)
                           for p, n in heap.blocks().items() if p not in was]
        part = t.process.partition
        t.system.init_patches.update((key, at) for key, at in t.patches.items()
                                     if not part.start <= at & t.mem.system.addr_mask < part.end)
        # The Notification Manager's requests made as the Mac starts up are
        # answered as its event mechanism begins.
        glue_os.respond(t)
        self._finder_starts()
        self.trap("PaintOne", 0, t.mem.r32(lowmem.GrayRgn))

    def _finder_starts(self) -> None:
        """The Mac goes on from its INITs to the Finder, and the Finder's
        event calls give their time to the INITs that patched SystemTask:
        After Dark 3.0's is ready for its panel only once it has seen the
        Finder current there, its name in CurApName."""
        mem = self.mem
        name = mem.read(lowmem.CurApName, 32)
        mem.write(lowmem.CurApName, mem.read(lowmem.FinderName, 16).ljust(32, b"\0"))
        try:
            glue_desk.give_time(self.t)
        finally:
            mem.write(lowmem.CurApName, name)

    def _rectangles(self) -> list[Rect]:
        h = self.t.process.resources.Get1Resource("nrct", CDEV_ID)
        data = self.t.tb.contents(h) if h else b""
        if len(data) < 2:
            return []
        n = min(struct.unpack_from(">h", data)[0], (len(data) - 2) // 8)
        return [Rect(*struct.unpack_from(">4h", data, 2 + 8 * i)) for i in range(max(n, 0))]

    def _font(self) -> tuple[int, int, int] | None:
        """The first font in the 'finf': its number, style and size."""
        h = self.t.process.resources.Get1Resource("finf", FONT_ID)
        data = self.t.tb.contents(h) if h else b""
        if len(data) < 8 or struct.unpack_from(">h", data)[0] < 1:
            return None
        font, face, size = struct.unpack_from(">hHh", data, 2)
        return font, face & 0x7F, size

    def _runs_here(self) -> bool:
        """What the 'mach' resource says, or with $0000 $FFFF what the
        panel says to macDev.  Other masks aren't checked."""
        h = self.t.process.resources.Get1Resource("mach", CDEV_ID)
        data = self.t.tb.contents(h) if h else b""
        if len(data) < 4 or struct.unpack_from(">HH", data) != (0, 0xFFFF):
            return True
        return self._send(macDev) != 0

    def _close(self, error: int | None = None) -> None:
        """closeDev, if it's the user's doing, and the window goes; an
        error the panel returned is said first."""
        name = self.t.process.name
        if error is None:
            self._send(closeDev)
        elif error == cdevMemErr:
            self._tell(f"There isn't enough memory for “{name}”.")
        elif error == cdevResErr:
            self._tell(f"“{name}” can't go on, because a resource it needs is "
                       "missing.")
        self.trap("DisposeDialog", self.dialog)
        self.dialog = 0

    def _tell(self, message: str) -> None:
        notify = getattr(self.t.tb._d, "notify", None) if self.t.tb is not None else None
        if notify is not None:
            notify(self.t.process.name, message)
        print(f"macrt: {message}")

    # =====================================================================
    # The panel's code
    # =====================================================================
    def _send(self, message: int, item: int = 0) -> int:
        t = self.t
        self.trap("SetPort", self.dialog)
        entry = t.system.heap_of(self.code).deref(self.code)
        result = t.call(entry, (("h", message), ("h", item), ("h", 0), ("h", ICON_LIST),
                                ("p", self.event), ("l", self.storage), ("p", self.dialog)),
                        "l") & 0xFFFFFFFF
        return result - (1 << 32) if result & 0x80000000 else result

    def _message(self, message: int, item: int = 0) -> bool:
        """Send the panel a message: False if it has closed."""
        result = self._send(message, item)
        if result in (cdevGenErr, cdevMemErr, cdevResErr):
            self._close(result)
            return False
        self.storage = result
        return True

    # =====================================================================
    # Events
    # =====================================================================
    def _event(self) -> None:
        ev = self.event
        self.trap("WaitNextEvent", k.everyEvent, ev, SLEEP, 0)
        what = self.mem.r16(ev)
        message = self.mem.r32(ev + 2)
        modifiers = self.mem.r16(ev + 14)
        if what == k.nullEvent:
            if self._front():
                self._select()
            if self.dialog:
                self._message(nulDev)
        elif what == k.mouseDown:
            self._mouse_down(self.mem.r_point(ev + 10))
        elif what in (k.keyDown, k.autoKey):
            if what == k.keyDown and modifiers & k.cmdKey:
                picked = self.trap("MenuKey", message & 0xFF)
                if picked >> 16:
                    self._menu(picked)
                    return
            if self._message(keyEvtDev) and self.mem.r16(ev) != k.nullEvent:
                self._select()
        elif what == k.updateEvt and message == self.dialog:
            self.trap("BeginUpdate", self.dialog)
            self._backdrop()
            self.trap("DrawDialog", self.dialog)
            self._message(updateDev)
            if self.dialog:
                self.trap("EndUpdate", self.dialog)
        elif what == k.activateEvt and message == self.dialog:
            self._select()
            self._message(activDev if modifiers & k.activeFlag else deActivDev)
        elif what == k.osEvt and message >> 24 == k.suspendResumeMessage:
            self._message(activDev if message & k.resumeFlag else deActivDev)

    def _go_on(self) -> None:
        """With the panel closed, the event loop goes on, as the Finder's
        would, while what it left wants it: a Notification Manager request
        whose response is due, or a window.  After Dark 3.0's panel closes
        itself as it opens, and its INIT, given the time, launches the
        panel's file as the application it is as well."""
        t, ev = self.t, self.event
        while not self.quit and (t.process.responses_due or self.mem.r32(lowmem.WindowList)):
            self.trap("WaitNextEvent", k.everyEvent, ev, SLEEP, 0)
            what = self.mem.r16(ev)
            if what == k.mouseDown:
                self._mouse_down(self.mem.r_point(ev + 10))
            elif what == k.keyDown and self.mem.r16(ev + 14) & k.cmdKey:
                picked = self.trap("MenuKey", self.mem.r32(ev + 2) & 0xFF)
                if picked >> 16:
                    self._menu(picked)

    def _mouse_down(self, where) -> None:
        part = self.trap("FindWindow", where, self.block + WINDOW)
        w = self.mem.r32(self.block + WINDOW)
        if part == k.inMenuBar:
            self._menu(self.trap("MenuSelect", where))
        elif part == k.inSysWindow:
            self.trap("SystemClick", self.event, w)
        elif w != self.dialog:
            return
        elif part == k.inDrag:
            self.trap("DragWindow", w, where, 0)
        elif part == k.inGoAway:
            if self.trap("TrackGoAway", w, where):
                self._close()
        elif part == k.inContent:
            if not self._front():
                self.trap("SelectWindow", w)
            else:
                self._select()

    def _select(self) -> None:
        """DialogSelect, and an item it hit is the panel's hitDev."""
        hit = self.trap("DialogSelect", self.event, self.block + WINDOW, self.block + ITEM)
        if hit and self.mem.r32(self.block + WINDOW) == self.dialog:
            self._message(hitDev, self.mem.r16s(self.block + ITEM))

    def _menu(self, picked: int) -> None:
        menu_id, item = picked >> 16, picked & 0xFFFF
        if menu_id == APPLE:
            self.trap("GetMenuItemText", self.apple, item, self.block + TEXT)
            self.trap("OpenDeskAcc", self.block + TEXT)
        elif menu_id == FILE and item in (CLOSE, QUIT):
            if self.dialog:
                self._close()
            self.quit = item == QUIT
        elif menu_id == EDIT and not self.trap("SystemEdit", item - 1):
            if item in EDITS and self._front():
                self._message(EDITS[item])
        self.trap("HiliteMenu", 0)

    def _front(self) -> bool:
        w = self.trap("FrontWindow")
        return bool(self.dialog) and w is not None and w.addr == self.dialog

    # =====================================================================
    # Drawing
    # =====================================================================
    def _backdrop(self) -> None:
        """Grey where the rectangles aren't, and their outlines."""
        r = self.block + RECT
        rest, part = self.trap("NewRgn"), self.trap("NewRgn")
        self.mem.w_rect(r, self.content)
        self.trap("RectRgn", rest, r)
        for rect in self.rects:
            self.mem.w_rect(r, rect)
            self.trap("RectRgn", part, r)
            self.trap("DiffRgn", rest, part, rest)
        self.trap("FillRgn", rest, self.t.process.partition.a5 - 4 + QD_GRAY)
        self.trap("PenNormal")
        for rect in self.rects:
            self.mem.w_rect(r, rect)
            self.trap("FrameRect", r)
        self.trap("DisposeRgn", rest)
        self.trap("DisposeRgn", part)

    def _pstr(self, s: str | bytes) -> int:
        self.t.set_str255(self.block + TEXT, s)
        return self.block + TEXT
