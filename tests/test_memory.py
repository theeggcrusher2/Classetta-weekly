"""RAM, the Memory Manager and the low-memory globals."""

import random
import struct
import threading

import pytest
from guest import Guest, Processor

from macrt.m68k import dispatch, runtime
from macrt.m68k.glue_common import _heap
from macrt.memory import RAM, BusError
from macrt.toolbox import lowmem
from macrt.toolbox.memmgr import (
    FREE, GROW_ZONE_CALLS, GZ_PROC, H_FST_FREE, HEADER, LOCKED, MIN_BLOCK, MORE_MAST,
    NONRELOCATABLE, RELOCATABLE, ZCB_FREE, Heap,
    memFullErr, memLockedErr, memWZErr, nilHandleErr, noErr,
)
from macrt.toolbox.types import Point, Rect


def test_ram_is_big_endian_with_24_bit_addresses():
    m = RAM(0x10000)
    m.w32(0x100, 0x12345678)
    assert m.read(0x100, 4) == b"\x12\x34\x56\x78"
    assert m.r16(0x102) == 0x5678
    assert m.r32(0xFF000100) == 0x12345678       # the top byte is ignored
    m.w16(0x200, -2)
    assert (m.r16(0x200), m.r16s(0x200)) == (0xFFFE, -2)
    with pytest.raises(BusError):
        m.r32(0xFFFE)


def test_a_mirrored_range_is_another_way_to_the_memory_it_mirrors():
    """As a Mac Plus's RAM repeats through its 4 MB of addresses."""
    m = RAM(0x10000)
    m.mirror(0xE000, 0x10000, 0x2000)
    m.w32(0xF700, 0x12345678)
    assert m.r32(0x3700) == 0x12345678
    m.write(0x3800, b"abc")
    assert m.read(0xF800, 3) == b"abc" and m.r8(0xFF00F801) == ord("b")
    assert m.physical(0xF700) == 0x3700 and m.physical(0xD700) == 0xD700
    assert m.take_dirty() == [(0x3700, 0x3803)]     # the memory written, by where it is


def test_whats_written_is_noted_in_each_page_from_its_first_byte_to_its_last():
    m = RAM(0x10000)
    rng = random.Random(7)
    for _ in range(300):
        seen: dict[int, list[int]] = {}         # page: [first, last + 1], byte by byte
        for _ in range(rng.randrange(1, 6)):
            at = rng.randrange(0x10000)
            n = rng.choice([0, 1, 2, 4, rng.randrange(1, 0x100), rng.randrange(1, 0x4000)])
            n = min(n, 0x10000 - at)
            rng.choice([lambda: m.write(at, bytes(n)), lambda: m.fill(at, n, 0xAA),
                        lambda: m.move(rng.randrange(0x10000 - n + 1), at, n)])()
            for page in range(at >> 12, (at + n - 1 >> 12) + 1 if n else at >> 12):
                lo, hi = max(at, page << 12), min(at + n, page + 1 << 12)
                span = seen.setdefault(page, [lo, hi])
                span[0], span[1] = min(span[0], lo), max(span[1], hi)
        want: list[tuple[int, int]] = []
        for page in sorted(seen):
            lo, hi = seen[page]
            if want and want[-1][1] == lo:
                want[-1] = (want[-1][0], hi)
            else:
                want.append((lo, hi))
        assert m.take_dirty() == want


def test_bytes_are_compared_where_they_are():
    m = RAM(0x10000)
    m.mirror(0xE000, 0x10000, 0x2000)
    m.write(0x3000, b"abcdef")
    assert m.same(0x3000, b"abcdef") and m.same(0xF001, bytearray(b"bcd"))
    assert not m.same(0x3001, b"abcd") and m.same(0x3000, b"")
    assert m.read(0x2000, 0x8000)[0x1000:0x1006] == b"abcdef"
    with pytest.raises(BusError):
        m.same(0x1FFFE, b"abc")


def test_ram_toolbox_types():
    m = RAM(0x1000)
    m.w_pstr(0x10, b"Hello")
    assert m.read(0x10, 6) == b"\x05Hello" and m.r_pstr(0x10) == b"Hello"
    m.w_rect(0x20, Rect(1, 2, 3, 4))
    assert [m.r16s(0x20 + 2 * i) for i in range(4)] == [1, 2, 3, 4]
    m.w_point(0x30, Point(-5, 7))
    assert (m.r16s(0x30), m.r16s(0x32)) == (-5, 7)
    m.w_rect(0x20, Rect(0, 0, 32768, 40000))      # wraps, as the ROM's sums do
    assert [m.r16s(0x20 + 2 * i) for i in range(4)] == [0, 0, -32768, 40000 - 65536]
    m.w_point(0x30, Point(32768, -32769))
    assert (m.r16s(0x30), m.r16s(0x32)) == (-32768, 32767)
    m.write(0x40, b"abcdef")
    m.move(0x40, 0x42, 4)                         # overlapping
    assert m.read(0x40, 6) == b"ababcd"


def heap(size=0x4000):
    m = RAM(0x10000)
    return m, Heap(m, 0x1000, 0x1000 + size)


def test_pointer_blocks_and_headers():
    m, h = heap()
    free = h.free_bytes()
    p = h.new_ptr(10)
    assert h.contains(p) and p % 4 == 0 and h.error == noErr
    assert m.r8(p - HEADER) >> 6 == NONRELOCATABLE
    phys = m.r32(p - HEADER) & 0xFFFFFF
    assert phys == 20 and m.r8(p - HEADER) & 0x0F == phys - HEADER - 10
    assert m.r32(p - 4) == h.start                # nonrelocatable: the zone
    assert h.ptr_size(p) == 10
    h.dispose_ptr(p)
    assert h.free_bytes() == free                 # merged back together
    assert h.new_ptr(10) == p                     # and reused


def test_pointer_errors():
    _, h = heap()
    assert h.new_ptr(0x10000) == 0 and h.error == memFullErr
    h.dispose_ptr(0x1234)
    assert h.error == memWZErr


def test_handles_go_through_master_pointers():
    m, h = heap()
    hd = h.new_handle(16, clear=True)
    data = h.deref(hd)
    assert m.r32(hd) == data and h.handle_size(hd) == 16
    assert m.r8(data - HEADER) >> 6 == RELOCATABLE
    assert h.start + m.r32(data - 4) == hd        # the relative handle
    assert h.recover_handle(data) == hd
    h.lock(hd)
    assert m.r8(hd) & LOCKED and h.deref(hd) == data
    h.unlock(hd)
    h.dispose_handle(hd)
    assert h.new_handle(4) == hd                  # the master pointer is reused


def test_a_handle_can_be_made_holding_bytes_or_not_at_all():
    m, h = heap()
    hd = h.new_handle_with(b"bytes")
    assert h.handle_size(hd) == 5 and m.read(h.deref(hd), 5) == b"bytes" and h.error == noErr
    free = h.free_bytes()
    assert h.new_handle_with(bytes(0x10000)) == 0 and h.error == memFullErr
    assert h.free_bytes() == free


def test_the_heap_an_address_is_in(qapp):
    """The heap of a block, the application's for one in none (a handle
    whose master pointer is in a program's globals) to the glue that has
    somewhere to make a block for it."""
    g = Guest()
    system, app = g.system, g.process.heap
    own = system.sys_heap.new_handle(4)
    for addr, where in ((app.new_handle(4), app), (own, system.sys_heap),
                        (system.sys_heap.deref(own), system.sys_heap)):
        assert system.find_heap(addr) is system.heap_of(addr) is _heap(g, addr) is where
    globals_ = g.process.partition.a5 - 4
    for addr in (0, lowmem.ApplLimit, globals_):
        assert system.find_heap(addr) is None and _heap(g, addr) is app
        with pytest.raises(ValueError):
            system.heap_of(addr)


def test_disposing_a_handle_whose_master_pointer_was_written_over():
    # Radical Castle, once it had crashed: its GCOD 0's led to an odd address.
    m, h = heap()
    hd, other = h.new_handle(16), h.new_handle(16)
    data, free = h.deref(hd), h.free_bytes()
    m.w32(hd, 0x60000000 | (data + 0x1F))
    h.dispose_handle(hd)
    assert h.error == memWZErr and not h.is_handle(hd)
    assert m.r32(h.start + H_FST_FREE) == hd      # back on the free list, at its front
    assert h.free_bytes() == free                 # nothing freed that wasn't a block
    h.dispose_handle(other)
    assert h.error == noErr and h.free_bytes() > free


