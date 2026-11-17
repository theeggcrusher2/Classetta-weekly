"""
The Slot Manager (Inside Macintosh V and Inside Macintosh: Devices, The
Slot Manager): the routine's selector in D0, a SpBlock in A0, the result
in D0.  On a Mac II there is one card, the screen's, in VIDEO_SLOT, and
the Slot Manager reads what's on it from its declaration ROM, as programs
do through it: the CyberFlix demos look for the screen's sResource to
find which modes it has, and Spaceship Warlock looks for it too.

The card's declaration ROM is macrt's own, laid out as Designing Cards
and Drivers for the Macintosh Family (third edition, NuBus Card Firmware)
lays out a card's: a format block at the top, and the sResource directory
it leads to, with two sResources in it.

    1       the board sResource: its sRsrcType (CatBoard, TypBoard, 0, 0),
            its name and its BoardId, 0, which is macrt's own
    $80     the screen's: its sRsrcType (catDisplay, typVideo, drSwApple,
            and a DrHW of macrt's own, 0, as the 'scrn' resource gives),
            its name, which is its driver's (glue_video), its sRsrcHWDevId,
            and a mode list for each mode the driver has: the mode's video
            parameters (a VPBlock), its one page and its device type

Its bytes are on all four byte lanes, so an offset is the bytes between,
and it's a block in the system heap, made the first time a program asks
the Slot Manager for something, so a program that never does keeps its
heap as it was.  The card's memory is the screen buffer: its device base
is the screen's pixels, and each mode's start at it.

The Slot Resource Table has the two sResources, the screen's with its
driver's reference number.  A program can disable one, delete one and
insert it again; the card's parameter RAM is the card's while macrt runs.
The routines only the Operating System uses, and the ones that load and
run a card's code (SGetDriver, SExec, PrimaryInit), aren't here: the
card's driver is macrt's.

    SpBlock     spResult, spsPointer, spSize, spOffsetData (longs),
                spIOFileName, spsExecPBlk, spParamData, spMisc,
                spReserved (longs), spIOReserved, spRefNum, spCategory,
                spCType, spDrvrSW, spDrvrHW (words), spTBMask, spSlot, spID,
                spExtDev, spHwDev, spByteLanes, spFlags, spKey (bytes)
    sInfo       siDirPtr, siInitStatusA, siInitStatusV, siState,
                siCPUByteLanes, siTopOfROM, siStatusFlags, siTOConstant,
                siReserved: Inside Macintosh V's, 16 bytes
    FHeaderRec  fhDirOffset, fhLength, fhCRC, fhROMRev, fhFormat, fhTstPat,
                fhReserved, fhByteLanes: the format block, as it is
    SPRAMRecord boardID, then six bytes for the card's own use
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ..toolbox.gdevice import FIXED_72, RGB_DIRECT, VIDEO_DR_HW, VIDEO_DRIVER, VIDEO_SLOT
from ..toolbox.memmgr import noErr
from . import glue_video
from .dispatch import trap
from .glue_os import _zone

# SpBlock
spResult, spsPointer, spSize = 0, 4, 8
spParamData = 24
spIOReserved, spRefNum, spCategory, spCType, spDrvrSW, spDrvrHW = 36, 38, 40, 42, 44, 46
spTBMask, spSlot, spID, spExtDev, spHwDev = 48, 49, 50, 51, 52
fAll, fOneSlot, fNext = 1, 2, 4                 # spParamData's flags

# The routines' selectors
sReadByte, sReadWord, sReadLong, sGetCString = 0, 1, 2, 3
sGetBlock, sFindStruct, sReadStruct, sVersion = 5, 6, 7, 8
sSetsRsrcState, sInsertSRTRec, sGetsRsrc, sGetTypesRsrc = 9, 0x0A, 0x0B, 0x0C
sReadInfo, sReadPRAMRec, sPutPRAMRec, sReadFHeader = 0x10, 0x11, 0x12, 0x13
sNextsRsrc, sNextTypesRsrc, sRsrcInfo = 0x14, 0x15, 0x16
sCkCardStat, sReadDrvrName, sFindDevBase, sFindBigDevBase = 0x18, 0x19, 0x1B, 0x1C
sCardChanged, sDeleteSRTRec = 0x22, 0x31

smEmptySlot = -300
smsPointerNil = -335
smSelOOBErr = -338
smNewPErr = -339
smNoMoresRsrcs = -344
smBadsPtrErr = -346
smRecNotFnd = -351

# sResource entries' IDs, and the board's and the video sResource's own
sRsrcType, sRsrcName, sRsrcHWDevId = 1, 2, 8
BoardId = 32
mVidParams, mPageCnt, mDevType = 1, 3, 4
END_OF_LIST = 0xFF
BOARD, VIDEO = 1, 0x80                          # the card's sResources' IDs
CAT_BOARD, CAT_DISPLAY, TYP_VIDEO, DR_SW_APPLE = 1, 3, 1, 1
BOARD_ID = 0                                    # macrt's own
HW_DEV_ID = 1
CARD_NAME = b"macrt Video Card"
VIDEO_NAME = b"Display_Video_Apple_TFB"         # its driver's name, without the period
SINFO_SIZE = 16
TIMEOUT = 100                                   # siTOConstant, the retries' default
stateSInit = 4                                  # siState: its initialisation done
# The format block's
ROM_REVISION, APPLE_FORMAT, TEST_PATTERN, ALL_BYTE_LANES = 1, 1, 0x5A932BC7, 0x0F
FHEADER_SIZE = 20
CRC_FIELD = 8                                   # where fhCRC is in it


@dataclass
class Slot:
    """An sResource in the Slot Resource Table."""
    sptr: int                                   # its sResource list
    refnum: int = 0                             # its driver's
    enabled: bool = True


@dataclass
class Card:
    """The card's declaration ROM and what the Slot Manager keeps of it."""
    rom: int                                    # where its ROM starts: its directory
    length: int
    lists: dict[int, int]                       # each sResource's list, by ID
    srt: dict[tuple[int, int, int], Slot]       # by slot, sResource ID, extDev
    pram: bytes = bytes(6)                      # its parameter RAM's vendor bytes


