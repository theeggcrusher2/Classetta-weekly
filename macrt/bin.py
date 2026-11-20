"""Read the data track of a cue/bin image: the .bin half of a CD image.

A cue sheet (see macrt.cue, when it lands) describes a .bin: one FILE,
usually with one data track and audio tracks.  Track 1's pregap is not
stored; a later track's pregap is INDEX 00 and its data starts at
INDEX 01, which is the offset the caller passes here.

The data track's sectors are lifted out into a plain 2048-byte-sector
stream, the form a raw .toast image takes, and opened through the usual
volume path.  The stream presents as a CD-ROM starting at 00:02:00
(the two-second pregap), the same way a raw-sector CD mounts today.

The track list is taken as plain parameters rather than imported from
macrt.cue, so this reader stands alone: the caller (subtask 2.3) will
build it from the sheet.  Each track carries its index, its byte offset
in the .bin, its sector size (the stride between sectors) and its mode.
Track length is derived from the next track's offset, or the end of the
image for the last track; a track may carry an explicit ``sectors``
count instead, which is then checked against the image.

Mode 1 and Mode 2 Form 1 data tracks are supported, with a 2048-byte
(cooked), 2352-byte (raw) or 2448-byte (raw with 96 bytes of interleaved
subchannel, skipped) stride.  Audio tracks carry nothing to browse and
are never exposed.

DVD images, Mode 2 Form 2 tracks (raw 2336-byte sectors, whether named
as a mode or seen as the Form 2 bit in a raw Mode 2 subheader), and
images carrying more than one data track are recognised and refused by
name.  Where the sheet gives offsets that don't line up with the image,
that is said by name too.

Written from the ECMA-130 and ECMA-168 sector layouts and the cue sheet
format notes at hh.schlang.de/src/cuesheet.htm; no code is carried over
and no dependency is added.
"""

from dataclasses import dataclass
from typing import Iterable

SECTOR = 2048
RAW = 2352
SUBCHANNEL = 96
COOKED = 2048
RAW_SUBCHANNEL = RAW + SUBCHANNEL  # 2448
FORM2_RAW = 2336

MODE_1 = "MODE1"
MODE_2_FORM1 = "MODE2_FORM1"
MODE_2_FORM2 = "MODE2_FORM2"
MODE_AUDIO = "AUDIO"
MODE_DVD = "DVD"

_DATA_MODES = (MODE_1, MODE_2_FORM1)
_ALL_MODES = _DATA_MODES + (MODE_2_FORM2, MODE_AUDIO, MODE_DVD)

# the submode byte's Form 2 bit (ECMA-168), at offsets 18 and 22 of a raw
# Mode 2 sector, doubled as the subheader requires
_SUBMODE = (18, 22)
_FORM2_BIT = 0x20


class BinError(ValueError):
    """A .bin image this reader won't read: damaged, or a form it lacks."""


@dataclass(frozen=True)
class BinTrack:
    """One track of a cue/bin image, as the sheet describes it.

    ``index`` is the track number (1-based), ``offset`` the byte offset of
    its first sector in the .bin, ``sector_size`` the stride between
    sectors (2048, 2352 or 2448), and ``mode`` one of MODE1, MODE2_FORM1,
    MODE2_FORM2, AUDIO or DVD.  ``sectors`` is optional: when absent the
    track runs to the next track's offset, or the end of the image.
    """

    index: int
    offset: int
    sector_size: int
    mode: str
    sectors: int | None = None

    def __post_init__(self):
        mode = str(self.mode).upper().replace("/", "_").replace("-", "_").replace(" ", "_")
        object.__setattr__(self, "mode", mode)


def _normalize(tracks: Iterable[BinTrack]) -> list[BinTrack]:
    out = []
    for t in tracks:
        if not isinstance(t, BinTrack):
            t = BinTrack(**t) if isinstance(t, dict) else BinTrack(*t)
        out.append(t)
    return out


