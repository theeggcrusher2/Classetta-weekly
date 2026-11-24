"""Rock Ridge (SUSP "NM") names in the one ISO 9660 reader.

The fixture is a generated ISO 9660 volume whose directory records
carry a System Use Area. The root directory's "." record carries an
"SP" signature (realism; the reader scans tolerantly and needs none),
and each file record carries its Rock Ridge name in "NM" entries.
"""

import struct
from pathlib import Path

import pytest

from macrt.iso9660 import ISO_SECTOR, _read_iso
from macrt.macfile import load_volume, volume_files

DATE = (89, 4, 25, 13, 31, 0, 0)      # 1989-04-25 13:31


def _entry(sig: bytes, payload: bytes, version: int = 1) -> bytes:
    """One SUSP entry: signature, length, version, body."""
    return sig + bytes([4 + len(payload), version]) + payload


def _sp_entry() -> bytes:
    """The SUSP signature SP entry; 0xBE 0xEF check bytes, skip 0."""
    return _entry(b'SP', b'\xbe\xef\x00')


def _nm_entry(name: bytes, flags: int = 0x00) -> bytes:
    return _entry(b'NM', bytes([flags]) + name)


def _dir_rec(img: bytearray, at: int, lba: int, size: int, is_dir: bool,
             name: bytes, sua: bytes = b'') -> int:
    """One ISO 9660 directory record's bytes; how many it took."""
    nlen = len(name)
    name_pad = 1 if nlen % 2 == 0 else 0
    rec = bytearray(33 + nlen + name_pad)   # the fixed part, zeroed
    rec[1] = 0
    struct.pack_into('>I', rec, 2, lba)
    struct.pack_into('<I', rec, 6, lba)
    struct.pack_into('>I', rec, 10, size)
    struct.pack_into('<I', rec, 14, size)
    rec[18:25] = bytes(DATE)
    rec[25] = 0x02 if is_dir else 0x00
    rec[28:30] = struct.pack('<H', 1)  # volume sequence number
    rec[30:32] = struct.pack('>H', 1)
    rec[32] = nlen
    rec[33:33 + nlen] = name
    rec += sua
    if len(rec) % 2:
        rec += b'\x00'                # records are even-length
    rec[0] = len(rec)
    img[at:at + len(rec)] = rec
    return len(rec)


@pytest.fixture
def rock_image(tmp_path: Path) -> Path:
    """A minimal Rock Ridge volume. Root holds: a file shown in caps
    on the ISO side but named "My File.txt" in Rock Ridge, a plain file
    with no NM entries, a folder "My Folder", a long-named file split
    across two NM entries, and the ISO_Joliet.TXT record whose NM chain
    carries a 150-character name."""
    sectors = 21
    img = bytearray(sectors * ISO_SECTOR)
    pvd_at = 16 * ISO_SECTOR
    img[pvd_at] = 1
    img[pvd_at + 1:pvd_at + 6] = b'CD001'
    img[pvd_at + 6] = 1
    img[pvd_at + 40:pvd_at + 72] = b'TESTISO'.ljust(32)
    struct.pack_into('>I', img, pvd_at + 80, sectors)
    struct.pack_into('<I', img, pvd_at + 84, sectors)
    struct.pack_into('>H', img, pvd_at + 128, 2048)
    struct.pack_into('<H', img, pvd_at + 130, 2048)
    img[pvd_at + 770:pvd_at + 787] = b'1989042513310000\x00'
    img[pvd_at + 787:pvd_at + 804] = b'1989042614450000\x00'
    _dir_rec(img, pvd_at + 147, 17, 2048, True, b'')   # the root record

    long_name = b'A very long Rock Ridge file name, over thirty-one characters, indeed.txt'
    assert len(long_name) > 31
    wide_name = bytes(('W' * 70) + ('x' * 80), 'ascii')  # 150 chars, one entry
    split_a, split_b = b'Split\x20' * 10, b'across' * 12   # 60 + 72 chars

    at = 17 * ISO_SECTOR
    at += _dir_rec(img, at, 17, 2048, True, b'.',
                   _sp_entry() + _nm_entry(b'.', 0x02))
    at += _dir_rec(img, at, 17, 2048, True, b'..',
                   _nm_entry(b'..', 0x04))
    at += _dir_rec(img, at, 19, 11, False, b'MYFILE.TXT;1',
                   _nm_entry(b'My File.txt'))
    at += _dir_rec(img, at, 19, 11, False, b'PLAIN.TXT;1')           # no NM
    at += _dir_rec(img, at, 18, 2048, True, b'SUBDIR',
                   _nm_entry(b'My Folder'))
    at += _dir_rec(img, at, 19, 11, False, b'LONG.TXT;1',
                   _nm_entry(long_name))
    at += _dir_rec(img, at, 19, 11, False, b'WIDE.TXT;1',
                   _nm_entry(wide_name))
    at += _dir_rec(img, at, 19, 11, False, b'SPLIT.TXT;1',
                   _nm_entry(split_a) + _nm_entry(split_b, 0x01))

    at = 18 * ISO_SECTOR
    at += _dir_rec(img, at, 18, 2048, True, b'.',
                   _nm_entry(b'.', 0x02))
    at += _dir_rec(img, at, 17, 2048, True, b'..',
                   _nm_entry(b'..', 0x04))
    at += _dir_rec(img, at, 20, 512, False, b'INNER.BIN;1',
                   _nm_entry(b'Inner File.bin'))
    img[19 * ISO_SECTOR:19 * ISO_SECTOR + 11] = b'hello world'
    img[20 * ISO_SECTOR:20 * ISO_SECTOR + 512] = b'x' * 512
    path = tmp_path / 'test.iso'
    path.write_bytes(bytes(img))
    return path


