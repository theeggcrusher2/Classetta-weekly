"""The OS traps as a program calls them: the Memory Manager's calls and
their errors, the trap table, Gestalt and SysEnvirons, OS events, the
date and time, the OS's string comparisons, VBL tasks, parameter RAM,
and launching an application that isn't there (Inside Macintosh II,
the Memory Manager, the OS Utilities, the Segment Loader)."""

import contextlib
import struct
from types import SimpleNamespace

import pytest
from guest import Processor

from macrt.m68k import glue_os as os_
from macrt.macfile import MacFile
from macrt.resfork import Resource, build_fork
from macrt.toolbox import lowmem
from macrt.toolbox.constants import activateEvt, kHighLevelEvent, osEvt
from macrt.toolbox.events import EventRecord
from macrt.toolbox.memmgr import memFullErr, memWZErr, nilHandleErr, noErr
from macrt.toolbox.types import Point

NOWHERE = 0x10                  # low memory: in no zone


def ostype(s: str) -> int:
    return struct.unpack(">I", s.encode("mac_roman"))[0]


def regs(g, word: int, d0: int = 0, a0: int = 0, a1: int = 0) -> int:
    """The register-based trap word run with D0, A0 and A1 set; D0 after."""
    g.run(struct.pack(">HI HI HI H", 0x207C, a0, 0x227C, a1, 0x203C, d0, word))
    return g.cpu.d(0)


@pytest.fixture
def g(qapp):
    return Processor()


def test_the_memory_managers_errors(g):
    g("DisposeHandle", 0)
    assert g.mem_err == nilHandleErr
    g("HLock", 0)
    assert g.mem_err == nilHandleErr
    assert g("GetHandleSize", 0) == 0 and g.mem_err == nilHandleErr
    # An address in no zone.
    assert g("GetPtrSize", NOWHERE) == 0 and g.mem_err == memWZErr
    g("SetPtrSize", NOWHERE, 4)
    assert g.mem_err == memWZErr
    assert g("GetHandleSize", NOWHERE) == 0 and g.mem_err == memWZErr
    assert g("RecoverHandle", NOWHERE, False) == 0 and g.mem_err == memWZErr
    # An empty handle has no size, until it's given a block.
    h = g("NewEmptyHandle", False)
    assert g.mem_err == noErr and g.heap.is_empty(h)
    assert g("GetHandleSize", h) == 0 and g.mem_err == nilHandleErr
    g("ReallocateHandle", h, 16)
    assert g.mem_err == noErr and g("GetHandleSize", h) == 16
    assert g("RecoverHandle", g.heap.deref(h), False) == h
    g("DisposeHandle", h)
    assert g.mem_err == noErr and not g.heap.is_handle(h)
    # A pointer's size, made smaller.
    p = g("NewPtr", 32, False, True)
    assert g("GetPtrSize", p) == 32
    g("SetPtrSize", p, 8)
    assert g.mem_err == noErr and g("GetPtrSize", p) == 8


def test_a_handles_state(g):
    h = g.handle(b"abcd")
    g("HSetState", h, 0xC0)
    assert g.heap.get_state(h) == 0xC0          # locked and purgeable
    g("HNoPurge", h)
    assert g.heap.get_state(h) == 0x80
    g("HSetRBit", h)
    assert g.heap.get_state(h) == 0xA0
    g("HClrRBit", h)
    assert g.heap.get_state(h) == 0x80 and g.mem_err == noErr


def test_zones_and_what_is_free_in_them(g):
    # A TheZone that is no zone is the application's heap.
    g.process.world.w32(lowmem.TheZone, NOWHERE)
    p = g("NewPtr", 8, False, False)
    assert g.heap.is_ptr(p)
    g.process.world.w32(lowmem.TheZone, g.heap.start)
    for name in ("InitApplZone", "SetGrowZone"):
        g.set_mem_err(memFullErr)
        g(name, *(() if name == "InitApplZone" else (0x5000,)))
        assert g.mem_err == noErr
    largest = g("MaxBlock", False)
    assert largest > 0
    assert g("CompactMem", 0, False) == largest
    assert g("CompactMem", 0x7FFFFFFF, False) == g.heap.max_block()
    _, total, contiguous = g("PurgeSpace", False)
    assert total >= contiguous > 0
    g.cpu.sp = g.heap.end + 0x100
    assert g("StackSpace") == 0x100
    g.cpu.sp = g.heap.start
    assert g("StackSpace") == 0


def test_block_move(g):
    src, dst = g.ptr(b"abcdef"), g.ptr(bytes(6))
    g("BlockMove", src, dst, 0, False)
    assert g.mem.read(dst, 6) == bytes(6)
    g.set_mem_err(memFullErr)
    g("BlockMove", src, dst, 4, False)
    assert g.mem.read(dst, 6) == b"abcd\0\0" and g.mem_err == noErr


