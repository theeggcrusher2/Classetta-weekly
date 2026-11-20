"""Read Apple's disk images: NDIF (Disk Copy 6) and UDIF (.dmg).

Both split the virtual disk into chunks, zero-fill what they don't
store, and no more.  NDIF (the New Disk Image Format Mac OS 8 and 9
wrote) keeps its chunk map in the resource fork, in the 'bcem' resource
128.  UDIF (the Universal Disk Image Format Mac OS X wrote) keeps it in
an XML property list, located by a 512-byte 'koly' trailer at the end of
the file (or at its start, in early ones).

A chunk may be stored raw, ADC-compressed (Apple Data Compression), zlib
(UDIF only) or left zero-filled; unallocated sectors read as zeros.
Other UDIF encodings -- bzip2, LZFSE and lzma -- are recognised and
refused by name, as are segmented images, images carrying more than one
block table, and NDIF's KenCode compression.

Written from libmodi's analysis of the Apple disk image types, Jonathan
Levin's "Demystifying the DMG File Format", Old File Viewer's NDIF notes
and ndif2raw (github.com/mhjacobson/ndif2raw).  No code is carried over;
the only dependency is the standard library's zlib.
"""

import plistlib
import struct
import zlib

from .resfork import COMPRESSED_TAG, InvalidForkError, decompress, open_fork

SECTOR = 512
KOLY = b"koly"
KOLY_SIZE = 512
MISH = b"mish"
MAX_SECTORS = 1 << 24                  # 8 GB: the most this reader unpacks
MAX_RUNS = 1 << 16

# UDIF chunk types, from libmodi's analysis
ZERO_FILL = 0x00000000
UNCOMPRESSED = 0x00000001
IGNORED = 0x00000002                   # Apple_Free: reads as zeros
COMMENT = 0x7FFFFFFE
CHUNK_ADC = 0x80000004
CHUNK_ZLIB = 0x80000005
CHUNK_BZIP2 = 0x80000006
CHUNK_LZFSE = 0x80000007
CHUNK_LZMA = 0x80000008
TERMINATOR = 0xFFFFFFFF
UNSUPPORTED_CHUNKS = {
    CHUNK_BZIP2: "bzip2 (UDBZ)",
    CHUNK_LZFSE: "LZFSE (ULFO)",
    CHUNK_LZMA: "lzma (ULMO)",
}

# NDIF chunk types, from Disk Copy 6's 'bcem' table
NDIF_ZERO = 0
NDIF_RAW = 2
NDIF_KEN_CODE = 128
NDIF_ADC = 131
NDIF_TERMINATOR = 255
RES_COMPRESSED = 0x01                  # resCompressed, in a resource's attributes


class DmgError(ValueError):
    """A disk image this reader won't read: damaged, or a feature it lacks."""


# --- the 'koly' trailer, shared by every UDIF ----------------------------

def _koly_at(data, at: int) -> bool:
    if at < 0 or at + KOLY_SIZE > len(data):
        return False
    return data[at:at + 4] == KOLY and struct.unpack_from(">I", data, at + 8)[0] == KOLY_SIZE


def _find_koly(data) -> int | None:
    n = len(data)
    for at in (n - KOLY_SIZE, 0):       # the end, normally; the start, in early images
        if _koly_at(data, at):
            return at
    return None


def is_udif(data) -> bool:
    """Whether data looks like a UDIF image: a 'koly' trailer to be found."""
    return _find_koly(data) is not None


# --- ADC, Apple Data Compression, three opcodes ---------------------------

def _adc_copy(out: bytearray, dist: int, length: int, dst_size: int) -> None:
    if dist > len(out):
        raise DmgError("the disk image is damaged (an ADC copy reaches before its chunk)")
    if len(out) + length > dst_size:
        raise DmgError("the disk image is damaged (an ADC copy overruns its chunk)")
    for _ in range(length):
        out.append(out[-dist])


def _adc(src, dst_size: int) -> bytes:
    src = bytes(src)
    out = bytearray()
    i, n = 0, len(src)
    while i < n and len(out) < dst_size:
        cmd = src[i]
        i += 1
        if cmd & 0x80:                                   # 1xxxxxxx: literal
            length = (cmd & 0x7F) + 1
            if i + length > n:
                raise DmgError("the disk image is damaged (an ADC literal runs off the end)")
            if len(out) + length > dst_size:
                raise DmgError("the disk image is damaged (an ADC literal overruns its chunk)")
            out += src[i:i + length]
            i += length
        elif cmd & 0x40:                                 # 01xxxxxx: 16-bit distance
            length = (cmd & 0x3F) + 4
            if i + 2 > n:
                raise DmgError("the disk image is damaged (an ADC copy runs off the end)")
            dist = ((src[i] << 8) | src[i + 1]) + 1
            i += 2
            _adc_copy(out, dist, length, dst_size)
        else:                                            # 00xxxxyy: 10-bit distance
            length = (cmd >> 2) + 3
            if i + 1 > n:
                raise DmgError("the disk image is damaged (an ADC copy runs off the end)")
            dist = (((cmd & 0x03) << 8) | src[i]) + 1
            i += 1
            _adc_copy(out, dist, length, dst_size)
    if len(out) != dst_size:
        raise DmgError(f"the disk image is damaged (an ADC chunk expanded to {len(out)} of {dst_size} bytes)")
    return bytes(out)