# =========================================================================
# The declaration ROM
# =========================================================================
class _Image:
    """A declaration ROM as it's made: its bytes, where its labels are,
    and the entries whose offsets are to labels."""

    def __init__(self):
        self.data = bytearray()
        self.labels: dict[str, int] = {}
        self.offsets: list[tuple[int, str]] = []

    def label(self, name: str) -> None:
        self.labels[name] = len(self.data)

    def entry(self, entry_id: int, to: str | None = None, data: int = 0) -> None:
        """An entry: its ID and an offset to a label, or its data."""
        if to is not None:
            self.offsets.append((len(self.data), to))
        self.data += struct.pack(">L", entry_id << 24 | data & 0xFFFFFF)

    def end(self) -> None:
        self.entry(END_OF_LIST)

    def put(self, name: str, data: bytes) -> None:
        self.label(name)
        self.data += data + bytes(len(data) & 1)

    def done(self) -> bytes:
        for at, name in self.offsets:
            self.data[at + 1:at + 4] = ((self.labels[name] - at) & 0xFFFFFF).to_bytes(3, "big")
        return bytes(self.data)


def _vp_block(system, depth: int) -> bytes:
    """A mode's VPBlock (Designing Cards and Drivers, NuBus Card Driver
    Design): as its PixMap would be, but for the offset from the card's
    base to the page, here none."""
    r = system.screen_bits.bounds
    direct = depth > 8
    count, size = (3, 5 if depth == 16 else 8) if direct else (1, depth)
    return struct.pack(">LH4hhhLLLhhhhL", 0, system.row_bytes_for(depth),
                       r.top, r.left, r.bottom, r.right, 1, 0, 0, FIXED_72, FIXED_72,
                       RGB_DIRECT if direct else 0, depth, count, size, 0)