def test_handles_filled_from_pointers_and_other_handles(g):
    src = g.ptr(b"hello, world")
    h = g.handle(b"old")
    assert g("PtrToXHand", src, h, 5) == noErr and g.contents(h) == b"hello"
    assert g("PtrToXHand", src, 0, 5) == nilHandleErr
    assert g("PtrToXHand", src, h, -3) == noErr and g.contents(h) == b""
    a, b = g.handle(b"ab"), g.handle(b"cd")
    assert g("HandAndHand", a, b) == noErr and g.contents(b) == b"cdab"
    assert g("HandAndHand", 0, b) == nilHandleErr
    assert g("HandAndHand", a, g("NewEmptyHandle", False)) == nilHandleErr
    assert g("PtrAndHand", src + 7, b, 5) == noErr and g.contents(b) == b"cdabworld"
    assert g("PtrAndHand", src, 0, 5) == nilHandleErr
    # A locked block can't grow past what there is room for.
    locked = g.handle(b"xy")
    g("HLock", locked)
    assert g("PtrAndHand", src, locked, g.heap.max_block() + 64) == memFullErr
    assert g.contents(locked) == b"xy"


def test_the_trap_table(g):
    g.patches = {}
    stub = g.machine.stub_for(0xA01E)                       # NewPtr
    assert g("GetTrapAddress", 0x1E, True, False) == stub
    assert g("GetTrapAddress", 0x1E, False, False) == stub  # the 64K ROM's table
    g("SetTrapAddress", 0x5000, 0x1E, True, False)
    assert g("GetTrapAddress", 0x1E, True, False) == 0x5000
    g("SetTrapAddress", stub, 0x1E, True, False)            # the original put back
    assert g.patches == {} and g("GetTrapAddress", 0x1E, True, False) == stub
    unimplemented = g.machine.stub_for(os_.UNIMPLEMENTED)
    assert g("GetTrapAddress", 0x8F, True, True) == unimplemented    # _OSDispatch
    assert g("GetTrapAddress", 0x303, True, True) == unimplemented   # QD32Trap on a Plus
    assert g("GetTrapAddress", 0x6E, True, False) == unimplemented   # no slots
    assert g("GetTrapAddress", 0x9F, True, True) == unimplemented    # _Unimplemented itself
    # The survey hears what was asked for and isn't there, but not about
    # _Unimplemented, which a program asks for to compare the others with.
    assert g.missing_asked == ["OSDispatch", "$AB03", "SlotManager"]
    errors = []
    g.system_error = lambda error_id, detail="": errors.append(error_id)
    g.run(struct.pack(">H", os_.QD32TRAP))
    g.run(struct.pack(">H", os_.UNIMPLEMENTED))
    assert errors == [12, 12]


def test_gestalt_selectors_of_a_programs_own(g):
    assert g("Gestalt", ostype("zzzz")) == (os_.gestaltUndefSelectorErr, 0)
    assert g("Gestalt", 0x00000001) == (os_.gestaltUndefSelectorErr, 0)
    assert g.missing_asked == ["Gestalt 'zzzz'", "Gestalt $00000001"]
    assert g("NewGestalt", ostype("vers"), 0x5000) == os_.gestaltDupSelectorErr
    assert g("ReplaceGestalt", ostype("zzzz"), 0x5000) == (os_.gestaltUndefSelectorErr, 0)
    assert g("ReplaceGestalt", ostype("vers"), 0x5000) == (noErr, 0)
    g.procs[0x5000] = lambda selector, response: g.mem.w32(response, 42)
    assert g("Gestalt", ostype("vers")) == (noErr, 42)
    assert g("ReplaceGestalt", ostype("vers"), 0x6000) == (noErr, 0x5000)
    assert len(g.missing_asked) == 2
    p = g.ptr(bytes(os_.SYSENV_SIZE))
    assert g("SysEnvirons", 0, p) == os_.envBadVers
    assert g("SysEnvirons", 3, p) == os_.envVersTooBig and g.mem.r16(p) == 2


def test_os_events(g):
    assert g("PostEvent", 3, 0x61, False) == noErr          # keyDown
    p = g.ptr(bytes(16))
    assert g("OSEventAvail", 0xFFFF, p)
    assert (g.mem.r16(p), g.mem.r32(p + 2)) == (3, 0x61)
    assert g("GetOSEvent", 0xFFFF, p) and not g("OSEventAvail", 0xFFFF, p)
    g.process.events.enabled = lambda: 0                    # SysEvtMask leaves it out
    assert g("PostEvent", 3, 0x62, False) == os_.evtNotEnb
    rec = g.ptr(bytes(36))
    assert g("NMRemove", rec) == os_.nmTypErr


