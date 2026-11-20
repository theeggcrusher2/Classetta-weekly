"""Unit tests for the disktracker library.

All fixtures are generated: catalogs are built in memory with the
helpers below (or by write_dtc) and parsed back.  No external sample
catalogs are read or required.
"""

import struct

import pytest

from disktracker import Item, parse_dtc, write_dtc
from disktracker.disktracker import HEADER_SIZE, _type_ok


def _header() -> bytes:
    h = bytearray(HEADER_SIZE)
    h[:8] = b'disktrak'
    struct.pack_into('>I', h, 0x0C, 2)
    return bytes(h)


def _front(typ: bytes = b'TEXT', creator: bytes = b'MACS', size: int = 0,
           parent: int = 0, created: int = 1000000, modified: int = 2000000,
           depth: int = 1, items: int = 0, descendants: int = 0,
           b0: int = 0x00) -> bytes:
    f = bytearray(39)
    f[0] = b0
    f[1:5] = typ
    f[5:9] = creator
    struct.pack_into('>I', f, 13, size)
    struct.pack_into('>I', f, 17, parent)
    struct.pack_into('>I', f, 21, created)
    struct.pack_into('>I', f, 25, modified)
    struct.pack_into('>H', f, 29, depth)
    struct.pack_into('>I', f, 31, items)
    struct.pack_into('>I', f, 35, descendants)
    return bytes(f)


def _disk_record(name: str, count: int, echo: bool = True) -> bytes:
    nb = name.encode('mac_roman')
    rec = bytearray(0x52 + 1 + len(nb) + 308)
    struct.pack_into('>I', rec, 6, 1000000)
    struct.pack_into('>I', rec, 10, 2000000)
    struct.pack_into('>I', rec, 0x14, count)
    if echo:
        rec[0x19:0x19 + len(nb)] = nb
    rec[0x52] = len(nb)
    rec[0x53:0x53 + len(nb)] = nb
    return bytes(rec)


def _item(name: str, depth: int = 1, typ: bytes = b'TEXT',
          creator: bytes = b'MACS', tail: bytes = b'\x00',
          items: int = 0, descendants: int = 0) -> bytes:
    nb = name.encode('mac_roman')
    return (_front(typ, creator, depth=depth, items=items,
                   descendants=descendants)
            + bytes([len(nb)]) + nb + tail)


def _long_item(name: str, depth: int = 1, typ: bytes = b'TEXT',
               creator: bytes = b'MACS', pad: int = 0,
               flen_zero: bool = False) -> bytes:
    """A long-name record: 0xe1 marker, truncated name1, NUL, optional
    zero padding, 8-byte meta.  With flen_zero the meta declares no full
    name (name1 is the name) and 8 extra bytes follow the meta."""
    nb = name.encode('utf-8')
    ln = 0xE1
    name1_len = 256 - ln
    rec = bytearray(_front(typ, creator, depth=depth))
    rec.append(ln)
    rec += nb[:name1_len].ljust(name1_len, b'\x00')
    rec += b'\x00'
    rec += b'\x00' * pad
    if flen_zero:
        rec += bytes(8) + bytes(8)
    else:
        # meta[0] must be non-zero: the parser's padded-meta candidate is
        # the first non-NUL byte after name1's NUL, which has to be the
        # meta's first byte
        meta = b'\x80\x00\x00\x00\x00\x00\x00' + bytes([len(nb)])
        rec += meta + nb + b'\x00'
    return bytes(rec)


LONG_NAME = 'A Rather Long File Name That Exceeds Thirty One Bytes For Sure'


def _catalog(*records: bytes) -> bytes:
    return _header() + b''.join(records)


def test_parse_generated_multi_record_catalog():
    body = _catalog(
        _disk_record('Synth Disk', 4),
        _item('A Folder', typ=b'\x00\x00\xff\xff', creator=b'FNDR',
              tail=b'\x00' * 5, items=2, descendants=2),
        _item('inner1.txt', depth=2),
        _item('inner2.txt', depth=2),
        _item('after.txt'),
    )
    recs = parse_dtc(body)
    assert [(r.kind, r.name, r.depth) for r in recs] == [
        ('disk', 'Synth Disk', 0),
        ('folder', 'A Folder', 1),
        ('file', 'inner1.txt', 2),
        ('file', 'inner2.txt', 2),
        ('file', 'after.txt', 1),
    ]
    folder = recs[1]
    assert (folder.items, folder.descendants) == (2, 2)
    assert folder.type == b'\x00\x00\xff\xff'
    assert folder.tail == b'\x00' * 5


