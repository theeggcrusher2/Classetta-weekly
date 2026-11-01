"""
OS traps: the Memory Manager, the trap table, Gestalt and SysEnvirons,
OS events, time, and the Segment Loader.  Names and arguments are as in
Inside Macintosh; see dispatch for how they're called.
"""

from __future__ import annotations

import calendar
import struct
import sys
import time

from ..macfile import MacFile
from ..memory import ADDRESS_MASK, from_mac
from ..process import Process, size_resource
from ..toolbox import lowmem, script
from ..toolbox.constants import activateEvt, event_mask, kHighLevelEvent, osEvt, updateEvt
from ..toolbox.events import EventRecord, ProcessKilled, tick_count
from ..toolbox.files import File, FileError
from ..toolbox.gdevice import VIDEO_SLOT
from ..toolbox.memmgr import GZ_PROC, MIN_BLOCK, ZONE_HEADER, memWZErr, nilHandleErr, noErr
from ..toolbox.system import DEFAULT_PARTITION, RAM_SIZE
from . import (decompressors, dispatch, glue_components, glue_desk, glue_sony, glue_threads,
               segload, timemgr)
from .dispatch import raw, trap
from .glue_common import _heap

memFullErr = -108
envBadVers = -5501
envVersTooBig = -5502
gestaltUndefSelectorErr = -5551
gestaltDupSelectorErr = -5552
fnfErr = -43
LC = 0x4C43                     # launchBlockID: a LaunchParamBlockRec
launchContinue = 0x4000
SYSENV_SIZE = 16


# =========================================================================
# Memory Manager.  Every call leaves its result in MemErr.
# =========================================================================
def _zone(t, sys: bool):
    if sys:
        return t.system.sys_heap
    return _heap(t, t.process.world.r32(lowmem.TheZone))


def _heap_of(t, addr: int):
    """The heap a block is in, None (with MemErr set) if it's in none."""
    heap = t.system.find_heap(addr)
    if heap is None:
        t.set_mem_err(memWZErr)
    return heap


def _done(t, heap, value=None):
    t.set_mem_err(heap.error if heap is not None else memWZErr)
    return value


def _handle_op(t, h: int, op) -> None:
    if not h:
        t.set_mem_err(nilHandleErr)
        return
    heap = _heap_of(t, h)
    if heap is not None:
        heap.error = noErr
        op(heap)
        _done(t, heap)


@trap
def NewPtr(t, size, sys, clear):
    # A negative size is one too big to have, not none: Run for the Money
    # works out its offscreen buffer's size in 16 bits, and on a 640-pixel
    # screen gets NIL back and gives up, where 0 bytes it would draw over.
    heap = _zone(t, sys)
    return _done(t, heap, heap.new_ptr(size & 0xFFFFFFFF, clear, low=True))


@trap
def NewHandle(t, size, sys, clear):
    heap = _zone(t, sys)
    return _done(t, heap, heap.new_handle(size & 0xFFFFFFFF, clear))


@trap
def NewEmptyHandle(t, sys):
    heap = _zone(t, sys)
    return _done(t, heap, heap.new_empty_handle())


@trap
def DisposePtr(t, p):
    heap = _heap_of(t, p) if p else None
    if heap is not None:
        heap.dispose_ptr(p)
        _done(t, heap)


@trap
def DisposeHandle(t, h):
    if h:
        _handle_op(t, h, lambda heap: heap.dispose_handle(h))
    else:
        t.set_mem_err(nilHandleErr)


@trap
def GetPtrSize(t, p):
    heap = _heap_of(t, p)
    return _done(t, heap, heap.ptr_size(p)) if heap else 0


@trap
def SetPtrSize(t, p, size):
    heap = _heap_of(t, p)
    if heap is not None:
        heap.set_ptr_size(p, size)
        _done(t, heap)


@trap
def GetHandleSize(t, h):
    if not h:
        t.set_mem_err(nilHandleErr)
        return 0
    heap = _heap_of(t, h)
    if heap is None:
        return 0
    if heap.is_empty(h):
        t.set_mem_err(nilHandleErr)
        return 0
    t.set_mem_err(noErr)
    return heap.handle_size(h)


@trap
def SetHandleSize(t, h, size):
    _handle_op(t, h, lambda heap: heap.set_handle_size(h, size)
               if not heap.is_empty(h) else setattr(heap, "error", nilHandleErr))


@trap
def ReallocateHandle(t, h, size):
    _handle_op(t, h, lambda heap: heap.realloc_handle(h, size))


@trap
def EmptyHandle(t, h):
    _handle_op(t, h, lambda heap: heap.empty_handle(h))


@trap
def RecoverHandle(t, p, sys):
    heap = _heap_of(t, p)
    return _done(t, heap, heap.recover_handle(p)) if heap else 0


@trap
def HLock(t, h):
    _handle_op(t, h, lambda heap: heap.lock(h))


@trap
def HUnlock(t, h):
    _handle_op(t, h, lambda heap: heap.unlock(h))


@trap
def HPurge(t, h):
    _handle_op(t, h, lambda heap: heap.set_purgeable(h, True))


@trap
def HNoPurge(t, h):
    _handle_op(t, h, lambda heap: heap.set_purgeable(h, False))


@trap
def HGetState(t, h):
    """nilHandleErr for an empty handle, as HLock gives: Museum Madness
    reads a picture in again when HGetState says that of its handle, and
    otherwise draws what the handle's block holds."""
    if not h or not t.mem.r32(h) & ADDRESS_MASK:
        t.set_mem_err(nilHandleErr)
        return 0
    t.set_mem_err(noErr)
    return t.mem.r8(h)


@trap
def HSetState(t, h, flags):
    _handle_op(t, h, lambda heap: heap.set_state(h, flags))


@trap
def HSetRBit(t, h):
    _handle_op(t, h, lambda heap: heap.set_state(h, heap.get_state(h) | 0x20))


