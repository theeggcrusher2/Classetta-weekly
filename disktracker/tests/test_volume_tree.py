"""Tests for the machfs Volume -> tree-of-dicts adapter (_volume_tree)."""

import sys
from pathlib import Path

import machfs
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / 'tests'))
from hfsplus_build import hfsplus_image as _hfsplus_image

from disktracker.index import _volume_tree
from macrt.hfsplus import read_hfsplus
from macrt.macfile import _read_hfs


@pytest.fixture
def hfs_image(tmp_path: Path) -> Path:
    """A small HFS volume: a file with forks at the root, a folder with
    one file, and the Finder's Desktop files (which the reader pops)."""
    v = machfs.Volume()
    v.name = 'TestDisk'
    v.crdate, v.mddate = 3000000, 4000000
    f = machfs.File()
    f.data = b'hello'                    # 5 bytes of data fork
    f.rsrc = bytearray(b'rsrc-bytes')    # 10 bytes of resource fork
    f.type, f.creator = b'TEXT', b'ttxt'
    f.crdate, f.mddate = 1000000, 2000000
    v['readme.txt'] = f
    sub = machfs.Folder()
    sub.crdate, sub.mddate = 1100000, 2100000
    g = machfs.File()
    g.data = b'x' * 5000
    g.rsrc = bytearray(b'')
    g.type, g.creator = b'DMdf', b'DskM'
    g.crdate, g.mddate = 1500000, 2500000
    sub['inner.bin'] = g
    v['docs'] = sub
    for desktop in ('Desktop', 'Desktop DB', 'Desktop DF'):
        d = machfs.File()
        d.data = b'desktop'
        v[desktop] = d
    path = tmp_path / 'test.hfs'
    path.write_bytes(v.write(800 * 1024))
    return path


def test_volume_tree_hfs(hfs_image: Path):
    vol = _read_hfs(hfs_image.read_bytes())
    name, crdate, mddate, root = _volume_tree(vol)

    assert name == 'TestDisk'
    assert (crdate, mddate) == (3000000, 4000000)

    assert root['kind'] == 'folder'
    assert root['name'] == 'TestDisk'
    assert (root['crdate'], root['mddate']) == (3000000, 4000000)

    by_name = {child['name']: child for child in root['children']}
    # the reader popped the Desktop files; the adapter skips nothing else
    assert 'Desktop' not in by_name
    assert 'Desktop DB' not in by_name
    assert 'Desktop DF' not in by_name
    assert set(by_name) == {'docs', 'readme.txt'}

    readme = by_name['readme.txt']
    assert readme['kind'] == 'file'
    assert readme['type'] == b'TEXT' and readme['creator'] == b'ttxt'
    assert readme['size'] == 5 + 10          # data + resource forks
    assert (readme['crdate'], readme['mddate']) == (1000000, 2000000)
    assert readme['children'] == []

    docs = by_name['docs']
    assert docs['kind'] == 'folder'
    assert (docs['crdate'], docs['mddate']) == (1100000, 2100000)
    inner = docs['children'][0]
    assert inner['name'] == 'inner.bin'
    assert inner['size'] == 5000
    assert (inner['crdate'], inner['mddate']) == (1500000, 2500000)


def test_volume_tree_hfs_sizes_do_not_read_forks(hfs_image: Path):
    vol = _read_hfs(hfs_image.read_bytes())
    _volume_tree(vol)
    # OnDisk reports lengths lazily; the adapter must not have read them
    assert vol['readme.txt']._read == {}
    assert vol['docs']['inner.bin']._read == {}


def test_volume_tree_hfsplus(tmp_path: Path):
    entries = [
        {'kind': 'folder', 'name': 'docs'},
        {'kind': 'file', 'name': 'inner.bin', 'parent': 'docs', 'type': b'DMdf',
         'creator': b'DskM', 'data': b'x' * 5000, 'rsrc': b'r' * 7},
        {'kind': 'file', 'name': 'readme.txt', 'type': b'TEXT', 'creator': b'ttxt',
         'data': b'hello', 'rsrc': b''},
        {'kind': 'file', 'name': 'Desktop', 'data': b'desktop'},
        {'kind': 'file', 'name': 'Desktop DB', 'data': b'db'},
        {'kind': 'file', 'name': 'Desktop DF', 'data': b'df'},
    ]
    path = tmp_path / 'test.hfs+'
    path.write_bytes(_hfsplus_image('TestPlus', entries, created=1000000))
    vol = read_hfsplus(path.read_bytes())
    name, crdate, mddate, root = _volume_tree(vol)

    assert name == 'TestPlus'
    assert root['kind'] == 'folder'
    assert (root['crdate'], root['mddate']) == (vol.crdate, vol.mddate)

    by_name = {child['name']: child for child in root['children']}
    # the reader popped the Desktop files; the adapter skips nothing else
    assert 'Desktop' not in by_name
    assert 'Desktop DB' not in by_name
    assert 'Desktop DF' not in by_name
    assert set(by_name) == {'docs', 'readme.txt'}

    readme = by_name['readme.txt']
    assert readme['kind'] == 'file'
    assert readme['type'] == b'TEXT' and readme['creator'] == b'ttxt'
    assert readme['size'] == 5
    assert (readme['crdate'], readme['mddate']) == \
        (vol['readme.txt'].crdate, vol['readme.txt'].mddate)

    inner = by_name['docs']['children'][0]
    assert inner['name'] == 'inner.bin'
    assert inner['size'] == 5000 + 7         # data + resource forks
    assert (inner['crdate'], inner['mddate']) == \
        (vol['docs']['inner.bin'].crdate, vol['docs']['inner.bin'].mddate)

    # OnDisk reports lengths lazily; the adapter must not have read the forks
    assert vol['readme.txt']._read == {}
    assert vol['docs']['inner.bin']._read == {}
