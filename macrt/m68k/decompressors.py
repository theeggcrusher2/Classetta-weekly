"""
Compressed resources whose decompressor rsrcfork hasn't got, such as the
'dcmp' 3 most of System 7.5's utilities' resources are compressed with:
the 'dcmp' resource the header names, found as GetResource finds it (in
the System file, or the application's own), is run on the 68k.

A compressed resource's data starts with a header:

    tag             long, $A89F6572
    headerLength    word, 18
    headerVersion   byte, 8 or 9
    attributes      byte
    size            long, the size expanded
    version 8:      a byte, the working buffer's size as a fraction (of 256)
                    of the data's; a byte, how many more bytes than size
                    the output needs; the 'dcmp' ID; a reserved word
    version 9:      the 'dcmp' ID; a word, how many more bytes the output
                    needs; two bytes for the decompressor

A version 8 decompressor is a Pascal procedure

    dcmp(source, dest, working: Ptr; length: LONGINT)

and a version 9 one

    dcmp(source, dest, header: Ptr)

with the length below its arguments on the stack.  source is the data
after the header, and length how long that is.  A 'dcmp' with its type
in bytes 4 to 7 starts at its first byte; the others start with three
words, the second the offset of where it decompresses.

An application's CODE 0 is read before it has a heap to run a 'dcmp' in:
one compressed with its own 'dcmp' (After Dark 3.0's, launched by its
INIT) is expanded by the application launching it, with the 'dcmp' from
the new application's file.
"""

from __future__ import annotations

import struct

from ..resfork import COMPRESSED_TAG, InvalidForkError, open_fork

HEADER = 18
DCMP = int.from_bytes(b"dcmp", "big")


def _parse(data: bytes) -> tuple[int, int, int, int] | None:
    """The header's version, expanded size, 'dcmp' ID and the room the
    output needs past the size."""
    if len(data) < HEADER or data[:4] != COMPRESSED_TAG:
        return None
    version, size = data[6], struct.unpack_from(">I", data, 8)[0]
    if version == 8:
        extra, dcmp_id = data[13], struct.unpack_from(">h", data, 14)[0]
    elif version == 9:
        dcmp_id, extra = struct.unpack_from(">hH", data, 12)
    else:
        return None
    return version, size, dcmp_id, extra


def expand(t, data: bytes, fork: bytes | None = None) -> bytes | None:
    """The Resource Manager's decompressor: data, a compressed resource,
    expanded by its 'dcmp'.  None if there isn't one, there's no room, or
    the application isn't the one running.  The 'dcmp' is the one
    GetResource finds, or, given a resource fork, that fork's."""
    header = _parse(data)
    if header is None or not t.on_own_thread() or t.expanding:
        return None
    dcmp_id = header[2]
    if fork is None:
        t.expanding = True          # in case the 'dcmp' needs one itself
        try:
            h = t.process.resources.GetResource("dcmp", dcmp_id)
        finally:
            t.expanding = False
        return _run(t, data, header, h) if h and t.mem.r32(h) else None
    try:
        fm = open_fork(fork)
        entry = fm.find(b"dcmp", dcmp_id)
        code = fm.raw(entry) if entry is not None else b""
    except InvalidForkError:
        return None
    heap = t.process.heap
    h = heap.new_handle(len(code)) if code and code[:4] != COMPRESSED_TAG else 0
    if not h:
        return None
    try:
        t.mem.write(heap.deref(h), code)
        return _run(t, data, header, h)
    finally:
        heap.dispose_handle(h)


def _run(t, data: bytes, header: tuple[int, int, int, int], h: int) -> bytes | None:
    """data expanded by the 'dcmp' in h."""
    version, size, dcmp_id, extra = header
    sys_heap = t.system.sys_heap
    heap = t.system.heap_of(h)
    length = len(data) - HEADER
    sizes = [len(data), size + extra]
    if version == 8:
        sizes.append(max(size, length + length * data[12] // 256))
    state = heap.get_state(h)
    heap.lock(h)                # locked first, so making the buffers can't purge it
    buffers = []
    try:
        for n in sizes:
            p = t.process.heap.new_ptr(n) or sys_heap.new_ptr(n)
            if not p:
                return None
            buffers.append(p)
        code = heap.deref(h)
        entry = code if t.mem.r32(code + 4) == DCMP else code + t.mem.r16(code + 2)
        src, dest = buffers[:2]
        t.mem.write(src, data)
        if version == 8:
            t.call(entry, (("p", src + HEADER), ("p", dest), ("p", buffers[2]),
                           ("l", length)))
        else:
            t.call(entry, (("l", length), ("p", src + HEADER), ("p", dest), ("p", src)))
        return t.mem.read(dest, size)
    finally:
        heap.set_state(h, state)
        for p in buffers:
            t.system.heap_of(p).dispose_ptr(p)
