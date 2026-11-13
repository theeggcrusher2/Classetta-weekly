"""
Toolbox Utilities traps: strings, Munger, PackBits, bit and fixed-point
arithmetic and number conversion, and the Package Manager's InitPack.
"""

from __future__ import annotations

import math
import struct

from ..memory import to_mac
from ..toolbox import sane
from ..toolbox.pict import pack_bits, unpack_bits_from
from ..toolbox.system import APP_PACKS, init_pack
from .dispatch import trap
from .glue_common import _noop
from .glue_resources import _rm


# =========================================================================
# Toolbox Utilities
# =========================================================================
@trap
def GetString(t, string_id):
    return t.tb.GetString(string_id)


@trap
def NewString(t, p):
    return t.process.heap.new_handle_with(t.mem.read(p, t.mem.r8(p) + 1))


@trap
def SetString(t, h, p):
    data = t.mem.read(p, t.mem.r8(p) + 1)
    heap = t.system.heap_of(h)
    if heap.set_handle_size(h, len(data)):
        t.mem.write(heap.deref(h), data)


def _get(rtype: str, name: str):
    def call(t, rid):
        return _rm(t).GetResource(rtype, rid)
    call.__name__ = name
    return call


for _rtype, _name in (("ICON", "GetIcon"), ("CURS", "GetCursor"), ("PAT ", "GetPattern"),
                      ("PICT", "GetPicture")):
    trap(_get(_rtype, _name))


@trap
def Munger(t, h, offset, p1, len1, p2, len2):
    """Find p1 in the handle from offset and replace it with p2 (IM I-468).
    A string with no length isn't read, so its pointer can be anything
    but NIL: Script Editor deletes with a p2 of -1."""
    def string(p, n):
        return t.mem.read(p, n) if n > 0 else b""
    heap = t.system.heap_of(h)
    data = t.mem.read(heap.deref(h), heap.handle_size(h))
    if offset > len(data):
        return -1
    if p1 == 0:                                 # replace len1 bytes at offset
        start = offset
        end = len(data) if len1 < 0 else min(offset + len1, len(data))
    else:
        target = string(p1, len1)
        if len1 == 0:                           # insert at offset
            start = end = offset
        else:
            start = data.find(target, offset)
            if start < 0:
                return -1
            end = start + len(target)
    if p2 == 0:
        return start                            # just find
    new = data[:start] + string(p2, len2) + data[end:]
    if not heap.set_handle_size(h, len(new)):
        return -1
    t.mem.write(heap.deref(h), new)
    return start + max(len2, 0)


@trap
def PackBits(t, src_var, dst_var, count):
    """Pack count bytes from the pointer at src_var to the one at dst_var,
    moving each past what it took and gave (IM I-470)."""
    m = t.mem
    src, dst, count = m.r32(src_var), m.r32(dst_var), max(count, 0)
    packed = pack_bits(m.read(src, count))
    m.write(dst, packed)
    m.w32(src_var, (src + count) & 0xFFFFFFFF)
    m.w32(dst_var, (dst + len(packed)) & 0xFFFFFFFF)


@trap
def UnpackBits(t, src_var, dst_var, count):
    """Unpack count bytes the same way."""
    m = t.mem
    src, dst, count = m.r32(src_var), m.r32(dst_var), max(count, 0)
    most = count + count // 128 + 2                 # all of it literal
    data, took = unpack_bits_from(m.read(src, min(most, m.size - (src & t.mem.system.addr_mask))), count)
    m.write(dst, data)
    m.w32(src_var, (src + took) & 0xFFFFFFFF)
    m.w32(dst_var, (dst + count) & 0xFFFFFFFF)


@trap
def HiWord(t, x):
    return (x >> 16) & 0xFFFF


@trap
def LoWord(t, x):
    return x & 0xFFFF


@trap
def BitAnd(t, a, b):
    return a & b


@trap
def BitOr(t, a, b):
    return a | b


@trap
def BitXor(t, a, b):
    return a ^ b


@trap
def BitNot(t, a):
    return ~a


@trap
def BitShift(t, x, count):
    count &= 31 if count >= 0 else -1
    x &= 0xFFFFFFFF
    return (x << count) if count >= 0 else (x >> -count)


def _bit(p: int, n: int) -> tuple[int, int]:
    """Bit n counting from the high bit of the byte at p, as BitTst does.
    The byte may be a device's: Mission Starlight sets a bit of the VIA's
    with BitSet."""
    return p + (n >> 3), 0x80 >> (n & 7)


@trap
def BitTst(t, p, n):
    at, mask = _bit(p, n)
    return bool(t.cpu.bus_r8(at) & mask)


@trap
def BitSet(t, p, n):
    at, mask = _bit(p, n)
    t.cpu.bus_w8(at, t.cpu.bus_r8(at) | mask)


@trap
def BitClr(t, p, n):
    at, mask = _bit(p, n)
    t.cpu.bus_w8(at, t.cpu.bus_r8(at) & ~mask)


def _held(v: int) -> int:
    return max(min(v, 0x7FFFFFFF), -0x80000000)


def _quotient(num: int, den: int) -> int:
    """num / den as the 128K ROM's fixed-point routines round it: half a
    unit away from zero, an overflow held to $7FFFFFFF or $80000000, and
    a division by zero $80000000 if num is negative, else $7FFFFFFF
    (Inside Macintosh IV-63)."""
    if den == 0:
        return -0x80000000 if num < 0 else 0x7FFFFFFF
    q = (2 * abs(num) + abs(den)) // (2 * abs(den))
    return _held(q if (num < 0) == (den < 0) else -q)


