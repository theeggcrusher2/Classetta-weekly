"""
A running guest application.

A guest is either a real Mac application, whose 68k code runs on the
emulated CPU (see macrt.m68k.runtime), or a Python callable that takes a
Toolbox and runs a classic event loop the same way.  Traps from 68k code
end up in the same Toolbox.  Either way it owns a partition of guest
memory, and its windows and menus are records in it.

An application file, if there is one, is opened as the application's
resource file at launch, as the Segment Loader does.  Its SIZE resource
says how big a partition to give it, and its CODE 0 how big an A5 world.
"""

from __future__ import annotations

import itertools
import struct
import threading
import traceback
from collections import deque
from typing import TYPE_CHECKING, Callable

from .m68k import segload
from .macfile import Folder, MacFile, is_control_panel
from .memory import BusError
from .resfork import InvalidForkError, open_fork
from .toolbox import lowmem
from .toolbox.events import EventQueue, ProcessKilled, tick_count
from .toolbox.files import FileManager
from .toolbox.menus import (HELP_ITEM_COUNT, MenuColors, MenuInfo, MenuList, mct_entries,
                            menu_color_table)
from .toolbox.quickdraw import QD_GLOBALS_SIZE
from .toolbox.resources import ResourceManager
from .toolbox.system import (ALTERNATE_ROOM, APP_PARAMS, DEFAULT_PARTITION, DEFAULT_STACK,
                             MIN_HEAP, SIZE_STACK, System)
from .toolbox.windows import WindowList

if TYPE_CHECKING:
    from .m68k.runtime import Runner
    from .toolbox.api import Toolbox
    from .toolbox.bitfont import Strike
    from .toolbox.windows import WindowRecord
    from .ui.menubar import MenuBarWidget

GuestMain = Callable[["Toolbox"], None]
_serial_numbers = itertools.count(0x2000)       # the low long of a ProcessSerialNumber


def _size_data(app: MacFile) -> bytes:
    """SIZE 0, the one the user set in the Finder's Get Info, or else
    SIZE -1, the developer's."""
    try:
        fm = open_fork(app.rsrc)
        entry = fm.find(b"SIZE", 0) or fm.find(b"SIZE", -1)
        return fm.raw(entry) if entry else b""
    except InvalidForkError:
        return b""


def size_resource(app: MacFile) -> tuple[int, int] | None:
    """(preferred, minimum) partition size from the SIZE resource."""
    data = _size_data(app)
    if len(data) < 10:
        return None
    return struct.unpack_from(">II", data, 2)


def size_flags(app: MacFile) -> int:
    """The SIZE resource's flags: what the application says it can do."""
    data = _size_data(app)
    return struct.unpack_from(">H", data)[0] if len(data) >= 2 else 0


