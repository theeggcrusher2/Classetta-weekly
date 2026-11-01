"""
The A-line trap dispatcher: what happens when 68k code executes a word
from $A000 to $AFFF.

Toolbox traps ($A800-$AFFF) take Pascal arguments on the stack: space
for the result first, then the arguments left to right, so the last is
on top.  The routine pops its arguments and leaves the result.  Bit 10
is auto-pop: the trap was reached by a JSR to glue, and it returns to
the address on top of the stack instead.  The trap number is the low 10
bits.

OS traps ($A000-$A7FF) take their arguments in registers, mostly A0 and
D0, and return a result in D0 (and in A0 when bit 8 is set).  Bits 9 and
10 are flags for the routine: SYS and CLEAR for the Memory Manager, HFS
and ASYNC for the File Manager.  The trap number is the low 8 bits.
After the routine the dispatcher tests D0.W, so code can branch on the
result.

Every trap's layout comes from trapdefs, generated from the Multiversal
Interfaces.  An implementation is a Python function registered with
@trap under its Inside Macintosh name.  It gets the trap context (see
runtime.Runner) and the arguments as Python ints, a Point for a Point,
in the order Inside Macintosh lists them, and returns the result.  An
Out<reg> argument isn't passed; the function returns (result, out...)
instead.  @raw registers a function that takes the trap context and the
trap word and works the registers and stack itself.

A trap with no implementation still has its arguments popped by its
signature and a zero result, unimpErr for an OSErr, and is logged once
by name.  One the interfaces don't describe stops the application: there
is no telling how much of the stack is its.
"""

from __future__ import annotations

import functools
import logging
import struct
from dataclasses import dataclass
from functools import cached_property
from typing import Callable, NamedTuple

from ..memory import BusError
from ..toolbox import lowmem
from ..toolbox.types import Point
from . import trapdefs

log = logging.getLogger("macrt.traps")

SIZES = {"b": 2, "B": 2, "h": 2, "H": 2, "e": 2, "l": 4, "L": 4, "p": 4, "P": 4, "E": 4}
TRAP_BITS = {"SYSBIT": 0x400, "CLRBIT": 0x200}
unimpErr = -4