@trap
def HClrRBit(t, h):
    _handle_op(t, h, lambda heap: heap.set_state(h, heap.get_state(h) & ~0x20))


@trap
def MoveHHi(t, h):
    _handle_op(t, h, lambda heap: heap.move_high(h))


@trap
def MoreMasters(t):
    heap = _zone(t, False)
    heap.more_masters()
    t.set_mem_err(heap.error)


@trap
def MaxApplZone(t):
    t.process.heap.grow(t.process.heap.limit)
    t.set_mem_err(noErr)


@trap
def InitZone(t, pb):
    """A zone from the parameter block's startPtr to its limitPtr, made
    the current zone."""
    start, limit, masters, grow_zone = struct.unpack(">IIHI", t.mem.read(pb, 14))
    start, limit = start & ADDRESS_MASK, limit & ADDRESS_MASK
    if limit - start < ZONE_HEADER + MIN_BLOCK + 8 + 4 * masters:   # no room for anything
        t.set_mem_err(memFullErr)
        return
    heap = t.system.init_zone(start, limit, masters, grow_zone, t.process.partition)
    heap.grow_zone = t.call_grow_zone
    t.process.world.w32(lowmem.TheZone, start)
    t.set_mem_err(noErr)


@trap
def InitApplZone(t):
    t.set_mem_err(noErr)


@trap
def SetApplLimit(t, p):
    """How far the application heap may grow.  It "enforces a minimum
    stack size equal to DefltStack" (Inside Macintosh: Memory errata,
    "Addendum to Description of SetApplLimit"), so a limit nearer
    CurStackBase than that, or past it, becomes the one launch gives, the
    long word at or below DefltStack under CurStackBase.  Grown past that,
    the heap takes the stack in use (Macify 2.7 asks for 128K past
    ApplZone in its 75K partition, Bumptz Science Carnival for its stack
    pointer, before MaxApplZone).  Below where the heap already ends is
    too low (memFullErr), though ApplLimit still says what was asked for."""
    proc = t.process
    p = min(p & ADDRESS_MASK, (proc.partition.stack_base - t.mem.r32(lowmem.DefltStack)) & ~3)
    proc.world.w32(lowmem.ApplLimit, p)
    proc.heap.set_limit(p)
    t.set_mem_err(noErr if p >= proc.heap.end else memFullErr)


@trap
def SetGrowZone(t, p):
    """The current zone's grow-zone function, which the Memory Manager calls
    when it can make room for a block no other way (Inside Macintosh:
    Memory, "MyGrowZone")."""
    heap = _zone(t, False)
    t.mem.w32(heap.start + GZ_PROC, p)
    heap.grow_zone = t.call_grow_zone
    t.set_mem_err(noErr)


@trap
def GetZone(t):
    t.set_mem_err(noErr)
    return t.process.world.r32(lowmem.TheZone)


@trap
def SetZone(t, zone):
    t.process.world.w32(lowmem.TheZone, zone)
    t.set_mem_err(noErr)


@trap
def HandleZone(t, h):
    heap = _heap_of(t, h) if h else None
    return _done(t, heap, heap.start if heap is not None else 0)


@trap
def PtrZone(t, p):
    heap = _heap_of(t, p) if p else None
    return _done(t, heap, heap.start if heap is not None else 0)


@trap
def FreeMem(t, sys):
    """The zone's zcbFree (Inside Macintosh: Memory, Memory Manager): its
    free blocks, not what it could still grow by, which MaxMem reports.
    Programs add the two (Starter Accounts' BASIC runtime, for FRE(-1))."""
    return _zone(t, sys).free_bytes(grow=False)


@trap
def MaxBlock(t, sys):
    return _zone(t, sys).max_block()


@trap
def MaxMem(t, sys):
    """The zone purged of every purgeable block and compacted, its largest
    free block and how far it can grow (Inside Macintosh II-38)."""
    heap = _zone(t, sys)
    heap.purge_for()
    heap.compact()
    t.set_mem_err(noErr)
    return heap.max_block(grow=False), heap.room_to_grow()     # D0 largest block, A0 growth


@trap
def CompactMem(t, size, sys):
    """The zone compacted, unless it already has a free block of size
    (blocks move only when they have to); its largest free block."""
    heap = _zone(t, sys)
    if heap.max_block(grow=False) < size:
        heap.compact()
    return heap.max_block()


@trap
def PurgeMem(t, size, sys):
    """Purgeable blocks purged until there's a free block of size, which
    the zone may grow for; nothing moves (Inside Macintosh II-40)."""
    t.set_mem_err(noErr if _zone(t, sys).purge_for(size) else memFullErr)


@trap
def ReserveMem(t, size, sys):
    """Room for a block of SIZE as low in the heap as it can be, handles
    moving up out of its way (Inside Macintosh II-39).  The zone grows for
    it, as far as it can when that's not enough, as a Mac's does: programs
    of the 64K ROM's day asked for more than there was in place of
    MaxApplZone (The Fool's Errand asks for 8 MB, then sizes its buffers by
    MaxMem)."""
    heap = _zone(t, sys)
    if heap.reserve(size):
        t.set_mem_err(noErr)
        return
    if heap.max_block(grow=False) < size:
        heap.grow_for(size)
    t.set_mem_err(noErr if heap.max_block(grow=False) >= size else memFullErr)


@trap
def PurgeSpace(t, sys):
    """What purging the zone's unlocked purgeable blocks would leave free,
    in all and in one piece, with nothing purged (Inside Macintosh IV,
    Memory Manager).  HyperCard 1.2 fills its heap with purgeable blocks,
    then asks this whether it has room to go on."""
    total, contig = _zone(t, sys).purge_space()
    return None, total, contig      # D0 total, A0 contiguous


@trap
def StackSpace(t):
    """In a Thread Manager thread, what's left of its own stack."""
    space = glue_threads.stack_space(t)
    return max(t.cpu.sp - t.process.heap.end, 0) if space is None else space


