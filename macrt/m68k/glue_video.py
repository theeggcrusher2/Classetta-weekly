"""
The screen's video driver, on a Mac II with Color QuickDraw: the main
device's gdRefNum, VIDEO_DRIVER, as a video card's driver is (Designing
Cards and Drivers for the Macintosh Family, its video drivers' calls;
Inside Macintosh V-122).  Its modes are a card's: $80 for 1 bit a pixel,
$81 for 2 and so on to $85 for 32, each with one page.  The card has the
memory for the depth macrt started with, so it has the modes as deep as
that or shallower.

A program asks which modes there are (GetPages), sets one (SetMode, or
InitGDevice, which sets the device's mode through its driver, or the
Palette Manager's SetDepth), and puts the old one back when it's done.
A new mode is a new depth for the whole screen: the device's PixMap and
colour table, the buffer, and each cGrafPort on the screen
(System.set_depth).  SetEntries changes the colours as the hardware
shows them, keeping the table's seed, and in grey mode (SetGray) shows
each as its luminance.  The driver keeps a gamma table, a linear one
to start with: GetGamma gives a pointer to it, and SetGamma puts a copy
of another in its place, or a linear one again for NIL (Designing Cards
and Drivers, the gamma table data structure).  The colours shown aren't
corrected by it.

    VDPageInfo          csMode (word), csData (long), csPage (word), csBaseAddr
    VDSetEntryRecord    csTable (a ColorSpec array), csStart, csCount
    VDGrayRecord, VDFlagRecord, VDDefModeRec: a byte
    VDGammaRecord       csGTable, a pointer to a gamma table
    GammaTbl            gVersion, gType, gFormulaSize, gChanCnt, gDataCnt,
                        gDataWidth (words), then the formula data and each
                        channel's gDataCnt values

A Control or Status call's csParam holds a pointer to its record.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ..memory import ADDRESS_MASK
from ..toolbox.color import luminance
from ..toolbox.files import FileError
from ..toolbox.gdevice import DEFAULT_MODE, DEPTH_MODES, GDevice, deref
from .dispatch import trap
from .glue_color import _new_colors

noErr, paramErr, controlErr, statusErr, unitEmptyErr = 0, -50, -17, -18, -22
cDepthErr = -157
csCode, csParam = 26, 28
gdDevType = 0

DEPTHS = {DEFAULT_MODE + m: d for d, m in DEPTH_MODES.items()}     # a mode's bits a pixel
# A linear gamma table: version 0, no formula data, one channel for all
# three of 256 byte-wide values, each the value it corrects.
LINEAR_GAMMA = struct.pack(">6h", 0, 0, 0, 1, 256, 8) + bytes(range(256))
MAX_GAMMA_VALUES = 4096         # a channel's, as many as macrt takes


@dataclass
class Card:
    """What the driver keeps of its own."""
    gray: bool = False          # SetGray: colours shown as their luminance
    interrupts: bool = True     # SetInterrupt: the card's VBL interrupts
    default_mode: int = DEFAULT_MODE    # SetDefaultMode's sResource ID
    gamma: int = 0              # its gamma table, once a program has asked


def _card(t) -> Card:
    card = getattr(t.system, "video_card", None)
    if card is None:
        card = t.system.video_card = Card()
    return card


def modes(system) -> list[int]:
    """The modes the screen can have: its depths, as far as they fit."""
    if system.devices is None:
        return []
    return [mode for mode, depth in DEPTHS.items() if system.fits(depth)]


def _mode(system) -> int:
    return DEFAULT_MODE + DEPTH_MODES[system.depth]


def set_mode(t, mode: int, gray: bool | None = None) -> bool:
    """Set the card to mode, with its colours grey or not if gray is
    given: the screen, its device and its ports to the mode's depth."""
    card = _card(t)
    if mode not in modes(t.system):
        return False
    if gray is not None:
        card.gray = gray
    return t.system.set_depth(DEPTHS[mode], card.gray)


# =========================================================================
# The driver's calls, from the File Manager's Control and Status
# =========================================================================
def _record(t, pb: int) -> int:
    return t.mem.r32(pb + csParam) & ADDRESS_MASK