# Traps the interfaces leave out or describe only as glue.
EXTRA_FUNCTIONS = {
    "LoadSeg": (0xA9F0, None, None, "h", "", (), None, ""),
    "UnloadSeg": (0xA9F1, None, None, "p", "", (), None, ""),
    "Chain": (0xA9F3, None, None, "", "e", (("A0", "p"),), "D0", ""),
    # Its parameter block's in A0 (IM II-29).
    "InitZone": (0xA019, None, None, "", "", (("A0", "p"),), None, "ReturnMemErr<D0>"),
    # PPostEvent is it with bit 8 set: the queue entry's address in A0.
    "PostEvent": (0xA02F, None, None, "", "e", (("A0", "h"), ("D0", "l"), ("TrapBit<0x100>", "b")),
                  "D0", ""),
    "Unimplemented": (0xA89F, None, None, "", "", (), None, ""),
    # The Start Manager's, a DefVideoRec or DefStartRec in A0 (Inside
    # Macintosh V).
    "GetVideoDefault": (0xA080, None, None, "", "", (("A0", "p"),), None, ""),
    "SetVideoDefault": (0xA081, None, None, "", "", (("A0", "p"),), None, ""),
    "GetDefaultStartup": (0xA07D, None, None, "", "", (("A0", "p"),), None, ""),
    # The routine's selector in D0, its SpBlock in A0 (Inside Macintosh V,
    # The Slot Manager).
    "SlotManager": (0xA06E, None, None, "", "e", (("A0", "p"), ("D0", "l")), "D0", ""),
    # The Vertical Retrace Manager's, theSlot in D0 and its result in D0
    # (Inside Macintosh: Processes).
    "AttachVBL": (0xA071, None, None, "", "e", (("D0", "h"),), "D0", ""),
    # The Deferred Task Manager's, the task in A0 and its result in D0
    # (Inside Macintosh: Processes).
    "DTInstall": (0xA082, None, None, "", "e", (("A0", "p"),), "D0", ""),
    "SetDefaultStartup": (0xA07E, None, None, "", "", (("A0", "p"),), None, ""),
    # The 64K ROM's, gone from later interfaces (IM I-121).
    "AddReference": (0xA9AC, None, None, "php", "", (), None, ""),
    "RmveReference": (0xA9AE, None, None, "p", "", (), None, ""),
    # Its glue pushes -1 after the pointer (MOVEQ #-1,D0; MOVE.L D0,-(SP)).
    "GetFrontProcess": (0xA88F, "OSDispatch", 0x39, "pl", "e", (), None, ""),
    # Not a trap: what GetStdFilterProc's procedure calls (see Machine).
    "StdFilterProc": (0xAA68, "DialogDispatch", 0x067F, "ppp", "b", (), None, ""),
    # Not traps either: what the System's standard definition procedures'
    # code calls (selectors as toolbox.system.STANDARD_DEFPROCS has them).
    "SystemWDEF0": (0xAA68, "DialogDispatch", 0x067E, "hphL", "l", (), None, ""),
    "SystemWDEF1": (0xAA68, "DialogDispatch", 0x067D, "hphL", "l", (), None, ""),
    "SystemMDEF0": (0xAA68, "DialogDispatch", 0x097C, "hppPp", "", (), None, ""),
    "SystemCDEF0": (0xAA68, "DialogDispatch", 0x067B, "hphL", "l", (), None, ""),
    "SystemCDEF1": (0xAA68, "DialogDispatch", 0x067A, "hphL", "l", (), None, ""),
    "SystemCDEF63": (0xAA68, "DialogDispatch", 0x0677, "hphL", "l", (), None, ""),
    "SystemLDEF0": (0xAA68, "DialogDispatch", 0x0A79, "hbpPhhp", "", (), None, ""),
    "SystemLDEF19": (0xAA68, "DialogDispatch", 0x0A76, "hbpPhhp", "", (), None, ""),
    "SystemMBDF0": (0xAA68, "DialogDispatch", 0x0578, "hhhL", "l", (), None, ""),
    # The interfaces leave out the keyword, which the selector counts.
    "AEManagerInfo": (0xA816, "Pack8", 0x0441, "Lp", "e", (), None, ""),
    # The interfaces leave out textLength, which the selector counts.
    "HiliteText": (0xA8B5, "ScriptUtil", 0x800E001C, "phhhp", "", (), None, ""),
    # LowercaseText and the rest push the routine's word last.
    "TextUtilFunctions": (0xA8B5, "ScriptUtil", 0x800AFFB6, "phhh", "", (), None, ""),
    # What the interfaces leave out (Inside Macintosh: Text, Text Utilities).
    "FormatRecToString": (0xA8B5, "ScriptUtil", 0x8210FFEA, "pppp", "h", (), None, ""),
    # The Menu Manager's for the Script Manager (Inside Macintosh: Macintosh
    # Toolbox Essentials, the Menu Manager), which the interfaces haven't.
    "InsertFontResMenu": (0xA825, "MenuDispatch", 0x0400, "phh", "", (), None, ""),
    "InsertIntlResMenu": (0xA825, "MenuDispatch", 0x0601, "plhh", "", (), None, ""),
    # Its menuID is an INTEGER and its resID a VAR one: three words, as its
    # selector counts (the interfaces give both as pointers).
    "HMGetMenuResID": (0xA830, "Pack14", 0x0314, "hp", "e", (), None, ""),
    # Its iconIndex is a Byte, which Pascal pushes as a word with the value
    # in its low byte (C's interface declares it a short).
    "SetItemIcon": (0xA940, None, None, "phH", "", (), None, ""),
    # A Pack6 routine the interfaces macrt has don't name, which Text (System
    # 7.5's control panel) calls with TextOrder's parameters to put the
    # items of its menus in order.
    "TextOrder30": (0xA9ED, "Pack6", 0x30, "pphhhhhh", "h", (), None, ""),
    # Two Process Manager routines the interfaces don't name either, with
    # no parameters and a word of result, which AppleScript calls on each
    # side of FSpOpenResFile.
    "OSDispatch40": (0xA88F, "OSDispatch", 0x40, "", "e", (), None, ""),
    "OSDispatch41": (0xA88F, "OSDispatch", 0x41, "", "e", (), None, ""),
    # Functions of an Extended, which the interfaces' signatures leave out:
    # the caller pushes the address of its Extended, then the argument, and
    # the address is left as the result (IM IV-65; MorseTrainer).
    "Fix2X": (0xA843, None, None, "l", "p", (), None, ""),
    "Frac2X": (0xA845, None, None, "l", "p", (), None, ""),
    # The SCSI Manager's, which the interfaces haven't: stack-based, with
    # the routine's selector in a word pushed last (IM IV and V, The SCSI
    # Manager).
    "SCSIReset": (0xA815, "SCSIDispatch", 0, "", "e", (), None, ""),
    "SCSIGet": (0xA815, "SCSIDispatch", 1, "", "e", (), None, ""),
    "SCSISelect": (0xA815, "SCSIDispatch", 2, "h", "e", (), None, ""),
    "SCSICmd": (0xA815, "SCSIDispatch", 3, "ph", "e", (), None, ""),
    "SCSIComplete": (0xA815, "SCSIDispatch", 4, "ppl", "e", (), None, ""),
    "SCSIRead": (0xA815, "SCSIDispatch", 5, "p", "e", (), None, ""),
    "SCSIWrite": (0xA815, "SCSIDispatch", 6, "p", "e", (), None, ""),
    "SCSIRBlind": (0xA815, "SCSIDispatch", 8, "p", "e", (), None, ""),
    "SCSIWBlind": (0xA815, "SCSIDispatch", 9, "p", "e", (), None, ""),
    "SCSIStat": (0xA815, "SCSIDispatch", 10, "", "h", (), None, ""),
    "SCSISelAtn": (0xA815, "SCSIDispatch", 11, "h", "e", (), None, ""),
    "SCSIMsgIn": (0xA815, "SCSIDispatch", 12, "p", "e", (), None, ""),
    "SCSIMsgOut": (0xA815, "SCSIDispatch", 13, "h", "e", (), None, ""),
}
EXTRA_DISPATCHERS = {"MenuDispatch": (0xA825, "D0W"), "SCSIDispatch": (0xA815, "StackW")}
STD_FILTER_SELECTOR = 0x067F
EXTRA_NAMES = {0xA02F: "PostEvent", 0xA89F: "Unimplemented", 0xA9F2: "Launch",
               0xA9EB: "FP68K", 0xA9EC: "Elems68K", 0xABF2: "ThreadDispatch"}