@trap
def FlushCodeCache(t):
    t.cpu.flush_code()


hwParamErr = -502
# HWPriv's selectors for the caches: swapping the instruction or data
# cache on or off, flushing either, the external cache's, and flushing a
# range of the code cache
CACHE_SELECTORS = {0: "SwapInstructionCache", 1: "FlushInstructionCache",
                   2: "SwapDataCache", 3: "FlushDataCache", 4: "EnableExtCache",
                   5: "DisableExtCache", 6: "FlushExtCache", 9: "FlushCodeCacheRange"}


@raw(0xA198)
def HWPriv(t, word):
    """The selector in D0.  Flushing the instruction cache, or a range
    of it, is FlushCodeCache; the other caches have nothing in them to
    flush, and turning one on or off changes nothing.  The swaps leave A0
    as it was, so the old state is the one asked for."""
    selector = t.cpu.d(0) & 0xFFFF
    if selector in CACHE_SELECTORS:
        if selector in (1, 9):
            t.cpu.flush_code()
        t.cpu.set_d(0, noErr)
    else:
        t.unimplemented(f"HWPriv selector {selector}", word)
        t.cpu.set_d(0, hwParamErr & 0xFFFFFFFF)


@trap
def BlockMove(t, src, dst, n, no_flush):
    if n > 0:
        t.mem.move(src, dst, n)
    t.set_mem_err(noErr)


def _copy_to_new_handle(t, data: bytes) -> int:
    h = t.process.heap.new_handle_with(data)
    t.set_mem_err(t.process.heap.error)
    return h


def _contents(t, h: int) -> bytes | None:
    heap = _heap_of(t, h) if h else None
    if heap is None or heap.is_empty(h):
        return None
    return t.mem.read(heap.deref(h), heap.handle_size(h))


@trap
def HandToHand(t, h):
    data = _contents(t, h)
    if data is None:
        return nilHandleErr, h
    new = _copy_to_new_handle(t, data)
    return (noErr, new) if new else (memFullErr, h)


@trap
def PtrToHand(t, src, size):
    new = _copy_to_new_handle(t, t.mem.read(src, max(size, 0)))
    return (noErr, new) if new else (memFullErr, 0)


def _set_contents(t, h: int, data: bytes) -> int:
    heap = _heap_of(t, h) if h else None
    if heap is None:
        return nilHandleErr
    if not heap.set_handle_size(h, len(data)):
        return heap.error or memFullErr
    t.mem.write(heap.deref(h), data)
    return noErr


@trap
def PtrToXHand(t, src, dst, size):
    return _set_contents(t, dst, t.mem.read(src, max(size, 0)))


@trap
def HandAndHand(t, a, b):
    data, existing = _contents(t, a), _contents(t, b)
    if data is None or existing is None:
        return nilHandleErr
    return _set_contents(t, b, existing + data)


@trap
def PtrAndHand(t, p, h, size):
    existing = _contents(t, h)
    if existing is None:
        return nilHandleErr
    return _set_contents(t, h, existing + t.mem.read(p, max(size, 0)))


# =========================================================================
# The trap table
# =========================================================================
UNIMPLEMENTED = 0xA89F
# Traps that work but that GetTrapAddress calls unimplemented.  Programs
# that look for MultiFinder this way, Microsoft's among them, go on to
# make its private _OSDispatch calls, and work without them.  Programs
# that want temporary memory or the Process Manager ask Gestalt, or just
# call them.
HIDDEN = {0xA88F}           # _OSDispatch
# 32-Bit QuickDraw's QD32Trap, there to be looked for: programs ask
# whether it's implemented to know 32-Bit QuickDraw is there (Pico).  It
# is, but only on a colour machine.
QD32TRAP = 0xAB03
# And what a Plus has no slots for, which programs look for in the same
# way: NetTrek asks whether SlotVInstall is there, and if it is, calls
# GetVideoDefault for the slot to give it.
SLOT_TRAPS = {0xA06E, 0xA06F, 0xA070,           # SlotManager, SlotVInstall, SlotVRemove,
              0xA071,                           # AttachVBL
              0xA080, 0xA081}                   # Get/SetVideoDefault
# Nor has macrt's Plus the ADB Manager, which Inside Macintosh V puts in
# the 256K ROM: CountADBs, GetIndADB, GetADBInfo, SetADBInfo.
ADB_TRAPS = {0xA077, 0xA078, 0xA079, 0xA07A}


def _trap_key(num: int, new: bool, toolbox: bool) -> int:
    if not new:
        # The 64K ROM's single table: $00-$4F were OS traps, the rest Toolbox.
        toolbox = (num & 0x1FF) > 0x4F
        return (0xA800 | (num & 0x1FF)) if toolbox else (0xA000 | (num & 0xFF))
    return (0xA800 | (num & 0x3FF)) if toolbox else (0xA000 | (num & 0xFF))


def trap_address(t, key: int) -> int:
    patch = t.patches.get(key)
    if patch is not None:
        return patch
    if ((key == QD32TRAP and not t.system.model.color)
            or (key in SLOT_TRAPS and not t.system.model.slots)
            or (key in ADB_TRAPS and not t.system.model.adb)):
        return t.machine.stub_for(UNIMPLEMENTED)
    if (dispatch.implemented(key) and key not in HIDDEN) or key == UNIMPLEMENTED:
        return t.machine.stub_for(key)
    return t.machine.stub_for(UNIMPLEMENTED)


@trap
def GetTrapAddress(t, num, new, toolbox):
    key = _trap_key(num, new, toolbox)
    addr = trap_address(t, key)
    if (key != UNIMPLEMENTED and key not in t.patches
            and addr == t.machine.stub_for(UNIMPLEMENTED)):
        t.asked_missing(dispatch.trap_name(key))
    return addr


