"""Read CHD (Compressed Hunks of Data) disk images.

MAME's CHD packs a disc's hunks — usually a hunk to a compression
block — behind a 124-byte header and a map.  The map says where each
hunk's bytes live and which codec packed them; this module walks it
and unpacks the hunks back into the raw image bytes the volume
readers want.  Only the codecs Python's standard library can undo are
read: none, zlib and LZMA.  A CHD naming anything else (zstd, Huffman,
FLAC, the CD front-ends, A/V) is refused by name, as is one that
needs its parent, whose bytes live in another file.

The whole image is unpacked into memory before the volume readers see
it: a CHD's hunks are addressed through its map, and the readers want
a plain image.  The files a volume holds are still read on demand, as
every other disk-image path reads them.

Written from MAME's public format description (chd.h, chdcodec.h and
chd.cpp in mamedev/mame); no code is carried over.
"""

import struct
import zlib

MAGIC = b"MComprHD"

# header sizes, per version (chd.h)
V3_HEADER_SIZE, V4_HEADER_SIZE, V5_HEADER_SIZE = 120, 108, 124

# codec tags (chdcodec.h): 'zlib', 'zstd', 'lzma', 'huff', 'flac',
# the CD front-ends over the first three, and A/V Huffman
CODEC_ZLIB = 0x7A6C6962
CODEC_LZMA = 0x6C7A6D61
CODEC_NAMES = {
    0x7A6C6962: "zlib", 0x7A737464: "zstd", 0x6C7A6D61: "lzma",
    0x68756666: "huff", 0x666C6163: "flac",
    0x63647A6C: "cd-zlib", 0x63647A73: "cd-zstd", 0x63646C7A: "cd-lzma",
    0x6364666C: "cd-flac", 0x61766875: "avhuff",
}

# v5 map entry types (chd.cpp)
COMPRESSION_NONE, COMPRESSION_SELF, COMPRESSION_PARENT = 4, 5, 6
RLE_SMALL, RLE_LARGE = 7, 8                      # map-codec run lengths
SELF_0, SELF_1 = 9, 10                           # last SELF block, +1
PARENT_SELF, PARENT_0, PARENT_1 = 11, 12, 13

# v3/v4 map entry types (chd.cpp)
TYPE_COMPRESSED, TYPE_UNCOMPRESSED, TYPE_MINI = 1, 2, 3
TYPE_SELF_HUNK, TYPE_PARENT_HUNK = 4, 5

V34_FLAG_NO_CRC = 0x10


class CHDError(ValueError):
    """A CHD this reader won't read: damaged, or a codec or parent it
    doesn't have."""


def is_chd(data: bytes) -> bool:
    """Whether data opens with a CHD header."""
    return data[:8] == MAGIC


def read_image(data: bytes) -> bytes:
    """The raw image bytes a CHD holds, every hunk unpacked."""
    return CHD(data).read()