def test_realloc_handle_takes_a_master_pointer_the_program_made():
    # Word 1.0 keeps one in its first segment for the rest of itself.
    m, h = heap()
    mine = h.new_ptr(8) + 4                       # NIL, in the zone, not the zone's
    assert h.realloc_handle(mine, 32) and h.error == noErr
    data = h.deref(mine)
    assert data and h.handle_size(mine) == 32 and h.start + m.r32(data - 4) == mine
    h.lock(mine)
    assert m.r8(mine) & LOCKED
    h.unlock(mine)
    h.dispose_handle(mine)
    assert h.error == noErr and h.new_handle(4) != mine     # not the zone's to reuse
    m.w32(mine, 0x1234)                                     # not NIL: not a master pointer
    assert not h.realloc_handle(mine, 8) and h.error == memWZErr


def test_set_handle_size_grows_in_place_or_moves():
    m, h = heap()
    hd = h.new_handle(8)
    m.write(h.deref(hd), b"12345678")
    first = h.deref(hd)
    assert h.set_handle_size(hd, 40) and h.deref(hd) == first     # room after it
    wall = h.new_ptr(4)                                           # now blocked
    assert h.set_handle_size(hd, 200)
    assert h.deref(hd) != first and m.read(h.deref(hd), 8) == b"12345678"
    assert h.handle_size(hd) == 200
    h.dispose_ptr(wall)


def test_locked_handle_does_not_move():
    _, h = heap()
    hd = h.new_handle(8)
    h.new_ptr(4)                                  # right after it
    h.lock(hd)
    before = h.deref(hd)
    assert not h.set_handle_size(hd, 200) and h.error == memFullErr
    assert h.deref(hd) == before
    h.unlock(hd)
    assert h.set_handle_size(hd, 200) and h.deref(hd) != before


def test_an_empty_handle_keeps_its_nil_master_pointer_and_says_so(qapp):
    """Inside Macintosh II-40/41: HLock, HUnlock, HPurge and HNoPurge give
    nilHandleErr for a NIL master pointer, and HGetState does as well:
    Museum Madness reads a picture in again when HGetState says that of
    its handle, and otherwise draws what's left where the block was."""
    _, h = heap()
    hd = h.new_handle(8)
    h.empty_handle(hd)
    for op in (h.lock, h.unlock, lambda hd: h.set_purgeable(hd, True),
               lambda hd: h.set_purgeable(hd, False), lambda hd: h.set_state(hd, 0xC0)):
        h.error = noErr
        op(hd)
        assert h.error == nilHandleErr and h.mem.r32(hd) == 0
    g = Guest()
    hd = g.handle(b"picture")
    g("HLock", hd)
    assert g("HGetState", hd) & LOCKED and g.mem_err == noErr
    g("HUnlock", hd)
    g("EmptyHandle", hd)
    for name in ("HLock", "HPurge"):
        g(name, hd)
        assert g.mem_err == nilHandleErr and g.mem.r32(hd) == 0
    assert g("HGetState", hd) == 0 and g.mem_err == nilHandleErr


def test_a_locked_handle_at_the_end_grows_the_zone_for_room():
    # A program that grows its locked first segment in place, as Pyramid of
    # Peril does, gets the room the application heap may still grow by.
    m = RAM(0x10000)
    h = Heap(m, 0x1000, 0x2000, 0x8000)
    hd = h.new_handle(8)
    m.write(h.deref(hd), b"12345678")
    h.lock(hd)
    before = h.deref(hd)
    assert h.set_handle_size(hd, 0x3000) and h.deref(hd) == before
    assert h.handle_size(hd) == 0x3000 and h.end >= before + 0x3000
    assert m.read(before, 8) == b"12345678"
    assert not h.set_handle_size(hd, 0x10000) and h.error == memFullErr
    assert h.deref(hd) == before and h.handle_size(hd) == 0x3000


def test_a_locked_handle_grows_the_zone_past_a_handle_it_moves():
    # Starter Accounts' BASIC runtime grows its locked program handle for
    # its workspace with an unlocked handle after it: the zone grows for the
    # room and for the handle it moves (Inside Macintosh II-34: only a
    # nonrelocatable or locked block above makes a locked block's growth fail).
    m = RAM(0x10000)
    h = Heap(m, 0x1000, 0x2000, 0x8000)
    hd = h.new_handle(8)
    h.lock(hd)
    other = h.new_handle(0x20)
    m.write(h.deref(other), b"o" * 0x20)
    before, end = h.deref(hd), h.end
    assert h.set_handle_size(hd, 0x3000) and h.deref(hd) == before and h.end > end
    assert h.deref(other) > before + 0x3000 and m.read(h.deref(other), 0x20) == b"o" * 0x20
    assert m.r32(h.start + ZCB_FREE) == sum(n for _, n in h._free)


def test_shrinking_frees_the_tail():
    _, h = heap()
    hd = h.new_handle(400)
    after = h.free_bytes()
    assert h.set_handle_size(hd, 20)
    assert h.free_bytes() == after + (408 - 28)


def test_compact_mem_slides_the_handles_down_past_the_free_space():
    m, h = heap()
    gone, a, wall, b, gap, c = (h.new_handle(64) for _ in range(6))
    h.dispose_handle(wall)
    wall = h.new_ptr(16)                          # where the handle was, and stays
    h.dispose_handle(gone)
    h.dispose_handle(gap)
    for hd, text in ((a, b"A"), (b, b"B"), (c, b"C")):
        m.write(h.deref(hd), text * 64)
    h.lock(b)
    was = {hd: h.deref(hd) for hd in (a, b, c)}
    free = h.free_bytes()
    h.compact()
    assert h.deref(a) < was[a] and h.deref(b) == was[b] and h.deref(c) < was[c]
    assert h.deref(a) + 64 + HEADER <= wall < h.deref(b)    # past the gaps only
    assert h.deref(c) == h.deref(b) + 64 + HEADER
    for hd, text in ((a, b"A"), (b, b"B"), (c, b"C")):
        assert m.read(h.deref(hd), 64) == text * 64
        assert h.start + m.r32(h.deref(hd) - 4) == hd        # its relative handle
    assert m.r8(b) & LOCKED and h.free_bytes() == free
    assert h.max_block(grow=False) == h.end - (h.deref(c) + 64) - HEADER


def filled(m, h, *sizes):
    """Handles of sizes, holding their own numbers, after a pointer at the
    bottom of the zone, and a last one taking the rest of it."""
    h.dispose_handle(h.new_handle(4))               # the master pointers first
    h.new_ptr(16)
    hs = [h.new_handle(n) for n in sizes]
    hs.append(h.new_handle(h.max_block(grow=False)))
    assert not h.max_block(grow=False)
    for i, hd in enumerate(hs):
        m.write(h.deref(hd), bytes([i]) * h.handle_size(hd))
    return hs


def contents(m, h, hs):
    """What each handle holds, if its block still leads back to it."""
    return [h.start + m.r32(h.deref(hd) - 4) == hd and m.read(h.deref(hd), h.handle_size(hd))
            for hd in hs]


def test_a_pointer_with_no_free_block_goes_low_and_the_handles_move_up():
    """And the zone grows for them, as a Mac's does to keep its
    nonrelocatable blocks together at the bottom (Inside Macintosh II-12)."""
    m = RAM(0x10000)
    h = Heap(m, 0x1000, 0x2000, 0x8000)
    hs = filled(m, h, 0x100, 0x100)
    low, end, held = h.deref(hs[0]), h.end, contents(m, h, hs)
    p = h.new_ptr(0x180)
    assert p == low and h.end > end
    assert all(h.deref(hd) > p + 0x180 for hd in hs[:2]) and h.deref(hs[2]) < end
    assert contents(m, h, hs) == held


def test_a_pointer_goes_low_into_free_blocks_the_handles_move_out_from_between():
    m = RAM(0x10000)
    h = Heap(m, 0x1000, 0x2000)
    hs = filled(m, h, 0x100, 0x100, 0x100, 0x100, 0xF0)
    h.dispose_handle(hs.pop(1))
    h.dispose_handle(hs.pop(2))                     # 0x108 bytes free twice
    low, was, held = h.deref(hs[0]), h.deref(hs[1]), contents(m, h, hs)
    p = h.new_ptr(0x180)
    assert p == low and h.deref(hs[1]) == was and h.deref(hs[0]) > was
    assert contents(m, h, hs) == held


