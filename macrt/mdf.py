"""Read Alcohol 120% disc images: a .mdf sector image and its .mds sidecar.

The .mds (Media Descriptor) is a little-endian table of sessions and
tracks; the .mdf holds the sectors.  A CD with a data track stores it
with the 12-byte sync, 4-byte header and 288 bytes of ECC/EDC (Mode 1)
or, for Mode 2 Form 1, 24 bytes of header (16 sync/header + 8 subheader)
and 276 bytes of ECC/EDC; the user bytes in either mode are 2048 per
sector.  Audio tracks hold nothing to browse.

The data track's sectors are lifted out into a plain 2048-byte-sector
stream, the form a raw .toast image takes, and opened through the usual
volume path.  Track 1's pregap is not stored; a later track's pregap is
skipped.  Interleaved subchannel, 96 bytes per sector, is skipped.

DVD images, Mode 2 (2336-byte) and Mode 2 Form 2 (2324-byte) tracks,
and images carrying more than one data track are recognised and refused
by name.

Written from Aaru's Alcohol120 reader (structs.cs, Read.cs), Jonathan
Levin's notes and the Format:Alcohol_MDS_and_MDF page on
reverseengineering.wiki; no code is carried over and no dependency is
added.
"""

import struct

SECTOR = 2048
RAW = 2352
SUBCHANNEL = 96

SIGNATURE = b"MEDIA DESCRIPTOR"
MAX_VERSION = 1

# track modes, from Aaru's enums
MODE_NO_DATA = 0x00
MODE_DVD = 0x02
MODE_AUDIO = (0xA9, 0xE9)
MODE_1 = (0xAA, 0xEA)
MODE_2 = 0xAB
MODE_2_F1 = (0xEC, 0xAC)
MODE_2_F2 = (0xED, 0xAD)
SUB_NONE = 0x00
SUB_INTERLEAVED = 0x08


class MdfError(ValueError):
    """An .mdf/.mds this reader won't read: damaged, or a form it lacks."""


def is_mds(data) -> bool:
    """Whether data looks like an .mds descriptor."""
    return len(data) >= 88 and data[:16] == SIGNATURE and data[16] <= MAX_VERSION


def _sessions(d: bytes) -> list[dict]:
    sessions, = struct.unpack_from("<H", d, 20)
    session_off, = struct.unpack_from("<I", d, 80)
    if session_off == 0 and sessions:
        raise MdfError("the .mds descriptor is damaged (its session table is at offset 0)")
    if session_off + sessions * 24 > len(d):
        raise MdfError("the .mds descriptor is damaged (its session table runs off its end)")
    out = []
    for i in range(sessions):
        at = session_off + i * 24
        sstart, send, seq, all_blocks, _non_track, first, last, _unk, track_off = \
            struct.unpack_from("<iiHBBHHII", d, at)
        out.append({"start": sstart, "end": send, "sequence": seq,
                    "allBlocks": all_blocks, "first": first, "last": last,
                    "trackOffset": track_off})
    return out


def _tracks(d: bytes) -> list[dict]:
    tracks = []
    for session in _sessions(d):
        at = session["trackOffset"]
        if at + session["allBlocks"] * 80 > len(d):
            raise MdfError("the .mds descriptor is damaged (a track table runs off its end)")
        for _ in range(session["allBlocks"]):
            (mode, sub, _adr, _tno, point, _mn, _sc, _fr, _zero,
             _pmin, _psec, _pframe) = struct.unpack_from("<12B", d, at)
            extra_off, = struct.unpack_from("<I", d, at + 12)
            start_lba, = struct.unpack_from("<I", d, at + 36)
            start_off, = struct.unpack_from("<Q", d, at + 40)
            footer_off, = struct.unpack_from("<I", d, at + 52)
            tracks.append({"mode": mode, "sub": sub, "point": point,
                           "startLba": start_lba, "startOffset": start_off,
                           "extraOffset": extra_off, "footerOffset": footer_off})
            at += 80
    return tracks


def _extra(d: bytes, offset: int) -> tuple[int, int]:
    pregap, sectors = struct.unpack_from("<II", d, offset)
    return pregap, sectors


def data_track(mds: bytes) -> dict:
    """The one data track this image holds, with its size and gap."""
    data = []
    for t in _tracks(mds):
        if t["point"] >= 0xA0:                    # the A0/A1/A2 TOC markers
            continue
        if t["mode"] in MODE_1 + MODE_2_F1:
            data.append(t)
        elif t["mode"] == MODE_DVD:
            raise MdfError("the .mdf image is unsupported (it is a DVD, not a CD)")
        elif t["mode"] in (MODE_2, *MODE_2_F2):
            raise MdfError(f"the .mdf image is unsupported (Mode 2 sector size "
                           f"{2336 if t['mode'] == MODE_2 else 2324})")
    if len(data) > 1:
        raise MdfError(f"the .mdf image is unsupported (it carries {len(data)} data tracks)")
    if not data:
        if any(t["mode"] in MODE_AUDIO for t in _tracks(mds)):
            raise MdfError("the .mdf image holds no data track (it is audio only)")
        raise MdfError("the .mdf image is damaged (it holds no data track)")
    t = data[0]
    if t["extraOffset"]:
        pregap, sectors = _extra(mds, t["extraOffset"])
    else:
        pregap, sectors = 0, 0
        for s in _sessions(mds):
            if s["first"] <= t["point"] <= s["last"]:
                sectors = s["end"] - t["startLba"] + 1
                break
    if sectors <= 0:
        raise MdfError("the .mdf image is damaged (its data track holds no sectors)")
    t["pregap"], t["sectors"] = pregap, sectors
    return t


def read_image(mdf: bytes, mds: bytes) -> bytes:
    """The data track's user sectors, as a plain 2048-byte-sector stream."""
    t = data_track(mds)
    if t["sub"] not in (SUB_NONE, SUB_INTERLEAVED):
        raise MdfError(f"the .mdf image is unsupported (subchannel mode {t['sub']})")
    stride = RAW + (SUBCHANNEL if t["sub"] == SUB_INTERLEAVED else 0)
    user_off = 16 if t["mode"] in MODE_1 else 24
    if t["startOffset"] > len(mdf):
        raise MdfError("the .mdf image is damaged (its data track starts past its end)")
    need = t["startOffset"] + (t["sectors"] - 1) * stride + user_off + SECTOR
    if need > len(mdf):
        raise MdfError("the .mdf image is damaged (its data track runs past the .mdf)")
    out = bytearray(t["sectors"] * SECTOR)
    at = t["startOffset"]
    for i in range(t["sectors"]):
        out[i * SECTOR:(i + 1) * SECTOR] = mdf[at + user_off:at + user_off + SECTOR]
        at += stride
    return bytes(out)
