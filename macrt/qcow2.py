"""Read qcow2 (QEMU Copy on Write version 2) disk images.

QEMU's qcow2 packs a virtual disk behind a header, two levels of
pointer tables (L1 and L2) and clusters stored raw, compressed with
deflate, or marked as reading as zeros.  This module walks the tables
and rebuilds the plain image bytes the volume readers want.  Only the
read-only core is supported: encryption, backing files (their bytes
live in another file), external data files, extended L2 entries,
non-deflate compression, and the dirty and corrupt incompatible
features are all refused by name, as are versions other than 2 and 3.

The whole image is unpacked into memory before the volume readers see
it, as for CHD; the files a volume holds are still read on demand
downstream.  Refcounts are not read: they govern allocation, not
reading, so a corrupt refcount block cannot mislead this reader.

Written from QEMU's public format description (docs/interop/qcow2.txt);
no code is carried over.
"""

import struct
import zlib

MAGIC = b"QFI\xfb"

# L1/L2 entry fields: the host offset lives in bits 9-55
OFFSET_MASK = 0x00FFFFFFFFFFFE00
COW_FLAG = 1 << 63                        # L1/L2: refcount is one; informational
COMPRESSED_FLAG = 1 << 62                 # L2: compressed cluster
ZERO_FLAG = 1                             # L2, v3: cluster reads as zeros

# incompatible feature bits (v3)
FEATURE_DIRTY, FEATURE_CORRUPT = 0, 1
FEATURE_EXTERNAL_DATA, FEATURE_COMPRESSION_TYPE, FEATURE_EXTENDED_L2 = 2, 3, 4
FEATURE_NAMES = {
    FEATURE_DIRTY: "dirty", FEATURE_CORRUPT: "corrupt",
    FEATURE_EXTERNAL_DATA: "external data file",
    FEATURE_COMPRESSION_TYPE: "non-deflate compression",
    FEATURE_EXTENDED_L2: "extended L2 entries",
}

MAX_CLUSTER_BITS = 21                     # QEMU's 2 MiB cluster limit
MAX_SIZE = 1 << 40                        # a virtual disk this reader will walk


class Qcow2Error(ValueError):
    """A qcow2 this reader won't read: damaged, or a feature it lacks."""


def is_qcow2(data: bytes) -> bool:
    """Whether data opens with a qcow2 header."""
    return data[:4] == MAGIC


def read_image(data: bytes) -> bytes:
    """The raw image bytes a qcow2 holds, its clusters unpacked."""
    return QCOW2(data).read()