# Inside Macintosh's names for routines the interfaces name otherwise.
RENAMED = {"ROMlib_Fpstr2dec": "PStr2Dec", "ROMlib_Fcstr2dec": "CStr2Dec",
           "ROMlib_Fdec2str": "Dec2Str", "IsMetric": "IUMetric",
           "GetIntlResource": "IUGetIntl", "SetIntlResource": "IUSetIntl",
           "ClearIntlResourceCache": "IUClearCache", "GetIntlResourceTable": "IUGetItlTable"}


# Pascal procedures after which macrt leaves D0 as the caller had it,
# rather than 0 or, for the Resource Manager's, ResErr (RESERR_FLAGS):
# DeadEnd 1.0's checksum routine, a C function, works its sum out in D0,
# ends with ReleaseResource and returns whatever D0 holds.
KEEPS_D0 = frozenset({"ReleaseResource"})


def is_toolbox(word: int) -> bool:
    return bool(word & 0x0800)


def trap_key(word: int) -> int:
    """The trap word with its flag bits cleared: which routine it is."""
    return (0xA800 | (word & 0x3FF)) if word & 0x0800 else (0xA000 | (word & 0xFF))


@dataclass(frozen=True)
class Signature:
    name: str
    trap: int
    dispatcher: str | None
    selector: int | None
    stack: str                  # argument codes, declaration order
    result: str
    regs: tuple                 # (register, code) pairs
    result_reg: str | None
    extras: str

    @property
    def stack_bytes(self) -> int:
        return sum(SIZES[c] for c in self.stack)

    @property
    def os_style(self) -> bool:
        """Registers in, D0 out: everything but Pascal stack calls."""
        return bool(self.regs) or self.result_reg is not None or not is_toolbox(self.trap)

    @cached_property
    def plan(self) -> Plan:
        """How call() is to call it, worked out once."""
        os_style = self.os_style
        d0 = None
        if "ReturnMemErrConditional" in self.extras:
            d0 = "mem_err_if_any"
        elif "ReturnMemErr" in self.extras:
            d0 = "mem_err"
        elif "ClearD0" in self.extras:
            d0 = "clear"
        elif not self.result and not os_style and self.name not in KEEPS_D0:
            d0 = "clear_if_implemented"
        result_reg = None
        if self.result and self.result_reg != "D0Minus1Boolean" and (self.result_reg or os_style):
            result_reg = (self.result_reg or "D0", self.result)
        return Plan(
            stack=self.stack,
            reads=tuple(_reader(reg, code) for reg, code in self.regs
                        if not reg.startswith("Out<")),
            stack_result=self.result if self.result and not os_style else "",
            unimplemented=unimpErr if self.result in ("e", "E") else 0,
            outs=tuple((reg[reg.index("<") + 1:-1], code) for reg, code in self.regs
                       if reg.startswith("Out<") or reg.startswith("InOut<")),
            minus1_boolean=self.result_reg == "D0Minus1Boolean",
            result_reg=result_reg,
            move_a1_to_a0="MoveA1ToA0" in self.extras,
            d0=d0,
            cc_from_d0="CCFromD0" in self.extras)