def test_write_dtc_roundtrip():
    items = [
        Item(kind='disk', name='First Disk', created=1000000, modified=2000000),
        Item(kind='folder', name='Docs', depth=1, items=1, descendants=1),
        Item(kind='file', name='letter.txt', depth=2, size=11,
             type=b'TEXT', creator=b'ttxt', created=1100000, modified=2100000),
        Item(kind='file', name='root.txt', depth=1, size=5,
             type=b'TEXT', creator=b'ttxt'),
        Item(kind='disk', name='Second Disk', created=3000000, modified=4000000),
        Item(kind='file', name='other.bin', depth=1, size=42,
             type=b'DMdf', creator=b'DskM'),
    ]
    recs = parse_dtc(write_dtc(items))
    disks = [r for r in recs if r.kind == 'disk']
    assert [d.name for d in disks] == ['First Disk', 'Second Disk']
    assert disks[0].items == 3
    assert disks[1].items == 1
    assert [r.name for r in recs] == [
        'First Disk', 'Docs', 'letter.txt', 'root.txt',
        'Second Disk', 'other.bin',
    ]
    by_name = {r.name: r for r in recs}
    assert by_name['Docs'].kind == 'folder'
    assert by_name['letter.txt'].type == b'TEXT'
    assert (by_name['letter.txt'].created, by_name['letter.txt'].modified) == (1100000, 2100000)


def test_write_dtc_defaults_to_a_disk():
    recs = parse_dtc(write_dtc([Item(kind='file', name='solo.txt', size=3, depth=1)]))
    assert [r.kind for r in recs] == ['disk', 'file']
    assert recs[0].name == 'Untitled Disk'


def test_long_name_record_with_full_name():
    body = _catalog(
        _disk_record('Synth Disk', 3),
        _long_item(LONG_NAME),
        _item('second.txt'),
        _item('third.txt'),
    )
    recs = parse_dtc(body)
    assert recs[1].name == LONG_NAME
    assert [r.name for r in recs] == ['Synth Disk', LONG_NAME, 'second.txt', 'third.txt']


def test_long_name_record_with_zero_padded_meta():
    # The meta is preceded by zero padding: the parser has to skip the
    # pad and validate the chained record to pick the right meta
    # position, or the real name is lost.
    body = _catalog(
        _disk_record('Synth Disk', 3),
        _long_item(LONG_NAME, pad=12),
        _item('second.txt'),
        _item('third.txt'),
    )
    recs = parse_dtc(body)
    assert recs[1].name == LONG_NAME
    assert [r.name for r in recs] == ['Synth Disk', LONG_NAME, 'second.txt', 'third.txt']


def test_long_name_record_flen_zero():
    body = _catalog(
        _disk_record('Synth Disk', 3),
        _long_item('Short Synth Name', flen_zero=True),
        _item('b.txt'),
        _item('c.txt'),
    )
    recs = parse_dtc(body)
    assert [r.name for r in recs] == ['Synth Disk', 'Short Synth Name', 'b.txt', 'c.txt']


def test_embedded_family_types_parse():
    # AppleWorks stationery embedded parts and signature items carry
    # binary type codes the printable-ASCII check would reject.
    wide = 5  # embedded no-type family uses the 5-byte tail
    cases = [
        (b'\x00\x1a\x1e\x34', b'jedi', 1),            # AppleWorks part, binary type
        (b'\x14\x00\x07\x6a', b'edi\xa9', 1),         # boundary-split 'jedi' + 0xa9
        (b'\x00\x00\x52\x33', b'\x58\x50\x00\x00', 1),  # Signature pair
        (b'\x00\x00\x2a\x2a', b'\x2a\x2a\x00\x00', 1),  # Signature pair
        (b'\x00\x1a\x00\x00', b'MACS', wide),        # generic embedded no-type family
        (b'\x00\x00\x00\x00', b'pdos', wide),        # all-zero type, PC Exchange
    ]
    for typ, creator, tail_len in cases:
        tail = b'\x00' * tail_len
        body = _catalog(
            _disk_record('Synth Disk', 2),
            _item('one.bin', typ=typ, creator=creator, tail=tail),
            _item('two.bin', typ=typ, creator=creator, tail=tail),
        )
        recs = parse_dtc(body)
        files = [r for r in recs if r.kind == 'file']
        assert [r.name for r in files] == ['one.bin', 'two.bin'], typ.hex()
        assert files[0].type == typ


def test_disk_record_requires_name_echo():
    # No name echo at +0x19: the record is not a disk record, and the
    # parser finds no catalog here at all.
    body = _catalog(
        _disk_record('Synth Disk', 2, echo=False),
        _item('alpha.txt'),
        _item('beta.txt'),
    )
    assert parse_dtc(body) == []


def test_junk_before_first_item_is_skipped():
    phantom = _item('Ghost')          # parses, but its successor doesn't
    body = _catalog(
        _disk_record('Synth Disk', 2),
        phantom + b'\x00' * 80,
        _item('alpha.txt'),
        _item('beta.txt'),
    )
    recs = parse_dtc(body)
    assert [r.name for r in recs] == ['Synth Disk', 'alpha.txt', 'beta.txt']


