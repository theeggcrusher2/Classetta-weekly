"""
Instructions the core doesn't know, done here as the processor does them:
the 68020's CHK2, CMP2, PACK and UNPK, and the 68881's FMOVE to and from
its packed decimal real format, FMOVE from #<data> to its control
registers, FMOVEM of its data registers to a predecrement, and FSAVE
and FRESTORE.

The core takes CHK2 and CMP2 for illegal instructions, and MPW Pascal's
code for a 68020 checks a subrange's bounds with CHK2 (MPWAccuracy's).
It has no packed decimal reals, and takes an FMOVE.P for an instruction
with a data type it doesn't implement, or with -(An), for an address
error; Mandelbrot 2.7 turns its numbers into text with one.  An FMOVE of
#<data> to the FPCR or FPSR it takes for an address error too, and
MPWAccuracy clears its FPSR with one.  FSAVE and FRESTORE it takes for
illegal instructions, and MPWAccuracy's own trap for raising a
floating-point exception has them.  It takes each before it has done any of it, so run() does it
here, from where it was (see CPU._intr).  PACK and UNPK it runs as
instructions of one word that leave the registers as they were, and then
their adjustment word as the next instruction, so the core is stopped at them, as at a TRAPcc, for
run() to do them here (see CPU._stop_at).  SCSITools looks for a 68020 by
running a PACK, with its own handler for the illegal instruction a 68000
would take it for.

An FMOVEM.X to -(An) with no registers in its list the core runs as if
it had one: it leaves An 12 bytes lower, though it writes nothing.
Masters of the Elements' code saves the FPU with an FSAVE, an FMOVEM of
FP0-FP7 and one of no registers, all to -(A7), and takes them back up
with an FMOVEM of no registers from (A7)+, which the core runs as it
should: so A7 comes back 12 bytes lower, and the code returns to what's
there.  So the core is stopped at an FMOVEM.X to -(An) with no registers
in its word, or with its list in a data register, which may have none,
for run() to do it here.

From the M68000 Family Programmer's Reference Manual: CHK2, CMP2,
PACK, UNPK, FMOVE, FMOVEM, FMOVE and FMOVEM of the system control registers, FSAVE and
FRESTORE, the packed decimal real format (1.5.1), and the FPCR and FPSR
(1.2.2, 1.2.3); and the MC68881/MC68882 User's Manual, for the 68881's
state frames.
"""

from __future__ import annotations

import logging
import struct

log = logging.getLogger("macrt.cpu")

# Exception vector numbers (as in cpu).
ILLEGAL, CHK, FORMAT_ERROR = 4, 6, 14

CCR_Z, CCR_C = 0x04, 0x01

# The FPCR's mode byte: rounding precision and mode.
PREC_SHIFT, RND_SHIFT = 6, 4
RN, RZ, RM, RP = 0, 1, 2, 3
PRECISIONS = (64, 24, 53, 64)           # the mantissa's bits, by PREC: extended, single, double
# The FPSR: the condition code byte, the exception byte, the accrued exception byte.
FPCC_N, FPCC_Z, FPCC_I, FPCC_NAN = 1 << 27, 1 << 26, 1 << 25, 1 << 24
FPCC = 0xFF000000
EXC = 0x0000FF00
SNAN, OPERR, INEX2, INEX1 = 1 << 14, 1 << 13, 1 << 9, 1 << 8
AEXC_IOP, AEXC_INEX = 1 << 7, 1 << 3