class Plan(NamedTuple):
    """A Signature as call() goes through it."""
    stack: str                  # the Pascal arguments' codes
    reads: tuple                # a reader(cpu, word) for each register argument
    stack_result: str           # the result's code, if it's left on the stack
    unimplemented: int          # the result with no implementation
    outs: tuple                 # (register, code) for each Out or InOut
    minus1_boolean: bool        # a Boolean in D0, 0 for TRUE
    result_reg: tuple | None    # (register, code) the result goes in
    move_a1_to_a0: bool
    d0: str | None              # what's left in D0 after: see call()
    cc_from_d0: bool


def _signatures() -> dict[str, Signature]:
    table = {**trapdefs.FUNCTIONS, **EXTRA_FUNCTIONS}
    return {RENAMED.get(name, name): Signature(RENAMED.get(name, name), *row)
            for name, row in table.items()}


SIGNATURES = _signatures()
BY_TRAP: dict[int, Signature] = {}
# Where a dispatcher looks for its selector, when that's not where the
# interfaces' table says: QDExtensions finds the size of the arguments in
# D0's high word, which 32-Bit QuickDraw's first interfaces leave 0 (Pico's
# NewGWorld is MOVEQ #0,D0).
SELECTOR_LOCATIONS = {"QDExtensions": "D0<0xFFFF>"}
# key -> (name, location, selectors)
DISPATCHERS: dict[int, tuple[str, str, dict[int, Signature]]] = {}
_ALL_DISPATCHERS = {**trapdefs.DISPATCHERS, **EXTRA_DISPATCHERS}
for _d_name, (_d_trap, _d_loc) in _ALL_DISPATCHERS.items():
    DISPATCHERS[trap_key(_d_trap)] = (_d_name, SELECTOR_LOCATIONS.get(_d_name, _d_loc), {})
for _sig in SIGNATURES.values():
    if _sig.dispatcher:
        _d = DISPATCHERS.get(trap_key(_ALL_DISPATCHERS[_sig.dispatcher][0]))
        if _d is not None and _sig.selector is not None:
            # By the part of D0 its dispatcher looks at: a selector's high
            # bits, the size of its arguments, aren't (the Sound Manager's,
            # which older interfaces leave 0, or PaletteDispatch's).
            _mask = int(_d[1][3:-1], 0) if _d[1].startswith("D0<") else -1
            _d[2].setdefault(_sig.selector & _mask, _sig)
    else:
        BY_TRAP.setdefault(trap_key(_sig.trap), _sig)
NAMES = {trap_key(t): n for t, n in trapdefs.TRAP_NAMES.items()}
NAMES.update({trap_key(s.trap): s.name for s in BY_TRAP.values()})
NAMES.update({trap_key(t): n for t, n in EXTRA_NAMES.items()})
for _key, (_d_name, _, _) in DISPATCHERS.items():
    NAMES.setdefault(_key, _d_name)


def trap_name(word: int) -> str:
    return NAMES.get(trap_key(word), f"${word:04X}")