def test_os_events_are_the_low_level_ones(g):
    """GetOSEvent "does not return activate, update, operating-system, or
    high-level events" (IM: Macintosh Toolbox Essentials, Event Manager,
    "Receiving Events"), and OSEventAvail reads the same queue.  G-Netix
    calls GetOSEvent with every bit of its mask set before it takes its
    Open Application event from WaitNextEvent."""
    oapp = ostype("oapp")
    where = Point(oapp >> 16, oapp & 0xFFFF)
    g.process.events.post(EventRecord(kHighLevelEvent, ostype("aevt"), 0, where, 0))
    g.process.events.post(EventRecord(osEvt, 0x01000001, 0, Point(0, 0), 0))    # resume
    g.process.events.post_activate(0x5000, True)
    assert g("PostEvent", 3, 0x61, False) == noErr          # keyDown
    p = g.ptr(bytes(16))
    assert g("GetOSEvent", 0xFFFF, p) and g.mem.r16(p) == 3
    assert not g("OSEventAvail", 0xFFFF, p) and not g("GetOSEvent", 0xFFFF, p)
    left = [g.process.events.get(0xFFFF, 0, lambda: None)[1].what for _ in range(3)]
    assert sorted(left) == [activateEvt, osEvt, kHighLevelEvent]


def test_ppostevent_gives_the_events_queue_entry(g):
    """_PPostEvent, PostEvent with bit 8 set, leaves the event's queue
    entry (an EvQEl) in A0: what the program puts there, it gets.  Troggle
    Trouble Math passes a pointer in evtQWhen and a flag in evtQModifiers."""
    assert regs(g, 0xA12F, d0=1000, a0=14) == noErr         # app3Evt
    qel = g.cpu.a(0)
    assert qel and g.mem.r16(qel + 4) == os_.evType
    assert (g.mem.r16(qel + 6), g.mem.r32(qel + 8)) == (14, 1000)
    g.mem.w32(qel + 12, 0x123456)
    g.mem.w16(qel + 20, 0x0201)
    p = g.ptr(bytes(16))
    assert g("OSEventAvail", 0xFFFF, p)                     # as it's left, looked at
    assert struct.unpack(">hLL", g.mem.read(p, 10)) == (14, 1000, 0x123456)
    assert g("GetOSEvent", 0xFFFF, p)                       # and got
    assert (g.mem.r32(p + 6), g.mem.r16(p + 14)) == (0x123456, 0x0201)
    # Once it's been got, its entry is the next one's.
    assert regs(g, 0xA12F, d0=1001, a0=14) == noErr and g.cpu.a(0) == qel
    assert regs(g, 0xA12F, d0=1002, a0=14) == noErr and g.cpu.a(0) != qel
    # Plain PostEvent leaves A0 as it was.
    assert regs(g, 0xA02F, d0=1003, a0=14) == noErr and g.cpu.a(0) == 14


def test_the_date_and_time(g):
    p = g.ptr(bytes(14))
    assert g("ReadDateTime", p) == noErr
    assert g.mem.read(p, 4) == g.mem.read(lowmem.Time, 4)
    assert g("SetDateTime", 0) == noErr
    g("SecondsToDate", 0, p)
    assert struct.unpack(">7h", g.mem.read(p, 14)) == (1904, 1, 1, 0, 0, 0, 6)   # a Friday
    assert g("DateToSeconds", p) == (None, 0)
    g.mem.write(p, struct.pack(">7h", 1984, 1, 24, 12, 0, 0, 0))
    _, secs = g("DateToSeconds", p)
    g("SecondsToDate", secs, p)
    assert struct.unpack(">6h", g.mem.read(p, 12)) == (1984, 1, 24, 12, 0, 0)
    g.mem.write(p, struct.pack(">7h", 1984, 13, 1, 0, 0, 0, 0))     # no such month
    assert g("DateToSeconds", p) == (None, 0)
    assert g("StripAddress", 0xFF123456) == 0x123456
    regs(g, 0xA193)                                         # Microseconds
    first = g.cpu.a(0) << 32 | g.cpu.d(0)
    regs(g, 0xA193)
    assert g.cpu.a(0) << 32 | g.cpu.d(0) >= first > 0