def _image(system) -> tuple[bytes, dict[str, int]]:
    """The card's declaration ROM, and where its sResources' lists are."""
    image = _Image()
    modes = glue_video.modes(system)
    image.label("directory")
    image.entry(BOARD, "board")
    image.entry(VIDEO, "video")
    image.end()
    image.label("board")
    image.entry(sRsrcType, "board type")
    image.entry(sRsrcName, "board name")
    image.entry(BoardId, data=BOARD_ID)
    image.end()
    image.label("video")
    image.entry(sRsrcType, "video type")
    image.entry(sRsrcName, "video name")
    image.entry(sRsrcHWDevId, data=HW_DEV_ID)
    for mode in modes:
        image.entry(mode, f"mode {mode}")
    image.end()
    for mode in modes:
        depth = glue_video.DEPTHS[mode]
        image.label(f"mode {mode}")
        image.entry(mVidParams, f"parameters {mode}")
        image.entry(mPageCnt, data=1)
        image.entry(mDevType, data=2 if depth > 8 else 0)      # directType, clutType
        image.end()
    image.put("board type", struct.pack(">4H", CAT_BOARD, 0, 0, 0))
    image.put("video type", struct.pack(">4H", CAT_DISPLAY, TYP_VIDEO, DR_SW_APPLE, VIDEO_DR_HW))
    image.put("board name", CARD_NAME + b"\0")
    image.put("video name", VIDEO_NAME + b"\0")
    for mode in modes:
        vp = _vp_block(system, glue_video.DEPTHS[mode])
        image.put(f"parameters {mode}", struct.pack(">L", 4 + len(vp)) + vp)
    rom = image.done()
    header = len(rom)
    length = header + FHEADER_SIZE
    fh = bytearray(struct.pack(">LLLBBLBB", -header & 0xFFFFFF, length, 0, ROM_REVISION,
                               APPLE_FORMAT, TEST_PATTERN, 0, ALL_BYTE_LANES))
    fh[CRC_FIELD:CRC_FIELD + 4] = struct.pack(">L", _crc(rom + bytes(fh), header + CRC_FIELD))
    return rom + bytes(fh), {name: image.labels[name] for name in ("directory", "board", "video")}


def _crc(rom: bytes, crc_at: int) -> int:
    """The format block's CRC (Designing Cards and Drivers, NuBus Card
    Firmware): each byte from the bottom added to the sum after it's
    rotated left a bit, the CRC's own four bytes counting as none."""
    total = 0
    for i, b in enumerate(rom):
        total = (total << 1 | total >> 31) & 0xFFFFFFFF
        if not crc_at <= i < crc_at + 4:
            total = (total + b) & 0xFFFFFFFF
    return total


def _card(t) -> Card | None:
    """The screen's card, its ROM made the first time it's asked for;
    None on a Mac without slots."""
    system = t.system
    card = getattr(system, "slot_card", None)
    if card is None and system.devices is not None:
        data, labels = _image(system)
        rom = system.sys_heap.new_ptr(len(data))
        if not rom:
            return None
        t.mem.write(rom, data)
        lists = {BOARD: rom + labels["board"], VIDEO: rom + labels["video"]}
        card = system.slot_card = Card(
            rom, len(data), lists,
            {(VIDEO_SLOT, BOARD, 0): Slot(lists[BOARD]),
             (VIDEO_SLOT, VIDEO, 0): Slot(lists[VIDEO], VIDEO_DRIVER)})
    return card


# =========================================================================
# Reading the ROM
# =========================================================================
def _in_rom(card: Card, p: int) -> bool:
    return card.rom <= p < card.rom + card.length


def _find(t, card: Card, sptr: int, entry_id: int) -> int | None:
    """The entry with an ID in the list at sptr, None if it has none."""
    m = t.mem
    p = sptr
    while _in_rom(card, p + 3):
        found = m.r8(p)
        if found == END_OF_LIST or found > entry_id:
            return None
        if found == entry_id:
            return p
        p += 4
    return None


def _offset(t, entry: int) -> int:
    """Where an entry's offset leads."""
    off = t.mem.r32(entry) & 0xFFFFFF
    return entry + (off - 0x1000000 if off & 0x800000 else off)


def _cstring(t, card: Card, p: int) -> bytes:
    m = t.mem
    end = p
    while _in_rom(card, end) and m.r8(end):
        end += 1
    return m.read(p, end - p)


def _type(t, card: Card, sptr: int) -> tuple[int, ...]:
    """An sResource's sRsrcType: Category, cType, DrSW and DrHW."""
    entry = _find(t, card, sptr, sRsrcType)
    return struct.unpack(">4H", t.mem.read(_offset(t, entry), 8)) if entry else (0,) * 4


def _matches(kind: tuple, wanted: tuple, mask: int) -> bool:
    """Whether an sRsrcType is one asked for, but for the fields that
    spTBMask's bits leave out: bit 0 DrHW, 1 DrSW, 2 cType, 3 Category."""
    return all(mask >> (3 - i) & 1 or k == w for i, (k, w) in enumerate(zip(kind, wanted)))


def _hw_dev(t, card: Card, sptr: int) -> int:
    entry = _find(t, card, sptr, sRsrcHWDevId)
    return t.mem.r8(entry + 3) if entry is not None else 0


