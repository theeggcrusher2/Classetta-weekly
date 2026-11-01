"""
The machine: guest RAM and how it's divided up.

    $000000  low-memory globals
    $003000  the system heap: the Window Manager port, the desktop region,
             the System file's resource map and its resources.  It
             starts at 256K and grows as it needs to, up to where the
             partitions start
    $100000  application partitions, one per running application
    top      the screen buffer: 1 bit a pixel, or on a Mac II with Color
             QuickDraw as many as its depth (see gdevice)

Each partition is laid out like a classic Mac's application space: the
application heap from the bottom, growing toward ApplLimit, the stack
below the A5 world, and the A5 world at the top.  The application's
globals sit below A5 with QuickDraw's last, so thePort is at -4(A5); the
application parameters and the jump table sit above it.
"""

from __future__ import annotations

import struct
import threading
from collections.abc import Sequence
from dataclasses import dataclass

from ..macfile import MacFile, finder_beside, fonts_beside
from ..memory import ADDRESS_MASK, RAM, from_mac, to_mac
from ..resfork import (InvalidForkError, build_fork, open_fork, resCompressed, resLocked,
                       resSysHeap, restype)
from . import lowmem, printing, sysres
from .files import FileManager, FileSystem
from .lowmem import World
from .bitfont import Strike, fond_ids, fond_sizes, font_family, font_id, font_ids
from .color import standard_table
from .constants import kApplicationMenuID
from .menus import MenuInfo
from .memmgr import APPL_MASTERS, GZ_PROC, LOCKED, MIN_BLOCK, MORE_MAST, RESOURCE, Heap
from .resources import UNWRITTEN, Item, ResourceManager, ResourceMap
from .events import InputGlobals
from .gdevice import Devices, PixMap
from .screen import Screen
from .pixels import rgb_value
from .qdraw import FAMILIES, MONO
from .quickdraw import QD_GLOBALS_SIZE, BitMap, GrafPort, init_qd_globals, new_rgn
from .types import Rect

RAM_SIZE = 8 << 20
# Over the top of RAM, where a Mac II's is with 24-bit addresses: a page of
# ROM, with the model's version at ROMBase + 8.  ROM85 says whether it has
# Color QuickDraw: a Mac II's does.  What the System leaves in the exception
# vectors is in it, above the screen, as it is on a real Mac: Flight
# Simulator takes a vector that isn't for a debugger's.
ROM_BASE, ROM_SIZE = RAM_SIZE, 0x1000
# The rest of the ROM's space, as a Mac II's is, reads 0 and drops what's
# written: Downhill Racer keeps ROMBase's high word where it keeps a handle
# it has none of, and reads through it.  So does what's above it, up to
# $C00000: HyperCard 2 copies its first card to the screen at its card
# window while that window is still hidden, $4000 pixels below and right of
# where it goes, which on a 640-by-480 screen is more than a megabyte past
# the top of RAM.
EMPTY_END = 0xC00000
SYS_HEAP_START = 0x3000
SYS_HEAP_END = SYS_HEAP_START + 0x80000
# A real System 7's System heap grows as it's needed ("the system heap
# expands dynamically to meet your request", Inside Macintosh: Memory,
# "Allocating Memory at Startup Time"), and its fonts and what programs
# load into it take more of it: with one it may grow to 1.5 MB, and
# applications' partitions start above that.  HomePublisher's segments
# all go in the System heap.  macrt's own System keeps to 512K, so that
# the first application's heap starts under 1 MB (below).
SYS7_HEAP_END = SYS_HEAP_START + 0x180000
SYS_HEAP_INITIAL = 0x40000      # the System heap's size before it grows
FONT_ROOM = 0x20000             # how much of a disk's System's fonts it adds
FOLDER_FONT_ROOM = 0x30000      # how much of a real System's Fonts folder's it adds
# Drivers a disk's System file has that macrt doesn't take from it, or
# load from any System file when a program opens one: those it answers
# itself (the printer's, the disk's, the sound's and the serial ports')
# and AppleTalk's, whose hardware it hasn't got.
UNLOADED_DRIVERS = frozenset({".print", ".sony", ".sound", ".ain", ".aout", ".bin", ".bout",
                              ".mpp", ".atp", ".xpp"})
# The types of resources Apple's System files have, which macrt makes its
# own of or leaves out; a disk System's other resources are its drivers'.
APPLE_SYSTEM_TYPES = frozenset(restype(kind) for kind in (
    "ALRT", "BNDL", "CDEF", "CNTL", "CURS", "DITL", "DLOG", "DRVR", "DSAT", "FKEY", "FMTR",
    "FOND", "FONT", "FREF", "FRSV", "FWID", "ICN#", "ICON", "INIT", "INTL", "KCAP", "KCHR",
    "KMAP", "KSWP", "LDEF", "MACS", "MBDF", "MDEF", "MENU", "NBPC", "NFNT", "PACK", "PAT ",
    "PAT#", "PDEF", "PICT", "PREC", "PRC0", "PRC3", "PTCH", "SERD", "SICN", "STR ", "STR#",
    "WDEF", "actb", "atlk", "boot", "cctb", "cdev", "cicn", "clut", "crsr", "dctb", "fctb",
    "finf", "gama", "ictb", "itl0", "itl1", "itl2", "itl4", "itlb", "itlc", "lmgr", "mcky",
    "mctb", "mitq", "pltt", "ppat", "ptch", "scrn", "snd ", "sysz", "wctb"))