class Process:
    """main is the Python guest to run; None runs app's 68k code."""

    def __init__(self, system: System, name: str, main: GuestMain | None, events: EventQueue,
                 partition_size: int | None = None, app: MacFile | None = None,
                 folder: Folder | None = None, page_option: int = 0,
                 memory: int | None = None, as_panel: bool = True,
                 code0: segload.Layout | None = None, finder_info: bytes | None = None):
        self.system = system
        self.name = name
        self.events = events
        self.app = app
        # The Finder information the application that launched it left
        # in its AppParmHandle; None: its own documents'.
        self.finder_info = finder_info
        self.serial_number = next(_serial_numbers)
        self.launch_ticks = tick_count()
        layout = None
        hosted = False
        if main is None:
            # A control panel is hosted, as the Finder does, unless it's
            # an application as well, launched as one: After Dark 3.0's
            # file has CODE resources, and its INIT launches it.  code0 is
            # CODE 0's layout, if the launching application has read it.
            panel = app is not None and is_control_panel(app)
            hosted = panel and (as_panel or not segload.has_code(app))
            if hosted:
                from .m68k import controlpanel
                layout = controlpanel.LAYOUT
            elif app is not None:
                layout = code0 or segload.code0(app)
            if layout is None:
                why = segload.why_no_code(app) if app is not None else "no application"
                raise ValueError(f"{name} has no 68k code ({why}) to run")
        # A negative page option, as a word, asks for the alternate screen.
        alternate = bool(page_option & 0x8000)
        self.partition = self._open_partition(partition_size, layout, memory, alternate,
                                              hosted=hosted)
        self.heap = self.partition.heap
        self.world = self.partition.world
        self.world.w16(lowmem.CurPageOption, page_option)  # Launch's: < 0 for the alternate screen
        self.windows = WindowList(system, self.world)     # front to back
        self.windows_set_up = False                       # InitWindows has been called
        self.frames_due: set[WindowRecord] = set()        # for their own WDEFs to shape and draw
        self.menus = MenuList(system, self.world)         # the menu bar, InsertMenu order
        self.menus.init(self.heap)
        if system.application_menu is not None:
            self.menus.add_system(system.application_menu)
        self.help_menu: MenuInfo | None = None             # its Help menu, once it asks for it
        # The application's own Chicago 12, if it brings one: a Mac draws
        # the menus in it (Toolbox.InsertMenu finds it).
        self.system_strike: Strike | None = None
        folder = folder or (app.folder if app else None)
        start = system.files.dir_for(folder)
        if app is not None and (start is None or folder is app.folder):
            start = system.files.node_for(app).parent
        self.files = FileManager(system.files, self.world, start)
        self.resources = ResourceManager(system, self.world, self.heap, folder, self.files)
        self.resources.init()
        if app is not None:
            refnum = self.resources.open(app)
            if refnum < 0:
                self.files.close_all()
                system.close_partition(self.partition)
                raise ValueError(f"{app.name!r} has no usable resource fork "
                                 f"(error {self.resources.error})")
            self.world.w16(lowmem.CurApRefNum, refnum)
        self.hidden = False
        self.faulted = False                        # its records broke the GUI; it's being stopped
        self.notifications: set[int] = set()        # NMRecs installed (NMInstall)
        self.responses_due: deque[int] = deque()    # of those, the ones to respond to
        self.shutdown_procs: dict[int, int] = {}    # ShutDwnInstall's, with their flags
        self.pict_infos: dict[int, object] = {}     # NewPictInfo's surveys, by ID
        # The Palette Manager's: each window's palette and the colour updates
        # it wants; -1's, the application's own default palette.
        self.palettes: dict[int, tuple[int, int]] = {}
        self.last_bar: MenuBarWidget | None = None  # where HiliteMenu applies
        self.menu_choice = 0                        # what MenuChoice returns
        # Its cursor's shape (macrt.toolbox.cursors), the one the display
        # was last told to show, and whether it's obscured till the mouse moves.
        self.cursor = None
        self.cursor_shown = None
        self.cursor_obscured = False
        self.thread: threading.Thread | None = None
        self.runner: Runner | None = None
        if layout is not None:
            from .m68k import runtime
            self.runner = runtime.Runner(self, layout)
            main = self.runner.main
        self.main = main

    def _open_partition(self, size: int | None, layout: segload.Layout | None,
                        memory: int | None = None, alternate: bool = False,
                        hosted: bool = False):
        """memory, if given, is the memory size the Finder's Get Info sets,
        in place of the SIZE resource's.  alternate: it was launched with
        the alternate screen, whose room comes on top.  hosted: it's a
        control panel, hosted as the Finder does, with a partition of the
        host's.  Its file's memory size and SIZE resource are for the
        application its CODE makes (After Dark 4.0's Demo)."""
        world = {}
        if layout is not None:
            world = dict(above_a5=max(layout.above_a5, APP_PARAMS), below_a5=layout.below_a5,
                         qd_globals=False)
        room = ALTERNATE_ROOM if alternate else 0
        if alternate:
            world["alternate"] = True
        if size is not None:
            return self.system.open_partition(self.name, size + room, **world)
        sizes = (None if hosted else (memory, memory) if memory else
                 size_resource(self.app) if self.app else None)
        if sizes is None:
            return self.system.open_partition(self.name, DEFAULT_PARTITION + room, **world)
        # As the Finder does: the preferred size if there's room, else as
        # much as there is, as long as that's at least the minimum.  The
        # sizes hold SIZE_STACK of stack and this Mac gives DEFAULT_STACK,
        # so the difference comes on top, and the heap is the one the
        # sizes leave room for.  GNU Chess Pro halves the two tables it
        # makes until both fit; in a heap 8K bigger they fit at 160K each
        # and leave too little for the rest of what it makes.
        # And room for the A5 world and the least heap there is: Beyond Dark
        # Castle's launcher asks for 20K.
        # (Rounding A5 and ApplLimit down to longs can take 6 bytes.)
        least = (MIN_HEAP + world.get("above_a5", APP_PARAMS)
                 + world.get("below_a5", QD_GLOBALS_SIZE) + 8)
        preferred, minimum = (max(n - SIZE_STACK, least) + DEFAULT_STACK + room for n in sizes)
        free = self.system.largest_free()
        if free < minimum:
            raise MemoryError(f"{self.name} needs {sizes[1] // 1024}K of memory and "
                              f"{(DEFAULT_STACK - SIZE_STACK) // 1024}K more for its stack, "
                              f"and only {free // 1024}K is free")
        return self.system.open_partition(self.name, min(max(preferred, minimum), free),
                                          **world)

    def start(self, toolbox: Toolbox, on_exit: Callable[[Process], None]) -> None:
        def run():
            try:
                self.main(toolbox)
            except ProcessKilled:
                pass
            except Exception:  # noqa: BLE001 - a crashing guest shouldn't take the runtime down
                traceback.print_exc()
            finally:
                on_exit(self)

        self.thread = threading.Thread(target=run, name=f"guest:{self.name}", daemon=True)
        self.thread.start()

    def kill(self) -> None:
        """Stop the application, wherever it is: waiting for an event,
        tracking the mouse, or in the middle of its own code."""
        self.events.kill()
        if self.runner is not None:
            self.runner.interrupt()

    def close(self) -> None:
        """Close the application's resource files and the files and working
        directories it left open, and give the partition back once the
        application is gone. A map chain the application overwrote
        leaves its maps where they are; their files close with the rest."""
        try:
            self.resources.close_all()
        except BusError:
            pass
        self.files.close_all()
        self.system.screen.detach(self)
        self.system.close_partition(self.partition)

    def bar_menus(self) -> list[MenuInfo]:
        """The menus its menu bar shows: its own in the menu list, then its
        Help menu once it has added items of its own to it."""
        menus = self.menus.own()
        if self.help_menu is not None and self.help_menu.count() > HELP_ITEM_COUNT:
            menus.append(self.help_menu)
        return menus

    def menu_colors(self) -> MenuColors:
        """The colours its menu colour table gives its menus."""
        return MenuColors(mct_entries(self.system, menu_color_table(self.system, self.world)))

    def front_window(self) -> WindowRecord | None:
        """The first visible window but GhostWindow's, which Multiplan
        points at the window its formula bar is edited in: always in front
        of the sheet, never the active one."""
        ghost = self.world.r32(lowmem.GhostWindow) & self.system.addr_mask
        for w in self.windows:
            if w.visible and w.addr & self.system.addr_mask != ghost:
                return w
        return None

    def __repr__(self) -> str:
        return f"<Process {self.name!r}>"
