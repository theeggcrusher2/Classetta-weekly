"""
Memory Manager: heap zones holding nonrelocatable blocks (pointers) and
relocatable ones (handles, reached through master pointers).

Every block starts with the 8-byte header of the 24-bit Memory Manager:
a tag byte (block type in the top two bits, size correction in the low
nibble), the physical size in the next three bytes, then the relative
handle (relocatable) or the zone's address (nonrelocatable).  Code that
peeks at a header finds what it expects there.  The free list and block
table live on the Python side, and are read back from the headers when
a program has moved blocks itself (MacWrite 4.5 slides the heap's blocks
down to move a segment to the top before it loads it, setting their
master pointers as the Memory Manager would).  The free master pointers
are a list in the zone, as on a Mac: hFstFree in the zone record is the
first, and each holds the address of the next.  Excel 1.0 calls
MoreMasters with an empty list and its own moreMast, and keeps the block
of master pointers it finds there as its table of handles; then it adds
zcbFree, the free bytes the record counts, to what it could purge, to see
if it has room to start.

Blocks move to compact the heap when a program asks, with CompactMem or
MaxMem, and when an allocation doesn't fit otherwise (below).  Otherwise
a handle moves only when SetHandleSize can't grow it
in place, when MoveHHi moves it up, or to make room low in the zone, for a
nonrelocatable block that has no free block to go in or for ResrvMem:
then the handles in the lowest stretch of the zone that can hold it move
up out of its way, the zone growing for them if it must, so that
nonrelocatable blocks stay together at the bottom of the zone, as on a
Mac (Inside Macintosh II-12, II-39).  The Angelsoft adventures' p-System
counts on it: it takes the largest free block MaxMem finds to be the one
that ends the zone.  Mac programs already have to allow for all of
these.  A program's NewPtr makes room low that way even when a free
block higher up would hold it, though it doesn't grow the zone to do it:
Excel 1.5 jumps from the 128K block it asks for into its first segment
with 16-bit offsets.  macrt's own nonrelocatable blocks, and a locked
resource being read in, still go in the first free block that holds
them, where a Mac's Resource Manager makes room low for a locked
resource, so handles stay put more often here than on a Mac.  When an
allocation doesn't fit, a zone with room to grow (the application heap,
up to ApplLimit) grows, as far as it can
when even that isn't enough; failing that, the zone is compacted, and if
that isn't enough either, unlocked purgeable blocks are purged, lowest
address first, until it does: the master pointer becomes NIL and the
handle stays valid, as on a Mac.  "The Memory Manager first tries to
obtain the requested amount of space by compacting the heap; if
compaction fails to free the required amount of space, the Memory Manager
then purges the heap" (Inside Macintosh: Memory, "Heap Purging and
Compaction").  Last, the zone's grow-zone function, if it has one, is
called with the block's physical size, GZRootHnd holding the handle
being resized, which it mustn't move or purge (0 for a new block), and
for as long as it says it freed something the zone is compacted and
purged again (Inside Macintosh: Memory,
"MyGrowZone").  Fine Artist installs one with SetGrowZone: at the 1,792K
its SIZE prefers, on a IIx at 8 bits, its NEXT button needed two calls,
for blocks of 20,076 and 26,620 bytes, which said they freed 25,966 and
157,490 bytes.
"""

from __future__ import annotations

import bisect
import struct
import threading
from typing import Callable

from ..memory import RAM

noErr = 0
memFullErr = -108
nilHandleErr = -109
memWZErr = -111
memPurErr = -112
memLockedErr = -117

HEADER = 8
MIN_BLOCK = 12
_LONG = struct.Struct(">I")
ZONE_HEADER = 52            # the zone record; bkLim, hFstFree, zcbFree, gzProc, moreMast kept
H_FST_FREE = 8              # hFstFree's offset in the record
ZCB_FREE = 12               # zcbFree's
GZ_PROC = 16                # gzProc's
MORE_MAST = 20              # moreMast's
MASTERS_PER_BLOCK = 32      # the system heap's moreMast
APPL_MASTERS = 64           # the application heap's (Inside Macintosh II-22)
GROW_STEP = 0x1000          # the least a zone grows by
GROW_ZONE_CALLS = 1024      # the most calls of a grow-zone function for one block

FREE, NONRELOCATABLE, RELOCATABLE = 0, 1, 2

# A handle's flags.  In 24-bit mode they live in the master pointer's
# high byte, and we do this to maintain compatibility with apps that
# (inadvisably) look at them directly.
# In 32-bit mode they can't be there as all bits are used for
# the address, so we store them separately in _xflags.
LOCKED = 0x80
PURGEABLE = 0x40
RESOURCE = 0x20


