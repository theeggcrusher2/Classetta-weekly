"""
memory.py: the Mac's RAM as a big-endian byte array, with annotated views
of the records stored in it.

Everything the Mac program sees is located in here: low-memory globals,
the heap, and window/menu records.

Python classes such as WindowRecord are views that use this as a backing
store. They contain a reference to an address inside the Mac's memory,
and their variables are read from / written to the Mac's memory, so Classetta
is fully compatible with Mac apps that write directly to such structures
without going via the Toolbox (a bit naughty, but quite common).

When in 24-bit mode, addresses are 24-bit: the top byte is masked off.
$00112233 is the same memory address as $99112233, but for clarity we'll
output it as just $112233.

In 24 bit mode, the Memory Manager kept flags in a master pointer's high
byte. This was a "clever" trick to save a bit of RAM, but it backfired
when 32 bit mode came into existance to allow Macs to have >8MB of RAM,
as apps (and Apple's own Toolbox code) sometimes messed with these
values directly - only now they were part of the address. We try to retain
compatibility with this for very old apps that rely on it.

Some machines with smaller amounts of RAM than the maximum may not decode
all address lines i.e. multiple copies of memory will be "mirrored" throughout
the address space. We allow for this on the Mac Plus.

In 32-bit mode, the full pointer is used, and no such tricks are employed.

The emulated M68k CPU (macrt.m68k) runs using this byte array for RAM.
It makes use of JIT translation for speed, so when we write any code to
the ROM, we must ensure that page is invalidated in the translation cache.
"""

from __future__ import annotations

import struct
from typing import Any, Callable, NamedTuple

from .toolbox.types import Point, Rect


class BusError(Exception):
    """An access outside RAM."""


PAGE_SHIFT = 12


class RAM:
    def __init__(self, system, size: int):
        self.system = system
        self.size = size
        self._b = bytearray(size)
        self._v = memoryview(self._b)   # to copy from without copying twice
        # Pages written from Python, by number: [start, end) of what's
        # been written in each.
        self.dirty: dict[int, list[int]] = {}
        self.mirror_lo = self.mirror_hi = size      # none, until mirror()
        self.mirror_by = 0

        mask = self.addr_mask
        width = 8 if mask == 0xFFFFFFFF else 6
        self.addr_str = lambda value: f"${(value & mask):0{width}X}"

    @property
    def addr_mask(self) -> int:
        return self.system.addr_mask

    def mirror(self, lo: int, hi: int, to: int) -> None:
        """Make the addresses from lo to hi another way to the memory from
        to: as a Mac Plus's RAM repeats through its 4 MB of addresses, so
        that $3FA700 is its screen whatever RAM it has.  lo, hi and to are
        on the CPU's 4K pages."""
        self.mirror_lo, self.mirror_hi, self.mirror_by = lo, hi, lo - to

    def physical(self, addr: int) -> int:
        """Where in the bytes an address is."""
        a = addr & self.system.addr_mask
        return a - self.mirror_by if self.mirror_lo <= a < self.mirror_hi else a

    def _at(self, addr: int, n: int) -> int:
        a = addr & self.system.addr_mask
        if self.mirror_lo <= a < self.mirror_hi:
            a -= self.mirror_by
        if a + n > self.size:
            raise BusError(f"access to {self.addr_str(a)} is outside RAM")
        return a

    def _wat(self, addr: int, n: int) -> int:
        """_at for a write: note what it writes."""
        a = addr & self.system.addr_mask
        if self.mirror_lo <= a < self.mirror_hi:
            a -= self.mirror_by
        end = a + n
        if end > self.size:
            raise BusError(f"access to {self.addr_str(a)} is outside RAM")
        if n:
            first, last = a >> PAGE_SHIFT, (end - 1) >> PAGE_SHIFT
            if first == last:
                span = self.dirty.get(first)
                if span is None:
                    self.dirty[first] = [a, end]
                else:
                    if a < span[0]:
                        span[0] = a
                    if end > span[1]:
                        span[1] = end
            else:                       # to the end of the first, from the start of the last
                dirty, after, before = self.dirty, (first + 1) << PAGE_SHIFT, last << PAGE_SHIFT
                span = dirty.get(first)
                if span is None:
                    dirty[first] = [a, after]
                else:
                    span[0], span[1] = min(span[0], a), after
                for p in range(first + 1, last):
                    dirty[p] = [p << PAGE_SHIFT, (p + 1) << PAGE_SHIFT]
                span = dirty.get(last)
                if span is None:
                    dirty[last] = [before, end]
                else:
                    span[0], span[1] = before, max(span[1], end)
        return a

    def take_dirty(self) -> list[tuple[int, int]]:
        """What's been written since the last call, as (start, end): in
        each page, from the first byte written to the last; merged."""
        if not self.dirty:
            return []
        taken, self.dirty = self.dirty, {}
        if len(taken) == 1:             # as after most traps: what's left on the stack
            (start, end), = taken.values()
            return [(start, end)]
        ranges: list[list[int]] = []
        for p in sorted(taken):
            start, end = taken[p]
            if ranges and ranges[-1][1] == start:
                ranges[-1][1] = end
            else:
                ranges.append([start, end])
        return [(a, b) for a, b in ranges]

    # -- integers ------------------------------------------------------------
    def r8(self, addr: int) -> int:
        return self._b[self._at(addr, 1)]

    def r16(self, addr: int) -> int:
        return struct.unpack_from(">H", self._b, self._at(addr, 2))[0]

    def r32(self, addr: int) -> int:
        return struct.unpack_from(">I", self._b, self._at(addr, 4))[0]

    def r8s(self, addr: int) -> int:
        return struct.unpack_from(">b", self._b, self._at(addr, 1))[0]

    def r16s(self, addr: int) -> int:
        return struct.unpack_from(">h", self._b, self._at(addr, 2))[0]

    def r32s(self, addr: int) -> int:
        return struct.unpack_from(">i", self._b, self._at(addr, 4))[0]

    # Writes take signed or unsigned values and keep the low bits.
    def w8(self, addr: int, value: int) -> None:
        self._b[self._wat(addr, 1)] = value & 0xFF

    def w16(self, addr: int, value: int) -> None:
        struct.pack_into(">H", self._b, self._wat(addr, 2), value & 0xFFFF)

    def w32(self, addr: int, value: int) -> None:
        struct.pack_into(">I", self._b, self._wat(addr, 4), value & 0xFFFFFFFF)

    def unpack(self, layout: struct.Struct, addr: int) -> tuple:
        """layout's values, from the bytes at addr on."""
        return layout.unpack_from(self._b, self._at(addr, layout.size))

    # -- bytes ---------------------------------------------------------------
    def read(self, addr: int, n: int) -> bytes:
        a = self._at(addr, n)
        return self._v[a:a + n].tobytes()

    def same(self, addr: int, data: bytes | bytearray) -> bool:
        """Whether the bytes at addr are data, without reading them out."""
        return self._b.startswith(data, self._at(addr, len(data)))

    def write(self, addr: int, data: bytes) -> None:
        a = self._wat(addr, len(data))
        self._b[a:a + len(data)] = data

    def fill(self, addr: int, n: int, value: int = 0) -> None:
        a = self._wat(addr, n)
        self._b[a:a + n] = bytes([value & 0xFF]) * n

    def move(self, src: int, dst: int, n: int) -> None:
        """BlockMove: overlapping ranges are fine."""
        s, d = self._at(src, n), self._wat(dst, n)
        self._b[d:d + n] = self._v[s:s + n].tobytes()

    # -- Toolbox types -----------------------------------------------------
    def r_pstr(self, addr: int) -> bytes:
        """A Pascal string: a length byte, then that many bytes."""
        return self.read(addr + 1, self.r8(addr))

    def w_pstr(self, addr: int, data: bytes) -> None:
        data = data[:255]
        self.write(addr, bytes([len(data)]) + data)

    def r_point(self, addr: int) -> Point:
        return Point(*struct.unpack_from(">hh", self._b, self._at(addr, 4)))

    def w_point(self, addr: int, pt: Point) -> None:
        struct.pack_into(">HH", self._b, self._wat(addr, 4), pt.v & 0xFFFF, pt.h & 0xFFFF)

    def r_rect(self, addr: int) -> Rect:
        return Rect(*struct.unpack_from(">hhhh", self._b, self._at(addr, 8)))

    def w_rect(self, addr: int, r: Rect) -> None:
        # A coordinate past an INTEGER's range wraps, as the ROM's sums do:
        # Deja Vu's backdrop window is 32768 pixels high.
        struct.pack_into(">HHHH", self._b, self._wat(addr, 8), *(v & 0xFFFF for v in r))


