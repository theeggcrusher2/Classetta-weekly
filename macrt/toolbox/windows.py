"""
Window Manager data: the WindowRecord, the window list, and how a procID
picks a frame.

A WindowRecord is a 156-byte record in guest memory that starts with the
window's GrafPort, laid out as in Inside Macintosh I-275.  The window's
place on the screen is in the port: `content` (the content area in global
coordinates) is portRect seen through portBits.bounds, so moving a window
changes portBits.bounds and sizing it changes portRect.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Iterator

from ..memory import BOOLEAN, INTEGER, LONG, LONGINT, Field, from_mac, to_mac
from .constants import userKind
from .lowmem import AuxWinHead, World, WindowList as WINDOW_LIST
from .memmgr import Heap
from .qdraw import Region, read_rgn, write_rgn
from .quickdraw import Backing, GrafPort, new_rgn, rgn_bbox, set_rect_rgn
from .types import Rect

if TYPE_CHECKING:
    from ..memory import RAM
    from ..process import Process
    from .system import System


class WindowKind(Enum):
    DOCUMENT = auto()       # documentProc and friends: title bar, stripes
    DBOX = auto()           # dBoxProc: modal dialog, thick double frame
    PLAIN = auto()          # plainDBox: 1-pixel frame
    ALT_DBOX = auto()       # altDBoxProc: frame plus drop shadow
    MOVABLE_DBOX = auto()   # movableDBoxProc (System 7): title bar, no boxes
    ROUNDED = auto()        # rDocProc: desk accessory, black title bar


# rDocProc variation -> corner diameter (Inside Macintosh I-273)
ROUNDED_DIAMETERS = {0: 16, 1: 4, 2: 6, 3: 8, 4: 10, 5: 12, 6: 20, 7: 24}


@dataclass(frozen=True)
class Definition:
    kind: WindowKind
    grow: bool = False
    zoom: bool = False
    corner: int = 0


def describe_proc(proc_id: int) -> Definition:
    wdef, var = proc_id >> 4, proc_id & 0xF
    if wdef == 1:
        return Definition(WindowKind.ROUNDED, corner=ROUNDED_DIAMETERS.get(var & 7, 16))
    zoom = bool(var & 8)
    base = var & 7
    if base == 0:
        return Definition(WindowKind.DOCUMENT, grow=True, zoom=zoom)
    if base == 4:
        return Definition(WindowKind.DOCUMENT, zoom=zoom)
    return Definition({
        1: WindowKind.DBOX,
        2: WindowKind.PLAIN,
        3: WindowKind.ALT_DBOX,
        5: WindowKind.MOVABLE_DBOX,
    }.get(base, WindowKind.DOCUMENT))


# Auxiliary window records (Inside Macintosh V-200): a list of handles
# from AuxWinHead, each to awNext, awOwner, awCTable, dialogCItem, awFlags,
# awReserved and awRefCon.  The last is the default, which has no owner.
# Auxiliary control records, from AuxCtlHead, are acNext, acOwner,
# acCTable, acFlags, acReserved and acRefCon, a list the same way.
AUX_WIN_SIZE = 28
AUX_CTL_SIZE = 22


def aux_wins(mem: RAM, head: int = AuxWinHead) -> Iterator[int]:
    """The handles in the auxiliary window (or control) list, in order."""
    return _aux_list(mem, mem.r32(head))


def _aux_list(mem: RAM, h: int) -> Iterator[int]:
    h, seen = h & mem.system.addr_mask, set()
    while h and h not in seen:
        seen.add(h)
        p = mem.r32(h) & mem.system.addr_mask
        if not p:
            return
        yield h
        h = mem.r32(p) & mem.system.addr_mask


def find_aux_win(mem: RAM, window: int, head: int = AuxWinHead) -> int:
    """A window's auxiliary record (with window 0, the default's); 0 if
    there's none."""
    for h in aux_wins(mem, head):
        if (mem.r32((mem.r32(h) & mem.system.addr_mask) + 4) ^ window) & mem.system.addr_mask == 0:
            return h
    return 0