# =========================================================================
# The registry
# =========================================================================
Impl = Callable[..., object]
IMPLEMENTATIONS: dict[str, Impl] = {}
RAW: dict[int, Callable] = {}
NAMERS: dict[int, Callable] = {}
RESERR_FLAGS: set[int] = set()      # trap keys that leave ResErr in D0 and the flags


def trap(fn: Impl) -> Impl:
    """Register fn as the implementation of the trap it's named after."""
    if fn.__name__ not in SIGNATURES:
        raise KeyError(f"no trap called {fn.__name__}")
    IMPLEMENTATIONS[fn.__name__] = fn
    return fn


def raw(word: int):
    """Register a function that handles a trap word on its own: it gets
    the trap context and the trap word, with PC already past the trap."""
    def register(fn):
        RAW[trap_key(word)] = fn
        return fn
    return register


def namer(word: int):
    """Register a function that names the routine a call to a @raw
    package is for; it gets memory and the address of the arguments."""
    def register(fn):
        NAMERS[trap_key(word)] = fn
        return fn
    return register


def assigned(key: int) -> bool:
    """Does the trap word name a routine, implemented or not?"""
    return key in RAW or key in BY_TRAP or key in DISPATCHERS


def implemented(key: int) -> bool:
    """Is there anything behind this trap?  (What GetTrapAddress tells
    TrapAvailable.)"""
    if key in RAW:
        return True
    sig = BY_TRAP.get(key)
    if sig is not None:
        return sig.name in IMPLEMENTATIONS
    d = DISPATCHERS.get(key)
    return d is not None and any(s.name in IMPLEMENTATIONS for s in d[2].values())


# =========================================================================
# Calling an implementation
# =========================================================================
class UnknownTrap(Exception):
    def __init__(self, word: int, pc: int, detail: str = ""):
        self.word, self.pc = word, pc
        super().__init__(f"unimplemented trap {trap_name(word)} (${word:04X}) at ${pc:06X}"
                         + (f": {detail}" if detail else ""))


def _reader(reg: str, code: str) -> Callable:
    """A function of the CPU and the trap word that reads an argument from
    a register (or the trap word's bits)."""
    if reg.startswith("TrapBit<"):
        mask = reg[8:-1]
        bit = TRAP_BITS[mask] if mask in TRAP_BITS else int(mask, 0)
        return lambda cpu, word: bool(word & bit)
    if reg.startswith("InOut<"):
        reg = reg[6:-1]
    if reg in ("D0LowWord", "D0HighWord"):
        shift = 16 if reg == "D0HighWord" else 0
        signed = code == "h"

        def half(cpu, word):
            v = cpu.d(0) >> shift & 0xFFFF
            return v - 0x10000 if signed and v & 0x8000 else v
        return half
    n = int(reg[1])
    if reg[0] == "D":
        return lambda cpu, word: _from_long(cpu.d(n), code)
    return lambda cpu, word: _from_long(cpu.a(n), code)


def _from_long(v: int, code: str):
    if code in "hHe":
        v &= 0xFFFF
        return v - 0x10000 if code != "H" and v & 0x8000 else v
    if code in "bB":
        v &= 0xFF
        return v - 0x100 if code == "b" and v & 0x80 else v
    if code == "P":
        return Point(_sign16(v >> 16), _sign16(v))
    if code in "lE":
        return v - 0x100000000 if v & 0x80000000 else v
    return v


def _to_long(v, code: str) -> int:
    if type(v) is int:
        return v & 0xFFFFFFFF
    if v is None or v is False:
        return 0
    if v is True:
        return 1
    if code == "P":
        return ((v.v & 0xFFFF) << 16) | (v.h & 0xFFFF)
    if hasattr(v, "addr"):
        v = v.addr
    return int(v) & 0xFFFFFFFF


def _sign16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _write_reg(cpu, reg: str, code: str, value) -> None:
    v = _to_long(value, code)
    if code in "hHe" and v & 0x8000 and code != "H":
        v |= 0xFFFF0000             # an OSErr comes back sign-extended, as from MOVEQ
    elif code in "hHe":
        v &= 0xFFFF
    if reg[0] == "D":
        cpu.set_d(int(reg[1]), v)
    else:
        cpu.set_a(int(reg[1]), v)


