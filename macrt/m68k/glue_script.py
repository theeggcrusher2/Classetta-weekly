"""
The Script Manager (Inside Macintosh V-293), through ScriptUtil ($A8B5),
on a Mac with only the Roman script.  macrt.toolbox.script has the
character tables, word and line breaks and the measuring; widths are the
current port's, as TextWidth gives them.
"""

from __future__ import annotations

import datetime
import struct

from ..memory import from_mac
from ..toolbox import intl
from ..toolbox import lowmem
from ..toolbox import numformat as nf
from ..toolbox import sane
from ..toolbox import script as sm
from ..toolbox.memmgr import noErr
from ..ui import keymap
from .dispatch import raw, trap
from .glue_intl import record
from .glue_common import _port

memFullErr = -108


def _text(t, p: int, n: int) -> bytes:
    return t.mem.read(p, n) if n > 0 else b""


def _width(t, numer=None, denom=None):
    """TextWidth in the current port, scaled by numer/denom (Points)."""
    scale = numer.h / denom.h if numer is not None and denom is not None and denom.h else 1

    def width(text: bytes) -> int:
        return round(t.tb.StringWidth(from_mac(text)) * scale) if text else 0
    return width


def _fixed(v: float) -> int:
    return int(round(v * 65536)) & 0xFFFFFFFF


# =========================================================================
# Scripts and their variables
# =========================================================================
@trap
def FontScript(t):
    return sm.smRoman


@trap
def IntlScript(t):
    return sm.smRoman


@trap
def FontToScript(t, family):
    return sm.smRoman                   # the script of any font not installed


@trap
def KeyScript(t, code):
    pass


def _kchr_cache(t) -> int:
    """The 'KCHR' KeyTranslate goes by, in the System heap, copied there
    the first time it's asked for.  Programs pass it to KeyTranslate, or
    read its tables themselves: Flash 2 makes its own table of the keys
    from it, and drops the keys it finds no character for."""
    p = t.system.kchr_cache
    if not p:
        p = t.system.kchr_cache = t.system.sys_heap.new_ptr(len(keymap.kchr()))
        t.mem.write(p, keymap.kchr())
    return p


@trap
def GetScriptManagerVariable(t, verb):
    if verb == sm.smSysRef:
        return t.mem.r16s(lowmem.BootDrive)
    if verb == sm.smKCHRCache:
        return _kchr_cache(t)
    value = sm.environs(verb, t.system.script_settings)
    return 0 if value is None else value


@trap
def SetScriptManagerVariable(t, verb, value):
    if verb not in sm.SETTABLE:
        return sm.smVerbNotFound
    t.system.script_settings[verb] = value
    return noErr


@trap
def GetScriptVariable(t, script, verb):
    if script not in (sm.smRoman, sm.smSystemScript):
        return 0
    if verb == sm.smScriptAppFond:
        return t.mem.r16(lowmem.ApFontID)
    value = sm.script_variable(verb)
    return 0 if value is None else value


@trap
def SetScriptVariable(t, script, verb, value):
    return sm.smVerbNotFound


# =========================================================================
# Characters
# =========================================================================
@trap
def CharByte(t, p, offset):
    return 0                            # smSingleByte: no two-byte characters


@trap
def CharacterByteType(t, p, offset, script):
    return 0


@trap
def CharType(t, p, offset):
    return sm.char_type(t.mem.r8(p + offset))


@trap
def CharacterType(t, p, offset, script):
    return sm.char_type(t.mem.r8(p + offset))


@trap
def ParseTable(t, table):
    t.mem.write(table, bytes(256))      # every byte a character by itself
    return True


@trap
def FillParseTable(t, table, script):
    return ParseTable(t, table)


@trap
def FindScriptRun(t, p, length, used_p):
    t.w32(used_p, length)               # it's all Roman
    return sm.smRoman


def _handle_text(t, h: int) -> bytes:
    heap = t.system.heap_of(h)
    return t.mem.read(heap.deref(h), heap.handle_size(h))


@trap
def Transliterate(t, src, dst, target, mask):
    out = sm.transliterate(_handle_text(t, src), target, mask)
    if out is None:
        return -1
    heap = t.system.heap_of(dst)
    if not heap.set_handle_size(dst, len(out)):
        return memFullErr
    t.mem.write(heap.deref(dst), out)
    return noErr


@trap
def TransliterateText(t, src, dst, target, mask, script):
    return Transliterate(t, src, dst, target, mask)


@trap
def ReplaceText(t, base, substitution, key_p):
    """Each instance of the key, a Str15, in the base text handle replaced
    by the substitution handle's text: how many it replaced."""
    key = t.mem.read(key_p + 1, min(t.mem.r8(key_p), 15))
    text = _handle_text(t, base)
    count = text.count(key) if key else 0
    if count:
        out = text.replace(key, _handle_text(t, substitution))
        heap = t.system.heap_of(base)
        if not heap.set_handle_size(base, len(out)):
            return memFullErr
        t.mem.write(heap.deref(base), out)
    return count