def table_colors(mem: RAM, ct: int) -> dict[int, tuple[int, int, int]]:
    """A window or control colour table's colours, by part: the first
    entry of each part's.  In order, so the first is the table's first
    colour, the one a part the table hasn't is drawn in (Inside
    Macintosh V, the Window Manager)."""
    ct &= mem.system.addr_mask
    p = mem.r32(ct) & mem.system.addr_mask if ct else 0
    if not p:
        return {}
    n = mem.r16(p + 6) + 1 & 0xFFFF
    out: dict[int, tuple[int, int, int]] = {}
    for i in range(min(n, 64)):
        e = p + 8 + 8 * i
        out.setdefault(mem.r16(e), (mem.r16(e + 2), mem.r16(e + 4), mem.r16(e + 6)))
    return out


def window_colors(mem: RAM, world: World, window: int) -> dict[int, tuple[int, int, int]]:
    """The colours a window's frame and content are drawn in, by part: its
    auxiliary record's table's or, if it has none, the default record's,
    from the list its application has (world's AuxWinHead, as it is while
    another application runs).  Empty if the list has neither."""
    found = 0
    for h in _aux_list(mem, world.r32(AuxWinHead)):
        owner = mem.r32((mem.r32(h) & mem.system.addr_mask) + 4) & mem.system.addr_mask
        if owner == window & mem.system.addr_mask:
            found = h
            break
        if not owner and not found:
            found = h                   # the default, at the end
    if not found:
        return {}
    return table_colors(mem, mem.r32((mem.r32(found) & mem.system.addr_mask) + 8))


def unlink_aux_win(mem: RAM, h: int, head: int = AuxWinHead) -> None:
    """Take a record out of the list."""
    prev = 0
    for x in aux_wins(mem, head):
        if x == h & mem.system.addr_mask:
            after = mem.r32(mem.r32(x) & mem.system.addr_mask)
            mem.w32((mem.r32(prev) & mem.system.addr_mask) if prev else head, after)
            return
        prev = x