def test_a_pointer_goes_low_though_a_free_block_higher_up_would_hold_it():
    """The handles in the way move up into that free block, and the zone
    doesn't grow: Excel 1.5 jumps from the 128K block it asks for into its
    first segment with 16-bit offsets."""
    m = RAM(0x10000)
    h = Heap(m, 0x1000, 0x3000, 0x8000)
    hs = filled(m, h, 0x100, 0x100, 0x800)
    h.dispose_handle(hs.pop(2))
    low, end, top, held = h.deref(hs[0]), h.end, h.deref(hs[2]), contents(m, h, hs)
    p = h.new_ptr(0x180, low=True)
    assert p == low and h.end == end and h.deref(hs[2]) == top
    assert all(p + 0x180 < h.deref(hd) < top for hd in hs[:2])
    assert contents(m, h, hs) == held


def test_a_pointer_goes_low_taking_part_of_a_free_block_the_handles_moving_into_the_rest():
    m = RAM(0x10000)
    h = Heap(m, 0x1000, 0x3000, 0x8000)
    hs = filled(m, h, 0x100, 0x100, 0x800)
    h.dispose_handle(hs.pop(2))
    low, end, top, held = h.deref(hs[0]), h.end, h.deref(hs[2]), contents(m, h, hs)
    p = h.new_ptr(0x400, low=True)
    assert p == low and h.end == end and h.deref(hs[2]) == top
    assert all(p + 0x400 < h.deref(hd) < top for hd in hs[:2])
    assert contents(m, h, hs) == held


def test_reserve_makes_room_low_the_handles_moving_up():
    """ResrvMem, for a block a program will keep locked: room as low in the
    zone as can be had, though there's a free block higher up (Inside
    Macintosh II-39)."""
    m = RAM(0x10000)
    h = Heap(m, 0x1000, 0x2000)
    hs = filled(m, h, 0x100, 0x100, 0x200)
    h.dispose_handle(hs.pop(2))
    low, held = h.deref(hs[0]), contents(m, h, hs)
    assert h.reserve(0x100)
    assert h.deref(hs[0]) > h.deref(hs[1]) > low and contents(m, h, hs) == held
    assert h.deref(h.new_handle(0x100)) == low


def test_move_hhi_moves_a_handle_up_till_a_block_that_cant_move():
    """Into the top of the highest free block below the first locked or
    nonrelocatable block above it, or the zone's end (Inside Macintosh
    II-44)."""
    m, h = heap()
    a = h.new_handle(0x40)
    gap = h.new_handle(0x100)
    wall = h.new_handle(0x40)
    h.lock(wall)
    h.dispose_handle(gap)
    m.write(h.deref(a), bytes(range(0x40)))
    h.move_high(a)
    assert h.error == noErr and h.deref(a) + 0x40 + HEADER == h.deref(wall)
    h.unlock(wall)
    h.move_high(a)
    assert h.error == noErr and h.deref(a) + 0x40 == h.end
    assert m.read(h.deref(a), 0x40) == bytes(range(0x40))
    assert h.max_block(grow=False) >= 0x100     # what it left merged with the gap


def test_move_hhi_leaves_a_locked_handle_and_says_so(qapp):
    m, h = heap()
    a = h.new_handle(0x40)
    h.lock(a)
    before = h.deref(a)
    h.move_high(a)
    assert h.error == memLockedErr and h.deref(a) == before
    h.move_high(0)
    assert h.error == nilHandleErr
    g = Guest()
    hd = g.handle(b"moves up")
    before = g.heap.deref(hd)
    g("MoveHHi", hd)
    assert g.mem_err == noErr and g.heap.deref(hd) > before and g.contents(hd) == b"moves up"