def _give(t, card: Card, pb: int, key: tuple[int, int, int], slot: Slot) -> None:
    """What the routines that find an sResource say of it."""
    m = t.mem
    m.w32(pb + spsPointer, slot.sptr)
    m.w16(pb + spIOReserved, 0)
    m.w16(pb + spRefNum, slot.refnum)
    for at, value in zip((spCategory, spCType, spDrvrSW, spDrvrHW), _type(t, card, slot.sptr)):
        m.w16(pb + at, value)
    m.w8(pb + spSlot, key[0])
    m.w8(pb + spID, key[1])
    m.w8(pb + spExtDev, key[2])
    m.w8(pb + spHwDev, _hw_dev(t, card, slot.sptr))


def _key(t, pb: int) -> tuple[int, int, int]:
    m = t.mem
    return m.r8(pb + spSlot), m.r8(pb + spID), m.r8(pb + spExtDev)


def _search(t, card: Card, pb: int, nxt: bool, every: bool, one_slot: bool,
            kind: tuple | None = None, mask: int = 0) -> int:
    """Find the sResource the SpBlock names, or the one after it, of a
    type if kind is given: in the next slots too unless one_slot, and
    disabled ones only if every."""
    key = _key(t, pb)
    for k in sorted(card.srt):
        if (k <= key if nxt else k != key) or one_slot and k[0] != key[0]:
            continue
        slot = card.srt[k]
        if not (slot.enabled or every):
            continue
        if kind is not None and not _matches(_type(t, card, slot.sptr), kind, mask):
            continue
        _give(t, card, pb, k, slot)
        return noErr
    return smNoMoresRsrcs


def _kind(t, pb: int) -> tuple:
    return tuple(t.mem.r16(pb + at) for at in (spCategory, spCType, spDrvrSW, spDrvrHW))


def _read(t, card: Card, pb: int, selector: int) -> int:
    """SReadByte to SReadStruct: from the list at spsPointer."""
    m = t.mem
    sptr = m.r32(pb + spsPointer) & t.mem.system.addr_mask
    if not sptr:
        return smsPointerNil
    if not _in_rom(card, sptr):
        return smBadsPtrErr
    if selector == sReadStruct:
        size = m.r32(pb + spSize)
        if not _in_rom(card, sptr + max(size, 1) - 1):
            return smBadsPtrErr
        m.write(m.r32(pb + spResult), m.read(sptr, size))
        return noErr
    entry = _find(t, card, sptr, m.r8(pb + spID))
    if entry is None:
        return smNoMoresRsrcs
    if selector == sReadByte:
        m.w32(pb + spResult, m.r8(entry + 3))
        return noErr
    if selector == sReadWord:
        m.w32(pb + spResult, m.r16(entry + 2))
        return noErr
    at = _offset(t, entry)
    if not _in_rom(card, at):
        return smBadsPtrErr
    if selector == sReadLong:
        m.w32(pb + spResult, m.r32(at))
        return noErr
    if selector == sFindStruct:
        m.w32(pb + spsPointer, at)
        return noErr
    if selector == sGetCString:
        data = _cstring(t, card, at) + b"\0"
    elif selector == sGetBlock:                 # what's in the sBlock, after its size
        end = card.rom + card.length
        data = m.read(at + 4, max(min(m.r32(at) - 4, end - at - 4), 0))
    else:
        return smSelOOBErr
    p = _zone(t, False).new_ptr(max(len(data), 1))
    if not p:
        return smNewPErr
    m.write(p, data)
    m.w32(pb + spResult, p)
    return noErr


# =========================================================================
# The slots
# =========================================================================
def _sinfo(t, card: Card) -> bytes:
    top = card.rom + card.length - 1
    return struct.pack(">LhhBBBBHBx", card.rom, 0, 0, stateSInit, ALL_BYTE_LANES,
                       top & 0xFF, 0, TIMEOUT, 0)