@trap
def SetTrapAddress(t, addr, num, new, toolbox):
    key = _trap_key(num, new, toolbox)
    if (addr & 0xFFFFFF) == t.machine.stub_for(key):
        t.patches.pop(key, None)            # the original put back
    else:
        t.patches[key] = addr & 0xFFFFFF


@raw(UNIMPLEMENTED)
def _unimplemented(t, word):
    t.system_error(12, "the application called _Unimplemented")


@raw(QD32TRAP)
def _qd32_trap(t, word):
    if not t.system.model.color:
        _unimplemented(t, word)


# =========================================================================
# What the machine is
# =========================================================================
def _ostype(s: str) -> int:
    return struct.unpack(">I", s.encode("mac_roman"))[0]


def gestalt_table(t) -> dict[int, int]:
    model = t.system.model
    return {_ostype(k): v for k, v in {
        "vers": 1,                  # Gestalt's own version
        "sysv": t.system.version,
        "sysa": 1,                  # 68k
        "proc": model.cpu + 1,      # 1 a 68000, 3 a 68020, 4 a 68030
        "fpu ": model.fpu,          # 1 a 68881, 2 a 68882
        "mmu ": 0,
        # gestalt32BitQD13, the System 7 version of Color QuickDraw (Inside
        # Macintosh: Imaging With QuickDraw, Color QuickDraw), as macrt is
        # a System 7; or the original.  Its features: colour, deep GWorlds
        # and direct pixel maps
        "qd  ": 0x230 if model.color else 0,
        "qdrw": 7 if model.color else 0,
        "mach": model.gestalt,
        "romv": model.rom,
        "ram ": RAM_SIZE,
        "lram": RAM_SIZE,
        "addr": 0,                  # 24-bit
        "vm  ": 0,
        # the Sound Manager's: SndPlayDoubleBuffer (bit 10), and channels
        # played together (gestaltMultiChannels, bit 11), as Lucy's colour
        # WorldBuilder games want; the Sound Input Manager's routines
        # (gestaltSoundIOMgrPresent, bit 3), as Meteor Storm wants before
        # it asks the Sound Manager's version; mono, no sound input device
        "snd ": 1 << 3 | 1 << 10 | 1 << 11,
        # The system heap grows; Launch can return and takes an FSSpec;
        # temporary memory, tracked
        "os  ": 0x7F,
        # gestaltFullExtFSDispatching, all of HFSDispatch; gestaltHasFSSpecCalls
        "fs  ": 3,
        "fold": 1,                  # gestaltFindFolderPresent
        "alis": 1,                  # gestaltAliasMgrPresent
        "stdf": 1,                  # gestaltStandardFile58: StandardGetFile and the rest
        "ditl": 1,                  # gestaltDITLExtPresent: AppendDITL, ShortenDITL, CountDITL
        "edtn": 1,                  # gestaltEditionMgrPresent
        "nmgr": 1,                  # gestaltNotificationPresent: NMInstall and NMRemove
        "evnt": 1,                  # gestaltAppleEventsPresent
        "help": 1,                  # gestaltHelpMgrPresent
        "ppc ": 0x1000,             # initialised, but nothing comes or goes on a network
        "tmgr": 3,                  # gestaltExtendedTimeMgr: InsXTime
        "thds": 1,                  # the Thread Manager (glue_threads)
        "scri": script.VERSION,
        "scr#": 1,                  # gestaltScriptCount: the Roman script, the only one
        "pop!": 1,                  # gestaltPopupPresent: CDEF 63 (popups)
        "cpnt": glue_components.VERSION,
        "kbd ": model.keyboard,     # 3 a Mac Plus's, 5 Apple Standard ADB
        "dbug": 0,
        "misc": 0,
        "hdwr": model.hardware,
    }.items()}


@trap
def Gestalt(t, selector):
    selector &= 0xFFFFFFFF
    proc = t.system.gestalt_procs.get(selector)
    if proc is not None:
        # pascal OSErr fn(OSType selector, long *response), with the
        # response on our side of the stack.
        cpu = t.cpu
        cpu.sp = response = cpu.sp - 4
        err = t.call(proc, (("l", selector), ("p", response)), "e")
        value = t.mem.r32(response)
        cpu.sp = response + 4
        return err, value
    value = gestalt_table(t).get(selector)
    if value is None:
        code = selector.to_bytes(4, "big")
        t.asked_missing("Gestalt " + (f"'{code.decode('mac_roman')}'"
                                      if all(32 <= b < 127 for b in code)
                                      else f"${selector:08X}"))
        return gestaltUndefSelectorErr, 0
    return noErr, value


@trap
def NewGestalt(t, selector, proc):
    """Add a selector answered by 68k code.  The function is the whole
    system's, as on a Mac, so it lives in the system heap."""
    selector &= 0xFFFFFFFF
    if selector in t.system.gestalt_procs or selector in gestalt_table(t):
        return gestaltDupSelectorErr
    t.system.gestalt_procs[selector] = proc
    return noErr


@trap
def ReplaceGestalt(t, selector, proc):
    selector &= 0xFFFFFFFF
    old = t.system.gestalt_procs.get(selector)
    if old is None and selector not in gestalt_table(t):
        return gestaltUndefSelectorErr, 0
    t.system.gestalt_procs[selector] = proc
    return noErr, old or 0


@trap
def SysEnvirons(t, version, p):
    if version <= 0:
        return envBadVers
    model = t.system.model
    sys_vref = t.system.file_manager.get_vol()[1]     # the System Folder's working directory
    rec = struct.pack(">hhhhBBhhh", min(version, 2), model.environs, t.system.version,
                      model.cpu + 1, model.fpu > 0, model.color, model.keyboard, 0, sys_vref)
    t.mem.write(p, rec)
    return envVersTooBig if version > 2 else noErr


# =========================================================================
# OS Event Manager
# =========================================================================
evtNotEnb = 1
evType = 4
# An EvQEl: qLink, qType, evtQWhat, evtQMessage, evtQWhen, evtQWhere, evtQModifiers
EVQEL_SIZE = 22


