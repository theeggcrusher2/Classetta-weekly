"""Read VMDK (VMware Virtual Machine Disk) images.

Two layouts share this reader: the version-4 sparse extent, opened by the
"KDMV" signature, and the older version-3 "COWD" extent.  Both keep a
grain directory whose entries point at grain tables, and those at grains;
an entry of zero reads as zeros.  Version-4 grains may be zlib-compressed,
with or without the grain marker that stream-optimized images wrap them
in, and its grain directory may sit in the file's footer instead of its
header.

A text descriptor on its own -- the small .vmdk that names a separate data
file -- is recognised and refused: its bytes live in other files this
single-file reader will not chase.  So are images that name a parent,
versions past 3, and any compression but deflate.

Written from VMware's published VMDK format description and QEMU's
block/vmdk.c; no code is carried over and no dependency is added beyond
the standard library's zlib.
"""

import struct
import zlib

MAGIC4 = b"KDMV"
MAGIC3 = b"COWD"
DESCRIPTOR = b"# Disk DescriptorFile"

FLAG_ZERO_GRAIN = 1 << 2          # a grain-table entry of 1 reads as zeros
FLAG_MARKER = 1 << 17             # grains are wrapped in a grain marker
GD_AT_END = 0xFFFFFFFFFFFFFFFF    # the grain directory lives in the footer
DEFLATE = 1
FOOTER_MARKER = 3
EOS_MARKER = 0

MAX_GTES = 512                    # grain-table entries QEMU will walk
MAX_GRANULARITY = 0x200000        # sectors per grain QEMU will walk
MAX_SIZE = 1 << 40                # a virtual disk this reader will unpack
L2_SIZE_V3 = 4096                 # grain-table entries in a version-3 extent
FOOTER_BYTES = 1536               # footer marker + header + end-of-stream


class VmdkError(ValueError):
    """A VMDK this reader won't read: damaged, or a feature it lacks."""


def is_vmdk(data: bytes) -> bool:
    """Whether data looks like a VMDK: a sparse extent's signature, or a
    text descriptor's first line."""
    return data[:4] in (MAGIC4, MAGIC3) or data[:len(DESCRIPTOR)] == DESCRIPTOR


def read_image(data: bytes) -> bytes:
    """The raw virtual-disk bytes a VMDK holds, its grains unpacked."""
    if data[:4] == MAGIC4:
        return _read_v4(data)
    if data[:4] == MAGIC3:
        return _read_v3(data)
    if data[:len(DESCRIPTOR)] == DESCRIPTOR:
        raise VmdkError(
            "the VMDK is unsupported (a text descriptor's data lives in other files)")
    raise VmdkError("not a VMDK image (no KDMV or COWD signature)")


def _v4_header(data: bytes, at: int) -> dict:
    """The version-4 header at byte `at`, where its magic sits."""
    if at + 79 > len(data):
        raise VmdkError("the VMDK is damaged (its header ends early)")
    (version, flags, capacity, granularity, desc_offset, desc_size,
     gtes, rgd_offset, gd_offset, grain_offset) = struct.unpack_from(
        "<IIQQQQIQQQ", data, at + 4)
    compress = struct.unpack_from("<H", data, at + 77)[0]
    return {"version": version, "flags": flags, "capacity": capacity,
            "granularity": granularity, "desc_offset": desc_offset,
            "desc_size": desc_size, "gtes": gtes, "rgd_offset": rgd_offset,
            "gd_offset": gd_offset, "grain_offset": grain_offset,
            "compress": compress}


def _v4_footer(data: bytes) -> dict:
    """The version-4 header the footer carries, for images whose grain
    directory is kept at the end (stream-optimized)."""
    base = len(data) - FOOTER_BYTES
    if base < 0:
        raise VmdkError("the VMDK is damaged (its footer is missing)")
    marker_size, marker_type = struct.unpack_from("<II", data, base + 8)
    if marker_size != 0 or marker_type != FOOTER_MARKER:
        raise VmdkError("the VMDK is damaged (its footer marker is wrong)")
    if data[base + 512:base + 516] != MAGIC4:
        raise VmdkError("the VMDK is damaged (its footer has no KDMV signature)")
    hdr = _v4_header(data, base + 512)
    eos_val, eos_size, eos_type = struct.unpack_from("<QII", data, base + 1024)
    if eos_val != 0 or eos_size != 0 or eos_type != EOS_MARKER:
        raise VmdkError("the VMDK is damaged (its end-of-stream marker is wrong)")
    return hdr


def _parent_cid(desc: bytes) -> int | None:
    """The descriptor's parentCID, or None if it has no such line."""
    at = desc.find(b"parentCID=")
    if at < 0:
        return None
    at += len(b"parentCID=")
    end = at
    while end < len(desc) and desc[end:end + 1] in b"0123456789abcdefABCDEF":
        end += 1
    if end == at:
        return None
    try:
        return int(desc[at:end], 16)
    except ValueError:
        return None


def _check_parent(data: bytes, hdr: dict) -> None:
    """Refuse an image whose descriptor names a parent: its unallocated
    grains read from that other file, which this reader will not chase."""
    if not hdr["desc_offset"] or not hdr["desc_size"]:
        return
    at = hdr["desc_offset"] << 9
    size = min(hdr["desc_size"] << 9, 1 << 20)
    desc = data[at:at + size]
    if b"parentFileNameHint" in desc:
        raise VmdkError("the VMDK is unsupported (it needs its parent image)")
    cid = _parent_cid(desc)
    if cid is not None and cid != 0xFFFFFFFF:
        raise VmdkError("the VMDK is unsupported (it needs its parent image)")


