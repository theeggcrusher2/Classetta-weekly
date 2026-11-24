"""Tests for disk image batch indexing into .dtc catalogs."""

import struct
from datetime import datetime, timezone
from pathlib import Path

import machfs
import pytest

from disktracker import build_dtc, index_image, index_images, parse_dtc
from disktracker.filer_adapter import DiskTrackerVolume

MAC_EPOCH = 2082844800


@pytest.fixture
def hfs_image(tmp_path: Path) -> Path:
    """A small HFS volume: a file at the root, a folder with one file."""
    v = machfs.Volume()
    v.name = 'TestDisk'
    f = machfs.File()
    f.data = b'hello'
    f.type, f.creator = b'TEXT', b'ttxt'
    f.crdate, f.mddate = 1000000, 2000000
    v['readme.txt'] = f
    sub = machfs.Folder()
    v['docs'] = sub
    g = machfs.File()
    g.data = b'x' * 5000
    g.type, g.creator = b'DMdf', b'DskM'
    g.crdate, g.mddate = 1500000, 2500000
    sub['inner.bin'] = g
    path = tmp_path / 'test.hfs'
    path.write_bytes(v.write(800 * 1024))
    return path


DATE = (89, 4, 25, 13, 31, 0, 0)      # 1989-04-25 13:31


def _dir_rec(img: bytearray, at: int, lba: int, size: int, is_dir: bool,
             name: bytes) -> int:
    """One ISO 9660 directory record's bytes; how many it took."""
    nlen = len(name)
    reclen = 33 + nlen
    reclen += reclen & 1               # padded to an even length
    rec = bytearray(reclen)
    rec[0] = reclen
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
    img[at:at + reclen] = rec
    return reclen


@pytest.fixture
def iso_image(tmp_path: Path) -> Path:
    """A minimal ISO 9660 volume: a root file and a folder with one
    file, sectors 16 (PVD), 17 (root), 18 (subdir), 19-20 (data)."""
    sectors = 21
    img = bytearray(sectors * 2048)
    pvd_at = 16 * 2048
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
    at = 17 * 2048
    at += _dir_rec(img, at, 17, 2048, True, b'.')
    at += _dir_rec(img, at, 17, 2048, True, b'..')
    at += _dir_rec(img, at, 19, 11, False, b'HELLO.TXT;1')
    at += _dir_rec(img, at, 18, 2048, True, b'SUBDIR')
    at = 18 * 2048
    at += _dir_rec(img, at, 18, 2048, True, b'.')
    at += _dir_rec(img, at, 17, 2048, True, b'..')
    at += _dir_rec(img, at, 20, 512, False, b'INNER.BIN;1')
    img[19 * 2048:19 * 2048 + 11] = b'hello world'
    img[20 * 2048:20 * 2048 + 512] = b'x' * 512
    path = tmp_path / 'test.iso'
    path.write_bytes(bytes(img))
    return path


def test_index_hfs_image(hfs_image: Path):
    items = index_image(hfs_image)
    disk = items[0]
    assert disk.kind == 'disk'
    assert disk.name == 'TestDisk'
    assert disk.items == len(items) - 1
    by_name = {i.name: i for i in items[1:]}
    readme = by_name['readme.txt']
    assert (readme.kind, readme.depth, readme.size) == ('file', 1, 5)
    assert (readme.type, readme.creator) == (b'TEXT', b'ttxt')
    assert (readme.created, readme.modified) == (1000000, 2000000)
    docs = by_name['docs']
    assert (docs.kind, docs.depth) == ('folder', 1)
    assert (docs.items, docs.descendants) == (1, 1)
    assert docs.size == 5000            # the files below it, summed
    inner = by_name['inner.bin']
    assert (inner.kind, inner.depth, inner.size) == ('file', 2, 5000)
    assert (inner.created, inner.modified) == (1500000, 2500000)


def test_build_dtc_roundtrip(hfs_image: Path):
    recs = parse_dtc(build_dtc([hfs_image]))
    assert recs[0].kind == 'disk'
    assert recs[0].name == 'TestDisk'
    assert recs[0].items == len(recs) - 1
    by_name = {r.name: r for r in recs[1:]}
    assert by_name['docs'].kind == 'folder'
    assert by_name['docs'].depth == 1
    assert by_name['inner.bin'].depth == 2
    assert by_name['readme.txt'].size == 5
    assert by_name['readme.txt'].type == b'TEXT'


