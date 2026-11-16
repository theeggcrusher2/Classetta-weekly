"""Tests for VMDK disk images: detection, browsing, failure paths.

Fixtures are written here with a small spec-faithful writer that follows
VMware's published VMDK format description and QEMU block/vmdk.c.
"""

import machfs
import pytest
import struct
import zlib

from macrt import vmdk
from macrt.macfile import image_kind, is_disk_image, load_volume
from macrt.vmdk import VmdkError, is_vmdk

SECTOR = 512
FLAG_ZERO_GRAIN = 1 << 2
FLAG_MARKER = 1 << 17
GD_AT_END = 0xFFFFFFFFFFFFFFFF
DEFLATE = 1


def _descriptor(parent: bool = False) -> bytes:
    """The small text descriptor a sparse extent embeds for itself."""
    text = ("# Disk DescriptorFile\n"
            "version=1\n"
            "createType=\"monolithicSparse\"\n"
            "CID=deadbeef\n")
    if parent:
        text += "parentFileNameHint=\"Parent.vmdk\"\nparentCID=12345678\n"
    else:
        text += "parentCID=ffffffff\n"
    return text.encode()


def _header(version: int, flags: int, capacity: int, granularity: int,
            desc_offset: int, desc_size: int, gtes: int, gd_offset: int,
            grain_offset: int, compress: int) -> bytes:
    h = bytearray(SECTOR)
    h[0:4] = b"KDMV"
    struct.pack_into("<IIQQQQIQQQ", h, 4, version, flags, capacity,
                     granularity, desc_offset, desc_size, gtes, 0,
                     gd_offset, grain_offset)
    struct.pack_into("<H", h, 77, compress)
    return bytes(h)


def _marker(kind: int) -> bytes:
    return struct.pack("<QII", 0, 0, kind).ljust(SECTOR, b"\0")