@trap
def PostEvent(t, what, message, ppost):
    """PostEvent, and PPostEvent (bit 8), which gives the program the
    event's queue entry in A0 (IM: Macintosh Toolbox Essentials, Event
    Manager).  macrt's entry holds that event alone, its qLink NIL; what
    the program changes in it before the event's got is what it gets."""
    events = t.process.events
    where, mods = events.input_state()
    ev = EventRecord(what, message & 0xFFFFFFFF, 0, where, mods)
    if not events.post(ev):
        return evtNotEnb                    # SysEvtMask leaves it out
    if ppost:
        qel = _event_qel(t)
        if qel:
            t.system.event_qels[qel] = (events, ev)
            t.mem.write(qel, struct.pack(">LhhLLhhH", 0, evType, ev.what, ev.message, ev.when,
                                         ev.where.v, ev.where.h, ev.modifiers))
            ev.qel = qel
        t.cpu.set_a(0, qel)
    return noErr


def _event_qel(t) -> int:
    """A queue entry in the system heap whose event has been got, or a new one."""
    qels = t.system.event_qels
    for qel, (events, ev) in qels.items():
        if not events.queued(ev):
            ev.qel = 0
            return qel
    return t.system.sys_heap.new_ptr(EVQEL_SIZE, clear=True)


# These two read the Operating System event queue only: GetOSEvent "does
# not return activate, update, operating-system, or high-level events"
# (IM: Macintosh Toolbox Essentials, Event Manager, "Receiving Events").
OS_EVENTS = 0xFFFF & ~(event_mask(activateEvt) | event_mask(updateEvt) | event_mask(osEvt) |
                       event_mask(kHighLevelEvent))


@trap
def GetOSEvent(t, mask, p):
    glue_sony.put_back(t)
    got, ev = t.process.events.get(mask & OS_EVENTS, 0, lambda: None)
    t.write_event(p, ev)
    return got


@trap
def OSEventAvail(t, mask, p):
    glue_sony.put_back(t)
    got, ev = t.process.events.peek(mask & OS_EVENTS, lambda: None)
    t.write_event(p, ev)
    return got


@trap
def FlushEvents(t, mask, stop_mask):
    t.process.events.flush(mask & 0xFFFF, stop_mask & 0xFFFF)


# =========================================================================
# The Notification Manager: a background application asks for the user.
# An NMRec is a queue element: qLink, qType (8), nmFlags, nmPrivate,
# nmReserved, nmMark, nmIcon, nmSound, nmStr, nmResp, nmRefCon.
# =========================================================================
nmType = 8
nmTypErr = -299
NM_STR, NM_RESP = 24, 28


@trap
def NMInstall(t, rec):
    """Its alert is shown at once; its response procedure, the request's
    last step, is called at an event call after the alert is dismissed,
    or the next if there's none (IM: Processes, Notification Manager)."""
    if t.mem.r16(rec + 4) != nmType:
        return nmTypErr
    proc = t.process
    proc.notifications.add(rec)
    p = t.mem.r32(rec + NM_STR) & ADDRESS_MASK
    notify = getattr(t.tb._d, "notify", None) if t.tb is not None else None
    if p and notify is not None:
        notify(proc.name, t.str255(p), lambda: proc.responses_due.append(rec))
    else:
        proc.responses_due.append(rec)
    return noErr


def respond(t) -> None:
    """Call the response procedures that are due, with the request's
    pointer.  One of -1 asks for the request to be removed; NIL, for
    nothing.  A request they install again waits for the next call."""
    proc = t.process
    for _ in range(len(proc.responses_due)):
        rec = proc.responses_due.popleft()
        if rec not in proc.notifications:
            continue                    # removed meanwhile
        resp = t.mem.r32(rec + NM_RESP)
        if resp == 0xFFFFFFFF:
            proc.notifications.discard(rec)
        elif resp:
            t.call(resp, (("p", rec),))


@trap
def NMRemove(t, rec):
    if t.mem.r16(rec + 4) != nmType:
        return nmTypErr
    t.process.notifications.discard(rec)
    return noErr


# =========================================================================
# The Shutdown Manager: procedures to call before the machine is turned
# off or restarted (IM V, The Shutdown Manager).  Nothing here turns the
# machine off, so they're only kept, each with the points it asked for
# (sdOnPowerOff 1, sdOnRestart 2, sdOnUnmount 4, sdOnDrivers 8).
# =========================================================================
@trap
def ShutDwnInstall(t, proc, flags):
    procs = t.process.shutdown_procs
    proc &= ADDRESS_MASK
    procs[proc] = procs.get(proc, 0) | flags & 0xF


@trap
def ShutDwnRemove(t, proc):
    """It's removed at every point it was installed for."""
    t.process.shutdown_procs.pop(proc & ADDRESS_MASK, None)


# =========================================================================
# Time
# =========================================================================
@trap
def Delay(t, ticks):
    t.process.events.timed()
    t.sleep(ticks)
    return None, tick_count()


@trap
def ReadDateTime(t, p):
    t.mem.write(p, t.mem.read(lowmem.Time, 4))
    return noErr


@trap
def SetDateTime(t, secs):
    return noErr                        # the host's clock stays as it is


@trap
def SecondsToDate(t, secs, p):
    from .cpu import MAC_1904
    tm = time.gmtime((secs & 0xFFFFFFFF) - MAC_1904)
    t.mem.write(p, struct.pack(">7h", tm.tm_year, tm.tm_mon, tm.tm_mday, tm.tm_hour,
                               tm.tm_min, tm.tm_sec, (tm.tm_wday + 1) % 7 + 1))


@trap
def DateToSeconds(t, p):
    from .cpu import MAC_1904
    year, month, day, hour, minute, sec, _ = struct.unpack(">7h", t.mem.read(p, 14))
    try:
        secs = calendar.timegm((year, month, day, hour, minute, sec, 0, 0, 0)) + MAC_1904
    except (ValueError, OverflowError):
        secs = 0
    return None, secs & 0xFFFFFFFF