def _page_info(t, p: int, mode: int | None = None) -> None:
    """Fill in a VDPageInfo: the mode, if it's given, page 0 and its base."""
    m = t.mem
    if mode is not None:
        m.w16(p, mode)
    m.w16(p + 6, 0)
    m.w32(p + 8, t.system.screen_bits.base)


def control(t, pb: int) -> None:
    m, system = t.mem, t.system
    if system.devices is None:
        raise FileError(unitEmptyErr)
    code, p = m.r16s(pb + csCode), _record(t, pb)
    card = _card(t)
    if code == 0:                                       # Reset: the first mode, page 0
        if not set_mode(t, DEFAULT_MODE):
            raise FileError(controlErr)
        _page_info(t, p, DEFAULT_MODE)
    elif code == 2:                                     # SetMode
        mode, page = m.r16(p), m.r16s(p + 6)
        if page != 0 or (mode != _mode(system) and not set_mode(t, mode)):
            raise FileError(controlErr)
        _page_info(t, p)
    elif code == 3:                                     # SetEntries
        _set_entries(t, p)
    elif code == 5:                                     # GrayPage
        if m.r16s(p + 6) != 0:
            raise FileError(controlErr)
        _gray_page(t)
    elif code == 6:                                     # SetGray
        card.gray = bool(m.r8(p))
    elif code == 7:                                     # SetInterrupt: 0 to enable
        card.interrupts = not m.r8(p)
    elif code == 9:                                     # SetDefaultMode
        card.default_mode = m.r8(p)
    elif code == 4:                                     # SetGamma
        _set_gamma(t, m.r32(p) & ADDRESS_MASK)
    elif code != 1:                                     # KillIO: nothing to do
        raise FileError(controlErr)                     # DirectSetEntries, on no direct mode


def status(t, pb: int) -> None:
    m, system = t.mem, t.system
    if system.devices is None:
        raise FileError(unitEmptyErr)
    code, p = m.r16s(pb + csCode), _record(t, pb)
    card = _card(t)
    if code == 2:                                       # GetMode
        _page_info(t, p, _mode(system))
    elif code == 3:                                     # GetEntries
        _get_entries(t, p)
    elif code == 4:                                     # GetPages: of csMode
        if m.r16(p) not in modes(system):
            raise FileError(statusErr)
        m.w16(p + 6, 1)
    elif code == 5:                                     # GetBaseAddr: of csPage
        if m.r16s(p + 6) != 0:
            raise FileError(statusErr)
        m.w32(p + 8, system.screen_bits.base)
    elif code == 6:                                     # GetGray
        m.w8(p, card.gray)
    elif code == 7:                                     # GetInterrupt: 0 if enabled
        m.w8(p, not card.interrupts)
    elif code == 8:                                     # GetGamma
        if not card.gamma:
            _set_gamma(t, 0)
        m.w32(p, card.gamma)
    elif code == 9:                                     # GetDefaultMode
        m.w8(p, card.default_mode)
    else:
        raise FileError(statusErr)


def _set_gamma(t, table: int) -> None:
    """The driver's gamma table a copy of the one at table, in the
    System heap, or a linear one for NIL: an error for a version but 0,
    a number of channels but 1 or 3, or values more than a byte wide or
    more than macrt takes."""
    m, card = t.mem, _card(t)
    data = LINEAR_GAMMA
    if table:
        version, _, formula, channels, count, width = struct.unpack(">6h", m.read(table, 12))
        if (version or formula < 0 or channels not in (1, 3)
                or not 0 < count <= MAX_GAMMA_VALUES or not 0 < width <= 8):
            raise FileError(controlErr)
        data = m.read(table, 12 + formula + channels * count)
    heap = t.system.sys_heap
    p = heap.new_ptr(len(data))
    if not p:
        raise FileError(controlErr)
    m.write(p, data)
    if card.gamma:
        heap.dispose_ptr(card.gamma)
    card.gamma = p


def _table(t) -> int:
    """The main device's colour table, as a pointer."""
    devices = t.system.devices
    pm = devices.pixmap(devices.main)
    return deref(t.mem, pm.pm_table) if pm is not None and t.system.depth <= 8 else 0


