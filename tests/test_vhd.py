"""Tests for VHD disk images: detection, browsing, failure paths.

Fixtures are written here with a small spec-faithful writer that follows the
public VHD specification (Keramics, comp3301 PDF) and QEMU block/vpc.c.
"""

import machfs
import pytest
import struct

from macrt import vhd
from macrt.macfile import image_kind, is_disk_image, load_volume
from macrt.vhd import VHDError, is_vhd

SECTOR = 512


def _checksum(struct_bytes: bytes) -> int:
    """The VHD checksum: one's complement of the 32-bit byte sum."""
    return (~(sum(struct_bytes) & 0xFFFFFFFF)) & 0xFFFFFFFF


def _footer(fields: dict) -> bytes:
    f = bytearray(SECTOR)
    f[0:8] = b"conectix"
    struct.pack_into(">I", f, 8, fields.get("features", 0x00000002))
    struct.pack_into(">I", f, 12, 0x00010000)              # version 1.0
    struct.pack_into(">Q", f, 16, fields.get("data_offset", 0xFFFFFFFFFFFFFFFF))
    struct.pack_into(">Q", f, 40, fields["current_size"])
    struct.pack_into(">Q", f, 48, fields["current_size"])
    struct.pack_into(">I", f, 56, 0)                       # geometry
    struct.pack_into(">I", f, 60, fields["disk_type"])
    f[84] = fields.get("saved_state", 0)
    struct.pack_into(">I", f, 64, _checksum(bytes(f[:64]) + b"\0" * 4))
    return bytes(f)


def _fixed_vhd(volume: bytes, disk_type: int = 2, saved_state: int = 0) -> bytes:
    """A fixed VHD: the virtual disk bytes, then the 512-byte footer."""
    footer = _footer({"current_size": len(volume), "disk_type": disk_type,
                      "saved_state": saved_state})
    return volume + footer


def _dynamic_header(table_offset: int, max_entries: int, block_size: int) -> bytes:
    d = bytearray(1024)
    d[0:8] = b"cxsparse"
    struct.pack_into(">Q", d, 8, 0xFFFFFFFFFFFFFFFF)       # data_offset (unused)
    struct.pack_into(">Q", d, 16, table_offset)            # BAT offset
    struct.pack_into(">I", d, 24, 0x00010000)              # header_version
    struct.pack_into(">I", d, 28, max_entries)
    struct.pack_into(">I", d, 32, block_size)
    struct.pack_into(">I", d, 36, _checksum(bytes(d[:36]) + b"\0" * 4 + bytes(d[40:])))
    return bytes(d)