# How an argument of each code is on the stack, for struct: a byte's in
# the high byte of its word, and a Point is its v, then its h.
STACK_FORMATS = {"b": "bx", "B": "Bx", "h": "h", "H": "H", "e": "h", "P": "hh", "l": "i",
                 "E": "i"}


@functools.cache
def _stack_layout(codes: str) -> tuple[struct.Struct, bool]:
    """Arguments of codes as they are on the stack, from the last, at the
    top, to the first; and whether there's a Point among them."""
    return (struct.Struct(">" + "".join(STACK_FORMATS.get(c, "I") for c in reversed(codes))),
            "P" in codes)


def pop_args(mem, sp: int, codes: str) -> tuple[list, int]:
    """Pascal arguments from the stack, in declaration order, and the
    address just past them (where the result goes)."""
    layout, points = _stack_layout(codes)
    values = mem.unpack(layout, sp)
    if not points:
        args = list(values)
    else:
        args, i = [], 0
        for c in reversed(codes):
            if c == "P":
                args.append(Point(values[i], values[i + 1]))
                i += 2
            else:
                args.append(values[i])
                i += 1
    args.reverse()
    return args, sp + layout.size


def put_result(mem, at: int, code: str, value) -> None:
    v = _to_long(value, code)
    if code in "bB":
        # The high byte of the word, and the low byte cleared: code that
        # leaves the slot as it found it tests the whole word (MacWrite
        # does after IsDialogEvent), and the ROM's routines clear it.
        mem.w16(at, (v & 0xFF) << 8)
    elif code in "hHe":
        mem.w16(at, v)
    elif code:
        mem.w32(at, v)


def call(t, sig: Signature, word: int) -> None:
    """Run sig's implementation, or skip it, with the CPU at the trap's
    arguments and PC already where the trap returns to."""
    cpu, mem = t.cpu, t.mem
    plan = sig.plan
    impl = IMPLEMENTATIONS.get(sig.name)
    if plan.stack or plan.stack_result:
        args, result_at = pop_args(mem, cpu.sp, plan.stack)
    else:
        args, result_at = [], 0
    for read in plan.reads:
        args.append(read(cpu, word))
    outs = plan.outs
    if impl is None:
        t.unimplemented(sig.name, word)
        value = plan.unimplemented
        outs_values = [0] * len(outs)
    else:
        value = impl(t, *args)
        outs_values = ()
        if outs:
            value, *outs_values = value
    # Pascal: drop the arguments, leave the result
    if plan.stack:
        cpu.sp = result_at
    if plan.stack_result:
        put_result(mem, result_at, plan.stack_result, value)
    for (reg, code), out in zip(outs, outs_values):
        _write_reg(cpu, reg, code, out)
    if plan.minus1_boolean:
        cpu.set_d(0, 0 if value else 0xFFFFFFFF)      # 0 for TRUE, as the OS returns it
    elif plan.result_reg is not None:
        _write_reg(cpu, plan.result_reg[0], plan.result_reg[1], value)
    if plan.move_a1_to_a0:
        cpu.set_a(0, cpu.a(1))
    d0 = plan.d0
    if d0 is None:
        pass
    elif d0 == "mem_err_if_any":
        if t.mem_err:                                   # a size on success, else the error
            cpu.set_d(0, t.mem_err & 0xFFFFFFFF)
    elif d0 == "mem_err":
        cpu.set_d(0, t.mem_err & 0xFFFFFFFF)
    elif d0 == "clear" or impl is not None:
        # A Pascal routine with no result leaves D0 as it likes: stack-based
        # traps keep only A2-A6 and D3-D7 (IM I, Using Assembly Language).
        # macrt leaves 0 there, not what the caller had, for C functions
        # that fall off their end after one and return whatever D0 holds
        # (BMAPEdit's bitmap routine, ending with SetPort).
        cpu.set_d(0, 0)
    if plan.cc_from_d0:
        # HandToHand and the like are numbered as Toolbox traps but end as
        # the OS's do, with the flags set from D0: Radical Castle branches on
        # them straight after.
        cpu.set_cc_from_word(cpu.d(0))


