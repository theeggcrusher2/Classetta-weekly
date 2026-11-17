"""
Running a 68k application: the Machine every 68k process shares, and
the Runner that is one process's main loop.

The Machine is the processor and what goes with it: the low-memory
globals that describe the hardware, the trap stubs GetTrapAddress hands
out, and the CPU lock.  As on a Mac under MultiFinder, one application
runs at a time.  It holds the lock while its code runs and gives it up
while it waits for an event; whoever takes it next gets its registers
and its switched low-memory globals put back.

The Runner starts the application through the Segment Loader, then runs
it: CPU.run() returns at each A-line trap, the Runner dispatches it, and
the CPU goes on.  Python code that needs to call back into 68k code (a
filter procedure, a patched trap) does it with call(), which runs a
nested loop until the routine returns.  The application's VBL tasks run
the same way, between two instructions, once a tick while its code runs
(see _vertical_retrace), with the Sound Driver's and the speaker's turn
after them (macrt.m68k.sound), and its Time Manager tasks at the tick
after they're due.  So does an application's own handler for the
VIA's interrupts, if it puts one in their vector.

An exception the application has no handler for stops it with a system
error, reported with its registers and the last traps it called, the
way MacsBug would.
"""

from __future__ import annotations

import collections
import functools
import logging
import struct
import sys
import threading
import time
import traceback
from contextlib import contextmanager, nullcontext
from typing import TYPE_CHECKING, Callable

from .. import moof
from ..memory import BusError, from_mac, to_mac
from ..toolbox import lowmem
from ..toolbox.files import ERIK, File
from ..toolbox.events import ProcessKilled, tick_count, to_next_tick
from ..toolbox.menus import MenuInfo
from ..toolbox.quickdraw import GRAY_PAT
from ..toolbox.screen import DESK
from ..toolbox.system import EMPTY_END, ROM_BASE, ROM_SIZE
from ..toolbox.types import Point, Rect
from ..toolbox.windows import WindowRecord
from . import cpu as m68k
from . import decompressors, dispatch, timemgr, wdefs
from .sound import ASC, ASC_BASE, ASC_SIZE, SoundDriver, Speaker, gestaltHasASC, vSndEnb
from .sound_manager import SoundManager
from .cpu import CPU
from .cycles import RATE, cycles
from .hardware import IWM, VIA, Clock, map_empty, map_test_rom, vBufA, vBufB, vHeadSel
from .segload import Layout, SegmentLoader, has_powerpc_code

if TYPE_CHECKING:
    from concurrent.futures import Future

    from ..process import Process
    from ..toolbox.api import Toolbox
    from ..toolbox.system import System

log = logging.getLogger("macrt.m68k")

ILLEGAL_WORD = 0x4AFC
RTS = 0x4E75
RTE = 0x4E73
TRAP_WORD = 0x4E40      # TRAP #n, with n in the low 4 bits
EXIT_TO_SHELL = 0xA9F4
DEBUGGER = 0xA9FF
N_TOOLBOX = 0x400
N_OS = 0x100

# System error IDs (Inside Macintosh, "System Error Handler")
SYSTEM_ERRORS = {
    0: "macrt itself failed",           # not a Mac ID: a bug here, not in the application
    1: "bus error", 2: "address error", 3: "illegal instruction", 4: "zero divide",
    5: "check trap", 6: "overflow trap", 7: "privilege violation", 8: "trace trap",
    9: "line 1010 trap", 10: "line 1111 trap", 11: "miscellaneous exception",
    12: "unimplemented trap", 15: "segment loader error", 25: "out of memory",
    28: "stack ran into heap",
}
VBL_ADDR, VBL_COUNT = 6, 10    # in a VBLTask record
DT_ADDR, DT_PARM = 8, 12       # in a DeferredTask record
SOUND_BUFFER = 0x300            # 370 words, rounded up as the Mac Plus's is
FINDER_INFO_PARAM = 16          # the application parameter with the Finder information's handle
USE_ASYNC = 2                   # a serial port's use type: the Serial Driver's, not AppleTalk's
appOpen = 0                     # the Finder information's message: open the documents

# The calls a program makes to ask how things are: EventAvail, GetMouse,
# StillDown, Button, TickCount, GetKeys, WaitMouseUp, OSEventAvail,
# GetOSEvent and SystemTask.  One that waits by asking over and over, a
# call within SPIN_GAP of the last with nothing else between, as Lemmings
# does on TickCount and Airborne! on Button, would keep the host's
# processor all the while.  What it asks about changes at a tick or with
# input, so after SPIN_BUDGET of that it waits for the next tick or for
# input, letting the others run as at Delay.  The host does many ticks of
# a Mac's work in that budget.  Both are the processor time its thread has
# had, where the host can tell (see thread_clock), not the clock's: the
# host giving the processor to something else between two calls, as it
# does under load, isn't the program doing something else.
POLLS = frozenset({0xA971, 0xA972, 0xA973, 0xA974, 0xA975, 0xA976, 0xA977,
                   0xA030, 0xA031, 0xA9B4})
# FlushEvents between them, as Runaround and Run call it between asking
# EventAvail for a key, keeps it asking, but doesn't wait itself: what came
# in while it waited there it might throw away.  So does KeyTranslate, as
# Space Junkie calls it between GetKeys, which is the same each time, and
# so do the calls that only say where things are or set the port, which
# change nothing the program or anyone sees, as ArcMac calls GetPort and
# SetPort between SystemTask, EventAvail and TickCount, and IAGO PtInRect
# between Button and StillDown.  So do locking a handle and unlocking it
# again, and priming a Time Manager task, which runs at the tick after it's
# due (see timemgr) whether the program asks or waits: Opening Night locks
# and unlocks the handles of its timers, primes a task for the next, and
# asks TickCount, OSEventAvail and GetMouse until it runs.
_KEYS = {name: key for key, name in dispatch.NAMES.items()}
KEEPS_POLLING = frozenset({0xA032, 0xA9C3} | {_KEYS[name] for name in (
    "GetPort", "SetPort", "PtInRect", "PtInRgn", "FrontWindow", "GlobalToLocal",
    "LocalToGlobal", "StripAddress", "HGetState", "HLock", "HUnlock", "PrimeTime")})
# The calls at whose end an application's own WDEFs shape and draw the
# windows whose frames need it (wdefs.refresh): the Window and Dialog
# Managers' that make, show, move, size, highlight or close windows, and
# the Event Manager's that a program waits in, for what the host did to its
# windows meanwhile, such as bringing another application forward.  Only
# these, not the next trap whatever it is: Excel 2.2a's WDEF is p-code
# that enters Excel's interpreter, and entered from within a BlockMove its
# interpreter makes, it left the interpreter returning to a segment that
# isn't there.
FRAMES_AT = frozenset(_KEYS[name] for name in (
    "NewWindow", "GetNewWindow", "NewCWindow", "GetNewCWindow", "CloseWindow",
    "DisposeWindow", "SetWTitle", "SelectWindow", "HideWindow", "ShowWindow", "ShowHide",
    "HiliteWindow", "BringToFront", "SendBehind", "MoveWindow", "DragWindow", "GrowWindow",
    "SizeWindow", "ZoomWindow", "TrackBox", "TrackGoAway", "DrawGrowIcon", "PaintOne",
    "PaintBehind", "DrawNew", "CalcVis", "CalcVisBehind", "SetWinColor",
    "NewDialog", "GetNewDialog", "NewColorDialog", "CloseDialog", "DisposeDialog",
    "ModalDialog", "DialogSelect", "Alert", "StopAlert", "NoteAlert", "CautionAlert",
    "GetNextEvent", "WaitNextEvent", "EventAvail", "SystemTask"))
SPIN_GAP = 0.0005               # seconds
SPIN_BUDGET = 0.002

# The ROM's handler for the VIA's interrupts: it calls the routine in
# Lvl1DT for the lowest of the flags that interrupted, with A1 pointing at
# the VIA, and keeps A0-A3 and D0-D3 for it (Inside Macintosh II,
# "Interrupts").  A flag still set after interrupts the processor again
# as it returns: here the handler goes round again.  A program puts its
# own routine in the table, as Master Tracks Pro does for timer 1, its
# tempo's clock.
LEVEL_1_HANDLER = bytes.fromhex(
    "48e7f0f0"          # movem.l d0-d3/a0-a3,-(sp)
    "41f8018e"          # 2: lea Lvl1DT-4,a0
    "227801d4"          # movea.l VIA,a1
    "10291a00"          # move.b vIFR(a1),d0
    "c0291c00"          # and.b vIER(a1),d0
    "0200007f"          # andi.b #$7F,d0
    "670c"              # beq.s 3f
    "5888"              # 1: addq.l #4,a0
    "e208"              # lsr.b #1,d0
    "64fa"              # bcc.s 1b
    "2050"              # movea.l (a0),a0
    "4e90"              # jsr (a0)
    "60de"              # bra.s 2b
    "4cdf0f0f"          # 3: movem.l (sp)+,d0-d3/a0-a3
    "4e73")             # rte
# What Lvl1DT points at, a routine for each of the VIA's flags, CA2 first,
# that the ROM's handler calls, or a program's own handler as the ROM's
# does (The Chessmaster 2000's is a copy of it).  Each clears its flag;
# the work of a tick or a second is done in Python.
LEVEL_1_ROUTINE = 12            # bytes each


def level_1_routine(flag: int) -> bytes:
    return struct.pack(">HHHHHH", 0x2078, lowmem.VIA,     # movea.l VIA,a0
                       0x117C, flag, 0x1A00,              # move.b #flag,vIFR(a0)
                       RTS)