@raw(0xA193)
def Microseconds(t, word):
    us = int(time.monotonic() * 1_000_000)
    t.cpu.set_a(0, us >> 32)
    t.cpu.set_d(0, us)


@trap
def StripAddress(t, addr):
    return addr & 0x00FFFFFF


@trap
def SwapMMUMode(t, mode):
    """D0's low byte: the addressing mode wanted, 1 for 32-bit; back in
    D0, the mode there was, the rest of D0 clear: Prince of Persia 2's
    sprite code goes on counting a row's pixels in D0's low word.  macrt's
    addresses are all 24-bit ones, the screen's too, so it stays in
    24-bit mode."""
    return None, t.mem.r8(lowmem.MMU32Bit)


# =========================================================================
# Strings the OS compares.  Bit 9 of the trap word: ignore diacriticals;
# bit 10: case matters.
# =========================================================================
def _fold(data: bytes, word: int) -> bytes:
    """Byte for byte, so the length stays: UprString works in place."""
    if word & 0x200:
        data = data.translate(script.STRIP)
    if not word & 0x400:
        data = data.translate(script.UPPER)
    return data


@raw(0xA03C)
def CmpString(t, word):
    """EqualString: A0, A1 the strings, D0 their lengths (high word, low word)."""
    cpu = t.cpu
    d0 = cpu.d(0)
    a = t.mem.read(cpu.a(0), d0 >> 16)
    b = t.mem.read(cpu.a(1), d0 & 0xFFFF)
    cpu.set_d(0, 0 if _fold(a, word) == _fold(b, word) else 1)


@raw(0xA050)
def RelString(t, word):
    """-1, 0 or 1, as the first string sorts before, with or after the second."""
    cpu = t.cpu
    d0 = cpu.d(0)
    a = _fold(t.mem.read(cpu.a(0), d0 >> 16), word)
    b = _fold(t.mem.read(cpu.a(1), d0 & 0xFFFF), word)
    cpu.set_d(0, 0 if a == b else (0xFFFFFFFF if a < b else 1))


@raw(0xA054)
def UprString(t, word):
    """Upper-case the string at A0, length D0.W, in place."""
    cpu = t.cpu
    p, n = cpu.a(0), cpu.d(0) & 0xFFFF
    t.mem.write(p, _fold(t.mem.read(p, n), (word & 0x200) | 0))


# =========================================================================
# Queues: the OS keeps them for VBL tasks, drivers and the like.
# =========================================================================
QTYPE = 4                           # an element's qType
qErr = -1


@trap
def Enqueue(t, elem, header):
    mem = t.mem
    mem.w32(elem, 0)
    tail = mem.r32(header + 6)
    if tail:
        mem.w32(tail, elem)
    else:
        mem.w32(header + 2, elem)
    mem.w32(header + 6, elem)


@trap
def Dequeue(t, elem, header):
    mem = t.mem
    prev, cur = 0, mem.r32(header + 2)
    seen = set()                        # a queue that goes round (an element enqueued twice)
    while cur and cur not in seen:
        seen.add(cur)
        if cur == elem:
            nxt = mem.r32(cur)
            if prev:
                mem.w32(prev, nxt)
            else:
                mem.w32(header + 2, nxt)
            if mem.r32(header + 6) == elem:
                mem.w32(header + 6, prev)
            return noErr
        prev, cur = cur, mem.r32(cur)
    return qErr


# =========================================================================
# VBL tasks: each application has its own queue, as under MultiFinder,
# and the runner runs it while the application has the processor.
# =========================================================================
vType = 1
vTypErr = -2


@trap
def VInstall(t, task):
    if t.mem.r16(task + QTYPE) != vType:
        return vTypErr
    Enqueue(t, task, lowmem.VBLQueue)
    return noErr


@trap
def VRemove(t, task):
    if t.mem.r16(task + QTYPE) != vType:
        return vTypErr
    return Dequeue(t, task, lowmem.VBLQueue)


# Deferred tasks: queued by an interrupt's tasks, to run once they're done
# (Runner._deferred_tasks).  Each application has its own queue too.
dtQType = 7


@trap
def DTInstall(t, task):
    if t.mem.r16(task + QTYPE) != dtQType:
        return vTypErr
    Enqueue(t, task, lowmem.DTQueue)
    return noErr


@trap
def SlotVInstall(t, task, slot):
    """For one video card's retrace; there's one screen, so the same."""
    return VInstall(t, task)


@trap
def SlotVRemove(t, task, slot):
    return VRemove(t, task)


slotNumErr = -360


# AttachVBL makes theSlot the primary monitor's (Inside Macintosh:
# Processes, Vertical Retrace Manager, Utility Routines).  The one screen
# is the card in VIDEO_SLOT, so that's the slot it can be; any other is
# slotNumErr.
@trap
def AttachVBL(t, slot):
    return noErr if slot == VIDEO_SLOT else slotNumErr


# The Start Manager's default video device, kept in parameter RAM (Inside
# Macintosh V, The Start Manager): which card the main screen is, a
# DefVideoRec's sdSlot and sdsResource bytes.  The screen is the one card
# there is, in the Mac II's first slot.
VIDEO_DEFAULT = (VIDEO_SLOT, 0x80)


@trap
def GetVideoDefault(t, rec):
    slot, sresource = getattr(t.system, "video_default", VIDEO_DEFAULT)
    t.mem.w8(rec, slot & 0xFF)
    t.mem.w8(rec + 1, sresource)


@trap
def SetVideoDefault(t, rec):
    t.system.video_default = (t.mem.r8(rec), t.mem.r8(rec + 1))