# The resource IDs a control panel owns ('cdev' -4064's), in its own file
# and in the System file.
CONTROL_PANEL_IDS = range(-4064, -4032)
STANDARD_CLUTS = (1, 4, 8)      # the ROM's 'clut's macrt has the tables of
APP_PACKS = 8                   # the packages AppPacks has handles to
DRIVER_ROOM = 0x20000           # how much of a disk's System's drivers it adds
SOUND_ROOM = 0x10000            # and of its sounds
DEVICE_ROOM = 0x1000            # what the unit table and its DCEs start in
# A 512x342 screen is where a Mac 512K's is, under the top of its RAM.
# Programs from 1984 write it there themselves, and flip to the alternate
# screen under it; the System heap stops short of both.
CLASSIC_SIZE = (512, 342)
CLASSIC_SCREEN, CLASSIC_ALTERNATE = 0x7A700, 0x72700
CLASSIC_END = 0x80000           # the top of a Mac 512K's RAM, over the screen
# A Mac Plus's RAM repeats through its 4 MB of addresses, so its screens are
# at $3FA700 and $3F2700 too, however much RAM it has, and programs from
# 1986 write them there: Apache Strike's flight.  Here those are the classic
# screens, and applications' partitions keep out from under them.
PLUS_SCREENS, PLUS_SCREENS_END = 0x3F2000, 0x400000
PLUS_SCREEN = 0x3FA700          # the main one there
# An application launched with the alternate screen has its A5 world under
# the alternate screen and sound buffers, and BufPtr still over them:
# MacRacquetball launches itself again until BufPtr is that far over A5.
# Unless its partition ends right under the Plus's screens, the top
# ALTERNATE_ROOM of it stands for them.
ALTERNATE_ROOM = 0x8000
DEPTHS = (1, 2, 4, 8, 16, 32)  # bits a pixel the screen may have, with Color QuickDraw
COLOR_DEPTH = 8                 # and has, unless it's told otherwise
DEFAULT_PARTITION = 1 << 20
DEFAULT_STACK = 0x8000
# The stack in the partition a SIZE resource, or the Finder's Get Info,
# gives a program, with its heap and A5 world (Inside Macintosh: Memory,
# "Organization of Memory in an Application Partition"): "At the start of
# each application program, the limit is initialized to allow 8K bytes for
# the stack" (Inside Macintosh II-17).
SIZE_STACK = 0x2000
MIN_STACK = 0x400           # MinStack, as a Mac has it
APP_PARAMS = 32             # above A5, before the jump table
MIN_HEAP = 0x8000
INITIAL_HEAP = 0x1800       # the application heap's size at launch
PRINTER_STR = -8192         # STR: the printer resource file's name (IM II-163)


@dataclass(frozen=True)
class Model:
    """Which Mac macrt says it is, to Gestalt, SysEnvirons and in low
    memory.  The processor runs the 68020's instructions and the 68881's
    either way; what the model changes is the exception frames it stacks.
    A 68000's are six bytes, with no format word, and programs from before
    the Mac II pop them themselves: MacWrite 4.5 returns from its TRAP
    handler with ADDQ.W #2,A7 and RTS.  The Mac IIx is a Mac II that says
    it has a 68030 and a 68882, for programs that ask for them."""
    name: str
    cpu: int            # CPUFlag: 0 for a 68000, 2 for a 68020, 3 for a 68030
    fpu: int            # Gestalt's 'fpu ': 0 for none, 1 for a 68881, 2 for a 68882
    gestalt: int        # Gestalt's 'mach'
    environs: int       # SysEnvirons' machineType
    rom: int            # the ROM's version
    keyboard: int       # Gestalt's and SysEnvirons' keyboard type
    # Gestalt's 'hdwr': gestaltHasVIA1 (bit 0), gestaltHasVIA2 (1),
    # gestaltHasASC (3, the Apple Sound Chip), gestaltHasSCC (4) and
    # gestaltHasSCSI (7, a 53C80)
    hardware: int
    color: bool = False     # Color QuickDraw, and a screen of more than one bit a pixel
    slots: bool = False     # NuBus slots: the Slot Manager's VBL calls, the Start Manager's video
    adb: bool = False       # the Apple Desktop Bus: the ADB Manager's keyboard and mouse
    # The ROM's Toolbox trap table's entries: 512 in the 128K ROM, from
    # $C00 to $13FF, whose dispatcher takes a Toolbox trap number from
    # bits 0-8 of the word, bit 9 being "reserved for future use" (Inside
    # Macintosh I and IV, Using Assembly Language).
    toolbox_traps: int = 1024


MODELS = {
    "plus": Model("Macintosh Plus", 0, 0, gestalt=4, environs=2, rom=0x0075, keyboard=3,
                  hardware=0x91, toolbox_traps=512),
    "ii": Model("Macintosh II", 2, 1, gestalt=6, environs=4, rom=0x0178, keyboard=5,
                hardware=0x9B, color=True, slots=True, adb=True),
    "iix": Model("Macintosh IIx", 3, 2, gestalt=7, environs=5, rom=0x0178, keyboard=5,
                 hardware=0x9B, color=True, slots=True, adb=True),
}

RTS = 0x4E75
VBL_TASK_SIZE = 14
SYSTEM_FILE = "System"
# The System version Gestalt, SysEnvirons and SysVersion give: System
# 7's, or a later System 7's when the System file's 'vers' 1 says so.
SYSTEM_VERSION = 0x0701
LAST_SYSTEM_7 = 0x0761
# The System file's standard definition procedures, and the selectors
# their code passes to _DialogDispatch (see System.defproc).  A selector's
# high byte is the size of the arguments in words.
STANDARD_DEFPROCS = {("WDEF", 0): 0x067E, ("WDEF", 1): 0x067D, ("MDEF", 0): 0x097C,
                     ("CDEF", 0): 0x067B, ("CDEF", 1): 0x067A, ("LDEF", 0): 0x0A79,
                     ("MBDF", 0): 0x0578, ("CDEF", 63): 0x0677, ("LDEF", 19): 0x0A76}


@dataclass(eq=False)
class Partition:
    name: str
    start: int
    end: int
    heap: Heap
    a5: int
    stack_base: int
    world: World
    vbl_task: int = 0       # the System's own task in its VBL queue
    # Where its QuickDraw has the screen, if not where the screen is:
    # another way to it (see System.screen_of).
    screen_base: int = 0
    # The A5 an InitGraf at another A5 moved QuickDraw's globals from; a
    # trap with A5 back at it moves them back (see Runner._trap).
    left_a5: int = 0


def file_version(f: MacFile | None) -> int:
    """The version f's 'vers' 1 gives, as $0755 for 7.5.5; 0 if it has none."""
    try:
        fork = open_fork(f.rsrc) if f is not None else None
    except InvalidForkError:
        fork = None
    vers = next((fork.raw(e) for e in fork.entries
                 if e.type == b"vers" and e.id == 1 and not e.attrs & resCompressed),
                b"") if fork is not None else b""
    return int.from_bytes(vers[:2], "big") if len(vers) >= 2 else 0


def system_version(f: MacFile | None) -> int:
    """The System version to give, with f the System file in use or on
    the applications' disk: its 'vers' 1's, as System 7.5's own utilities
    look for 7.5, but never earlier than SYSTEM_VERSION (macrt is a System
    7) or later than LAST_SYSTEM_7."""
    vers = file_version(f)
    if not vers:
        return SYSTEM_VERSION
    return min(max(vers, SYSTEM_VERSION), LAST_SYSTEM_7)


