"""Tests for the cue sheet parser: FILE/TRACK/INDEX structure, both
one-FILE-per-track and one-FILE-many-TRACKs shapes, the common header
lines, and the malformed sheets, one per refusal.

Fixtures are cue sheets written inline as bytes, in the layout cdrdao
and the CD mastering tools produce.
"""

import pytest

from macrt import cue
from macrt.cue import CueError, TrackType

SINGLE_IMAGE = b"""\
REM GENRE Game
REM DATE 1997
PERFORMER "Nobody"
TITLE "Apple Works CD"
FILE "game.bin" BINARY
  TRACK 01 MODE1/2352
    TITLE "Data"
    PREGAP 00:02:00
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Track 2"
    PERFORMER "Someone"
    INDEX 00 05:32:15
    INDEX 01 05:34:15
"""

PER_TRACK = b"""\
FILE "track01.bin" BINARY
  TRACK 01 MODE1/2352
    INDEX 01 00:00:00
FILE "track02.bin" BINARY
  TRACK 02 AUDIO
    INDEX 01 00:00:00
FILE "track03.bin" WAVE
  TRACK 03 AUDIO
    INDEX 01 00:00:00
"""

# A Modenese/DataReady-style sheet: heavy REM usage, one file per track,
# MODE2 data.  Hand-written, in that style.
MODENESE = b"""\
REM COMMENT "Modenese DataReady"
REM GENRE "Data disc"
REM DATE 1999
REM DISCID ABCD1234
REM COMMENT "Processed by DataReady"
CATALOG 1234567890123
PERFORMER "Vendor"
TITLE "Product CD"
FILE "data.bin" BINARY
  TRACK 01 MODE2/2352
    TITLE "Product data"
    REM ISRC IT-AB1-99-00001
    INDEX 00 00:00:00
    INDEX 01 00:02:00
FILE "audio.bin" BINARY
  TRACK 02 AUDIO
    TITLE "Jingle"
    REM COMMENT "Premastered by DataReady"
    SONGWRITER "Vendor jingle dept"
    ISRC IT-AB1-99-00002
    PREGAP 00:02:00
    INDEX 01 00:00:00
"""


def test_single_file_many_tracks():
    sheet = cue.parse_cue(SINGLE_IMAGE)
    assert [f.name for f in sheet.files] == ["game.bin"]
    f = sheet.files[0]
    assert f.file_format == "BINARY"
    assert [t.number for t in f.tracks] == [1, 2]
    assert f.tracks[0].type is TrackType.MODE1_2352
    assert f.tracks[1].type is TrackType.AUDIO


def test_single_file_track_details():
    sheet = cue.parse_cue(SINGLE_IMAGE)
    data, audio = sheet.files[0].tracks
    assert data.meta["title"] == "Data"
    assert data.pregap.frames == (0 * 60 + 2) * 75
    assert [i.number for i in data.indexes] == [1]
    assert data.indexes[0].time.frames == 0
    assert data.indexes[0].time.text == "00:00:00"
    assert audio.indexes[0].time.frames == (5 * 60 + 32) * 75 + 15
    assert audio.indexes[1].time.frames == (5 * 60 + 34) * 75 + 15
    assert audio.meta["performer"] == "Someone"
    assert audio.meta["title"] == "Track 2"


def test_sheet_wide_metadata():
    sheet = cue.parse_cue(SINGLE_IMAGE)
    assert sheet.meta["performer"] == "Nobody"
    assert sheet.meta["title"] == "Apple Works CD"
    assert sheet.rems == ["GENRE Game", "DATE 1997"]


def test_per_track_files_round_trip_distinctly():
    sheet = cue.parse_cue(PER_TRACK)
    assert [f.name for f in sheet.files] == ["track01.bin", "track02.bin", "track03.bin"]
    assert [f.file_format for f in sheet.files] == ["BINARY", "BINARY", "WAVE"]
    assert [[t.number for t in f.tracks] for f in sheet.files] == [[1], [2], [3]]
    assert [f.tracks[0].type for f in sheet.files] == [
        TrackType.MODE1_2352, TrackType.AUDIO, TrackType.AUDIO]
    # the one-file shape is not flattened into the per-track shape
    assert len(sheet.files) == 3
    assert [t.number for t in sheet.tracks] == [1, 2, 3]