def test_move_hhi_goes_where_the_headers_above_say(qapp):
    """Over zones of all sorts: into the top of the highest free block that
    holds it below the first block above it that's locked or
    nonrelocatable, as the headers and master pointers in memory have them,
    with what every handle holds kept."""
    rng = random.Random(11)
    for _ in range(150):
        m, h = heap()
        hs = []
        while h.max_block(grow=False) > 0x300:
            if rng.random() < 0.15:
                h.new_ptr(rng.randrange(1, 0x80))
            hs.append(h.new_handle(rng.randrange(1, 0x200)))
        for hd in rng.sample(hs, len(hs) // 3):
            hs.remove(hd)
            h.dispose_handle(hd)
        for i, hd in enumerate(hs):
            m.write(h.deref(hd), bytes([i]) * h.handle_size(hd))
        for _ in range(6):
            for hd in hs:
                (h.lock if rng.random() < 0.2 else h.unlock)(hd)
            a = rng.choice([hd for hd in hs if not m.r8(hd) & LOCKED] or hs)
            if m.r8(a) & LOCKED:
                continue
            data, size = h.deref(a), h.handle_size(a)
            need = max((size + HEADER + 3) & -4, MIN_BLOCK)
            want = data
            at = data - HEADER + (m.r32(data - HEADER) & 0xFFFFFF)
            while at < h.end:
                kind, n = m.r8(at) >> 6, m.r32(at) & 0xFFFFFF
                if kind == FREE and (n == need or n - need >= MIN_BLOCK):
                    want = at + n - need + HEADER
                elif kind == NONRELOCATABLE or (
                        kind == RELOCATABLE and m.r8(h.start + m.r32(at + 4)) & LOCKED):
                    break
                at += n
            held, free = contents(m, h, hs), h.free_bytes()
            h.move_high(a)
            assert h.error == noErr and h.deref(a) == want
            assert contents(m, h, hs) == held and h.free_bytes() == free


def test_move_hhi_reads_the_zone_back_if_a_header_above_has_changed(qapp):
    """A program that takes the free block after one of its handles into
    the handle's block, by its header, leaves MoveHHi no room there."""
    m, h = heap()
    a, b, gap, wall = (h.new_handle(0x40) for _ in range(4))
    h.lock(wall)
    h.dispose_handle(gap)
    at = h.deref(b) - HEADER
    m.w32(at, m.r32(at) + (m.r32(h.deref(wall) - HEADER) & 0xFFFFFF))   # gap: as big as wall
    before = h.deref(a)
    h.move_high(a)
    assert h.error == noErr and h.deref(a) == before
    assert h.deref(b) - HEADER + (m.r32(at) & 0xFFFFFF) == h.deref(wall) - HEADER


def test_move_hhi_leaves_a_handle_whose_block_a_program_has_taken_into_another(qapp):
    m, h = heap()
    before, a, b, gap, wall = (h.new_handle(0x40) for _ in range(5))
    h.lock(wall)
    h.dispose_handle(gap)
    for low, high in ((before, a), (b, wall)):          # each takes the block after it
        at = h.deref(low) - HEADER
        m.w32(at, m.r32(at) + (m.r32(h.deref(high) - HEADER) & 0xFFFFFF))
    was = h.deref(a)
    h.move_high(a)
    assert h.deref(a) == was and h.error == noErr


def test_move_hhi_reads_back_the_free_block_below_before_joining_it(qapp):
    """Macintosh Pascal makes blocks of its own at the top of a free block:
    the room a handle leaves when it moves up doesn't join that free block
    over one there."""
    m, h = heap()
    low, gap, a = (h.new_handle(0x40) for _ in range(3))
    h.dispose_handle(gap)
    free_at = h.deref(low) - HEADER + (m.r32(h.deref(low) - HEADER) & 0xFFFFFF)
    own = h.deref(a) - HEADER - 0x20
    m.w32(own, NONRELOCATABLE << 30 | 0x20)             # $18 bytes
    m.w32(own + 4, h.start)
    m.w32(free_at, own - free_at)
    h.move_high(a)
    assert h.error == noErr and h.deref(a) + 0x40 == h.end
    assert m.r32(free_at) == own - free_at and h.ptr_size(own + HEADER) == 0x18


def test_move_hhi_reads_back_the_free_blocks_above(qapp):
    """A block a program made at the top of the free block above, or a
    handle it moved there itself, as MacWrite 4.5 and Macintosh Pascal do,
    stops it there."""
    m, h = heap()
    a = h.new_handle(0x40)
    free_at = h.deref(a) + 0x40
    own = h.end - 0x20
    m.w32(own, NONRELOCATABLE << 30 | 0x20)
    m.w32(own + 4, h.start)
    m.w32(free_at, own - free_at)
    h.move_high(a)
    assert h.deref(a) + 0x40 == own and h.ptr_size(own + HEADER) == 0x18
    m, h = heap()
    a, b = h.new_handle(0x40), h.new_handle(0x40)
    data = h.deref(b)
    m.write(data, b"b" * 0x40)
    phys = m.r32(data - HEADER) & 0xFFFFFF
    to = h.end - phys
    m.move(data - HEADER, to, phys)
    m.w32(data - HEADER, phys)                    # free, where it was
    m.w32(data - HEADER + phys, to - (data - HEADER + phys))
    m.w32(b, to + HEADER)
    h.move_high(a)
    assert h.deref(a) + 0x40 == to and m.read(h.deref(b), 0x40) == b"b" * 0x40


def test_a_zone_reads_its_headers_through_a_mirror_over_it(qapp):
    """Its checks read hundreds of headers at a time straight from RAM's
    bytes, but not where a mirror lies over the zone: there its addresses
    aren't where its bytes are."""
    m = RAM(0x10000)
    m.mirror(0x8000, 0x10000, 0x0000)
    m._b[0x9000:0xD000] = b"\xff" * 0x4000          # what the mirror hides
    h = Heap(m, 0x9000, 0xD000)
    a = h.new_handle(0x40)
    h.move_high(a)
    at = h.deref(a) - 8
    assert h._longs()(at) == m.r32(at) != 0xFFFFFFFF
    assert h.deref(a) + 0x40 == h.end and h.start + m.r32(at + 4) == a
    m2, h2 = heap()
    assert h2._longs()(h2.start) == m2.r32(h2.start)


def test_move_hhi_moves_a_block_whose_header_names_another_master_pointer(qapp):
    """And names its own in the header where it goes."""
    m, h = heap()
    a = h.new_handle(0x40)
    m.w32(h.deref(a) - 4, 0x1234)
    h.move_high(a)
    assert h.deref(a) + 0x40 == h.end and h.start + m.r32(h.deref(a) - 4) == a


def test_move_hhi_goes_into_the_room_a_program_grew_the_zone_by(qapp):
    """PackIt III grows the zone itself, with a free block where it ended
    and bkLim past it."""
    m = RAM(0x10000)
    h = Heap(m, 0x1000, 0x2000, 0x8000)
    a = h.new_handle(0x40)
    old, new = h.end, 0x6000
    m.w32(old, FREE << 30 | (new - old))
    m.w32(h.start, new)
    h.move_high(a)
    assert h.error == noErr and h.deref(a) + 0x40 == new == h.end


def test_max_mem_compacts_the_zone(qapp):
    """Inside Macintosh II-38: the p-System of the Angelsoft adventures takes
    the largest free block to be the one at the zone's end."""
    g = Guest()
    heap = g.process.heap
    hs = [heap.new_handle(0x400) for _ in range(3)]
    heap.dispose_handle(hs[1])
    was = heap.deref(hs[2])
    largest, growth = g("MaxMem", 0)
    assert heap.deref(hs[2]) == was - 0x408
    addr, size = heap._free[-1]
    assert addr + size == heap.end and size - HEADER == largest


def test_max_mem_purges_every_purgeable_block(qapp):
    """Inside Macintosh II-38: "MaxMem compacts the current heap zone and
    purges all purgeable blocks from the zone", the locked ones aside."""
    g = Guest()
    heap = g.process.heap
    purged, locked, kept = (heap.new_handle(0x400) for _ in range(3))
    for h in (purged, locked):
        heap.set_purgeable(h)
    heap.lock(locked)
    largest, growth = g("MaxMem", 0)
    assert heap.is_empty(purged) and heap.is_handle(purged)
    assert not heap.is_empty(locked) and not heap.is_empty(kept)
    assert g.mem_err == noErr and largest == heap.max_block(grow=False)


def test_purge_mem_purges_until_there_is_room_and_moves_nothing():
    """Inside Macintosh II-40: blocks are purged "until a contiguous block
    of at least cbNeeded free bytes is created"; it doesn't compact."""
    m, h = heap(0x1800)
    first, kept, second, third = (h.new_handle(0x500) for _ in range(4))
    for hd in (first, second, third):
        h.set_purgeable(hd)
    where = h.deref(kept)
    assert h.max_block() < 0x400
    assert h.purge_for(0x400)
    assert h.is_empty(first) and not h.is_empty(second) and not h.is_empty(third)
    assert h.deref(kept) == where
    assert not h.purge_for(0x2000)
    assert h.is_empty(second) and h.is_empty(third) and h.deref(kept) == where


def test_purge_mem_says_when_there_is_no_room(qapp):
    g = Guest()
    hd = g.process.heap.new_handle(0x100)
    g.process.heap.set_purgeable(hd)
    g("PurgeMem", 0x10, 0)
    assert g.mem_err == noErr and not g.process.heap.is_empty(hd)
    g("PurgeMem", 0x7FFFFFFF, 0)
    assert g.mem_err == memFullErr and g.process.heap.is_empty(hd)


def test_purge_space_counts_what_a_purge_would_free_and_purges_nothing():
    """Inside Macintosh IV, Memory Manager: PurgeSpace gives "the total
    amount of space in bytes that could be obtained by a general purge
    (without actually doing the purge)", what's already free included, and
    the most of it to be had in one piece.  HyperCard 1.2 fills its heap
    with purgeable blocks, then asks it whether it has room to go on."""
    m, h = heap(0x1800)
    first, second, kept, locked = (h.new_handle(0x500) for _ in range(4))
    for hd in (first, second, locked):
        h.set_purgeable(hd)
    h.lock(locked)
    block = h._physical(0x500)
    free = h.free_bytes()
    assert free < block
    assert h.purge_space() == (free + 2 * block, 2 * block - HEADER)
    assert not any(h.is_empty(hd) for hd in (first, second, kept, locked))
    h.set_purgeable(kept)
    assert h.purge_space() == (free + 3 * block, 3 * block - HEADER)


def test_purge_space_counts_what_the_zone_could_grow_by():
    m = RAM(0x10000)
    h = Heap(m, 0x1000, 0x2000, 0x8000)
    hd = h.new_handle(0x400)
    h.set_purgeable(hd)
    total, contig = h.purge_space()
    assert total == h.free_bytes() + h._physical(0x400)
    assert contig == h.max_block() + h._physical(0x400)
    h.lock(hd)
    assert h.purge_space() == (h.free_bytes(), h.max_block())


def test_an_allocation_compacts_the_zone_before_it_purges_anything():
    """"The Memory Manager first tries to obtain the requested amount of
    space by compacting the heap" (Inside Macintosh: Memory, "Heap Purging
    and Compaction"): a block more than either gap but less than both."""
    m, h = heap()
    hs = filled(m, h, 0x100, 0x100, 0x100, 0x100)
    h.set_purgeable(hs[1])
    h.dispose_handle(hs[0])
    h.dispose_handle(hs[2])
    assert h.new_handle(0x180)
    assert not h.is_empty(hs[1])
    assert contents(m, h, hs[3:]) == [bytes([3]) * 0x100, bytes([4]) * h.handle_size(hs[4])]
    assert contents(m, h, hs[1:2]) == [bytes([1]) * 0x100]


def test_a_handle_grown_into_the_compacted_zone_keeps_what_it_held():
    """Compacting for it moves it too, before it's copied."""
    m, h = heap()
    hs = filled(m, h, 0x100, 0x100, 0x100, 0x100)
    h.dispose_handle(hs[0])
    h.dispose_handle(hs[2])
    assert h.set_handle_size(hs[3], 0x200)
    assert m.read(h.deref(hs[3]), 0x100) == bytes([3]) * 0x100
    assert contents(m, h, [hs[1], hs[4]]) == [bytes([1]) * 0x100,
                                              bytes([4]) * h.handle_size(hs[4])]


def grow_zone(h, answer, proc=0x4000):
    """A grow-zone function at proc for zone h: answer(cbNeeded, handle)
    says what it freed.  The calls it gets, with what was purgeable and
    still there each time."""
    calls = []

    def call(at, needed, protected):
        assert at == proc
        calls.append((needed, protected))
        return answer(needed, protected)
    h.mem.w32(h.start + GZ_PROC, proc)
    h.grow_zone = call
    return calls


def test_the_grow_zone_function_is_called_last_and_again_while_it_frees_something():
    """Once compacting and purging can't make room, with the block's
    physical size, and again, after compacting and purging again, only
    while it says it freed something (Inside Macintosh: Memory,
    "MyGrowZone")."""
    m, h = heap()
    hs = filled(m, h, 0x100, 0x100, 0x100)
    h.set_purgeable(hs[1])
    gone = [hs[0], hs[2]]
    purged = []

    def answer(needed, protected):
        purged.append(h.is_empty(hs[1]))
        if not gone:
            return 0
        g = gone.pop(0)
        h.dispose_handle(g)
        return 0x108
    calls = grow_zone(h, answer)
    assert h.new_handle(0x300)
    assert calls == [(0x308, 0), (0x308, 0)] and purged == [True, True]
    calls.clear()
    assert not h.new_handle(0x1000) and h.error == memFullErr
    assert calls == [(0x1008, 0)]                   # it said it freed nothing


def test_the_grow_zone_function_is_given_the_handle_being_grown():
    """In GZRootHnd, which GZSaveHnd returns, for it to leave alone."""
    m, h = heap()
    hs = filled(m, h, 0x100)
    calls = grow_zone(h, lambda needed, protected: 0)
    assert not h.set_handle_size(hs[0], 0x200)
    assert calls == [(0x208, hs[0])]
    assert m.read(h.deref(hs[0]), 0x100) == bytes([0]) * 0x100


def test_a_handle_its_grow_zone_function_takes_is_not_grown():
    """Though it shouldn't: the block isn't copied into what it freed."""
    m, h = heap()
    hs = filled(m, h, 0x100)
    free = h.free_bytes()

    def answer(needed, protected):
        h.dispose_handle(hs[1])
        h.empty_handle(protected)
        return 1
    grow_zone(h, answer)
    assert not h.set_handle_size(hs[0], 0x200) and h.error == memFullErr
    assert h.is_empty(hs[0]) and h.free_bytes() > free


def test_no_grow_zone_function_is_called_without_one_or_from_itself():
    """None with a gzProc of 0, and none for what it allocates itself,
    which it isn't to do (Inside Macintosh: Memory, "MyGrowZone"); one
    that always says it freed something is called only so many times."""
    m, h = heap()
    filled(m, h)
    calls = grow_zone(h, lambda needed, protected: 1, proc=0)
    assert not h.new_ptr(0x100) and calls == []
    inner = []
    calls = grow_zone(h, lambda needed, protected: inner.append(h.new_ptr(0x200)) or 0)
    assert not h.new_ptr(0x100)
    assert calls == [(0x108, 0)] and inner == [0]
    calls = grow_zone(h, lambda needed, protected: 1)
    assert not h.new_ptr(0x100) and len(calls) == GROW_ZONE_CALLS


def test_blocks_a_program_moved_itself_are_found_where_they_are():
    """MacWrite 4.5 moves a segment to the top of the heap before it loads
    it, writing the blocks' headers and master pointer as the Memory
    Manager would."""
    m, h = heap()
    hd = h.new_handle(40)
    data = h.deref(hd)
    m.write(data, b"x" * 40)
    tail = data + 40                              # the free space after it
    phys = m.r32(data - HEADER) & 0xFFFFFF
    to = h.end - phys
    m.move(data - HEADER, to, phys)
    m.w32(data - HEADER, phys)                    # free, where it was
    m.w32(tail, to - tail)                        # and below where it is now
    m.w32(hd, to + HEADER)
    assert h.handle_size(hd) == 40 and m.read(h.deref(hd), 40) == b"x" * 40
    p = h.new_ptr(16)
    assert p == data                              # the space it left
    h.dispose_ptr(p)
    h.dispose_handle(hd)
    assert h.max_block(grow=False) == h.end - data


def test_freeing_a_block_keeps_the_ones_a_program_made_above_it():
    """Macintosh Pascal moves a handle to the top of the zone by taking
    the room from the free block there: freeing the block below that one
    leaves it be, and the room up to it free."""
    m, h = heap()
    hd, below = h.new_handle(40), h.new_handle(40)
    data = h.deref(hd)
    m.write(data, b"x" * 40)
    phys = m.r32(data - HEADER) & 0xFFFFFF
    tail = h.deref(below) - HEADER + phys         # the free block after both
    to = h.end - phys
    m.move(data - HEADER, to, phys)
    m.w32(data - HEADER, phys)                    # free, where it was
    m.w32(tail, to - tail)                        # the free block, short of it
    m.w32(hd, to + HEADER)
    h.dispose_handle(below)
    assert h.handle_size(hd) == 40 and m.read(h.deref(hd), 40) == b"x" * 40
    assert h.set_handle_size(hd, 0) and h.handle_size(hd) == 0
    assert h.max_block(grow=False) == to - data


def test_a_block_a_program_made_past_the_zone_and_moved_bklim_over_is_in_it():
    """The Angelsoft adventures' p-System carves a block of its own at the
    top of the room the zone can grow into, then moves bkLim past it and
    the free block at the zone's end up to it."""
    m = RAM(0x10000)
    h = Heap(m, 0x1000, 0x2000, 0x8000)
    at = h.start + 0x34
    while at + (m.r32(at) & 0xFFFFFF) != h.end:         # the zone's last block, free
        at += m.r32(at) & 0xFFFFFF
    assert m.r32(at) >> 30 == FREE
    own = 0x7000
    m.w32(own, NONRELOCATABLE << 30 | 2 << 24 | 0x100)  # $F6 bytes
    m.w32(own + 4, h.start)
    m.w32(at, own - at)
    m.w32(h.start, own + 0x100)
    p = own + HEADER
    assert h.ptr_size(p) == 0xF6 and h.end == own + 0x100
    assert h.set_ptr_size(p, 0x20) and h.ptr_size(p) == 0x20
    q = h.new_ptr(0x4000)
    assert h.start < q < own and h.ptr_size(p) == 0x20


def test_a_zone_a_program_grew_itself_ends_where_it_grew_it():
    """PackIt III, taking a Mac II's ROM85 for an old ROM's, grows the zone
    as programs for the 64K ROM did: a free block where the zone ended,
    then bkLim past it.  The next block that wants room has it there, the
    zone not growing back over it."""
    m = RAM(0x20000)
    ends = []
    h = Heap(m, 0x1000, 0x2000, 0x8000, on_grow=ends.append)
    old, new = h.end, 0x9006                # past its limit, and 2 more than a multiple of 4
    m.w32(old, FREE << 30 | (new - old))
    m.w32(h.start, new)
    p = h.new_ptr(0x5000)
    assert p and h.ptr_size(p) == 0x5000
    assert h.end == new == m.r32(h.start) and h.room_to_grow() == 0 and ends == []
    assert h.max_block(grow=False) > 0x2000


def test_a_bklim_that_says_nothing_of_the_zone_is_let_be():
    m = RAM(0x10000)
    h = Heap(m, 0x1000, 0x2000)
    end = h.end
    m.w32(h.start, 0x7777)                  # past anything the zone's blocks reach
    p = h.new_ptr(0x100)
    assert p and h.end == end and h.ptr_size(p) == 0x100


def test_a_68000s_blocks_are_a_multiple_of_two_bytes():
    """Macintosh Pascal checksums its code with a block's size from its
    header, which on a 68000 is its logical size and 8, made even."""
    m = RAM(0x10000)
    h2, h4 = Heap(m, 0x1000, 0x3000, align=2), Heap(m, 0x4000, 0x6000)
    for zone, phys in ((h2, 0x175A), (h4, 0x175C)):
        data = zone.deref(zone.new_handle(0x1752))
        assert m.r32(data - HEADER) & 0xFFFFFF == phys
        assert zone.handle_size(zone.recover_handle(data)) == 0x1752


def test_a_68000s_zone_grows_by_all_a_block_needs():
    """The zone grows by what the free block at its end lacks, made a
    multiple of 4, even when that's 2 more than one: Klondike's first
    segment was 2 bytes too big for the room the zone grew to."""
    m = RAM(0x20000)
    h = Heap(m, 0x1000, 0x1800, limit=0x10000, align=2)
    h.new_ptr(6)                                    # 14 bytes
    last = h.max_block(grow=False) + HEADER
    assert last % 4 == 2
    size = last + 0x2002 - HEADER                   # more than the zone grows by at least
    p = h.new_ptr(size)
    assert p and h.ptr_size(p) == size


def test_a_block_grows_in_place_by_moving_the_ones_after_it():
    """SetPtrSize, and SetHandleSize on a locked handle, move the unlocked
    relocatable blocks after the block out of its way, and purge the
    purgeable ones when there's nowhere for them: Macintosh Pascal grows a
    pointer before it runs a program."""
    m, h = heap(0x1000)
    h.dispose_handle(h.new_handle(4))               # the master pointers first
    p = h.new_ptr(4)
    moved, purged = h.new_handle(16), h.new_handle(0x400)
    m.write(h.deref(moved), b"m" * 16)
    h.set_purgeable(purged)
    h.new_ptr(0x780)                                # too little left for purged
    assert h.set_ptr_size(p, 0x100) and h.ptr_size(p) == 0x100
    assert m.read(h.deref(moved), 16) == b"m" * 16 and h.deref(moved) > p + 0x100
    assert h.is_empty(purged)
    assert m.r32(h.start + ZCB_FREE) == sum(n for _, n in h._free)
    # a locked handle in the way stays, and so does the block
    q = h.new_ptr(4)
    stuck = h.new_handle(8)
    h.lock(stuck)
    assert not h.set_ptr_size(q, 0x40) and h.ptr_size(q) == 4
    # a locked handle grows the same way
    lk = h.new_handle(4)
    h.lock(lk)
    after = h.new_handle(8)
    assert h.set_handle_size(lk, 0x40) and h.deref(lk) < h.deref(after)


def test_a_block_grows_past_a_handle_into_the_free_space_after_it():
    """The handle in the way moves into what's free past the room the block
    grows into: HyperCard 2.0 grows a pointer past a handle with all the
    rest of the zone free after it."""
    m, h = heap(0x1000)
    h.dispose_handle(h.new_handle(4))               # the master pointers first
    p = h.new_ptr(0x100)
    moved = h.new_handle(0x40)
    m.write(h.deref(moved), b"m" * 0x40)
    assert h.set_ptr_size(p, 0x180) and h.ptr_size(p) == 0x180
    assert m.read(h.deref(moved), 0x40) == b"m" * 0x40 and h.deref(moved) > p + 0x180
    assert m.r32(h.start + ZCB_FREE) == sum(n for _, n in h._free)
    assert h._walk(h.end) == (h._free, h._blocks)


def test_an_unlocked_handle_moves_before_the_blocks_after_it_do():
    """SetHandleSize moves an unlocked handle with no room after it, and
    moves the blocks after it only when there's nowhere else for it: Mac'N
    Med Engine grows its Apple menu just below a segment it's unloaded and
    is still running in."""
    m, h = heap(0x1000)
    h.dispose_handle(h.new_handle(4))               # the master pointers first
    hd = h.new_handle(16)
    seg = h.new_handle(0x40)
    m.write(h.deref(seg), b"s" * 0x40)
    before = h.deref(seg)
    assert h.set_handle_size(hd, 0x60) and h.handle_size(hd) == 0x60
    assert h.deref(seg) == before and m.read(before, 0x40) == b"s" * 0x40
    assert h.deref(hd) > before
    # with no free block big enough, the ones after it make way after all
    big = h.new_handle(0x100)
    after = h.new_handle(0x40)
    m.write(h.deref(after), b"a" * 0x40)
    gap = h.new_handle(0x60)
    h.new_handle(max(n for _, n in h._free) - 0x100)
    h.dispose_handle(gap)
    assert max(n for _, n in h._free) < 0x188
    assert h.set_handle_size(big, 0x180) and h.handle_size(big) == 0x180
    assert h.deref(after) > h.deref(big) and m.read(h.deref(after), 0x40) == b"a" * 0x40
    assert m.r32(h.start + ZCB_FREE) == sum(n for _, n in h._free)
    assert h._walk(h.end) == (h._free, h._blocks)


def test_growing_a_block_reads_back_the_blocks_a_program_made_after_it():
    """Microsoft File reads its p-code file, laid out as heap blocks, over
    a handle's block, headers and all: growing a block before it moves the
    handle's block as its header has it now, and leaves the program's be."""
    m, h = heap(0x1000)
    h.dispose_handle(h.new_handle(4))               # the master pointers first
    p = h.new_ptr(4)
    hd = h.new_handle(0x100)
    data = h.deref(hd)
    assert data == p + (m.r32(p - HEADER) & 0xFFFFFF)          # right after p
    phys = m.r32(data - HEADER) & 0xFFFFFF
    m.w32(data - HEADER, RELOCATABLE << 30 | 0x88)              # the handle's, cut short
    m.w32(data + 0x80, NONRELOCATABLE << 30 | (phys - 0x88))    # and the program's own
    m.write(data, b"h" * 0x80)
    m.write(data + 0x80 + HEADER, b"p" * 0x10)
    assert h.set_ptr_size(p, 0x40) and h.ptr_size(p) == 0x40
    assert h.handle_size(hd) == 0x80 and m.read(h.deref(hd), 0x80) == b"h" * 0x80
    assert m.read(data + 0x80 + HEADER, 0x10) == b"p" * 0x10
    assert m.r32(h.start + ZCB_FREE) == sum(n for _, n in h._free)


def test_the_zone_record_says_how_many_masters_a_block_holds():
    # Multiplan calls MoreMasters until it has what it wants, counting moreMast.
    m = RAM(0x10000)
    h = Heap(m, 0x1000, 0x2000)
    assert m.r16(h.start + 20) == 32


def test_the_application_heap_has_masters_64_at_a_time(qapp):
    """Inside Macintosh II-22: 32 at a time for the system heap, 64 for
    the application's."""
    g = Guest()
    heap = g.process.heap
    assert g.mem.r16(heap.start + MORE_MAST) == 64
    before = len(heap._masters)
    g("MoreMasters")
    assert len(heap._masters) == before + 64
    assert g.mem.r16(g.system.sys_heap.start + MORE_MAST) == 32


def test_a_zone_grows_toward_its_limit():
    m = RAM(0x10000)
    ends = []
    h = Heap(m, 0x1000, 0x2000, 0x8000, on_grow=ends.append)
    free = h.free_bytes()
    assert h.room_to_grow() == 0x6000 and free > 0x6000       # growth counts as free
    assert h.max_block(grow=False) < 0x1000 < 0x6000 < h.max_block()
    p = h.new_ptr(0x3000)
    assert p and 0x2000 < h.end < 0x8000 and ends == [h.end]
    assert m.r32(h.start) == h.end                             # bkLim
    assert h.free_bytes() == free - (0x3000 + HEADER)
    h.set_limit(0x1800)                                        # never below its end
    assert h.limit == h.end and h.new_ptr(0x6000) == 0 and h.error == memFullErr
    h.set_limit(0x8000)
    h.grow(0x9000)                                             # only as far as its limit
    assert h.end == 0x8000 and ends[-1] == 0x8000 and h.room_to_grow() == 0


def test_asking_for_more_than_there_is_grows_the_zone_all_the_way():
    # How programs written before MaxApplZone got the whole heap.
    m = RAM(0x10000)
    h = Heap(m, 0x1000, 0x2000, 0x8000)
    assert h.new_handle(0x100000) == 0 and h.error == memFullErr
    assert h.end == 0x8000 and m.r32(h.start) == 0x8000
    free = 0
    at = h.start + 0x34                     # the first block, walked as a program does
    while at != h.end:
        header = m.r32(at)
        free += header & 0xFFFFFF if header >> 30 == FREE else 0
        at += header & 0xFFFFFF
    assert free > 0x6000


def test_application_heap_grows_by_the_traps(qapp):
    g = Guest()
    proc = g.process
    heap, world = proc.heap, proc.world
    limit = world.r32(lowmem.ApplLimit)
    assert heap.end < limit == heap.limit <= proc.partition.stack_base
    largest, growth = g("MaxMem", 0)
    assert growth == limit - heap.end and largest < growth
    free = g("FreeMem", 0)                  # zcbFree: not what the zone could grow by
    assert free == g.mem.r32(heap.start + ZCB_FREE) == heap.free_bytes(grow=False) < growth
    g("SetApplLimit", limit - 0x1000)
    assert world.r32(lowmem.ApplLimit) == heap.limit == limit - 0x1000 and g.mem_err == noErr
    g("MaxApplZone")
    assert heap.end == heap.limit == world.r32(lowmem.HeapEnd) and g("MaxMem", 0)[1] == 0
    g("SetApplLimit", heap.end - 0x100)                        # below HeapEnd: too low
    assert g.mem_err == memFullErr and heap.limit == heap.end
    assert world.r32(lowmem.ApplLimit) == heap.end - 0x100


def test_the_stack_is_what_defltstack_says(qapp):
    """The Fool's Errand moves ApplLimit down by what DefltStack is short
    of the 32K of stack it wants."""
    g = Guest()
    world = g.process.world
    stack = g.mem.r32(lowmem.DefltStack)
    assert stack >= 0x8000 and g.mem.r32(lowmem.MinStack) <= stack
    assert 0 <= world.r32(lowmem.CurStackBase) - world.r32(lowmem.ApplLimit) - stack < 4


def test_reserve_mem_says_whether_theres_room(qapp):
    g = Guest()
    g("ReserveMem", 0x1000, 0)
    assert g.mem_err == noErr
    g("ReserveMem", 0x7FFFFF, 0)
    assert g.mem_err == memFullErr


def test_new_rgn_sets_mem_err(qapp):
    """The Prince quits when MemErr isn't noErr after NewRgn; a PurgeMem
    for more than the zone has left it at memFullErr just before."""
    g = Guest()
    g("PurgeMem", 0x7FFFFFFF, 0)
    assert g.mem_err == memFullErr
    assert g("NewRgn") and g.mem_err == noErr


def test_a_region_whose_handle_is_in_no_heap_is_left_as_it_is(qapp):
    """MacTriv passes RectRgn the address of its rectangle, on its stack,
    for the region, and the region for the rectangle."""
    g = Guest()
    rgn = g("NewRgn")
    empty = g.contents(rgn)
    local = g.process.heap.limit + 0x100    # where no heap is
    rect = struct.pack(">4h", 0, 0, 40, 60)
    g.mem.write(local, rect)
    low = g.mem.read(0, 10)                 # where its "master pointer" points
    g("RectRgn", local, rgn)
    assert g.mem.read(local, 8) == rect and g.mem.read(0, 10) == low
    assert g.contents(rgn) == empty


def test_reserve_mem_grows_the_application_zone(qapp):
    """For what it reserves, and as far as it can when that's not enough:
    The Fool's Errand asks for 8 MB in place of MaxApplZone, then sizes its
    buffers by what MaxMem says is free."""
    g = Guest()
    heap = g.process.heap
    largest, growth = g("MaxMem", 0)
    g("ReserveMem", largest + 0x2000, 0)
    assert g.mem_err == noErr and heap.end < heap.limit
    assert g("MaxMem", 0)[0] >= largest + 0x2000
    g("ReserveMem", 0x7A1200, 0)
    assert g.mem_err == memFullErr and heap.end == heap.limit
    largest, growth = g("MaxMem", 0)
    assert growth == 0 and largest > 0x10000


def test_world_holds_globals_until_switched_in():
    m = RAM(0x4000)
    a, b = lowmem.World(m), lowmem.World(m)
    a.w32(lowmem.WindowList, 0x1234)
    b.w32(lowmem.WindowList, 0x5678)
    assert m.r32(lowmem.WindowList) == 0
    a.switch_in()
    assert m.r32(lowmem.WindowList) == 0x1234
    m.w32(lowmem.WindowList, 0x9999)              # the app changes it itself
    assert a.r32(lowmem.WindowList) == 0x9999
    a.switch_out()
    b.switch_in()
    assert m.r32(lowmem.WindowList) == 0x5678
    assert a.r32(lowmem.WindowList) == 0x9999
    with pytest.raises(KeyError):
        a.r32(lowmem.MemTop)                      # not a per-application global


def test_hand_to_hand_leaves_the_flags_as_d0(qapp):
    """It's numbered as a Toolbox trap but ends as the OS's do, with the
    flags set from D0: Radical Castle branches on them straight after."""
    from guest import Processor
    m = Processor()
    h = m.handle(b"abcd")
    copy = struct.pack(">HIHHH", 0x207C, h, 0xA9E1, 0x56C7, 0x5BC6)     # ...; SNE D7; SMI D6
    m.cpu.sr = 0x2008                                       # N set, Z clear
    m.run(copy)
    assert (m.cpu.d(0), m.cpu.d(7) & 0xFF, m.cpu.d(6) & 0xFF) == (0, 0, 0)
    assert m.contents(m.cpu.a(0)) == b"abcd" and m.cpu.a(0) != h
    m.run(struct.pack(">HIHHH", 0x207C, 0, 0xA9E1, 0x56C7, 0x5BC6))     # a nil handle
    assert m.cpu.d(0) & 0xFFFF == 0x10000 - 109             # nilHandleErr
    assert (m.cpu.d(7) & 0xFF, m.cpu.d(6) & 0xFF) == (0xFF, 0xFF)


def test_munger_finds_replaces_inserts_and_deletes(qapp):
    """Without a length a string isn't read, so its pointer needn't point
    anywhere: Script Editor deletes with a replacement of -1."""
    g = Guest()
    h = g.handle(b"one two three")
    two, xyz = g.ptr(b"two"), g.ptr(b"XYZ")
    assert g("Munger", h, 0, two, 3, 0, 0) == 4                 # found, not replaced
    assert g("Munger", h, 0, two, 3, xyz, 3) == 7
    assert g.contents(h) == b"one XYZ three"
    assert g("Munger", h, 3, 0, 4, 0xFFFFFFFF, 0) == 3          # 4 bytes deleted
    assert g.contents(h) == b"one three"
    assert g("Munger", h, 0, 0, 0, xyz, 3) == 3                 # inserted
    assert g.contents(h) == b"XYZone three"
    assert g("Munger", h, 0, 0, -1, 0xFFFFFFFF, 0) == 0         # to the end
    assert g.contents(h) == b""
    assert g("Munger", g.handle(b"abc"), 0, two, 3, xyz, 3) & 0xFFFFFFFF == 0xFFFFFFFF


def test_a_negative_size_is_too_big_to_have(qapp):
    """Not none: Run for the Money works its offscreen buffer's size out in
    16 bits, and on a 640 by 480 screen draws over its code in 0 bytes."""
    g = Guest()
    for name in ("NewPtr", "NewHandle"):
        assert g(name, 80 * 480 - 0x10000, False, False) == 0
        assert g.mem_err == memFullErr
        assert g(name, 0, False, False) != 0 and g.mem_err == noErr


def test_the_zone_record_keeps_its_free_bytes_and_master_pointers():
    """zcbFree counts the zone's free blocks, and hFstFree heads the list
    of free master pointers, each holding the next: Excel 1.0 reads both."""
    m, h = heap()

    def counted():
        return sum(n for _, n in h._free)
    assert m.r32(h.start + ZCB_FREE) == counted() > 0
    p, hd = h.new_ptr(100), h.new_handle(50)
    assert m.r32(h.start + ZCB_FREE) == counted()
    h.set_handle_size(hd, 400)
    h.set_ptr_size(p, 20)
    assert m.r32(h.start + ZCB_FREE) == counted()
    h.dispose_ptr(p)
    h.dispose_handle(hd)
    h.compact()
    assert m.r32(h.start + ZCB_FREE) == counted()
    # Excel's MoreMasters: an empty list, and as many as it wants
    m.w32(h.start + H_FST_FREE, 0)
    m.w16(h.start + MORE_MAST, 200)
    assert h.more_masters()
    last = m.r32(h.start + H_FST_FREE)
    first = last - 199 * 4
    assert m.r32(first) == 0 and m.r32(first + 4) == first and m.r32(last) == last - 4
    assert m.r8(first - HEADER) >> 6 == NONRELOCATABLE
    assert h.new_handle(8) == last and h.new_handle(8) == last - 4
    assert m.r32(h.start + H_FST_FREE) == last - 8
    h.dispose_handle(last)
    assert m.r32(h.start + H_FST_FREE) == last and m.r32(last) == last - 8


def test_a_free_master_pointer_list_written_over_is_let_go():
    """A program that stores through a handle it has disposed of spoils the
    list; the zone stops at the spoiled link and makes more."""
    m, h = heap()
    a, b = h.new_handle(8), h.new_handle(8)
    h.dispose_handle(b)
    data = h.deref(a)
    m.w32(b, data)                              # stored through b, after it went
    c = h.new_handle(8)
    d = h.new_handle(8)
    assert c == b and d not in (a, c) and d != data and h.deref(a) == data


def test_init_zone_makes_a_zone_and_the_current_zone(qapp):
    """In a block of the application heap, or, as Winter Games makes its
    own, in memory no zone has; blocks then come from it until SetZone."""
    g = Guest()
    appl = g.process.heap.start
    block = g("NewPtr", 0x2000, 0, 0)
    free = g.process.partition.end + 0x1000
    for start, limit in ((block, block + 0x2000), (free, free + 0x2A80)):
        pb = g.ptr(struct.pack(">IIHI", start, limit, 16, 0))
        g("InitZone", pb)
        assert g.mem_err == noErr and g("GetZone") == start
        assert g.mem.r16(start + MORE_MAST) == 16
        p, h = g("NewPtr", 0x100, 0, 0), g("NewHandle", 0x100, 0, 0)
        assert start < p < limit and start < g.mem.r32(h) < limit
        assert g("HandleZone", h) == g("PtrZone", p) == start
        g("DisposePtr", p)
        assert g.mem_err == noErr
        g("SetZone", appl)
        p = g("NewPtr", 0x100, 0, 0)
        assert g("PtrZone", p) == appl
    # Too small to hold its header, trailer and master pointers.
    g("InitZone", g.ptr(struct.pack(">IIHI", free, free + 0x40, 16, 0)))
    assert g.mem_err == memFullErr and g("GetZone") == appl


def test_a_zone_made_in_the_system_heap_outlives_the_application(qapp):
    """As After Dark 3.0's INIT makes one there for its modules; a zone in
    the application's heap goes with it."""
    g = Guest()
    in_system = g.system.sys_heap.new_ptr(0x2000)
    in_appl = g("NewPtr", 0x2000, 0, 0)
    for start in (in_system, in_appl):
        g("InitZone", g.ptr(struct.pack(">IIHI", start, start + 0x2000, 16, 0)))
        assert g.mem_err == noErr and g.system.zone(start) is not None
    g.system.close_partition(g.process.partition)
    assert g.system.zone(in_system) is not None
    assert g.system.zone(in_appl) is None


def test_dequeue_from_a_queue_that_goes_round_ends(qapp):
    """An element enqueued again while it's the last links it to itself:
    Dequeue of one that isn't there then stops where the queue starts over."""
    g = Guest()
    header, a, b, c = (g.ptr(bytes(10)) for _ in range(4))
    for elem in (a, b, b):
        g("Enqueue", elem, header)
    assert g.mem.r32(b) == b                    # b's link is b
    assert g("Dequeue", c, header) == -1        # qErr: not in it
    assert g("Dequeue", a, header) == 0
    assert g.mem.r32(header + 2) == b


def test_a_limit_past_the_stack_leaves_the_stack(qapp):
    """Macify 2.7 sets ApplLimit 128K past ApplZone, past the stack of its
    75K partition, then calls MaxApplZone and loads a segment: the heap
    stops DefltStack short of the stack's base, and the segment can't land
    on the stack."""
    g = Guest()
    proc = g.process
    heap, world = proc.heap, proc.world
    limit, stack_base = world.r32(lowmem.ApplLimit), proc.partition.stack_base
    assert limit == (stack_base - g.mem.r32(lowmem.DefltStack)) & ~3
    g("SetApplLimit", stack_base + 0x10000)
    assert g.mem_err == noErr and world.r32(lowmem.ApplLimit) == limit
    g("MaxApplZone")
    assert heap.end == heap.limit == limit == world.r32(lowmem.HeapEnd)


def test_set_appl_limit_leaves_a_stack_of_defltstack(qapp):
    """SetApplLimit "enforces a minimum stack size equal to DefltStack"
    (Inside Macintosh: Memory errata).  Bumptz Science Carnival sets the
    limit at its stack pointer, then calls MaxApplZone and moves a new
    handle high: the block lands below the stack, not on it."""
    g = Guest()
    proc = g.process
    heap, world = proc.heap, proc.world
    stack_base = proc.partition.stack_base
    g("SetApplLimit", stack_base - 0x100)
    least = (stack_base - g.mem.r32(lowmem.DefltStack)) & ~3
    assert g.mem_err == noErr and world.r32(lowmem.ApplLimit) == heap.limit == least
    g("SetApplLimit", least - 0x1000)                          # a bigger stack, as asked
    assert world.r32(lowmem.ApplLimit) == heap.limit == least - 0x1000
    g.mem.w32(lowmem.DefltStack, 0x4000)
    g("SetApplLimit", stack_base)
    assert world.r32(lowmem.ApplLimit) == heap.limit == (stack_base - 0x4000) & ~3
    g.mem.w32(lowmem.DefltStack, 0x8000)
    g("SetApplLimit", stack_base - 0x1E)                       # where its stack pointer is
    g("MaxApplZone")
    assert heap.end == least
    h = g("NewHandle", 0x1F4, 0, 0)
    g("MoveHHi", h)
    assert g.mem_err == noErr and g.heap.deref(h) + 0x1F4 <= least


# FUNCTION MyGrowZone(cbNeeded: Size): LongInt, logging cbNeeded and
# GZRootHnd at the address patched in at 2, then disposing of the handle
# after them, if there is one, and answering its size.
GROW_ZONE = bytes.fromhex(
    "207C00000000 20EF0004 20F80328 2610 4290 4A83 6710"
    "2043 A025 2800 2043 A023 2F440008 6004"
    "42AF0008 205F 588F 4ED0")


class Runner(Processor):
    """A Processor whose calls to 68k code run it, nested, as the
    runtime's do."""

    call = runtime.Runner.call

    def __init__(self):
        super().__init__()
        self.stack_top = self.ptr(bytes(0x1000)) + 0x1000
        self.cpu.sp = self.stack_top
        self.cpu.sr = 0x2000
        self.stop = self.ptr(bytes(4))
        self.thread_id = threading.get_ident()

    def _jsr(self, addr):
        cpu = self.cpu
        pc = cpu.pc
        cpu.push32(self.stop)
        cpu.pc = addr
        while (ev := cpu.run(self.stop)).kind == "trap":
            auto_pop = dispatch.is_toolbox(ev.code) and ev.code & 0x400
            cpu.pc = cpu.pop32() if auto_pop else ev.pc + 2
            dispatch.dispatch(self, ev.code, ev.pc)
        assert ev.kind == "until", ev
        cpu.pc = pc


def test_set_grow_zone_installs_a_function_the_memory_manager_calls(qapp):
    """In the current zone's gzProc; called from 68k with cbNeeded, the
    handle being grown in GZRootHnd while it runs (Inside Macintosh:
    Memory, "MyGrowZone")."""
    g = Runner()
    heap = g.process.heap
    log = g.ptr(bytes(12))
    code = bytearray(GROW_ZONE)
    code[2:6] = struct.pack(">I", log)
    proc = g.ptr(bytes(code))
    g("SetGrowZone", proc)
    assert g.mem_err == noErr and g.mem.r32(heap.start + GZ_PROC) == proc
    grown = g("NewHandle", 0x100, 0, 0)
    spare = g("NewHandle", 0x400, 0, 0)
    g.mem.w32(log + 8, spare)
    g.mem.w32(lowmem.GZRootHnd, 0x1234)
    g("SetApplLimit", heap.end)
    g("NewPtr", heap.max_block(grow=False), 0, 0)
    g("SetHandleSize", grown, 0x300)
    assert g.mem_err == noErr
    assert struct.unpack(">III", g.mem.read(log, 12)) == (0x308, grown, 0)
    assert not heap.is_handle(spare) and heap.handle_size(grown) == 0x300
    assert g.mem.r32(lowmem.GZRootHnd) == 0x1234 and g.cpu.sp == g.stack_top