class WindowRecord(GrafPort):
    SIZE = 156

    window_kind = Field(108, INTEGER)
    visible = Field(110, BOOLEAN)
    hilited = Field(111, BOOLEAN)
    go_away = Field(112, BOOLEAN)               # goAwayFlag
    spare_flag = Field(113, BOOLEAN)            # has a zoom box
    struc_rgn = Field(114, LONG)
    cont_rgn = Field(118, LONG)
    update_rgn = Field(122, LONG)
    window_def_proc = Field(126, LONG)          # WDEF handle; variation in the high byte
    data_handle = Field(130, LONG)              # WStateData for zoomable windows
    title_handle = Field(134, LONG)
    title_width = Field(138, INTEGER)
    control_list = Field(140, LONG)
    next_window = Field(144, LONG)
    window_pic = Field(148, LONG)
    ref_con = Field(152, LONGINT)

    def __init__(self, system: System, process: Process, heap: Heap, addr: int):
        super().__init__(system.mem, addr)
        self.system = system
        self.process = process
        self.heap = heap                # where its regions and handles live
        self.owns_storage = False
        self.dialog = None              # the Dialog Manager's DialogState, for a dialog
        self._proc_id: tuple[int, int, bool] | None = None  # (windowDefProc, procID, own)
        self.saved_vis: tuple[Region, Rect] | None = None   # BeginUpdate's: the visRgn, portRect
        self.updates_begun = 0          # its BeginUpdates so far
        # A window with a WDEF of the application's own (see own_wdef) has
        # the regions its wCalcRgns worked out once it has run, and the
        # frame its wDraw drew: (left, top, image), its top left relative
        # to the content area's.
        self.wdef_shaped = False
        self.frame_image: tuple | None = None
        self.wdef_pressed = 0           # the box the System's WDEF has highlighted for it

    @classmethod
    def new(cls, system: System, process: Process, heap: Heap, storage: int, bounds: Rect,
            title: str, visible: bool, proc_id: int, go_away: bool,
            ref_con: int, color: bool = False) -> WindowRecord | None:
        """NewWindow's work, or NewCWindow's with color, a window that's a
        cGrafPort: fill in the record at `storage`, or in a new
        nonrelocatable block when that's 0."""
        addr = storage or heap.new_ptr(cls.SIZE)
        if not addr:
            return None
        rec = cls(system, process, heap, addr)
        rec.owns_storage = not storage
        system.mem.fill(addr, cls.SIZE)
        screen = system.screen_of(process)
        if not color:
            rec.open(heap, screen)
        elif not rec.open_color(heap, screen):
            if rec.owns_storage:
                heap.dispose_ptr(addr)
            return None
        rec.window_kind = userKind
        rec.visible = visible
        rec.go_away = go_away
        wdef = process.resources.defproc("WDEF", proc_id >> 4)
        rec.window_def_proc = ((proc_id & 0xF) << 24) | wdef
        rec.struc_rgn = new_rgn(heap)
        rec.cont_rgn = new_rgn(heap)
        rec.update_rgn = new_rgn(heap)
        rec.title_handle = heap.new_handle(0)
        rec.title = title
        if rec.definition.zoom:
            rec.spare_flag = True
            rec.data_handle = heap.new_handle(16, clear=True)
        rec.ref_con = ref_con
        rec.port_rect = Rect(0, 0, bounds.height, bounds.width)
        rec.content = bounds
        rec.update_pending = True
        rec.backing = Backing(rec.kept(bounds), Rect(0, 0, bounds.height, bounds.width))
        rec.backing_at = bounds.top_left    # where on the screen it keeps them for
        system.register(rec)
        return rec

    def close(self) -> None:
        """CloseWindow's work: release everything but the record itself,
        and the window's auxiliary record, if it has one."""
        self.system.unregister(self)
        for h in (self.struc_rgn, self.cont_rgn, self.update_rgn, self.title_handle,
                  self.data_handle):
            if h:
                self.heap.dispose_handle(h)
        aux = find_aux_win(self.mem, self.addr)
        if aux:
            unlink_aux_win(self.mem, aux)
            heap = self.system.find_heap(aux)
            if heap is not None:
                heap.dispose_handle(aux)
        super().close(self.heap)

    def dispose(self) -> None:
        """DisposeWindow's: CloseWindow, then free the record if NewWindow
        allocated it."""
        self.close()
        if self.owns_storage:
            self.heap.dispose_ptr(self.addr)

    # -- where it is ---------------------------------------------------------
    @property
    def content(self) -> Rect:
        """The content area in global coordinates."""
        r, b = self.port_rect, self.port_bits_bounds
        return r.offset(-b.left, -b.top)

    @content.setter
    def content(self, r: Rect) -> None:
        old = self.content
        pr = self.port_rect
        self.port_rect = Rect(pr.top, pr.left, pr.top + r.height, pr.left + r.width)
        screen = self.system.screen_bits.bounds
        top, left = pr.top - r.top, pr.left - r.left
        self.port_bits_bounds = Rect(top, left, top + screen.height, left + screen.width)
        heap = self.heap
        if self.wdef_shaped:
            # Its WDEF's regions go with it until the WDEF works them out again.
            dh, dv = r.left - old.left, r.top - old.top
            for h in (self.struc_rgn, self.cont_rgn):
                write_rgn(heap, h, read_rgn(self.mem, h).offset(dh, dv))
        else:
            set_rect_rgn(heap, self.cont_rgn, r)
        self.calc_vis()
        self.saved_vis = None
        if self.update_pending:
            self.update = self.update.offset(r.left - old.left, r.top - old.top)

    def calc_regions(self) -> None:
        """The content region and visRgn worked out again from the port's
        rectangle, which a program may have set itself.  A window with its
        own WDEF has that work out its regions."""
        if not self.wdef_shaped:
            set_rect_rgn(self.heap, self.cont_rgn, self.content)
        self.calc_vis()
        self.saved_vis = None

    def calc_vis(self) -> None:
        """The visRgn, as CalcVis works it out: the port's rectangle,
        without what of the menu bar isn't desk, and without what's too far
        from the screen to be seen, which the Backing doesn't keep (the
        visRgn is what of the port "is actually visible on the screen",
        Inside Macintosh I, QuickDraw).  A program that hides the bar puts
        it in GrayRgn and has the visRgns worked out again.  HyperCard 2.0
        makes its card window at (16384, 16384), and on a Plus copies the
        card to the screen itself, clipped to the visRgn, before it moves
        the window onto the screen."""
        b = self.port_bits_bounds
        rect = self.port_rect.sect(self._seen().offset(b.left, b.top))
        hidden = self.system.screen.bar_hidden().offset(b.left, b.top).sect(Region.rect(rect))
        if hidden.empty:
            set_rect_rgn(self.heap, self.vis_rgn, rect)
        else:
            write_rgn(self.heap, self.vis_rgn, Region.rect(rect).diff(hidden))

    def _seen(self) -> Rect:
        """Where a window can be seen (global): within a screen's width
        and height of the screen.  The rest can't be seen without coming
        nearer first."""
        s = self.system.screen_bits.bounds
        return s.inset(-s.width, -s.height)

    def kept(self, r: Rect) -> Rect:
        """The part of the content area r whose pixels the window's Backing
        keeps, relative to r's top left: what can be seen, so a window far
        bigger than the screen (Excel 3 makes one 30000 pixels wide, nearly
        all of it off the screen) costs no more than one three screens
        wide."""
        seen = r.sect(self._seen())
        return Rect(0, 0, 0, 0) if seen.empty else seen.offset(-r.left, -r.top)

    @property
    def backing_rect(self) -> Rect:
        """The content area where the Window Manager last placed it
        (global): what of the screen its Backing keeps pixels for, whatever
        the program has done to its portRect since.  Ricochet sets the
        portRect to the half of its window it is about to copy to, without
        SetOrigin, and QuickDraw goes on drawing where portBits.bounds puts
        the pixels."""
        at, port = self.backing_at, self.backing.port
        return Rect(at.v, at.h, at.v + port.height, at.h + port.width)

    def fit_backing(self, moved: bool = False) -> bool:
        """After the window changes size, or moves, its pixels with it (or
        not: MovePortTo moves the port on the screen, and leaves what's on
        the screen where it was): True if pixels it had to drop are kept
        now, and need drawing."""
        r = self.content
        at, self.backing_at = self.backing_at, r.top_left
        if not moved and at != r.top_left:
            self.backing.scroll(at.h - r.left, at.v - r.top)
        return self.backing.fit(self.kept(r), Rect(0, 0, r.height, r.width))

    @property
    def struc_rect(self) -> Rect:
        return rgn_bbox(self.mem, self.struc_rgn)

    def wdef_struc(self) -> Region:
        """The structure region its own WDEF worked out, as far as it's on
        the screen: all of the window there is to show, whatever the WDEF
        made of it."""
        screen = Region.rect(self.system.screen_bits.bounds)
        return read_rgn(self.mem, self.struc_rgn).sect(screen)

    @struc_rect.setter
    def struc_rect(self, r: Rect) -> None:
        set_rect_rgn(self.heap, self.struc_rgn, r)

    def covers(self) -> Region:
        """What of the screen the window covers: its frame and content."""
        return read_rgn(self.mem, self.struc_rgn).union(Region.rect(self.content))

    # -- what it is ----------------------------------------------------------
    @property
    def title(self) -> str:
        h = self.title_handle
        return from_mac(self.mem.r_pstr(self.mem.r32(h) & self.system.addr_mask)) if h else ""

    @title.setter
    def title(self, text: str) -> None:
        data = to_mac(text)[:255]
        if self.heap.set_handle_size(self.title_handle, len(data) + 1):
            self.mem.w_pstr(self.heap.deref(self.title_handle), data)

    @property
    def proc_id(self) -> int:
        # Finding the WDEF's ID means searching the resource maps, and the
        # chrome asks on every paint, so remember it until windowDefProc changes.
        return self._defproc()[1]

    @property
    def own_wdef(self) -> bool:
        """True if windowDefProc is the application's own code (a WDEF
        resource of its own, or code it put in a handle itself) rather than
        the System's WDEF 0 or 1, whose frames the host draws."""
        return self._defproc()[2]

    def _defproc(self) -> tuple[int, int, bool]:
        wdp = self.window_def_proc
        if self._proc_id is None or self._proc_id[0] != wdp:
            # The low 24 bits are the WDEF's handle; the top byte, which 24-bit
            # machines pack the ID and variation in, is part of the handle on
            # a 32-bit machine, so addr_mask, not a 24-bit mask.
            kind, wdef = self.process.resources.defproc_id(wdp & self.system.addr_mask)
            own = bool(wdp & self.system.addr_mask) and not (kind == "WDEF" and wdef in (0, 1))
            self._proc_id = (wdp, (wdef << 4) | ((wdp >> 24) & 0xF), own)
        return self._proc_id

    @property
    def definition(self) -> Definition:
        return describe_proc(self.proc_id)

    # -- zooming -------------------------------------------------------------
    def _state(self, offset: int) -> Rect | None:
        h = self.data_handle
        return self.mem.r_rect((self.mem.r32(h) & self.system.addr_mask) + offset) if h else None

    def _set_state(self, offset: int, r: Rect) -> None:
        h = self.data_handle
        if h:
            self.mem.w_rect((self.mem.r32(h) & self.system.addr_mask) + offset, r)

    @property
    def user_state(self) -> Rect | None:
        return self._state(0)

    @user_state.setter
    def user_state(self, r: Rect) -> None:
        self._set_state(0, r)

    @property
    def std_state(self) -> Rect | None:
        return self._state(8)

    @std_state.setter
    def std_state(self, r: Rect) -> None:
        self._set_state(8, r)

    @property
    def zoomed_out(self) -> bool:
        """True when the window sits at its standard (zoomed) state."""
        std = self.std_state
        return std is not None and self.content == std

    # -- updating ------------------------------------------------------------
    @property
    def update(self) -> Region:
        """The update region, in global coordinates."""
        return read_rgn(self.mem, self.update_rgn)

    @update.setter
    def update(self, rgn: Region) -> None:
        write_rgn(self.heap, self.update_rgn, rgn)

    @property
    def update_pending(self) -> bool:
        return not rgn_bbox(self.mem, self.update_rgn).empty

    @update_pending.setter
    def update_pending(self, pending: bool) -> None:
        set_rect_rgn(self.heap, self.update_rgn, self.content if pending else Rect(0, 0, 0, 0))

    def invalidate(self, r: Rect | Region) -> None:
        """Add a rect or region, in global coordinates, to the update
        region: what of it is in the content area."""
        rgn = r if isinstance(r, Region) else Region.rect(r)
        self.update = self.update.union(rgn.sect(Region.rect(self.content)))

    def validate(self, r: Rect | Region) -> None:
        """Take a rect or region, in global coordinates, out of the
        update region."""
        rgn = r if isinstance(r, Region) else Region.rect(r)
        self.update = self.update.diff(rgn)

    def __repr__(self) -> str:
        return (f"<WindowRecord {self.title!r} at ${self.addr:06X} proc={self.proc_id} "
                f"{self.content}>")