def test_modenese_style_sheet():
    sheet = cue.parse_cue(MODENESE)
    assert sheet.rems == [
        'COMMENT "Modenese DataReady"',
        'GENRE "Data disc"',
        "DATE 1999",
        "DISCID ABCD1234",
        'COMMENT "Processed by DataReady"',
    ]
    assert sheet.meta["catalog"] == "1234567890123"
    assert sheet.meta["performer"] == "Vendor"
    assert sheet.meta["title"] == "Product CD"
    data, audio = sheet.files[0].tracks[0], sheet.files[1].tracks[0]
    assert data.type is TrackType.MODE2_2352
    assert data.indexes[0].number == 0
    assert data.indexes[1].time.frames == (0 * 60 + 2) * 75
    assert data.rems == ["ISRC IT-AB1-99-00001"]
    assert audio.type is TrackType.AUDIO
    assert audio.pregap.frames == 150
    assert audio.rems == ['COMMENT "Premastered by DataReady"']
    assert audio.meta["songwriter"] == "Vendor jingle dept"
    assert audio.meta["isrc"] == "IT-AB1-99-00002"


def test_crlf_and_bom_are_tolerated():
    sheet = cue.parse_cue(b'\xef\xbb\xbfFILE "a.bin" BINARY\r\n  TRACK 01 AUDIO\r\n    INDEX 01 00:00:00\r\n')
    assert sheet.files[0].name == "a.bin"
    assert sheet.files[0].tracks[0].indexes[0].time.frames == 0


def test_names_with_spaces_stay_verbatim():
    sheet = cue.parse_cue(b'FILE "my game, disc 1.bin" BINARY\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n')
    assert sheet.files[0].name == "my game, disc 1.bin"


def test_str_input():
    sheet = cue.parse_cue('FILE "a.bin" BINARY\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n')
    assert sheet.files[0].name == "a.bin"


def test_no_file_is_refused():
    with pytest.raises(CueError) as e:
        cue.parse_cue(b'REM GENRE Game\nTITLE "Nothing"\n')
    assert "no FILE" in str(e.value)


def test_track_before_file_is_refused():
    with pytest.raises(CueError) as e:
        cue.parse_cue(b"TRACK 01 AUDIO\n  INDEX 01 00:00:00\n")
    assert e.value.line == 1
    assert "TRACK before any FILE" in str(e.value)


def test_index_before_track_is_refused():
    with pytest.raises(CueError) as e:
        cue.parse_cue(b'FILE "a.bin" BINARY\nINDEX 01 00:00:00\n')
    assert e.value.line == 2
    assert "INDEX before any TRACK" in str(e.value)


def test_bad_index_time_is_refused():
    for bad in (b"00:61:00", b"00:00:75", b"aa:bb:cc", b"0:0:0:0", b"00-00-00"):
        sheet = b'FILE "a.bin" BINARY\n  TRACK 01 AUDIO\n    INDEX 01 ' + bad + b"\n"
        with pytest.raises(CueError) as e:
            cue.parse_cue(sheet)
        assert e.value.line == 3
        assert "bad time" in str(e.value)


def test_unknown_track_type_is_refused():
    with pytest.raises(CueError) as e:
        cue.parse_cue(b'FILE "a.bin" BINARY\n  TRACK 01 MODE3/2352\n')
    assert e.value.line == 2
    assert "unknown track type" in str(e.value)


def test_file_without_track_is_refused():
    with pytest.raises(CueError) as e:
        cue.parse_cue(b'FILE "a.bin" BINARY\nFILE "b.bin" BINARY\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n')
    assert 'FILE "a.bin" holds no TRACK' in str(e.value)


def test_bad_track_number_is_refused():
    with pytest.raises(CueError) as e:
        cue.parse_cue(b'FILE "a.bin" BINARY\n  TRACK 100 AUDIO\n')
    assert e.value.line == 2


def test_pregap_before_track_is_refused():
    with pytest.raises(CueError) as e:
        cue.parse_cue(b'FILE "a.bin" BINARY\nPREGAP 00:02:00\n')
    assert e.value.line == 2
    assert "PREGAP before any TRACK" in str(e.value)


def test_bad_pregap_time_is_refused():
    with pytest.raises(CueError) as e:
        cue.parse_cue(b'FILE "a.bin" BINARY\n  TRACK 01 AUDIO\n    PREGAP 00:99:00\n')
    assert e.value.line == 3
    assert "bad time" in str(e.value)


def test_error_carries_line_number():
    with pytest.raises(CueError) as e:
        cue.parse_cue(b'FILE "a.bin" BINARY\n  TRACK 01 AUDIO\n    INDEX 01 00:00:75\n')
    assert e.value.line == 3
    assert str(e.value).startswith("line 3: ")