def test_unprintable_type_is_rejected():
    assert not _type_ok(b'\x01\x02\x03\x04', b'MACS')
    assert not _type_ok(b'\x7f\x01\x02\x03', b'MACS')


def _v1_catalog(disk_name: str, items: list) -> bytes:
    h = bytearray(0x37e)
    h[0:8] = b'disktrak'
    struct.pack_into('>I', h, 0x0C, 1)
    
    # Volume record (76 bytes)
    vol = bytearray(76)
    struct.pack_into('>H', vol, 0, 0xa000)
    vol[2:6] = b'\xff\xff\xff\xff'
    struct.pack_into('>II', vol, 0x12, 2700000000, 2710000000)
    struct.pack_into('>H', vol, 0x1a, 0)
    struct.pack_into('>I', vol, 0x1c, len(items)) # immediate children
    struct.pack_into('>I', vol, 0x20, len(items)) # descendants
    nb = disk_name.encode('mac_roman')
    vol[0x24] = len(nb)
    vol[0x25:0x25+len(nb)] = nb
    
    body = bytearray(h) + bytes(vol)
    for it in items:
        # 36-byte front + pascal name
        fb = bytearray(36)
        is_folder = it.get('folder', False)
        if is_folder:
            struct.pack_into('>H', fb, 0, 0x8000)
            fb[2:6] = b'\x00\x00\xff\xff'
            fb[6:10] = b'\x00\x00\x010'
        else:
            fb[2:6] = it.get('type', b'TEXT')
            fb[6:10] = it.get('creator', b'ttxt')
        struct.pack_into('>I', fb, 10, it.get('size', 100))
        struct.pack_into('>II', fb, 18, 1000000, 2000000)
        struct.pack_into('>HII', fb, 26, it.get('depth', 1), it.get('items', 0), it.get('descendants', 0))
        inb = it['name'].encode('mac_roman')
        fb.append(len(inb))
        body += bytes(fb) + inb
    return bytes(body)


def test_parse_v1_generated_catalog():
    body = _v1_catalog('v1 Disk', [
        dict(name='folder1', folder=True, depth=1, items=1, descendants=1),
        dict(name='file1.txt', depth=2, size=42, type=b'TEXT', creator=b'ttxt'),
    ])
    recs = parse_dtc(body)
    assert [(r.kind, r.name, r.depth) for r in recs] == [
        ('disk', 'v1 Disk', 0),
        ('folder', 'folder1', 1),
        ('file', 'file1.txt', 2),
    ]
    assert recs[0].items == 2
    assert recs[2].size == 42
    assert recs[2].type == b'TEXT'


def test_long_name_writer_roundtrip():
    long_name = 'A Rather Long Rock Ridge Name That Does Not Fit In Sixty Three Bytes At All'
    items = [
        Item(kind='disk', name='Synth Disk', created=1000000, modified=2000000),
        Item(kind='file', name=long_name, depth=1, size=5,
             type=b'TEXT', creator=b'ttxt'),
        Item(kind='file', name='after.txt', depth=1, size=3),
    ]
    recs = parse_dtc(write_dtc(items))
    assert [r.name for r in recs] == ['Synth Disk', long_name, 'after.txt']


def test_colon_in_a_name_roundtrips_via_long_name_variant():
    items = [
        Item(kind='disk', name='Synth Disk', created=1000000, modified=2000000),
        Item(kind='file', name='a:b.txt', depth=1, size=5,
             type=b'TEXT', creator=b'ttxt'),
        Item(kind='file', name='after.txt', depth=1, size=3),
    ]
    recs = parse_dtc(write_dtc(items))
    assert [r.name for r in recs] == ['Synth Disk', 'a:b.txt', 'after.txt']


def test_a_name_over_255_bytes_is_refused_by_name():
    items = [Item(kind='disk', name='Synth Disk', created=1000000, modified=2000000),
             Item(kind='file', name='界' * 90, depth=1, size=5)]
    import pytest as _pytest
    with _pytest.raises(ValueError, match='too long for a Disk Tracker catalog'):
        write_dtc(items)


def test_a_long_disk_name_is_truncated_at_a_codepoint_boundary():
    items = [Item(kind='disk', name='D' * 100, created=1000000, modified=2000000),
             Item(kind='file', name='f.txt', depth=1, size=3)]
    recs = parse_dtc(write_dtc(items))
    assert recs[0].kind == 'disk'
    assert recs[0].name == 'D' * (0x52 - 0x19)


def test_utf8_prefix_cuts_at_a_codepoint_boundary():
    from disktracker.writer import _utf8_prefix
    assert _utf8_prefix('界' * 40, 63) == '界'.encode('utf-8') * 21   # 63 bytes = 21 CJK chars