def _change_case(t, p: int, length: int, routine: int) -> None:
    text = _text(t, p, length)
    if routine & 0x200:
        text = text.translate(sm.STRIP)
    if routine & 0x400:
        text = text.translate(sm.UPPER)
    elif not routine & 0x200:
        text = text.translate(sm.LOWER)
    t.mem.write(p, text)


@trap
def TextUtilFunctions(t, p, length, script, routine):
    """LowercaseText ($0000), StripDiacritics ($0200), UppercaseText
    ($0400) and UppercaseStripDiacritics ($0600), in place."""
    _change_case(t, p, length, routine)


@raw(0xA056)
def LwrText(t, word):
    """Inside Macintosh VI's LowerText: the text at A0, length D0.W, in
    place, with the trap word's bits 9 and 10 the routines' above
    (StripText $A256, UprText $A456, StripUpperText $A656)."""
    cpu = t.cpu
    length = cpu.d(0) & 0xFFFF
    _change_case(t, cpu.a(0), length - 0x10000 if length & 0x8000 else length, word)
    cpu.set_d(0, noErr)


# =========================================================================
# Words and lines
# =========================================================================
def _offsets(t, p: int, first: int, second: int) -> None:
    """An OffsetTable: three pairs, only the first used in Roman text."""
    t.mem.write(p, struct.pack(">6h", first, second, 0, 0, 0, 0))


@trap
def FindWord(t, p, length, offset, leading_edge, breaks, offsets):
    _offsets(t, offsets, *sm.find_word(_text(t, p, length), offset, bool(leading_edge)))


@trap
def HiliteText(t, p, length, first, second, offsets):
    _offsets(t, offsets, min(first, second), max(first, second))


@trap
def VisibleLength(t, p, length):
    return sm.visible_length(_text(t, p, length))


@trap
def StyledLineBreak(t, p, length, start, end, flags, width_p, offset_p):
    avail = t.mem.r32s(width_p) / 65536
    first = t.mem.r32(offset_p) != 0
    code, offset, left = sm.line_break(_text(t, p, length), start, end, avail, first,
                                       _width(t))
    t.w32(width_p, _fixed(left))
    t.w32(offset_p, offset)
    return code


@trap
def TruncString(t, avail, p, where):
    out = sm.truncate(t.pstr(p), avail, where, _width(t))
    if out is None:
        return sm.NotTruncated
    t.set_str255(p, out)
    return sm.Truncated


# =========================================================================
# Measuring and drawing justified text: the slop shared among the spaces
# =========================================================================
@trap
def Char2Pixel(t, p, length, slop, offset, direction):
    return round(sm.char_locations(_text(t, p, length), slop, _width(t))[
        max(0, min(offset, length))])


@trap
def CharToPixel(t, p, length, slop, offset, direction, run, numer, denom):
    return round(sm.char_locations(_text(t, p, length), slop / 65536, _width(t, numer, denom))[
        max(0, min(offset, length))])


@trap
def Pixel2Char(t, p, length, slop, x, leading_p):
    offset, leading, _ = sm.pixel_to_char(_text(t, p, length), slop, x, _width(t))
    t.mem.w8(leading_p, 1 if leading else 0)
    return offset


@trap
def PixelToChar(t, p, length, slop, x, leading_p, remaining_p, run, numer, denom):
    offset, leading, remaining = sm.pixel_to_char(_text(t, p, length), slop / 65536,
                                                  x / 65536, _width(t, numer, denom))
    t.mem.w8(leading_p, 1 if leading else 0)
    if remaining_p & t.mem.system.addr_mask:
        t.w32(remaining_p, _fixed(remaining))
    return offset


@trap
def MeasureJust(t, p, length, slop, locs):
    for i, x in enumerate(sm.char_locations(_text(t, p, length), slop, _width(t))):
        t.w16(locs + 2 * i, round(x))


@trap
def MeasureJustified(t, p, length, slop, locs, run, numer, denom):
    for i, x in enumerate(sm.char_locations(_text(t, p, length), slop / 65536,
                                            _width(t, numer, denom))):
        t.w16(locs + 2 * i, round(x))


def _draw_just(t, text: bytes, slop: float) -> None:
    port = _port(t)
    spaces = text.count(b" ")
    if port is None or not text:
        return
    extra = port.sp_extra
    if spaces:
        port.sp_extra = extra + int(slop * 65536 / spaces)
    try:
        t.tb.DrawString(from_mac(text))
    finally:
        port.sp_extra = extra


@trap
def DrawJust(t, p, length, slop):
    _draw_just(t, _text(t, p, length), slop)


@trap
def DrawJustified(t, p, length, slop, run, numer, denom):
    _draw_just(t, _text(t, p, length), slop / 65536)


@trap
def PortionLine(t, p, length, run, numer, denom):
    """How much of a line's slop the run takes: by its width."""
    return _fixed(_width(t, numer, denom)(_text(t, p, length)))


