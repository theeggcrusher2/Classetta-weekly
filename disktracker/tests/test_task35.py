"""Task 3.5: the Filer and the Disk Tracker list the same volumes —
Rock Ridge and UDF — and the names roundtrip through a .dtc catalog."""

import sys
from collections import Counter
from pathlib import Path

import machfs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / 'tests'))
import test_rockridge  # noqa: E402
import test_udf  # noqa: E402

from disktracker import build_dtc, index_image, parse_dtc  # noqa: E402
from macrt.macfile import load_volume  # noqa: E402


def _walk(node):
    out = []
    for n, child in node.items():
        out.append(n)
        if isinstance(child, machfs.Folder):
            out.extend(_walk(child))
    return out


def test_rock_ridge_names_reach_the_tracker(tmp_path: Path):
    img = test_rockridge.rock_image.__wrapped__(tmp_path)
    items = index_image(img)
    names = [i.name for i in items[1:]]
    assert 'My File.txt' in names
    assert 'My Folder' in names
    assert any(n.startswith('A very long Rock Ridge file name') for n in names)
    # the Filer and the tracker list the same names
    assert Counter(_walk(load_volume(img))) == Counter(names)


def test_rock_ridge_names_roundtrip_through_dtc(tmp_path: Path):
    img = test_rockridge.rock_image.__wrapped__(tmp_path)
    names = [i.name for i in index_image(img)[1:]]
    recs = parse_dtc(build_dtc([img]))
    assert [r.name for r in recs[1:]] == names


def test_udf_volume_reaches_the_tracker(tmp_path: Path):
    img = tmp_path / 'disc.iso'
    img.write_bytes(bytes(test_udf.udf_image()))
    items = index_image(img)
    by_name = {i.name: i for i in items[1:]}
    assert by_name['Plain.txt'].size == len(test_udf.PLAIN_DATA)
    assert by_name['Subdir'].descendants == 1
    assert 'Inner.txt' in by_name
    names = [i.name for i in items[1:]]
    assert Counter(_walk(load_volume(img))) == Counter(names)


def test_udf_names_roundtrip_through_dtc(tmp_path: Path):
    img = tmp_path / 'disc.iso'
    img.write_bytes(bytes(test_udf.udf_image()))
    names = [i.name for i in index_image(img)[1:]]
    recs = parse_dtc(build_dtc([img]))
    assert [r.name for r in recs[1:]] == names
