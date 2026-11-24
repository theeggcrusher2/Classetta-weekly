"""Tests for CHD disk images: detection, browsing, failure paths.

The fixtures are written here by a small CHD writer that follows the
same public description the reader does (MAME's chd.h/chd.cpp), so a
round-trip proves the reader against the format, not against itself:
the zlib and LZMA streams come from the standard library, and the
map's CRC-16 and the hunks' CRC-32/CRC-16 are computed independently
of the reader's checks.
"""

import lzma
import struct
import zlib

import machfs
import pytest

from macrt import chd
from macrt.chd import CHDError, _Bits, _Huffman, _crc16
from macrt.macfile import image_kind, is_disk_image, load_volume, open_container, volume_files

HUNK = 16384                      # bytes per hunk
UNIT = 2048                       # bytes per unit: a CD frame
CODEC_ZLIB, CODEC_LZMA = chd.CODEC_ZLIB, chd.CODEC_LZMA


# --- the writer --------------------------------------------------------

class _BitWriter:
    """Bits packed most-significant first, as CHD packs them."""

    def __init__(self):
        self._bits = []

    def write(self, value: int, nbits: int):
        for i in range(nbits - 1, -1, -1):
            self._bits.append((value >> i) & 1)

    def flush(self) -> bytes:
        bits = self._bits + [0] * (-len(self._bits) % 8)
        out = bytearray()
        for i in range(0, len(bits), 8):
            b = 0
            for bit in bits[i:i + 8]:
                b = (b << 1) | bit
            out.append(b)
        return bytes(out)


def _rle_types(types: list[int]) -> list[int]:
    """The map's type stream, run-length coded the way chd.cpp's
    compress_v5_map writes it."""
    out, lastcomp, count = [], 0, 0
    n = len(types)
    for hunknum, curcomp in enumerate(types):
        if curcomp == lastcomp:
            count += 1
        if curcomp != lastcomp or hunknum == n - 1:
            while count:
                if count < 3:
                    out.append(lastcomp)
                    count -= 1
                elif count <= 3 + 15:
                    out += [chd.RLE_SMALL, count - 3]
                    count = 0
                else:
                    this = min(count, 3 + 16 + 255)
                    out += [chd.RLE_LARGE, (this - 3 - 16) >> 4, (this - 3 - 16) & 15]
                    count -= this
            if curcomp != lastcomp:
                out.append(curcomp)
                lastcomp = curcomp
    return out


def _compress(codec: str, raw: bytes) -> bytes:
    if codec == "zlib":
        return zlib.compress(raw, 9)
    filt = [{"id": lzma.FILTER_LZMA1, "dict_size": max(4096, HUNK), "lc": 3, "lp": 0, "pb": 2}]
    co = lzma.LZMACompressor(format=lzma.FORMAT_RAW, filters=filt)
    return co.compress(raw) + co.flush()


def _encode_v5_map(entries: list[list[int]]):
    """The compressed map, and its field widths.  Offsets are
    placeholders here; the caller fills them in and encodes again, as
    the stream's length never depends on them."""
    types = [e[0] for e in entries]
    stream = _rle_types(types)
    lengthbits = max((e[1] for e in entries if e[0] <= 3), default=0).bit_length()
    selfbits = max((e[2] for e in entries if e[0] == 5), default=0).bit_length()
    w = _BitWriter()
    for _ in range(16):                       # the tree: 16 symbols, 4 bits each
        w.write(4, 4)
    for sym in stream:
        w.write(sym, 4)
    for t, length, offset, crc in entries:
        if t <= 3:                            # a codec's block
            w.write(length, lengthbits)
            w.write(crc, 16)
        elif t == 4:                          # COMPRESSION_NONE
            w.write(crc, 16)
        elif t == 5:                          # COMPRESSION_SELF
            w.write(offset, selfbits)
    return w.flush(), lengthbits, selfbits