BIAS = 16383                            # an extended real's exponent's
MAX_EXPONENT = 0x7FFF                   # an infinity's or a NaN's
QUIET = 1 << 62                         # in a NaN's mantissa: a quiet one
FRACTION = (1 << 63) - 1                # a mantissa's bits after the integer bit
DIGITS = 17                             # a packed decimal real's: one, then 16 after the point
PACKED = 12                             # bytes
FMOVE_P_IN, FMOVE_P_OUT = 0x4C00, 0x6C00   # an FMOVE.P's second word, but its register and k-factor
P_STATIC, P_DYNAMIC = 3, 7              # a destination format's: a k-factor in the word, in Dn
FMOVE_IMMEDIATE = 0xF23C                # an FMOVE from #<data>
FMOVEM_DOWN = 0xF220                    # an FMOVEM with -(An), but An
FMOVEM_X_DOWN, DYNAMIC_LIST = 0xE000, 0x0800    # its second word: FPn to -(An), the list in Dn
EXTENDED = 12                           # an extended real's bytes in memory
FSAVE, FRESTORE = 0xF300, 0xF340
# The 68881's state frames, from its user's manual (6.4.2): a null one, a
# format word of 0, and an idle one, of the first version, with 24 bytes
# after its format word and the word reserved: the command/condition
# register's image, a word reserved, written as $FFFF, the exceptional
# operand, the operand register's, and the BIU flags.  Those that are
# defined (16-31) say nothing is pending here: no protocol violation,
# instruction or access of a register (28-30 set), exception (27 set) or
# operand to be written (26 set); the rest are written as ones.
NULL_FRAME, IDLE_FORMAT, IDLE_FRAME = 4, 0x1F18, 28
NO_EXCEPTION_PENDING = 1 << 27
IDLE = struct.pack(">HHHH12sII", IDLE_FORMAT, 0, 0, 0xFFFF, bytes(12), 0, 0x7C00FFFF)
FMOVE_TO_CONTROL, CONTROL_LIST = 0x8000, 0x1C00     # its second word: to FPCR, FPSR, FPIAR
PACK, UNPK = 0x8140, 0x8180             # but their registers and R/M bit


def _s8(value: int) -> int:
    return value - 0x100 if value & 0x80 else value


def _s16(value: int) -> int:
    return value - 0x10000 if value & 0x8000 else value


def _s32(value: int) -> int:
    return value - 0x100000000 if value & 0x80000000 else value


class Illegal(Exception):
    """An extension word the processor has no meaning for."""


def is_pack(op: int) -> bool:
    """Whether op is a PACK's or an UNPK's first word."""
    return op & 0xF1F0 in (PACK, UNPK)


def known(mem, at: int) -> bool:
    """Whether the instruction at at is one done here."""
    op, ext = mem.r16(at), mem.r16(at + 2)
    mode, reg = op >> 3 & 7, op & 7
    if op & 0xF9C0 == 0x00C0 and op & 0x0600 != 0x0600:     # CHK2, CMP2
        return ext & 0x07FF == 0 and (mode in (2, 5, 6) or mode == 7 and reg < 4)
    if op & 0xFFC0 == FSAVE:
        return mode in (2, 4, 5, 6) or mode == 7 and reg < 2
    if op & 0xFFC0 == FRESTORE:
        return mode in (2, 3, 5, 6) or mode == 7 and reg < 4
    if op == FMOVE_IMMEDIATE and ext & 0xE000 == FMOVE_TO_CONTROL:     # FMOVEM.L #<data>,<list>
        return ext & 0xE3FF == FMOVE_TO_CONTROL and ext & CONTROL_LIST != 0
    if op & 0xFFF8 == FMOVEM_DOWN and ext & 0xE000 == FMOVEM_X_DOWN:    # FMOVEM.X <list>,-(An)
        return ext == FMOVEM_X_DOWN or ext & 0xFF8F == FMOVEM_X_DOWN | DYNAMIC_LIST
    if op & 0xFFC0 == 0xF200 and mode > 1:                  # FMOVE.P, from memory or to it
        return (ext & 0xFC7F == FMOVE_P_IN
                or ext & 0xFC00 == FMOVE_P_OUT and (mode < 7 or reg < 2)
                or ext & 0xFC0F == FMOVE_P_OUT | 0x1000 and (mode < 7 or reg < 2))
    return False


def do(cpu, at: int) -> tuple[int, int | None]:
    """Run the instruction at at: where it goes on from, and the vector of
    the exception it raises, if it raises one."""
    mem = cpu.mem
    op, ext = mem.r16(at), mem.r16(at + 2)
    try:
        if op & 0xFFC0 == FSAVE:
            return _fsave(cpu, at, op), None
        if op & 0xFFC0 == FRESTORE:
            return _frestore(cpu, at, op)
        if op == FMOVE_IMMEDIATE and ext & 0xE000 == FMOVE_TO_CONTROL:
            return _fmove_to_control(cpu, at, ext), None
        if op & 0xFFF8 == FMOVEM_DOWN and ext & 0xF700 == FMOVEM_X_DOWN:
            return _fmovem_down(cpu, at, op, ext), None
        if op & 0xFFC0 == 0xF200:
            return _fmove_packed(cpu, at, op, ext), None
        if is_pack(op):
            return _pack(cpu, at, op, ext), None
        return _chk2(cpu, at, op, ext)
    except Illegal:
        return at, ILLEGAL


