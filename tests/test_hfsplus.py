"""HFS Plus disk images ('H+' at offset 1024 of the volume), read-only:
the volume's own names, its files' forks left on the disk until they're
wanted, the journal and other private files hidden, and an image wrapped
in an Apple partition map, the way old Apple CD-ROMs ship."""

import struct
import unicodedata

import machfs
import pytest

from hfsplus_build import hfsplus_image

from macrt.hfsplus import read_hfsplus, _spans
from macrt.macfile import is_disk_image, load_volume, volume_files

ENTRIES = [
    {"kind": "folder", "name": "Games"},
    {"kind": "file", "name": "TestApp", "parent": "Games", "type": b"APPL",
     "creator": b"TEST", "data": b"hello world" * 500, "rsrc": b"\x00\x01" * 100,
     "fdflags": 0x0400},
    {"kind": "file", "name": "Plain", "data": b"plain", "rsrc": b""},
]


def partitioned(volume, start):
    """A CD-like hard disk image: driver descriptor, partition map, the volume."""
    def entry(first, blocks, kind):
        e = bytearray(512)
        e[0:2] = b"PM"
        struct.pack_into(">III", e, 4, 3, first, blocks)
        e[48:48 + len(kind)] = kind
        return bytes(e)
    blocks = len(volume) // 512
    ddm = b"ER" + struct.pack(">HI", 512, start + blocks)
    head = (ddm.ljust(512, b"\0") + entry(1, 63, b"Apple_partition_map")
            + entry(start, blocks, b"Apple_HFS"))
    return head.ljust(512 * start, b"\0") + volume


def test_hfsplus_volume_round_trip():
    img = hfsplus_image("TestDisk", ENTRIES, created=0xA0000100)
    assert is_disk_image(img)
    vol = load_volume(img)
    assert vol.name == "TestDisk"
    assert vol.crdate == 0xA0000100
    games = vol["Games"]
    app = games["TestApp"]
    assert (app.type, app.creator) == (b"APPL", b"TEST")
    assert app.flags & 0x0400                      # its Finder custom icon bit
    assert app.data == b"hello world" * 500        # eleven blocks, read on demand
    assert app.rsrc == b"\x00\x01" * 100
    assert vol["Plain"].data == b"plain" and vol["Plain"].rsrc == b""
    keys = {f.key for f in volume_files(vol)}
    assert "TestDisk:Games:TestApp" in keys


def test_hfsplus_dates_and_locked_file():
    entries = [
        {"kind": "file", "name": "Locked", "data": b"x", "rsrc": b"", "locked": True},
    ]
    vol = load_volume(hfsplus_image("Dates", entries, created=0xA1112222))
    node = vol["Locked"]
    assert node.locked
    assert node.crdate == 0xA1112222 == node.mddate
    assert vol.crdate == 0xA1112222


def test_hfsplus_invisible_entries_are_hidden():
    entries = [
        {"kind": "folder", "name": "Visible"},
        {"kind": "folder", "name": ".Trashes", "frflags": 0x4000},
        {"kind": "file", "name": "Inside", "parent": ".Trashes", "data": b"t"},
        {"kind": "file", "name": ".DS_Store", "data": b"ds", "fdflags": 0x4000},
        {"kind": "file", "name": "Normal", "data": b"n"},
    ]
    vol = load_volume(hfsplus_image("Hidden", entries))
    assert sorted(vol.keys()) == ["Normal", "Visible"]


def test_hfsplus_names_are_composed_for_display():
    decomposed = unicodedata.normalize("NFD", "Café")
    entries = [{"kind": "file", "name": decomposed, "data": b"x", "rsrc": b""}]
    vol = load_volume(hfsplus_image("Names", entries))
    assert list(vol.keys()) == [unicodedata.normalize("NFC", decomposed)]