def to_mac(text: str) -> bytes:
    """Python text to Mac Roman, the encoding of every Toolbox string."""
    return text.encode("mac_roman", "replace")


def from_mac(data: bytes) -> str:
    return data.decode("mac_roman")


# ---------------------------------------------------------------------------
# Record views
# ---------------------------------------------------------------------------
class Codec(NamedTuple):
    size: int
    read: Callable[[RAM, int], Any]
    write: Callable[[RAM, int, Any], None]


BYTE = Codec(1, RAM.r8, RAM.w8)
INTEGER = Codec(2, RAM.r16s, RAM.w16)
LONGINT = Codec(4, RAM.r32s, RAM.w32)
# Pointers, handles and flag words: unsigned 32 bits.
LONG = Codec(4, RAM.r32, RAM.w32)
# Pascal's TRUE is 1.  Code tests for nonzero, so read it that way.
BOOLEAN = Codec(1, lambda m, a: m.r8(a) != 0, lambda m, a, v: m.w8(a, 1 if v else 0))
POINT = Codec(4, RAM.r_point, RAM.w_point)
RECT = Codec(8, RAM.r_rect, RAM.w_rect)
PATTERN = Codec(8, lambda m, a: m.read(a, 8), RAM.write)


def _w_rgb(m: RAM, a: int, rgb: tuple[int, int, int]) -> None:
    for i, c in enumerate(rgb):
        m.w16(a + 2 * i, c)


# An RGBColor: red, green and blue, 16 bits each.
RGB = Codec(6, lambda m, a: (m.r16(a), m.r16(a + 2), m.r16(a + 4)), _w_rgb)


class Field:
    """A record field at a fixed offset from the record's address."""

    def __init__(self, offset: int, codec: Codec):
        self.offset = offset
        self.codec = codec

    def __get__(self, rec, owner=None):
        if rec is None:
            return self
        return self.codec.read(rec.mem, rec.addr + self.offset)

    def __set__(self, rec, value) -> None:
        self.codec.write(rec.mem, rec.addr + self.offset, value)


class Record:
    """A view of a record in RAM.  Subclasses declare their Fields."""

    SIZE = 0

    def __init__(self, mem: RAM, addr: int):
        self.mem = mem
        self.addr = addr

    def __repr__(self) -> str:
        return f"<{type(self).__name__} at ${self.addr:06X}>"