def _v4(grains: dict, capacity: int, *, granularity: int = 8, gtes: int = 512,
        version: int = 1, flags: int = 0, compress: int = 0,
        gd_at_end: bool = False, zero_grains: tuple = (),
        descriptor: bytes | None = None,
        transform=None) -> bytes:
    """A version-4 sparse extent; `grains` maps grain index to its bytes.

    Grains not in `grains` stay unallocated and read back as zeros.
    `transform`, given the grain index and its on-disk block, replaces the
    block — for planting damage the writer would never write itself.
    """
    if descriptor is None:
        descriptor = _descriptor()
    grain_bytes = granularity * SECTOR
    desc_sectors = max(1, -(-len(descriptor) // SECTOR))
    desc_off = 1
    l1_size = -(-capacity // (gtes * granularity))
    gd_sectors = max(1, -(-(l1_size * 4) // SECTOR))
    tbl_sectors = max(1, -(-(gtes * 4) // SECTOR))

    def block(index: int, data: bytes) -> bytes:
        if compress != DEFLATE:
            return data
        blob = zlib.compress(data)
        if flags & FLAG_MARKER:
            blob = struct.pack("<QI", index * granularity, len(blob)) + blob
        return blob

    positions = {}
    if gd_at_end:
        at = desc_off + desc_sectors
        for index in sorted(grains):
            positions[index] = at
            at += -(-len(block(index, grains[index])) // SECTOR)
        tbl_off = at
        at += l1_size * tbl_sectors
        gd_off = at
        at += gd_sectors
        footer_off = at
        end = footer_off + 3
    else:
        gd_off = desc_off + desc_sectors
        tbl_off = gd_off + gd_sectors
        at = tbl_off + l1_size * tbl_sectors
        for index in sorted(grains):
            positions[index] = at
            at += -(-len(block(index, grains[index])) // SECTOR)
        footer_off = None
        end = max(at, tbl_off + l1_size * tbl_sectors)

    tables = [[0] * gtes for _ in range(l1_size)]
    for index in zero_grains:
        l1, l2 = divmod(index, gtes)
        tables[l1][l2] = 1
    for index, pos in positions.items():
        l1, l2 = divmod(index, gtes)
        tables[l1][l2] = pos
    gd = [tbl_off + i * tbl_sectors if any(tables[i]) else 0
          for i in range(l1_size)]

    out = bytearray(end * SECTOR)
    out[:SECTOR] = _header(version, flags, capacity, granularity, desc_off,
                           desc_sectors, gtes,
                           GD_AT_END if gd_at_end else gd_off,
                           positions[min(positions)] if positions
                           else tbl_off + l1_size * tbl_sectors,
                           compress)
    at = desc_off * SECTOR
    out[at:at + len(descriptor)] = descriptor
    for i, entry in enumerate(gd):
        struct.pack_into("<I", out, gd_off * SECTOR + i * 4, entry)
    for i, table in enumerate(tables):
        base = tbl_off * SECTOR + i * tbl_sectors * SECTOR
        for j, entry in enumerate(table):
            struct.pack_into("<I", out, base + j * 4, entry)
    for index, pos in positions.items():
        data = block(index, grains[index])
        if transform is not None:
            data = transform(index, data)
        at = pos * SECTOR
        out[at:at + len(data)] = data
    if gd_at_end:
        out[footer_off * SECTOR:(footer_off + 1) * SECTOR] = _marker(3)
        base = (footer_off + 1) * SECTOR
        out[base:base + SECTOR] = _header(version, flags, capacity,
                                          granularity, desc_off,
                                          desc_sectors, gtes, gd_off,
                                          positions[min(positions)]
                                          if positions else tbl_off,
                                          compress)
        out[base + SECTOR:base + 2 * SECTOR] = _marker(0)
    return bytes(out)


def _v3(grains: dict, capacity: int, *, granularity: int = 8) -> bytes:
    """A version-3 COWD extent: uncompressed, 4096-entry grain tables."""
    l2_size = 4096
    l1_size = max(1, -(-capacity // (l2_size * granularity)))
    grain_bytes = granularity * SECTOR
    tbl_sectors = l2_size * 4 // SECTOR
    at = 1
    positions = {}
    for index in sorted(grains):
        positions[index] = at
        at += granularity
    tbl_off = at
    gd_off = tbl_off + l1_size * tbl_sectors
    tables = [[0] * l2_size for _ in range(l1_size)]
    for index, pos in positions.items():
        l1, l2 = divmod(index, l2_size)
        tables[l1][l2] = pos
    gd = [tbl_off + i * tbl_sectors if any(tables[i]) else 0
          for i in range(l1_size)]
    out = bytearray((gd_off + 1) * SECTOR)
    head = b"COWD" + struct.pack("<10I", 3, 0, capacity, granularity,
                                 gd_off, l1_size, capacity, 0, 0, 0)
    out[:len(head)] = head
    for i, entry in enumerate(gd):
        struct.pack_into("<I", out, gd_off * SECTOR + i * 4, entry)
    for i, table in enumerate(tables):
        base = tbl_off * SECTOR + i * tbl_sectors * SECTOR
        for j, entry in enumerate(table):
            struct.pack_into("<I", out, base + j * 4, entry)
    for index, pos in positions.items():
        at = pos * SECTOR
        out[at:at + grain_bytes] = grains[index]
    return bytes(out)


def _volume_raw() -> bytes:
    v = machfs.Volume()
    v.name = "VMDK Disk"
    f = machfs.File()
    f.type, f.creator, f.data = b"TEXT", b"ttxt", bytearray(b"vmdk payload " * 100)
    v["readme.txt"] = f
    return v.write(size=1024 * 1024)


def _grains(raw: bytes, granularity: int = 8) -> dict:
    """The volume in grain-sized pieces, keyed by grain index."""
    step = granularity * SECTOR
    return {i // step: raw[i:i + step] for i in range(0, len(raw), step)}


def _macfile(data: bytes, name: str = "disk.vmdk"):
    from macrt.macfile import MacFile
    return MacFile(name, data, None, None, b"????", b"????", 0, "k", None)


# --- detection ----------------------------------------------------------


def test_v4_and_v3_are_detected_as_disk_images():
    raw = _volume_raw()
    v4 = _v4(_grains(raw), 2048)
    v3 = _v3(_grains(raw), 2048)
    v4_end = _v4(_grains(raw), 2048, compress=DEFLATE, gd_at_end=True,
                 flags=FLAG_MARKER)
    assert is_vmdk(v4) and is_disk_image(v4)
    assert is_vmdk(v3) and is_disk_image(v3)
    assert is_vmdk(v4_end) and is_disk_image(v4_end)
    assert not is_vmdk(b"not a vmdk at all")


def test_a_vmdk_is_named_by_kind():
    raw = _volume_raw()
    v4 = _v4(_grains(raw), 2048)
    v3 = _v3(_grains(raw), 2048)
    assert image_kind(_macfile(v4)) == "VMDK disk image"
    assert image_kind(_macfile(v3)) == "VMDK disk image"


# --- browsing ----------------------------------------------------------


def test_a_raw_v4_opens_as_a_volume():
    raw = _volume_raw()
    vol = load_volume(_v4(_grains(raw), 2048))
    assert vol.name == "VMDK Disk"
    assert vol["readme.txt"].data == b"vmdk payload " * 100


def test_a_compressed_v4_with_markers_and_the_gd_at_the_end_opens():
    raw = _volume_raw()
    img = _v4(_grains(raw), 2048, compress=DEFLATE, gd_at_end=True,
              flags=FLAG_MARKER)
    opened = load_volume(img)
    assert opened.name == "VMDK Disk"
    assert opened["readme.txt"].data == b"vmdk payload " * 100


def test_a_compressed_v4_without_markers_opens_as_a_volume():
    raw = _volume_raw()
    img = _v4(_grains(raw), 2048, compress=DEFLATE)
    opened = load_volume(img)
    assert opened.name == "VMDK Disk"
    assert opened["readme.txt"].data == b"vmdk payload " * 100


def test_a_v3_cowd_opens_as_a_volume():
    raw = _volume_raw()
    img = _v3(_grains(raw), 2048)
    vol = load_volume(img)
    assert vol.name == "VMDK Disk"
    assert vol["readme.txt"].data == b"vmdk payload " * 100


def test_an_unallocated_grain_reads_zeros():
    raw = _volume_raw()
    grains = _grains(raw)
    # Ten grains across three grain directories; only two allocated.
    sparse = {0: grains[0], 3: grains[3]}
    img = _v4(sparse, 64, gtes=2)
    out = vmdk.read_image(img)
    assert out[:4096] == grains[0]
    assert out[4096:3 * 4096] == b"\0" * 2 * 4096
    assert out[3 * 4096:4 * 4096] == grains[3]
    assert out[4 * 4096:] == b"\0" * (64 * SECTOR - 4 * 4096)


def test_a_zero_grain_reads_zeros():
    raw = _volume_raw()
    img = _v4(_grains(raw), 2048, flags=FLAG_ZERO_GRAIN, zero_grains=(5,))
    out = vmdk.read_image(img)
    assert out[5 * 4096:6 * 4096] == b"\0" * 4096
    assert out[:5 * 4096] == raw[:5 * 4096]
    assert out[6 * 4096:] == raw[6 * 4096:]


# --- refusal -----------------------------------------------------------


def test_a_text_descriptor_is_refused():
    data = _descriptor()
    assert is_vmdk(data)
    with pytest.raises(VmdkError, match="a text descriptor's data lives in other files"):
        vmdk.read_image(data)


def test_a_vmdk_with_a_parent_file_name_is_refused():
    img = _v4({}, 64, descriptor=_descriptor(parent=True))
    with pytest.raises(VmdkError, match="needs its parent image"):
        vmdk.read_image(img)


def test_a_vmdk_with_a_parent_cid_is_refused():
    descriptor = (b"# Disk DescriptorFile\nversion=1\nparentCID=12345678\n")
    img = _v4({}, 64, descriptor=descriptor)
    with pytest.raises(VmdkError, match="needs its parent image"):
        vmdk.read_image(img)


def test_a_vmdk_version_above_3_is_refused():
    img = _v4({}, 64, version=4)
    with pytest.raises(VmdkError, match="unsupported VMDK version 4"):
        vmdk.read_image(img)


def test_a_vmdk_with_unreadable_compression_is_refused():
    img = _v4({}, 64, compress=2)
    with pytest.raises(VmdkError, match="compression 2 is not read"):
        vmdk.read_image(img)


def test_a_file_that_is_not_a_vmdk_is_refused():
    with pytest.raises(VmdkError, match="no KDMV or COWD signature"):
        vmdk.read_image(b"NOPE" + b"\0" * 512)


def test_a_vmdk_whose_header_is_short_is_refused():
    with pytest.raises(VmdkError, match="its header ends early"):
        vmdk.read_image(b"KDMV" + b"\0" * 100)


def test_a_vmdk_whose_grain_directory_ends_early_is_refused():
    # The header's GD offset points past the end of the file.
    img = _v4({0: b"\1" * 4096}, 8, gtes=1)
    img = img[:56] + struct.pack("<Q", 0xFFFFFFFF) + img[64:]
    with pytest.raises(VmdkError, match="its grain directory ends early"):
        vmdk.read_image(img)


def test_a_vmdk_whose_grain_table_ends_early_is_refused():
    # The grain directory's entry points past the end of the file.
    img = _v4({0: b"\1" * 4096}, 8, gtes=1)
    gd_at = 2 * SECTOR                                  # GD at sector 2
    img = img[:gd_at] + struct.pack("<I", 0xFFFFFFF0) + img[gd_at + 4:]
    with pytest.raises(VmdkError, match="a grain table ends early"):
        vmdk.read_image(img)


def test_a_vmdk_whose_grain_ends_early_is_refused():
    # The grain table's entry points past the end of the file.
    img = _v4({0: b"\1" * 4096}, 8, gtes=1)
    tbl_at = 3 * SECTOR                                # table at sector 3
    img = img[:tbl_at] + struct.pack("<I", 0xFFFFFFF8) + img[tbl_at + 4:]
    with pytest.raises(VmdkError, match="a grain ends early"):
        vmdk.read_image(img)


def test_a_vmdk_whose_grains_are_past_the_end_is_refused():
    # The header's grain offset is past the end of the file.
    img = _v4({0: b"\1" * 4096}, 8, gtes=1)
    img = img[:64] + struct.pack("<Q", 0xFFFFFFFF) + img[72:]
    with pytest.raises(VmdkError, match="its grains are past the end"):
        vmdk.read_image(img)


def test_a_vmdk_with_a_capacity_of_zero_is_refused():
    img = _v4({}, 8, gtes=1)
    img = img[:12] + b"\0" * 8 + img[20:]
    with pytest.raises(VmdkError, match="its capacity makes no sense"):
        vmdk.read_image(img)


def test_a_vmdk_with_no_grain_table_entries_is_refused():
    img = _v4({}, 8, gtes=1)
    img = img[:44] + b"\0" * 4 + img[48:]
    with pytest.raises(VmdkError, match="its grain tables hold 0 entries"):
        vmdk.read_image(img)


def test_a_grain_that_does_not_decompress_is_refused():
    raw = _volume_raw()
    grains = _grains(raw)
    img = _v4(grains, 2048, compress=DEFLATE, transform=lambda i, b: b"\0" * 20)
    with pytest.raises(VmdkError, match="a grain does not decompress"):
        vmdk.read_image(img)


def test_a_grain_marker_whose_size_is_wrong_is_refused():
    raw = _volume_raw()
    grains = _grains(raw)

    def plant(index, block):
        if index == 0:
            return struct.pack("<QI", 0, 999999) + b"\0" * 16
        return block

    img = _v4(grains, 2048, compress=DEFLATE, flags=FLAG_MARKER,
              transform=plant)
    with pytest.raises(VmdkError, match="a grain marker size is wrong"):
        vmdk.read_image(img)


def test_a_footer_whose_marker_is_wrong_is_refused():
    raw = _volume_raw()
    img = bytearray(_v4(_grains(raw), 2048, compress=DEFLATE,
                        gd_at_end=True, flags=FLAG_MARKER))
    base = len(img) - 1536
    img[base + 12] ^= 0xFF                     # the footer marker's type
    with pytest.raises(VmdkError, match="its footer marker is wrong"):
        vmdk.read_image(bytes(img))


def test_a_footer_without_a_kdmv_signature_is_refused():
    raw = _volume_raw()
    img = bytearray(_v4(_grains(raw), 2048, compress=DEFLATE,
                        gd_at_end=True, flags=FLAG_MARKER))
    base = len(img) - 1536 + 512
    img[base:base + 4] = b"NOPE"
    with pytest.raises(VmdkError, match="its footer has no KDMV signature"):
        vmdk.read_image(bytes(img))


def test_a_footer_whose_end_of_stream_marker_is_wrong_is_refused():
    raw = _volume_raw()
    img = bytearray(_v4(_grains(raw), 2048, compress=DEFLATE,
                        gd_at_end=True, flags=FLAG_MARKER))
    base = len(img) - 1536 + 1024 + 8
    img[base:base + 4] = b"\1\1\1\1"           # the EOS marker's size
    with pytest.raises(VmdkError, match="its end-of-stream marker is wrong"):
        vmdk.read_image(bytes(img))


def test_a_footer_is_required_when_the_grain_directory_is_at_the_end():
    hdr = _header(1, 0, 8, 8, 1, 1, 1, GD_AT_END, 2, 0)
    with pytest.raises(VmdkError, match="its footer is missing"):
        vmdk.read_image(hdr + b"\0" * 88)      # 600 bytes: no room for a footer


def test_a_v3_whose_header_is_short_is_refused():
    with pytest.raises(VmdkError, match="its header ends early"):
        vmdk.read_image(b"COWD" + b"\0" * 20)