# The ROM's keyboard translation, Key2Trans then Key1Trans, for programs
# that call it from low memory themselves (SuperPaint): D2 the key code,
# less $40 for Key2Trans's keypad keys, and D1 the modifiers' high byte;
# the character comes back in D0.  It is KeyTranslate.  Key1Trans's
# second word is a flag, as in the ROM, that Key Caps clears while it asks
# about a key, to have no dead keys: there are none here anyway.
KEY_TRANS = bytes.fromhex(
    "7040"              # Key2Trans: moveq #$40,d0
    "6006"              # bra.s translate
    "6002"              # Key1Trans: bra.s over the flag
    "ffff"              # dead keys: on
    "7000"              # moveq #0,d0
    "48e760c0"          # translate: movem.l d1-d2/a0-a1,-(sp)
    "e149"              # lsl.w #8,d1
    "1202"              # move.b d2,d1
    "d240"              # add.w d0,d1
    "42a7"              # clr.l -(sp): the state
    "42a7"              # clr.l -(sp): the result
    "42a7"              # clr.l -(sp): no transData
    "3f01"              # move.w d1,-(sp): the key code and modifiers
    "486f000a"          # pea 10(sp): the state
    "a9c3"              # _KeyTranslate
    "201f"              # move.l (sp)+,d0
    "588f"              # addq.l #4,sp
    "0280000000ff"      # andi.l #$FF,d0
    "4cdf0306"          # movem.l (sp)+,d1-d2/a0-a1
    "4e75")             # rts
# The cursor routines QuickDraw keeps vectors to in low memory, JHideCursor
# to JCrsrObscure, for programs that call them there (the Alarm Clock).
# JShieldCursor takes a rectangle in global coordinates, its top left
# pushed first, and pops it.
CURSOR_VECTORS = bytes.fromhex(
    "a8524e75"          # JHideCursor: _HideCursor; rts
    "a8534e75"          # JShowCursor: _ShowCursor; rts
    "a8504e75"          # JInitCrsr: _InitCursor; rts
    "a8564e75"          # JCrsrObscure: _ObscureCursor; rts
    "48e7e0c0"          # JShieldCursor: movem.l d0-d2/a0-a1,-(sp)
    "2f2f0018"          # move.l 24(sp),-(sp): bottom right
    "2f2f0020"          # move.l 32(sp),-(sp): top left
    "41d7"              # lea (sp),a0
    "2f08"              # move.l a0,-(sp): the rectangle
    "42a7"              # clr.l -(sp): global already
    "a855"              # _ShieldCursor
    "508f"              # addq.l #8,sp
    "4cdf0307"          # movem.l (sp)+,d0-d2/a0-a1
    "2f570008"          # move.l (sp),8(sp)
    "508f"              # addq.l #8,sp
    "4e75")             # rts
# FMSwapFont, for programs that call it by way of JSwapFont, as HyperCard
# does: called with JSR as the trap is, the input record's pointer on the
# stack above the return address and room for the result above that.
SWAP_FONT = bytes.fromhex(
    "42a7"              # clr.l -(sp)
    "2f2f0008"          # move.l 8(sp),-(sp): the input record
    "a901"              # _FMSwapFont
    "201f"              # move.l (sp)+,d0
    "205f"              # move.l (sp)+,a0: the return address
    "2f400004"          # move.l d0,4(sp)
    "588f"              # addq.l #4,sp
    "4ed0")             # jmp (a0)
# How the Device Manager waits for a synchronous request a driver hasn't
# finished, A0 its parameter block: until ioResult isn't 1 any more.  Each
# time round, the host waits for the next tick (Runner._wait_for_tick).
WAIT_FOR_IO = bytes.fromhex(
    "4a680010"          # wait: tst.w ioResult(a0)
    "6f04"              # ble.s done
    "4afc"              # the next tick
    "60f6"              # bra.s wait
    "30280010"          # done: move.w ioResult(a0),d0
    "48c0"              # ext.l d0
    "4e75")             # rts
WAIT_FOR_IO_TICK = 6

# How the Sound Manager's callers wait for a channel: until the word at A0
# isn't 1 any more, then on to A1.  The host waits for each tick too.
SOUND_WAIT = bytes.fromhex(
    "4a50"              # wait: tst.w (a0)
    "6f04"              # ble.s done
    "4afc"              # the next tick
    "60f8"              # bra.s wait
    "4ed1")             # done: jmp (a1)
SOUND_WAIT_TICK = 4


def init_dispatcher(at: int, dispatcher: int) -> bytes:
    """Where the ROM's start-up code puts its A-line trap dispatcher in the
    vector: the LEA for a 68000 or the one for a 68020, then MOVE.L A0,$28.
    Lotus Jazz finds the dispatcher by looking for the MOVE and taking the
    LEA before it (the one 6 bytes further back, on a 68000), to put it
    back in the vector in case a debugger has taken it."""
    return struct.pack(">HhHHhHH", 0x41FA, dispatcher - (at + 2),   # LEA dispatcher(PC),A0
                       0x6004,                                      # BRA.S set
                       0x41FA, dispatcher - (at + 8),               # LEA dispatcher(PC),A0
                       0x21C8, 0x0028)                              # set: MOVE.L A0,$28


MAX_LATE = 20           # ticks a retrace catches up on when the host was late
MAX_RETRACES = 6        # and after a break (see _retrace_tasks); after a longer gap, one
MAX_TIME_TASKS = 64     # Time Manager tasks run at one tick, at most
MAX_DEFERRED_TASKS = 64     # deferred tasks run at one tick, at most
INTERRUPT_REGISTERS = 32  # what an interrupt handler keeps on the stack: A0-A3 and D0-D3

VECTOR_ERRORS = {m68k.BUS_ERROR: 1, m68k.ADDRESS_ERROR: 2, m68k.ILLEGAL: 3,
                 m68k.ZERO_DIVIDE: 4, m68k.CHK: 5, m68k.TRAPV: 6, m68k.PRIVILEGE: 7,
                 m68k.TRACE: 8, m68k.LINE_A: 9, m68k.LINE_F: 10}
FORMAT_2 = {m68k.ZERO_DIVIDE, m68k.CHK, m68k.TRAPV, m68k.TRACE}


def _extension(mem, at: int, ea: int, size: int) -> int:
    """How many bytes of extension words the effective address ea (mode
    and register, as in an opcode's low 6 bits) has, from at."""
    mode = ea >> 3
    if mode == 5 or ea in (0o70, 0o72):     # d16(An), abs.W, d16(PC)
        return 2
    if ea == 0o71:                          # abs.L
        return 4
    if ea == 0o74:                          # immediate
        return 4 if size == 4 else 2
    if mode == 6 or ea == 0o73:             # indexed: brief, or the 68020's full format
        ext = mem.r16(at)
        if not ext & 0x100:
            return 2
        n = 2 + (0, 0, 2, 4)[ext >> 4 & 3]  # base displacement
        if ext & 7:                         # memory indirect: an outer displacement
            n += (0, 0, 2, 4)[ext & 3]
        return n
    return 0


def after_instruction(mem, pc: int) -> int:
    """Where the DIVU, DIVS, CHK, CHK2, TRAPV or TRAPcc at pc ends: where
    the exception it raises returns to."""
    op = mem.r16(pc)
    if m68k.is_trapcc(op):
        return pc + m68k.TRAPCC_SIZES[op & 7]
    if op & 0xF9C0 == 0x00C0:               # CHK2: an extension word, then the EA
        return pc + 4 + _extension(mem, pc + 4, op & 0x3F, 1 << (op >> 9 & 3))
    if op & 0xFFC0 == 0x4C40:               # DIVU.L, DIVS.L: an extension word, then the EA
        return pc + 4 + _extension(mem, pc + 4, op & 0x3F, 4)
    if op & 0xF0C0 == 0x80C0 or op & 0xF140 == 0x4100:     # DIVx.W, CHK.W, CHK.L
        size = 4 if op & 0xF1C0 == 0x4100 else 2
        return pc + 2 + _extension(mem, pc + 2, op & 0x3F, size)
    return pc + 2                           # TRAPV


def thread_clock() -> Callable[[], float]:
    """A reading of the calling thread's processor time that any thread can
    take, where the host has one (Linux); elsewhere, as on macOS, the
    clock's time, which counts the host's other work too."""
    clock = getattr(time, "pthread_getcpuclockid", None)
    if clock is None:
        return time.monotonic
    return functools.partial(time.clock_gettime, clock(threading.get_ident()))


class SystemError68k(Exception):
    """The application hit something it can't come back from."""

    def __init__(self, error_id: int, detail: str = "", pc: int | None = None):
        self.error_id = error_id
        self.detail = detail
        self.pc = pc                        # where it stopped, when that's known
        what = SYSTEM_ERRORS.get(error_id, "system error")
        super().__init__(f"system error {error_id} ({what})" + (f": {detail}" if detail else ""))