def _places(t, p: int) -> list[tuple[int, int]]:
    """A VDSetEntryRecord's entries: each ColorSpec's address and the
    index it's for, csCount + 1 of them from csStart, or with csStart -1
    those their values name; ctSize + 1 the table's entries."""
    m = t.mem
    specs, start, count = m.r32(p) & ADDRESS_MASK, m.r16s(p + 4), m.r16s(p + 6)
    ct = _table(t)
    size = m.r16s(ct + 6) + 1 if ct else 0
    places = [(specs + 8 * i, m.r16s(specs + 8 * i) if start < 0 else start + i)
              for i in range(count + 1)]
    if not ct or count < 0 or any(not 0 <= at < size for _, at in places):
        raise FileError(paramErr)
    return places


def _set_entries(t, p: int) -> None:
    m = t.mem
    ct, gray = _table(t), _card(t).gray
    for spec, at in _places(t, p):
        r, g, b = struct.unpack(">HHH", m.read(spec + 2, 6))
        if gray:
            r = g = b = luminance(r, g, b)
        m.write(ct + 8 + 8 * at + 2, struct.pack(">HHH", r, g, b))
    _new_colors(t, t.system.devices.main, ct, seed=False)


def _get_entries(t, p: int) -> None:
    m = t.mem
    ct = _table(t)
    for spec, at in _places(t, p):
        m.w16(spec, at)
        m.write(spec + 2, m.read(ct + 8 + 8 * at + 2, 6))


def _gray_page(t) -> None:
    """Fill the screen with grey: every other pixel black, by rows."""
    system = t.system
    bits, depth = system.screen_bits, system.depth
    w, h = bits.bounds.width, bits.bounds.height
    white, black = ((0, (1 << depth) - 1) if depth <= 8
                    else ((1 << (15 if depth == 16 else 24)) - 1, 0))
    rows = []
    for phase in (0, 1):
        v = 0
        for x in range(w):
            v = v << depth | (black if (x + phase) & 1 else white)
        rows.append((v << bits.row_bytes * 8 - w * depth).to_bytes(bits.row_bytes, "big"))
    t.mem.write(bits.base, b"".join(rows[y & 1] for y in range(h)))
    system.screen.look()


# =========================================================================
# The Palette Manager's depth calls (Inside Macintosh VI-21)
# =========================================================================
def _is_main(t, gdh: int) -> bool:
    devices = t.system.devices
    return devices is not None and gdh & ADDRESS_MASK == devices.main & ADDRESS_MASK


@trap
def HasDepth(t, gdh, depth, which_flags, flags):
    """The mode for a depth the device can be, 0 if it can't."""
    if not _is_main(t, gdh) or depth not in DEPTH_MODES:
        return 0
    mode = DEFAULT_MODE + DEPTH_MODES[depth]
    colour = which_flags & 1 << gdDevType and flags & 1 << gdDevType
    return mode if mode in modes(t.system) and not (colour and depth == 1) else 0


@trap
def SetDepth(t, gdh, depth, which_flags, flags):
    """Set the device to a depth (or a mode), in colour or grey as flags'
    gdDevType says, if which_flags has it."""
    if not _is_main(t, gdh):
        return paramErr
    mode = depth if depth >= DEFAULT_MODE else DEFAULT_MODE + DEPTH_MODES.get(depth, -DEFAULT_MODE)
    gray = not flags & 1 << gdDevType if which_flags & 1 << gdDevType else None
    return noErr if set_mode(t, mode, gray) else cDepthErr


@trap
def RestoreDeviceClut(t, gdh):
    """The device's colour table back to the standard one for its depth,
    or every device's, given NIL (Inside Macintosh: Advanced Color
    Imaging, the Palette Manager): only the main screen has one to
    change, as its driver does when it's set to the mode it's in."""
    if t.system.devices is not None and (not gdh or _is_main(t, gdh)):
        set_mode(t, _mode(t.system))


@trap
def InitGDevice(t, ref_num, mode, gdh):
    """Set a device to a mode, through its driver: the main screen's
    driver changes its depth (the colour table the standard one again);
    another device has no driver to set, so it's only the record's mode
    that changes, as for a device without one."""
    if _is_main(t, gdh):
        set_mode(t, mode)
        return
    p = deref(t.mem, gdh) if t.system.devices is not None else 0
    if p:
        GDevice(t.mem, p).mode = mode
