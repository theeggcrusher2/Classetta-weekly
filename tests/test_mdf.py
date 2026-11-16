"""Tests for Alcohol 120% disc images: a .mdf sector image with its
.mds Media Descriptor sidecar.

Fixtures are written here with small spec-faithful writers that follow
Aaru's Alcohol120 reader (Structs.cs, Read.cs): little-endian, sessions
at an offset, 80-byte track blocks, a raw track stored with a 2352 or
2448-byte sector stride.
"""

import machfs
import pytest
import struct

from macrt import mdf
from macrt.macfile import image_kind, is_disk_image, load_volume
from macrt.macfile import MacFile
from macrt.mdf import MdfError

SECTOR = 2048

MODE_NO_DATA = 0x00
MODE_DVD = 0x02
MODE_AUDIO = 0xA9
MODE_1 = 0xAA
MODE_2 = 0xAB
MODE_2_F1 = 0xEC
MODE_2_F2 = 0xED
SUB_NONE = 0x00
SUB_INTERLEAVED = 0x08


def _volume_raw(name="Apple Disk") -> bytes:
    v = machfs.Volume()
    v.name = name
    f = machfs.File()
    f.type, f.creator, f.data = b"TEXT", b"ttxt", bytearray(b"mdf payload " * 100)
    v["readme.txt"] = f
    return v.write(size=1024 * 1024)


def _sectors(user: bytes, stride: int, user_off: int) -> bytes:
    """Slice the user bytes into raw CD sectors at the offset and stride."""
    out = bytearray()
    for at in range(0, len(user), SECTOR):
        sec = bytearray(stride)
        sec[user_off:user_off + SECTOR] = user[at:at + SECTOR]
        out += sec
    return bytes(out)


def _pair(tracks, *, medium=0x00, version=1):
    """The (mds, mdf) bytes for a list of real tracks, each a dict with
    point, mode, sub, startLba, pregap, sectors and payload (the strided
    sector bytes).  Marker blocks (A0/A1/A2) are added by the writer."""
    n = len(tracks) + 3
    session_off = 88
    track_off = session_off + 24
    at = track_off + n * 80
    for t in tracks:                       # 8 bytes each, in order
        t["extraOffset"] = at
        at += 8
    for t in tracks:                       # 16 bytes of footer, one only
        t["footerOffset"] = at
        at += 16
        break
    for t in tracks:
        t.setdefault("footerOffset", track_off)   # markers: no footer of their own
    name_off = at
    name = b"*.mdf\0"
    mds = bytearray(name_off + len(name))
    mds[0:16] = b"MEDIA DESCRIPTOR"
    mds[16], mds[17] = version, 5
    struct.pack_into("<HH", mds, 18, medium, 1)
    struct.pack_into("<I", mds, 80, session_off)          # sessionOffset
    struct.pack_into("<iiHBBHHII", mds, session_off, -150,
                     max(t["startLba"] + t["sectors"] for t in tracks),
                     1, n, 3, 1, max(t["point"] for t in tracks), 0, track_off)
    mdf = bytearray()
    at = track_off
    for p in (0xA0, 0xA1, 0xA2):
        _track_block(mds, at, MODE_NO_DATA, 0, 0x10, p, 0, 0, 0)
        at += 80
    for t in tracks:
        off = len(mdf)
        t["startOffset"] = off
        mdf += t["payload"]
        _track_block(mds, at, t["mode"], t["sub"], 0x14 if t["point"] < 100 else 0x10,
                     t["point"], t["startLba"], t["startOffset"],
                     t["extraOffset"], t["footerOffset"], t.get("ssize", 0))
        struct.pack_into("<II", mds, t["extraOffset"], t.get("pregap", 0), t["sectors"])
        at += 80
    struct.pack_into("<II", mds, tracks[0]["footerOffset"], name_off, 0)
    mds[name_off:name_off + len(name)] = name
    return bytes(mds), bytes(mdf)