class Machine:
    """The processor and the parts of the Mac around it, one per System."""

    def __init__(self, system: System):
        self.system = system
        self.mem = system.mem
        self.rom_base = system.rom_base     # the ROM page: over the top of its RAM
        self.owner: Runner | None = None    # before the CPU's clock asks after its timers
        self.cpu = CPU(system.mem, system.model.cpu_core)
        self.clock = Clock()                # the VIA's and the drives', the same
        self.chips = system.chips           # where the machine's chips answer
        self.via = VIA(button=lambda: not self.mem.r8(lowmem.MBState) & 0x80, clock=self.clock)
        self.via.map(self.cpu.uc, *self.chips.via_window)
        self.via.on_page = system.screen.show_page
        self.iwm = IWM(head_select=lambda: bool(self.via.regs[vBufA] & vHeadSel),
                       clock=self.clock)
        self.iwm.surface = lambda drive, track, side: glue_sony.surface(self, drive, track, side)
        self.iwm.bits = lambda drive, track, side: glue_sony.bits(self, drive, track, side)
        self.iwm.map(self.cpu.uc, *self.chips.iwm_window)
        self.tag_buffer = 0                 # where the disk driver puts the tags it reads too
        self.disk_position = 0              # and where on the disk it read or wrote up to
        self.sector_buffer = 0              # the File Manager's, for a sector it reads so
        self.inserting = 0                  # the drive a disk switch is putting a disk in
        self.ejected: dict[int, int] = {}   # drive: the tick its disk was ejected at
        self.put_back: set[int] = set()     # drives whose disk is back but not mounted
        self.choosing: dict[int, tuple[Future, list]] = {}  # drive: the disk the user chooses
        self._map_address_space()
        self.cpu.on_tick = self._tick
        self.cpu.timers_waiting = lambda: self.owner is not None and bool(self.owner.time_tasks.due)
        self.sound = SoundDriver(self)
        self.sound_manager = SoundManager(self)
        self.asc: ASC | None = None
        if system.model.hardware >> gestaltHasASC & 1:
            self.asc = ASC()
            self.asc.map(self.cpu.uc, self.chips.asc)
        else:
            # Where the chip is on a colour machine, reads 0 without one:
            # Excel 1.5, where the ROM's version is below $0100, reads two
            # words there and compares them with 'RA'.
            map_empty(self.cpu.uc, ASC_BASE, ASC_BASE + ASC_SIZE)
        self.speaker = Speaker(self.mem, self.via, system.sound, self.asc, self.cpu)
        self.cpu.sound_waiting = lambda: (self.sound.counting() or self.speaker.waiting
                                          or self.sound_manager.busy())
        self.cpu.watching = self._quiet_pages
        if system.speed:
            self.cpu.rate = int(system.speed * RATE)
        self.cpu.short_frames = system.model.cpu == 0
        self._lock = threading.Lock()
        self._ticker: threading.Thread | None = None
        self.drivers: dict[int, Process] = {}      # unit: whose 68k driver it is
        self._make_stubs()
        self._init_globals()

    def _map_address_space(self) -> None:
        """Map what answers to everything the address space holds beyond
        RAM, so a stray read answers rather than faulting, as it does on
        a Mac with nothing in a slot.

        A 24-bit Mac's is macrt's own layout: the ROM's empty space under
        the chips'.  A 32-bit machine's RAM reaches past both, and what
        the core's static mapping doesn't cover is filled in around the
        chips' windows: it all reads 0 and drops what's written, as the
        Mac II's space above RAM does (see EMPTY_END)."""
        uc = self.cpu.uc
        if not self.system.mmu_32_bit:
            map_test_rom(uc)
            map_empty(uc, self.rom_base + ROM_SIZE, EMPTY_END)
            return
        chips = self.chips
        # What answers for itself: RAM and the ROM page mirrored into its
        # bytes, the chips, and the ASC if this model has one (mapped by
        # its own map(), below; its room is kept out here).
        taken = [(0, self.system.ram_size + ROM_SIZE), chips.via_window, chips.iwm_window]
        if self.system.model.hardware >> gestaltHasASC & 1:
            taken.append((chips.asc, chips.asc + 0x1000))
        taken.append((self.rom_base, self.rom_base + ROM_SIZE))
        taken.sort()
        at = taken[0][1]
        for lo, hi in taken[1:]:
            map_empty(uc, at, lo)
            at = hi
        map_empty(uc, at, 1 << 32)         # past the ROM page, to the top

    def _quiet_pages(self) -> list[int]:
        """The pages a store to which may turn the sound on, for the CPU to
        hold in a run without slices: vBufB's while the speaker's off, the
        chip's while it has nothing to play."""
        if not self.speaker.outputs:
            return []
        pages = []
        if self.via.regs[vBufB] & vSndEnb:
            pages.append(self.chips.via >> m68k.PAGE_BITS)
        if self.asc is not None and not self.asc.waiting:
            pages.append(self.chips.asc >> m68k.PAGE_BITS)
        return pages

    def _tick(self) -> None:
        """At each tick, from the clock: the cursor's task, and a look at
        the screen for code that runs a while without traps."""
        self.system.input.tick()
        self.system.screen.glance()

    def _make_stubs(self) -> None:
        """Code GetTrapAddress can point at: one stub per trap that, called
        with JSR, does that trap and returns, plus a few markers."""
        mem = self.mem
        size = 2 * N_TOOLBOX + 4 * N_OS + 20
        # On pages of their own: a page the core has code from is written
        # to by way of Python (see CPU._tlb_fill), and the sound buffer,
        # next in the System heap, is written to 370 times a tick by a game
        # that makes its own music, as Tetris does.
        span = -(-size // m68k.PAGE_SIZE) * m68k.PAGE_SIZE
        block = self.system.sys_heap.new_ptr(span + m68k.PAGE_SIZE)
        base = -(-block // m68k.PAGE_SIZE) * m68k.PAGE_SIZE
        self.stubs = (base, base + size)
        self.toolbox_stubs = base
        self.os_stubs = base + 2 * N_TOOLBOX
        for n in range(N_TOOLBOX):
            mem.w16(self.toolbox_stubs + 2 * n, 0xAC00 | n)        # auto-pop
        for n in range(N_OS):
            mem.w16(self.os_stubs + 4 * n, 0xA000 | n)
            mem.w16(self.os_stubs + 4 * n + 2, RTS)
        marks = self.os_stubs + 4 * N_OS
        self.return_stub = marks            # nested calls return here
        self.exit_stub = marks + 4          # an application's main routine returns here
        self.std_filter = marks + 8         # the Dialog Manager's, for GetStdFilterProc
        # An OS trap's patch returns to the dispatcher here: A0 to put back, or A0 a result
        self.os_patch_returns = (marks + 14, marks + 16)
        # Where a program that looks past a trap's code for the ROM's next RTE,
        # to make an interrupt do nothing, finds one
        self.rte_stub = marks + 18
        self.bomb_stub = self.rom_base + 16      # every exception vector, until replaced
        self.key_trans = self.rom_base + 64      # Key2Trans, then Key1Trans 4 bytes on
        self.io_done = self.rom_base + 130       # JIODone's: glue_devices does it
        self.cursor_vectors = self.rom_base + 132
        self.cursor_task = self.rom_base + 502   # JCrsrTask's: an RTS, the cursor's the host's
        self.data_marks = self.rom_base + 184    # what begins and ends a sector's data field
        self.rd_data = self.rom_base + 190       # the ROM's RdData: glue_sony does it
        self.te_do_text = self.rom_base + 192    # TextEdit's DoText: glue_textedit does it
        self.a_line = self.rom_base + 194        # the A-line trap dispatcher, for vector $28
        self.init_dispatcher = self.rom_base + 196
        self.rd_addr = self.rom_base + 210       # the ROM's RdAddr and AdrDisk: glue_sony does them
        self.adr_disk = self.rom_base + 212
        self.mount_vol = self.rom_base + 214     # _MountVol, as a program may have patched it
        self.level_1_routines = self.rom_base + 220      # Lvl1DT's, 8 of them
        self.denibble = self.rom_base + 316      # the six bits each byte on a disk stands for, from $96
        self.sony_routines = self.rom_base + 422     # the disk driver's other routines, 2 bytes each
        self.level_1 = self.rom_base + 434       # the VIA's interrupts' handler
        self.sound_task = self.rom_base + 478    # the Sound Driver's VBL task's: an RTS
        self.address_marks = self.rom_base + 486     # what begins and ends a sector's address field
        self.swap_font = self.rom_base + 492     # JSwapFont's
        self.crsr_task = self.rom_base + 512     # JCrsrTask's: the input globals do it
        self.load_trap = self.rom_base + 514     # _LoadSeg's _Debugger, with LoadTrap set
        self.adb_service = self.rom_base + 518   # the ADB devices' service routines: an RTS
        self.sound_wait = self.rom_base + 520    # where the Sound Manager's callers wait
        self.wait_for_io = self.rom_base + 530
        self.thread_exit = self.rom_base + 548   # where a Thread Manager thread's entry returns
        # Where those wait for a tick
        self.tick_waits = (self.sound_wait + SOUND_WAIT_TICK, self.wait_for_io + WAIT_FOR_IO_TICK)
        self.cpu.rom_interrupts = self.level_1
        self.cpu.no_handler = self.bomb_stub
        self.cpu.rom_level_1_routines = (self.level_1_routines,
                                         self.level_1_routines + 8 * LEVEL_1_ROUTINE)
        for at in (self.return_stub, *self.os_patch_returns):
            mem.w16(at, ILLEGAL_WORD)
        mem.w16(self.exit_stub, EXIT_TO_SHELL)
        mem.w16(self.rte_stub, RTE)
        mem.w16(self.bomb_stub, ILLEGAL_WORD)
        mem.write(self.level_1, LEVEL_1_HANDLER)
        mem.w16(self.sound_task, RTS)
        mem.w16(self.adb_service, RTS)
        mem.write(self.key_trans, KEY_TRANS)
        mem.write(self.wait_for_io, WAIT_FOR_IO)
        mem.write(self.sound_wait, SOUND_WAIT)
        mem.w16(self.io_done, ILLEGAL_WORD)
        mem.w16(self.cursor_task, RTS)
        mem.w32(lowmem.JCrsrTask, self.cursor_task)
        mem.w16(self.rd_data, ILLEGAL_WORD)
        mem.w16(self.rd_addr, ILLEGAL_WORD)
        mem.w16(self.adr_disk, ILLEGAL_WORD)
        for _, at in self._sony_table():
            mem.w16(at, ILLEGAL_WORD)
        mem.w16(self.te_do_text, ILLEGAL_WORD)
        mem.w16(self.crsr_task, ILLEGAL_WORD)
        mem.write(self.load_trap, struct.pack(">HH", DEBUGGER, RTS))
        mem.w16(self.a_line, ILLEGAL_WORD)
        mem.w16(self.thread_exit, ILLEGAL_WORD)
        mem.write(self.init_dispatcher, init_dispatcher(self.init_dispatcher, self.a_line))
        mem.write(self.cursor_vectors, CURSOR_VECTORS)
        mem.write(self.swap_font, SWAP_FONT)
        mem.write(self.mount_vol, struct.pack(">HH", 0xA00F, RTS))
        for n in range(8):
            mem.write(self.level_1_routines + LEVEL_1_ROUTINE * n, level_1_routine(1 << n))
        mem.write(self.data_marks, b"\xd5\xaa\xad\xde\xaa\xff")
        mem.write(self.address_marks, b"\xd5\xaa\x96\xde\xaa\xff")
        mem.write(self.denibble, bytes(moof.SIX.get(b, 0xFF) for b in range(moof.GCR[0], 0x100)))
        # MOVE.W #selector,D0; _DialogDispatch with auto-pop
        mem.write(self.std_filter,
                  struct.pack(">HHH", 0x303C, dispatch.STD_FILTER_SELECTOR, 0xAE68))

    def reset_vectors(self, part) -> None:
        """Put back the System's exception vectors and the disk driver's
        that an application leaving part pointed into it."""
        mem = self.mem
        for v in range(2, 64):
            if part.start <= mem.r32(4 * v) & self.mem.system.addr_mask < part.end:
                mem.w32(4 * v, self.vector(v))
        for vector, rom in ((lowmem.RdAddr, self.rd_addr), (lowmem.RdData, self.rd_data),
                            (lowmem.AdrDisk, self.adr_disk), (lowmem.TEDoText, self.te_do_text),
                            (lowmem.JSwapFont, self.swap_font), (lowmem.JCrsrTask, self.crsr_task),
                            *self._level_1_table(), *self._sony_table()):
            if part.start <= mem.r32(vector) & self.mem.system.addr_mask < part.end:
                mem.w32(vector, rom)
        if part.start <= self.tag_buffer < part.end:
            self.tag_buffer = 0

    def _level_1_table(self) -> list[tuple[int, int]]:
        return [(lowmem.Lvl1DT + 4 * n, self.level_1_routines + LEVEL_1_ROUTINE * n)
                for n in range(8)]

    def _sony_table(self) -> list[tuple[int, int]]:
        return [(vector, self.sony_routines + 2 * n)
                for n, (vector, _) in enumerate(glue_sony.ROUTINES)]

    def vector(self, v: int) -> int:
        """What the System leaves in exception vector v."""
        return {m68k.LEVEL_1: self.level_1, m68k.LINE_A: self.a_line}.get(v, self.bomb_stub)

    def stub_for(self, key: int) -> int:
        if dispatch.is_toolbox(key):
            return self.toolbox_stubs + 2 * (key & 0x3FF)
        return self.os_stubs + 4 * (key & 0xFF)

    def _init_globals(self) -> None:
        """What the ROM and System leave in low memory about the machine."""
        mem = self.mem
        for v in range(2, 64):
            mem.w32(4 * v, self.vector(v))
        for entry, rom in self._level_1_table():
            mem.w32(entry, rom)
        model = self.system.model
        mem.w8(lowmem.CPUFlag, model.cpu)
        # 128K ROM or later; with Color QuickDraw, its top two bits clear.
        mem.w16(lowmem.ROM85, 0x3FFF if model.color else 0x7FFF)
        mem.w16(lowmem.SysVersion, self.system.version)
        # Nonzero, and not the same twice: Leisure Suit Larry seeds
        # QuickDraw's randSeed from it, and from 0 every Random is 0.
        mem.w32(lowmem.RndSeed, m68k.mac_time() or 1)
        # MacWrite won't print, quit or open a desk accessory while it's 0,
        # in case The Monkey would.
        mem.w16(lowmem.MonkeyLives, 0xFFFF)
        # Lo3Bytes is how code is meant to learn the mask: $00FFFFFF
        # in 24-bit mode, and all of it where addresses aren't stripped.
        mem.w32(lowmem.Lo3Bytes, self.system.addr_mask)
        mem.w32(lowmem.MinusOne, 0xFFFFFFFF)
        mem.w32(lowmem.OneOne, 0x00010001)
        mem.w8(lowmem.CrsrCouple, 0xFF)
        mem.w16(lowmem.FSFCBLen, 94)                # HFS
        mem.w8(lowmem.MMU32Bit, 1 if model.mmu_32_bit else 0)
        mem.w16(lowmem.HWCfgFlags, 0x1000 if model.fpu else 0)
        mem.w16(lowmem.ScrVRes, 72)
        mem.w16(lowmem.ScrHRes, 72)
        # The initialization code determines these (Inside Macintosh:
        # Operating System Utilities, Start Manager, "Global Timing
        # Variables"): how many times a DBRA can be executed a millisecond,
        # and the SCC and the SCSI chip accessed.  macrt's are how many
        # times round a loop of a DBRA, and of a TST.B (A0) and a DBRA, the
        # cycle model lets the core go a millisecond when --speed paces it.
        # Unpaced, it goes round faster than a word counts, so they're the
        # most a word holds as programs take it, signed: Sesame Street
        # Letters draws every other row while TimeDBRA is 5004 or less.
        dbra, tst = cycles(b"\x51\xC8", 0), cycles(b"\x4A\x10", 0)     # DBRA D0; TST.B (A0)
        rate = self.cpu.rate
        for at, took in ((lowmem.TimeDBRA, dbra), (lowmem.TimeSCCDB, tst + dbra),
                         (lowmem.TimeSCSIDB, tst + dbra)):
            mem.w16(at, min(0x7FFF, rate // 1000 // took) if rate else 0x7FFF)
        double, caret, thresh, rate, ap_font = 32, 32, 16, 4, 3     # ticks; Geneva
        volume = 7                                  # the VIA's, as it starts (hardware.VIA)
        mem.w32(lowmem.DoubleTime, double)
        mem.w32(lowmem.CaretTime, caret)
        mem.w16(lowmem.KeyThresh, thresh)
        mem.w16(lowmem.KeyRepThresh, rate)
        mem.w16(lowmem.SysFontFam, 0)
        mem.w16(lowmem.SysFontSize, 12)
        mem.w16(lowmem.ApFontID, ap_font)           # the font family 1 means
        # Dark Castle mixes its sounds into the sound buffer only while this
        # says the volume isn't 0.
        mem.w8(lowmem.SdVolume, volume)
        # Parameter RAM saying the same, as InitUtil leaves it: Wizardry
        # takes its double-click time from volClik, not DoubleTime.  Its
        # port use, SPConfig, has the printer port the Serial Driver's, so
        # that programs that look don't open AppleTalk, which isn't here
        # (Inside Macintosh II, the AppleTalk Manager): JamPaint.
        mem.write(lowmem.SysParam, struct.pack(
            ">4B2HIh3H", 0xA8, 0, 0, USE_ASYNC,     # valid; AppleTalk node hints; port use
            0xCC0A, 0xCC0A, 0,                      # the serial ports: 9600 baud, 8N2; alarm
            ap_font - 1,
            (thresh // 4) << 12 | (rate // 2) << 8,  # kbdPrint: the printer on the printer port
            volume << 8 | (double // 4) << 4 | caret // 4,  # volClik
            0x4C))                                  # misc: mouse scaling; menus blink 3 times
        mem.w16(lowmem.SysEvtMask, 0xFFEF)          # everything but keyUp
        mem.w8(lowmem.SEvtEnb, 0xFF)
        mem.w8(lowmem.KbdType, 2)
        mem.w32(lowmem.Key1Trans, self.key_trans + 4)
        mem.w32(lowmem.Key2Trans, self.key_trans)
        for i, vector in enumerate((lowmem.JHideCursor, lowmem.JShowCursor, lowmem.JInitCrsr,
                                    lowmem.JCrsrObscure, lowmem.JShieldCursor)):
            mem.w32(vector, self.cursor_vectors + 4 * i)
        mem.w32(lowmem.JSwapFont, self.swap_font)
        mem.w32(lowmem.JCrsrTask, self.crsr_task)
        # The alarm isn't flashing the Apple menu's icon, has beeped and is
        # off, as the Alarm Clock leaves it: or the Alarm Clock, opening,
        # turns the icon back over.
        mem.w8(lowmem.AlarmState, 0xFE)
        mem.write(lowmem.DeskPattern, DESK)
        mem.write(lowmem.DragPattern, GRAY_PAT)
        mem.w32(lowmem.VIA, self.chips.via)
        mem.w32(lowmem.IWM, self.chips.iwm)
        if self.asc is not None:
            mem.w32(lowmem.ASCBase, self.chips.asc)
        mem.w32(lowmem.RdAddr, self.rd_addr)
        mem.w32(lowmem.RdData, self.rd_data)
        mem.w32(lowmem.AdrDisk, self.adr_disk)
        for vector, at in self._sony_table():
            mem.w32(vector, at)
        mem.w32(lowmem.TEDoText, self.te_do_text)
        mem.w16(lowmem.BootDrive, self.system.files.system_wd)
        mem.write(lowmem.FinderName, b"\x06Finder")
        # No driver has either serial port: their use bytes are negative
        # (Inside Macintosh II, the AppleTalk Manager, on PortBUse; Technical
        # Note DV 11 names PortAUse beside it).  LEGO Control Lab looks at
        # the modem port's before it opens the port for its interface box.
        mem.w8(lowmem.PortAUse, 0xFF)
        mem.w8(lowmem.PortBUse, 0xFF)
        # Word 1.0 takes $FFFF in this long's high word to mean it has kept
        # no handle there yet; anything else it uses as one.
        mem.w32(lowmem.Unnamed_AFC, 0xFFFFFFFF)
        # The sound buffer, which the speaker plays (macrt.m68k.sound).
        # Games write their samples straight into it, as on a Mac Plus or SE.
        mem.w32(lowmem.SoundBase, self.system.sys_heap.new_ptr(SOUND_BUFFER, clear=True))
        self.sound.install(self.system.sys_heap)
        sony = self.system.sys_heap.new_ptr(glue_sony.SONY_VARS, clear=True)
        mem.w32(lowmem.SonyVars, sony)
        glue_sony.set_up_vars(mem, sony)
        # Programs that load code call this before they run it.
        mem.w32(lowmem.jCacheFlush, self.stub_for(dispatch.trap_key(0xA0BD)))  # FlushCodeCache
        mem.w32(lowmem.jDTInstall, self.stub_for(dispatch.trap_key(0xA082)))   # DTInstall
        glue_devices.init(self)

    # =====================================================================
    # Who has the processor
    # =====================================================================
    def acquire(self, runner: Runner) -> None:
        self._lock.acquire()
        if self.owner is not runner:
            old = self.owner
            if old is not None:
                old.ctx = self.cpu.context()
                old.process.world.switch_out()
            if runner.ctx is not None:
                self.cpu.restore(runner.ctx)
                runner.ctx = None
            runner.process.world.switch_in()
            runner.catch_up = min(runner.catch_up, MAX_RETRACES)   # another's turn
            self.owner = runner

    def release(self) -> None:
        self._lock.release()

    def idle(self, runner: Runner) -> None:
        """runner, holding the processor, is about to wait for the host: if
        it has things to happen at the ticks while it does, the ticker
        sees to them."""
        if self._ticker is None and runner.wants_ticks():
            self._ticker = threading.Thread(target=self._tick_idle, name="macrt ticks",
                                            daemon=True)
            self._ticker.start()

    def ticking(self) -> bool:
        """Whether this is the ticker's thread, which holds the processor."""
        return threading.current_thread() is self._ticker

    def _tick_idle(self) -> None:
        """The ticker: at each tick that no application has the processor,
        the ticks of the one that had it last, which is waiting, as on a
        Mac, where interrupts go on while a program waits.  Once it has
        none to come, the ticker stops."""
        while True:
            time.sleep(1 / 60)
            if not self._lock.acquire(blocking=False):
                continue                # an application is running: it has its ticks
            try:
                owner = self.owner
                if owner is None or not owner.wants_ticks():
                    self._ticker = None
                    return
                owner.idle_tick()
            finally:
                self._lock.release()

    def leave(self, runner: Runner) -> None:
        """runner's process is done; it holds the lock."""
        if self.owner is runner:
            runner.process.world.switch_out()
            self.owner = None
        self._lock.release()

    def close(self) -> None:
        """The System's done with: its applications have stopped.  Stop
        the CPU's clock."""
        self.cpu.close()


def machine_for(system: System) -> Machine:
    with system.lock:
        m = getattr(system, "machine", None)
        if m is None:
            m = system.machine = Machine(system)
        return m


def finder_information(documents: list[tuple[int, File]]) -> bytes:
    """What the Finder tells an application it launches: the message, the
    count, then each document's working directory, type, version and name
    (Inside Macintosh II, the Segment Loader)."""
    out = [struct.pack(">hh", appOpen, len(documents))]
    for vref, node in documents:
        name = to_mac(node.name)[:63]
        entry = struct.pack(">h4sBx", vref, node.type, 0) + bytes([len(name)]) + name
        out.append(entry + bytes(len(entry) & 1))
    return b"".join(out)


class Runner:
    """One 68k application's main loop; it is the trap context the glue
    gets as t."""

    def __init__(self, process: Process, layout: Layout):
        self.process = process
        self.system = process.system
        self.mem = process.system.mem
        self.screen = process.system.screen
        self.machine = machine_for(process.system)
        self.cpu = self.machine.cpu
        self.layout = layout
        self.segments = SegmentLoader(self, layout)
        self.tb: Toolbox | None = None
        self.ctx = None
        self.holding = False
        self.expanding = False                      # running a 'dcmp'
        # Trap key -> address, from SetTrapAddress; the INITs' are everyone's.
        self.patches: dict[int, int] = dict(process.system.init_patches)
        self.history: collections.deque = collections.deque(maxlen=16)
        self.reported: set[str] = set()
        self.unimplemented_calls: collections.Counter = collections.Counter()
        # The traps it asked GetTrapAddress for, and the Gestalt selectors
        # it asked about, that macrt hasn't: a program that looks before it
        # calls takes another way without making an unimplemented call.
        self.missing_asked: collections.Counter = collections.Counter()
        self.internal_error: str | None = None      # the traceback, if macrt failed running it
        self.temp_handles: set[int] = set()         # TempNewHandle's
        self.trace = False
        self.menu_rect = 0          # mbRect's answer while MenuSelect pulls a menu down
        self.retrace_tick = 0       # when its VBL tasks were last counted down
        self.catch_up = 1           # the most ticks the next count makes up for
        self.null_tick = -1         # when GetNextEvent or WaitNextEvent last had nothing
        self.updated = (None, -1, 0)    # the update event it was last given: window, when, begun
        self.cpu_time = time.monotonic  # its thread's processor time, once it runs
        self.polled = -SPIN_GAP     # when the last of POLLS returned, if no other trap since
        self.spun_from = 0.0        # when it started asking over and over
        self.in_gne_patch = False   # WaitNextEvent is calling a GetNextEvent patch
        self.in_patch: set[int] = set()     # traps the System is calling a patch of
        self.ticks_failed = False   # a tick while it waited went wrong
        self.time_tasks = timemgr.TimeTasks()
        self.in_wdef = 0            # how deep in calls to its own WDEFs
        self.thread_id: int | None = None   # its thread's, once it runs
        process.events.enabled = lambda: self.mem.r16(lowmem.SysEvtMask)
        process.heap.grow_zone = self.call_grow_zone

    # =====================================================================
    # Life cycle
    # =====================================================================
    def main(self, toolbox: Toolbox) -> None:
        """The process's thread."""
        self.tb = toolbox
        if toolbox is not None:
            toolbox.wdefs = wdefs.WindowDefinitions(self)
        self.cpu_time = thread_clock()
        self.thread_id = threading.get_ident()
        self.machine.acquire(self)
        self.holding = True
        try:
            if self.process.events.killed:
                raise ProcessKilled()
            entry = self._start()
            if self.layout is controlpanel.LAYOUT:
                controlpanel.run(self)
            else:
                self.run_until(self.machine.exit_stub, entry)
            glue_desk.close_all(self)
            glue_components.close_all(self)
        except SystemError68k as e:
            self._report(e)
        except ProcessKilled:
            raise
        except Exception as e:  # noqa: BLE001 - say so, and stop only this application
            traceback.print_exc()
            self.internal_error = traceback.format_exc()
            where = (f" in {dispatch.trap_name(self.history[-1][0])}" if self.history else "")
            self._report(SystemError68k(0, f"{type(e).__name__}{where}: {e}"))
        finally:
            glue_threads.forget(self)
            glue_process.free_temp_memory(self)
            glue_devices.forget(self)
            glue_components.forget(self)
            self.machine.sound.forget(self)
            self.machine.sound_manager.forget(self)
            self.machine.reset_vectors(self.process.partition)
            self.machine.via.main_page()
            if self.holding:
                self.holding = False
                self.machine.leave(self)
            if self.unimplemented_calls:
                summary = ", ".join(f"{n} ({c})" for n, c in
                                    self.unimplemented_calls.most_common(12))
                log.info("%s called traps macrt doesn't implement: %s",
                         self.process.name, summary)

    def _start(self) -> int:
        """Registers and memory as the Segment Loader leaves them at launch."""
        cpu, part, world = self.cpu, self.process.partition, self.process.world
        for n in range(8):
            cpu.set_d(n, 0)
        for n in range(7):
            cpu.set_a(n, 0)
        cpu.sr = 0x2000
        cpu.set_a(5, part.a5)
        cpu.sp = part.stack_base
        cpu.push32(self.machine.exit_stub)
        entry = self.segments.install()
        app = self.process.app
        if app is not None and has_powerpc_code(app):
            log.info("%s also has PowerPC code (a 'cfrg' resource); macrt runs its 68k code",
                     self.process.name)
        documents = self._documents()
        info = self.process.finder_info
        if info is None:
            info = finder_information(documents)
        h = self.process.heap.new_handle(len(info))
        self.mem.write(self.process.heap.deref(h), info)
        world.w32(lowmem.AppParmHandle, h)
        # and in the application parameters, at 16(A5), where Alter Ego's
        # launcher reads it from
        self.mem.w32(part.a5 + FINDER_INFO_PARAM, h)
        glue_scrap.desk(self, from_host=True)               # its copy of the scrap (IM I-453)
        glue_aevents.launched(self, documents)
        self.process.resources.load_hook = (
            lambda m, e, force: glue_resources.load_resource(self, m, e, force))
        self.process.resources.get_hook = (
            lambda rtype, rid, one: glue_resources.get_resource(self, rtype, rid, one))
        self.process.resources.decompressor = lambda data: decompressors.expand(self, data)
        return entry

    def _documents(self) -> list[tuple[int, File]]:
        """The documents it's launched with, each with the working
        directory the Finder opens for its folder."""
        app, files = self.process.app, self.process.files
        out = []
        for doc in app.documents if app is not None else ():
            node = files.fs.node_for(doc)
            out.append((files.fs.open_wd(node.volume, node.parent.cnid, ERIK, files), node))
        return out

    def interrupt(self) -> None:
        """Stop the application's code soon, wherever it is (it's being killed)."""
        if self.machine.owner is self:
            self.cpu.interrupt()

    def _report(self, e: SystemError68k) -> None:
        lines = [f"macrt: {self.process.name} stopped: {e}", "  " + self.cpu.registers()]
        if self.history:
            lines.append("  last traps: " + ", ".join(
                f"{dispatch.trap_name(w)} at {self.mem.addr_str(pc)}" for w, pc in self.history))
        print("\n".join(lines), file=sys.stderr)
        report = getattr(self.tb._d, "report_system_error", None) if self.tb is not None else None
        if report is not None:
            report(self.process, e.error_id, str(e))

    # =====================================================================
    # Running code
    # =====================================================================
    def run_until(self, until: int, pc: int | None = None) -> None:
        cpu = self.cpu
        if pc is not None:
            cpu.pc = pc
        events = self.process.events
        while True:
            ev = cpu.run(until)
            if events.killed:
                cpu.interrupt_requested = False
                raise ProcessKilled()
            kind = ev.kind
            if kind == "trap":
                if self._own_a_line(ev.pc):
                    cpu.take_exception(m68k.LINE_A, ev.pc)
                else:
                    self._trap(ev.code, ev.pc)
            elif kind == "until":
                return
            elif kind == "interrupted":
                cpu.interrupt_requested = False
                if cpu.retrace_pending:
                    self._vertical_retrace()
                elif cpu.own_interrupts():
                    self._via_interrupt()
            elif kind == "exception":
                self._exception(ev.code, ev.pc)
            else:
                raise SystemError68k(1, f"unicorn error {ev.code} at {self.mem.addr_str(ev.pc)} "
                                        "(an access outside RAM?)", ev.pc)

    def _vertical_retrace(self) -> None:
        """The vertical retrace interrupt, between two instructions: count
        down each VBL task in the application's queue, once for each tick
        since the last time (see _retrace_tasks), and run those that
        reach 0, with A0 pointing at the task.  A task that wants to run
        again sets its count again.  After them at each tick, the Sound
        Driver's task and the Sound Manager's channels make the sound
        buffer's frame, and the speaker plays it.  Then the deferred
        tasks they queued run (_deferred_tasks), and the VIA may interrupt
        it (_via_interrupt).  While the application has interrupts masked,
        it waits."""
        cpu = self.cpu
        if cpu.sr & m68k.INTERRUPT_MASK:
            self.catch_up = min(self.catch_up, MAX_RETRACES)
            return
        cpu.retrace_pending = False
        self.ticks_failed = False
        self._retrace_tasks()
        self._time_tasks()
        self._deferred_tasks()
        self._via_interrupt()

    def wants_ticks(self) -> bool:
        """Whether something is to happen at the ticks to come: a VBL task
        counting down, a Time Manager task or a deferred task waiting, or a
        sound to make or play.  With its machine held."""
        cpu = self.cpu
        return not (self.ticks_failed or self.process.events.killed) and bool(
            cpu.vbl_counting() or self.time_tasks.due or cpu.deferred_waiting()
            or cpu.sound_waiting())

    def idle_tick(self) -> None:
        """A tick while the application waits for the host (for an event, a
        menu, the end of a Delay), from the machine's ticker: its VBL
        tasks, its sound, its Time Manager tasks and its deferred tasks, as
        the vertical retrace interrupt would have them go on.  Not the VIA's
        interrupt, which mustn't come in the middle of a trap.  If something
        goes wrong, the ticks stop until the application's own code has
        them again, to meet it there."""
        cpu = self.cpu
        if cpu.sr & m68k.INTERRUPT_MASK:
            self.catch_up = min(self.catch_up, MAX_RETRACES)
            return
        ctx = cpu.context()
        try:
            self._retrace_tasks()
            self._time_tasks()
            self._deferred_tasks()
        except Exception:  # noqa: BLE001 - a system error, or the process killed
            log.debug("%s: a tick while it waited failed", self.process.name, exc_info=True)
            cpu.restore(ctx)
            self.ticks_failed = True
            self.catch_up = min(self.catch_up, MAX_RETRACES)

    def _retrace_tasks(self) -> None:
        """The VBL tasks and the sound of each tick since the last.  A task
        that hasn't set its vblCount again once it's run is taken out of the
        queue, as Inside Macintosh II-350 has it: Dragonworld's music, which
        it installs again when it plays the next tune.

        Ticks the host was late for are made up for, a third of a second
        of them at most.  After a break, only a few: when the application had
        interrupts masked, a tick while it waited failed, or another
        application had its turn.  After a tick that left nothing counting
        down, just the one, so that a task installed since counts from
        then.  A longer gap counts once."""
        cpu, mem = self.cpu, self.mem
        now = tick_count()
        ticks = now - self.retrace_tick
        if ticks <= 0:
            return
        if ticks > self.catch_up:
            ticks = 1
        self.retrace_tick = now
        if mem.r32(lowmem.VBLQueue + 2) or cpu.sound_waiting():
            ctx = cpu.context()
            self._interrupt_frame()
            sound, speaker = self.machine.sound, self.machine.speaker
            manager = self.machine.sound_manager
            for _ in range(ticks):
                task = mem.r32(lowmem.VBLQueue + 2) & mem.system.addr_mask
                for _ in range(m68k.MAX_VBL_TASKS):     # a queue that goes round, once
                    if not task:
                        break
                    after = mem.r32(task) & mem.system.addr_mask    # before the task can remove itself
                    count = mem.r16s(task + VBL_COUNT)
                    if count > 0:
                        mem.w16(task + VBL_COUNT, count - 1)
                        if count == 1:
                            cpu.set_a(0, task)
                            self._jsr(mem.r32(task + VBL_ADDR))
                            if not mem.r16(task + VBL_COUNT):
                                glue_os.Dequeue(self, task, lowmem.VBLQueue)
                    task = after
                sound.retrace(self)
                manager.retrace(self)
                speaker.retrace()
            cpu.restore(ctx)
        self.catch_up = MAX_LATE if cpu.vbl_counting() or cpu.sound_waiting() else 1

    def _via_interrupt(self) -> None:
        """Interrupt the application for the VIA, between two instructions,
        if it's flagged something the application handles itself: with its
        own handler, anything enabled, and with its own routine in Lvl1DT,
        that routine's flag.  The ROM's handler calls the routine for the
        lowest flag set, which may be one of the System's first."""
        cpu = self.cpu
        if cpu.sr & m68k.INTERRUPT_MASK:
            return
        pending = self.machine.via.pending()
        if pending and (cpu.own_interrupt_handler()
                        or pending & cpu.own_level_1_routines()):
            cpu.take_exception(m68k.LEVEL_1, cpu.pc)
            cpu.sr = cpu.sr & ~m68k.INTERRUPT_MASK | m68k.VBL_LEVEL

    def _interrupt_frame(self) -> None:
        """What the interrupt stacks under the program's stack before its
        handler calls a task: the processor's exception frame, and the
        registers the handler keeps, A0-A3 and D0-D3 (Inside Macintosh
        II-200), with the interrupt's priority in SR.  Factory's sound
        callback leaves a word above its parameters, as if it were a
        function, which mustn't land in the interrupted code's frame."""
        cpu = self.cpu
        cpu.sp = cpu.sp - (6 if cpu.short_frames else 8) - INTERRUPT_REGISTERS
        cpu.sr = cpu.sr | m68k.VBL_LEVEL

    def _time_tasks(self) -> None:
        """Run the application's Time Manager tasks that are due, with A1
        pointing at each, in the order they're due: a task that primes
        itself again for a moment later may run again now."""
        tasks = self.time_tasks
        if not tasks.due:
            return
        cpu, mem = self.cpu, self.mem
        now = tasks.clock()
        ctx = cpu.context()
        self._interrupt_frame()
        for _ in range(MAX_TIME_TASKS):
            due = tasks.next_due(now)
            if due is None:
                break
            task, tasks.running = due
            q = task + timemgr.QTYPE
            mem.w16(q, mem.r16(q) & ~timemgr.ACTIVE)
            routine = mem.r32(task + timemgr.TM_ADDR)
            try:
                if routine:                 # none: the task only measures time
                    cpu.set_a(1, task)
                    self._jsr(routine)
            finally:
                tasks.running = None
        cpu.restore(ctx)

    def _deferred_tasks(self) -> None:
        """Run the application's deferred tasks, each taken out of the
        queue before it runs, with dtParm in A1 (Inside Macintosh:
        Processes).  At the end of a tick's tasks, as an interrupt's
        handler runs them, and at the interrupt's priority still: one
        queued by a task that ran this tick runs now, one queued by the
        application itself at the next tick."""
        mem = self.mem
        if not mem.r32(lowmem.DTQueue + 2) & mem.system.addr_mask:
            return
        cpu = self.cpu
        ctx = cpu.context()
        self._interrupt_frame()
        for _ in range(MAX_DEFERRED_TASKS):
            task = mem.r32(lowmem.DTQueue + 2) & mem.system.addr_mask
            if not task:
                break
            glue_os.Dequeue(self, task, lowmem.DTQueue)
            routine = mem.r32(task + DT_ADDR) & mem.system.addr_mask
            if routine:
                cpu.set_a(1, mem.r32(task + DT_PARM))
                self._jsr(routine)
        cpu.restore(ctx)

    def _own_a_line(self, pc: int) -> bool:
        """Whether the trap at pc goes to a handler the program has put in
        vector $28, as every A-line instruction goes to the vector's.  Not
        one in the stubs or the ROM's MountVol: called through its address,
        a trap's routine is the ROM's code itself."""
        if self.mem.r32(4 * m68k.LINE_A) & self.mem.system.addr_mask == self.machine.a_line:
            return False
        start, end = self.machine.stubs
        pc &= self.mem.system.addr_mask
        return not start <= pc < end and pc != self.machine.mount_vol

    def _trap(self, word: int, pc: int) -> None:
        cpu = self.cpu
        part = self.process.partition
        if part.left_a5 and cpu.a(5) & self.mem.system.addr_mask == part.left_a5:
            # A5 is back where it was before an InitGraf at another A5, and
            # QuickDraw's globals with it: DD Expand finds the screen's
            # bounds through an InitGraf on a stack frame, then goes back.
            part.a5, part.left_a5 = part.left_a5, 0
        self.history.append((word, pc))
        self.screen.tick()          # show what it's written in the screen buffer
        if self.trace:
            log.debug("%s: %s at $%06X", self.process.name, dispatch.describe(self, word), pc)
        start, end = self.machine.stubs
        via_stub = start <= (pc & self.mem.system.addr_mask) < end
        if pc & self.mem.system.addr_mask == self.machine.return_stub:
            raise SystemError68k(3, "returned through a callback that wasn't called")
        if pc & self.mem.system.addr_mask == self.machine.load_trap:
            cpu.pc = pc + 2             # no debugger to stop in: on to the routine
            return
        key = dispatch.trap_key(word)
        if ((word & 0x0A00) == 0x0A00 and self.system.model.toolbox_traps == 512
                and not dispatch.assigned(key) and key not in self.patches):
            # A word with bit 9 set that names nothing: a program for the
            # 128K ROM, whose dispatcher ignored bit 9, may leave it set
            # (Crozzle 0.8 calls NewWindow as $AB13).
            word &= ~0x0200
            key = dispatch.trap_key(word)
        if dispatch.is_toolbox(word):
            patch = None if via_stub else self.patches.get(key)
            if patch is not None:
                if not word & 0x400:
                    cpu.push32(pc + 2)
                cpu.pc = patch
                return
            cpu.pc = cpu.pop32() if word & 0x400 else pc + 2
            self._dispatch(word, pc)
            return
        if via_stub:
            # Called through its address: the flags are in D1, if anywhere.
            d1 = cpu.d(1) & 0xFFFF
            word = d1 if (d1 & 0xF800) == 0xA000 and dispatch.trap_key(d1) == key else key
        else:
            patch = self.patches.get(key)
            if patch is not None:
                self._call_os_patch(patch, word, pc + 2)
                return
        cpu.pc = pc + 2
        self._dispatch(word, pc)
        cpu.set_cc_from_word(cpu.d(0))

    def _vectored_trap(self) -> None:
        """The ROM's A-line dispatcher, jumped to as through vector $28,
        with the trap's address stacked: the trap is done as when the core
        stops at it."""
        cpu = self.cpu
        cpu.return_from_exception()
        pc = cpu.pc
        self._trap(self.mem.r16(pc & self.mem.system.addr_mask), pc)

    def _dispatch(self, word: int, pc: int) -> None:
        key = dispatch.trap_key(word)
        poll = key in POLLS
        if poll:
            self._polling()
        try:
            dispatch.dispatch(self, word, pc)
        except dispatch.UnknownTrap as e:
            raise SystemError68k(12, str(e), pc & self.mem.system.addr_mask) from None
        except BusError as e:           # it passed the trap a bad pointer or handle
            raise SystemError68k(1, f"in {dispatch.trap_name(word)} at {self.mem.addr_str(pc)}: "
                                    f"{e}", pc & self.mem.system.addr_mask) from None
        if key in FRAMES_AT:
            wdefs.refresh(self)
        self.polled = self.cpu_time() if poll or key in KEEPS_POLLING else -SPIN_GAP

    def _polling(self) -> None:
        """One of POLLS: if the program has done nothing but ask for
        SPIN_BUDGET, it waits for the next tick or for input.  Not at a
        tick, with interrupts masked, or paced, where the time it takes
        is the point.  The ticker's calls, made while the thread waits,
        take no time from it."""
        now = self.cpu_time()
        if now - self.polled >= SPIN_GAP:
            self.spun_from = now
            return
        if now - self.spun_from < SPIN_BUDGET:
            return
        self._wait_for_tick()
        self.spun_from = self.cpu_time()

    def _wait_for_tick(self) -> None:
        """The program has nothing to do until the next tick, or input:
        the host waits for them, and other applications may run.  Not at
        a tick, with interrupts masked, or paced, where it goes on."""
        if (self.holding and not self.machine.ticking() and not self.cpu.rate
                and not self.cpu.sr & m68k.INTERRUPT_MASK):
            with self.yielding():
                self.process.events.nap(to_next_tick() / 60 + 2 * m68k.SETTLE)

    def _call_os_patch(self, addr: int, word: int, ret: int) -> None:
        """What the OS trap dispatcher does for a patched trap, on the stack
        as a Mac's does it, for patches that go back to the caller
        themselves, as Microsoft Word's does: stack the return address, SR
        and the trap word, then A2, D2, D1, A1, and A0 unless bit 8 says
        it's a result; and call the patch with the trap word in D1, its
        place in the table in D2 and its address in A2.  When it returns,
        _os_patch_returned takes them off again."""
        cpu = self.cpu
        cpu.push32(ret)
        cpu.push16(cpu.sr)
        cpu.push16(word)
        for value in (cpu.a(2), cpu.d(2), cpu.d(1), cpu.a(1)):
            cpu.push32(value)
        keeps_a0 = not word & 0x100
        if keeps_a0:
            cpu.push32(cpu.a(0))
        cpu.set_d(1, (cpu.d(1) & 0xFFFF0000) | word)
        cpu.set_d(2, (word & 0xFF) << 2)
        cpu.set_a(2, addr)
        cpu.push32(self.machine.os_patch_returns[0 if keeps_a0 else 1])
        cpu.pc = addr

    def _os_patch_returned(self, keeps_a0: bool) -> None:
        cpu = self.cpu
        if keeps_a0:
            cpu.set_a(0, cpu.pop32())
        cpu.set_a(1, cpu.pop32())
        cpu.set_d(1, cpu.pop32())
        cpu.set_d(2, cpu.pop32())
        cpu.set_a(2, cpu.pop32())
        cpu.sp += 4                     # the trap word and SR
        cpu.pc = cpu.pop32()
        cpu.set_cc_from_word(cpu.d(0))

    def _jsr(self, addr: int) -> None:
        cpu = self.cpu
        pc = cpu.pc
        cpu.push32(self.machine.return_stub)
        self.run_until(self.machine.return_stub, addr)
        cpu.pc = pc

    def call(self, addr: int, args: tuple = (), result: str = "") -> int:
        """Call a Pascal routine in 68k code: args are (code, value) pairs
        as in trapdefs.  Registers come back as they were."""
        cpu, mem = self.cpu, self.mem
        ctx = cpu.context()
        if result:
            cpu.sp = cpu.sp - dispatch.SIZES[result]
        for code, value in args:
            v = dispatch._to_long(value, code)
            if dispatch.SIZES[code] == 2:
                cpu.push16(v << 8 if code in "bB" else v)
            else:
                cpu.push32(v)
        self._jsr(addr)
        value = 0
        if result:
            value = dispatch.pop_args(mem, cpu.sp, result)[0][0]
        cpu.restore(ctx)
        return value

    def on_own_thread(self) -> bool:
        """Whether the calling host thread is running the application's
        code, with the processor held: its own thread, or a Thread Manager
        thread's (glue_threads) while that has the processor."""
        return self.holding and threading.get_ident() == self.thread_id

    def call_grow_zone(self, proc: int, needed: int, protected: int) -> int:
        """Call a zone's grow-zone function, FUNCTION MyGrowZone(cbNeeded:
        Size): LongInt, for a block of needed bytes, GZRootHnd holding the
        handle it mustn't move or purge, which GZSaveHnd returns (Inside
        Macintosh: Memory): what it says it freed.  Only from the host thread
        running its code (on_own_thread), and not at interrupt time; 0
        otherwise."""
        if not self.on_own_thread() or self.cpu.sr & m68k.INTERRUPT_MASK:
            return 0
        mem = self.mem
        saved = mem.r32(lowmem.GZRootHnd)
        mem.w32(lowmem.GZRootHnd, protected)
        try:
            return self.call(proc, (("l", needed),), "l")
        finally:
            mem.w32(lowmem.GZRootHnd, saved)

    def call_with(self, addr: int, **regs: int) -> int:
        """Call 68k code that takes its arguments in registers, a0=pb and
        so on, as a driver's routines do: what it leaves in D0.  The other
        registers come back as they were."""
        return self.call_for(addr, ("d0",), **regs)[0]

    def call_for(self, addr: int, out: tuple[str, ...], **regs: int) -> tuple[int, ...]:
        """Call it so, for what it leaves in the registers out, "d2" and
        so on, as the ROM's RdAddr leaves the sector it found."""
        cpu = self.cpu
        ctx = cpu.context()
        for reg, value in regs.items():
            (cpu.set_a if reg[0] == "a" else cpu.set_d)(int(reg[1]), value)
        self._jsr(addr)
        found = tuple((cpu.a if reg[0] == "a" else cpu.d)(int(reg[1])) for reg in out)
        cpu.restore(ctx)
        return found

    def _exception(self, vector: int, pc: int) -> None:
        cpu = self.cpu
        if vector == m68k.RTE:
            cpu.return_from_exception()
            return
        if vector == m68k.HALT:             # STOP: nothing to do until an interrupt
            cpu.pc = pc + 4
            return
        if pc & self.mem.system.addr_mask == self.machine.return_stub:
            raise SystemError68k(3, "returned through a callback that wasn't called")
        returns = self.machine.os_patch_returns
        if pc & self.mem.system.addr_mask in returns:
            self._os_patch_returned(pc & self.mem.system.addr_mask == returns[0])
            return
        if pc & self.mem.system.addr_mask == self.machine.io_done:
            glue_devices.io_done(self)
            return
        if pc & self.mem.system.addr_mask == self.machine.rd_data:
            glue_sony.rd_data(self)
            return
        if pc & self.mem.system.addr_mask == self.machine.rd_addr:
            glue_sony.rd_addr(self)
            return
        if pc & self.mem.system.addr_mask == self.machine.adr_disk:
            glue_sony.adr_disk(self)
            return
        n, odd = divmod((pc & self.mem.system.addr_mask) - self.machine.sony_routines, 2)
        if 0 <= n < len(glue_sony.ROUTINES) and not odd:
            glue_sony.ROUTINES[n][1](self)
            return
        if pc & self.mem.system.addr_mask == self.machine.te_do_text:
            glue_textedit.do_text(self)
            return
        if pc & self.mem.system.addr_mask == self.machine.crsr_task:
            # Called by a program that has moved the mouse by RawMouse
            # and set CrsrNew, as the clock calls it each tick.
            self.system.input.tick()
            cpu.pc = cpu.pop32()
            return
        if pc & self.mem.system.addr_mask == self.machine.a_line:
            self._vectored_trap()
            return
        if pc & self.mem.system.addr_mask in self.machine.tick_waits:
            self._wait_for_tick()
            cpu.pc = pc + 2
            return
        handler = self.mem.r32(4 * vector) if vector < 256 else 0
        if handler & self.mem.system.addr_mask not in (0, self.machine.bomb_stub):
            if (m68k.TRAP_0 <= vector < m68k.TRAP_0 + 16
                    and self.mem.r16(pc) & 0xFFF0 == TRAP_WORD):
                pc += 2                     # the core stops at a TRAP; it returns past it
            if vector == m68k.CHK:
                pc -= 2                     # and a word into a CHK, but at a divide or TRAPV
            if vector in (m68k.ZERO_DIVIDE, m68k.CHK, m68k.TRAPV):
                cpu.take_exception(vector, after_instruction(self.mem, pc), pc)
            elif vector == m68k.TRACE:      # after the instruction traced
                cpu.take_exception(vector, pc, cpu.traced)
            else:
                cpu.take_exception(vector, pc, pc if vector in FORMAT_2 else None)
            return
        error_id = VECTOR_ERRORS.get(vector, 11)
        raise SystemError68k(error_id, f"at {self.mem.addr_str(pc)}", pc & self.mem.system.addr_mask)

    # =====================================================================
    # For the glue
    # =====================================================================
    @contextmanager
    def yielding(self):
        """Let other applications run while this one waits, and its ticks
        go on.  A task that waits at a tick keeps the processor, as with
        interrupts masked: the application's own code mustn't run under it."""
        if self.machine.ticking():
            yield
            return
        self.machine.idle(self)
        self.holding = False
        self.machine.release()
        ok = False
        try:
            yield
            ok = True
        finally:
            if ok:
                self.machine.acquire(self)
                self.holding = True

    @contextmanager
    def resumed(self):
        """Inside yielding: hold the machine again to run 68k code.  If
        that fails, it stays held, as after any other trap.  The ticker,
        running the application's code at a tick, has it already."""
        if self.machine.ticking():
            yield
            return
        self.machine.acquire(self)
        self.holding = True
        yield
        self.holding = False
        self.machine.release()

    def unimplemented(self, name: str, word: int) -> None:
        self.unimplemented_calls[name] += 1
        self.note_once(name, "%s (trap $%04X) isn't implemented; skipped", name, word)

    def asked_missing(self, name: str) -> None:
        self.missing_asked[name] += 1

    def note_once(self, key: str, message: str, *args) -> None:
        """Log message about the application the first time key comes up."""
        if key not in self.reported:
            self.reported.add(key)
            log.warning("%s: " + message, self.process.name, *args)

    def system_error(self, error_id: int, detail: str = ""):
        raise SystemError68k(error_id, detail)

    @property
    def mem_err(self) -> int:
        return self.process.world.r16(lowmem.MemErr)

    def set_mem_err(self, err: int) -> None:
        self.process.world.w16(lowmem.MemErr, err)

    # Arguments: from pointers to Python values and back.
    def rect(self, p: int) -> Rect:
        return self.mem.r_rect(p)

    def set_rect(self, p: int, r: Rect) -> None:
        if p:
            self.mem.w_rect(p, r)

    def point(self, p: int) -> Point:
        return self.mem.r_point(p)

    def set_point(self, p: int, pt: Point) -> None:
        if p:
            self.mem.w_point(p, pt)

    def str255(self, p: int) -> str:
        return from_mac(self.mem.r_pstr(p)) if p else ""

    def pstr(self, p: int) -> bytes:
        return self.mem.r_pstr(p) if p else b""

    def set_str255(self, p: int, s: str | bytes) -> None:
        if p:
            self.mem.w_pstr(p, to_mac(s) if isinstance(s, str) else s)

    def w16(self, p: int, v: int) -> None:
        if p:
            self.mem.w16(p, v)

    def w32(self, p: int, v: int) -> None:
        if p:
            self.mem.w32(p, v)

    @staticmethod
    def ostype(v: int) -> bytes:
        return (v & 0xFFFFFFFF).to_bytes(4, "big")

    def write_event(self, p: int, ev) -> None:
        """An EventRecord: what, message, when, where, modifiers."""
        if not p:
            return
        mem = self.mem
        if ev.qel:                  # PPostEvent's: as the program left its queue entry
            ev.message, ev.when, ev.modifiers = mem.r32(ev.qel + 8), mem.r32(ev.qel + 12), \
                mem.r16(ev.qel + 20)
            ev.where = mem.r_point(ev.qel + 16)
        msg = ev.message
        msg = msg.addr if hasattr(msg, "addr") else int(msg or 0)
        mem.w16(p, ev.what)
        mem.w32(p + 2, msg)
        mem.w32(p + 6, ev.when)
        mem.w_point(p + 10, ev.where)
        mem.w16(p + 14, ev.modifiers)

    def window(self, p: int) -> WindowRecord | None:
        """The window a WindowPtr points at, None for anything else."""
        port = self.system.port_at(p) if p else None
        return port if isinstance(port, WindowRecord) else None

    def menu(self, h: int) -> MenuInfo | None:
        return MenuInfo(self.system, h) if h and self.mem.r32(h) & self.mem.system.addr_mask else None

    def take_mouse_up(self) -> None:
        """The mouse-up that ended a press the Toolbox tracked, out of the
        queue: After Dark 3.0 takes any event after its Demo button's
        click for the user coming back, and stops the module at once."""
        with self.yielding() if self.holding else nullcontext():
            self.tb.take_mouse_up()

    def sleep(self, ticks: int) -> None:
        """Wait, letting the other applications run (Delay)."""
        events = self.process.events
        with self.yielding():
            deadline = time.monotonic() + max(ticks, 0) / 60
            while not events.killed:
                left = deadline - time.monotonic()
                if left <= 0:
                    return
                time.sleep(min(left, 0.05))
        raise ProcessKilled()


# Register the trap implementations.
from . import (  # noqa: E402,F401
    glue_adb, glue_aevents, glue_aliases, glue_color, glue_common, glue_components, glue_controls,
    glue_cport, glue_desk, glue_devices, glue_dialogs, glue_editions, glue_events, glue_files,
    glue_fonts, glue_gworld, glue_help, glue_icons, glue_intl, glue_lists, glue_menucolors,
    glue_menus, glue_os, glue_palette, glue_picker, glue_pict, glue_pictinfo, glue_ppc, glue_print,
    glue_process, glue_quickdraw, glue_resources, glue_sane, glue_scrap, glue_script, glue_scsi,
    glue_slots, glue_sony, glue_sound, glue_stdfile, glue_textedit, glue_threads, glue_utilities,
    glue_video, glue_windows)
from . import controlpanel  # noqa: E402 - the host for control panels, which calls the glue