def init_pack(rm: ResourceManager, n: int) -> None:
    """Put a handle to package n's resource in AppPacks, as InitPack does
    (Inside Macintosh I, the Package Manager), without reading it in:
    macrt runs every package itself, and a program that looks in AppPacks
    loads it if it wants it (Date & Time checks Pack7's is there)."""
    if 0 <= n < APP_PACKS:
        load, err = rm.res_load, rm.error
        rm.SetResLoad(False)
        try:
            rm.mem.w32(lowmem.AppPacks + 4 * n, rm.GetResource("PACK", n))
        finally:
            rm.SetResLoad(load)
            rm.error = err


class System:
    def __init__(self, screen_size: tuple[int, int] = (640, 480), *, desk_top: int = 0,
                 menu_bar_height: int = 20, system_file: MacFile | None = None,
                 disk_system: MacFile | None = None, disk_fonts: Sequence[MacFile] = (),
                 speed: float | None = None, model: str = "plus", sound: Sequence = (),
                 depth: int | None = None, row_bytes: int | None = None):
        """system_file: a real System file to use in place of macrt's own.
        disk_system: the System file on the disk the applications come
        from, whose fonts macrt's own adds (see _disk_fonts), and
        disk_fonts the font suitcases beside it (macfile.fonts_beside),
        whose fonts it adds too.  speed: how
        fast 68k code may run, as a multiple of a Mac Plus; None for as
        fast as it goes.  model: which of MODELS it says it is.  sound:
        where the speaker's sound goes, if anywhere (macrt.m68k.sound's
        Speaker).  depth: the screen's bits a pixel, on a model with Color
        QuickDraw (COLOR_DEPTH if not given); 1 on one without.  row_bytes:
        how many bytes apart the screen's rows are at that depth, and at
        another in proportion to its bits a pixel, as if the screen's
        memory were that many pixels wider than shows (as few as a row
        needs, if not given), for a program that takes them to be so many
        rather than asking."""
        w, h = screen_size
        self.speed = speed
        self.sound = list(sound)
        self.model = MODELS[model]
        self.version = system_version(system_file or disk_system)
        if depth is None:
            depth = COLOR_DEPTH if self.model.color else 1
        if depth not in DEPTHS or (depth != 1 and not self.model.color):
            raise ValueError(f"a {self.model.name}'s screen can't be {depth} bits a pixel")
        self.depth = depth
        self._width, self._row_bytes = w, (row_bytes or 0, depth)
        if row_bytes is not None and (row_bytes & 1 or not 0 < row_bytes < 0x4000):
            raise ValueError(f"a screen's rows can't be {row_bytes} bytes apart")
        self.mem = mem = RAM(RAM_SIZE + ROM_SIZE)
        self.lock = threading.RLock()
        row_bytes = self.row_bytes_for(depth)
        # Applications' partitions start where the System heap stops, or
        # over the classic screen, as on a Mac: the first one's heap starts
        # under 1 MB, unless a real System 7's heap has room to grow
        # (SYS7_HEAP_END).  Excel 1.0 keeps itself to the first megabyte,
        # and can't start if none of its heap is there.
        self._reserved: list[tuple[int, int]] = []      # what partitions keep out of
        classic = (w, h) == CLASSIC_SIZE and depth == 1 and row_bytes == w // 8
        if classic:
            base, sys_heap_end = CLASSIC_SCREEN, CLASSIC_ALTERNATE
            self.partitions_start, self.partitions_end = CLASSIC_END, RAM_SIZE
            mem.mirror(PLUS_SCREENS, PLUS_SCREENS_END, CLASSIC_ALTERNATE & ~0xFFF)
            self._reserved.append((PLUS_SCREENS, PLUS_SCREENS_END))
        else:
            base = (RAM_SIZE - row_bytes * h) & ~3
            if base < SYS_HEAP_END + DEFAULT_PARTITION:
                raise ValueError(f"a {w}x{h} screen leaves no room for applications")
            sys_heap_end = SYS_HEAP_END
            if file_version(system_file) >= 0x0700 and base >= SYS7_HEAP_END + DEFAULT_PARTITION:
                sys_heap_end = SYS7_HEAP_END
            self.partitions_start = sys_heap_end
            self.partitions_end = base
        self.screen_bits = BitMap(base, row_bytes, Rect(0, 0, h, w))
        self.screen_room = row_bytes * h               # what a deeper screen has to fit in
        # The alternate screen buffer, which the VIA can show instead.
        self.alternate_screen = CLASSIC_ALTERNATE if classic else None
        # What the heaps' blocks' sizes are multiples of, as the model's
        # Memory Manager has them: 2 on a 68000, 4 on a 68020 and later.
        self.align = 2 if self.model.cpu == 0 else 4
        self.sys_heap = Heap(mem, SYS_HEAP_START, SYS_HEAP_START + SYS_HEAP_INITIAL,
                             sys_heap_end, align=self.align)
        # Room at the bottom of the System heap for the Device Manager's
        # unit table and drivers' device control entries, which a Mac makes
        # there at startup: macrt.m68k.glue_devices has it.
        self.device_room = self.sys_heap.new_ptr(DEVICE_ROOM)
        self.ports: dict[int, GrafPort] = {}            # canonical views, by address
        # Drawing that's landed outside a port's portRect, which macrt
        # doesn't clip to, when something's watching for it (a survey's
        # outside_port_rect): QuickDraw._outside_port_rect's.  None if not.
        self.outside_port_rect: dict[tuple, dict] | None = None
        self.gestalt_procs: dict[int, int] = {}         # NewGestalt selector -> 68k function
        # PPostEvent's event queue entries in the system heap, each with the
        # queue and event it was last given for: free once that's been got
        self.event_qels: dict[int, tuple] = {}
        self.init_patches: dict[int, int] = {}          # trap key -> address: the INITs'
        self.extensions_off: set[str] = set()           # control panels to open without their INITs
        self.script_settings: dict[int, int] = {}       # SetEnvirons verb -> value
        self.kchr_cache = 0                             # GetEnvirons(smKCHRCache)'s copy
        self.font_records: dict[tuple, int] = {}        # FMSwapFont's FONTs of host fonts
        self.cicon_records: dict[int, int] = {}         # GetCIcon's records: handle -> size
        self.font_lines: dict[tuple[int, int], tuple[int, int, int]] = {}  # _disk_font_lines
        self.width_table = 0                            # and its global width table,
        self.width_request: tuple | None = None         # with what it was last made for
        self._partitions: list[Partition] = []
        self._zones: dict[int, tuple[Heap, Partition | None]] = {}     # InitZone's, by start
        self.temp_zone: Heap | None = None              # temporary memory's, once it's asked for
        self._rts = self.sys_heap.new_ptr(2)            # a routine that does nothing
        mem.w16(self._rts, RTS)

        mem.w32(lowmem.MemTop, RAM_SIZE)
        mem.w32(lowmem.ROMBase, ROM_BASE)
        mem.w16(ROM_BASE + 8, self.model.rom)
        mem.w32(lowmem.ScrnBase, base)
        # The top of applications' memory, as when nothing is installed
        # under the screen: Dark Castle asks for the alternate screen only
        # then.  Each application has its own (open_partition).
        mem.w32(lowmem.BufPtr, base)
        mem.w16(lowmem.ScreenRow, row_bytes)
        # The stack an application gets: The Fool's Errand moves ApplLimit
        # down by what it's short of 32K.
        mem.w32(lowmem.DefltStack, DEFAULT_STACK)
        mem.w32(lowmem.MinStack, MIN_STACK)
        mem.w32(lowmem.SysZone, SYS_HEAP_START)
        mem.w16(lowmem.MBarHeight, menu_bar_height)
        mem.w16(lowmem.ApFontID, 3)                     # the application font is Geneva
        # Color QuickDraw's graphics device, the screen's.
        self.devices = (Devices(mem, self.sys_heap, Rect(0, 0, h, w), base, row_bytes, depth)
                        if self.model.color else None)
        self.open_wmgr_port()
        mem.w32(lowmem.GrayRgn, new_rgn(self.sys_heap, Rect(desk_top, 0, h, w)))
        self.files = FileSystem(self)
        self.fcbs = self.files.fcbs                     # open files, by reference number
        self._open_system_file(system_file, disk_system, disk_fonts)
        self.screen = Screen(self, desk_top)            # the buffer at ScrnBase
        self.input = InputGlobals(mem, Rect(0, 0, h, w))  # the mouse and keys, in low memory
        # System 7's Application menu, in every application's menu list
        # after its own menus; the display gives it its items.
        self.application_menu = MenuInfo.new(self, self.sys_heap, kApplicationMenuID, "")

    def row_bytes_for(self, depth: int) -> int:
        """How many bytes apart the screen's rows are at a depth: as many
        as a row needs, or as many as asked for (row_bytes) at the depth
        it was asked for at, in proportion, if more."""
        asked, at = self._row_bytes
        return max((self._width * depth + 15) // 16 * 2, (-(-asked * depth // at) + 1) & ~1)

    def fits(self, depth: int) -> bool:
        """Whether the screen's buffer has room for it at a depth."""
        return self.row_bytes_for(depth) * self.screen_bits.bounds.height <= self.screen_room

    def set_depth(self, depth: int, gray: bool = False) -> bool:
        """Change the screen's bits a pixel, as its video card's driver
        changes mode (glue_video): to a depth whose buffer fits where the
        screen's is, as a card's memory holds only so many bits.  The
        screen's device, its colour table (the greys, with gray), the
        cGrafPorts on the screen and the buffer change with it.  False
        if the screen can't be that deep, or there's no Color QuickDraw."""
        devices = self.devices
        bits = self.screen_bits
        row_bytes = self.row_bytes_for(depth)
        if devices is None or depth not in DEPTHS or not self.fits(depth):
            return False
        with self.lock:
            if not devices.set_depth(depth, row_bytes, gray):
                return False
            self.depth = depth
            self.screen_bits = BitMap(bits.base, row_bytes, bits.bounds)
            self.mem.w16(lowmem.ScreenRow, row_bytes)
            self._screen_ports()
            self.screen.set_depth()
        return True

    def _screen_ports(self) -> None:
        """The screen's depth has changed: so has that of each cGrafPort on
        it, whose PixMap is a copy of the screen's, and its colours'
        pixel values (and each GrafPort's rowBytes)."""
        mem, devices = self.mem, self.devices
        main = devices.pixmap(devices.main)
        base, matching = main.base_addr & ADDRESS_MASK, devices.matching()
        for port in list(self.ports.values()):
            if port.port_bits_base & ADDRESS_MASK != base and not port.is_color:
                continue
            if not port.is_color:
                port.port_bits_row_bytes = self.screen_bits.row_bytes
                continue
            p = port.pixmap()
            if not p or mem.r32(p) & ADDRESS_MASK != base:
                continue
            pm = PixMap(mem, p)
            pm.row_bytes, pm.pixel_type, pm.pixel_size = (main.row_bytes, main.pixel_type,
                                                          main.pixel_size)
            pm.cmp_count, pm.cmp_size, pm.pm_table = main.cmp_count, main.cmp_size, main.pm_table
            for rgb, fore in ((port.rgb_fg_color, True), (port.rgb_bk_color, False)):
                r, g, b = rgb
                value = rgb_value(0xFF000000 | (r >> 8) << 16 | (g >> 8) << 8 | b >> 8,
                                  self.depth, matching)
                if fore:
                    port.fg_color = value
                else:
                    port.bk_color = value

    def wmgr_port_kept(self) -> bool:
        """Whether the Window Manager port is still there, not closed or
        disposed of."""
        old = getattr(self, "_wmgr", None)
        return (old is not None and self.ports.get(old.addr) is old
                and self.sys_heap.is_ptr(old.addr))

    def open_wmgr_port(self) -> None:
        """Make the Window Manager port, at startup and again for InitWindows
        when a program has thrown the last one away; with Color QuickDraw,
        its cGrafPort too, the first time."""
        with self.lock:
            old = getattr(self, "_wmgr", None)
            if old is not None and self.ports.get(old.addr) is old:
                self.unregister(old)
            self._wmgr = GrafPort(self.mem, self.sys_heap.new_ptr(GrafPort.SIZE))
            self._wmgr.open(self.sys_heap, self.screen_bits)
            self.register(self._wmgr)
            self.mem.w32(lowmem.WMgrPort, self._wmgr.addr)
            self.mem.w8(lowmem.WWExist, 0)
            if self.devices is not None and not self.mem.r32(lowmem.WMgrCPort):
                cport = GrafPort(self.mem, self.sys_heap.new_ptr(GrafPort.SIZE))
                if cport.open_color(self.sys_heap, self.screen_bits):
                    self.register(cport)
                    self.mem.w32(lowmem.WMgrCPort, cport.addr)

    # =====================================================================
    # Partitions
    # =====================================================================
    def open_partition(self, name: str, size: int = DEFAULT_PARTITION,
                       stack: int = DEFAULT_STACK, *, above_a5: int = APP_PARAMS,
                       below_a5: int = QD_GLOBALS_SIZE, qd_globals: bool = True,
                       alternate: bool = False) -> Partition:
        """Set up an application's memory: heap, stack, A5 world, and the
        switched globals that describe them.  A 68k application's CODE 0
        says how big its A5 world is, and its own InitGraf call sets up
        its QuickDraw globals; for a Python one they're set up here.
        alternate is for one launched with the alternate screen: with the
        classic screen, its partition ends right under the Plus's screens
        if there's room, and its ScrnBase and BufPtr are the main screen
        there, over the alternate buffers, as on a Mac Plus with 4 MB:
        Arkanoid's demo stops unless BufPtr is ScrnBase.  Else the top
        ALTERNATE_ROOM of the partition stands for the alternate buffers,
        between its A5 world and BufPtr, which is the Plus's screen still
        if the partition is under it (Arkanoid launches its game from a
        partition there), else the partition's end."""
        with self.lock:
            start = self._under_plus_screens(size) if alternate else None
            if start is None:
                start = self._room_for(size)
                top = start + size - (ALTERNATE_ROOM if alternate else 0)
            else:
                top = start + size
            end = start + size
            screen_base = buf_ptr = self.screen_bits.base
            if alternate:
                buf_ptr = end
                if self.alternate_screen is not None and end <= PLUS_SCREENS:
                    screen_base = buf_ptr = PLUS_SCREEN
            a5 = (top - above_a5) & ~3
            # Exactly below_a5 under A5, not rounded: start-up code that
            # fills the globals from DATA and ZERO resources copies from
            # CurStackBase until it reaches A5, and runs on past it if the
            # two are further apart than CODE 0 said.
            stack_base = a5 - below_a5
            appl_limit = (stack_base - stack) & ~3
            if appl_limit - start < MIN_HEAP:
                raise MemoryError(f"a {size // 1024}K partition is too small for {name}: "
                                  f"its globals and stack leave no room for a heap")
            self.mem.fill(appl_limit, end - appl_limit)
            world = World(self.mem)
            if not qd_globals:
                # QuickDraw and the Window Manager not set up yet, for the
                # application to do (Burtonia sets up the Toolbox only then).
                world.write(lowmem.WWExist, b"\xff\xff")      # and QDExist
            # The heap starts small and grows toward ApplLimit as it's
            # needed, or all at once when the application calls MaxApplZone.
            heap = Heap(self.mem, start, start + INITIAL_HEAP, appl_limit,
                        on_grow=lambda heap_end: world.w32(lowmem.HeapEnd, heap_end),
                        align=self.align, more_mast=APPL_MASTERS)
            if qd_globals:
                the_port = a5 - 4
                init_qd_globals(self.mem, the_port, self._screen_at(screen_base))
                self.mem.w32(a5, the_port)              # what InitGraf leaves in 0(A5)
            for addr, value in ((lowmem.ApplZone, start), (lowmem.TheZone, start),
                                (lowmem.HeapEnd, heap.end), (lowmem.ApplLimit, appl_limit),
                                (lowmem.CurStackBase, stack_base), (lowmem.CurrentA5, a5),
                                (lowmem.BufPtr, buf_ptr), (lowmem.ScrnBase, screen_base)):
                world.w32(addr, value)
            app_name = to_mac(name)[:31]
            world.write(lowmem.CurApName, bytes([len(app_name)]) + app_name.ljust(31, b"\0"))
            # A Mac's VBL queue always holds a task of the System's own,
            # the disk driver's: Prince of Persia looks for the task before
            # its own, to move its own to the front.  This one never comes due.
            vbl = self.sys_heap.new_ptr(VBL_TASK_SIZE, clear=True)
            self.mem.w16(vbl + 4, 1)                # qType: vType
            self.mem.w32(vbl + 6, self._rts)        # vblAddr
            world.write(lowmem.VBLQueue, struct.pack(">HII", 0, vbl, vbl))
            if self.devices is not None:
                world.w32(lowmem.TheGDevice, self.devices.main)
            part = Partition(name, start, end, heap, a5, stack_base, world, vbl,
                             screen_base if screen_base != self.screen_bits.base else 0)
            self._partitions.append(part)
            self._partitions.sort(key=lambda p: p.start)
            self._bound_temp_zone()
            return part

    def screen_of(self, process) -> BitMap:
        """The screen as process's QuickDraw has it, for screenBits and
        the ports it opens: at its partition's ScrnBase, which under the
        Plus's screens is $3FA700, another way to the classic screen, as
        on a Mac Plus with 4 MB.  Yeager's AFT launches itself again until
        its A5 world is under screenBits.baseAddr less $8000."""
        part = getattr(process, "partition", None)
        return self._screen_at(part.screen_base if part is not None else 0)

    def _screen_at(self, base: int) -> BitMap:
        bits = self.screen_bits
        return BitMap(base, bits.row_bytes, bits.bounds) if base else bits

    def close_partition(self, part: Partition) -> None:
        with self.lock:
            for addr in [a for a in self.ports if part.start <= a < part.end]:
                del self.ports[addr]
            # A Gestalt function left in the application's heap would be
            # called after it's gone; on a Mac that crashes, here it's forgotten.
            for sel in [s for s, a in self.gestalt_procs.items() if part.start <= a < part.end]:
                del self.gestalt_procs[sel]
            if part.vbl_task:
                self.sys_heap.dispose_ptr(part.vbl_task)
            for start in [s for s, (_, owner) in self._zones.items() if owner is part]:
                del self._zones[start]
            self._partitions.remove(part)
            self._bound_temp_zone()

    def _taken(self) -> list[tuple[int, int]]:
        """What partitions can't be made in, in order: the partitions open,
        what's reserved, and temporary memory's zone."""
        temp = [(self.temp_zone.start, self.temp_zone.end)] if self.temp_zone else []
        return sorted([(p.start, p.end) for p in self._partitions] + self._reserved + temp)

    def _room_for(self, size: int) -> int:
        at = self.partitions_start
        for start, end in self._taken():
            if start - at >= size:
                break
            at = end
        if at + size > self.partitions_end:
            raise MemoryError(f"no room for a {size // 1024}K partition")
        return at

    def _under_plus_screens(self, size: int) -> int | None:
        """Where a partition of size can start to end under the Plus's
        screens, if they're the classic screens and nothing's in the way."""
        start = PLUS_SCREENS - size
        if self.alternate_screen is None or start < self.partitions_start:
            return None
        if any(s < PLUS_SCREENS and e > start for s, e in self._taken()):
            return None
        return start

    def largest_free(self) -> int:
        """The biggest partition open_partition could make now, in whole K."""
        with self.lock:
            best, at = 0, self.partitions_start
            for start, end in self._taken():
                best, at = max(best, start - at), end
            return max(best, self.partitions_end - at) & ~0x3FF

    # =====================================================================
    # Temporary memory
    # =====================================================================
    def temp_heap(self) -> Heap:
        """Where temporary memory comes from: under MultiFinder, the memory
        outside every partition.  Here it's a zone made the first time it's
        asked for, above the partitions open then, that grows toward the top
        as it's needed, as far as the next partition up; the system heap, if
        there's no room.  After Dark 3.0 loads its modules' libraries into
        it, each a 20K block or more."""
        with self.lock:
            if self.temp_zone is None:
                at = max([self.partitions_start] + [end for _, end in self._taken()])
                if self.partitions_end - at >= MIN_HEAP:
                    self.temp_zone = Heap(self.mem, at, at + INITIAL_HEAP, self.partitions_end,
                                          align=self.align)
            return self.temp_zone or self.sys_heap

    def _bound_temp_zone(self) -> None:
        """Temporary memory's zone grows as far as the next partition up."""
        zone = self.temp_zone
        if zone is not None:
            above = [start for start, _ in self._taken() if start >= zone.end]
            zone.set_limit(min(above + [self.partitions_end]))

    def init_zone(self, start: int, limit: int, more_masters: int, grow_zone: int,
                  owner: Partition | None) -> Heap:
        """InitZone: a zone from start to limit, with its 12-byte trailer,
        and its first cMoreMasters master pointers (Inside Macintosh II-29).
        It may be in a block of another zone or in memory no zone has, as
        Winter Games makes one between the alternate screen buffer and the
        screen; it replaces the zones it overlaps, and goes with the
        partition of the program that made it, unless it's in the system
        heap, as After Dark 3.0's INIT makes one for its modules."""
        if self.sys_heap.contains(start):
            owner = None
        with self.lock:
            for s in [s for s, (h, _) in self._zones.items() if h.start < limit and start < h.end]:
                del self._zones[s]
            heap = Heap(self.mem, start, limit - MIN_BLOCK, align=self.align)
            self.mem.w32(start + GZ_PROC, grow_zone)
            self.mem.w16(start + MORE_MAST, more_masters)
            heap.more_masters()
            self._zones[start] = heap, owner
            return heap

    def zone(self, start: int) -> Heap | None:
        """The zone starting at start, if it's the system heap or one
        InitZone made."""
        start &= ADDRESS_MASK
        if start == self.sys_heap.start:
            return self.sys_heap
        zone = self._zones.get(start)
        return zone[0] if zone else None

    def find_heap(self, addr: int) -> Heap | None:
        """The heap addr is in; None for one in none, such as a handle
        whose master pointer is in a program's globals."""
        a = addr & ADDRESS_MASK
        for heap, _ in self._zones.values():
            if heap.contains(a):
                return heap
        if self.sys_heap.contains(a):
            return self.sys_heap
        if self.temp_zone is not None and self.temp_zone.contains(a):
            return self.temp_zone
        for p in self._partitions:
            if p.heap.contains(a):
                return p.heap
        return None

    def heap_of(self, addr: int) -> Heap:
        heap = self.find_heap(addr)
        if heap is None:
            raise ValueError(f"${addr & ADDRESS_MASK:06X} is not in a heap")
        return heap

    # =====================================================================
    # Canonical views: one Python object per port, so a WindowPtr that
    # comes back from guest code finds the same object.
    # =====================================================================
    def register(self, port: GrafPort) -> None:
        # By the address alone: a program may pass its storage with flags
        # in the high byte, as SuperPaint passes a locked handle's master
        # pointer to NewWindow, and use it without them after.
        with self.lock:
            self.ports[port.addr & ADDRESS_MASK] = port

    def unregister(self, port: GrafPort) -> None:
        with self.lock:
            self.ports.pop(port.addr & ADDRESS_MASK, None)

    def port_at(self, addr: int) -> GrafPort | None:
        return self.ports.get(addr & ADDRESS_MASK)

    def port_with_pixmap(self, pm: int) -> GrafPort | None:
        """The cGrafPort whose portPixMap is the PixMap at pm, if one's is."""
        with self.lock:
            ports = list(self.ports.values())
        for port in ports:
            if port.is_color and port.pixmap() == pm & ADDRESS_MASK:
                return port
        return None

    # =====================================================================
    # Files
    # =====================================================================
    def _open_system_file(self, f: MacFile | None, disk_system: MacFile | None,
                          disk_fonts: Sequence[MacFile] = ()) -> None:
        """The System file's map heads every application's resource chain
        at the bottom.  Without a real one, one with only what sysres
        gives it and the definition procedures, and the fonts and drivers
        of disk_system's, and the fonts of disk_fonts', that it hasn't.
        A real one gets the fonts of the Fonts folder beside it."""
        real = f is not None
        f = f or MacFile(SYSTEM_FILE, rsrc=build_fork([]), key="<system>")
        name = to_mac(SYSTEM_FILE)
        self.mem.write(lowmem.SysResName, bytes([len(name)]) + name)
        world = World(self.mem)
        self.file_manager = FileManager(self.files, world, self.files.system_folder)
        self.resources = ResourceManager(self, world, self.sys_heap, files=self.file_manager)
        self.resources.init()
        if self.resources.open_node(self.files.system_node(f), source=f) < 0:
            raise ValueError(f"{f.name!r} has no usable resource fork")
        self.mem.w32(lowmem.SysMapHndl, self.resources.top)
        self.mem.w16(lowmem.SysMap, self.resources.cur_map)
        for kind, def_id in STANDARD_DEFPROCS:
            self.defproc(kind, def_id)
        m = self.sys_map
        self._add_system_resources([r for r in sysres.standard()
                                    if m.find(restype(r[0]), r[1]) is None])
        # On a Mac II, the screen as it starts up, as the Control Panel
        # would have left it ('scrn' 0).
        if self.devices is not None and m.find(restype("scrn"), 0) is None:
            self._add_system_resources([("scrn", 0, self.devices.scrn())])
        # And the standard colour tables of 1, 4 and 8 bits, 'clut' 1, 4 and
        # 8, among the 'clut's Inside Macintosh V's "Resources in ROM" lists,
        # where RGetResource finds them; macrt's ROM resources are the System
        # file's.  Easy Color Paint sets the screen's colours to 'clut' 4.
        if self.devices is not None:
            self._add_system_resources([("clut", n, standard_table(n)) for n in STANDARD_CLUTS
                                        if m.find(restype("clut"), n) is None])
        if all(next(m.entries(restype(kind)), None) is None for kind in ("FOND", "FONT", "NFNT")):
            # A real System file brings its own.
            font_files = [disk_system, *disk_fonts] if disk_system is not None else []
            self._add_system_resources(sysres.fonts(self._disk_outlines(font_files)))
            if disk_system is not None:
                self._add_system_resources(self._disk_fonts(font_files))
                for font_file in font_files:
                    for key, lines in self._disk_font_lines(font_file).items():
                        self.font_lines.setdefault(key, lines)
                self._add_system_resources(self._disk_drivers(disk_system))
                self._add_system_resources(self._disk_tables(disk_system))
        if real:
            self._add_system_resources(self._folder_fonts(fonts_beside(f)), replace=True)
            # And the Finder beside it, in the System Folder with it, as on
            # the disk: FinderColors opens it, and says to restart with
            # MultiFinder off when it can't.
            finder = finder_beside(f)
            if finder is not None:
                self.files.in_system_folder(finder)
        # The printer is macrt's whatever the System file says: its own
        # printer file's code would drive a serial port.
        name = to_mac(printing.PRINTER_NAME)
        self._add_system_resources([("STR ", PRINTER_STR, bytes([len(name)]) + name)],
                                   replace=True)
        if m.find(restype("DRVR"), 2) is None:
            self._add_system_resources([("DRVR", 2, printing.driver(), ".Print")])
        self.files.in_system_folder(printing.printer_file())
        for n in range(APP_PACKS):
            init_pack(self.resources, n)

    def _disk_fonts(self, files: Sequence[MacFile]) -> list[tuple]:
        """The fonts in files, the System file on the disk the applications
        come from and the suitcases beside it, up to FONT_ROOM's worth, the
        first file's first: of the families macrt's own
        System hasn't got, their FONTs, their FONDs and the NFNTs these
        list; and then the strikes of its monospaced ones, Monaco and
        Courier, which are still drawn with host fonts but measured with
        these (AppFonts.measure), as with a real System file.  Programs
        drew in the fonts of the System they started up with, and some
        brought their own there: Mac Attack's tanks, missiles and
        landscape are characters in four fonts of its disk's System, and
        early disks have Venice, London and Athens.  And some count on a
        monospaced font's width: Hacker types each letter of Monaco 9 and
        its cursor after it, then backs up 6 pixels, Monaco's width, to
        type the next over the cursor.  (A host font's letters set at the
        widths of a proportional one's, Geneva's, are spaced unevenly.)"""
        fonts, room, taken = [], FONT_ROOM, set()
        for f in files:
            try:
                fork = open_fork(f.rsrc)
            except InvalidForkError:
                continue
            entries = [e for e in fork.entries if not e.attrs & resCompressed]
            fonds = [e for e in entries if e.type == b"FOND" and e.id not in sysres.FONTS]
            listed = {rid for e in fonds for rid in fond_ids(fork.raw(e))}
            picked = [e for e in entries
                      if e.type == b"FONT" and (e.id >> 7 not in sysres.FONTS or e.id in listed)
                      or e.type == b"NFNT" and e.id in listed] + fonds
            picked += [e for e in entries if e.type == b"FONT" and e.id & 0x7F
                       and FAMILIES.get(e.id >> 7) == MONO and e.id >> 7 in sysres.FONTS
                       and e not in picked]
            for e in picked:
                data = fork.raw(e)
                if (len(data) <= room and (e.type, e.id) not in taken
                        and self.sys_map.find(e.type, e.id) is None):
                    room -= len(data)
                    taken.add((e.type, e.id))
                    name = [e.name.decode("mac_roman")] if e.name else []
                    fonts.append((e.type.decode("mac_roman"), e.id, data, *name))
        return fonts

    def _disk_outlines(self, files: Sequence[MacFile]) -> dict[int, int]:
        """The standard families files, the System file on the disk the
        applications come from and the suitcases beside it, have an outline
        font of, size 0 in their FONDs, with its 'sfnt''s ID, the first
        file's first.  macrt's FONDs of these list it too, as the Fonts
        folder's do beside a real System file (_folder_fonts), and the
        'sfnt' isn't taken there either: host fonts draw these families at
        any size.  For an outline font RealFont "returns TRUE for almost
        any size" (Inside Macintosh: Text, "Adding Font Sizes and Names to
        the Menu"), and ATPM's DOCMaker issues, on a disk whose Fonts
        folder has Geneva, Helvetica and Monaco as outline fonts, ask after
        Geneva 11 and Helvetica 43 and say which are missing."""
        outlines: dict[int, int] = {}
        for f in files:
            try:
                fork = open_fork(f.rsrc)
            except InvalidForkError:
                continue
            for e in fork.entries:
                if e.type == b"FOND" and e.id in sysres.FONTS and not e.attrs & resCompressed:
                    for rid in font_ids(fork.raw(e), 0)[:1]:
                        outlines.setdefault(e.id, rid)
        return outlines

    def _folder_fonts(self, files: Sequence[MacFile]) -> list[tuple]:
        """The fonts in files, the suitcases in the Fonts folder beside a
        real System file, up to FOLDER_FONT_ROOM's worth: their FONDs, in
        place of the System file's of the same families, and then the
        strikes these list and the suitcases' FONTs, those of families
        host fonts don't stand in for first, as they're drawn from them.
        System 7.5's System file has Geneva 9 and 12, and its Fonts
        folder's Geneva the other sizes and the FOND that lists them all:
        HyperCard Player's stacks ask for Geneva 10 and 14, and for
        Courier and Helvetica by name.  Their outline fonts, the 'sfnt's,
        aren't taken, as host fonts draw the standard families at any
        size."""
        fonds, strikes = [], []
        for f in files:
            try:
                fork = open_fork(f.rsrc)
            except InvalidForkError:
                continue
            entries = [e for e in fork.entries if not e.attrs & resCompressed]
            families = [e for e in entries if e.type == b"FOND"]
            listed = {rid: e.id for e in families for rid in fond_ids(fork.raw(e))}
            fonds += [(fork, e) for e in families]
            for e in entries:
                if e.type in (b"FONT", b"NFNT") and (e.type == b"FONT" or e.id in listed):
                    family = listed.get(e.id, font_family(e.id))
                    strikes.append((family in sysres.FONTS, fork, e))
        strikes.sort(key=lambda s: s[0])
        found, room, taken = [], FOLDER_FONT_ROOM, set()
        for fork, e in fonds + [s[1:] for s in strikes]:
            if (e.type, e.id) in taken or (e.type != b"FOND"
                                           and self.sys_map.find(e.type, e.id) is not None):
                continue
            data = fork.raw(e)
            if len(data) <= room:
                room -= len(data)
                taken.add((e.type, e.id))
                found.append((e.type.decode("mac_roman"), e.id, data,
                              *([e.name.decode("mac_roman")] if e.name else [])))
        return found

    def _disk_font_lines(self, f: MacFile) -> dict[tuple[int, int], tuple[int, int, int]]:
        """The ascent, descent and leading of the strikes of the standard
        families in f, the System file on the disk the applications come
        from or a suitcase beside it, by family and size.  Host fonts draw these families, at
        their own widths (_disk_fonts), but lines of them are as far apart
        as the System's strikes set them: Chicago 12's are 16 pixels, and
        The Venice Project, which spaces its lines by GetFontInfo, puts
        "Enter the ... above" under the field it asks for, not across it."""
        try:
            fork = open_fork(f.rsrc)
        except InvalidForkError:
            return {}
        entries = [e for e in fork.entries if not e.attrs & resCompressed]
        fonds = {e.id: fork.raw(e) for e in entries if e.type == b"FOND"}
        strikes = {(e.type, e.id): e for e in entries if e.type in (b"FONT", b"NFNT")}
        lines = {}
        for family in sysres.FONTS:
            fond = fonds.get(family)
            sizes = fond_sizes(fond) if fond else set()
            sizes |= {rid & 0x7F for kind, rid in strikes
                      if kind == b"FONT" and font_family(rid) == family}
            for size in sizes - {0}:
                places = [(b"FONT", font_id(family, size))]
                places += [(kind, rid) for rid in (font_ids(fond, size) if fond else ())
                           for kind in (b"NFNT", b"FONT")]
                found = (Strike.parse(fork.raw(strikes[p])) for p in places if p in strikes)
                strike = next((s for s in found if s is not None), None)
                if strike is not None:
                    lines[family, size] = strike.ascent, strike.descent, strike.leading
        return lines

    def _disk_drivers(self, f: MacFile) -> list[tuple]:
        """The drivers in f, the System file on the disk the applications
        come from, that programs open by name, except those in
        UNLOADED_DRIVERS (not its desk accessories), and the resources of
        types Apple's System files haven't, which are theirs: up to
        DRIVER_ROOM's worth.  On a Mac, opening a driver that isn't in the
        unit table loads it from the System file, and some programs came
        with theirs there: Speller Bee speaks its words through its disk
        System's .SAY, which loads its code from OVLY resources there."""
        try:
            fork = open_fork(f.rsrc)
        except InvalidForkError:
            return []
        entries = [e for e in fork.entries
                   if not e.attrs & resCompressed and self.sys_map.find(e.type, e.id) is None]
        drivers = [e for e in entries if e.type == b"DRVR" and e.name
                   and e.name.startswith(b".") and from_mac(e.name).lower() not in UNLOADED_DRIVERS]
        if not drivers:
            return []
        found, room = [], DRIVER_ROOM
        for e in drivers + [e for e in entries if e.type not in APPLE_SYSTEM_TYPES]:
            data = fork.raw(e)
            if len(data) > room:
                break
            room -= len(data)
            found.append((e.type.decode("mac_roman"), e.id, data,
                          *([e.name.decode("mac_roman")] if e.name else [])))
        return found

    def _disk_tables(self, f: MacFile) -> list[tuple]:
        """The 'dcmp', 'fld#', 'itl2' and 'snd ' resources in f, the System file
        on the disk the applications come from, and the control panels'
        there.  A 'dcmp' is the code that expands the resources compressed
        with it, which the Resource Manager runs when rsrcfork can't
        (System 7.5's utilities' are compressed with its 'dcmp' 3); 'fld#'
        0 names the System Folder's folders, which Extensions Manager reads
        to name the ones it disables things into, and it keeps its sets in
        the System file, as an 'ESet' with an ID that control panels own.
        The 'itl2's are the ways of sorting text, by language, which Text
        lists by name (macrt's own comparisons stay the English ones), and
        the 'snd 's its alert sounds, which Sound lists (as many as fit in
        SOUND_ROOM)."""
        try:
            fork = open_fork(f.rsrc)
        except InvalidForkError:
            return []
        found, room = [], SOUND_ROOM
        for e in fork.entries:
            if (e.type not in (b"dcmp", b"fld#", b"itl2", b"snd ")
                    and not (e.id in CONTROL_PANEL_IDS and e.type not in APPLE_SYSTEM_TYPES)
                    or e.attrs & resCompressed or self.sys_map.find(e.type, e.id) is not None):
                continue
            data = fork.raw(e)
            if e.type == b"snd ":
                if len(data) > room:
                    continue
                room -= len(data)
            found.append((e.type.decode("mac_roman"), e.id, data,
                          *([e.name.decode("mac_roman")] if e.name else [])))
        return found

    @property
    def sys_map(self) -> ResourceMap:
        return ResourceMap(self.mem, self.mem.r32(lowmem.SysMapHndl))

    # =====================================================================
    # Definition procedures.  The standard ones are Python, but records
    # hold a handle to one.  They're resources in the System file; without
    # a real System file a standard one's code is MOVE.W #selector,D0 and
    # an auto-pop _DialogDispatch, so 68k code can call it too (Excel's
    # own CDEF passes messages on to CDEF 0), and any other's is an RTS.
    # =====================================================================
    def defproc(self, kind: str, def_id: int) -> int:
        with self.lock:
            h = self.resources.get(kind, def_id)
            if h:
                return h
            sel = STANDARD_DEFPROCS.get((kind, def_id))
            code = (struct.pack(">HHH", 0x303C, sel, 0xAE68) if sel is not None
                    else RTS.to_bytes(2, "big"))
            return self._add_system_resources([(kind, def_id, code)])[0]

    def _add_system_resources(self, resources: list[tuple], replace: bool = False) -> list[int]:
        """Add resources, (type, ID, data) or (type, ID, data, name), to the
        System file's map, in memory only and locked in the system heap,
        in place of any with the same type and ID if replace.  Their
        handles."""
        with self.lock:
            m = self.sys_map
            resident = self.resources.resident(m)
            items = []
            for kind, rid, data, *name in resources:
                h = self.sys_heap.new_handle_with(data)
                self.sys_heap.set_state(h, LOCKED | RESOURCE)
                items.append(Item(restype(kind), rid, to_mac(name[0]) if name else None,
                                  resSysHeap | resLocked, UNWRITTEN, h))
                resident[(restype(kind), rid)] = bytes(data)
            if items:
                new = {(it.type, it.id) for it in items} if replace else set()
                m.rewrite(self.sys_heap,
                          [it for it in m.items() if (it.type, it.id) not in new] + items)
            return [item.handle for item in items]

    def defproc_id(self, h: int) -> tuple[str, int]:
        """(resource type, ID) for a System file definition procedure,
        ("", 0) for anything else."""
        with self.lock:
            e = self.sys_map.find_handle(h)
            return (e.type.decode("mac_roman"), e.id) if e else ("", 0)