class CHD:
    """A CHD opened for reading: its header parsed, its map expanded,
    its hunks unpacked on demand."""

    def __init__(self, data: bytes):
        self._data = data
        if len(data) < 8 or data[:8] != MAGIC:
            raise CHDError("not a CHD image (no MComprHD signature)")
        length, version = struct.unpack_from(">II", data, 8)
        if version == 5:
            if length != V5_HEADER_SIZE:
                raise CHDError(f"the CHD header is damaged (its length is {length}, not {V5_HEADER_SIZE})")
            self._v5 = True
            self._open_v5(data)
        elif version in (3, 4):
            size = V3_HEADER_SIZE if version == 3 else V4_HEADER_SIZE
            if length != size:
                raise CHDError(f"the CHD header is damaged (its length is {length}, not {size})")
            self._v5 = False
            self._open_v34(data, version)
        else:
            raise CHDError(f"unsupported CHD version {version} (this reads 3, 4 and 5)")
        if not self._hunkbytes:
            raise CHDError("the CHD header is damaged (its hunk size is zero)")
        if self._mapoffset > len(data):
            raise CHDError("the CHD header is damaged (its map starts past the file)")

    # -- the header ----------------------------------------------------

    def _open_v5(self, data: bytes):
        comps = struct.unpack_from(">IIII", data, 16)
        self._logical = struct.unpack_from(">Q", data, 32)[0]
        self._mapoffset = struct.unpack_from(">Q", data, 40)[0]
        self._hunkbytes = struct.unpack_from(">I", data, 56)[0]
        self._unitbytes = struct.unpack_from(">I", data, 60)[0]
        if not self._unitbytes:
            raise CHDError("the CHD header is damaged (its unit size is zero)")
        self._hunkcount = -(-self._logical // self._hunkbytes)
        for tag in comps:
            if tag not in (0, CODEC_ZLIB, CODEC_LZMA):
                raise CHDError(f"the CHD image is unsupported (its {CODEC_NAMES.get(tag, hex(tag))} codec is not read)")
        self._codecs = comps
        self._compressed = comps[0] != 0
        self._entries = self._read_map_v5(data)

    def _open_v34(self, data: bytes, version: int):
        flags, compression = struct.unpack_from(">II", data, 16)
        self._hunkcount = struct.unpack_from(">I", data, 24)[0]
        self._logical = struct.unpack_from(">Q", data, 28)[0]
        self._mapoffset = 120 if version == 3 else 108
        self._hunkbytes = struct.unpack_from(">I", data, 76 if version == 3 else 44)[0]
        if compression in (0, 1, 2):
            self._codecs = (0 if compression == 0 else CODEC_ZLIB,) + (0,) * 3
            self._compressed = compression != 0
        else:
            raise CHDError(f"the CHD image is unsupported (its {CODEC_NAMES.get(compression, hex(compression))} codec is not read)")
        self._v34_flags = flags
        self._entries = self._read_map_v34(data)

    # -- the map -------------------------------------------------------

    def _read_map_v5(self, data: bytes) -> list[tuple[int, int, int, int]]:
        """Each hunk's (type, length, offset, crc): uncompressed maps
        are one u32 per hunk, compressed ones a Huffman-coded stream
        whose 16-byte header says how wide each field is."""
        if not self._compressed:
            end = self._mapoffset + 4 * self._hunkcount
            if end > len(data):
                raise CHDError("the CHD map is damaged (it runs past the file)")
            # an entry of zero means the hunk is zeros (or the parent's)
            return [(COMPRESSION_NONE, self._hunkbytes,
                     raw * self._hunkbytes, 0)
                    for raw in struct.unpack_from(f">{self._hunkcount}I", data, self._mapoffset)]
        hdr = data[self._mapoffset:self._mapoffset + 16]
        if len(hdr) < 16:
            raise CHDError("the CHD map is damaged (its header ends early)")
        mapbytes = struct.unpack_from(">I", hdr, 0)[0]
        firstoffs = int.from_bytes(hdr[4:10], "big")
        mapcrc = struct.unpack_from(">H", hdr, 10)[0]
        lengthbits, selfbits, parentbits = hdr[12], hdr[13], hdr[14]
        if not 0 <= lengthbits <= 32 or not 0 <= selfbits <= 32 or not 0 <= parentbits <= 32:
            raise CHDError("the CHD map is damaged (its field widths make no sense)")
        blob = data[self._mapoffset + 16:self._mapoffset + 16 + mapbytes]
        if len(blob) < mapbytes:
            raise CHDError("the CHD map is damaged (it ends early)")
        bits = _Bits(blob)
        huff = _Huffman(bits, 16, 4)
        types: list[int] = []
        lastcomp, repcount = 0, 0
        for _ in range(self._hunkcount):
            if repcount:
                types.append(lastcomp)
                repcount -= 1
                continue
            val = huff.decode(bits)
            if val == RLE_SMALL:
                repcount = 2 + huff.decode(bits)
                types.append(lastcomp)
            elif val == RLE_LARGE:
                repcount = 2 + 16 + (huff.decode(bits) << 4) + huff.decode(bits)
                types.append(lastcomp)
            else:
                lastcomp = val
                types.append(val)
        entries = []
        curoffset = firstoffs
        last_self, last_parent = 0, 0
        for hunknum, t in enumerate(types):
            offset, length, crc = curoffset, 0, 0
            if 0 <= t <= 3:                        # a codec's block
                length = bits.read(lengthbits)
                crc = bits.read(16)
                curoffset += length
            elif t == COMPRESSION_NONE:
                length = self._hunkbytes
                crc = bits.read(16)
                curoffset += length
            elif t == COMPRESSION_SELF:
                offset = bits.read(selfbits)
                last_self = offset
            elif t == COMPRESSION_PARENT:
                offset = bits.read(parentbits)
                last_parent = offset
            elif t == SELF_1:
                last_self += 1
                offset = last_self
            elif t == SELF_0:
                offset = last_self
            elif t == PARENT_SELF:
                offset = last_parent = hunknum * self._hunkbytes // self._unitbytes
            elif t == PARENT_1:
                last_parent += self._hunkbytes // self._unitbytes
                offset = last_parent
            elif t == PARENT_0:
                offset = last_parent
            else:
                raise CHDError(f"the CHD map is damaged (hunk {hunknum} is type {t})")
            entries.append((t, length, offset, crc))
        rawmap = b"".join(struct.pack(">B", t) + length.to_bytes(3, "big")
                          + offset.to_bytes(6, "big") + struct.pack(">H", crc)
                          for t, length, offset, crc in entries)
        if _crc16(rawmap) != mapcrc:              # the map's own check
            raise CHDError("the CHD map is damaged (its CRC-16 does not match)")
        return entries

    def _read_map_v34(self, data: bytes) -> list[tuple[int, int, int, int]]:
        """Each hunk's (type, length, offset, crc32), from a map of
        16-byte entries."""
        end = self._mapoffset + 16 * self._hunkcount
        if end > len(data):
            raise CHDError("the CHD map is damaged (it runs past the file)")
        entries = []
        for hunknum in range(self._hunkcount):
            e = data[self._mapoffset + 16 * hunknum:self._mapoffset + 16 * hunknum + 16]
            offset = int.from_bytes(e[0:8], "big")
            crc32 = struct.unpack_from(">I", e, 8)[0]
            length = struct.unpack_from(">H", e, 12)[0] | (e[14] << 16)
            t = e[15] & 0x0F
            if t not in (TYPE_COMPRESSED, TYPE_UNCOMPRESSED, TYPE_MINI,
                         TYPE_SELF_HUNK, TYPE_PARENT_HUNK):
                raise CHDError(f"the CHD map is damaged (hunk {hunknum} is type {t})")
            entries.append((t, length, offset, crc32))
        return entries

    # -- the hunks -----------------------------------------------------

    def read(self) -> bytes:
        """The whole image, its hunks unpacked in order."""
        out = bytearray()
        for n in range(self._hunkcount):
            out += self.read_hunk(n)
        return bytes(out[:self._logical])

    def read_hunk(self, hunknum: int, _depth: int = 0) -> bytes:
        """One hunk's bytes, unpacked from wherever its map entry says."""
        if hunknum >= self._hunkcount:
            raise CHDError(f"the CHD map is damaged (hunk {hunknum} is past its end)")
        if _depth > 32:
            raise CHDError("the CHD map is damaged (its hunks reference themselves in a loop)")
        t, length, offset, crc = self._entries[hunknum]
        data = self._data
        if self._v5:
            if not self._compressed:               # the map is raw u32s
                if offset == 0:                    # no parent: the hunk is zeros
                    return b"\0" * self._hunkbytes
                raw = data[offset:offset + self._hunkbytes]
                if len(raw) < self._hunkbytes:
                    raise CHDError(f"the CHD image is damaged (hunk {hunknum} ends early)")
                return raw
            if t in (0, 1, 2, 3):                  # a codec's block
                block = data[offset:offset + length]
                if len(block) < length:
                    raise CHDError(f"the CHD image is damaged (hunk {hunknum} ends early)")
                raw = self._decode(self._codecs[t], block)
                if _crc16(raw) != crc:
                    raise CHDError(f"the CHD image is damaged (hunk {hunknum} fails its CRC-16)")
                return raw
            if t == COMPRESSION_NONE:
                raw = data[offset:offset + self._hunkbytes]
                if len(raw) < self._hunkbytes:
                    raise CHDError(f"the CHD image is damaged (hunk {hunknum} ends early)")
                if _crc16(raw) != crc:
                    raise CHDError(f"the CHD image is damaged (hunk {hunknum} fails its CRC-16)")
                return raw
            if t == COMPRESSION_SELF:
                return self.read_hunk(offset, _depth + 1)
            raise CHDError("the CHD image is unsupported (it needs its parent CHD)")
        # v3/v4
        if t == TYPE_COMPRESSED:
            block = data[offset:offset + length]
            if len(block) < length:
                raise CHDError(f"the CHD image is damaged (hunk {hunknum} ends early)")
            raw = self._decode(self._codecs[0], block)
        elif t == TYPE_UNCOMPRESSED:
            raw = data[offset:offset + self._hunkbytes]
            if len(raw) < self._hunkbytes:
                raise CHDError(f"the CHD image is damaged (hunk {hunknum} ends early)")
        elif t == TYPE_MINI:                       # the offset, spread over the hunk
            raw = bytearray(self._hunkbytes)
            raw[0:8] = offset.to_bytes(8, "big")
            for i in range(8, self._hunkbytes):
                raw[i] = raw[i - 8]
            raw = bytes(raw)
        elif t == TYPE_SELF_HUNK:
            return self.read_hunk(offset, _depth + 1)
        else:
            raise CHDError("the CHD image is unsupported (it needs its parent CHD)")
        if not (self._v34_flags & V34_FLAG_NO_CRC) and zlib.crc32(raw) != crc:
            raise CHDError(f"the CHD image is damaged (hunk {hunknum} fails its CRC-32)")
        return raw

    def _decode(self, tag: int, block: bytes) -> bytes:
        """One block through its codec: zlib, or LZMA the way CHD packs
        it (raw, its properties fixed by the hunk size)."""
        if tag == CODEC_ZLIB:
            try:
                return zlib.decompress(block)
            except zlib.error:
                raise CHDError("the CHD image is damaged (a hunk's zlib stream does not decode)") from None
        if tag == CODEC_LZMA:
            import lzma
            filt = [{"id": lzma.FILTER_LZMA1, "dict_size": max(4096, self._hunkbytes),
                     "lc": 3, "lp": 0, "pb": 2}]
            try:
                raw = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filt).decompress(
                    block, self._hunkbytes)
            except lzma.LZMAError:
                raise CHDError("the CHD image is damaged (a hunk's LZMA stream does not decode)") from None
            if len(raw) != self._hunkbytes:
                raise CHDError("the CHD image is damaged (a hunk's LZMA stream stops short)")
            return raw
        raise CHDError(f"the CHD image is unsupported (its {CODEC_NAMES.get(tag, hex(tag))} codec is not read)")