class Heap:
    def __init__(self, mem: RAM, start: int, end: int, limit: int | None = None,
                 on_grow: Callable[[int], None] | None = None, align: int = 4,
                 more_mast: int = MASTERS_PER_BLOCK):
        """A zone from start to end.  With a limit it can grow that far,
        and on_grow hears of each new end (the application heap's HeapEnd).
        Its blocks' sizes are multiples of align: 2 on a 68000, 4 on a
        68020 and later (Inside Macintosh: Memory, "Organization of
        Memory").  Macintosh Pascal checksums its own code segment to the
        end of its block as a Mac Plus has it."""
        self.mem = mem
        self.mmu_32_bit = mem.system.mmu_32_bit
        self._xflags: dict[int, int] = {}     # 32-bit mode: h -> its flags
        self.align = align
        self.start = start
        self.end = end & ~3
        self.limit = self.end if limit is None else max(limit & ~3, self.end)
        self.on_grow = on_grow
        # Calls the grow-zone function in the zone record's gzProc, for a
        # block of cbNeeded bytes, the handle it mustn't move or purge (or
        # 0) given too: (proc, cbNeeded, handle) -> what it says it freed.
        # The program that installs one sets it.
        self.grow_zone: Callable[[int, int, int], int] | None = None
        self._in_grow_zone = False
        self.error = noErr
        self._lock = threading.RLock()
        first = (start + ZONE_HEADER + 3) & ~3
        self._free: list[list[int]] = []                # [address, size], sorted
        self._blocks: dict[int, list[int]] = {}         # data address -> [physical, logical, type]
        self._handles: set[int] = set()
        self._masters: set[int] = set()                 # every master pointer MoreMasters made
        self._borrowed: set[int] = set()               # master pointers a program made itself
        self._bk_seen = self.end                        # the bkLim last read back
        mem.fill(start, ZONE_HEADER)
        mem.w32(start, self.end)                        # bkLim
        # Multiplan calls MoreMasters until it has as many master pointers
        # as it wants, counting moreMast for each call.
        mem.w16(start + MORE_MAST, more_mast)
        self._add_free(first, self.end - first)

    def contains(self, addr: int) -> bool:
        return self.start <= (addr & self.mem.system.addr_mask) < self.end

    def blocks(self) -> dict[int, int]:
        """Each block allocated, by its data's address: its logical size."""
        with self._lock:
            return {data: size for data, (_, size, _) in self._blocks.items()}

    def free_bytes(self, grow: bool = True) -> int:
        """What's free, counting what the zone could still grow by, or, if
        grow is False, only its free blocks: zcbFree, which FreeMem returns."""
        return sum(n for _, n in self._free) + (self.room_to_grow() if grow else 0)

    def max_block(self, grow: bool = True) -> int:
        """MaxBlock: the largest block that could be allocated now, the
        zone growing for it if it may (programs size their buffers by
        this before they call MaxApplZone), or not if grow is False."""
        sizes = [n for _, n in self._free]
        if grow and self.room_to_grow():
            last = self._free[-1] if self._free and sum(self._free[-1]) == self.end else None
            sizes.append((last[1] if last else 0) + self.room_to_grow())
        return max(max(sizes, default=0) - HEADER, 0)

    def room_to_grow(self) -> int:
        return self.limit - self.end

    def grow(self, end: int) -> None:
        """Move the zone's end up toward end, as far as its limit allows
        (MaxApplZone, and what an allocation does when it needs room)."""
        with self._lock:
            self._check_free()
            end = min((end + 3) & ~3, self.limit)       # what a block needs, at least
            joins = bool(self._free) and sum(self._free[-1]) == self.end
            if end <= self.end or (end - self.end < MIN_BLOCK and not joins):
                return
            old, self.end = self.end, end
            self.mem.w32(self.start, end)               # bkLim
            self._add_free(old, end - old)
            if self.on_grow is not None:
                self.on_grow(end)

    def grow_for(self, size: int) -> None:
        """Grow the zone, if it may, by what its last free block lacks for a
        block of size; or as far as it can go when that's not enough, as a
        Mac's does.  Before MaxApplZone, programs asked for more than there
        was to get all of it (MacWrite asks for a megabyte, then counts
        what's free)."""
        with self._lock:
            last = self._free[-1] if self._free and sum(self._free[-1]) == self.end else None
            need = self._physical(size) - (last[1] if last else 0)
            self.grow(self.end + max(need, GROW_STEP))

    def set_limit(self, limit: int) -> None:
        """How far the zone may grow; never below where it already ends."""
        with self._lock:
            self.limit = max(limit & ~3, self.end)

    # =====================================================================
    # Pointers
    # =====================================================================
    def new_ptr(self, size: int, clear: bool = False, low: bool = False) -> int:
        """A nonrelocatable block; clear says the caller needs zeros, as
        NewPtrClear does, though every new block starts as zeros (_fit).
        low makes room for it as low in the zone as the handles that can
        move let it go, though a free block higher up would hold it."""
        with self._lock:
            if low and size >= 0:
                self._room_low(self._physical(size), may_grow=False)
            return self._result(self._alloc(size, NONRELOCATABLE, self.start))

    def dispose_ptr(self, p: int) -> None:
        with self._lock:
            if self._kind(p) != NONRELOCATABLE:
                self.error = memWZErr
                return
            self._release(p & self.mem.system.addr_mask)
            self.error = noErr

    def is_ptr(self, p: int) -> bool:
        with self._lock:
            return self._kind(p) == NONRELOCATABLE

    def ptr_size(self, p: int) -> int:
        with self._lock:
            if self._kind(p) != NONRELOCATABLE:
                self.error = memWZErr
                return 0
            self.error = noErr
            return self._block(p & self.mem.system.addr_mask)[1]

    def set_ptr_size(self, p: int, size: int) -> bool:
        with self._lock:
            if self._kind(p) != NONRELOCATABLE:
                self.error = memWZErr
                return False
            ok = self._resize_in_place(p & self.mem.system.addr_mask, size)
            self.error = noErr if ok else memFullErr
            return ok

    # =====================================================================
    # Handles
    # =====================================================================
    def new_handle(self, size: int, clear: bool = False) -> int:
        """A relocatable block; clear as for new_ptr."""
        with self._lock:
            mp = self._master()
            if not mp:
                return 0
            data = self._alloc(size, RELOCATABLE, mp - self.start)
            if not data:
                self._free_master(mp)
                self.error = memFullErr
                return 0
            self.mem.w32(mp, data)
            self._handles.add(mp)
            self.error = noErr
            return mp

    def new_handle_with(self, data: bytes) -> int:
        """A relocatable block holding a copy of data; 0, with memFullErr,
        if there isn't room."""
        h = self.new_handle(len(data))
        if h:
            self.mem.write(self.deref(h), data)
        return h

    def new_empty_handle(self) -> int:
        with self._lock:
            mp = self._master()
            if mp:
                self.mem.w32(mp, 0)
                self._handles.add(mp)
                self.error = noErr
            return mp

    def is_handle(self, h: int) -> bool:
        return (h & self.mem.system.addr_mask) in self._handles

    def deref(self, h: int) -> int:
        return self.mem.r32(h) & self.mem.system.addr_mask

    # -- a master pointer's flags ------------------------------------------
    def _flags(self, h: int) -> int:
        if self.mmu_32_bit:
            return self._xflags.get(h, 0)
        else:
            return self.mem.r8(h)

    def _put_flags(self, h: int, flags: int) -> None:
        if self.mmu_32_bit:
            self._xflags[h] = flags & 0xE0
        else:
            self.mem.w8(h, flags & 0xE0)

    def _put_master(self, h: int, flags: int, addr: int) -> None:
        self.mem.w32(h, addr if self.mmu_32_bit else (flags << 24 | addr))

    def _drop_flags(self, h: int) -> None:
        self._xflags.pop(h, None)

    def dispose_handle(self, h: int) -> None:
        with self._lock:
            h &= self.mem.system.addr_mask
            if h not in self._handles:
                self.error = nilHandleErr if h == 0 else memWZErr
                return
            data = self.deref(h)
            freed = not data or self._release_if_block(data)
            self._handles.remove(h)
            self._drop_flags(h)
            self.mem.w32(h, 0)
            if h in self._borrowed:
                self._borrowed.discard(h)       # the program's, not the zone's to hand out
            else:
                self._free_master(h)
            self.error = noErr if freed else memWZErr

    def empty_handle(self, h: int) -> None:
        """EmptyHandle: free the block, keep the handle; its master pointer
        becomes NIL."""
        with self._lock:
            h &= self.mem.system.addr_mask
            if h not in self._handles:
                self.error = nilHandleErr if h == 0 else memWZErr
                return
            data = self.deref(h)
            if data:
                if self._flags(h) & LOCKED:
                    self.error = memPurErr
                    return
                self._release(data)
            self.mem.w32(h, 0)
            self._drop_flags(h)
            self.error = noErr

    def realloc_handle(self, h: int, size: int) -> bool:
        """ReallocHandle: a new block of the given size for h; the old
        contents (if any) are gone.  h may be a master pointer the program
        made itself, anywhere in the zone, so long as it's NIL: a Mac
        takes any (Microsoft Word 1.0 makes one in its first segment for
        the block it loads the rest of itself into)."""
        with self._lock:
            h &= self.mem.system.addr_mask
            borrowed = h not in self._handles
            if borrowed and (not h or h & 1 or not self.contains(h) or self.deref(h)):
                self.error = nilHandleErr if h == 0 else memWZErr
                return False
            flags = self._flags(h)
            data = self.deref(h)
            if data:
                if flags & LOCKED:
                    self.error = memLockedErr
                    return False
                self._release(data)
                self.mem.w32(h, 0)
            new = self._alloc(size, RELOCATABLE, h - self.start)
            if not new:
                self.error = memFullErr
                return False
            self._put_master(h, flags, new)
            if borrowed:
                self._handles.add(h)
                self._borrowed.add(h)
            self.error = noErr
            return True

    def is_empty(self, h: int) -> bool:
        """A purged or emptied handle: valid, but its master pointer is NIL."""
        return self.is_handle(h) and not self.deref(h)

    def handle_size(self, h: int) -> int:
        with self._lock:
            data = self._handle_data(h)
            return self._block(data)[1] if data else 0

    def set_handle_size(self, h: int, size: int) -> bool:
        """SetHandleSize.  A block with no room to grow in place moves, if
        it's unlocked, before the blocks after it are moved out of its way:
        SetHandleSize "might need to move the relocatable block", and it's
        a locked one whose growth the blocks around it can stop (Inside
        Macintosh: Memory).  Mac'N Med Engine unloads the segment it's
        running in, then grows its Apple menu, just below it, with
        AppendResMenu."""
        with self._lock:
            data = self._handle_data(h)
            if not data:
                return False
            flags = self._flags(h)
            if self._resize_in_place(data, size, clear=bool(flags & LOCKED)):
                self.error = noErr
                return True
            if flags & LOCKED:
                self.error = memFullErr
                return False
            link = (h & self.mem.system.addr_mask) - self.start
            new = self._fit(size, RELOCATABLE, link)
            if not new and self.room_to_grow():
                self.grow_for(size)
                new = self._fit(size, RELOCATABLE, link)
            if not new and self._resize_in_place(data, size):
                self.error = noErr
                return True
            new = new or self._alloc(size, RELOCATABLE, link)
            if not new:
                self.error = memFullErr
                return False
            data = self.deref(h)                # moved, if the zone was compacted for new
            if link + self.start not in self._handles or data not in self._blocks:
                # A grow-zone function purged or disposed of it after all.
                self._release(new)
                self.error = memFullErr
                return False
            self.mem.move(data, new, min(self._block(data)[1], size))
            self._release(data)
            self._put_master(h, flags, new)
            self.error = noErr
            return True

    def recover_handle(self, p: int) -> int:
        """RecoverHandle: the handle whose block starts at p."""
        with self._lock:
            if self._kind(p) != RELOCATABLE:
                self.error = memWZErr
                return 0
            self.error = noErr
            return self.start + self.mem.r32((p & self.mem.system.addr_mask) - 4)

    def get_state(self, h: int) -> int:
        return self._flags(h)

    def set_state(self, h: int, flags: int) -> None:
        if not self._empty(h):
            self._put_flags(h, flags)

    def lock(self, h: int) -> None:
        self._set_flag(h, LOCKED, True)

    def unlock(self, h: int) -> None:
        self._set_flag(h, LOCKED, False)

    def set_purgeable(self, h: int, on: bool = True) -> None:
        """HPurge / HNoPurge"""
        self._set_flag(h, PURGEABLE, on)

    def _set_flag(self, h: int, flag: int, on: bool) -> None:
        if not self._empty(h):
            flags = self._flags(h)
            self._put_flags(h, (flags | flag) if on else (flags & ~flag))

    def _empty(self, h: int) -> bool:
        """Whether h's master pointer is NIL, with nilHandleErr if it is:
        an empty handle has no flags to set, and setting them would make
        its NIL a pointer (Inside Macintosh II-40 gives nilHandleErr, "NIL
        master pointer", for HLock, HUnlock, HPurge and HNoPurge)."""
        if self.deref(h):
            return False
        self.error = nilHandleErr
        return True

    def move_high(self, h: int) -> None:
        """MoveHHi: move h's block up toward the top of the zone, until it
        comes to a nonrelocatable or locked block or the zone's end (Inside
        Macintosh II-44): into the highest free block below that holds it.
        Locked there, it's out of the way of the blocks allocated low
        (Stratego moves its pictures up before it draws them into a
        300K pixel map)."""
        with self._lock:
            h &= self.mem.system.addr_mask
            data = self.deref(h) if h in self._handles else 0
            if not data or data not in self._blocks:
                self.error = nilHandleErr
                return
            self.error = noErr
            if self._flags(h) & LOCKED:
                self.error = memLockedErr
                return
            spans = self._spans_up_from(data)
            if spans is None:
                self._check_free()
                spans = self._spans()
            block = self._blocks.get(data)
            if block is None:
                return
            size = block[1]
            need = self._physical(size)
            best = None
            for addr, n, what in spans or ():
                if what < 0 and addr > data:
                    break
                if what == 0 and addr > data and (n == need or n - need >= MIN_BLOCK):
                    best = addr, n
            if best is None:
                return
            addr, n = best
            i = bisect.bisect_left(self._free, [addr, 0])
            if n == need:
                del self._free[i]
            else:
                self._free[i][1] = n - need
                self._write_header(addr, FREE, n - need, 0, 0)
            self._counted_free(-need)
            at = addr + n - need
            self._write_header(at, RELOCATABLE, need, size, h - self.start)
            self.mem.move(data, at + HEADER, size)
            self._put_master(h, self._flags(h), at + HEADER)
            phys = self._blocks.pop(data)[0]
            self._blocks[at + HEADER] = [need, size, RELOCATABLE]
            self._add_free(data - HEADER, phys)

    def compact(self) -> None:
        """CompactMem: slide the unlocked relocatable blocks down over the
        free space below them, as far as the nonrelocatable and locked
        blocks they come to allow, leaving it together above them."""
        with self._lock:
            if not self._rescan():
                return
            mem = self.mem
            blocks: dict[int, list[int]] = {}
            gaps = []
            to = (self.start + ZONE_HEADER + 3) & ~3    # where the next block that can move goes
            for data in sorted(self._blocks):
                block, start = self._blocks[data], data - HEADER
                h = self.start + mem.r32(data - 4)
                if (block[2] == RELOCATABLE and h in self._handles and self.deref(h) == data
                        and not self._flags(h) & LOCKED):
                    if to < start:
                        mem.move(start, to, block[0])
                        self._put_master(h, self._flags(h), to + HEADER)
                    start = to
                elif to < start:
                    gaps.append([to, start - to])
                blocks[start + HEADER] = block
                to = start + block[0]
            if to < self.end:
                gaps.append([to, self.end - to])
            self._blocks, self._free = blocks, gaps
            for addr, size in gaps:
                self._write_header(addr, FREE, size, 0, 0)
            mem.w32(self.start + ZCB_FREE, sum(n for _, n in gaps))

    def purge(self, h: int) -> None:
        """Purge one handle now, if it may be purged."""
        with self._lock:
            flags = self._flags(h)
            if self.is_handle(h) and flags & PURGEABLE and not flags & LOCKED:
                self.empty_handle(h)

    def purge_for(self, size: int | None = None) -> bool:
        """PurgeMem: purge the unlocked purgeable blocks, lowest first, until
        a block of size could be had without moving any, or every one of
        them if size is None (MaxMem); whether one could."""
        with self._lock:
            for h in self._purgeable(0):
                if size is not None and self.max_block() >= size:
                    break
                self.empty_handle(h)
            return size is None or self.max_block() >= size

    def purge_space(self) -> tuple[int, int]:
        """PurgeSpace: the space a purge of every unlocked purgeable block
        would give, counting what's already free and what the zone could
        still grow by, in all and in one piece, without purging anything
        (Inside Macintosh IV, Memory Manager)."""
        with self._lock:
            self._check_free()
            runs = sorted([(addr, n) for addr, n in self._free]
                          + [(self.deref(h) - HEADER, self._blocks[self.deref(h)][0])
                             for h in self._purgeable(0)])
            best = run = at = 0
            for addr, n in runs:
                run = run + n if addr == at else n
                at = addr + n
                best = max(best, run)
            grow = self.room_to_grow()
            if grow:
                best = max(best, (run if at == self.end else 0) + grow)
            return sum(n for _, n in runs) + grow, max(best - HEADER, 0)

    # =====================================================================
    # Internals
    # =====================================================================
    def _result(self, addr: int) -> int:
        self.error = noErr if addr else memFullErr
        return addr

    def _kind(self, p: int) -> int | None:
        try:
            return self._block(p & self.mem.system.addr_mask)[2]
        except KeyError:
            return None

    def _block(self, data: int) -> list[int]:
        """[physical, logical, type] of the block whose data is at data,
        read back from the headers if a program has moved or resized it."""
        block = self._blocks.get(data)
        if ((block is None or self.mem.r32(data - HEADER) & 0xFFFFFF != block[0])
                and (self.contains(data) or self._bk_lim() != self.end) and self._rescan()):
            block = self._blocks.get(data)
        if block is None:
            raise KeyError(data)
        return block

    def _physical(self, size: int) -> int:
        return max((size + HEADER + self.align - 1) & -self.align, MIN_BLOCK)

    def _check_free(self) -> None:
        """Read the zone back from its headers if a program has written
        over a free block's or moved bkLim, before what's free is merged or
        used: Macintosh Pascal moves its handles to the top of the zone
        itself, taking the room from the free block below them, and PackIt
        III, taking a Mac II's ROM85 for an old ROM's, grows the zone itself
        as programs for the 64K ROM did, with a free block where it ended
        and bkLim and HeapEnd past that."""
        bk_lim = self._bk_lim()
        r32 = self._longs()
        if ((bk_lim != self.end and bk_lim != self._bk_seen)
                or any(r32(addr) != size for addr, size in self._free)):
            self._bk_seen = bk_lim
            self._rescan()

    def _bk_lim(self) -> int:
        return self.mem.r32(self.start) & self.mem.system.addr_mask

    def _longs(self) -> Callable[[int], int]:
        """A reader of the zone's long words, for the checks that read
        hundreds of its headers at a time: straight from RAM's bytes when
        the zone is in RAM with no mirror over it, so its addresses are
        where its bytes are."""
        mem = self.mem
        if self.end <= mem.size and (mem.mirror_hi <= self.start or self.end <= mem.mirror_lo):
            b, unpack = mem._b, _LONG.unpack_from
            return lambda addr: unpack(b, addr)[0]
        return mem.r32

    def _rescan(self) -> bool:
        """Read the zone's blocks and free space back from their headers.
        False, with nothing changed, if they don't make up the zone.  The
        zone ends at its bkLim if a program has moved that and the blocks
        reach it: the Angelsoft adventures' p-System makes a block of its
        own at the stack's end of the free space above the heap, then moves
        bkLim and HeapEnd past it, and the free block below up to it."""
        mem = self.mem
        for end in dict.fromkeys((self._bk_lim(), self.end)):
            found = self._walk(end)
            if found:
                break
        else:
            return False
        if end != self.end:
            self.end, self.limit = end, max(self.limit, end)
        self._free, self._blocks = found
        for addr, size in self._free:
            self._write_header(addr, FREE, size, 0, 0)
        mem.w32(self.start + ZCB_FREE, sum(n for _, n in self._free))
        return True

    def _walk(self, end: int) -> tuple[list[list[int]], dict[int, list[int]]] | None:
        """The free space and blocks from the zone's first block to end, if
        their headers make it up exactly."""
        mem = self.mem
        free: list[list[int]] = []
        blocks: dict[int, list[int]] = {}
        addr = (self.start + ZONE_HEADER + 3) & ~3
        if end < addr or end & 1:
            return None
        while addr < end:
            word = mem.r32(addr)
            kind, phys = word >> 30, word & 0xFFFFFF
            if kind > RELOCATABLE or phys < HEADER or phys & 1 or addr + phys > end:
                return None
            if kind != FREE:
                blocks[addr + HEADER] = [phys, phys - HEADER - (word >> 24 & 15), kind]
            elif free and sum(free[-1]) == addr:
                free[-1][1] += phys
            else:
                free.append([addr, phys])
            addr += phys
        return free, blocks

    def _handle_data(self, h: int) -> int:
        h &= self.mem.system.addr_mask
        if h not in self._handles:
            self.error = nilHandleErr if h == 0 else memWZErr
            return 0
        data = self.deref(h)
        if not data:
            self.error = nilHandleErr
        else:
            self.error = noErr
        return data

    def more_masters(self) -> bool:
        """MoreMasters: a block of moreMast master pointers, put on the
        front of the free list in order, so the list starts at the last."""
        with self._lock:
            count = self.mem.r16(self.start + MORE_MAST)
            count = count if 0 < count < 0x8000 else MASTERS_PER_BLOCK
            block = self._alloc(4 * count, NONRELOCATABLE, self.start)
            if not block:
                self.error = memFullErr
                return False
            for mp in range(block, block + 4 * count, 4):
                self._masters.add(mp)
                self._free_master(mp)
            self.error = noErr
            return True

    def _master(self) -> int:
        """A master pointer from the front of the free list, which gets
        another block when it's empty.  A list a program has written over
        (it stored through a handle it had disposed of) is let go."""
        head = self.start + H_FST_FREE
        mp = self.mem.r32(head) & self.mem.system.addr_mask
        if mp and not self._may_be_free(mp):
            mp = 0
        if not mp:
            self.mem.w32(head, 0)
            if not self.more_masters():
                return 0
            mp = self.mem.r32(head)
        nxt = self.mem.r32(mp) & self.mem.system.addr_mask
        self.mem.w32(head, nxt if self._may_be_free(nxt) else 0)
        return mp

    def _may_be_free(self, mp: int) -> bool:
        return mp in self._masters and mp not in self._handles

    def _free_master(self, mp: int) -> None:
        head = self.start + H_FST_FREE
        self.mem.w32(mp, self.mem.r32(head))
        self.mem.w32(head, mp)

    def _write_header(self, phys_addr: int, kind: int, phys: int, logical: int, link: int) -> None:
        correction = min(phys - HEADER - logical, 15) if kind != FREE else 0
        self.mem.w32(phys_addr, (((kind << 6) | correction) << 24) | phys)
        self.mem.w32(phys_addr + 4, link)

    def _add_free(self, addr: int, size: int) -> None:
        """Return a range to the free list, merging it with its neighbours."""
        self._counted_free(size)
        i = bisect.bisect_left(self._free, [addr, 0])
        if i < len(self._free) and self._free[i][0] == addr + size:
            size += self._free.pop(i)[1]
        if i > 0 and self._free[i - 1][0] + self._free[i - 1][1] == addr:
            i -= 1
            addr = self._free[i][0]
            size += self._free.pop(i)[1]
        self._free.insert(i, [addr, size])
        self._write_header(addr, FREE, size, 0, 0)

    def _counted_free(self, change: int) -> None:
        """Add change to zcbFree, as the Memory Manager does, over what a
        program may have done to it itself."""
        at = self.start + ZCB_FREE
        self.mem.w32(at, (self.mem.r32(at) + change) & 0xFFFFFFFF)

    def reserve(self, size: int) -> bool:
        """ResrvMem: a free block for size as low in the zone as one can be
        had (Inside Macintosh II-39)."""
        with self._lock:
            return size >= 0 and self._room_low(self._physical(size))

    def _alloc(self, size: int, kind: int, link: int) -> int:
        if size < 0:
            return 0
        data = self._fit(size, kind, link)
        if data:
            return data
        if kind == NONRELOCATABLE and self._room_low(self._physical(size)):
            data = self._fit(size, kind, link)
            if data:
                return data
        if self.room_to_grow():
            self.grow_for(size)
            data = self._fit(size, kind, link)
            if data:
                return data
        # Compact and purge; then, for as long as the zone's grow-zone
        # function says it freed something, compact and purge again (one
        # that always says so is asked only so many times).
        phys = self._physical(size)
        owner = self.start + link if kind == RELOCATABLE else 0
        data = self._make_room(size, kind, link, phys, owner)
        protected = owner if owner in self._handles else 0      # not a new handle's
        for _ in range(GROW_ZONE_CALLS):
            if data or not self._call_grow_zone(phys, protected):
                break
            data = self._make_room(size, kind, link, phys, owner)
        return data

    def _make_room(self, size: int, kind: int, link: int, phys: int, owner: int) -> int:
        """Compact, then purge until a block of size fits, never owner's,
        compacting again whenever what's purged adds to what's free."""
        data, compacted = self._compacted_fit(size, kind, link, phys, -1)
        if data:
            return data
        for h in self._purgeable(owner):
            self.empty_handle(h)
            data = self._fit(size, kind, link)
            if not data:
                data, compacted = self._compacted_fit(size, kind, link, phys, compacted)
            if data:
                return data
        return 0

    def _call_grow_zone(self, phys: int, protected: int) -> bool:
        """Ask the zone's grow-zone function, if it has one, for room for a
        block of phys bytes, the handle protected (or 0) not to be moved or
        purged: whether it freed any.  Not while it's already running: it
        isn't to allocate memory itself (Inside Macintosh: Memory,
        "MyGrowZone"), and one that does gets no second call."""
        proc = self.mem.r32(self.start + GZ_PROC) & self.mem.system.addr_mask
        if not proc or self.grow_zone is None or self._in_grow_zone:
            return False
        self._in_grow_zone = True
        try:
            return self.grow_zone(proc, phys, protected) != 0
        finally:
            self._in_grow_zone = False

    def _compacted_fit(self, size: int, kind: int, link: int, phys: int,
                       compacted: int) -> tuple[int, int]:
        """A block of size, kind and link, made after compacting the zone,
        if what's free adds up to its phys bytes and is more than was free
        when it was last compacted for the block (compacted, or -1); 0 for
        the block if there's still no room.  And what was free when the
        zone was last compacted."""
        free = sum(n for _, n in self._free)
        if free < phys or free <= compacted:
            return 0, compacted
        self.compact()
        if kind == NONRELOCATABLE:
            self._room_low(phys, may_grow=False)
        return self._fit(size, kind, link), free

    def _purgeable(self, owner: int) -> list[int]:
        found = []
        for h in self._handles:
            data = self.mem.r32(h) & self.mem.system.addr_mask
            flags = self._flags(h)
            if data in self._blocks and h != owner and flags & PURGEABLE and not flags & LOCKED:
                found.append((data, h))
        return [h for _, h in sorted(found)]

    def _room_low(self, phys: int, may_grow: bool = True) -> bool:
        """Make a free block of phys bytes as low in the zone as one can be
        had, by moving the unlocked relocatable blocks in the way up into
        free blocks above, and growing the zone for them if there's no other
        room for them: the Memory Manager makes room for a nonrelocatable
        block "near the bottom of the zone, by moving other blocks upward,
        expanding the zone, or purging blocks from it" (Inside Macintosh
        II-12).  The stretch it takes runs from a block that can't move (or
        the zone's start) over free and movable blocks; the first one whose
        blocks all find room above it, without growing if any does.  When a
        free block ends it, it takes only what it needs of that block, and
        the rest is room for the blocks that move, as are the free blocks
        past it: Excel 1.5 jumps from the 128K block it asks for into its
        first segment with 16-bit offsets, so the block has to start within
        32K of the segment.  False if there's none."""
        self._check_free()
        spans = self._spans()
        if spans is None:
            return False
        for grow in (False, True):
            if grow and not (may_grow and self.room_to_grow()):
                break
            i = 0
            while i < len(spans):
                if spans[i][2] < 0:
                    i += 1
                    continue
                total, j = 0, i
                while j < len(spans) and total < phys and spans[j][2] >= 0:
                    total += spans[j][1]
                    j += 1
                if total < phys:
                    i = j                   # a block that can't move, or the end
                    continue
                lo, (last, n, what) = spans[i][0], spans[j - 1]
                cut = max(lo + phys, last + MIN_BLOCK)
                split = what == 0 and last + n - cut >= MIN_BLOCK
                if split:
                    above = [(cut, last + n - cut, 0)] + spans[j:]
                    hi = cut                # the rest of the free block is above
                else:
                    above = spans[j:]
                    hi = last + n
                movers = [s for s in spans[i:j] if s[2] > 0]
                stuck, growth = self._plan(movers, above, grow)
                if stuck is None:
                    if split:
                        self._split_free(last, cut)
                    return self._move_up(lo, hi, movers, growth)
                i = spans.index(stuck) + 1
        return False

    def _split_free(self, addr: int, at: int) -> None:
        """Make the free block at addr two, the second starting at at."""
        i = bisect.bisect_left(self._free, [addr, 0])
        n = self._free[i][1]
        self._free[i:i + 1] = [[addr, at - addr], [at, addr + n - at]]
        self._write_header(addr, FREE, at - addr, 0, 0)
        self._write_header(at, FREE, addr + n - at, 0, 0)

    def _spans(self, rescanned: bool = False) -> list[tuple[int, int, int]] | None:
        """The zone's free and allocated blocks in address order, as
        (address, physical size, what): what is 0 for a free block, the data
        address of a block that can move, -1 for one that can't.  None if
        their headers don't make up the zone."""
        mem = self.mem
        spans = [(addr, n, 0) for addr, n in self._free]
        for data, (phys, _, kind) in self._blocks.items():
            if mem.r32(data - HEADER) & 0xFFFFFF != phys:
                return None if rescanned or not self._rescan() else self._spans(rescanned=True)
            h = self.start + mem.r32(data - 4)
            movable = (kind == RELOCATABLE and h in self._handles and self.deref(h) == data
                       and not self._flags(h) & LOCKED)
            spans.append((data - HEADER, phys, data if movable else -1))
        spans.sort()
        at = (self.start + ZONE_HEADER + 3) & ~3
        for addr, n, _ in spans:
            if addr != at:
                return None
            at += n
        return spans if at == self.end else None

    def _spans_up_from(self, data: int) -> list[tuple[int, int, int]] | None:
        """What _spans gives from the block whose data is at data up to the
        first block above it that can't move, or the zone's end, read only
        that far: MoveHHi looks no further, and GolfMaster! moves a handle
        up some 470 times a second in a zone of 800 blocks.  None if the
        headers there, or that of the free block just below that the
        block's room would join, aren't as the zone has them, or bkLim has
        moved: what _check_free and _spans would find, for the part of the
        zone MoveHHi uses."""
        mem, blocks, free = self.mem, self._blocks, self._free
        bk_lim = self._bk_lim()
        if bk_lim != self.end and bk_lim != self._bk_seen:
            return None
        r32 = self._longs()
        at = start = data - HEADER
        f = bisect.bisect_left(free, [at, 0])
        if f and sum(free[f - 1]) == at and r32(free[f - 1][0]) != free[f - 1][1]:
            return None
        spans: list[tuple[int, int, int]] = []
        while at < self.end:
            if f < len(free) and free[f][0] == at:
                n = free[f][1]
                if r32(at) != n:
                    return None
                spans.append((at, n, 0))
                f += 1
            else:
                block, word = blocks.get(at + HEADER), r32(at)
                if block is None or word & 0xFFFFFF != block[0] or word >> 30 != block[2]:
                    return None
                n, h = block[0], self.start + r32(at + 4)
                movable = (block[2] == RELOCATABLE and h in self._handles
                           and self.deref(h) == at + HEADER and not mem.r8(h) & LOCKED)
                spans.append((at, n, at + HEADER if movable else -1))
                if not movable and at != start:
                    return spans
            at += n
        return spans

    def _plan(self, movers: list[tuple[int, int, int]], above: list[tuple[int, int, int]],
              grow: bool) -> tuple[tuple[int, int, int] | None, int]:
        """Whether the movers fit, first fit, in the free blocks above, and
        how much the zone must grow for those that don't, if it may: (None,
        growth), or (the first mover that can't go, 0)."""
        room = [n for _, n, what in above if what == 0]
        growth = 0
        for mover in movers:
            need = self._physical(self._blocks[mover[2]][1])
            for i, n in enumerate(room):
                if n >= need:
                    room[i] = n - need if n - need >= MIN_BLOCK else 0
                    break
            else:
                if not grow or growth + need > self.room_to_grow():
                    return mover, 0
                growth += need
        return None, growth

    def _move_up(self, lo: int, hi: int, movers: list[tuple[int, int, int]], growth: int) -> bool:
        """Move the movers, which are between lo and hi, to free blocks above
        hi, growing the zone by growth first; then lo to hi is free."""
        mem = self.mem
        held = [f for f in self._free if lo <= f[0] < hi]      # kept out, so nothing goes there
        self._free = [f for f in self._free if not lo <= f[0] < hi]
        self._counted_free(-sum(n for _, n in held))
        if growth:
            self.grow(self.end + max(growth, GROW_STEP))
        moved = True
        for _, phys, data in movers:
            size = self._blocks[data][1]
            h = self.start + mem.r32(data - 4)
            flags = self._flags(h)
            new = self._fit(size, RELOCATABLE, h - self.start, above=hi)
            if not new:
                moved = False
                break
            mem.move(data, new, size)
            self._put_master(h, flags, new)
            del self._blocks[data]
            held.append([data - HEADER, phys])
        for at, n in held:
            self._add_free(at, n)
        return moved

    def _fit(self, size: int, kind: int, link: int, rescanned: bool = False,
             above: int = 0) -> int:
        """The first free block size fits in, made a block of kind.  Its
        bytes start as zeros, since programs read what they never wrote and
        first fit hands a block just freed straight back, which a Mac's
        Memory Manager often doesn't.  The Falcon launcher plays an
        animation table it fills only for the CLIP resources it finds, and
        "Falcon Scene" has none: over the music it had just freed, its blits
        went into the jump table.  Only free blocks from above on count."""
        phys = self._physical(size)
        for i, (addr, n) in enumerate(self._free):
            if n < phys or addr < above:
                continue
            if self.mem.r32(addr) != n and not rescanned and self._rescan():
                return self._fit(size, kind, link, rescanned=True, above=above)   # not free now
            rest = n - phys
            if rest < MIN_BLOCK:
                phys = n
                del self._free[i]
            else:
                self._free[i] = [addr + phys, rest]
                self._write_header(addr + phys, FREE, rest, 0, 0)
            self._write_header(addr, kind, phys, size, link)
            self._counted_free(-phys)
            data = addr + HEADER
            self._blocks[data] = [phys, size, kind]
            self.mem.fill(data, size)
            return data
        return 0

    def _release(self, data: int) -> None:
        self._check_free()
        phys = self._block(data)[0]
        del self._blocks[data]
        self._add_free(data - HEADER, phys)

    def _release_if_block(self, data: int) -> bool:
        """Free the block at data; False, with nothing freed, if there's no
        block there: the program wrote over the master pointer that led to
        it (Radical Castle, once it had crashed, over its GCOD 0's)."""
        try:
            self._release(data)
        except KeyError:
            return False
        return True

    def _clear_after(self, addr: int, room: int, rescanned: bool = False) -> bool:
        """Free the room bytes from addr by moving the unlocked relocatable
        blocks there elsewhere, or purging them when they may be and there's
        nowhere for them, as the Memory Manager does to grow a block in
        place: Macintosh Pascal grows a pointer before each run.  False if
        a block there can't go, or the zone ends first; none moves if the
        room has one that can't, as is found before any moves.  The blocks
        are read back from their headers first if a program has made its
        own in one of ours: Microsoft File reads its p-code file, laid out
        as blocks, over the one it gets with ReallocHandle, headers and all.
        The zone grows toward its limit for the room and for the blocks
        that move: Starter Accounts' BASIC runtime grows its locked program
        handle, an unlocked handle after it, for its workspace."""
        mem, end = self.mem, addr
        in_way = []
        last_free = False
        while end < addr + room:
            if end >= self.end:
                old = self.end
                self.grow(max(addr + room, self.end + GROW_STEP))
                return self.end > old and self._clear_after(addr, room, rescanned)
            i = bisect.bisect_left(self._free, [end, 0])
            last_free = i < len(self._free) and self._free[i][0] == end
            if last_free:
                end += self._free[i][1]
                continue
            data = end + HEADER
            block = self._blocks.get(data)
            if (block is None or mem.r32(end) & 0xFFFFFF != block[0]) and not rescanned:
                return self._rescan() and self._clear_after(addr, room, rescanned=True)
            if block is None or block[2] != RELOCATABLE:
                return False
            h = self.start + mem.r32(end + 4)
            if h not in self._handles or self.deref(h) != data or self._flags(h) & LOCKED:
                return False
            in_way.append((data, h))
            end += block[0]
        if not in_way:
            return True
        # Hold the free blocks in the room back, so nothing moves into it,
        # but for the part of the last one past the room, where the blocks
        # in the way can go: HyperCard 2.0 grows a pointer past a handle
        # with all the rest of the zone free after it.
        held = [list(f) for f in self._free if addr <= f[0] < end]
        self._free = [f for f in self._free if not addr <= f[0] < end]
        if last_free:
            first = held[-1][0]
            cut = max(addr + room, first + MIN_BLOCK)
            if end - cut >= MIN_BLOCK:
                self._free.insert(bisect.bisect_left(self._free, [cut, 0]), [cut, end - cut])
                self._write_header(cut, FREE, end - cut, 0, 0)
                held[-1][1], end = cut - first, cut
        self._counted_free(-sum(n for _, n in held))
        for data, h in in_way:
            phys, size, _ = self._blocks[data]
            flags = self._flags(h)
            new = self._fit(size, RELOCATABLE, h - self.start)
            if not new and self.room_to_grow():
                self.grow_for(size)
                new = self._fit(size, RELOCATABLE, h - self.start)
            if new:
                mem.move(data, new, size)
                self._put_master(h, flags, new)
            elif flags & PURGEABLE:
                mem.w32(h, 0)
                self._drop_flags(h)
            else:
                break
            del self._blocks[data]
            held.append([data - HEADER, phys])
        for at, n in held:
            self._add_free(at, n)
        return sum(n for _, n in held) >= end - addr

    def _resize_in_place(self, data: int, size: int, clear: bool = True) -> bool:
        """Change a block's size where it is; clear, by moving the blocks
        after it out of the way if it must."""
        self._check_free()
        block = self._block(data)
        phys, _, kind = block
        start = data - HEADER
        link = self.mem.r32(start + 4)
        need = self._physical(size)
        if need > phys:
            # Grow into the free block right after this one, if there is one,
            # and the zone toward its limit when that's where the zone ends:
            # Pyramid of Peril, in MasterFORTH, grows its locked first segment
            # for room it keeps its stacks in.
            nxt = start + phys

            def free_after() -> int:
                i = bisect.bisect_left(self._free, [nxt, 0])
                return self._free[i][1] if i < len(self._free) and self._free[i][0] == nxt else 0
            avail = free_after()
            if phys + avail < need and clear and self._clear_after(nxt, need - phys):
                avail = free_after()
            if phys + avail < need and nxt + avail == self.end and self.room_to_grow():
                self.grow(max(start + need, self.end + GROW_STEP))
                avail = free_after()
            if phys + avail < need:
                return False
            self._free.pop(bisect.bisect_left(self._free, [nxt, 0]))
            self._counted_free(-avail)
            rest = phys + avail - need
            if rest < MIN_BLOCK:
                need += rest
            else:
                self._add_free(start + need, rest)
            block[0] = need
        elif phys - need >= MIN_BLOCK:
            self._add_free(start + need, phys - need)
            block[0] = need
        block[1] = size
        self._write_header(start, kind, block[0], size, link)
        return True
