"""Read DART and DiskMaker floppy images: a header, a chunk-length table,
then the chunks themselves.

DART (Disk Archive/Retrieval Tool) was Apple's compressed answer to Disk
Copy 4.2, and DiskMaker wrote the same bytes before it: the guided-tour
images Apple put on its Developer CDs carry type 'DMdf' and creator
'DskM'.  Each 20960-byte chunk holds 40 sectors of a floppy as 40
512-byte blocks followed by 40 sets of 12 tag bytes — the Apple II
tag-RAM bytes, which is why DiskMaker archived ProDOS disks too; on a
Mac's HFS or MFS disk they are zero — so the tags are stripped and the
plain 512-byte sector stream is returned, the form the volume readers
take.  A 400K image is MFS, an 800K one HFS; both mount.

The header names the compression ('fast' word-oriented RLE, 'best'
LZHUF, or none) and the disk's type and size in kiB.  Each chunk's
length sits in the table: in words for 'fast', in bytes for 'best',
20960 or 0xffff for stored, zero past the disk's end, and 0xffff for a
chunk that would not compress.  All multi-byte values are big-endian.

The 'best' LZHUF compression and the Lisa, Apple II and MS-DOS disk
types are recognised and refused by name.

Written from the format notes at ciderpress2.com/formatdoc/DART-notes.html;
no code is carried over and no dependency is added.
"""

import mmap
import struct

HEADER = 4                 # srcCmp, srcType, srcSize
SECTOR = 512
TAG = 12
CHUNKS = 40                # sectors per chunk
DATA = CHUNKS * SECTOR     # 20480
CHUNK = DATA + CHUNKS * TAG      # 20960

FAST, BEST, STORED = 0, 1, 2     # srcCmp

# srcType, and what each disk is
MAC, LISA, APPLE_II, MAC_HD, DOS_720, DOS_1400 = 1, 2, 3, 16, 17, 18
TYPES = {
    MAC: "a Macintosh 400K or 800K floppy",
    LISA: "a Lisa floppy",
    APPLE_II: "an Apple II floppy",
    MAC_HD: "a Macintosh 1440K floppy",
    DOS_720: "an MS-DOS 720K floppy",
    DOS_1400: "an MS-DOS 1400K floppy",
}
MAC_TYPES = (MAC, MAC_HD)

# a disk's size in kiB, and how many chunks that is
SIZES = (400, 720, 800, 1400, 1440)


class DartError(ValueError):
    """A DART/DiskMaker image this reader won't read: damaged, or a form
    it lacks."""


def is_dart(data) -> bool:
    """Whether data looks like a DART or DiskMaker image, from its header
    and chunk-length table alone.  A listing reads only the head of a
    big file, so the chunk total isn't required to fit: the reader
    checks the whole file when the image is opened."""
    if len(data) < HEADER + 2:
        return False
    cmp_, typ, size = data[0], data[1], struct.unpack_from(">H", data, 2)[0]
    if cmp_ not in (FAST, BEST, STORED) or typ not in TYPES or size not in SIZES:
        return False
    n = 72 if size >= 1400 else 40
    if len(data) < HEADER + 2 * n:
        return False
    lens = struct.unpack_from(f">{n}H", data, HEADER)
    if not any(lens):
        return False
    limit = CHUNK if cmp_ != FAST else CHUNK // 2
    for L in lens:
        if L != 0xffff and L > limit:
            return False
        if cmp_ == STORED and L not in (CHUNK, 0xffff):
            return False
    return True


def _fast(chunk: bytes) -> bytes:
    """One chunk through the fast RLE: signed 16-bit word counts, a
    positive one copying that many words, a negative one repeating the
    word after it."""
    out = bytearray()
    at = 0
    while len(out) < CHUNK:
        if at + 2 > len(chunk):
            raise DartError("the DART image is damaged (a chunk's RLE runs short)")
        cnt, = struct.unpack_from(">h", chunk, at)
        at += 2
        if cnt > 0:
            if at + cnt * 2 > len(chunk):
                raise DartError("the DART image is damaged (a chunk's RLE copy runs past its end)")
            out += chunk[at:at + cnt * 2]
            at += cnt * 2
        elif cnt < 0:
            if at + 2 > len(chunk):
                raise DartError("the DART image is damaged (a chunk's RLE pattern is missing)")
            out += chunk[at:at + 2] * (-cnt)
            at += 2
        else:
            raise DartError("the DART image is damaged (a chunk's RLE run count is zero)")
    if len(out) != CHUNK:
        raise DartError(f"the DART image is damaged (a chunk's RLE overflows to {len(out)} bytes)")
    return bytes(out)


def read_image(data: bytes | bytearray | mmap.mmap) -> bytes:
    """The floppy's plain 512-byte sector stream, tag bytes stripped."""
    cmp_, typ, size = data[0], data[1], struct.unpack_from(">H", data, 2)[0]
    if typ not in MAC_TYPES:
        raise DartError(f"the DART image is unsupported (it is {TYPES[typ]}, not a Mac floppy)")
    if cmp_ == BEST:
        raise DartError("the DART image is unsupported (its 'best' LZHUF compression isn't read)")
    n = 72 if size >= 1400 else 40
    lens = struct.unpack_from(f">{n}H", data, HEADER)
    out = bytearray()
    at = HEADER + 2 * n
    for i, L in enumerate(lens):
        if L == 0:
            continue
        if cmp_ == STORED and L not in (CHUNK, 0xffff):
            raise DartError(f"the DART image is damaged (its stored chunk {i} is {L} bytes, not {CHUNK})")
        stored = CHUNK if L == 0xffff else (L * 2 if cmp_ == FAST else L)
        if at + stored > len(data):
            raise DartError(f"the DART image is damaged (its chunk {i} runs past its end)")
        chunk = data[at:at + stored]
        at += stored
        # 0xffff marks a chunk that would not compress: stored as-is,
        # in any mode.
        out += (_fast(chunk) if cmp_ == FAST and L != 0xffff else chunk)[:DATA]
    if len(out) != size * 1024:
        raise DartError(f"the DART image is damaged (its chunks hold {len(out)} bytes, "
                        f"not its {size * 1024})")
    return bytes(out)