def _grain(data: bytes, at: int, grain_bytes: int, compressed: bool,
           has_marker: bool) -> bytes:
    """One grain read from the extent file at byte `at`."""
    if not compressed:
        blob = data[at:at + grain_bytes]
        if len(blob) < grain_bytes:
            raise VmdkError("the VMDK is damaged (a grain ends early)")
        return blob
    blob = data[at:at + grain_bytes * 2]     # marker + data may exceed a grain
    if has_marker:
        if len(blob) < 12:
            raise VmdkError("the VMDK is damaged (a grain marker ends early)")
        size = struct.unpack_from("<I", blob, 8)[0]
        if not 0 < size <= len(blob) - 12:
            raise VmdkError("the VMDK is damaged (a grain marker size is wrong)")
        payload = blob[12:12 + size]
    else:
        payload = blob[:grain_bytes]
    decomp = zlib.decompressobj()
    try:
        out = decomp.decompress(payload) + decomp.flush()
    except zlib.error:
        raise VmdkError("the VMDK is damaged (a grain does not decompress)") from None
    if len(out) != grain_bytes:
        raise VmdkError(
            f"the VMDK is damaged (a grain decompresses to {len(out)} bytes, "
            f"not {grain_bytes})")
    return out


def _unpack(data: bytes, l1: tuple, gtes: int, granularity: int,
            capacity: int, compressed: bool, has_marker: bool,
            zero_grain: bool) -> bytes:
    """The whole virtual disk, grain by grain, through the grain directory
    `l1` and the grain tables its entries point at."""
    grain_bytes = granularity * 512
    total = capacity * 512
    out = bytearray()
    tables: dict[int, tuple] = {}
    grain = 0
    while len(out) < total:
        l1_index, l2_index = divmod(grain, gtes)
        if l1_index >= len(l1):
            raise VmdkError(
                "the VMDK is damaged (its grain directory cannot reach its capacity)")
        gt_sector = l1[l1_index]
        if gt_sector == 0:
            out += b"\0" * grain_bytes
        else:
            table = tables.get(gt_sector)
            if table is None:
                at = gt_sector << 9
                raw = data[at:at + gtes * 4]
                if len(raw) < gtes * 4:
                    raise VmdkError("the VMDK is damaged (a grain table ends early)")
                table = struct.unpack(f"<{gtes}I", raw)
                tables[gt_sector] = table
            cluster = table[l2_index]
            if cluster == 0 or (zero_grain and cluster == 1):
                out += b"\0" * grain_bytes
            else:
                out += _grain(data, cluster << 9, grain_bytes, compressed, has_marker)
        grain += 1
    return bytes(out[:total])


def _read_v4(data: bytes) -> bytes:
    if len(data) < 512:
        raise VmdkError("the VMDK is damaged (its header ends early)")
    hdr = _v4_header(data, 0)
    if hdr["gd_offset"] == GD_AT_END:
        hdr = _v4_footer(data)
    version = hdr["version"]
    if version > 3:
        raise VmdkError(f"unsupported VMDK version {version} (this reads 1 to 3)")
    gtes = hdr["gtes"]
    if not 0 < gtes <= MAX_GTES:
        raise VmdkError(f"the VMDK is damaged (its grain tables hold {gtes} entries)")
    granularity = hdr["granularity"]
    if not 0 < granularity <= MAX_GRANULARITY:
        raise VmdkError(
            f"the VMDK is damaged (its grain size is {granularity} sectors)")
    capacity = hdr["capacity"]
    if not 0 < capacity * 512 <= MAX_SIZE:
        raise VmdkError("the VMDK is damaged (its capacity makes no sense)")
    if hdr["grain_offset"] * 512 > len(data):
        raise VmdkError("the VMDK is truncated (its grains are past the end)")
    compress = hdr["compress"]
    if compress not in (0, DEFLATE):
        raise VmdkError(f"the VMDK is unsupported (compression {compress} is not read)")
    _check_parent(data, hdr)

    l1_size = -(-capacity // (gtes * granularity))
    at = hdr["gd_offset"] << 9
    raw = data[at:at + l1_size * 4]
    if len(raw) < l1_size * 4:
        raise VmdkError("the VMDK is damaged (its grain directory ends early)")
    l1 = struct.unpack(f"<{l1_size}I", raw)
    return _unpack(data, l1, gtes, granularity, capacity,
                   compress == DEFLATE, bool(hdr["flags"] & FLAG_MARKER),
                   bool(hdr["flags"] & FLAG_ZERO_GRAIN))


def _read_v3(data: bytes) -> bytes:
    """The older "COWD" extent: an uncompressed grain directory and grain
    tables of a fixed 4096 entries, with no descriptor of its own."""
    if len(data) < 44:
        raise VmdkError("the VMDK is damaged (its header ends early)")
    (_version, _flags, capacity, granularity, l1_offset, l1_size,
     _file_sectors, _cyl, _heads, _spt) = struct.unpack_from("<10I", data, 4)
    if not 0 < granularity <= MAX_GRANULARITY:
        raise VmdkError(
            f"the VMDK is damaged (its grain size is {granularity} sectors)")
    if not 0 < capacity * 512 <= MAX_SIZE:
        raise VmdkError("the VMDK is damaged (its capacity makes no sense)")
    if not 0 < l1_size:
        raise VmdkError("the VMDK is damaged (its grain directory is empty)")
    if l1_size * L2_SIZE_V3 * granularity < capacity:
        raise VmdkError(
            "the VMDK is damaged (its grain directory cannot reach its capacity)")
    at = l1_offset << 9
    raw = data[at:at + l1_size * 4]
    if len(raw) < l1_size * 4:
        raise VmdkError("the VMDK is damaged (its grain directory ends early)")
    l1 = struct.unpack(f"<{l1_size}I", raw)
    return _unpack(data, l1, L2_SIZE_V3, granularity, capacity, False, False, False)