# -- effective addresses ---------------------------------------------------------
def _address(cpu, mode: int, reg: int, at: int, size: int) -> tuple[int, int, int | None]:
    """Where the operand of size bytes an effective address of mode and
    reg is in memory, its extension words from at: the address, where they
    end, and what (An)+ or -(An) leaves An, to be set once it's used."""
    mem = cpu.mem
    if mode == 2:
        return cpu.a(reg), at, None
    if mode == 3:
        a = cpu.a(reg)
        return a, at, (a + size) & 0xFFFFFFFF
    if mode == 4:
        a = (cpu.a(reg) - size) & 0xFFFFFFFF
        return a, at, a
    if mode == 5:
        return (cpu.a(reg) + _s16(mem.r16(at))) & 0xFFFFFFFF, at + 2, None
    if mode == 6:
        return (*_indexed(cpu, cpu.a(reg), at), None)
    if reg == 0:                        # (xxx).W
        return _s16(mem.r16(at)) & 0xFFFFFFFF, at + 2, None
    if reg == 1:                        # (xxx).L
        return mem.r32(at), at + 4, None
    if reg == 2:                        # (d16,PC): from the extension word
        return (at + _s16(mem.r16(at))) & 0xFFFFFFFF, at + 2, None
    if reg == 3:
        return (*_indexed(cpu, at, at), None)
    if reg == 4:                        # #<data>: in the instruction's words
        return at, at + size, None
    raise Illegal


def _indexed(cpu, base: int, at: int) -> tuple[int, int]:
    """An indexed address, from An or the PC as base, and its extension
    words from at, in a 68000's brief format or the 68020's full one; and
    where they end."""
    mem = cpu.mem
    ext = mem.r16(at)
    at += 2
    n = ext >> 12 & 7
    index = cpu.a(n) if ext & 0x8000 else cpu.d(n)
    index = _s32(index) if ext & 0x800 else _s16(index & 0xFFFF)
    index <<= ext >> 9 & 3
    if not ext & 0x100:
        return (base + _s8(ext & 0xFF) + index) & 0xFFFFFFFF, at
    if ext & 0x8:
        raise Illegal

    def displacement(size: int) -> int:
        nonlocal at
        if size == 0:
            raise Illegal
        if size == 2:
            at += 2
            return _s16(mem.r16(at - 2))
        if size == 3:
            at += 4
            return _s32(mem.r32(at - 4))
        return 0
    if ext & 0x80:                      # the base suppressed
        base = 0
    if ext & 0x40:                      # the index suppressed
        index = 0
    base += displacement(ext >> 4 & 3)
    indirect = ext & 7
    if indirect == 0:
        return (base + index) & 0xFFFFFFFF, at
    if indirect == 4 or ext & 0x40 and indirect > 4:
        raise Illegal
    outer = displacement(indirect & 3)
    if indirect < 4:                    # indexed, then indirect
        return (mem.r32((base + index) & 0xFFFFFFFF) + outer) & 0xFFFFFFFF, at
    return (mem.r32(base & 0xFFFFFFFF) + index + outer) & 0xFFFFFFFF, at    # indirect, then indexed


# -- CHK2, CMP2 --------------------------------------------------------------------
def _chk2(cpu, at: int, op: int, ext: int) -> tuple[int, int | None]:
    """Compare Rn with the bounds at the effective address, the lower and
    then the upper: Z set if it's either, C if it's outside them; and for
    CHK2, a CHK exception if it is.  An address register is compared whole,
    with bounds of a byte or word sign-extended.  The bounds may be signed
    or unsigned, the smaller first either way: inside them is from the
    lower bound on up to the upper, round past the top if need be."""
    size = 1 << (op >> 9 & 3)
    addr, end, _ = _address(cpu, op >> 3 & 7, op & 7, at + 4, size)
    mem = cpu.mem
    read = {1: mem.r8, 2: mem.r16, 4: mem.r32}[size]
    lower, upper = read(addr), read((addr + size) & 0xFFFFFFFF)
    n = ext >> 12 & 7
    if ext & 0x8000:
        bits, value = 32, cpu.a(n)
        lower, upper = _extend(lower, size), _extend(upper, size)
    else:
        bits, value = 8 * size, cpu.d(n)
    mask = (1 << bits) - 1
    value, lower, upper = value & mask, lower & mask, upper & mask
    out = (value - lower) & mask > (upper - lower) & mask
    sr = cpu.sr & ~(CCR_Z | CCR_C)
    if value in (lower, upper):
        sr |= CCR_Z
    if out:
        sr |= CCR_C
    cpu.sr = sr
    if out and ext & 0x800:
        return at + 2, CHK              # a word into it, as the core's CHK leaves the PC
    return end, None