# And the disk to start up from, a DefStartRec: for a SCSI disk, two bytes
# reserved and its driver's reference number.  It's the boot volume's
# disk until a program (Startup Disk) sets another.
@trap
def GetDefaultStartup(t, rec):
    default = getattr(t.system, "default_startup", None)
    if default is None:
        boot = t.system.files.boot
        default = bytes(2) + struct.pack(">h", 0 if boot.floppy else boot.driver)
    t.mem.write(rec, default)


@trap
def SetDefaultStartup(t, rec):
    t.system.default_startup = t.mem.read(rec, 4)


# =========================================================================
# The Time Manager: each application's tasks, which the runner runs as it
# does VBL tasks (see timemgr).
# =========================================================================
def _wait(t, task: int, waiting: bool) -> None:
    q = t.mem.r16(task + QTYPE)
    t.mem.w16(task + QTYPE, q | timemgr.ACTIVE if waiting else q & ~timemgr.ACTIVE)


@raw(0xA058)
def InsTime(t, word):
    """A0 the task.  InsXTime is the same trap with bit 10 set (Super
    Maze Wars's clock), and installs an extended task."""
    task = t.cpu.a(0)
    t.time_tasks.install(task, extended=bool(word & 0x400))
    _wait(t, task, False)
    t.cpu.set_d(0, noErr)


@trap
def PrimeTime(t, task, count):
    wake_up = task + timemgr.TM_WAKE_UP
    tasks = t.time_tasks
    extended = task in tasks.extended
    when = tasks.prime(task, count, woken=extended and t.mem.r32(wake_up) != 0)
    if extended:                    # when it's due, in milliseconds; never 0
        t.mem.w32(wake_up, int(when * 1000) & 0xFFFFFFFF or 1)
    _wait(t, task, True)
    return noErr


@trap
def RmvTime(t, task):
    t.mem.w32(task + timemgr.TM_COUNT, timemgr.remaining(t.time_tasks.remove(task)) & 0xFFFFFFFF)
    _wait(t, task, False)
    return noErr


@trap
def InitUtil(t):
    return noErr


@trap
def WriteParam(t):
    return noErr


@raw(0xA051)
def ReadXPRam(t, word):
    """Parameter RAM: A0 the buffer, D0 count (high word) and offset."""
    cpu = t.cpu
    n = (cpu.d(0) >> 16) & 0xFFFF
    t.mem.fill(cpu.a(0), n)
    cpu.set_d(0, 0)


@raw(0xA052)
def WriteXPRam(t, word):
    t.cpu.set_d(0, 0)


# =========================================================================
# Segment Loader and the Process Manager's part of it
# =========================================================================
@raw(0xA9F0)
def LoadSeg(t, word):
    t.segments.trap_load_seg(t.cpu.pc)


@trap
def UnloadSeg(t, routine):
    t.segments.unload(routine)


@trap
def ExitToShell(t):
    glue_desk.close_all(t)
    raise ProcessKilled()


dsReinsert = 30                 # "Please insert the disk:", with no buttons


@raw(0xA9C9)
def SysError(t, word):
    """The System Error Handler, with the ID in D0 (Inside Macintosh II,
    "The System Error Handler").  Its alert comes back only with no
    buttons: the disk-switch alert, and the startup alerts, whose IDs are
    negative.  Any other is a bomb, its buttons Restart and, with a resume
    procedure, Resume; macrt stops the application, as for any other
    system error.  Downhill Racer's and The Surgeon's exception handlers
    call it and then jump to the ROM's restart."""
    error_id = t.cpu.d(0) & 0xFFFF
    error_id -= error_id >> 15 << 16
    t.mem.w16(lowmem.DSErrCode, error_id & 0xFFFF)
    if error_id < 0 or error_id == dsReinsert:
        return
    t.system_error(error_id, f"SysError at ${t.cpu.pc - 2 & ADDRESS_MASK:06X}")


@raw(0xA9F2)
def Launch(t, word):
    """Launch, and LaunchApplication, the same trap: start another
    application.  A0 points at the application's name and a word of
    configuration, or, marked 'LC', a LaunchParamBlockRec with an FSSpec,
    which gets the new process's serial number and its SIZE's partition
    sizes back.  The configuration is the page option, for CurPageOption:
    Dark Castle asks for the alternate screen with it.  Unless
    launchContinue is set, this application quits for it."""
    mem, pb = t.mem, t.cpu.a(0) & ADDRESS_MASK
    if mem.r16(pb + 6) == LC:
        spec = mem.r32(pb + 16) & ADDRESS_MASK
        app = _app_at(t, spec) if spec else None
        if app is not None:
            preferred, minimum = size_resource(app) or (DEFAULT_PARTITION, DEFAULT_PARTITION)
            mem.w32(pb + 28, preferred)
            mem.w32(pb + 32, minimum)
            mem.w32(pb + 36, t.system.largest_free())
        proc = _start(t, app, 0, mem.r16(pb + 14))
        if proc is not None:
            mem.w32(pb + 20, 0)
            mem.w32(pb + 24, proc.serial_number)
    else:
        name = t.str255(mem.r32(pb) & ADDRESS_MASK)
        _start(t, _app_named(t, name), mem.r16(pb + 4), 0, _finder_information(t))


@raw(0xA9F3)
def Chain(t, word):
    """Start another application in this one's place, as Launch does, A0
    pointing at its name and the configuration word.  On a Mac it got
    this one's heap, and what was left in it; here it gets a partition of
    its own.  Skyfox's first application chains to the game, asking for
    the alternate screen."""
    mem, pb = t.mem, t.cpu.a(0)
    _start(t, _app_named(t, t.str255(mem.r32(pb) & 0xFFFFFF)), mem.r16(pb + 4), 0,
           _finder_information(t))


def _app_named(t, name: str) -> MacFile | None:
    folder = t.process.app.folder if t.process.app is not None else None
    on_disk = ":" in name.lstrip(":")       # Falcon's launcher names "Falcon 2:Falcon"
    app = folder.find(name) if folder is not None and name and not on_disk else None
    if app is None and name:
        try:
            node = t.process.files.lookup_file(0, 0, name)
        except FileError:
            node = None
        app = t.system.files.macfile_of(node) if node is not None else None
    return app