def _read_selector(t, location: str, word: int, sp: int) -> tuple[int, int]:
    """A dispatcher's selector, and how many bytes of stack it takes up."""
    cpu, mem = t.cpu, t.mem
    if location == "StackW":
        return mem.r16(sp), 2
    if location == "StackL":
        return mem.r32(sp), 4
    if location.startswith("StackWLookahead<"):
        return mem.r16(sp) & int(location[16:-1], 0), 0
    if location.startswith("StackWMasked<"):
        return mem.r16(sp) & int(location[13:-1], 0), 2
    if location == "D0W":
        return cpu.d(0) & 0xFFFF, 0
    if location == "D0L":
        return cpu.d(0), 0
    if location.startswith("D0<"):
        return cpu.d(0) & int(location[3:-1], 0), 0
    if location == "TrapBits":
        return word & 0x0600, 0
    raise ValueError(f"selector location {location}")


def selector(t, location: str, word: int) -> int:
    """Read (and, from the stack, pop) a dispatcher's selector."""
    sel, size = _read_selector(t, location, word, t.cpu.sp)
    if size:
        t.cpu.sp = t.cpu.sp + size
    return sel


def _routine(location: str, table: dict[int, Signature], sel: int) -> Signature | None:
    sig = table.get(sel)
    if sig is None and location == "D0W":
        sig = next((s for s in table.values() if (s.selector & 0xFF) == (sel & 0xFF)), None)
    return sig


def describe(t, word: int) -> str:
    """The trace's name for a trap about to be dispatched: for a package,
    the routine its selector picks."""
    key = trap_key(word)
    sp = t.cpu.sp + (4 if is_toolbox(word) and word & 0x400 else 0)
    try:
        if key in NAMERS:
            return NAMERS[key](t.mem, sp)
        d = DISPATCHERS.get(key)
        if d is not None and key not in RAW and key not in BY_TRAP:
            sig = _routine(d[1], d[2], _read_selector(t, d[1], word, sp)[0])
            if sig is not None:
                return sig.name
    except BusError:        # a stack pointer it can't read from: just the trap
        pass
    return trap_name(word)


def dispatch(t, word: int, pc: int) -> None:
    """Handle an A-line trap whose implementation is in Python.  PC is
    already where it returns to; for an auto-pop trap the return address
    has been popped."""
    key = trap_key(word)
    handler = RAW.get(key)
    if handler is not None:
        handler(t, word)
        return
    sig = BY_TRAP.get(key)
    if sig is None:
        d = DISPATCHERS.get(key)
        if d is None:
            raise UnknownTrap(word, pc)
        name, location, table = d
        sel = selector(t, location, word)
        sig = _routine(location, table, sel)
        if sig is None:
            if location == "D0W" and is_toolbox(word):
                # Most D0W packages put the size of the arguments, in words,
                # in the high byte, so the stack can still be cleaned up.
                t.unimplemented(f"{name} selector ${sel:04X}", word)
                t.cpu.sp = t.cpu.sp + 2 * (sel >> 8)
                t.cpu.set_d(0, unimpErr & 0xFFFFFFFF)
                return
            if name == "SoundDispatch" and t.cpu.d(0) >> 24:
                # Sound Manager 3's selectors have the size of the
                # arguments, in words, in D0's high byte, and its routines
                # with arguments return an OSErr.
                t.unimplemented(f"{name} selector ${t.cpu.d(0):08X}", word)
                t.cpu.sp = t.cpu.sp + 2 * (t.cpu.d(0) >> 24)
                t.mem.w16(t.cpu.sp, unimpErr & 0xFFFF)
                t.cpu.set_d(0, unimpErr & 0xFFFFFFFF)
                return
            if name == "ScriptUtil":
                # The selector has the size of the arguments, and bit 30 for
                # a script code after them, and the size of the result.
                t.unimplemented(f"{name} selector ${sel:08X}", word)
                t.cpu.sp = t.cpu.sp + (sel >> 16 & 0xFF) + (2 if sel & 0x40000000 else 0)
                t.mem.write(t.cpu.sp, bytes(sel >> 24 & 0x3F))
                return
            raise UnknownTrap(word, pc, f"{name} selector ${sel:X}")
    call(t, sig, word)
    if key in RESERR_FLAGS:
        err = t.process.world.r16(lowmem.ResErr)
        t.cpu.set_d(0, err | (0xFFFF0000 if err & 0x8000 else 0))
        t.cpu.set_cc_from_word(err)
