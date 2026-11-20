"""Read Virtual Hard Disk (VHD) images.

Windows' VHD format: a fixed or dynamic virtual disk with a 512-byte footer
at the end.  This reader handles fixed and dynamic types; it refuses
differencing (type 4) and unknown types, and validates footer and dynamic
header checksums (one's complement of the sum of all other bytes).

From the VHD spec (Keramics, comp3301 PDF) and QEMU block/vpc.c.
"""

import struct

MAGIC = b"conectix"   # @offset 0 in footer


class VHDError(ValueError):
    """A VHD this reader won't read: damaged, unsupported type, or bad checksum."""


def is_vhd(data: bytes) -> bool:
    """Whether data looks like a VHD: the footer cookie at the end (every
    VHD's real footer) or at the start (the copy dynamic VHDs keep there,
    which is all a head sample can see of one)."""
    if len(data) < 512:
        return False
    return data[:8] == MAGIC or data[-512:-504] == MAGIC


def _one_complement_sum(data: bytes) -> int:
    """One's complement (bitwise NOT) of the 32-bit sum of all bytes."""
    s = sum(data) & 0xFFFFFFFF
    return (~s) & 0xFFFFFFFF


def _read_footer(data: bytes):
    """Parse the 512-byte VHD footer at the end of data.  Returns a dict."""
    footer = data[-512:]
    cookie = footer[0:8]
    if cookie != MAGIC:
        raise VHDError("footer has no VHD magic (cookie not 'conectix')")
    features = struct.unpack_from(">I", footer, 8)[0]
    version = struct.unpack_from(">I", footer, 12)[0]
    data_offset = struct.unpack_from(">Q", footer, 16)[0]
    time_stamp = struct.unpack_from(">I", footer, 24)[0]
    creator_app = struct.unpack_from(">I", footer, 28)[0]
    creator_version = struct.unpack_from(">I", footer, 32)[0]
    creator_host = struct.unpack_from(">I", footer, 36)[0]
    original_size = struct.unpack_from(">Q", footer, 40)[0]
    current_size = struct.unpack_from(">Q", footer, 48)[0]
    disk_geometry = struct.unpack_from(">I", footer, 56)[0]
    disk_type = struct.unpack_from(">I", footer, 60)[0]
    checksum = struct.unpack_from(">I", footer, 64)[0]
    unique_id = footer[68:84]
    saved_state = footer[84]
    return {
        "cookie": cookie,
        "features": features,
        "version": version,
        "data_offset": data_offset,
        "time_stamp": time_stamp,
        "creator_app": creator_app,
        "creator_version": creator_version,
        "creator_host": creator_host,
        "original_size": original_size,
        "current_size": current_size,
        "disk_geometry": disk_geometry,
        "disk_type": disk_type,
        "checksum": checksum,
        "unique_id": unique_id,
        "saved_state": saved_state,
        "raw": footer,
    }


def _validate_footer_checksum(footer_dict: dict) -> None:
    """Validate the footer checksum: ~sum(bytes 0..63) must equal bytes 64..67."""
    footer = footer_dict["raw"]
    # bytes 64..67 are the stored checksum; zero them and compute
    altered = bytearray(footer[:64]) + bytearray(48)  # 64..111 zeroed; but checksum is at 64..67
    # Actually we need bytes 0..63 with 64..67 zeroed, then ~sum == stored checksum
    checker = bytearray(footer[:64]) + b"\x00" * 4  # zero the checksum field
    computed = _one_complement_sum(bytes(checker))
    if computed != footer_dict["checksum"]:
        raise VHDError(
            f"footer checksum mismatch: computed {computed:#010x}, stored {footer_dict['checksum']:#010x}"
        )


