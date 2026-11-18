"""Tests for cue/bin disc images: a .cue sheet beside its .bin.

Fixtures are written here, in memory and in tmp_path: the data track holds
an HFS volume (the one tests/test_files.py builds for raw-CD fixtures),
the audio track is silent, and the sheet is hand-written text.
"""

import logging

import pytest

from macrt import bin as binmod
from macrt.bin import BinError
from macrt.macfile import MacFile, image_kind, load_volume, volume_files
from macrt.macfile import image_volume

from test_files import hfs_image, partitioned

SECTOR = 2048
RAW = 2352
AUDIO_FRAMES = 75

SYNC = b"\x00" + b"\xff" * 10 + b"\x00"


def _raw_cd(disc: bytes) -> bytes:
    """An image of a CD's whole 2352-byte mode 1 sectors (ECMA-130)."""
    return b"".join((SYNC + bytes([0, 2, n // SECTOR % 75, 1]) + disc[n:n + SECTOR])
                    .ljust(RAW, b"\0") for n in range(0, len(disc), SECTOR))


def _disc() -> bytes:
    return partitioned(hfs_image(), start=96)


def _timecode(frames: int) -> str:
    return f"{frames // 4500:02d}:{frames // 75 % 60:02d}:{frames % 75:02d}"


PREGAP_FRAMES = 150

def _sheet(bin_name: str, extra_track: bool = True) -> str:
    """A single-file sheet: one MODE1/2352 data track, then one AUDIO
    track whose pregap is written as INDEX 00 (and stored, as silence)."""
    disc_frames = len(_disc()) // SECTOR
    text = (f'FILE "{bin_name}" BINARY\n'
            f'  TRACK 01 MODE1/2352\n'
            f'    INDEX 01 00:00:00\n')
    if extra_track:
        text += (f'  TRACK 02 AUDIO\n'
                 f'    INDEX 00 {_timecode(disc_frames)}\n'
                 f'    INDEX 01 {_timecode(disc_frames + PREGAP_FRAMES)}\n')
    return text


def _pair(tmp_path, sheet: str, bin_name: str, disc: bytes):
    cue = tmp_path / "game.cue"
    cue.write_text(sheet)
    image = tmp_path / bin_name
    image.write_bytes(_raw_cd(disc))
    return cue, image


def _with_audio(tmp_path, sheet: str, bin_name: str, disc: bytes):
    cue, image = _pair(tmp_path, sheet, bin_name, disc)
    with image.open("ab") as f:
        f.write(b"\x00" * PREGAP_FRAMES * RAW)   # track 2's INDEX 00 pregap
        f.write(b"\x11" * AUDIO_FRAMES * RAW)
    return cue, image


# --- browsing -----------------------------------------------------------

def test_a_data_and_audio_track_image_opens(tmp_path):
    """The data track is extracted, the audio track dropped: the volume on
    the cue mounts as a CD and its HFS volume browses."""
    disc = _disc()
    cue, image = _with_audio(tmp_path, _sheet("game.bin"), "game.bin", disc)
    for path in (cue, image):
        vol = load_volume(path)
        assert vol.name == "Test Disk"
        keys = {f.key for f in volume_files(vol)}
        assert "Test Disk:Games:Test App" in keys


def test_a_single_track_data_image_opens(tmp_path):
    disc = _disc()
    cue, image = _pair(tmp_path, _sheet("game.bin", extra_track=False), "game.bin", disc)
    vol = load_volume(cue)
    assert vol.name == "Test Disk"
    keys = {f.key for f in volume_files(vol)}
    assert "Test Disk:Games:Test App" in keys


def test_a_mode_2_form_1_image_opens(tmp_path):
    """A MODE2/2352 sheet: Mode 2 Form 1 sectors, its subheader doubling
    the 0x08 submode bit."""
    disc = _disc()
    sync = SYNC
    def mode2(user, n):
        hdr = bytes([0, 2, n // 75 % 60, 2])
        sub = bytes([0, 0, 0x08, 0]) * 2
        return (sync + hdr + sub + user).ljust(RAW, b"\0")
    image_bytes = b"".join(mode2(disc[n:n + SECTOR], n // SECTOR)
                           for n in range(0, len(disc), SECTOR))
    cue = tmp_path / "mode2.cue"
    cue.write_text('FILE "mode2.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n')
    (tmp_path / "mode2.bin").write_bytes(image_bytes)
    vol = load_volume(cue)
    assert vol.name == "Test Disk"


def test_a_cue_mounts_as_a_cd(tmp_path):
    from macrt.macfile import open_container
    disc = _disc()
    cue, image = _with_audio(tmp_path, _sheet("game.bin"), "game.bin", disc)
    for path in (cue, image):
        vol = open_container(path)
        assert vol.cdrom


def test_a_pair_member_opens_from_an_archive():
    """A (.cue, .bin) pair nested in an archive opens from either member,
    the same way an (.mds, .mdf) pair does."""
    from macrt.macfile import ArchiveFolder
    disc = _disc()
    image = _raw_cd(disc)
    cue_text = _sheet("ArchivePairFixture.bin", extra_track=False)
    cue_f = MacFile("ArchivePairFixture.cue", cue_text.encode(), b"", b"????", b"????",
                    0, "ArchivePairFixture.cue")
    bin_f = MacFile("ArchivePairFixture.bin", image, b"", b"????", b"????",
                    0, "ArchivePairFixture.bin")
    ArchiveFolder([cue_f, bin_f], "pair.7z", b"")
    for member in (cue_f, bin_f):
        vol = image_volume(member)
        assert vol.name == "Test Disk"
        assert "Test Disk:Games:Test App" in {f.key for f in volume_files(vol)}


def test_a_pair_member_without_its_other_half_says_so():
    from macrt.macfile import ArchiveFolder
    cue_f = MacFile("LonelyPairFixture.cue", _sheet("LonelyPairFixture.bin").encode(),
                    b"", b"????", b"????", 0, "LonelyPairFixture.cue")
    ArchiveFolder([cue_f], "pair.7z", b"")
    with pytest.raises(ValueError, match="LonelyPairFixture.bin is missing"):
        image_volume(cue_f)


def test_image_kind_names_a_pair(tmp_path):
    from macrt.macfile import ArchiveFolder
    cue, image = _pair(tmp_path, _sheet("game.bin", extra_track=False), "game.bin", _disc())
    cue_f = MacFile(cue.name, cue.read_bytes(), b"", b"????", b"????", 0, str(cue))
    bin_f = MacFile(image.name, image.read_bytes(), b"", b"????", b"????", 0, str(image))
    ArchiveFolder([cue_f, bin_f], "pair.7z", b"")
    for member in (cue_f, bin_f):
        assert image_kind(member) == "BIN/CUE CD image"
    lone = MacFile("lone.bin", image.read_bytes(), b"", b"????", b"????", 0, str(image))
    assert image_kind(lone) != "BIN/CUE CD image"


# --- failure paths ------------------------------------------------------

def test_a_bare_bin_opens_with_a_warning(tmp_path, caplog):
    """A .bin with no sheet beside it opens as it always has — the raw-CD
    path — but with a warning that it is unknown what disc this is."""
    disc = _disc()
    image = tmp_path / "Game.bin"
    image.write_bytes(_raw_cd(disc))
    with caplog.at_level(logging.WARNING, logger="macrt.macfile"):
        vol = load_volume(image)
    assert vol.name == "Test Disk"
    assert any("it is unknown what disc this is" in r.message for r in caplog.records)


def test_a_multi_file_sheet_is_refused_with_a_warning(tmp_path, caplog):
    """One .bin per track is unsupported: a warning recommending the files
    be merged, and a refusal — no pieces are opened."""
    disc = _disc()
    sheet = ('FILE "track1.bin" BINARY\n'
             '  TRACK 01 MODE1/2352\n'
             '    INDEX 01 00:00:00\n'
             'FILE "track2.bin" BINARY\n'
             '  TRACK 02 AUDIO\n'
             f'    INDEX 01 {_timecode(len(disc) // SECTOR)}\n')
    cue = tmp_path / "multi.cue"
    cue.write_text(sheet)
    (tmp_path / "track1.bin").write_bytes(_raw_cd(disc))
    (tmp_path / "track2.bin").write_bytes(b"\x11" * AUDIO_FRAMES * RAW)
    with caplog.at_level(logging.WARNING, logger="macrt.macfile"):
        with pytest.raises(ValueError, match="one file per track"):
            load_volume(cue)
    assert any("merge" in r.message for r in caplog.records)


def test_a_cue_with_a_missing_bin_says_so(tmp_path):
    cue = tmp_path / "game.cue"
    cue.write_text(_sheet("game.bin", extra_track=False))
    with pytest.raises(ValueError, match="game.bin is missing"):
        load_volume(cue)


def test_a_cue_naming_a_bin_that_is_not_there_says_so(tmp_path):
    """The sheet names its .bin: a pair whose sheet names another file, or
    none, is refused by name."""
    disc = _disc()
    cue = tmp_path / "game.cue"
    cue.write_text('FILE "elsewhere.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n')
    (tmp_path / "game.bin").write_bytes(_raw_cd(disc))
    with pytest.raises(ValueError, match="elsewhere.bin is missing"):
        load_volume(cue)


def test_an_audio_only_sheet_is_refused(tmp_path):
    audio = b"\x11" * 10 * RAW
    cue = tmp_path / "audio.cue"
    cue.write_text('FILE "audio.bin" BINARY\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n')
    (tmp_path / "audio.bin").write_bytes(audio)
    with pytest.raises(BinError, match="holds no data track"):
        load_volume(cue)


def test_a_mode_2_form_2_sheet_is_refused(tmp_path):
    """MODE2/2336 names a raw Form 2 sector: refused by name, the way
    .mdf refuses its Mode 2 tracks."""
    disc = _disc()
    cue = tmp_path / "f2.cue"
    cue.write_text('FILE "f2.bin" BINARY\n  TRACK 01 MODE2/2336\n    INDEX 01 00:00:00\n')
    (tmp_path / "f2.bin").write_bytes(b"\0" * (len(disc) // SECTOR * 2336))
    with pytest.raises(BinError, match="Mode 2 Form 2"):
        load_volume(cue)


def test_a_sheet_with_a_track_without_index_01_says_so(tmp_path):
    cue = tmp_path / "noindex.cue"
    cue.write_text('FILE "noindex.bin" BINARY\n  TRACK 01 MODE1/2352\n')
    (tmp_path / "noindex.bin").write_bytes(b"\0" * RAW)
    with pytest.raises(ValueError, match="track 01 has no INDEX 01"):
        load_volume(cue)


def test_a_sheet_whose_offsets_do_not_line_up_is_refused(tmp_path):
    """INDEX 01 past the end of the .bin: the sheet and the image don't
    line up, said by name."""
    disc = _disc()
    image = _raw_cd(disc)
    cue = tmp_path / "late.cue"
    frames = _timecode(len(image) // RAW + 100)
    cue.write_text(f'FILE "late.bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 {frames}\n')
    (tmp_path / "late.bin").write_bytes(image)
    with pytest.raises(BinError, match="offsets don't line up"):
        load_volume(cue)


def test_a_damaged_sheet_says_so(tmp_path):
    cue = tmp_path / "junk.cue"
    cue.write_text("FILE\n")
    (tmp_path / "junk.bin").write_bytes(b"\0" * RAW)
    with pytest.raises(ValueError, match="junk.cue"):
        load_volume(cue)