def test_index_images_batches_two_disks(hfs_image: Path, iso_image: Path):
    recs = parse_dtc(build_dtc([hfs_image, iso_image]))
    at = [i for i, r in enumerate(recs) if r.kind == 'disk']
    assert len(at) == 2
    first, second = recs[at[0]], recs[at[1]]
    assert (first.name, second.name) == ('TestDisk', 'TESTISO')
    # Each disk's records follow it, one block per image, depths
    # restarting under each disk
    block1, block2 = recs[at[0] + 1:at[1]], recs[at[1] + 1:]
    assert len(block1) == first.items
    assert len(block2) == second.items
    assert all(r.depth >= 1 for r in block1 + block2)
    assert {'readme.txt', 'docs'} <= {r.name for r in block1}
    assert {'HELLO.TXT', 'SUBDIR', 'INNER.BIN'} <= {r.name for r in block2}


def test_index_iso_image(iso_image: Path):
    items = index_image(iso_image)
    disk = items[0]
    assert (disk.kind, disk.name) == ('disk', 'TESTISO')
    want = datetime(1989, 4, 25, 13, 31, tzinfo=timezone.utc)
    assert disk.created == int(want.timestamp()) + MAC_EPOCH
    by_name = {i.name: i for i in items[1:]}
    hello = by_name['HELLO.TXT']        # the ';1' version suffix is gone
    assert (hello.kind, hello.depth, hello.size) == ('file', 1, 11)
    subdir = by_name['SUBDIR']
    assert (subdir.kind, subdir.depth) == ('folder', 1)
    assert (subdir.items, subdir.descendants, subdir.size) == (1, 1, 512)
    inner = by_name['INNER.BIN']
    assert (inner.kind, inner.depth, inner.size) == ('file', 2, 512)
    # ISO files have no Finder type of their own
    assert (inner.type, inner.creator) == (b'????', b'????')


def test_load_volume_reads_iso_image(iso_image: Path):
    import machfs
    from macrt.macfile import load_volume
    vol = load_volume(iso_image)
    assert isinstance(vol, machfs.Volume)
    assert vol.name == 'TESTISO'
    hello = vol['HELLO.TXT']
    assert isinstance(hello, machfs.File)
    assert hello.data == b'hello world'
    inner = vol['SUBDIR']['INNER.BIN']
    assert inner.data == b'x' * 512


def test_an_unsupported_image_is_refused(tmp_path: Path):
    path = tmp_path / 'junk.bin'
    path.write_bytes(b'not a disk image at all' * 100)
    with pytest.raises(ValueError, match='unsupported disk image'):
        index_image(path)


def test_an_mfs_volume_is_refused(tmp_path: Path):
    data = bytearray(4096)
    data[1024:1026] = b'\xd2\xd7'       # MFS's signature in its MDB
    path = tmp_path / 'mfs.dsk'
    path.write_bytes(bytes(data))
    with pytest.raises(ValueError, match='MFS volumes are not supported'):
        index_image(path)


def test_a_generated_catalog_opens_in_filer(hfs_image: Path, tmp_path: Path):
    out = tmp_path / 'catalog.dtc'
    out.write_bytes(build_dtc([hfs_image]))
    volume = DiskTrackerVolume(out)
    # The root lists only the indexed disks; the disk's contents are inside
    disk = volume['TestDisk']
    assert set(disk.keys()) >= {'docs', 'readme.txt'}

@pytest.fixture
def big_hfs_image(tmp_path: Path) -> Path:
    """A generated ~300-file HFS volume: ten folders of thirty files."""
    v = machfs.Volume()
    v.name = 'BigDisk'
    for d in range(10):
        folder = machfs.Folder()
        for i in range(30):
            f = machfs.File()
            f.data = b'file %d %d\n' % (d, i)
            f.type, f.creator = b'TEXT', b'ttxt'
            f.crdate, f.mddate = 1000000 + i, 2000000 + i
            folder[f'file_{i:03d}.txt'] = f
        v[f'folder_{d:02d}'] = folder
    path = tmp_path / 'big.hfs'
    path.write_bytes(v.write(8 * 1024 * 1024))
    return path


def test_index_a_large_generated_image(big_hfs_image: Path):
    """Index a large generated image and check the build -> parse roundtrip."""
    items = index_image(big_hfs_image)
    assert len(items) > 1
    disk = items[0]
    assert disk.kind == 'disk'
    assert disk.name == 'BigDisk'
    assert disk.items == len(items) - 1
    # Check that we can successfully write a catalog from this image
    catalog_bytes = build_dtc([big_hfs_image])
    assert len(catalog_bytes) > 0
    # Verify round-trip by parsing the catalog
    parsed = parse_dtc(catalog_bytes)
    assert len(parsed) == len(items)


# -- HFS Plus -----------------------------------------------------------------
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / 'tests'))
from hfsplus_build import hfsplus_image as _hfsplus_image