class QCOW2:
    """A qcow2 opened for reading: its header parsed, its L1 table loaded,
    its clusters unpacked on demand."""

    def __init__(self, data: bytes):
        self._data = data
        if len(data) < 72 or data[:4] != MAGIC:
            raise Qcow2Error("not a qcow2 image (no QFI\\xfb signature)")
        version = struct.unpack_from(">I", data, 4)[0]
        if version == 2:
            self._v2 = True
            self._header_length = 72
        elif version == 3:
            if len(data) < 104:
                raise Qcow2Error("the qcow2 header is damaged (its version 3 header ends early)")
            self._v2 = False
            self._header_length = struct.unpack_from(">I", data, 100)[0]
            if self._header_length < 104 or self._header_length % 8:
                raise Qcow2Error(f"the qcow2 header is damaged (its header_length is {self._header_length})")
        else:
            raise Qcow2Error(f"unsupported qcow2 version {version} (this reads 2 and 3)")
        self._version = version
        self._backing_offset, self._backing_size = struct.unpack_from(">QI", data, 8)
        self._cluster_bits = struct.unpack_from(">I", data, 20)[0]
        self._size = struct.unpack_from(">Q", data, 24)[0]
        self._crypt = struct.unpack_from(">I", data, 32)[0]
        self._l1_size = struct.unpack_from(">I", data, 36)[0]
        self._l1_offset = struct.unpack_from(">Q", data, 40)[0]
        self._parse_header_v3(data)
        self._parse_checked(len(data))
        l1_bytes = self._l1_size * 8
        l1 = data[self._l1_offset:self._l1_offset + l1_bytes]
        if len(l1) < l1_bytes:
            raise Qcow2Error("the qcow2 header is damaged (its L1 table ends early)")
        self._l1 = struct.unpack(f">{self._l1_size}Q", l1)

    def _parse_header_v3(self, data: bytes):
        """v3-only header fields: the incompatible feature bits must all
        name features this reader has (none do)."""
        if self._v2:
            return
        incompatible = struct.unpack_from(">Q", data, 72)[0]
        for bit, name in FEATURE_NAMES.items():
            if incompatible & (1 << bit):
                raise Qcow2Error(f"the qcow2 image is unsupported (its {name} feature is not read)")
        unknown = incompatible & ~sum(1 << bit for bit in FEATURE_NAMES)
        if unknown:
            raise Qcow2Error(f"the qcow2 image is unsupported (its incompatible features {unknown:#x} are not read)")
        compression_type = data[104] if self._header_length > 104 else 0
        if compression_type:
            raise Qcow2Error("the qcow2 image is unsupported (its non-deflate compression is not read)")

    def _parse_checked(self, length: int):
        if not 9 <= self._cluster_bits <= MAX_CLUSTER_BITS:
            raise Qcow2Error(f"the qcow2 header is damaged (its cluster size is 2^{self._cluster_bits} bytes)")
        self._cluster_size = 1 << self._cluster_bits
        self._l2_entries = self._cluster_size // 8
        if self._crypt:
            raise Qcow2Error("the qcow2 image is unsupported (it is encrypted)")
        if self._backing_offset or self._backing_size:
            raise Qcow2Error("the qcow2 image is unsupported (it needs its backing file)")
        if not 0 < self._size <= MAX_SIZE:
            raise Qcow2Error("the qcow2 header is damaged (its virtual size makes no sense)")
        if not self._l1_size:
            raise Qcow2Error("the qcow2 header is damaged (its L1 table is empty)")
        reach = self._l1_size * self._l2_entries * self._cluster_size
        if reach < self._size:
            raise Qcow2Error("the qcow2 header is damaged (its L1 table cannot reach its size)")
        if self._l1_offset % self._cluster_size:
            raise Qcow2Error("the qcow2 header is damaged (its L1 table is not cluster-aligned)")


    # -- reading -------------------------------------------------------

    def read(self) -> bytes:
        """The whole image, its clusters unpacked in order."""
        out = bytearray()
        for i in range(-(-self._size // self._cluster_size)):
            out += self._cluster(i)
        return bytes(out[:self._size])

    def _cluster(self, index: int) -> bytes:
        """One guest cluster: zeros if unallocated, raw bytes, or a raw
        deflate stream's product."""
        zeros = b"\0" * self._cluster_size
        l1 = self._l1[index // self._l2_entries]
        if l1 == 0:
            return zeros
        self._check_entry(l1, "L1", index)
        l2_offset = l1 & OFFSET_MASK
        if l2_offset == 0:                  # an unused L1 slot
            return zeros
        data = self._data
        l2 = data[l2_offset:l2_offset + self._cluster_size]
        if len(l2) < self._cluster_size:
            raise Qcow2Error("the qcow2 image is damaged (its L2 table ends early)")
        entry = struct.unpack_from(">Q", l2, (index % self._l2_entries) * 8)[0]
        if entry == 0:
            return zeros
        if entry & COMPRESSED_FLAG:
            return self._compressed(entry, zeros)
        if entry & ZERO_FLAG:
            if self._v2:
                raise Qcow2Error("the qcow2 image is damaged (its zero flag is set in a version 2 image)")
            return zeros
        self._check_entry(entry, "L2", index)
        offset = entry & OFFSET_MASK
        if offset == 0:
            raise Qcow2Error("the qcow2 image is damaged (its entry points at offset 0)")
        blob = data[offset:offset + self._cluster_size]
        if len(blob) < self._cluster_size:
            raise Qcow2Error("the qcow2 image is damaged (a cluster ends early)")
        return blob

    def _compressed(self, entry: int, zeros: bytes) -> bytes:
        if entry & COW_FLAG:
            raise Qcow2Error("the qcow2 image is damaged (a compressed entry sets the COW flag)")
        if entry & ZERO_FLAG:
            raise Qcow2Error("the qcow2 image is damaged (a compressed entry sets the zero flag)")
        x = 62 - (self._cluster_bits - 8)     # offset bits 0..x-1, then sectors
        offset = entry & ((1 << x) - 1)
        if offset >> 56:                      # compressed offsets stay within 56 bits
            raise Qcow2Error("the qcow2 image is damaged (a compressed cluster offset is out of range)")
        sectors = 1 + ((entry >> x) & ((1 << (62 - x)) - 1))
        blob = self._data[offset:offset + sectors * 512]
        if len(blob) < sectors * 512:
            raise Qcow2Error("the qcow2 image is damaged (a compressed cluster ends early)")
        decomp = zlib.decompressobj(wbits=-15)
        try:
            raw = decomp.decompress(blob, self._cluster_size)
        except zlib.error:
            raise Qcow2Error("the qcow2 image is damaged (a deflate stream does not decode)") from None
        if len(raw) != self._cluster_size:
            raise Qcow2Error("the qcow2 image is damaged (a deflate stream stops short)")
        return raw

    @staticmethod
    def _check_entry(entry: int, table: str, index: int):
        reserved = (entry & 0x7F00000000000000) | (entry & 0x1FE)   # bits 56-62, bits 1-8
        if reserved:
            raise Qcow2Error(
                f"the qcow2 image is damaged ({table} entry {index} sets reserved bits {reserved:#x})")