class WindowList:
    """An application's windows, front to back: records chained through
    nextWindow, starting at its WindowList global."""

    def __init__(self, system: System, world: World):
        self.system = system
        self.world = world

    @property
    def head(self) -> int:
        return self.world.r32(WINDOW_LIST)

    @head.setter
    def head(self, addr: int) -> None:
        self.world.w32(WINDOW_LIST, addr)

    def __iter__(self) -> Iterator[WindowRecord]:
        addr, seen = self.head & self.system.addr_mask, set()
        while addr and addr not in seen:
            rec = self.system.port_at(addr)
            if not isinstance(rec, WindowRecord):
                return
            seen.add(addr)
            yield rec
            addr = rec.next_window & self.system.addr_mask

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __getitem__(self, index: int) -> WindowRecord:
        return list(self)[index]

    def __reversed__(self) -> Iterator[WindowRecord]:
        return reversed(list(self))

    def __contains__(self, rec) -> bool:
        return any(w is rec for w in self)

    def insert_front(self, rec: WindowRecord) -> None:
        rec.next_window = self.head
        self.head = rec.addr

    def insert_back(self, rec: WindowRecord) -> None:
        rec.next_window = 0
        last = None
        for last in self:
            pass
        if last is None:
            self.head = rec.addr
        else:
            last.next_window = rec.addr

    def insert_behind(self, rec: WindowRecord, other: WindowRecord) -> None:
        rec.next_window = other.next_window
        other.next_window = rec.addr

    def remove(self, rec: WindowRecord) -> None:
        addr = rec.addr & self.system.addr_mask
        if self.head & self.system.addr_mask == addr:
            self.head = rec.next_window
        else:
            for w in self:
                if w.next_window & self.system.addr_mask == addr:
                    w.next_window = rec.next_window
                    break
        rec.next_window = 0

    def bring_to_front(self, rec: WindowRecord) -> None:
        self.remove(rec)
        self.insert_front(rec)

    def clear(self) -> None:
        self.head = 0