def _slot_call(t, card: Card, pb: int, selector: int) -> int:
    """The routines that ask about a slot, and the card in it."""
    m = t.mem
    slot = m.r8(pb + spSlot)
    if slot != VIDEO_SLOT:
        return smEmptySlot
    result = m.r32(pb + spResult) & t.mem.system.addr_mask
    if selector == sReadInfo:
        m.write(result, _sinfo(t, card))
    elif selector == sReadFHeader:
        m.write(result, m.read(card.rom + card.length - FHEADER_SIZE, FHEADER_SIZE))
    elif selector == sReadPRAMRec:
        m.write(result, struct.pack(">H", BOARD_ID) + card.pram)
    elif selector == sPutPRAMRec:               # all but the board's ID
        card.pram = m.read((m.r32(pb + spsPointer) & t.mem.system.addr_mask) + 2, 6)
    elif selector == sCardChanged:
        m.w32(pb + spResult, 0)
    elif selector in (sFindDevBase, sFindBigDevBase):
        key = (slot, m.r8(pb + spID), 0)
        if key not in card.srt:
            return smNoMoresRsrcs
        m.w32(pb + spResult, t.system.screen_bits.base)
    return noErr                                # sCkCardStat: it's working


def _table_call(t, card: Card, pb: int, selector: int) -> int:
    """The routines that change the Slot Resource Table."""
    m = t.mem
    key = _key(t, pb)
    if selector == sSetsRsrcState:
        if key not in card.srt:
            return smNoMoresRsrcs
        card.srt[key].enabled = not m.r32(pb + spParamData)
    elif selector == sDeleteSRTRec:
        if card.srt.pop(key, None) is None:
            return smRecNotFnd
    else:                                       # sInsertSRTRec, from the card's directory
        if key[0] != VIDEO_SLOT or key[2] or key[1] not in card.lists:
            return smNoMoresRsrcs
        card.srt[key] = Slot(card.lists[key[1]], m.r16s(pb + spRefNum),
                             not m.r32(pb + spParamData))
    return noErr


@trap
def SlotManager(t, pb, selector):
    m = t.mem
    selector &= 0xFFFF
    card = _card(t)
    if card is None:
        # No slots: no cards, and no sResources to find.
        if selector <= sReadStruct:
            return smBadsPtrErr if m.r32(pb + spsPointer) else smsPointerNil
        if selector in (sNextsRsrc, sNextTypesRsrc, sRsrcInfo, sReadDrvrName, sGetsRsrc,
                        sGetTypesRsrc):
            return smNoMoresRsrcs
        if selector in (sReadInfo, sReadPRAMRec, sPutPRAMRec, sReadFHeader, sCkCardStat,
                        sFindDevBase, sFindBigDevBase, sCardChanged):
            return smEmptySlot
        return smSelOOBErr
    if selector <= sReadStruct and selector != 4:
        return _read(t, card, pb, selector)
    if selector == sVersion:                    # System 7's, in RAM
        m.w32(pb + spResult, 1)
        m.w32(pb + spsPointer, 0)
        return noErr
    if selector in (sSetsRsrcState, sInsertSRTRec, sDeleteSRTRec):
        return _table_call(t, card, pb, selector)
    if selector == sGetsRsrc:
        flags = m.r32(pb + spParamData)
        err = _search(t, card, pb, bool(flags & fNext), bool(flags & fAll),
                      bool(flags & fOneSlot))
    elif selector == sGetTypesRsrc:
        flags = m.r32(pb + spParamData)
        err = _search(t, card, pb, True, bool(flags & fAll), bool(flags & fOneSlot),
                      _kind(t, pb), m.r8(pb + spTBMask))
    elif selector == sNextsRsrc:
        return _search(t, card, pb, True, False, False)
    elif selector == sNextTypesRsrc:
        return _search(t, card, pb, True, False, False, _kind(t, pb), m.r8(pb + spTBMask))
    elif selector == sRsrcInfo:
        return _search(t, card, pb, False, False, True)
    elif selector == sReadDrvrName:
        slot = card.srt.get(_key(t, pb)[:2] + (0,))
        if slot is None or not slot.enabled:
            return smNoMoresRsrcs
        entry = _find(t, card, slot.sptr, sRsrcName)
        if entry is None:
            return smNoMoresRsrcs
        name = b"." + _cstring(t, card, _offset(t, entry))[:254]
        m.write(m.r32(pb + spResult) & t.mem.system.addr_mask, bytes([len(name)]) + name)
        return noErr
    elif selector in (sReadInfo, sReadPRAMRec, sPutPRAMRec, sReadFHeader, sCkCardStat,
                      sFindDevBase, sFindBigDevBase, sCardChanged):
        return _slot_call(t, card, pb, selector)
    else:
        return smSelOOBErr
    if err == noErr:                            # whether the one found is enabled
        m.w32(pb + spParamData, 0 if card.srt[_key(t, pb)].enabled else 1)
    return err