# --- UDIF -----------------------------------------------------------------

def _mish(payload: bytes, what: str) -> tuple[int, int, list]:
    if len(payload) < 204 + 4:
        raise DmgError(f"the UDIF image's {what} block table is cut short")
    sig, version = struct.unpack_from(">4sI", payload, 0)
    if sig != MISH:
        raise DmgError(f"the UDIF image's {what} block table is damaged (no mish signature)")
    if version != 1:
        raise DmgError(f"the UDIF image is unsupported (block table version {version})")
    start_sector, sector_count, data_offset = struct.unpack_from(">QQQ", payload, 8)
    num_runs, = struct.unpack_from(">I", payload, 200)
    if num_runs > MAX_RUNS:
        raise DmgError(f"the UDIF image's {what} block table is damaged (too many runs)")
    if 204 + num_runs * 40 > len(payload):
        raise DmgError(f"the UDIF image's {what} block table is cut short")
    runs = []
    at = 204
    for _ in range(num_runs):
        rtype, _comment, rstart, rcount, roff, rsize = struct.unpack_from(">IIQQQQ", payload, at)
        runs.append({"type": rtype, "start": rstart, "count": rcount,
                     "offset": roff + data_offset, "size": rsize})
        at += 40
    return start_sector, sector_count, runs


def read_udif(data) -> bytes:
    """The virtual disk a UDIF image holds, its chunks inflated."""
    at = _find_koly(data)
    if at is None:
        raise DmgError("not a UDIF image (no koly trailer)")
    trailer = data[at:at + KOLY_SIZE]
    version, _header_size = struct.unpack_from(">II", trailer, 4)
    if version != 4:
        raise DmgError(f"the UDIF image is unsupported (its version is {version})")
    sector_count, = struct.unpack_from(">Q", trailer, 492)
    xml_offset, xml_length = struct.unpack_from(">QQ", trailer, 216)
    seg_number, seg_count = struct.unpack_from(">II", trailer, 56)
    if seg_count > 1 or seg_number > 1:
        raise DmgError("the UDIF image is unsupported (it is split across files)")
    if xml_length == 0 or xml_offset + xml_length > len(data):
        raise DmgError("the UDIF image is damaged (the property list runs off its end)")
    if xml_length > 1 << 26:
        raise DmgError("the UDIF image is damaged (the property list is too big)")
    try:
        xml = plistlib.loads(bytes(data[xml_offset:xml_offset + xml_length]))
    except Exception as e:      # noqa: BLE001 - any parser failure is one refusal
        raise DmgError(f"the UDIF image is damaged (the property list won't parse: {e})") from None
    try:
        tables = xml["resource-fork"]["blkx"]
    except (KeyError, TypeError):
        raise DmgError("the UDIF image is damaged (the blkx table is missing from its property list)") from None
    if not isinstance(tables, list):
        raise DmgError("the UDIF image is damaged (the blkx table is not a list)")
    if len(tables) != 1:
        raise DmgError(f"the UDIF image is unsupported (it carries {len(tables)} block tables)")
    entry = tables[0]
    payload = entry.get("Data") if isinstance(entry, dict) else None
    if not isinstance(payload, (bytes, bytearray)):
        raise DmgError("the UDIF image is damaged (a block table has no data)")
    start_sector, table_sectors, runs = _mish(bytes(payload), "blkx")
    if sector_count == 0:
        sector_count = start_sector + table_sectors
    if sector_count == 0:
        raise DmgError("the UDIF image is damaged (it holds no sectors)")
    if sector_count > MAX_SECTORS:
        raise DmgError(f"the disk image is too big to read ({sector_count} sectors)")
    out = bytearray(sector_count * SECTOR)
    for run in runs:
        rtype = run["type"]
        if rtype == TERMINATOR:
            break
        expanded = run["count"] * SECTOR
        dest = (start_sector + run["start"]) * SECTOR
        if dest + expanded > len(out):
            raise DmgError("the UDIF image is damaged (a chunk runs past its disk)")
        if rtype in (ZERO_FILL, IGNORED) or run["count"] == 0:
            continue
        if rtype == COMMENT:
            continue
        at2 = run["offset"]
        if at2 + run["size"] > len(data):
            raise DmgError("the UDIF image is damaged (a chunk runs past its end)")
        if rtype == UNCOMPRESSED:
            if run["size"] == 0:
                continue                                # a sparse hole
            if run["size"] < expanded:
                raise DmgError("the UDIF image is damaged (an uncompressed chunk is too small)")
            out[dest:dest + expanded] = data[at2:at2 + expanded]
        elif rtype == CHUNK_ADC:
            out[dest:dest + expanded] = _adc(data[at2:at2 + run["size"]], expanded)
        elif rtype == CHUNK_ZLIB:
            try:
                dec = zlib.decompress(bytes(data[at2:at2 + run["size"]]))
            except zlib.error as e:
                raise DmgError(f"the UDIF image is damaged (a zlib chunk won't decompress: {e})") from None
            if len(dec) != expanded:
                raise DmgError(f"the UDIF image is damaged (a zlib chunk expanded to {len(dec)} of {expanded} bytes)")
            out[dest:dest + expanded] = dec
        elif rtype in UNSUPPORTED_CHUNKS:
            raise DmgError(f"the UDIF encoding is unsupported ({UNSUPPORTED_CHUNKS[rtype]})")
        else:
            raise DmgError(f"the UDIF encoding is unsupported (chunk type 0x{rtype:08x})")
    return bytes(out)