def _track_block(d, at, mode, sub, adr, point, start_lba, start_offset,
                 extra_offset, footer_offset=None, ssize=0):
    d[at], d[at + 1], d[at + 2], d[at + 3], d[at + 4] = mode, sub, adr, 1, point
    struct.pack_into("<I", d, at + 12, extra_offset)
    struct.pack_into("<H", d, at + 16, ssize)
    # at+18..at+36: 18 unknown bytes, left zero
    struct.pack_into("<I", d, at + 36, start_lba)
    struct.pack_into("<Q", d, at + 40, start_offset)
    struct.pack_into("<I", d, at + 48, 1 if mode != MODE_NO_DATA else 0)
    if footer_offset is not None:
        struct.pack_into("<I", d, at + 52, footer_offset)


def _spec(point, lba, payload, mode=MODE_1, sub=SUB_INTERLEAVED, pregap=150):
    stride = 2352 + (96 if sub == SUB_INTERLEAVED else 0)
    return {"point": point, "mode": mode, "sub": sub, "startLba": lba,
            "pregap": pregap, "sectors": len(payload) // stride,
            "payload": payload, "ssize": stride}


def _cd(raw, mode=MODE_1, sub=SUB_INTERLEAVED, after=()):
    """A pair whose one data track carries raw, as a Mac volume's."""
    stride = 2352 + (96 if sub == SUB_INTERLEAVED else 0)
    user_off = 16 if mode in (MODE_1,) else 24
    payload = _sectors(raw, stride, user_off)
    specs = [_spec(1, 0, payload, mode, sub)]
    specs += list(after)
    return _pair(specs)


# --- detection ----------------------------------------------------------

def test_an_mds_is_detected_by_its_signature():
    mds_bytes, mdf_bytes = _cd(_volume_raw())
    assert mdf.is_mds(mds_bytes) and is_disk_image(mds_bytes)
    assert not mdf.is_mds(mdf_bytes)
    junk = bytearray(mds_bytes)
    junk[0:16] = b"NOT A DESCRIPTOR"
    assert not mdf.is_mds(junk)
    v2 = bytearray(mds_bytes)
    v2[16] = 2                                   # a version past 1
    assert not mdf.is_mds(v2)


def test_a_pair_is_named_by_kind(tmp_path):
    mds_bytes, mdf_bytes = _cd(_volume_raw())
    f = MacFile("CD.mds", mds_bytes, b"", b"????", b"????", 0, "CD.mds", None)
    assert image_kind(f) == "MDF/MDS CD image"
    g = MacFile("CD.mdf", mdf_bytes, b"", b"????", b"????", 0, "CD.mdf", None)
    assert image_kind(g) == "MDF/MDS CD image"


# --- browsing -----------------------------------------------------------

def test_a_pair_opens_as_a_volume_from_either_file(tmp_path):
    mds_bytes, mdf_bytes = _cd(_volume_raw())
    (tmp_path / "CD.mds").write_bytes(mds_bytes)
    (tmp_path / "CD.mdf").write_bytes(mdf_bytes)
    for name in ("CD.mds", "CD.mdf"):
        vol = load_volume(tmp_path / name)
        assert vol.name == "Apple Disk"
        assert vol["readme.txt"].data == b"mdf payload " * 100


def test_a_pair_without_interleaved_subchannel_opens(tmp_path):
    mds_bytes, mdf_bytes = _cd(_volume_raw(), sub=SUB_NONE)
    (tmp_path / "CD.mds").write_bytes(mds_bytes)
    (tmp_path / "CD.mdf").write_bytes(mdf_bytes)
    vol = load_volume(tmp_path / "CD.mdf")
    assert vol.name == "Apple Disk"