def test_the_os_compares_strings(g):
    def compare(word, a, b):
        return regs(g, word, len(a) << 16 | len(b), g.ptr(a), g.ptr(b))

    assert compare(0xA03C, b"abc", b"ABC") == 0             # EqualString
    assert compare(0xA43C, b"abc", b"ABC") == 1             # case matters
    assert compare(0xA03C, b"\x8E", b"e") == 1              # é isn't e,
    assert compare(0xA23C, b"\x8E", b"e") == 0              # unless marks are ignored
    assert compare(0xA050, b"abc", b"abd") == 0xFFFFFFFF    # RelString
    assert compare(0xA050, b"abd", b"ABC") == 1
    assert compare(0xA050, b"ABC", b"abc") == 0
    assert compare(0xA450, b"ABC", b"abc") == 0xFFFFFFFF


def test_vbl_tasks_of_the_wrong_type(g):
    task = g.ptr(bytes(14))
    assert g("VInstall", task) == os_.vTypErr
    assert g("VRemove", task) == os_.vTypErr
    g.mem.w16(task + os_.QTYPE, os_.vType)
    assert g("SlotVInstall", task, 0) == noErr
    assert g.mem.r32(lowmem.VBLQueue + 2) == task
    assert g("SlotVRemove", task, 0) == noErr
    assert g.mem.r32(lowmem.VBLQueue + 2) == 0


def test_attach_vbl_takes_the_screen_s_slot_only(g):
    """Snd2SysBeep gives AttachVBL the slot GetVideoDefault names."""
    from macrt.toolbox.gdevice import VIDEO_SLOT
    assert g("AttachVBL", VIDEO_SLOT) == noErr
    assert g("AttachVBL", VIDEO_SLOT + 1) == os_.slotNumErr
    assert g("AttachVBL", 0) == os_.slotNumErr


def test_parameter_ram(g):
    assert g("InitUtil") == noErr and g("WriteParam") == noErr
    buf = g.ptr(b"\xFF" * 8)
    assert regs(g, 0xA051, 4 << 16 | 0x10, buf) == 0       # ReadXPRam
    assert g.mem.read(buf, 8) == bytes(4) + b"\xFF" * 4
    assert regs(g, 0xA052, 4 << 16 | 0x10, buf) == 0       # WriteXPRam


def test_launching_an_application_that_isnt_there(g):
    pb = g.ptr(bytes(40))
    g.mem.w32(pb, g.ptr(b"\x07Nothing"))
    assert regs(g, 0xA9F2, 0, pb) == os_.fnfErr & 0xFFFFFFFF
    # By an FSSpec, in a LaunchParamBlockRec.
    spec = g.ptr(struct.pack(">hI", g.process.files.get_vol()[0].vref, 2) + b"\x07Nothing")
    g.mem.write(pb, bytes(40))
    g.mem.w16(pb + 6, os_.LC)
    g.mem.w32(pb + 16, spec)
    assert regs(g, 0xA9F2, 0, pb) == os_.fnfErr & 0xFFFFFFFF
    assert g.mem.read(pb + 20, 20) == bytes(20)


def test_a_launch_that_fails(g, capsys):
    """One the display can't start: it says why, and this application goes on."""
    other = g.process.app.folder.add(MacFile("Other", rsrc=build_fork([]), type=b"APPL",
                                             creator=b"OTHR"))
    asked = []

    def launch_application(app, page_option, code0, finder_info):
        asked.append((app, code0))
        return "not enough memory"

    g.tb._d = SimpleNamespace(bridge=SimpleNamespace(call=lambda fn, *args: fn(*args)),
                              launch_application=launch_application)
    g.yielding = contextlib.nullcontext
    pb = g.ptr(bytes(8))
    g.mem.w32(pb, g.ptr(b"\x05Other"))
    assert regs(g, 0xA9F2, 0, pb) == memFullErr & 0xFFFFFFFF
    assert asked == [(other, None)]                         # it has no CODE 0
    assert "couldn't launch Other: not enough memory" in capsys.readouterr().err
    # Nor has one whose CODE 0 can't be read: the launch says why.
    fork = bytearray(build_fork([Resource(b"CODE", 0, bytes(24))]))
    data_offset, = struct.unpack_from(">I", fork, 0)
    struct.pack_into(">I", fork, data_offset, 0x7FFFFFFF)
    other.rsrc = bytes(fork)
    assert regs(g, 0xA9F2, 0, pb) == memFullErr & 0xFFFFFFFF
    assert asked[-1] == (other, None)


def test_finder_information_cut_short(g):
    """A list of documents that says it has more than it has: the ones
    there are kept, and the list goes no further."""
    shared = []
    g.process.files.fs.share_wd = lambda refnum, owner: shared.append(refnum)
    one = struct.pack(">hIhB", -32000, 0, 0, 0)
    os_._keep_directories(struct.pack(">hh", 0, 3) + one + b"\0" + one, g.process)
    assert shared == [-32000, -32000]