def _v5_file(hunks: list[bytes], codec: str) -> bytes:
    """A v5 CHD with a compressed map, one block per hunk.  "stored"
    packs the map but keeps every hunk raw, as COMPRESSION_NONE."""
    entries = []
    for raw in hunks:
        if codec == "stored":
            entries.append([chd.COMPRESSION_NONE, HUNK, 0, _crc16(raw)])
        else:
            block = _compress(codec, raw)
            entries.append([0, len(block), 0, _crc16(raw)])
    blob, lengthbits, selfbits = _encode_v5_map(entries)
    firstoffs = 124 + 16 + len(blob)
    at = firstoffs
    for e in entries:
        e[2] = at
        at += e[1] if e[0] <= 4 else 0
    blob, lengthbits, selfbits = _encode_v5_map(entries)  # same size
    parentbits = 0
    rawmap = b"".join(struct.pack(">B", t) + length.to_bytes(3, "big")
                      + offset.to_bytes(6, "big") + struct.pack(">H", crc)
                      for t, length, offset, crc in entries)
    hdr = bytearray(124)
    hdr[0:8] = chd.MAGIC
    struct.pack_into(">II", hdr, 8, 124, 5)
    struct.pack_into(">I", hdr, 16, 0 if codec == "none" else {"zlib": CODEC_ZLIB, "lzma": CODEC_LZMA, "stored": CODEC_ZLIB}[codec])
    struct.pack_into(">Q", hdr, 32, HUNK * len(hunks))
    struct.pack_into(">Q", hdr, 40, 124)      # the map follows the header
    struct.pack_into(">II", hdr, 56, HUNK, UNIT)
    maphdr = struct.pack(">I", len(blob)) + firstoffs.to_bytes(6, "big") \
        + struct.pack(">H", _crc16(rawmap)) + bytes([lengthbits, selfbits, parentbits, 0])
    return bytes(hdr) + maphdr + blob + b"".join(
        _compress(codec, raw) if codec not in ("none", "stored") else raw for raw in hunks)