def _extents(data: bytes, tracks: list[BinTrack]) -> list[int]:
    """How many sectors each track holds, derived from the sheet and the
    image, checking the sheet's offsets line up with the image."""
    n = len(tracks)
    extents = [0] * n
    for i, t in enumerate(tracks):
        if t.offset < 0:
            raise BinError(f"the sheet's offsets don't line up with the image "
                           f"(track {t.index} starts at a negative offset)")
        if t.offset > len(data):
            raise BinError(f"the sheet's offsets don't line up with the image "
                           f"(track {t.index} starts past its end)")
        if t.sectors is not None:
            if t.sectors < 0:
                raise BinError(f"the sheet's offsets don't line up with the image "
                               f"(track {t.index} holds a negative sector count)")
            if t.offset + t.sectors * t.sector_size > len(data):
                raise BinError(f"the sheet's offsets don't line up with the image "
                               f"(track {t.index} runs past the .bin)")
            extents[i] = t.sectors
            continue
        end = tracks[i + 1].offset if i + 1 < n else len(data)
        if end < t.offset:
            raise BinError(f"the sheet's offsets don't line up with the image "
                           f"(track {tracks[i + 1].index} starts before track {t.index} ends)")
        length = end - t.offset
        if t.sector_size <= 0 or length % t.sector_size:
            raise BinError(f"the sheet's offsets don't line up with the image "
                           f"(track {t.index}'s length is not a whole number of sectors)")
        extents[i] = length // t.sector_size
    return extents


def data_track(tracks: Iterable[BinTrack]) -> BinTrack:
    """The one data track this image holds."""
    tracks = _normalize(tracks)
    if not tracks:
        raise BinError("the .bin image is damaged (it holds no data track)")
    data = []
    for t in tracks:
        if t.mode in _DATA_MODES:
            data.append(t)
        elif t.mode == MODE_DVD:
            raise BinError("the .bin image is unsupported (it is a DVD, not a CD)")
        elif t.mode == MODE_2_FORM2:
            raise BinError(f"the .bin image is unsupported (Mode 2 Form 2, "
                           f"raw {FORM2_RAW}-byte sectors)")
        elif t.mode != MODE_AUDIO:
            raise BinError(f"the .bin image is unsupported (track {t.index}'s mode {t.mode})")
    if len(data) > 1:
        raise BinError(f"the .bin image is unsupported (it carries {len(data)} data tracks)")
    if not data:
        if any(t.mode == MODE_AUDIO for t in tracks):
            raise BinError("the .bin image holds no data track (it is audio only)")
        raise BinError("the .bin image is damaged (it holds no data track)")
    t = data[0]
    if t.sector_size == FORM2_RAW:
        raise BinError(f"the .bin image is unsupported (Mode 2 Form 2, "
                       f"raw {FORM2_RAW}-byte sectors)")
    if t.sector_size not in (COOKED, RAW, RAW_SUBCHANNEL):
        raise BinError(f"the .bin image is unsupported (sector size {t.sector_size})")
    return t


def _form2_sector(data: bytes, at: int) -> bool:
    """Whether a raw Mode 2 sector at ``at`` carries the Form 2 bit in its
    subheader.  Both doubled subheader copies are consulted."""
    return any(data[at + off] & _FORM2_BIT for off in _SUBMODE)


def read_image(bin_data, tracks: Iterable[BinTrack]) -> bytes:
    """The data track's user sectors, as a plain 2048-byte-sector stream."""
    tracks = _normalize(tracks)
    t = data_track(tracks)
    extents = _extents(bin_data, tracks)
    sectors = extents[next(i for i, x in enumerate(tracks) if x is t)]
    if sectors <= 0:
        raise BinError("the .bin image is damaged (its data track holds no sectors)")
    stride = t.sector_size
    if stride == COOKED:
        user_off = 0
    elif stride == RAW:
        user_off = 16 if t.mode == MODE_1 else 24
    else:  # RAW_SUBCHANNEL: 96 bytes of interleaved subchannel are skipped
        user_off = 16 if t.mode == MODE_1 else 24
    out = bytearray(sectors * SECTOR)
    at = t.offset
    for i in range(sectors):
        if stride != COOKED and t.mode == MODE_2_FORM1 and _form2_sector(bin_data, at):
            raise BinError(f"the .bin image is unsupported (Mode 2 Form 2, "
                           f"track {t.index} sector {i})")
        out[i * SECTOR:(i + 1) * SECTOR] = bin_data[at + user_off:at + user_off + SECTOR]
        at += stride
    return bytes(out)