@pytest.fixture
def hfsplus_file(tmp_path: Path) -> Path:
    """A small HFS Plus volume: a file at the root, a folder with one file."""
    entries = [
        {'kind': 'folder', 'name': 'docs'},
        {'kind': 'file', 'name': 'inner.bin', 'parent': 'docs', 'type': b'DMdf',
         'creator': b'DskM', 'data': b'x' * 5000},
        {'kind': 'file', 'name': 'readme.txt', 'type': b'TEXT', 'creator': b'ttxt',
         'data': b'hello', 'rsrc': b''},
    ]
    path = tmp_path / 'test.hfs+'
    path.write_bytes(_hfsplus_image('TestPlus', entries, created=1000000))
    return path


def test_index_hfsplus_image(hfsplus_file: Path):
    items = index_image(hfsplus_file)
    disk = items[0]
    assert (disk.kind, disk.name) == ('disk', 'TestPlus')
    assert disk.items == len(items) - 1
    by_name = {i.name: i for i in items[1:]}
    readme = by_name['readme.txt']
    assert (readme.kind, readme.depth, readme.size) == ('file', 1, 5)
    assert (readme.type, readme.creator) == (b'TEXT', b'ttxt')
    docs = by_name['docs']
    assert (docs.kind, docs.depth, docs.items, docs.descendants) == ('folder', 1, 1, 1)
    assert docs.size == 5000
    inner = by_name['inner.bin']
    assert (inner.kind, inner.depth, inner.size) == ('file', 2, 5000)
    assert (inner.type, inner.creator) == (b'DMdf', b'DskM')


def test_index_hfsplus_wrapped_in_a_partition_map(tmp_path: Path):
    def partitioned(volume, start):
        def entry(first, blocks, kind):
            e = bytearray(512)
            e[0:2] = b'PM'
            struct.pack_into('>III', e, 4, 3, first, blocks)
            e[48:48 + len(kind)] = kind
            return bytes(e)
        blocks = len(volume) // 512
        ddm = b'ER' + struct.pack('>HI', 512, start + blocks)
        head = (ddm.ljust(512, b'\0') + entry(1, 63, b'Apple_partition_map')
                + entry(start, blocks, b'Apple_HFS'))
        return head.ljust(512 * start, b'\0') + volume

    raw = _hfsplus_image('WrappedPlus', [
        {'kind': 'file', 'name': 'data.bin', 'type': b'BINA',
         'creator': b'TEST', 'data': b'w' * 300},
    ])
    path = tmp_path / 'wrapped.img'
    path.write_bytes(partitioned(raw, start=32))
    items = index_image(path)
    assert items[0].name == 'WrappedPlus'
    assert [i.name for i in items[1:]] == ['data.bin']


def test_index_hfsplus_invisible_entries_are_left_out(tmp_path: Path):
    entries = [
        {'kind': 'file', 'name': 'Normal', 'data': b'n'},
        {'kind': 'file', 'name': '.DS_Store', 'data': b's', 'fdflags': 0x4000},
        {'kind': 'folder', 'name': '.Trashes', 'frflags': 0x4000},
        {'kind': 'file', 'name': 'Hidden', 'parent': '.Trashes', 'data': b'h'},
    ]
    path = tmp_path / 'hidden.hfs+'
    path.write_bytes(_hfsplus_image('HiddenPlus', entries))
    items = index_image(path)
    assert [i.name for i in items[1:]] == ['Normal']


def test_index_hfsplus_catalog_roundtrip(hfsplus_file: Path):
    recs = parse_dtc(build_dtc([hfsplus_file]))
    assert recs[0].kind == 'disk' and recs[0].name == 'TestPlus'
    assert {r.name for r in recs} >= {'docs', 'inner.bin', 'readme.txt'}


def test_a_damaged_hfsplus_image_is_refused(tmp_path: Path):
    img = bytearray(_hfsplus_image('Broken', [
        {'kind': 'file', 'name': 'a.txt', 'data': b'x'},
    ]))
    img[1024 + 44:1024 + 48] = b'\x00\x00\x00\x00'    # no total blocks
    path = tmp_path / 'broken.hfs+'
    path.write_bytes(bytes(img))
    with pytest.raises(ValueError, match='no blocks|unsupported'):
        index_image(path)