def _children(vol):
    return {c['name']: c for c in vol[3]['children']}


def test_rock_ridge_names_win_over_iso_names(rock_image: Path):
    vol = _read_iso(rock_image.read_bytes(), 'stem')
    kids = _children(vol)
    assert 'My File.txt' in kids
    assert 'MYFILE.TXT' not in kids
    assert kids['My File.txt']['kind'] == 'file'
    folder = kids['My Folder']
    assert folder['kind'] == 'folder'
    assert [c['name'] for c in folder['children']] == ['Inner File.bin']


def test_record_without_nm_keeps_iso_name(rock_image: Path):
    vol = _read_iso(rock_image.read_bytes(), 'stem')
    kids = _children(vol)
    assert 'PLAIN.TXT' in kids      # latin-1, ";1" stripped


def test_dot_and_dotdot_are_not_nm_named_children(rock_image: Path):
    vol = _read_iso(rock_image.read_bytes(), 'stem')
    kids = _children(vol)
    assert '.' not in kids and '..' not in kids
    folder = kids['My Folder']
    names = [c['name'] for c in folder['children']]
    assert '.' not in names and '..' not in names


def test_case_and_internal_spaces_survive(rock_image: Path):
    vol = _read_iso(rock_image.read_bytes(), 'stem')
    kids = _children(vol)
    assert kids['My File.txt']['size'] == 11


def test_long_name_round_trips(rock_image: Path):
    vol = _read_iso(rock_image.read_bytes(), 'stem')
    kids = _children(vol)
    long_name = 'A very long Rock Ridge file name, over thirty-one characters, indeed.txt'
    assert len(long_name) > 31
    assert long_name in kids
    wide = 'W' * 70 + 'x' * 80
    assert len(wide) == 150
    assert wide in kids


def test_nm_chain_split_across_entries_concatenates(rock_image: Path):
    vol = _read_iso(rock_image.read_bytes(), 'stem')
    kids = _children(vol)
    expected = (b'Split\x20' * 10 + b'across' * 12).decode('utf-8')
    assert expected in kids


def test_filer_visible_names_are_rock_ridge(rock_image: Path):
    vol = load_volume(rock_image)
    files = {f.key: f for f in volume_files(vol)}
    assert 'TESTISO:My File.txt' in files
    assert 'TESTISO:My Folder:Inner File.bin' in files
    assert 'TESTISO:PLAIN.TXT' in files
