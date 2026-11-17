"""Parse cue sheets: the text half of a BIN/CUE disc image.

A cue sheet is a plain-text table of contents for a CD image.  It names
the files that hold the tracks (one .bin per track, or one .bin holding
every track), and for each track its number, its mode and its index
times.  The sectors themselves live in the .bin; this module reads only
the sheet, knows nothing about image formats and performs no disk I/O.

Two shapes occur in the wild and both are kept apart in the result: a
sheet with one FILE per track, and a sheet with one FILE whose TRACKs
all follow it.  `CueSheet.files` lists each FILE as its own `CueFile`,
so `files[i].tracks` is exactly the tracks that FILE holds.

Times are MM:SS:FF at 75 frames a second, as the sheet writes them;
each `CueIndex` keeps both the verbatim string and the absolute frame
count.  Track modes are normalised to the `TrackType` enum, named for
the sector layout a reader will need (MODE1_2352 is a 2352-byte Mode 1
sector and so on); a mode the enum does not know is refused.

Malformed sheets raise `CueError`, which carries the offending line
number.  REM, PERFORMER, TITLE, SONGWRITER, ISRC, CATALOG, FLAGS and
friends are kept (in `meta` and `rems`, sheet-wide or per track,
depending on where they appear); unknown directives are skipped.

Written from the cue sheet format as produced by cdrdao and the
CD mastering tools (toc2cue, Alcohol 120%); no code is carried over and
no dependency is added.
"""

import re
import shlex
from dataclasses import dataclass, field
from enum import Enum


class CueError(ValueError):
    """A cue sheet this parser won't read: damaged, or a form it lacks."""

    def __init__(self, line: int | None, message: str):
        self.line = line
        self.message = message
        super().__init__(f"line {line}: {message}" if line else message)


class TrackType(Enum):
    """The track modes a cue sheet may name, normalised.

    The names carry the sector size a reader needs; 2352 is a raw CD
    sector, 2336 is a raw sector without its 16 bytes of sync/header,
    2048 is the user bytes alone.
    """

    AUDIO = "AUDIO"
    MODE1_2048 = "MODE1_2048"
    MODE1_2352 = "MODE1_2352"
    MODE2_2048 = "MODE2_2048"
    MODE2_2352 = "MODE2_2352"
    MODE2_2336 = "MODE2_2336"


_TRACK_TYPES = {
    "AUDIO": TrackType.AUDIO,
    "MODE1/2048": TrackType.MODE1_2048,
    "MODE1/2352": TrackType.MODE1_2352,
    "MODE2/2048": TrackType.MODE2_2048,
    "MODE2/2352": TrackType.MODE2_2352,
    "MODE2/2336": TrackType.MODE2_2336,
}

@dataclass(frozen=True)
class CueTime:
    """One MM:SS:FF time from the sheet, verbatim and as a frame count."""

    text: str
    frames: int


@dataclass(frozen=True)
class CueIndex:
    """One INDEX line: its number (00 is the track's pregap) and its time."""

    number: int
    time: CueTime


@dataclass
class CueTrack:
    """One TRACK block: its number, its mode and the INDEX lines that follow."""

    number: int
    type: TrackType
    indexes: list[CueIndex] = field(default_factory=list)
    pregap: CueTime | None = None
    postgap: CueTime | None = None
    meta: dict[str, str] = field(default_factory=dict)
    rems: list[str] = field(default_factory=list)


@dataclass
class CueFile:
    """One FILE block: the file's name and format, and the tracks it holds."""

    name: str
    file_format: str
    tracks: list[CueTrack] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)
    rems: list[str] = field(default_factory=list)


@dataclass
class CueSheet:
    """The whole sheet: its FILE blocks in order, plus sheet-wide metadata."""

    files: list[CueFile] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)
    rems: list[str] = field(default_factory=list)

    @property
    def tracks(self) -> list[CueTrack]:
        """Every track in the sheet, in order."""
        return [t for f in self.files for t in f.tracks]


# the directive name, any case, then the rest of the line
_DIRECTIVE = re.compile(r"^([A-Za-z_]+)[ \t]*(.*)$")
_TIME = re.compile(r"^(\d{1,3}):(\d{1,2}):(\d{1,2})$")

# directives that take one free-text value and are kept in `meta`
_META_DIRECTIVES = {"PERFORMER", "TITLE", "SONGWRITER", "ISRC", "CATALOG", "FLAGS"}