def test_load_volume_reads_iso_data_on_demand(iso_image: Path):
    # The directory record locates the file's bytes; nothing is read
    # until the file is. The ISO volume grades nothing, so a mangled
    # body lists fine and reads back mangled (tar's precedent).
    import machfs
    from macrt.macfile import load_volume, volume_files

    raw = bytearray(iso_image.read_bytes())
    raw[19 * 2048] ^= 0xFF                     # HELLO.TXT's first byte
    iso_image.write_bytes(bytes(raw))
    vol = load_volume(iso_image)
    assert vol.name == 'TESTISO'
    files = {f.key: f for f in volume_files(vol)}
    hello = files['TESTISO:HELLO.TXT']
    assert hello._node._read == {}                   # nothing decoded for the listing
    assert hello.sizes() == (11, 0)
    assert hello.data[:1] != b'h'              # reads the mangled byte on demand
    inner = files['TESTISO:SUBDIR:INNER.BIN']
    assert inner._node._read == {}
    assert inner.data == b'x' * 512


def test_a_bin_with_a_cue_sidecar_lists_from_the_directory(iso_image: Path,
                                                           tmp_path: Path):
    # A BIN/CUE data disc is a raw MODE1/2048 image; the Filer opens
    # the .bin and lists it from its directory. The .cue sidecar is a
    # text manifest, not needed for reading.
    from macrt.macfile import open_container, volume_files

    raw = bytearray(iso_image.read_bytes())
    raw[19 * 2048] ^= 0xFF
    bin_path = tmp_path / 'disc.bin'
    bin_path.write_bytes(bytes(raw))
    (tmp_path / 'disc.cue').write_text(
        'FILE "disc.bin" BINARY\n'
        '  TRACK 01 MODE1/2048\n'
        '    INDEX 01 00:00:00\n')
    vol = open_container(bin_path)
    assert vol.name == 'TESTISO'
    files = {f.key: f for f in volume_files(vol)}
    hello = files['TESTISO:HELLO.TXT']
    assert hello._node._read == {}
    assert hello.data[:1] != b'h'

# -- The 4.6 invariants ------------------------------------------------------

def test_index_hfs_desktop_files_are_left_out(tmp_path: Path):
    v = machfs.Volume()
    v.name = 'DesktopDisk'
    for name in ('Desktop', 'Desktop DB', 'Desktop DF'):
        f = machfs.File()
        f.data = b'desktop'
        v[name] = f
    keep = machfs.File()
    keep.data = b'hello'
    keep.rsrc = b'world!'
    keep.crdate, keep.mddate = 1000000, 2000000
    v['readme.txt'] = keep
    path = tmp_path / 'desktop.hfs'
    path.write_bytes(v.write(800 * 1024))
    items = index_image(path)
    names = {i.name for i in items[1:]}
    assert 'Desktop' not in names
    assert 'Desktop DB' not in names
    assert 'Desktop DF' not in names
    readme = next(i for i in items if i.name == 'readme.txt')
    assert readme.size == len(b'hello') + len(b'world!')


def test_index_hfsplus_invariants(tmp_path: Path):
    import unicodedata
    from macrt.macfile import load_volume
    entries = [
        {'kind': 'file', 'name': 'readme.txt', 'type': b'TEXT', 'creator': b'ttxt',
         'data': b'hello', 'rsrc': b'1234567'},
        {'kind': 'file', 'name': 'Desktop', 'data': b'D', 'rsrc': b''},
        {'kind': 'file', 'name': 'Desktop DB', 'data': b'DB', 'rsrc': b''},
        {'kind': 'file', 'name': 'Desktop DF', 'data': b'DF', 'rsrc': b''},
        {'kind': 'file', 'name': '.DS_Store', 'data': b's', 'fdflags': 0x4000},
        {'kind': 'folder', 'name': '.Trashes', 'frflags': 0x4000},
        {'kind': 'file', 'name': 'Hidden', 'parent': '.Trashes', 'data': b'h'},
        {'kind': 'file', 'name': unicodedata.normalize('NFD', 'Café au lait.txt'),
         'data': b'x'},
        {'kind': 'file', 'name': '🎉 party.txt', 'data': b'y'},
    ]
    path = tmp_path / 'invariants.hfs+'
    path.write_bytes(_hfsplus_image('Invariants', entries))
    items = index_image(path)
    names = [i.name for i in items[1:]]
    for gone in ('Desktop', 'Desktop DB', 'Desktop DF', '.DS_Store', '.Trashes', 'Hidden'):
        assert gone not in names
    readme = next(i for i in items if i.name == 'readme.txt')
    assert readme.size == 5 + 7            # both forks, no fork read

    # The catalog's names are the ones the Filer lists: both views come
    # from the same reader, so NFD names, unencodable names and the
    # visibility rules all agree.
    vol = load_volume(path)
    def walk(node):
        out = []
        for n, child in node.items():
            out.append(n)
            if isinstance(child, machfs.Folder):
                out.extend(walk(child))
        return out
    from collections import Counter
    assert Counter(walk(vol)) == Counter(names)
