"""Tests for Disk Tracker Filer integration."""

import pytest
from pathlib import Path
import machfs
from disktracker.filer_adapter import DiskTrackerVolume


def _image(tmp_path: Path, name: str, files: dict[str, bytes]) -> Path:
    v = machfs.Volume()
    v.name = name
    for fname, payload in files.items():
        f = machfs.File()
        f.data = payload
        f.type, f.creator = b'TEXT', b'ttxt'
        f.crdate = f.mddate = 4000000000
        v[fname] = f
    img = tmp_path / f'{name}.hfs'
    img.write_bytes(v.write(1024 * 1024))
    return img


def test_disktracker_volume_adapter(tmp_path: Path):
    from disktracker import build_dtc

    alpha = _image(tmp_path, 'Alpha CD', {'alpha_only.txt': b'alpha'})
    beta = _image(tmp_path, 'Beta CD', {'beta_only.txt': b'beta'})
    cat = tmp_path / 'catalog'
    cat.write_bytes(build_dtc([str(alpha), str(beta)]))

    v = DiskTrackerVolume(cat)
    assert v.name == 'catalog'
    # The root lists only the indexed disks, one folder per disk
    assert list(v.keys()) == ['Alpha CD', 'Beta CD']
    assert isinstance(v['Alpha CD'], machfs.Folder)
    assert 'alpha_only.txt' in v['Alpha CD']
    assert 'beta_only.txt' in v['Beta CD']
    # No item from inside a disk leaks to the root, nor to the other disk
    assert 'alpha_only.txt' not in list(v.keys())
    assert 'alpha_only.txt' not in v['Beta CD']
    assert 'beta_only.txt' not in v['Alpha CD']

def test_duplicate_disk_names_are_numbered(tmp_path: Path):
    import machfs
    from disktracker import build_dtc

    imgs = []
    for payload in (b'first-copy', b'second-copy'):
        v = machfs.Volume()
        v.name = 'SameName'
        f = machfs.File()
        f.data = payload
        f.type, f.creator = b'TEXT', b'ttxt'
        f.crdate = f.mddate = 4000000000
        v[payload.decode().replace('-', '_')] = f
        img = tmp_path / (payload.decode() + '.hfs')
        img.write_bytes(v.write(1024 * 1024))
        imgs.append(img)

    cat = tmp_path / 'dup.dtc'
    cat.write_bytes(build_dtc(imgs))
    v = DiskTrackerVolume(cat)
    assert list(v.keys()) == ['SameName', 'SameName (2)']
    assert 'first_copy' in v['SameName']
    assert 'second_copy' in v['SameName (2)']
    assert 'first_copy' not in v['SameName (2)']
