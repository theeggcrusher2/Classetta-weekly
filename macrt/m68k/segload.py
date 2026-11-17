"""
The Segment Loader: an application's code is in CODE resources, and the
jump table ties them together.

CODE 0 describes the A5 world (Inside Macintosh II-61):

    long  bytes above A5: application parameters plus the jump table
    long  bytes below A5: the application's globals, QuickDraw's among them
    long  size of the jump table
    long  offset of the jump table from A5 (32)
          the jump table

Each jump table entry is 8 bytes.  While its segment isn't in memory it
is the routine's offset in the segment, then MOVE.W #segment,-(SP) and
_LoadSeg; code calls an entry by JSR to its second word.  _LoadSeg reads
CODE n, locks it, rewrites every entry of that segment as the segment
number and a JMP to the routine, and goes back to the entry's second word
(Inside Macintosh II, The Segment Loader).  A segment that isn't locked,
read in without the resLocked attribute or in memory unlocked, it first
moves toward the top of the heap with MoveHHi, out of the way of the
blocks allocated low (Inside Macintosh IV, The Segment Loader): HyperCard
2.0 grows a pointer it made just before it loads a segment.  _UnloadSeg
puts the entries back and makes the segment purgeable.  _LoadSeg reads
the segment with the GetResource trap, so a program that has patched
GetResource sees it asked for: FullPaint's and Q-Sheet's patches decrypt
their segments as they're read.

The jump table _LoadSeg rewrites is the one A5 points at, CurJTOffset
above it.  That's the application's, but code with an A5 world of its own
can have one too: Cosmic Osmo's XCMD runs MacroMind Player's segments, from
the Player's own file, with A5 pointing at a world it made for them.

CODE n starts with the offset of its first entry in the jump table and
the number of entries; its code follows.  Segments built for the "far"
model of MPW 3.2 and later start with $FFFF and a 40-byte header instead,
which this loader doesn't handle yet.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from ..macfile import MacFile
from ..resfork import (
    COMPRESSED_TAG, DecompressError, InvalidForkError, decompress, open_fork, resCompressed,
)
from ..toolbox import lowmem
from ..toolbox.memmgr import LOCKED

if TYPE_CHECKING:
    from .runtime import Runner

MOVE_W_IMM_PUSH = 0x3F3C        # MOVE.W #imm,-(SP)
LOAD_SEG = 0xA9F0
GET_RESOURCE = 0xA9A0
CODE = struct.unpack(">I", b"CODE")[0]
JMP_ABS_L = 0x4EF9
ENTRY = 8
dsLoadErr = 15                  # system error: a segment couldn't be loaded


def _code_id(seg: int) -> int:
    """The CODE resource a segment number names: the number is an INTEGER,
    so $FFFF is CODE -1 (GraphicConverter's first entry loads it)."""
    return seg - 0x10000 if seg & 0x8000 else seg


class SegmentError(ValueError):
    """An application whose code the Segment Loader can't use."""


@dataclass(frozen=True)
class Layout:
    """What CODE 0 says about an application's A5 world."""
    above_a5: int
    below_a5: int
    jt_offset: int
    jump_table: bytes

    @property
    def entries(self) -> int:
        return len(self.jump_table) // ENTRY


def code0(app: MacFile, expand: Callable[[bytes], bytes | None] | None = None) -> Layout | None:
    """The application's CODE 0, None if it has no 68k code.  One
    compressed in a way rsrcfork can't expand is expanded by expand, if
    given: it runs the 'dcmp' on the 68k."""
    try:
        fm = open_fork(app.rsrc)
    except InvalidForkError:
        return None
    entry = fm.find(b"CODE", 0)
    if entry is None:
        return None
    try:
        data = fm.raw(entry)
    except InvalidForkError as e:
        raise SegmentError(f"CODE 0 can't be read ({e})") from e
    # It can be compressed, as the Resource Manager reads it (Script Editor's).
    if entry.attrs & resCompressed and data[:4] == COMPRESSED_TAG:
        try:
            data = decompress(data)
        except DecompressError as e:
            expanded = expand(data) if expand is not None else None
            if expanded is None:
                raise SegmentError(f"CODE 0 is compressed in a way macrt can't expand ({e})") \
                    from e
            data = expanded
    if len(data) < 16:
        raise SegmentError("CODE 0 is too short to describe an A5 world")
    above, below, jt_size, jt_offset = struct.unpack_from(">IIII", data)
    if jt_size < ENTRY or len(data) < 16 + ENTRY or jt_offset + jt_size > above \
            or above > 0x800000 or below > 0x800000:
        raise SegmentError(f"CODE 0 doesn't add up (above A5 {above}, below A5 {below}, "
                           f"jump table {jt_size} bytes at {jt_offset})")
    # A self-expanding application (one compressed by a tool such as
    # AutoDoubler) has a CODE 0 shorter than its jump table: the first
    # entry unpacks the rest.  The Segment Loader copies the full size
    # regardless; past the resource there's nothing, so zeros.
    table = data[16:16 + jt_size].ljust(jt_size, b"\0")
    if table[:8] == bytes.fromhex("0000FFFF00000000"):
        raise SegmentError("it was built for MPW's far code model (32-bit jump table), "
                           "which macrt's Segment Loader doesn't handle yet")
    return Layout(above, below, jt_offset, table)


def has_code(app: MacFile) -> bool:
    try:
        fm = open_fork(app.rsrc)
    except InvalidForkError:
        return False
    return fm.find(b"CODE", 0) is not None


def has_powerpc_code(app: MacFile) -> bool:
    """Whether it has a 'cfrg' resource, naming PowerPC code fragments."""
    try:
        return open_fork(app.rsrc).find(b"cfrg", 0) is not None
    except InvalidForkError:
        return False


def why_no_code(app: MacFile) -> str:
    """Why an application without CODE 0 has none, for its error message."""
    if has_powerpc_code(app):
        return "it's a PowerPC application"
    try:
        if open_fork(app.data).find(b"CODE", 0) is not None:
            # Copied by something that didn't keep the forks apart.
            return "no CODE resources: its resource fork is in its data fork"
    except InvalidForkError:
        pass
    return "no CODE resources"


class SegmentLoader:
    def __init__(self, t: Runner, layout: Layout):
        self.t = t
        self.layout = layout
        self.a5 = t.process.partition.a5
        self.table = self.a5 + layout.jt_offset
        self.loaded: dict[int, tuple[int, int, int]] = {}  # segment -> (handle, code, size)
        # Segments loaded into other A5 worlds' jump tables:
        # (table, segment) -> (handle, code, size)
        self.elsewhere: dict[tuple[int, int], tuple[int, int, int]] = {}

    def install(self) -> int:
        """Copy the jump table into the A5 world; return where execution
        starts (the first entry)."""
        mem = self.t.mem
        mem.write(self.table, self.layout.jump_table)
        world = self.t.process.world
        world.w16(lowmem.CurJTOffset, self.layout.jt_offset)
        return self.table + 2

    def _entry_segment(self, e: int) -> int | None:
        mem = self.t.mem
        if mem.r16(e + 2) == MOVE_W_IMM_PUSH and mem.r16(e + 6) == LOAD_SEG:
            return mem.r16(e + 4)
        if mem.r16(e + 2) == JMP_ABS_L:
            return mem.r16(e)
        return None

    def load(self, seg: int) -> None:
        """LoadSeg: read the segment, lock it, and point its entries at it."""
        if seg in self.loaded:
            h, code, _ = self.loaded[seg]
            if self.t.mem.r32(h) & self.t.mem.system.addr_mask == code:
                # still where it was, but perhaps unloaded since: locked
                # again all the same, for a program can unload the very
                # segment it's running in and load it again (Habadex)
                code = self._hold(h).deref(h)
                self.loaded[seg] = (h, code, self.loaded[seg][2])
                self._patch(seg, code)
                return
        t = self.t
        h = self._get_code(seg)
        if not h:
            t.system_error(dsLoadErr, f"CODE {_code_id(seg)} isn't in the application")
        heap = self._hold(h)
        code = heap.deref(h)
        size = heap.handle_size(h)
        if size >= 2 and t.mem.r16(code) == 0xFFFF:
            t.system_error(dsLoadErr, f"CODE {_code_id(seg)} uses the far code model, "
                                      "which the Segment Loader doesn't handle yet")
        self.loaded[seg] = (h, code, size)
        self._patch(seg, code)

    def _hold(self, h: int):
        """Move a segment's handle high if it isn't locked, lock it and make
        it unpurgeable, as LoadSeg does; return its heap."""
        heap = self.t.system.heap_of(h)
        if not self.t.mem.r8(h) & LOCKED:
            heap.move_high(h)
        heap.lock(h)
        heap.set_purgeable(h, False)
        return heap

    def _get_code(self, seg: int) -> int:
        """GetResource("CODE", seg), through the program's own GetResource
        if it has patched it, and read in though the program has turned
        ResLoad off: LoadSeg reads in a segment that isn't in memory, and
        SoundApp calls a routine in another segment between its
        SetResLoad(FALSE) and SetResLoad(TRUE)."""
        t = self.t
        resources = t.process.resources
        patch = t.patches.get(GET_RESOURCE)
        if patch is not None:
            h = t.call(patch, (("l", CODE), ("h", seg)), "p") & t.mem.system.addr_mask
        else:
            h = resources.GetResource("CODE", _code_id(seg))
        if h and not t.mem.r32(h) & t.mem.system.addr_mask:
            resources.LoadResource(h)
        return h

    def _load_elsewhere(self, seg: int, table: int) -> None:
        """LoadSeg into another A5 world's jump table."""
        held = self.elsewhere.get((table, seg))
        if held is not None and self.t.mem.r32(held[0]) & self.t.mem.system.addr_mask == held[1]:
            h, _, size = held
            code = self._hold(h).deref(h)
            self.elsewhere[(table, seg)] = (h, code, size)
        else:
            h = self._get_code(seg)
            if not h:
                self.t.system_error(dsLoadErr, f"CODE {_code_id(seg)} isn't in any open file")
            heap = self._hold(h)
            code = heap.deref(h)
            self.elsewhere[(table, seg)] = (h, code, heap.handle_size(h))
        self._patch(seg, code, table)

    def _patch(self, seg: int, code: int, table: int | None = None) -> None:
        mem = self.t.mem
        table = self.table if table is None else table
        first, count = mem.r16(code), mem.r16(code + 2)
        for i in range(count):
            e = table + first + ENTRY * i
            if mem.r16(e + 2) == MOVE_W_IMM_PUSH and mem.r16(e + 4) == seg:
                offset = mem.r16(e)
                mem.w16(e, seg)
                mem.w16(e + 2, JMP_ABS_L)
                mem.w32(e + 4, code + 4 + offset)

    def unload(self, routine: int) -> None:
        """UnloadSeg: the segment holding routine goes back to purgeable,
        its entries back to _LoadSeg."""
        seg = self._segment_of(routine & self.t.mem.system.addr_mask)
        table = self.table
        if seg is not None:
            h, code, _ = self.loaded[seg]
        else:
            held = next(((key, v) for key, v in self.elsewhere.items()
                         if v[1] <= routine & self.t.mem.system.addr_mask < v[1] + v[2]), None)
            if held is None:
                return
            (table, seg), (h, code, _) = held
        mem = self.t.mem
        first, count = mem.r16(code), mem.r16(code + 2)
        for i in range(count):
            e = table + first + ENTRY * i
            if mem.r16(e + 2) == JMP_ABS_L and mem.r16(e) == seg:
                offset = (mem.r32(e + 4) & mem.system.addr_mask) - (code + 4)
                mem.w16(e, offset)
                mem.w16(e + 2, MOVE_W_IMM_PUSH)
                mem.w16(e + 4, seg)
                mem.w16(e + 6, LOAD_SEG)
        heap = self.t.system.heap_of(h)
        heap.unlock(h)
        heap.set_purgeable(h, True)

    def _segment_of(self, routine: int) -> int | None:
        """The loaded segment routine is in: an address in its code, or the
        JMP of one of its jump table entries, which is what Pascal's @ gives
        for a routine in another segment (Microsoft BASIC 2 unloads its
        segments by those)."""
        mem = self.t.mem
        entry = routine - 2 - self.table
        if (0 <= entry < len(self.layout.jump_table) and entry % ENTRY == 0
                and mem.r16(routine) == JMP_ABS_L):
            seg = mem.r16(routine - 2)
            return seg if seg in self.loaded else None
        for seg, (_, code, size) in self.loaded.items():
            if code <= routine < code + size:
                return seg
        return None

    def trap_load_seg(self, return_pc: int) -> None:
        """_LoadSeg, from a jump table entry or called directly."""
        cpu = self.t.cpu
        seg = cpu.pop16()
        # the trap is the entry's last word; a program can call an entry
        # through an address with flags in its high byte, which a 68000
        # ignores (Macintosh Pascal does, from a master pointer)
        e = (return_pc & cpu.mask) - 8
        ours = self.table <= e < self.table + len(self.layout.jump_table)
        a5 = cpu.a(5) & cpu.mask
        if not ours and a5 != self.a5 and self._entry_segment(e) == seg:
            self._load_elsewhere(seg, a5 + self.t.mem.r16(lowmem.CurJTOffset))
            if self.t.mem.r16(e + 2) == JMP_ABS_L:
                self._go_on(e)
            return
        self.load(seg)
        # _LoadSeg goes back 6 bytes before where it was called from: for
        # an entry that's the MOVE.W just rewritten as a JMP to the routine.
        # A patch on _LoadSeg that calls the original pushes an address 6
        # past where it wants to go on (Easy View's does)
        if ours and self._entry_segment(e) == seg:
            if self.t.mem.r16(e + 2) != JMP_ABS_L:
                self._not_in_segment(seg)
        elif self._entry_segment(e) == seg and self.t.mem.r16(e + 2) != JMP_ABS_L:
            return                  # an entry it didn't rewrite: going back would load forever
        self._go_on(e)

    def _go_on(self, e: int) -> None:
        """Go on at entry e's JMP.  With LoadTrap set, by way of _Debugger,
        the routine's address on the stack, for a debugger to stop there or
        go on to it with an RTS (Inside Macintosh: Processes, Segment
        Manager).  Crystal Paint DEMO's patch on _LoadSeg runs the original
        on a stack of its own and takes the _Debugger, in its own A-line
        handler, to get back to the application's."""
        mem = self.t.mem
        if mem.r8(lowmem.LoadTrap):
            self.t.cpu.push32(mem.r32(e + 4) if mem.r16(e + 2) == JMP_ABS_L else e + 2)
            self.t.cpu.pc = self.t.machine.load_trap
        else:
            self.t.cpu.pc = e + 2

    def _not_in_segment(self, seg: int) -> None:
        """The entry called is still _LoadSeg after loading its segment:
        going on would load it forever."""
        h, code, size = self.loaded[seg]
        if self.t.mem.read(code, 4) == b"SALN":
            self.t.system_error(dsLoadErr, f"CODE {_code_id(seg)} is still compressed by "
                                           "AutoDoubler: the application's decompressor "
                                           "didn't expand it")
        self.t.system_error(dsLoadErr, f"CODE {_code_id(seg)} doesn't have the entry the "
                                       "jump table points to")