def _parse_time(text: str, line: int) -> CueTime:
    m = _TIME.match(text)
    if not m:
        raise CueError(line, f"bad time {text!r} (want MM:SS:FF)")
    minutes, seconds, frames = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if minutes > 99:
        raise CueError(line, f"bad time {text!r} (minutes run past 99)")
    if seconds > 59:
        raise CueError(line, f"bad time {text!r} (seconds run past 59)")
    if frames > 74:
        raise CueError(line, f"bad time {text!r} (frames run past 74)")
    return CueTime(text=text, frames=(minutes * 60 + seconds) * 75 + frames)


def _split(line: str, line_no: int) -> list[str]:
    try:
        return shlex.split(line, comments=False, posix=True)
    except ValueError as e:
        raise CueError(line_no, f"unbalanced quotes ({e})") from None


def parse_cue(data: bytes | str) -> CueSheet:
    """Parse a cue sheet (bytes or text) into a `CueSheet`."""
    if isinstance(data, bytes):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as e:
            raise CueError(None, f"the cue sheet is not UTF-8 text ({e})") from None
    else:
        text = data
        if text.startswith("\ufeff"):
            text = text[1:]

    sheet = CueSheet()
    cur_file: CueFile | None = None
    cur_track: CueTrack | None = None

    def target():
        """Where a REM/meta line lands: the open track, its FILE, or the sheet."""
        return cur_track if cur_track is not None else (cur_file if cur_file is not None else sheet)

    lines = text.splitlines()
    for line_no, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line:
            continue
        m = _DIRECTIVE.match(line)
        if not m:
            raise CueError(line_no, f"unrecognised line {raw.strip()!r}")
        directive = m.group(1).upper()
        rest = m.group(2).strip()
        args = _split(rest, line_no) if rest else []

        if directive == "FILE":
            if len(args) < 2:
                raise CueError(line_no, "FILE wants a file name and a format "
                                        '(e.g. FILE "name.bin" BINARY)')
            name, file_format = args[0], args[1].upper()
            if len(args) > 2:
                raise CueError(line_no, f"FILE takes a name and a format, got {len(args)} arguments")
            cur_file = CueFile(name=name, file_format=file_format)
            sheet.files.append(cur_file)
            cur_track = None
        elif directive == "TRACK":
            if cur_file is None:
                raise CueError(line_no, "TRACK before any FILE")
            if len(args) != 2:
                raise CueError(line_no, "TRACK wants a number and a mode "
                                        "(e.g. TRACK 01 AUDIO)")
            try:
                number = int(args[0], 10)
            except ValueError:
                raise CueError(line_no, f"bad track number {args[0]!r}") from None
            if not 1 <= number <= 99:
                raise CueError(line_no, f"track number {number} is outside 01-99")
            mode = args[1].upper()
            track_type = _TRACK_TYPES.get(mode)
            if track_type is None:
                raise CueError(line_no, f"unknown track type {args[1]!r}")
            cur_track = CueTrack(number=number, type=track_type)
            cur_file.tracks.append(cur_track)
        elif directive == "INDEX":
            if cur_track is None:
                raise CueError(line_no, "INDEX before any TRACK")
            if len(args) != 2:
                raise CueError(line_no, "INDEX wants a number and a time "
                                        "(e.g. INDEX 01 00:00:00)")
            try:
                number = int(args[0], 10)
            except ValueError:
                raise CueError(line_no, f"bad INDEX number {args[0]!r}") from None
            if not 0 <= number <= 99:
                raise CueError(line_no, f"INDEX number {number} is outside 00-99")
            cur_track.indexes.append(CueIndex(number=number,
                                              time=_parse_time(args[1], line_no)))
        elif directive in ("PREGAP", "POSTGAP"):
            if cur_track is None:
                raise CueError(line_no, f"{directive} before any TRACK")
            if len(args) != 1:
                raise CueError(line_no, f"{directive} wants one time (e.g. {directive} 00:02:00)")
            time = _parse_time(args[0], line_no)
            if directive == "PREGAP":
                cur_track.pregap = time
            else:
                cur_track.postgap = time
        elif directive == "REM":
            target().rems.append(rest)
        elif directive in _META_DIRECTIVES:
            # a quoted value is one argument; anything else is the raw text
            target().meta[directive.lower()] = args[0] if len(args) == 1 else rest
        # anything else (CDTEXTFILE, CATALOG-free vendor extensions, ...) is skipped

    if not sheet.files:
        raise CueError(None, "the cue sheet holds no FILE")
    for f in sheet.files:
        if not f.tracks:
            raise CueError(None, f'FILE "{f.name}" holds no TRACK')
    return sheet