def test_a_mode_2_form_1_pair_opens(tmp_path):
    mds_bytes, mdf_bytes = _cd(_volume_raw(), mode=MODE_2_F1)
    (tmp_path / "CD.mds").write_bytes(mds_bytes)
    (tmp_path / "CD.mdf").write_bytes(mdf_bytes)
    vol = load_volume(tmp_path / "CD.mdf")
    assert vol.name == "Apple Disk"


def test_a_pair_with_audio_tracks_opens_its_data_track(tmp_path):
    raw = _volume_raw()
    stride = 2448
    audio = b"\0" * 10 * stride
    after = [_spec(2, len(raw) // SECTOR, audio, MODE_AUDIO, SUB_INTERLEAVED)]
    mds_bytes, mdf_bytes = _cd(raw, after=after)
    (tmp_path / "CD.mds").write_bytes(mds_bytes)
    (tmp_path / "CD.mdf").write_bytes(mdf_bytes)
    vol = load_volume(tmp_path / "CD.mds")
    assert vol.name == "Apple Disk"


# --- failure paths ------------------------------------------------------

def test_an_mdf_without_its_sidecar_says_so(tmp_path):
    mds_bytes, mdf_bytes = _cd(_volume_raw())
    (tmp_path / "CD.mdf").write_bytes(mdf_bytes)
    with pytest.raises(ValueError, match="it has no .mds sidecar beside it"):
        load_volume(tmp_path / "CD.mdf")


def test_an_mds_without_its_image_says_so(tmp_path):
    mds_bytes, mdf_bytes = _cd(_volume_raw())
    (tmp_path / "CD.mds").write_bytes(mds_bytes)
    with pytest.raises(ValueError, match="CD.mdf is missing"):
        load_volume(tmp_path / "CD.mds")


def test_a_junk_sidecar_says_so(tmp_path):
    (tmp_path / "CD.mds").write_bytes(b"junk" * 100)
    (tmp_path / "CD.mdf").write_bytes(b"\0" * 10000)
    with pytest.raises(ValueError, match="not an Alcohol .mds descriptor"):
        load_volume(tmp_path / "CD.mds")


def test_mds_bytes_must_open_by_path():
    mds_bytes, _ = _cd(_volume_raw())
    with pytest.raises(ValueError, match="open it by its path"):
        load_volume(mds_bytes)


def test_a_session_table_at_offset_zero_says_so(tmp_path):
    mds_bytes, mdf_bytes = _cd(_volume_raw())
    broken = bytearray(mds_bytes)
    struct.pack_into("<I", broken, 80, 0)                # sessionOffset
    (tmp_path / "CD.mds").write_bytes(broken)
    (tmp_path / "CD.mdf").write_bytes(mdf_bytes)
    with pytest.raises(MdfError, match="its session table is at offset 0"):
        load_volume(tmp_path / "CD.mds")


def test_a_dvd_track_is_refused_by_name(tmp_path):
    mds_bytes, mdf_bytes = _cd(_volume_raw(), mode=MODE_DVD)
    (tmp_path / "CD.mds").write_bytes(mds_bytes)
    (tmp_path / "CD.mdf").write_bytes(mdf_bytes)
    with pytest.raises(MdfError, match="it is a DVD, not a CD"):
        load_volume(tmp_path / "CD.mdf")


@pytest.mark.parametrize("mode, size", [(MODE_2, 2336), (MODE_2_F2, 2324)])
def test_a_mode_2_track_is_refused_by_name(tmp_path, mode, size):
    mds_bytes, mdf_bytes = _cd(_volume_raw(), mode=mode)
    (tmp_path / "CD.mds").write_bytes(mds_bytes)
    (tmp_path / "CD.mdf").write_bytes(mdf_bytes)
    with pytest.raises(MdfError, match=f"Mode 2 sector size {size}"):
        load_volume(tmp_path / "CD.mdf")


def test_two_data_tracks_are_refused(tmp_path):
    raw = _volume_raw()
    half = len(raw) // 2
    one = _sectors(raw[:half], 2448, 16)
    two = _sectors(raw[half:], 2448, 16)
    mds_bytes, mdf_bytes = _pair([_spec(1, 0, one), _spec(2, half // SECTOR, two)])
    (tmp_path / "CD.mds").write_bytes(mds_bytes)
    (tmp_path / "CD.mdf").write_bytes(mdf_bytes)
    with pytest.raises(MdfError, match="it carries 2 data tracks"):
        load_volume(tmp_path / "CD.mdf")


def test_an_audio_only_pair_is_refused(tmp_path):
    audio = b"\0" * 10 * 2352
    mds_bytes, mdf_bytes = _pair([_spec(1, 0, audio, MODE_AUDIO, SUB_NONE)])
    (tmp_path / "CD.mds").write_bytes(mds_bytes)
    (tmp_path / "CD.mdf").write_bytes(mdf_bytes)
    with pytest.raises(MdfError, match="holds no data track"):
        load_volume(tmp_path / "CD.mdf")


def test_a_truncated_mdf_says_so(tmp_path):
    mds_bytes, mdf_bytes = _cd(_volume_raw())
    (tmp_path / "CD.mds").write_bytes(mds_bytes)
    (tmp_path / "CD.mdf").write_bytes(mdf_bytes[:-4896])
    with pytest.raises(MdfError, match="its data track runs past the .mdf"):
        load_volume(tmp_path / "CD.mdf")


def test_a_track_starting_past_the_end_says_so(tmp_path):
    raw = _volume_raw()
    mds_bytes, mdf_bytes = _cd(raw)
    broken = bytearray(mds_bytes)
    # the first real track block starts at 88 + 24 + 3 * 80
    struct.pack_into("<Q", broken, 88 + 24 + 3 * 80 + 40, len(mdf_bytes) * 2)
    (tmp_path / "CD.mds").write_bytes(broken)
    (tmp_path / "CD.mdf").write_bytes(mdf_bytes)
    with pytest.raises(MdfError, match="its data track starts past its end"):
        load_volume(tmp_path / "CD.mdf")


def test_a_track_table_running_off_the_end_says_so(tmp_path):
    mds_bytes, mdf_bytes = _cd(_volume_raw())
    broken = bytearray(mds_bytes)
    struct.pack_into("<I", broken, 80, 10**6)             # sessionOffset
    (tmp_path / "CD.mds").write_bytes(broken)
    (tmp_path / "CD.mdf").write_bytes(mdf_bytes)
    with pytest.raises(MdfError, match="session table runs off its end"):
        load_volume(tmp_path / "CD.mds")


# --- a pair inside an archive -------------------------------------------

def test_a_pair_in_an_archive_opens_from_either_member():
    from macrt.macfile import ArchiveFolder, MacFile, image_volume
    mds_bytes, mdf_bytes = _cd(_volume_raw())
    mds = MacFile("ArchivePairFixture.mds", mds_bytes, b"", b"????", b"????",
                  0, "ArchivePairFixture.mds")
    mdf_file = MacFile("ArchivePairFixture.mdf", mdf_bytes, b"", b"????", b"????",
                       0, "ArchivePairFixture.mdf")
    ArchiveFolder([mds, mdf_file], "Pair.7z", b"")
    for member in (mds, mdf_file):
        vol = image_volume(member)
        assert vol.name == "Apple Disk"
        assert sorted(n for n, _ in vol.items()) == ["readme.txt"]


def test_a_pair_member_without_its_other_half_says_so():
    from macrt.macfile import ArchiveFolder, MacFile, image_volume
    _, mdf_bytes = _cd(_volume_raw())
    mdf_file = MacFile("LonelyPairFixture.mdf", mdf_bytes, b"", b"????", b"????",
                       0, "LonelyPairFixture.mdf")
    ArchiveFolder([mdf_file], "Pair.7z", b"")
    with pytest.raises(ValueError, match="it has no .mds beside it"):
        image_volume(mdf_file)