def _crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE, the map's and the hunks' check (chd.cpp)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


class _Bits:
    """A bit reader, most-significant bit first, as CHD's map stream is
    packed."""

    __slots__ = ("_d", "_pos")

    def __init__(self, data: bytes):
        self._d, self._pos = data, 0

    def read(self, n: int) -> int:
        pos = self._pos
        if pos + n > len(self._d) * 8:
            raise CHDError("the CHD map is damaged (its bit stream ends early)")
        v = 0
        d = self._d
        for i in range(n):
            v = (v << 1) | ((d[(pos + i) >> 3] >> (7 - ((pos + i) & 7))) & 1)
        self._pos = pos + n
        return v


class _Huffman:
    """The map's codec: a 16-symbol Huffman code whose tree arrives
    run-length coded, the way chd.cpp's import_tree_rle writes it."""

    def __init__(self, bits: _Bits, numcodes: int, numbits: int):
        lengths = [0] * numcodes
        curnode = 0
        while curnode < numcodes:
            v = bits.read(numbits)
            if v != 1:
                lengths[curnode] = v
                curnode += 1
            else:
                v2 = bits.read(numbits)
                if v2 == 1:                    # a double 1 is a single 1
                    lengths[curnode] = 1
                    curnode += 1
                else:
                    rep = bits.read(numbits) + 3
                    while rep and curnode < numcodes:
                        lengths[curnode] = v2
                        curnode += 1
                        rep -= 1
        if curnode != numcodes:
            raise CHDError("the CHD map is damaged (its Huffman tree ends early)")
        if any(n > 8 for n in lengths):
            raise CHDError("the CHD map is damaged (its Huffman tree is too deep)")
        histo = [0] * 33
        for n in lengths:
            histo[n] += 1
        starts, curstart = {}, 0
        for codelen in range(32, 0, -1):
            starts[codelen] = curstart
            curstart = (curstart + histo[codelen]) >> 1
        self._codes = {}
        counters = dict(starts)
        for sym, n in enumerate(lengths):
            if n:
                self._codes[(counters[n], n)] = sym
                counters[n] += 1

    def decode(self, bits: _Bits) -> int:
        code = 0
        for length in range(1, 9):
            code = (code << 1) | bits.read(1)
            sym = self._codes.get((code, length))
            if sym is not None:
                return sym
        raise CHDError("the CHD map is damaged (its Huffman stream stops early)")