def _scaled(x: float, bits: int) -> int:
    """x with that many fraction bits, rounded half away from zero."""
    if math.isnan(x):
        return 0x7FFFFFFF
    if math.isinf(x):
        return _held(int(math.copysign(1 << 32, x)))
    return _held(int(math.copysign(math.floor(abs(x) * (1 << bits) + 0.5), x)))


# A Fixed has 16 fraction bits, a Fract 30 (Inside Macintosh IV-62)
FIXED, FRACT = 16, 30
# The ROM reduces an angle by 0.C910 hex, FixATan2(1, 1), for pi/4, so its
# sine and cosine repeat every 2 * 3.1416015625 rather than every 2 pi
_PI = 4 * 0xC910 / 65536


@trap
def FixMul(t, a, b):
    return _quotient(a * b, 1 << FIXED)


@trap
def FixRatio(t, num, den):
    return _quotient(num << FIXED, den)


@trap
def FixDiv(t, a, b):
    return _quotient(a << FIXED, b)


@trap
def FracMul(t, a, b):
    return _quotient(a * b, 1 << FRACT)


@trap
def FracDiv(t, a, b):
    return _quotient(a << FRACT, b)


@trap
def FracSqrt(t, x):
    """x, and the root, taken unsigned: 0 to 4 (Inside Macintosh IV-64)."""
    n = (x & 0xFFFFFFFF) << FRACT
    r = math.isqrt(n)
    return r + (n - r * r > r)


@trap
def FracSin(t, x):
    return _scaled(math.sin(x / (1 << FIXED) * math.pi / _PI), FRACT)


@trap
def FracCos(t, x):
    return _scaled(math.cos(x / (1 << FIXED) * math.pi / _PI), FRACT)


@trap
def FixATan2(t, x, y):
    """The arctangent of y/x in radians, from -pi to pi."""
    return _scaled(math.atan2(y, x), FIXED)


@trap
def Long2Fix(t, x):
    return _held(x << FIXED)


@trap
def Fix2Long(t, x):
    return _quotient(x, 1 << FIXED)


@trap
def Fix2Frac(t, x):
    return _held(x << FRACT - FIXED)


@trap
def Frac2Fix(t, x):
    return _quotient(x, 1 << FRACT - FIXED)


def _extended(t, p: int) -> float:
    return sane.to_float(sane.unpack(sane.X, t.mem.read(p, 10)))


@trap
def R_X2Fix(t, p):
    """X2Fix, which takes the address of its extended."""
    return _scaled(_extended(t, p), FIXED)


@trap
def R_X2Frac(t, p):
    return _scaled(_extended(t, p), FRACT)


def _to_extended(t, x: int, fraction: int) -> int:
    """x, with that many bits of fraction, as an Extended where its
    caller's address under the argument says: that's the result."""
    p = t.mem.r32(t.cpu.sp + 4)
    t.mem.write(p, sane.encode(sane.X, sane.Num(sane.FINITE, int(x < 0), abs(x), -fraction)))
    return p


@trap
def Fix2X(t, x):
    return _to_extended(t, x, FIXED)


@trap
def Frac2X(t, x):
    return _to_extended(t, x, FRACT)


@trap
def FixRound(t, x):
    return (x + 0x8000) >> 16


@trap
def SlopeFromAngle(t, angle):
    """The slope dh/dv of a line that many degrees clockwise from 12
    o'clock, the angle taken mod 180 (IM I-475): v grows downward, so
    45 degrees is -1.0."""
    return _scaled(-math.tan(math.radians(angle % 180)), FIXED)


@trap
def AngleFromSlope(t, slope):
    """The angle, 1 to 180 degrees clockwise from 12 o'clock, of a line
    with that slope dh/dv; a slope of 0 is 180 (IM I-475)."""
    angle = round(math.degrees(math.atan(-slope / 65536)))
    return angle if angle > 0 else angle + 180


@trap
def LongMul(t, a, b, p):
    t.mem.write(p, struct.pack(">q", a * b))


@trap
def NumToString(t, n, p):
    """Writes the string wherever theString points, NIL included: SCSI
    Evaluator converts its numbers into the string at address 0 and draws
    them from there."""
    t.mem.w_pstr(p, to_mac(str(n)))


@trap
def StringToNum(t, p):
    """IM I-490: after a sign, each character's last four bits are a digit,
    whatever the character (a space is a zero, 'CAT' is 314), and the
    magnitude is taken modulo 2^32 before it's negated."""
    s = t.mem.read(p + 1, t.mem.r8(p))
    sign = -1 if s[:1] == b"-" else 1
    value = 0
    for c in s[1:] if s[:1] in (b"-", b"+") else s:
        value = (value * 10 + (c & 0xF)) & 0xFFFFFFFF
    return None, (sign * value) & 0xFFFFFFFF


# =========================================================================
# Package Manager (IM I-483): every package is always there, so there's
# nothing to load or unload, but AppPacks has their handles
# =========================================================================
@trap
def InitPack(t, n):
    init_pack(_rm(t), n)


@trap
def InitAllPacks(t):
    for n in range(APP_PACKS):
        init_pack(_rm(t), n)


for _name in ("DILoad", "DIUnload"):
    trap(_noop(_name))
