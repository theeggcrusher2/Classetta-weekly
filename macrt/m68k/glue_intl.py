"""
Pack6, the International Utilities (Inside Macintosh I-493): dates and
times written out, and strings compared, as the international resources
say.  macrt.toolbox.intl has the formats; a System file without 'INTL' 0
and 1 is given the US ones (macrt.toolbox.sysres).
"""

from __future__ import annotations

from ..toolbox import intl
from ..toolbox import script as sm
from ..toolbox.memmgr import noErr
from .dispatch import trap

# The Script Manager's names for the international resources, ID 0 in a
# US System
ITL = {0: "itl0", 1: "itl1", 2: "itl2", 4: "itl4"}
SIZES = {0: intl.INTL0_SIZE, 1: intl.INTL1_SIZE}
# GetIntlResourceTable's 'itl4' tables, by where its header has their
# offsets and lengths: the number parts, untoken and white space tables
ITL4_TABLES = {2: (intl.DEF_PARTS_OFFSET, intl.DEF_PARTS_LENGTH),
               3: (intl.UNTOKEN_OFFSET, intl.UNTOKEN_LENGTH),
               4: (intl.WHT_SP_LIST_OFFSET, intl.WHT_SP_LIST_LENGTH)}
ROMAN = (sm.smRoman, sm.smSystemScript, sm.smCurrentScript)


def intl_handle(t, which: int) -> int:
    """IUGetIntl's resource: 'INTL' which from the resource chain, else
    its 'itl' equivalent.  ResError is left alone."""
    rm = t.process.resources
    err = rm.error
    h = rm.get("INTL", which)
    if not h and which in ITL:
        h = rm.get(ITL[which], 0)
    rm.error = err
    return h


def _record(t, h: int, which: int) -> bytes:
    p = t.mem.r32(h) & t.mem.system.addr_mask if h else 0
    return t.mem.read(p, SIZES[which]) if p else intl.DEFAULTS[which]()


def record(t, which: int) -> bytes:
    """The Intl0Rec or Intl1Rec in use."""
    return _record(t, intl_handle(t, which), which)


def _date(t, secs: int, form: int, h: int) -> bytes:
    which = 0 if form == intl.shortDate else 1
    return intl.date_string(secs, form, _record(t, h or intl_handle(t, which), which))


def _time(t, secs: int, seconds: int, h: int) -> bytes:
    return intl.time_string(secs, bool(seconds), _record(t, h or intl_handle(t, 0), 0))


def _long_date_time(t, p: int) -> int:
    return int.from_bytes(t.mem.read(p, 8), "big", signed=True)


@trap
def IUDateString(t, secs, form, result):
    t.set_str255(result, _date(t, secs & 0xFFFFFFFF, form, 0))


@trap
def IUDatePString(t, secs, form, result, h):
    t.set_str255(result, _date(t, secs & 0xFFFFFFFF, form, h))


@trap
def IULDateString(t, p, form, result, h):
    t.set_str255(result, _date(t, _long_date_time(t, p), form, h))


@trap
def IUTimeString(t, secs, seconds, result):
    t.set_str255(result, _time(t, secs & 0xFFFFFFFF, seconds, 0))


@trap
def IUTimePString(t, secs, seconds, result, h):
    t.set_str255(result, _time(t, secs & 0xFFFFFFFF, seconds, h))


@trap
def IULTimeString(t, p, seconds, result, h):
    t.set_str255(result, _time(t, _long_date_time(t, p), seconds, h))


@trap
def IUMetric(t):
    return intl.is_metric(_record(t, intl_handle(t, 0), 0))


@trap
def IUGetIntl(t, which):
    return intl_handle(t, which)


@trap
def IUSetIntl(t, refnum, which, h):
    """Make h's record the file's 'INTL' which: copied into the one it
    has, else added as a new resource."""
    rm = t.process.resources
    current = rm.CurResFile()
    rm.UseResFile(refnum)
    if rm.error == noErr:
        old = rm.get("INTL", which, one=True)
        if old and old != h:
            data = t.mem.read(t.mem.r32(h) & t.mem.system.addr_mask, t.system.heap_of(h).handle_size(h))
            heap = t.system.heap_of(old)
            if heap.set_handle_size(old, len(data)):
                t.mem.write(heap.deref(old), data)
        if old:
            rm.ChangedResource(old)
        else:
            rm.AddResource(h, "INTL", which)
    rm.UseResFile(current)


@trap
def IUClearCache(t):
    pass        # nothing's cached: each call reads the resources


@trap
def IUGetItlTable(t, script, table_code, itl_p, offset_p, length_p):
    """GetIntlResourceTable: where one of the 'itl4' tables is, by the
    offset and length its header gives.  The 'itl2' ones (word selection
    and wrapping) aren't given, as macrt's Script Manager works from its
    own, not a disk's 'itl2', so for those, or a script other than Roman,
    itlPtr is NIL, as for a table a script hasn't got (Inside Macintosh:
    Text, the Script Manager)."""
    h = intl_handle(t, 4) if script in ROMAN and table_code in ITL4_TABLES else 0
    p = t.mem.r32(h) & t.mem.system.addr_mask if h else 0
    offset_at, length_at = ITL4_TABLES.get(table_code, (0, 0))
    offset = t.mem.r32(p + offset_at) if p else 0
    length = t.mem.r16(p + length_at) if p else 0
    found = p and offset and length
    t.w32(itl_p, h if found else 0)
    t.w32(offset_p, offset if found else 0)
    t.w32(length_p, length if found else 0)


def _text(t, p: int, length: int) -> bytes:
    return t.mem.read(p, length) if length > 0 else b""


@trap
def IUMagString(t, a, b, a_len, b_len):
    return intl.compare(_text(t, a, a_len), _text(t, b, b_len))


@trap
def IUMagIDString(t, a, b, a_len, b_len):
    return intl.equal(_text(t, a, a_len), _text(t, b, b_len))


@trap
def IUMagPString(t, a, b, a_len, b_len, itl2):
    return intl.compare(_text(t, a, a_len), _text(t, b, b_len))


@trap
def IUMagIDPString(t, a, b, a_len, b_len, itl2):
    return intl.equal(_text(t, a, a_len), _text(t, b, b_len))


@trap
def IUTextOrder(t, a, b, a_len, b_len, a_script, b_script, a_lang, b_lang):
    """TextOrder: as IUMagString, there being one script, Roman, whose
    text macrt orders as English whatever its language."""
    return intl.compare(_text(t, a, a_len), _text(t, b, b_len))


@trap
def TextOrder30(t, a, b, a_len, b_len, a_script, b_script, a_lang, b_lang):
    return IUTextOrder(t, a, b, a_len, b_len, a_script, b_script, a_lang, b_lang)