def _extend(value: int, size: int) -> int:
    bits = 8 * size
    return value - (1 << bits) if value >> (bits - 1) else value


# -- PACK, UNPK --------------------------------------------------------------------
def _pack(cpu, at: int, op: int, adjustment: int) -> int:
    """PACK: the adjustment added to the source's word, and the low four
    bits of each of its bytes made a byte, into the destination's low
    byte.  UNPK: the source byte's two digits made the low four bits of
    each byte of a word, and the adjustment added, into the destination's
    low word.  From data register to data register, or from -(Ax) to -(Ay),
    a byte at a time, the word's low byte at the higher address.  The
    condition codes are as they were."""
    src, dst = op & 7, op >> 9 & 7
    unpack = op & 0xF1F0 == UNPK
    if not op & 8:
        value = cpu.d(src)
        if unpack:
            result = (value << 4 & 0xF00 | value & 0xF) + adjustment
            cpu.set_d(dst, cpu.d(dst) & 0xFFFF0000 | result & 0xFFFF)
        else:
            result = value + adjustment
            cpu.set_d(dst, cpu.d(dst) & 0xFFFFFF00 | result >> 4 & 0xF0 | result & 0xF)
        return at + 4
    mem = cpu.mem
    if unpack:
        value = mem.r8(_predecrement(cpu, src))
        result = (value << 4 & 0xF00 | value & 0xF) + adjustment
        mem.w8(_predecrement(cpu, dst), result & 0xFF)
        mem.w8(_predecrement(cpu, dst), result >> 8 & 0xFF)
    else:
        low = mem.r8(_predecrement(cpu, src))
        result = (mem.r8(_predecrement(cpu, src)) << 8 | low) + adjustment
        mem.w8(_predecrement(cpu, dst), result >> 4 & 0xF0 | result & 0xF)
    return at + 4


def _predecrement(cpu, n: int) -> int:
    """-(An) for a byte: by two for A7, which stays even."""
    a = (cpu.a(n) - (2 if n == 7 else 1)) & 0xFFFFFFFF
    cpu.set_a(n, a)
    return a


# -- FSAVE and FRESTORE ------------------------------------------------------------
def _fsave(cpu, at: int, op: int) -> int:
    """FSAVE <ea>: an idle state frame, the core being done with each
    instruction before the next."""
    addr, end, an = _address(cpu, op >> 3 & 7, op & 7, at + 2, IDLE_FRAME)
    cpu.mem.write(addr, IDLE)
    if an is not None:
        cpu.set_a(op & 7, an)
    return end


def _frestore(cpu, at: int, op: int) -> tuple[int, int | None]:
    """FRESTORE <ea>: an idle state frame leaves things as they are, but for
    an exception it says is pending, which the core can't take; a null one
    resets the FPU; any other raises a format error."""
    mode, reg = op >> 3 & 7, op & 7
    addr, end, _ = _address(cpu, mode, reg, at + 2, 0)
    mem = cpu.mem
    format_word = mem.r16(addr)
    if format_word >> 8 == 0:
        size = NULL_FRAME
        for n in range(8):
            cpu.set_fp(n, MAX_EXPONENT, (1 << 64) - 1)     # the NaN the FPU makes
        cpu.fpcr = cpu.fpsr = 0
    elif format_word == IDLE_FORMAT:
        size = IDLE_FRAME
        if not mem.r32(addr + 24) & NO_EXCEPTION_PENDING:
            log.info("FRESTORE at $%06X: a floating-point exception pending isn't taken", at)
    else:
        return at, FORMAT_ERROR
    if mode == 3:
        cpu.set_a(reg, (addr + size) & 0xFFFFFFFF)
    return end, None