def _dynamic_vhd(hunks: list[bytes], block_size: int = 512 * 1024,
                 unallocated=frozenset()) -> bytes:
    """A dynamic VHD of one block per hunk (block_size each).

    Each block on disk: a bitmap of sectors (ceil(block_size/4096) bytes,
    rounded up to 512) followed by the present sectors.  All sectors are
    present when allocated; blocks in `unallocated` have their BAT entry
    set to 0xFFFFFFFF and read back as zeros.
    """
    sectors_per_block = block_size // SECTOR
    bitmap_sectors = -(-(block_size // 4096) // SECTOR)     # ceil/4096 then ceil/512
    bitmap_bytes = bitmap_sectors * SECTOR
    # Layout: dynamic header (1024 B), BAT padded to a sector boundary, blocks.
    # BAT entries are absolute sector offsets of each block.
    table_off = 1024
    bat_bytes_len = 4 * len(hunks)
    blocks_off = table_off + -(-bat_bytes_len // SECTOR) * SECTOR
    on_disk_block = bitmap_bytes + block_size
    bat = []
    blocks = []
    for i, hunk in enumerate(hunks):
        if i in unallocated:
            bat.append(0xFFFFFFFF)                         # unallocated block
        else:
            bat.append((blocks_off + i * on_disk_block) // SECTOR)
        # Build bitmap: one bit per sector, MSB first, then the sectors
        bitmap = bytearray(bitmap_bytes)
        present = bytearray()
        for s in range(sectors_per_block):
            bitmap[s // 8] |= 1 << (7 - s % 8)
            present += hunk[s * SECTOR:(s + 1) * SECTOR]
        blocks.append((bytes(bitmap), bytes(present)))
    out = bytearray()
    out += _dynamic_header(table_off, len(hunks), block_size)
    out += bat_bytes_len and b"".join(struct.pack(">I", b) for b in bat) or b""
    out += b"\0" * (blocks_off - table_off - bat_bytes_len)   # pad BAT to a sector
    for bitmap, present in blocks:
        out += bitmap + present
    # A fixed-style footer, with data_offset pointing at the dynamic header
    footer = _footer({"current_size": len(hunks) * block_size, "disk_type": 3,
                      "data_offset": 0})
    out += footer
    return bytes(out)


# --- fixtures ----------------------------------------------------------


def _volume_raw() -> bytes:
    v = machfs.Volume()
    v.name = "VHD Disk"
    f = machfs.File()
    f.type, f.creator, f.data = b"TEXT", b"ttxt", bytearray(b"vhd payload " * 100)
    v["readme.txt"] = f
    return v.write(size=1024 * 1024)


def _hunks(raw: bytes) -> list[bytes]:
    """The volume in 512 KiB hunks, one dynamic VHD block each."""
    step = 512 * 1024
    return [raw[i:i + step] for i in range(0, len(raw), step)]


# --- detection ---------------------------------------------------------


def test_vhd_is_detected_as_a_disk_image():
    raw = _volume_raw()
    fixed = _fixed_vhd(raw)
    dynamic = _dynamic_vhd(_hunks(raw))
    assert is_vhd(fixed) and is_disk_image(fixed)
    assert is_vhd(dynamic) and is_disk_image(dynamic)
    assert not is_vhd(b"not a vhd at all")


def _macfile(data: bytes):
    from macrt.macfile import MacFile
    return MacFile("disk.vhd", data, None, None, b"????", b"????", 0, "k", None)


def test_a_vhd_is_named_by_kind():
    raw = _volume_raw()
    fixed = _fixed_vhd(raw)
    dynamic = _dynamic_vhd(_hunks(raw))
    assert image_kind(_macfile(fixed)) == "VHD disk image"
    assert image_kind(_macfile(dynamic)) == "VHD disk image"


# --- browsing ----------------------------------------------------------


def test_a_fixed_vhd_opens_as_a_volume():
    raw = _volume_raw()
    vol = load_volume(_fixed_vhd(raw))
    assert vol.name == "VHD Disk"
    assert vol["readme.txt"].data == b"vhd payload " * 100


def test_a_dynamic_vhd_opens_as_a_volume():
    raw = _volume_raw()
    vol = load_volume(_dynamic_vhd(_hunks(raw)))
    assert vol.name == "VHD Disk"
    assert vol["readme.txt"].data == b"vhd payload " * 100


def test_an_unallocated_block_reads_zeros():
    raw = _volume_raw()
    # Mark the first half's block unallocated: the volume header is lost
    img = _dynamic_vhd(_hunks(raw), unallocated={0})
    out = vhd.read_image(img)
    assert out[:512 * 1024] == b"\0" * (512 * 1024)        # block 0 gone
    assert out[512 * 1024:] == raw[512 * 1024:]           # block 1 intact


# --- corruption and refusal --------------------------------------------


def test_a_vhd_with_the_wrong_cookie_is_refused():
    raw = _volume_raw()
    img = bytearray(_fixed_vhd(raw))
    img[-512:-512 + 8] = b"wrongmag"  # corrupt the cookie at start of footer
    with pytest.raises(VHDError, match="footer has no VHD magic"):
        vhd.read_image(bytes(img))


def test_a_vhd_with_a_bad_footer_checksum_is_refused():
    raw = _volume_raw()
    img = bytearray(_fixed_vhd(raw))
    img[-512 + 64] ^= 0xFF  # corrupt the checksum
    with pytest.raises(VHDError, match="footer checksum"):
        vhd.read_image(bytes(img))


def test_a_dynamic_vhd_with_a_bad_dyn_header_checksum_is_refused():
    raw = _volume_raw()
    img = bytearray(_dynamic_vhd(_hunks(raw)))
    # The dynamic header starts at offset 0 in the file body
    img[36] ^= 0xFF
    with pytest.raises(VHDError, match="dynamic header checksum"):
        vhd.read_image(bytes(img))


def test_a_differencing_vhd_is_refused():
    raw = _volume_raw()
    img = _fixed_vhd(raw, disk_type=4)
    with pytest.raises(VHDError, match="differencing"):
        vhd.read_image(img)


def test_a_vhd_with_saved_state_one_is_refused():
    raw = _volume_raw()
    img = _fixed_vhd(raw, saved_state=1)
    with pytest.raises(VHDError, match="saved state"):
        vhd.read_image(img)


def test_a_vhd_with_an_unknown_disk_type_is_refused():
    raw = _volume_raw()
    img = _fixed_vhd(raw, disk_type=99)
    with pytest.raises(VHDError, match="unsupported disk type"):
        vhd.read_image(img)