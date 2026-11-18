"""Tests for LazyDtcCatalog with disk-record suffixes larger than 308 bytes.

Image-sourced DT2 catalogs carry disk-record suffixes of 724-1755 bytes.
The lazy offset walk used to hardcode 308, mis-locating everything after
the disk record.  These tests build catalogs inline (see
disktracker/writer.py and disktracker/tests/test_dtc.py for the on-disk
shape) and check the variable-suffix handling.  No external files are read.
"""

import struct

from disktracker.disktracker import HEADER_SIZE, _find_first_item, _parse_item_at
from disktracker.lazy import LazyDtcCatalog


def _header() -> bytes:
    h = bytearray(HEADER_SIZE)
    h[:8] = b'disktrak'
    struct.pack_into('>I', h, 0x0C, 2)
    return bytes(h)


def _front(typ: bytes = b'TEXT', creator: bytes = b'MACS', depth: int = 1,
           items: int = 0, descendants: int = 0) -> bytes:
    f = bytearray(39)
    f[1:5] = typ
    f[5:9] = creator
    struct.pack_into('>I', f, 13, 0)        # size
    struct.pack_into('>I', f, 17, 0)        # parent
    struct.pack_into('>I', f, 21, 1000000)  # created
    struct.pack_into('>I', f, 25, 2000000)  # modified
    struct.pack_into('>H', f, 29, depth)
    struct.pack_into('>I', f, 31, items)
    struct.pack_into('>I', f, 35, descendants)
    return bytes(f)


def _item(name: str, depth: int = 1, typ: bytes = b'TEXT',
          creator: bytes = b'MACS', tail: bytes = b'\x00') -> bytes:
    nb = name.encode('mac_roman')
    return _front(typ, creator, depth) + bytes([len(nb)]) + nb + tail


def _disk_record(name: str, count: int, suffix: bytes) -> bytes:
    nb = name.encode('mac_roman')
    rec = bytearray(0x52 + 1 + len(nb) + len(suffix))
    struct.pack_into('>I', rec, 6, 1000000)
    struct.pack_into('>I', rec, 10, 2000000)
    struct.pack_into('>I', rec, 0x14, count)
    rec[0x19:0x19 + len(nb)] = nb  # name echo, required by disk-record detection
    rec[0x52] = len(nb)
    rec[0x53:0x53 + len(nb)] = nb
    rec[0x53 + len(nb):] = suffix
    return bytes(rec)


# 724-byte suffix: name tag + twelve 12 00 / 13 00 tags + NUL filler.
# The filler carries no byte 0x01 in a depth position, so the first-item
# scan cannot mistake it for an item record.
BIG_SUFFIX = (b'\x13BigVol' + b'\x12\x00' * 6 + b'\x13\x00' * 6)
BIG_SUFFIX = BIG_SUFFIX + b'\x00' * (724 - len(BIG_SUFFIX))
assert len(BIG_SUFFIX) == 724


def test_large_suffix_is_not_truncated():
    disk = _disk_record('Big Disk', 1, BIG_SUFFIX)
    data = _header() + disk + _item('hello.txt')
    cat = LazyDtcCatalog(data)
    d = cat.load_item(0)
    assert d.suffix == BIG_SUFFIX
    assert len(d.suffix) == 724


def test_items_after_large_suffix_parse():
    disk = _disk_record('Big Disk', 2, BIG_SUFFIX)
    data = _header() + disk + _item('hello.txt') + _item('world.txt')
    after = HEADER_SIZE + 0x53 + len('Big Disk')
    first = _find_first_item(data, after)
    assert first == HEADER_SIZE + len(disk)
    parsed, nxt = _parse_item_at(data, first)
    assert parsed.name == 'hello.txt'
    parsed2, _ = _parse_item_at(data, nxt)
    assert parsed2.name == 'world.txt'
    # The offset walk must land on the disk record only; no phantom
    # disk records picked up inside the suffix.
    cat = LazyDtcCatalog(data)
    assert cat._item_offsets == [HEADER_SIZE]


def test_second_disk_found_after_large_suffix():
    disk1 = _disk_record('First Disk', 1, BIG_SUFFIX)
    items1 = _item('hello.txt')
    disk2_off = HEADER_SIZE + len(disk1) + len(items1)
    disk2 = _disk_record('Second Disk', 1, b'\x13Small' + b'\x00' * 302)
    data = _header() + disk1 + items1 + disk2 + _item('world.txt')
    cat = LazyDtcCatalog(data)
    assert cat._item_offsets == [HEADER_SIZE, disk2_off]
    # load_item only parses the index-0 disk record; other entries are
    # placeholders carrying the offset the walk computed.
    d2 = cat.load_item(1)
    assert d2.offset == disk2_off


def test_suffix_of_exactly_308_still_parses():
    suffix = b'\x13Phys' + b'\x00' * (308 - 5)
    disk1 = _disk_record('First Disk', 1, suffix)
    items1 = _item('hello.txt')
    disk2_off = HEADER_SIZE + len(disk1) + len(items1)
    disk2 = _disk_record('Second Disk', 1, suffix)
    data = _header() + disk1 + items1 + disk2 + _item('world.txt')
    cat = LazyDtcCatalog(data)
    assert cat._item_offsets == [HEADER_SIZE, disk2_off]
    d0 = cat.load_item(0)
    assert d0.suffix == suffix
    assert len(d0.suffix) == 308


def test_suffix_smaller_than_308_still_parses():
    suffix = b'\x13Tiny' + b'\x00' * (100 - 5)  # 100 bytes
    disk1 = _disk_record('First Disk', 1, suffix)
    items1 = _item('hello.txt')
    disk2_off = HEADER_SIZE + len(disk1) + len(items1)
    disk2 = _disk_record('Second Disk', 1, suffix)
    data = _header() + disk1 + items1 + disk2 + _item('world.txt')
    cat = LazyDtcCatalog(data)
    assert cat._item_offsets == [HEADER_SIZE, disk2_off]
    d0 = cat.load_item(0)
    assert d0.suffix == suffix
    assert len(d0.suffix) == 100


def test_damaged_catalog_falls_back_to_308():
    # count claims one item but the record is followed by 724 bytes of
    # 0xFF with no parseable item: the scan returns None and lazy.py
    # falls back to the historical 308-byte assumption instead of crashing.
    disk = _disk_record('Broken Disk', 1, b'\xff' * 724)
    data = _header() + disk
    cat = LazyDtcCatalog(data)  # must not raise
    d = cat.load_item(0)
    after = HEADER_SIZE + 0x53 + len('Broken Disk')
    assert d.suffix == data[after:after + 308]
    assert len(d.suffix) == 308