# -- FMOVEM.L #<data>,<list> -------------------------------------------------------
def _fmove_to_control(cpu, at: int, ext: int) -> int:
    """FMOVE.L or FMOVEM.L from #<data> to the FPCR, FPSR and FPIAR: a long
    of the data for each in the list, in that order.  The core keeps no
    FPIAR, so what's for it goes nowhere."""
    mem, at = cpu.mem, at + 4
    if ext & 0x1000:
        cpu.fpcr, at = mem.r32(at), at + 4
    if ext & 0x0800:
        cpu.fpsr, at = mem.r32(at), at + 4
    if ext & 0x0400:
        at += 4
    return at


# -- FMOVEM.X <list>,-(An) ---------------------------------------------------------
def _fmovem_down(cpu, at: int, op: int, ext: int) -> int:
    """FMOVEM.X <list>,-(An): each FPn in the list, from FP7 down, put as
    an extended real 12 bytes below An, An being taken down to it, so that
    it's left at the last.  The list is the word's low byte, or Dn's, a
    bit n for each FPn: with none in it, An is left as it is."""
    n = op & 7
    listed = cpu.d(ext >> 4 & 7) if ext & DYNAMIC_LIST else ext
    a = cpu.a(n)
    for r in range(7, -1, -1):
        if listed >> r & 1:
            se, mantissa = cpu.fp(r)
            a = (a - EXTENDED) & 0xFFFFFFFF
            cpu.mem.write(a, struct.pack(">HHQ", se, 0, mantissa))
    cpu.set_a(n, a)
    return at + 4


# -- FMOVE.P -----------------------------------------------------------------------
def _fmove_packed(cpu, at: int, op: int, ext: int) -> int:
    """FMOVE.P <ea>,FPn or FMOVE.P FPn,<ea>{k}."""
    addr, end, an = _address(cpu, op >> 3 & 7, op & 7, at + 4, PACKED)
    mem = cpu.mem
    n = ext >> 7 & 7
    fpcr, fpsr = cpu.fpcr, cpu.fpsr & ~EXC
    if ext & 0x2000:                    # to memory
        k = cpu.d(ext >> 4 & 7) if ext >> 10 & 7 == P_DYNAMIC else ext
        data, raised = to_packed(*cpu.fp(n), _s8((k & 0x7F) << 1) >> 1, fpcr >> RND_SHIFT & 3)
        mem.write(addr, data)
    else:
        se, mantissa, raised = from_packed(mem.read(addr, PACKED), fpcr)
        cpu.set_fp(n, se, mantissa)
        fpsr = fpsr & ~FPCC | _condition(se, mantissa)
    fpsr |= raised
    if raised & (SNAN | OPERR):
        fpsr |= AEXC_IOP
    if raised & (INEX1 | INEX2):
        fpsr |= AEXC_INEX
    cpu.fpsr = fpsr
    if an is not None:
        cpu.set_a(op & 7, an)
    return end


def _condition(se: int, mantissa: int) -> int:
    """The FPSR's condition codes for an extended real."""
    cc = FPCC_N if se & 0x8000 else 0
    if se & MAX_EXPONENT == MAX_EXPONENT:
        return cc | (FPCC_NAN if mantissa & FRACTION else FPCC_I)
    return cc | (FPCC_Z if not mantissa else 0)


def _rounded(num: int, den: int, mode: int, negative: bool) -> tuple[int, bool]:
    """num / den, a positive fraction, rounded to an integer in the FPCR's
    rounding mode; and whether that's inexact."""
    q, r = divmod(num, den)
    if r and (mode == RN and (2 * r > den or 2 * r == den and q & 1)
              or mode == RM and negative or mode == RP and not negative):
        q += 1
    return q, r != 0