def test_hfsplus_catalog_spanning_leaf_nodes():
    entries = [{"kind": "folder", "name": "Sub"}]
    entries += [{"kind": "file", "name": f"A file with a rather long name {n:02d}",
                 "parent": "Sub", "data": bytes([n]), "rsrc": b""}
                for n in range(60)]
    vol = load_volume(hfsplus_image("Many", entries))
    assert len(vol["Sub"].keys()) == 60
    assert vol["Sub"]["A file with a rather long name 42"].data == b"*"


def test_hfsplus_wrapped_in_an_apple_partition_map():
    raw = hfsplus_image("Wrapped", ENTRIES)
    img = partitioned(raw, start=64)
    assert is_disk_image(img)
    vol = load_volume(img)
    assert vol.name == "Wrapped"
    assert vol["Games"]["TestApp"].data == b"hello world" * 500


def test_hfsplus_empty_volume():
    vol = load_volume(hfsplus_image("Empty", []))
    assert vol.name == "Empty" and len(vol) == 0


def test_hfsplus_damaged_images_say_so():
    img = hfsplus_image("TestDisk", ENTRIES)
    damaged = bytearray(img)
    damaged[1024:1026] = b"XX"
    with pytest.raises(ValueError, match="signature"):
        read_hfsplus(damaged)
    damaged = bytearray(img)
    damaged[1026:1028] = b"\x00\x03"                   # HFS+ version 3
    with pytest.raises(ValueError, match="version"):
        read_hfsplus(damaged)
    damaged = bytearray(img)
    damaged[1024 + 44:1024 + 48] = b"\x00\x00\x00\x00"  # no blocks
    with pytest.raises(ValueError, match="no blocks"):
        read_hfsplus(damaged)
    with pytest.raises(ValueError):
        read_hfsplus(img[:1536 + 100])                  # catalog cut short


def test_hfsplus_a_catalog_whose_leaves_link_in_a_circle():
    entries = [{"kind": "folder", "name": "Sub"}]
    entries += [{"kind": "file", "name": f"file number {n:02d} with a wordy name",
                 "parent": "Sub", "data": b"x", "rsrc": b""} for n in range(40)]
    img = bytearray(hfsplus_image("Circle", entries))
    catalog = _catalog_extent(img)
    # The catalog's first leaf node lives right after its header node.
    struct.pack_into(">I", img, catalog + 512 * 1, 1)    # its forward link: itself
    with pytest.raises(ValueError, match="circle"):
        load_volume(bytes(img))


def _catalog_extent(img):
    base = 1024
    block_size, = struct.unpack_from(">I", img, base + 40)
    start, = struct.unpack_from(">I", img, base + 272 + 16)
    return start * block_size


def test_spans_through_the_extents_overflow_file():
    overflow = {(0, 42, 2): [(20, 1)]}
    assert _spans(512, 3 * 512, [(10, 2)], overflow, 42, 0) == [
        (10 * 512, 12 * 512), (20 * 512, 21 * 512)]
    with pytest.raises(ValueError, match="extents end"):
        _spans(512, 3 * 512, [(10, 1)], overflow, 42, 0)
    # An inline run that covers the fork never asks the overflow file.
    assert _spans(512, 2 * 512, [(10, 2)], {}, 42, 0) == [(10 * 512, 12 * 512)]


def test_hfsplus_lists_from_the_catalog_only(tmp_path):
    # The catalog B-tree locates each fork; the forks are read the
    # first time they are wanted. The HFS+ catalog grades nothing,
    # so a mangled body reads back mangled.
    import struct as _struct
    entries = [
        {"kind": "file", "name": "Victim", "data": b"victim-body" * 300,
         "rsrc": b"", "type": b"TEXT", "creator": b"ttxt"},
    ]
    img = bytearray(hfsplus_image("Lazily", entries))
    at = img.find(b"victim-body")
    assert at > 0
    img[at] ^= 0xFF
    vol = load_volume(bytes(img))
    files = {f.name: f for f in volume_files(vol)}
    victim = files["Victim"]
    assert victim._node._read == {}
    assert victim.data[:11] != b"victim-body"
