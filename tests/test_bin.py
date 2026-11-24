"""Tests for the cue/bin data track reader: a .bin image's data track,
with any audio tracks present but never exposed.

Fixtures are written here, in memory: a 16-sector image built from raw
Mode 1 or Mode 2 Form 1 sector bytes, with sync, header and (for Mode 2)
a doubled subheader, at a 2048, 2352 or 2448-byte stride.
"""

import pytest

from macrt.bin import COOKED, BinError, BinTrack, read_image

SECTOR = 2048
RAW = 2352
SUBCHANNEL = 96
RAW_SUB = RAW + SUBCHANNEL
FORM2_RAW = 2336
NSECTORS = 16

SYNC = b"\x00" + b"\xff" * 10 + b"\x00"


def _user(seed: int, n: int = NSECTORS) -> bytes:
    """Distinct, recognisable user bytes for each of n sectors."""
    out = bytearray()
    for i in range(n):
        out += bytes([(seed + i) & 0xff]) * SECTOR
    return bytes(out)


def _mode1_sector(user: bytes) -> bytes:
    hdr = b"\x00\x02\x00" + b"\x01"          # 00:02:00, mode 1
    return SYNC + hdr + user + b"\x00" * 288


def _mode2_form1_sector(user: bytes, submode: int = 0x08) -> bytes:
    hdr = b"\x00\x02\x00" + b"\x02"          # 00:02:00, mode 2
    sub = bytes([0, 0, submode, 0]) * 2      # doubled subheader
    return SYNC + hdr + sub + user + b"\x00" * 280


def _audio_sector(seed: int) -> bytes:
    return bytes([seed & 0xff]) * RAW


def _image(make_sector, user: bytes, stride: int) -> bytes:
    out = bytearray()
    for at in range(0, len(user), SECTOR):
        sec = bytearray(make_sector(user[at:at + SECTOR]))
        sec += b"\x00" * (stride - len(sec))
        out += sec
    return bytes(out)


def _tracks(*specs) -> list[BinTrack]:
    """A disc's track list from (index, offset, sector_size, mode) specs."""
    return [BinTrack(*spec) for spec in specs]


def _one_data_track(user: bytes, *, mode="MODE1", stride=RAW, after=()):
    """A .bin with one data track holding user, plus extra tracks after it."""
    if stride == 2048:
        sec = _image(lambda u: u, user, stride)
    elif mode == "MODE1":
        sec = _image(_mode1_sector, user, stride)
    else:
        sec = _image(_mode2_form1_sector, user, stride)
    tracks = _tracks((1, 0, stride, mode))
    off = len(sec)
    for i, (amode, abytes, astride) in enumerate(after, start=2):
        tracks = tracks + _tracks((i, off, astride, amode))
        sec += abytes
        off += len(abytes)
    return sec, tracks


# --- Mode 1 -------------------------------------------------------------

def test_a_mode_1_track_reads_its_user_sectors():
    user = _user(0x11)
    sec, tracks = _one_data_track(user, mode="MODE1", stride=RAW)
    assert read_image(sec, tracks) == user


def test_a_mode_1_track_with_interleaved_subchannel_reads():
    user = _user(0x22)
    sec, tracks = _one_data_track(user, mode="MODE1", stride=RAW_SUB)
    assert read_image(sec, tracks) == user


def test_a_cooked_2048_byte_track_passes_through():
    user = _user(0x33)
    sec, tracks = _one_data_track(user, stride=COOKED)
    assert read_image(sec, tracks) == user


def test_mode_1_sectors_are_16_and_2048_bytes():
    user = _user(0x44)
    sec, tracks = _one_data_track(user, mode="MODE1", stride=RAW)
    out = read_image(sec, tracks)
    assert len(out) == NSECTORS * SECTOR


# --- Mode 2 Form 1 ------------------------------------------------------

def test_a_mode_2_form_1_track_reads_its_user_sectors():
    user = _user(0x55)
    sec, tracks = _one_data_track(user, mode="MODE2_FORM1", stride=RAW)
    assert read_image(sec, tracks) == user


def test_a_mode_2_form_1_track_with_interleaved_subchannel_reads():
    user = _user(0x66)
    sec, tracks = _one_data_track(user, mode="MODE2_FORM1", stride=RAW_SUB)
    assert read_image(sec, tracks) == user


# --- audio tracks are carried but never read ----------------------------

def test_audio_tracks_are_skipped():
    user = _user(0x77)
    audio = _audio_sector(0xa0) * 3
    sec, tracks = _one_data_track(user, mode="MODE1", stride=RAW,
                                  after=[("AUDIO", audio, RAW)])
    assert read_image(sec, tracks) == user


def test_a_data_track_after_an_audio_track_reads_at_its_offset():
    audio = _audio_sector(0xa1) * 4
    user = _user(0x78)
    data = _image(_mode1_sector, user, RAW)
    sec = audio + data
    tracks = _tracks((1, 0, RAW, "AUDIO"), (2, len(audio), RAW, "MODE1"))
    assert read_image(sec, tracks) == user