def from_packed(data: bytes, fpcr: int = 0) -> tuple[int, int, int]:
    """A packed decimal real, as an extended real's sign and exponent word
    and its mantissa, rounded to the FPCR's precision in its mode; and the
    FPSR's exception bits it raises.  Its digits are taken at their value,
    as the processor takes them, if one isn't a decimal digit."""
    w0, fraction = struct.unpack(">IQ", data)
    sign = w0 >> 16 & 0x8000
    if w0 & 0x7FFF0000 == 0x7FFF0000:               # SE, both Y bits and an exponent of $FFF
        if fraction & (QUIET - 1) and not fraction & QUIET:
            return sign | MAX_EXPONENT, fraction | QUIET, SNAN     # a signalling NaN: quiet
        return sign | MAX_EXPONENT, fraction, 0     # an infinity, or a NaN, its bits moved over
    digits = w0 & 0xF
    for shift in range(60, -4, -4):
        digits = digits * 10 + (fraction >> shift & 0xF)
    if not digits:
        return sign, 0, 0
    exponent = (w0 >> 24 & 0xF) * 100 + (w0 >> 20 & 0xF) * 10 + (w0 >> 16 & 0xF)
    exponent = (-exponent if w0 & 0x40000000 else exponent) - (DIGITS - 1)
    num, den = (digits * 10 ** exponent, 1) if exponent >= 0 else (digits, 10 ** -exponent)
    bits = PRECISIONS[fpcr >> PREC_SHIFT & 3]
    scale = num.bit_length() - den.bit_length() - bits + 1     # its lowest bit's power of two
    if num << max(0, -scale) < den << max(0, scale) << (bits - 1):
        scale -= 1                                  # num / den < 2 ** (scale + bits - 1)
    q, inexact = _rounded(num << max(0, -scale), den << max(0, scale),
                          fpcr >> RND_SHIFT & 3, bool(sign))
    if q >> bits:                                   # rounded up to the next power of two
        q, scale = q >> 1, scale + 1
    return (sign | (scale + bits - 1 + BIAS), q << (64 - bits), INEX1 if inexact else 0)


def to_packed(se: int, mantissa: int, k: int, mode: int = RN) -> tuple[bytes, int]:
    """An extended real as a packed decimal real, rounded in the FPCR's
    rounding mode to k digits after the point (k from -64 to 0) or to k
    significant digits (1 to 17, more being taken for 17); and the FPSR's
    exception bits it raises.  At least one digit, whatever k."""
    sign = (se & 0x8000) << 16
    exponent = se & MAX_EXPONENT
    raised = 0
    if exponent == MAX_EXPONENT:
        if mantissa & FRACTION and not mantissa & QUIET:
            mantissa, raised = mantissa | QUIET, SNAN       # a signalling NaN: as a quiet one
        elif not mantissa & FRACTION:
            mantissa = 0                                    # an infinity
        return struct.pack(">IQ", sign | 0x7FFF0000, mantissa), raised
    if not mantissa:
        return struct.pack(">IQ", sign, 0), 0
    if k > DIGITS:
        k, raised = DIGITS, OPERR
    power = exponent - BIAS - 63                    # the value is mantissa * 2 ** power
    num, den = (mantissa << power, 1) if power >= 0 else (mantissa, 1 << -power)

    def at_least(p: int) -> bool:                   # whether the value is at least 10 ** p
        return num * 10 ** max(0, -p) >= den * 10 ** max(0, p)
    log = int((num.bit_length() - den.bit_length()) * 0.30103)
    while not at_least(log):
        log -= 1
    while at_least(log + 1):
        log += 1
    length = k if k > 0 else max(1, min(DIGITS, log + 1 - k))
    scale = log + 1 - length
    digits, inexact = _rounded(num * 10 ** max(0, -scale), den * 10 ** max(0, scale),
                               mode, bool(sign))
    if digits == 10 ** length:                      # rounded up to the next power of ten
        digits, log = digits // 10, log + 1
    if inexact:
        raised |= INEX2
    digits *= 10 ** (DIGITS - length)
    if abs(log) > 999:
        raised |= OPERR
    e = abs(log)
    w0 = (sign | (0x40000000 if log < 0 else 0) | (e // 100 % 10) << 24 | (e // 10 % 10) << 20
          | (e % 10) << 16 | (e // 1000 % 10) << 12 | digits // 10 ** (DIGITS - 1))
    fraction = 0
    for d in str(digits % 10 ** (DIGITS - 1)).rjust(DIGITS - 1, "0"):
        fraction = fraction << 4 | int(d)
    return struct.pack(">IQ", w0, fraction), raised