# =========================================================================
# Dates and times
# =========================================================================
@trap
def InitDateCache(t, cache):
    """The cache StringToDate and StringToTime keep their parsing tables
    in: nothing of it is used here."""
    return noErr


def _parsed(t, parsed, used_p: int, date_p: int, fields: tuple[int, ...]) -> int:
    """The bytes taken, and the LongDateRec's fields at those word
    offsets: only the ones the routine fills in."""
    t.mem.w32(used_p, parsed.used)
    for at, value in zip(fields, parsed.fields):
        t.mem.w16(date_p + at, value)
    return parsed.status


@trap
def StringToDate(t, p, length, cache, used_p, date_p):
    """The year, month, day and day of the week at the start of the text;
    what isn't there is today's."""
    today = intl.date_time(t.mem.r32(lowmem.Time))
    parsed = intl.parse_date(_text(t, p, length), record(t, 0), record(t, 1), today)
    return _parsed(t, parsed, used_p, date_p, (2, 4, 6, 14))


@trap
def StringToTime(t, p, length, cache, used_p, date_p):
    """The hour, minute and second at the start of the text."""
    parsed = intl.parse_time(_text(t, p, length), record(t, 0))
    return _parsed(t, parsed, used_p, date_p, (8, 10, 12))


# A LongDateTime is a signed 64-bit count of seconds since the start of
# 1904; a LongDateRec, 14 words: era, year, month, day, hour, minute,
# second, dayOfWeek (1 for Sunday), dayOfYear, weekOfYear, pm and three
# reserved (Inside Macintosh: Text, the Script Manager's date utilities).
EPOCH = datetime.datetime(1904, 1, 1)


@trap
def LongSecondsToDate(t, secs_p, date_p):
    secs = int.from_bytes(t.mem.read(secs_p, 8), "big", signed=True)
    try:
        d = EPOCH + datetime.timedelta(seconds=secs)
    except OverflowError:           # outside the years 1 to 9999
        t.mem.fill(date_p, 28)
        return
    year_day = d.timetuple().tm_yday
    first = (d.replace(month=1, day=1).isoweekday() % 7)       # Sunday 0
    t.mem.write(date_p, struct.pack(
        ">14h", 0, d.year, d.month, d.day, d.hour, d.minute, d.second,
        d.isoweekday() % 7 + 1, year_day, (year_day - 1 + first) // 7 + 1, d.hour >= 12,
        0, 0, 0))


@trap
def LongDateToSeconds(t, date_p, secs_p):
    """Fields past their range carry over, as 13 months is a year and a
    month."""
    _, year, month, day, hour, minute, second = struct.unpack(">7h", t.mem.read(date_p, 14))
    years, month = divmod(year * 12 + month - 1, 12)
    try:
        d = datetime.datetime(years, month + 1, 1) + datetime.timedelta(
            days=day - 1, hours=hour, minutes=minute, seconds=second)
        secs = (d - EPOCH) // datetime.timedelta(seconds=1)
    except (ValueError, OverflowError):
        secs = 0
    t.mem.write(secs_p, secs.to_bytes(8, "big", signed=True))


# Number formats (Inside Macintosh: Text, Text Utilities), with the
# number parts table the application passes, the one 'itl4' has.

def _parts(t, p: int) -> nf.Parts:
    return nf.Parts(t.mem.read(p, nf.NUMBER_PARTS_SIZE))


def _format(t, p: int) -> nf.Format | None:
    return nf.Format.decode(t.mem.read(p, nf.NUM_FORMAT_SIZE))


@trap
def StringToFormatRec(t, spec_p, parts_p, rec_p):
    fmt, status = nf.parse(t.pstr(spec_p), _parts(t, parts_p))
    rec = fmt.encode() if fmt is not None else None
    if fmt is not None and rec is None:          # more than a record holds
        status = nf.fFormStrIsNAN
    if rec is not None:
        t.mem.write(rec_p, rec)
    return status


@trap
def FormatRecToString(t, rec_p, parts_p, out_p, positions_p):
    fmt, parts = _format(t, rec_p), _parts(t, parts_p)
    if fmt is None or not parts.valid:
        t.set_str255(out_p, b"")
        t.mem.fill(positions_p, 12)
        return nf.fBadPartsTable if fmt is not None else nf.fFormStrIsNAN
    out, where = nf.unparse(fmt, parts)
    t.set_str255(out_p, out[:255])
    t.mem.write(positions_p, struct.pack(">6h", *(n for pair in where for n in pair)))
    return nf.fFormatOK


@trap
def ExtendedToString(t, x_p, rec_p, parts_p, out_p):
    x = sane.unpack(sane.X, t.mem.read(x_p, sane.SIZE[sane.X]))
    out, status = nf.write(x, _format(t, rec_p), _parts(t, parts_p))
    t.set_str255(out_p, out)
    return status


@trap
def StringToExtended(t, text_p, rec_p, parts_p, x_p):
    x, status = nf.read(t.pstr(text_p), _format(t, rec_p), _parts(t, parts_p))
    t.mem.write(x_p, sane.convert(x, sane.X, sane.Env()))
    return status