def _app_at(t, spec: int) -> MacFile | None:
    """The application an FSSpec names: its volume, parent and name."""
    mem = t.mem
    name = from_mac(mem.r_pstr(spec + 6))
    try:
        node = t.process.files.lookup(mem.r16s(spec), mem.r32(spec + 2), name)
    except FileError:
        return None
    return t.system.files.macfile_of(node) if isinstance(node, File) else None


def _finder_information(t) -> bytes | None:
    """What this application's AppParmHandle has, for the application it
    launches: the Finder starts an application with Launch, and "passes
    along a list of documents" in its Finder information (Inside
    Macintosh II, the Segment Loader), so a program that launches another
    with documents to open puts them there first.  Cipher names itself,
    a program for PShell, and launches PShell to run it."""
    h, heap = t.process.world.r32(lowmem.AppParmHandle) & ADDRESS_MASK, t.process.heap
    if not heap.is_handle(h) or heap.is_empty(h) or heap.handle_size(h) < 4:
        return None
    return t.mem.read(heap.deref(h), heap.handle_size(h))


def _keep_directories(info: bytes, proc: Process) -> None:
    """The working directories the Finder information names its documents
    by stay open for the new application after this one quits."""
    at, count = 4, struct.unpack_from(">h", info, 2)[0]
    for _ in range(count):
        if at + 9 > len(info):
            break
        proc.files.fs.share_wd(struct.unpack_from(">h", info, at)[0], proc.files)
        at += 9 + info[at + 8]
        at += at & 1


def _start(t, app: MacFile | None, page_option: int, flags: int,
           finder_info: bytes | None = None) -> Process | None:
    cpu = t.cpu
    if app is None:
        cpu.set_d(0, fnfErr & 0xFFFFFFFF)
        return None
    # Its CODE 0 is read here, where a 'dcmp' of its own can be run on it.
    try:
        code0 = segload.code0(app, lambda data: decompressors.expand(t, data, app.rsrc))
    except segload.SegmentError:
        code0 = None                # the launch says why
    d = t.tb._d
    with t.yielding():
        started = d.bridge.call(d.launch_application, app, page_option, code0, finder_info)
        if not isinstance(started, str) and finder_info is not None:
            _keep_directories(finder_info, started)
        if not isinstance(started, str) and not flags & launchContinue:
            # It quits from here, not after waiting for the processor,
            # which the new application may never give up.
            raise ProcessKilled()
    if isinstance(started, str):
        print(f"macrt: {t.process.name} couldn't launch {app.name}: {started}", file=sys.stderr)
        cpu.set_d(0, memFullErr & 0xFFFFFFFF)
        return None
    cpu.set_d(0, 0)
    return started


@trap
def GetAppParms(t, name_p, refnum_p, parm_p):
    world = t.process.world
    t.mem.write(name_p, world.read(lowmem.CurApName, 32))
    t.w16(refnum_p, world.r16(lowmem.CurApRefNum))
    t.w32(parm_p, world.r32(lowmem.AppParmHandle))


# =========================================================================
# Object Pascal
# =========================================================================
@raw(0xA9F8)
def MethodDispatch(t, word):
    """A method's call, as the Mac Plus's ROM makes it.  A call jumps to
    its own stub, which calls this, its table after it, with the object,
    a handle, under the caller's return address.  The table is a count
    of entries, less one; the entry last found, for the class it was
    found for; then an entry for each class with the method, highest
    numbered first, and a 0.  An entry is the class and the method's
    offset: an even one is the method's jump table entry, an odd one is
    from the entry.  An object's class is its first word, and a class's
    superclass is the word that far into the class table, which the
    pointer ClassTable points to points to.  If no class of the object's
    has the method, it goes to MethodNotFound instead.  Q-Sheet sets
    them; on a 64K ROM it uses its own copy of the same code."""
    cpu, m = t.cpu, t.mem
    table = cpu.pc
    cls = m.r16(m.r32(m.r32(cpu.sp + 4) & ADDRESS_MASK) & ADDRESS_MASK)
    count = m.r16(table)
    first = entry = table + 2

    def search(entry: int, count: int, step: bool) -> tuple[int, int, bool]:
        """The entries from here, as DBCC goes through them: where it
        stopped, the count left, and whether that's the class's."""
        while True:
            if step:
                entry += 4
            step = True
            if cls >= m.r16(entry):
                break
            count = (count - 1) & 0xFFFF
            if count == 0xFFFF:
                break
        return entry, count, cls == m.r16(entry)

    found = m.r16(entry) == cls
    if not found:
        m.w16(entry, cls)
        entry, count, found = search(entry, count, True)
        classes = m.r32(m.r32(lowmem.ClassTable) & ADDRESS_MASK) & ADDRESS_MASK
        for _ in range(0x8000):         # as many classes as there can be
            if found:
                break
            cls = m.r16(classes + _signed16(cls) & ADDRESS_MASK)
            if not cls:
                break
            entry, count, found = search(entry, count, False)
        if found:
            m.w16(first + 2, m.r16(entry + 2))
        else:
            m.w32(first, 0)
            cpu.set_d(0, 0)
            cpu.pc = m.r32(lowmem.MethodNotFound) & ADDRESS_MASK
        cpu.set_d(1, cpu.d(1) & ~0xFFFF | count)
        cpu.set_d(2, first)
    cpu.set_a(0, entry)
    if found:
        offset = m.r16(entry + 2)
        cpu.set_d(0, cpu.d(0) & ~0xFFFF | offset & ~1)
        base = entry + 2 if offset & 1 else cpu.a(5)
        cpu.pc = base + _signed16(offset & ~1) & ADDRESS_MASK


def _signed16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v