def _read_dynamic_header(data: bytes, footer_dict: dict):
    """Parse the dynamic header; called when footer disk_type == 3 (dynamic)."""
    offset = footer_dict["data_offset"]
    if offset >= len(data):
        raise VHDError(f"dynamic header offset {offset:#x} exceeds file length")
    dhead = data[offset:offset + 1024]  # dynamic header is always 1024 B
    if len(dhead) < 1024:
        raise VHDError("dynamic header runs past end of file")
    cookie = dhead[0:8]
    if cookie != b"cxsparse":
        raise VHDError("dynamic header has no 'cxsparse' cookie")
    data_offset_dyn = struct.unpack_from(">Q", dhead, 8)[0]  # usually 0xFFFFFFFF..
    table_offset = struct.unpack_from(">Q", dhead, 16)[0]
    header_version = struct.unpack_from(">I", dhead, 24)[0]
    max_table_entries = struct.unpack_from(">I", dhead, 28)[0]
    block_size = struct.unpack_from(">I", dhead, 32)[0]
    checksum = struct.unpack_from(">I", dhead, 36)[0]
    # Validate dynamic header checksum: ~sum of bytes 0..35 + 40..1023
    # (i.e. all bytes except the checksum at 36..39)
    checker = bytearray(dhead[:36]) + bytearray(4) + bytearray(dhead[40:])
    computed = _one_complement_sum(bytes(checker))
    if computed != checksum:
        raise VHDError(
            f"dynamic header checksum mismatch: computed {computed:#010x}, stored {checksum:#010x}"
        )
    return {
        "data_offset": data_offset_dyn,
        "table_offset": table_offset,
        "header_version": header_version,
        "max_table_entries": max_table_entries,
        "block_size": block_size,
        "checksum": checksum,
    }


def read_image(data: bytes, /) -> bytes:
    """The raw virtual-disk bytes a VHD holds, unpacked.

    Fixed VHDs: data = file[:-512][:current_size].
    Dynamic VHDs: unpack clusters using the BAT; bitmap bits set → zeros;
      clear bits → read the actual data.
    Raises VHDError for unsupported types, differencing, bad checksums,
    or saved_state == 1.
    """
    if not is_vhd(data):
        raise VHDError("footer has no VHD magic (cookie not 'conectix')")
    footer = _read_footer(data)
    _validate_footer_checksum(footer)

    disk_type = footer["disk_type"]
    saved_state = footer["saved_state"]

    if saved_state == 1:
        raise VHDError("saved state == 1: refusing to load a suspended VM image")

    if disk_type == 4:
        raise VHDError("differencing (type 4) VHDs need a parent; not supported")
    if disk_type not in (2, 3):
        raise VHDError(f"unsupported disk type {disk_type} (2=fixed, 3=dynamic, 4=differencing)")

    current_size = footer["current_size"]

    if disk_type == 2:
        # Fixed: the virtual disk is the file minus its 512-byte footer
        if len(data) - 512 < current_size:
            raise VHDError("file shorter than current_size after stripping footer")
        return data[:current_size]

    # disk_type == 3: dynamic
    dynamic = _read_dynamic_header(data, footer)
    block_size = dynamic["block_size"]
    max_table_entries = dynamic["max_table_entries"]
    table_offset = dynamic["table_offset"]

    # sectors_per_block = block_size // 512  (block_size is power-of-two in sectors)
    sectors_per_block = block_size // 512

    # Read BAT: max_table_entries × u32 BE starting at table_offset
    bat_data = data[table_offset : table_offset + max_table_entries * 4]
    if len(bat_data) < max_table_entries * 4:
        raise VHDError("BAT runs past end of file")
    bat = struct.unpack(f">{max_table_entries}I", bat_data)

    # The file holds only allocated blocks, so it may be shorter than
    # current_size; the footer is the last 512 bytes
    raw = data[:-512]  # strip footer

    # Each on-disk block is a sector bitmap of ceil(block_size/4096) bytes
    # rounded up to a 512-byte multiple, then block_size bytes of data.
    bitmap_size = (block_size + 4095) // 4096
    bitmap_size = (bitmap_size + 511) // 512 * 512

    out = bytearray()
    total_sectors = (current_size + 511) // 512
    for sec_idx in range(total_sectors):
        block = sec_idx // sectors_per_block
        sector_in_block = sec_idx % sectors_per_block
        if block >= max_table_entries:
            out += b"\0" * 512
            continue
        bat_entry = bat[block]
        if bat_entry == 0xFFFFFFFF or bat_entry == 0:
            out += b"\0" * 512
            continue
        block_offset = bat_entry * 512
        block_end = block_offset + bitmap_size + block_size
        if block_offset >= len(raw):
            out += b"\0" * 512
            continue
        block_data = raw[block_offset:block_end]
        if len(block_data) < block_end - block_offset:
            block_data += b"\0" * (block_end - block_offset - len(block_data))
        bitmap = block_data[:bitmap_size]
        # The bitmap is MSB first: sector 0 is bit 7 of byte 0
        byte_idx = sector_in_block // 8
        bit_idx = 7 - (sector_in_block % 8)
        if byte_idx < len(bitmap) and bitmap[byte_idx] & (1 << bit_idx):
            data_start = bitmap_size + sector_in_block * 512
            out += block_data[data_start:data_start + 512]
        else:
            out += b"\0" * 512

    return bytes(out)