# --- NDIF -----------------------------------------------------------------

def is_ndif_fork(rsrc) -> bool:
    """Whether a resource fork holds the 'bcem' chunk map 128 NDIF images carry."""
    if not rsrc or len(rsrc) < 256:
        return False
    try:
        fm = open_fork(rsrc)
    except InvalidForkError:
        return False
    return any(e.type == b"bcem" and e.id == 128 for e in fm.entries)


def _bcem(rsrc) -> bytes:
    if not rsrc or len(rsrc) < 256:
        raise DmgError("the NDIF image's resource fork is missing or empty")
    try:
        fm = open_fork(rsrc)
    except InvalidForkError as e:
        raise DmgError(f"the NDIF image's resource fork is damaged ({e})") from None
    body = None
    for e in fm.entries:
        if e.type == b"bcem" and e.id == 128:
            try:
                body = fm.raw(e)
            except InvalidForkError as e2:
                raise DmgError(f"the NDIF image's bcem resource is damaged ({e2})") from None
            if e.attrs & RES_COMPRESSED and body[:4] == COMPRESSED_TAG:
                try:
                    body = decompress(body)
                except Exception as e2:      # noqa: BLE001 - one refusal, whatever it was
                    raise DmgError(f"the NDIF image's bcem resource won't decompress ({e2})") from None
            break
    if body is None:
        raise DmgError("the NDIF image has no bcem resource (its resource fork is missing)")
    if len(body) < 128:
        raise DmgError("the NDIF image's bcem resource is cut short")
    return body


def read_ndif(data, rsrc) -> bytes:
    """The virtual disk a NDIF image holds, its chunks inflated."""
    body = _bcem(rsrc)
    version, _fsid = struct.unpack_from(">HH", body, 0)
    if version not in (10, 11, 12):
        raise DmgError(f"the NDIF image is unsupported (its version is {version})")
    disk_blocks, _largest, backing_offset, _crc32, _segmented = struct.unpack_from(">IIIII", body, 68)
    nchunk, = struct.unpack_from(">I", body, 124)
    if 128 + nchunk * 12 > len(body):
        raise DmgError("the NDIF image is damaged (its chunk table is cut short)")
    if disk_blocks == 0 or disk_blocks > MAX_SECTORS:
        raise DmgError(f"the NDIF image is unsupported (it holds {disk_blocks} sectors)")
    chunks = []
    at = 128
    for _ in range(nchunk):
        logical = (body[at] << 16) | (body[at + 1] << 8) | body[at + 2]
        ctype = body[at + 3]
        boffset, bsize = struct.unpack_from(">II", body, at + 4)
        chunks.append((logical, ctype, boffset, bsize))
        at += 12
    out = bytearray(disk_blocks * SECTOR)
    for i, (start, ctype, boffset, bsize) in enumerate(chunks):
        if ctype == NDIF_TERMINATOR:
            break
        nxt = chunks[i + 1][0] if i + 1 < len(chunks) else disk_blocks
        end = min(nxt, disk_blocks)
        if end <= start:
            raise DmgError("the NDIF image is damaged (its chunk table runs backwards)")
        span = (end - start) * SECTOR
        dest = start * SECTOR
        if dest + span > len(out):
            raise DmgError("the NDIF image is damaged (a chunk runs past its disk)")
        if ctype == NDIF_ZERO:
            continue
        if ctype == NDIF_RAW:
            if boffset + span > len(data):
                raise DmgError("the NDIF image is damaged (a raw chunk runs past its data fork)")
            out[dest:dest + span] = data[boffset:boffset + span]
        elif ctype == NDIF_ADC:
            at2 = backing_offset + boffset
            if at2 + bsize > len(data):
                raise DmgError("the NDIF image is damaged (an ADC chunk runs past its data fork)")
            out[dest:dest + span] = _adc(data[at2:at2 + bsize], span)
        elif ctype == NDIF_KEN_CODE:
            raise DmgError("the NDIF encoding is unsupported (KenCode)")
        else:
            raise DmgError(f"the NDIF encoding is unsupported (chunk type {ctype})")
    return bytes(out)