# --- refusals, by name ---------------------------------------------------

def test_a_dvd_track_is_refused_by_name():
    user = _user(0x01)
    sec, tracks = _one_data_track(user, mode="DVD")
    with pytest.raises(BinError, match="it is a DVD, not a CD"):
        read_image(sec, tracks)


def test_a_mode_2_form_2_mode_is_refused_by_name():
    user = _user(0x02)
    sec, tracks = _one_data_track(user, mode="MODE2_FORM2")
    with pytest.raises(BinError, match="Mode 2 Form 2"):
        read_image(sec, tracks)


def test_a_2336_byte_sector_size_is_refused_by_name():
    user = _user(0x03)
    sec, tracks = _one_data_track(user, stride=FORM2_RAW)
    with pytest.raises(BinError, match="Mode 2 Form 2"):
        read_image(sec, tracks)


def test_a_form_2_subheader_bit_is_refused_by_name():
    user = _user(0x04)
    sec = _image(lambda u: _mode2_form1_sector(u, submode=0x28), user, RAW)  # Form 2 | Data
    tracks = _tracks((1, 0, RAW, "MODE2_FORM1"))
    with pytest.raises(BinError, match="Mode 2 Form 2"):
        read_image(sec, tracks)


def test_two_data_tracks_are_refused():
    user = _user(0x05)
    first = _image(_mode1_sector, user, RAW)
    second = _image(_mode1_sector, user, RAW)
    sec = first + second
    tracks = _tracks((1, 0, RAW, "MODE1"), (2, len(first), RAW, "MODE1"))
    with pytest.raises(BinError, match="it carries 2 data tracks"):
        read_image(sec, tracks)


def test_an_audio_only_disc_is_refused():
    audio = _audio_sector(0xa2) * NSECTORS
    with pytest.raises(BinError, match="it is audio only"):
        read_image(audio, _tracks((1, 0, RAW, "AUDIO")))


def test_an_empty_track_list_is_refused():
    with pytest.raises(BinError, match="it holds no data track"):
        read_image(b"", [])


def test_an_unrecognised_sector_size_is_refused_by_name():
    user = _user(0x06)
    sec = _image(_mode1_sector, user, RAW)
    tracks = _tracks((1, 0, 1234, "MODE1"))
    with pytest.raises(BinError, match="sector size 1234"):
        read_image(sec, tracks)


# --- offsets that don't line up with the image ---------------------------

def test_a_track_starting_past_the_end_of_the_image_says_so():
    user = _user(0x07)
    sec, _ = _one_data_track(user, mode="MODE1", stride=RAW)
    tracks = _tracks((1, len(sec) + RAW, RAW, "MODE1"))
    with pytest.raises(BinError, match="track 1 starts past its end"):
        read_image(sec, tracks)


def test_a_negative_offset_says_so():
    user = _user(0x08)
    sec, _ = _one_data_track(user, mode="MODE1", stride=RAW)
    tracks = _tracks((1, -RAW, RAW, "MODE1"))
    with pytest.raises(BinError, match="track 1 starts at a negative offset"):
        read_image(sec, tracks)


def test_a_track_length_not_a_whole_number_of_sectors_says_so():
    user = _user(0x09)
    sec, _ = _one_data_track(user, mode="MODE1", stride=RAW)
    tracks = _tracks((1, 0, RAW, "MODE1"), (2, len(sec) - 1, RAW, "AUDIO"))
    with pytest.raises(BinError, match="track 1's length is not a whole number of sectors"):
        read_image(sec, tracks)


def test_a_later_track_before_an_earlier_one_ends_says_so():
    user = _user(0x0a)
    sec, _ = _one_data_track(user, mode="MODE1", stride=RAW)
    tracks = _tracks((1, 0, RAW, "MODE1"), (2, RAW, RAW, "AUDIO"),
                     (3, RAW // 2, RAW, "AUDIO"))
    with pytest.raises(BinError, match="starts before track 2 ends"):
        read_image(sec, tracks)


def test_a_track_overrunning_the_image_says_so():
    user = _user(0x0b)
    sec, _ = _one_data_track(user, mode="MODE1", stride=RAW)
    tracks = _tracks((1, 0, RAW, "MODE1", NSECTORS + 1))
    with pytest.raises(BinError, match="track 1 runs past the .bin"):
        read_image(sec, tracks)


def test_a_stated_sector_count_is_honoured():
    user = _user(0x0c)
    sec, _ = _one_data_track(user, mode="MODE1", stride=RAW)
    tracks = _tracks((1, 0, RAW, "MODE1", NSECTORS))
    assert read_image(sec, tracks) == user


def test_a_data_track_holding_no_sectors_is_refused():
    tracks = _tracks((1, 0, RAW, "MODE1", 0))
    with pytest.raises(BinError, match="its data track holds no sectors"):
        read_image(b"", tracks)