def _v5_uncompressed_file(hunks: list[bytes]) -> bytes:
    """A v5 CHD with no compression: its map is one u32 per hunk."""
    hdr = bytearray(124)
    hdr[0:8] = chd.MAGIC
    struct.pack_into(">II", hdr, 8, 124, 5)
    struct.pack_into(">Q", hdr, 32, HUNK * len(hunks))
    struct.pack_into(">Q", hdr, 40, 124)
    struct.pack_into(">II", hdr, 56, HUNK, UNIT)
    map_at = 124 + 4 * len(hunks)
    blocks_at = -(-map_at // HUNK) * HUNK     # blocks start hunk-aligned
    blocks, entries = b"", []
    at = blocks_at
    for raw in hunks:
        if raw == b"\0" * HUNK:
            entries.append(0)                 # the hunk is zeros
        else:
            entries.append(at // HUNK)
            blocks += raw
            at += HUNK
    map_ = b"".join(struct.pack(">I", e) for e in entries)
    return bytes(hdr) + map_ + b"\0" * (blocks_at - map_at) + blocks


def _v34_file(hunks: list[bytes], version: int) -> bytes:
    """A v3 or v4 CHD, zlib-packed, with MINI and SELF entries thrown in."""
    size = 120 if version == 3 else 108
    hdr = bytearray(size)
    hdr[0:8] = chd.MAGIC
    struct.pack_into(">II", hdr, 8, size, version)
    struct.pack_into(">II", hdr, 16, 0, 1)    # no flags, zlib
    struct.pack_into(">I", hdr, 24, len(hunks))
    struct.pack_into(">Q", hdr, 28, HUNK * len(hunks))
    struct.pack_into(">I", hdr, 76 if version == 3 else 44, HUNK)
    entries, blocks = b"", b""
    at = size + 16 * len(hunks)
    for i, raw in enumerate(hunks):
        if i == len(hunks) - 2:               # a MINI hunk: the offset is the data
            value = int.from_bytes(raw[:8], "big")
            entries += struct.pack(">Q", value) + struct.pack(">I", zlib.crc32(raw)) \
                + struct.pack(">H", 0) + b"\0" + bytes([chd.TYPE_MINI])
            continue
        if i == len(hunks) - 1:               # a SELF hunk: a copy of hunk 0
            entries += struct.pack(">Q", 0) + struct.pack(">I", zlib.crc32(raw)) \
                + struct.pack(">H", 0) + b"\0" + bytes([chd.TYPE_SELF_HUNK])
            continue
        block = zlib.compress(raw, 9)
        entries += struct.pack(">Q", at) + struct.pack(">I", zlib.crc32(raw)) \
            + struct.pack(">H", len(block)) + bytes([0]) + bytes([chd.TYPE_COMPRESSED])
        blocks += block
        at += len(block)
    return bytes(hdr) + entries + blocks


# --- fixtures ----------------------------------------------------------

@pytest.fixture
def chd_volume(tmp_path: pytest.TempPathFactory) -> tuple:
    """An HFS volume in a v5 zlib CHD, and its raw bytes."""
    v = machfs.Volume()
    v.name = "CHD Disk"
    f = machfs.File()
    f.data = bytearray(b"chd payload " * 100)
    f.type, f.creator = b"TEXT", b"ttxt"
    v["readme.txt"] = f
    raw = v.write(800 * 1024)
    path = tmp_path / "disk.chd"
    path.write_bytes(_v5_file([raw[i:i + HUNK] for i in range(0, len(raw), HUNK)], "zlib"))
    return path, raw


# --- detection ---------------------------------------------------------

def test_crc16_is_ccitt_false():
    assert _crc16(b"123456789") == 0x29B1


def test_a_chd_is_detected_as_a_disk_image(chd_volume):
    path, _ = chd_volume
    data = path.read_bytes()
    assert chd.is_chd(data) and is_disk_image(data)
    assert image_kind(_macfile(data)) == "CHD disk image"
    assert not chd.is_chd(b"not a chd at all")


def _macfile(data: bytes):
    from macrt.macfile import MacFile
    return MacFile("disk.chd", data, None, None, b"????", b"????", 0, "k", None)


# --- browsing ----------------------------------------------------------

@pytest.mark.parametrize("codec", ["zlib", "lzma"])
def test_a_v5_chd_opens_as_a_volume(codec, tmp_path):
    v = machfs.Volume()
    v.name = "CHD Disk"
    f = machfs.File()
    f.data = bytearray(b"chd payload " * 100)
    f.type, f.creator = b"TEXT", b"ttxt"
    v["readme.txt"] = f
    raw = v.write(800 * 1024)
    path = tmp_path / f"disk.{codec}.chd"
    path.write_bytes(_v5_file([raw[i:i + HUNK] for i in range(0, len(raw), HUNK)], codec))
    vol = load_volume(path)
    assert vol.name == "CHD Disk"
    assert vol["readme.txt"].data == b"chd payload " * 100


def test_a_v5_chd_with_stored_hunks_opens(tmp_path):
    v = machfs.Volume()
    v.name = "Stored CHD"
    f = machfs.File()
    f.data = bytearray(b"stored hunk data " * 100)
    v["s.bin"] = f
    raw = v.write(800 * 1024)
    path = tmp_path / "stored.chd"
    path.write_bytes(_v5_file([raw[i:i + HUNK] for i in range(0, len(raw), HUNK)], "stored"))
    vol = load_volume(path)
    assert vol.name == "Stored CHD"
    assert vol["s.bin"].data == b"stored hunk data " * 100


def test_a_v5_chd_with_an_uncompressed_map_opens(tmp_path):
    v = machfs.Volume()
    v.name = "Raw Map"
    f = machfs.File()
    f.data = bytearray(b"raw map payload " * 100)
    v["b.bin"] = f
    raw = v.write(800 * 1024)
    path = tmp_path / "rawmap.chd"
    path.write_bytes(_v5_uncompressed_file([raw[i:i + HUNK] for i in range(0, len(raw), HUNK)]))
    vol = load_volume(path)
    assert vol.name == "Raw Map"
    assert vol["b.bin"].data == b"raw map payload " * 100


def test_v3_and_v4_chds_open(tmp_path):
    v = machfs.Volume()
    v.name = "Old CHD"
    f = machfs.File()
    f.data = bytearray(b"old codec payload " * 100)
    v["c.bin"] = f
    raw = v.write(800 * 1024)
    hunks = [raw[i:i + HUNK] for i in range(0, len(raw), HUNK)]
    for version in (3, 4):
        path = tmp_path / f"old{version}.chd"
        path.write_bytes(_v34_file(hunks, version))
        vol = load_volume(path)
        assert vol.name == "Old CHD"
        assert vol["c.bin"].data == b"old codec payload " * 100


def test_a_chd_in_the_filer_browses_its_files(chd_volume):
    path, _ = chd_volume
    vol = open_container(path)
    files = {f.name: f for f in volume_files(vol)}
    assert vol.name == "CHD Disk"
    assert files["readme.txt"].data == b"chd payload " * 100
    assert files["readme.txt"].type == b"TEXT"


# --- corruption and refusal --------------------------------------------

def test_a_truncated_chd_is_refused(chd_volume):
    path, _ = chd_volume
    data = path.read_bytes()
    with pytest.raises(CHDError, match="damaged"):
        chd.CHD(data[:200])


def test_a_chd_with_a_bad_map_crc_is_refused(chd_volume):
    path, _ = chd_volume
    data = bytearray(path.read_bytes())
    data[124 + 10] ^= 0xFF                    # the map's CRC-16
    with pytest.raises(CHDError, match="CRC-16"):
        chd.CHD(bytes(data))


def test_a_chd_with_a_bad_hunk_crc_is_refused(tmp_path):
    v = machfs.Volume()
    v.name = "CRC Disk"
    f = machfs.File()
    f.data = bytearray(b"crc payload " * 100)
    v["c.bin"] = f
    raw = v.write(800 * 1024)
    hunks = [raw[i:i + HUNK] for i in range(0, len(raw), HUNK)]
    path = tmp_path / "badcrc.chd"
    path.write_bytes(_v34_file(hunks, 3))
    data = bytearray(path.read_bytes())
    struct.pack_into(">I", data, 120 + 8, zlib.crc32(b"not the hunk"))   # hunk 0's crc32
    with pytest.raises(CHDError, match="CRC-32"):
        chd.CHD(bytes(data)).read()


def test_a_chd_with_a_bad_zlib_block_is_refused(chd_volume):
    path, _ = chd_volume
    data = bytearray(path.read_bytes())
    at = int.from_bytes(data[124 + 4:124 + 10], "big")
    data[at + 2] ^= 0xFF                      # inside the zlib stream
    with pytest.raises(CHDError, match="zlib"):
        chd.CHD(bytes(data)).read()


def test_an_unsupported_codec_is_refused_by_name(chd_volume):
    path, _ = chd_volume
    data = bytearray(path.read_bytes())
    struct.pack_into(">I", data, 16, 0x7A737464)      # 'zstd'
    with pytest.raises(CHDError, match="zstd"):
        chd.CHD(bytes(data))


def test_a_chd_needing_its_parent_is_refused(tmp_path):
    hunks = [b"\0" * HUNK, b"parented payload" + b"\0" * (HUNK - 16)]
    entries = [[6, 0, 0, 0], [0, 10, 0, _crc16(hunks[1])]]
    blob, lengthbits, selfbits = _encode_v5_map(entries)
    firstoffs = 124 + 16 + len(blob)
    entries[1][2] = firstoffs
    blob, lengthbits, selfbits = _encode_v5_map(entries)
    hdr = bytearray(124)
    hdr[0:8] = chd.MAGIC
    struct.pack_into(">II", hdr, 8, 124, 5)
    struct.pack_into(">I", hdr, 16, CODEC_ZLIB)
    struct.pack_into(">Q", hdr, 32, HUNK * 2)
    struct.pack_into(">Q", hdr, 40, 124)
    struct.pack_into(">II", hdr, 56, HUNK, UNIT)
    rawmap = b"".join(struct.pack(">B", t) + length.to_bytes(3, "big")
                      + offset.to_bytes(6, "big") + struct.pack(">H", crc)
                      for t, length, offset, crc in entries)
    maphdr = struct.pack(">I", len(blob)) + firstoffs.to_bytes(6, "big") \
        + struct.pack(">H", _crc16(rawmap)) + bytes([lengthbits, selfbits, 0, 0])
    path = tmp_path / "parented.chd"
    path.write_bytes(bytes(hdr) + maphdr + blob + zlib.compress(hunks[1], 9))
    with pytest.raises(CHDError, match="parent"):
        chd.CHD(path.read_bytes()).read()


def test_a_bad_version_and_signature_are_refused(tmp_path):
    with pytest.raises(CHDError, match="version"):
        chd.CHD(b"MComprHD" + struct.pack(">II", 124, 6))
    with pytest.raises(CHDError, match="signature"):
        chd.CHD(b"NotChdAtAll" + b"\0" * 200)


def test_a_bad_huffman_tree_is_refused(tmp_path):
    mapblob = b"\xff" * 32                    # lengths of 15 everywhere: not a tree
    hdr = bytearray(124)
    hdr[0:8] = chd.MAGIC
    struct.pack_into(">II", hdr, 8, 124, 5)
    struct.pack_into(">I", hdr, 16, CODEC_ZLIB)
    struct.pack_into(">Q", hdr, 32, HUNK)
    struct.pack_into(">Q", hdr, 40, 124)
    struct.pack_into(">II", hdr, 56, HUNK, UNIT)
    path = tmp_path / "badtree.chd"
    path.write_bytes(bytes(hdr) + b"\0" * 16 + mapblob)
    with pytest.raises(CHDError):
        chd.CHD(path.read_bytes())


# --- the map codec on its own ------------------------------------------

def test_the_map_huffman_reads_a_skewed_tree():
    """A tree with a length-1 code and a run of unused symbols, exactly
    as import_tree_rle's escape writes it."""
    w = _BitWriter()
    w.write(1, 4); w.write(1, 4)              # node 0: escape, then a literal 1
    w.write(2, 4); w.write(2, 4)              # nodes 1 and 2: length 2
    w.write(1, 4); w.write(0, 4); w.write(10, 4)   # escape: thirteen length-0 nodes
    w.write(1, 1)                             # symbol 0: its code is '1'
    w.write(0, 2)                             # symbol 1: '00'
    w.write(1, 2)                             # symbol 2: '01'
    bits = _Bits(w.flush())
    huff = _Huffman(bits, 16, 4)
    assert huff.decode(bits) == 0
    assert huff.decode(bits) == 1
    assert huff.decode(bits) == 2
