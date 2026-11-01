"""
A CD-ROM's driver, .AppleCD, for a disk image macrt mounts as a CD-ROM
(one named .iso, .cdr or .toast): the Control calls Technical Note DV 22,
"CD-ROM Driver Calls", gives the Apple CD-ROM driver, as the disc an
image holds would answer them.  The image is the disc's data track, all
of it there is: track 1, of 2048-byte blocks, starting two seconds in,
after the lead-in, at 00:02:00.  Lode Runner: The Legend Returns asks
for the audio play status and stops play at track 3 before it starts
its music.

ReadTOC (100) gives the table of contents of that one track, in each of
the first five forms: its first and last track numbers, where the Lead
Out starts, each track's start, the whole table and its one session.
The audio calls, AudioTrackSearch (103) to AudioScan (108), are refused
with paramErr, as the Note has them for audio operations tried on a data
track.  AudioControl (109) sets the volume of the drive's two audio
channels and ReadAudioVolume (112) reads it back; the Note has both
working with no disc in the drive.  Until a program sets them they're
both 255, the loudest (macrt's own choice).  The Note's other calls are
macrt's controlErr still, as calls the driver doesn't have are.
"""

from __future__ import annotations

from ..memory import ADDRESS_MASK
from ..toolbox.files import FileError, paramErr

controlErr = -17

csCode = 26
csParam = 28

ReadTOC = 100
AUDIO = range(103, 109)         # AudioTrackSearch to AudioScan
AudioControl, ReadAudioVolume = 109, 112

BLOCK = 2048                    # a CD-ROM's data block, a frame's
FRAMES, SECONDS = 75, 60        # frames a second, seconds a minute
LEAD_IN = 2 * FRAMES            # where track 1 starts: 00:02:00
DATA_TRACK = 0x4                # the Track Control Field's 01x0
LOUDEST = 0xFFFF                # both channels' volume, left in the high byte


def _bcd(n: int) -> int:
    return n // 10 << 4 | n % 10


def _msf(frames: int) -> bytes:
    """An absolute address in BCD minutes, seconds and frames."""
    seconds, frame = divmod(frames, FRAMES)
    minutes, second = divmod(seconds, SECONDS)
    return bytes((_bcd(minutes), _bcd(second), _bcd(frame)))


def _lead_out(vol) -> bytes:
    disk = getattr(vol.store.volume, "disk", None)
    size = len(disk.sectors) if disk is not None else vol.store.volume.size
    return _msf(LEAD_IN + -(-size // BLOCK))


def control(t, pb: int, vol) -> None:
    """Control call csCode to the CD-ROM vol is on."""
    m = t.mem
    code = m.r16s(pb + csCode)
    if code == ReadTOC:
        _read_toc(t, pb, vol)
    elif code in AUDIO:
        raise FileError(paramErr)           # the disc's one track is data
    elif code == AudioControl:
        t.system.cd_volume = m.r16(pb + csParam)
    elif code == ReadAudioVolume:
        m.w16(pb + csParam, getattr(t.system, "cd_volume", LOUDEST))
    else:
        raise FileError(controlErr)


def _read_toc(t, pb: int, vol) -> None:
    m = t.mem
    kind = m.r16(pb + csParam)
    lead_out, start = _lead_out(vol), _msf(LEAD_IN)
    if kind == 1:                           # the first and last tracks
        m.write(pb + csParam, bytes((1, 1)))
    elif kind == 2:                         # where the Lead Out starts
        m.write(pb + csParam, lead_out + b"\0")
    elif kind == 3:                         # from a track on, as many as the buffer holds
        buffer, size = m.r32(pb + csParam + 2) & ADDRESS_MASK, m.r16(pb + csParam + 6)
        if m.r8(pb + csParam + 8) <= 1 and size >= 4:
            m.write(buffer, bytes((DATA_TRACK,)) + start)
    elif kind == 4:                         # the whole table, points A0, A1 and A2 first
        toc = bytearray(512)
        toc[1:16] = (bytes((DATA_TRACK, 0xA0, 1, 0, 0)) + bytes((DATA_TRACK, 0xA1, 1, 0, 0))
                     + bytes((DATA_TRACK, 0xA2)) + lead_out)
        toc[16:21] = bytes((DATA_TRACK, 1)) + start
        m.write(m.r32(pb + csParam + 2) & ADDRESS_MASK, bytes(toc))
    elif kind == 5:                         # one session, its first track 1
        m.write(pb + csParam, bytes((0, 1, 0, 1, 0, 1, DATA_TRACK)) + start)
    else:
        raise FileError(paramErr)
