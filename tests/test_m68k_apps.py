"""Real 68k applications (built by tests/apps/build.py from assembly
source) running on the emulated CPU, driven with synthetic input."""

import struct
import threading
import time
import wave
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import machfs
import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QFont, QFontMetrics
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from macrt.m68k import controlpanel, dispatch, glue_threads, runtime
from macrt.m68k.cpu import CPU, PAGE_SIZE
from macrt.m68k.hardware import vBufA
from macrt.m68k.runtime import LEVEL_1_ROUTINE, Runner, SystemError68k
from macrt.macfile import MacFile, MemoryFolder, load_volume, open_application, volume_files
from macrt.memory import RAM
from macrt.resfork import Resource, build_fork, open_fork, resSysHeap
from macrt.toolbox import constants as k
from macrt.toolbox import lowmem
from macrt.toolbox import qdraw
from macrt.toolbox import textedit as te
from macrt.toolbox.events import EventRecord, tick_count
from macrt.toolbox.system import DEFAULT_PARTITION, DEFAULT_STACK, SIZE_STACK
from macrt.toolbox.templates import btnCtrl, chkCtrl, ctrlItem, editText
from macrt.toolbox.types import Point, Rect
from macrt.toolbox.windows import describe_proc
from macrt.ui import chrome, theme

from apps.build import LOCKED_PRELOAD, code_resources
from builders import ditl
from conftest import Recorder

SYSTEM_24 = SimpleNamespace(mmu_32_bit=False, addr_mask=0xFFFFFF)

APPS = Path(__file__).parent / "apps"
CLICKS = -236               # hello.s's click count, below A5
# Tests that measure an application thread's own processor time, from this one.
OWN_TIME = pytest.mark.skipif(not hasattr(time, "pthread_getcpuclockid"),
                              reason="the host can't read another thread's processor time")


def launch_hello(h):
    f = open_application(APPS / "Hello.bin")
    # A Python app alongside, so the display stays up when Hello quits.
    h.launch(Recorder("Other"))
    proc = h.d.launch(f.name, None, app=f)
    h.wait_until(lambda: proc.windows and proc.windows[0].visible, timeout=10,
                 what="Hello's window")
    rec = proc.windows[0]
    h.wait_until(lambda: black(rec, 70, 65), what="Hello's drawing")
    return proc, rec


def black(rec, h, v):
    """Is the window's pixel at (h, v), local, black?"""
    with rec.backing.lock:
        return rec.backing.image.pixel(h, v) & 0xFFFFFF == 0


def clicks(proc):
    return proc.system.mem.r16(proc.partition.a5 + CLICKS)


def test_hello_draws_its_window(harness):
    h = harness()
    proc, rec = launch_hello(h)
    assert rec.title == "Hello"
    # All at once: it frames the rectangle after painting the oval, and
    # erases everything first when it redraws.
    h.wait_until(lambda: (black(rec, 70, 65)                    # inside the painted oval
                          and not black(rec, 31, 41)            # outside it, at a corner
                          and black(rec, 130, 40) and black(rec, 209, 89)   # the frame
                          and not black(rec, 170, 65)),         # and not inside it
                 what="Hello's oval and rectangle")
    assert proc.runner.unimplemented_calls == {}


def test_the_cache_flush_vector_is_code(harness):
    h = harness()
    proc, _ = launch_hello(h)
    mem = proc.system.mem
    flush = mem.r32(lowmem.jCacheFlush)
    assert mem.read(flush, 4) == b"\xA0\xBD\x4E\x75"      # _FlushCodeCache; RTS
    assert dispatch.implemented(0xA0BD)


def test_the_finder_is_what_exit_to_shell_would_launch(harness):
    """FinderName (Inside Macintosh II, Segment Loader), as After Dark
    3.0's INIT reads it to know when the Finder is running."""
    h = harness()
    proc, _ = launch_hello(h)
    assert proc.system.mem.read(lowmem.FinderName, 7) == b"\x06Finder"


def test_the_sound_buffer_is_out_of_the_way(harness):
    """Games write samples to SoundBase directly; it mustn't be low memory."""
    h = harness()
    proc, _ = launch_hello(h)
    system = proc.system
    buffer = system.mem.r32(lowmem.SoundBase)
    assert system.sys_heap.contains(buffer)
    assert system.sys_heap.ptr_size(buffer) >= 370 * 2


def test_the_trap_stubs_have_pages_of_their_own(harness):
    """A page the core has code from is written to by way of Python, and
    nested calls return through the stubs, as each VBL task does: the
    sound buffer beside them made Tetris's music take up each tick."""
    h = harness()
    proc, _ = launch_hello(h)
    start, end = proc.runner.machine.stubs
    pages = range(start // PAGE_SIZE, (end - 1) // PAGE_SIZE + 1)
    assert start % PAGE_SIZE == 0
    buffer = proc.system.mem.r32(lowmem.SoundBase)
    assert buffer // PAGE_SIZE not in pages
    assert (buffer + 370 * 2 - 1) // PAGE_SIZE not in pages
    after = proc.system.sys_heap.new_ptr(16)
    assert after // PAGE_SIZE not in pages


def test_the_system_event_mask_says_what_is_posted(harness):
    """As on a Mac, keyUp isn't posted until a program puts it in SysEvtMask."""
    h = harness()
    proc, _ = launch_hello(h)
    mem = proc.system.mem
    up = EventRecord(k.keyUp, 0x2561, 0, Point(0, 0), 0)
    assert mem.r16(lowmem.SysEvtMask) == k.everyEvent & ~k.event_mask(k.keyUp)
    assert not proc.events.post(up)
    mem.w16(lowmem.SysEvtMask, k.everyEvent)
    assert proc.events.post(up)


def test_parameter_ram_has_the_timing_the_globals_do(harness):
    """Wizardry takes its double-click time from volClik, not DoubleTime.
    The printer port is configured for the Serial Driver, not AppleTalk,
    which programs then don't try to open (JamPaint)."""
    h = harness()
    proc, _ = launch_hello(h)
    mem = proc.system.mem
    kbd_print, vol_clik = mem.r16(lowmem.SysParam + 14), mem.r16(lowmem.SysParam + 16)
    assert mem.r8(lowmem.SysParam) == 0xA8                         # valid
    assert mem.r8(lowmem.SysParam + 3) & 0xF == 2                   # SPConfig: port B useAsync
    assert (vol_clik >> 4 & 0xF) * 4 == mem.r32(lowmem.DoubleTime)
    assert (vol_clik & 0xF) * 4 == mem.r32(lowmem.CaretTime)
    assert (kbd_print >> 12) * 4 == mem.r16(lowmem.KeyThresh)
    assert (kbd_print >> 8 & 0xF) * 2 == mem.r16(lowmem.KeyRepThresh)
    # The speaker's volume, which Dark Castle reads from SdVolume.
    via = proc.runner.machine.via
    assert vol_clik >> 8 & 7 == mem.r8(lowmem.SdVolume) == via.regs[vBufA] & 7 == 7


def test_the_random_seed_in_low_memory_is_not_zero(harness):
    """Leisure Suit Larry seeds QuickDraw's randSeed from RndSeed: from 0,
    every Random is 0, and its age quiz never picks a question."""
    h = harness()
    proc, _ = launch_hello(h)
    assert proc.system.mem.r32(lowmem.RndSeed) != 0


@pytest.mark.parametrize("speed, dbra, chips", [(None, 0x7FFF, 0x7FFF), (2, 1200, 666)])
def test_the_timing_globals_count_a_millisecond_of_the_core(harness, speed, dbra, chips):
    """TimeDBRA, TimeSCCDB and TimeSCSIDB: as many times round a DBRA, and
    a TST.B (A0) and a DBRA, as the cycle model lets --speed run them in a
    millisecond; unpaced, the most a signed word holds.  Sesame Street
    Letters draws every other row while TimeDBRA is 5004 or less."""
    h = harness(speed=speed)
    proc, _ = launch_hello(h)
    mem = proc.system.mem
    assert mem.r16(lowmem.TimeDBRA) == dbra
    assert mem.r16(lowmem.TimeSCCDB) == mem.r16(lowmem.TimeSCSIDB) == chips


def test_hello_counts_clicks_in_its_window(harness):
    h = harness()
    proc, rec = launch_hello(h)
    c = rec.content
    h.click(c.left + 200, c.top + 140)
    h.wait_until(lambda: clicks(proc) == 1, what="a click counted")
    h.click(c.left + 200, c.top + 140)
    h.wait_until(lambda: clicks(proc) == 2, what="another")


@pytest.mark.parametrize("how", ["menu", "command key", "close box"])
def test_hello_quits(harness, how):
    h = harness()
    proc, rec = launch_hello(h)
    if how == "menu":
        h.choose(h.d.default_bar(proc), "File", 1)
    elif how == "command key":
        h.key(Qt.Key.Key_Q, Qt.KeyboardModifier.ControlModifier)
    else:
        h.click(*h.box_point(rec, k.wInGoAway))
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Hello to quit")
    assert rec not in h.d.views


def test_the_clock_stops_once_the_display_is_done_with(harness):
    h = harness()
    proc, _ = launch_hello(h)
    cpu = proc.runner.machine.cpu
    h.d.shutdown(quit_app=False)
    h.d.join(5)
    assert cpu._closed.is_set()


def test_a_crash_stops_only_that_app(harness):
    """An illegal instruction is a system error, ID 3: the app goes, the
    user is told, and everything else carries on."""
    h = harness()
    other = h.launch(Recorder("Other"))
    crash = MacFile("Crash", rsrc=build_fork(code_resources(bytes.fromhex("4AFC"), 0)),
                    type=b"APPL", creator=b"CRSH")
    proc = h.d.launch(crash.name, None, app=crash)
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Crash to stop")
    assert h.d.system_errors[-1][:2] == ("Crash", 3)
    h.wait_until(lambda: any(a.isVisible() for a in h.d._alerts), what="the alert")
    assert other in h.d.processes


def test_sys_error_is_a_bomb_but_for_the_alerts_with_no_buttons(harness):
    """A startup alert (a negative ID) and the disk-switch alert (30) come
    back; zero divide (4) doesn't, as Downhill Racer's handler expects,
    jumping to the ROM's restart after it."""
    h = harness()
    code = bytes.fromhex("70FF A9C9"        # moveq #-1,d0; _SysError
                         "701E A9C9"        # moveq #30,d0; _SysError
                         "7004 A9C9"        # moveq #4,d0; _SysError
                         "60FE")            # bra.s *
    bomb = MacFile("Bomb", rsrc=build_fork(code_resources(code, 0)), type=b"APPL", creator=b"BOMB")
    proc = h.d.launch(bomb.name, None, app=bomb)
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Bomb to stop")
    assert h.d.system_errors[-1][:2] == ("Bomb", 4)
    assert proc.system.mem.r16(lowmem.DSErrCode) == 4


@pytest.mark.parametrize("model", ["plus", "ii"])
def test_a_plus_ignores_bit_9_of_a_toolbox_trap_word_that_names_nothing(harness, model):
    """The 128K ROM's dispatcher took a Toolbox trap number from bits 0-8:
    Crozzle 0.8 calls NewWindow as $AB13.  A Mac II's table has more
    entries, and the word is an unimplemented trap there."""
    h = harness(model=model)
    code = bytes.fromhex("42A7"         # CLR.L -(SP)
                         "AB75"         # _TickCount, with bit 9 set
                         "2B5F FFFC"    # MOVE.L (SP)+,-4(A5)
                         "A9F4")        # _ExitToShell
    app = MacFile("Ticks", rsrc=build_fork(code_resources(code, 4)), type=b"APPL",
                  creator=b"TICK")
    proc = h.d.launch(app.name, None, app=app)
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Ticks to finish")
    if model == "plus":
        assert not h.d.system_errors
        assert proc.system.mem.r32(proc.partition.a5 - 4) > 0
    else:
        assert h.d.system_errors[-1][:2] == ("Ticks", 12)


def test_the_alert_outlives_the_last_app(harness):
    """With nothing else running, the display stays up until the user has
    read the alert, then closes."""
    h = harness()
    crash = MacFile("Crash", rsrc=build_fork(code_resources(bytes.fromhex("4AFC"), 0)),
                    type=b"APPL", creator=b"CRSH")
    proc = h.d.launch(crash.name, None, app=crash)
    h.wait_until(lambda: proc not in h.d.processes and h.d._alerts, timeout=5,
                 what="Crash to stop")
    assert not h.d.shutting_down
    h.d._alerts[0].accept()
    h.wait_until(lambda: h.d.shutting_down, what="the display to close")


def test_an_app_launches_another_in_its_place(harness):
    """Launcher answers a Gestalt selector of its own (NewGestalt), then
    Launches Hello, which is in the same folder, and quits for it."""
    h = harness()
    folder = MemoryFolder([open_application(APPS / "Launcher.bin"),
                           open_application(APPS / "Hello.bin")])
    launcher = folder.find("Launcher")
    proc = h.d.launch(launcher.name, None, app=launcher)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Launcher to quit")
    assert (mem.r16s(a5 - 2), mem.r16s(a5 - 4), mem.r32(a5 - 8)) == (0, 0, 42)
    hello = next(p for p in h.d.processes if p.name == "Hello")
    h.wait_until(lambda: hello.windows and hello.windows[0].visible, what="Hello's window")


def test_a_launcher_quits_even_if_what_it_launched_keeps_the_processor(harness):
    """An application that never calls WaitNextEvent keeps the processor,
    as it would under MultiFinder; the launcher that started it goes
    anyway, without waiting for its turn."""
    h = harness()
    spin = MacFile("Hello", rsrc=build_fork(code_resources(bytes.fromhex("60FE"), 0)),
                   type=b"APPL", creator=b"SPIN")                   # BRA.S *
    folder = MemoryFolder([open_application(APPS / "Launcher.bin"), spin])
    launcher = folder.find("Launcher")
    proc = h.d.launch(launcher.name, None, app=launcher)
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Launcher to quit")
    assert [p.name for p in h.d.processes] == ["Hello"]


def test_an_app_chains_to_another(harness):
    """Skyfox's first application Chains to the game: the other starts in
    its place, as with Launch."""
    h = harness()
    chainer = MacFile("Chainer", rsrc=build_fork(code_resources(bytes.fromhex(
        "4267"                  # CLR.W -(SP): the configuration
        "487A 0006"             # PEA name(PC)
        "204F"                  # MOVEA.L SP,A0
        "A9F3"                  # _Chain
        "0548656C6C6F"), 0)),   # name: "Hello"
        type=b"APPL", creator=b"CHAN")
    folder = MemoryFolder([chainer, open_application(APPS / "Hello.bin")])
    chainer = folder.find("Chainer")
    proc = h.d.launch(chainer.name, None, app=chainer)
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Chainer to go")
    hello = next(p for p in h.d.processes if p.name == "Hello")
    h.wait_until(lambda: hello.windows and hello.windows[0].visible, what="Hello's window")


def test_launch_passes_on_the_finder_information(harness):
    """As the Finder does, starting an application with Launch: what the
    launcher leaves in AppParmHandle is the new application's Finder
    information (Cipher names itself there and launches PShell, its
    interpreter, to run it).  The working directory it names the
    document by stays open after the launcher quits."""
    launcher = machfs.File()
    launcher.type, launcher.creator = b"APPL", b"LNCH"
    launcher.rsrc = build_fork(code_resources(bytes.fromhex(
        "2078 0AEC 702C A024"   # MOVEA.L AppParmHandle,A0; MOVEQ #44,D0; _SetHandleSize
        "2078 0AEC 2050"        # MOVEA.L AppParmHandle,A0; MOVEA.L (A0),A0
        "20BC 00000001"         # MOVE.L #1,(A0): open, one document
        "3178 0384 0004"        # MOVE.W DefVRefNum,4(A0)
        "217C 54455854 0006"    # MOVE.L #'TEXT',6(A0)
        "4268 000A"             # CLR.W 10(A0): its version
        "217C 03446F63 000C"    # MOVE.L #$03446F63,12(A0): "Doc"
        "4267 487A 0006 204F"   # CLR.W -(SP); PEA name(PC); MOVEA.L SP,A0
        "A9F2"                  # _Launch
        "09 4469 736B 3A53 7069 6E"), 0))  # name: "Disk:Spin"
    spin = machfs.File()
    spin.type, spin.creator = b"APPL", b"SPIN"
    spin.rsrc = build_fork(code_resources(bytes.fromhex("60FE"), 0))     # BRA.S *
    v = machfs.Volume()
    v.name = "Disk"
    v["Spin"] = spin
    v["Tools"] = machfs.Folder()
    v["Tools"]["Launcher"] = launcher
    vol = load_volume(v.write(800 * 1024, desktopdb=False, bootable=False))
    h = harness(disks=[vol])
    app = next(a for a in volume_files(vol) if a.name == "Launcher")
    proc = h.d.launch(app.name, None, app=app)
    wd = proc.files.default[0]                      # the Tools folder's
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Launcher to quit")
    new = next(p for p in h.d.processes if p.name == "Spin")
    expected = struct.pack(">hhh4sBx", 0, 1, wd, b"TEXT", 0) + b"\x03Doc"

    def info():
        hd = new.world.r32(lowmem.AppParmHandle)
        return new.system.mem.read(new.heap.deref(hd), 16) if hd else b""
    h.wait_until(lambda: info() == expected, what="Spin's Finder information")
    fs = new.files.fs
    assert wd < 0 and new.files in fs.wds[wd].owners
    assert fs.wds[wd].dirid == fs.volume_named("Disk").root.child("Tools").cnid


def test_launch_with_an_fsspec_and_launch_continue(harness):
    """System 7's LaunchApplication: the parameter block says 'LC' and
    has an FSSpec, here FSMakeFSSpec's.  With launchContinue the
    launcher goes on, and the block has the new process's serial number
    and its SIZE's partition sizes."""
    h = harness()
    launcher = MacFile("Launcher", rsrc=build_fork(code_resources(bytes.fromhex(
        "4267 4267 42A7"        # CLR.W -(SP) twice, CLR.L -(SP): result, vRefNum, dirID
        "487A 0036"             # PEA name(PC)
        "486D FF74"             # PEA -140(A5): the FSSpec
        "7001 AA52"             # MOVEQ #1,D0; _HighLevelFSDispatch: FSMakeFSSpec
        "3B5F FFFE"             # MOVE.W (SP)+,-2(A5)
        "41ED FFC4"             # LEA -60(A5),A0: the LaunchParamBlockRec
        "317C 4C43 0006"        # MOVE.W #'LC',6(A0): launchBlockID
        "217C 00000020 0008"    # MOVE.L #32,8(A0): launchEPBLength
        "317C 4800 000E"        # MOVE.W #$4800,14(A0): launchContinue, launchNoFileFlags
        "43ED FF74 2149 0010"   # LEA -140(A5),A1; MOVE.L A1,16(A0): launchAppSpec
        "A9F2"                  # _Launch
        "3B40 FFFC"             # MOVE.W D0,-4(A5)
        "A9F4"                  # _ExitToShell
        "0548656C6C6F"          # name: "Hello"
    ), 160)), type=b"APPL", creator=b"LNCH")
    folder = MemoryFolder([launcher, open_application(APPS / "Hello.bin")])
    launcher = folder.find("Launcher")
    proc = h.d.launch(launcher.name, None, app=launcher)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Launcher to go")
    assert (mem.r16s(a5 - 2), mem.r16s(a5 - 4)) == (0, 0)
    hello = next(p for p in h.d.processes if p.name == "Hello")
    pb = a5 - 60
    assert (mem.r32(pb + 20), mem.r32(pb + 24)) == (0, hello.serial_number)
    assert (mem.r32(pb + 28), mem.r32(pb + 32)) == (262144, 131072)
    assert mem.r32(pb + 36) > 0                                 # launchAvailableSize
    h.wait_until(lambda: hello.windows and hello.windows[0].visible, what="Hello's window")


def test_unloadseg_by_a_jump_table_entry(harness):
    """UnloadSeg given the address Pascal's @ gives for a routine in
    another segment, its jump table entry's JMP (Microsoft BASIC 2 unloads
    its segments so): the entry goes back to _LoadSeg, and loads again."""
    h = harness()
    h.launch(Recorder("Other"))
    entry = lambda seg: bytes.fromhex(f"00003F3C{seg:04X}A9F0")     # noqa: E731
    table = entry(1) + entry(2)
    code0 = bytes.fromhex(f"{32 + len(table):08X}00000004{len(table):08X}00000020") + table
    main = bytes.fromhex("0000 0001"            # first entry 0, one entry
                         "4EAD 002A"            # JSR 42(A5): entry 1, in CODE 2
                         "486D 002A A9F1"       # PEA 42(A5); _UnloadSeg
                         "3B6D 002A FFFE"       # MOVE.W 42(A5),-2(A5)
                         "4EAD 002A"            # JSR 42(A5) again
                         "3B6D 002A FFFC"       # MOVE.W 42(A5),-4(A5)
                         "A9F4")                # _ExitToShell
    sub = bytes.fromhex("0008 0001 4E75")       # first entry 8, one entry; RTS
    app = MacFile("Segs", rsrc=build_fork([
        Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),
        Resource(b"CODE", 1, main, b"Main", 0x20),
        Resource(b"CODE", 2, sub, b"Sub", 0x20)]), type=b"APPL", creator=b"SEGS")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Segs to quit")
    assert not h.d.system_errors
    assert mem.r16(a5 - 2) == 0x3F3C            # unloaded: MOVE.W #2,-(SP); _LoadSeg
    assert mem.r16(a5 - 4) == 0x4EF9            # loaded again: JMP


def test_a_segment_unloaded_and_loaded_again_is_locked_again(harness):
    """Habadex unloads all its segments, the one it's running in too, and
    loads that one again at once: LoadSeg locks it again, though it's still
    in memory, or the next CompactMem moves the code that's running."""
    h = harness()
    h.launch(Recorder("Other"))
    entry = lambda seg: bytes.fromhex(f"00003F3C{seg:04X}A9F0")     # noqa: E731
    table = entry(1) + entry(2)
    code0 = bytes.fromhex(f"{32 + len(table):08X}00000004{len(table):08X}00000020") + table
    main = bytes.fromhex("0000 0001"            # first entry 0, one entry
                         "4EAD 002A"            # JSR 42(A5): entry 1, in CODE 2
                         "486D 002A A9F1"       # PEA 42(A5); _UnloadSeg
                         "4EAD 002A"            # JSR 42(A5) again
                         "42A7 2F3C 434F4445"   # CLR.L -(SP); MOVE.L #'CODE',-(SP)
                         "3F3C 0002 A9A0"       # MOVE.W #2,-(SP); _GetResource
                         "205F A069"            # MOVEA.L (SP)+,A0; _HGetState
                         "1B40 FFFE"            # MOVE.B D0,-2(A5)
                         "A9F4")                # _ExitToShell
    sub = bytes.fromhex("0008 0001 4E75")       # first entry 8, one entry; RTS
    app = MacFile("Segs", rsrc=build_fork([
        Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),
        Resource(b"CODE", 1, main, b"Main", 0x20),
        Resource(b"CODE", 2, sub, b"Sub", 0x20)]), type=b"APPL", creator=b"SEGS")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Segs to quit")
    assert not h.d.system_errors
    state = mem.r8(a5 - 2)
    assert state & 0x80                         # locked
    assert not state & 0x40                     # and not purgeable


def test_load_seg_moves_an_unlocked_segment_high(harness):
    """A segment without the resLocked attribute LoadSeg moves high with
    MoveHHi before it locks it, out of the way of the blocks allocated low
    (Inside Macintosh IV, The Segment Loader): HyperCard 2.0 grows a
    pointer it has just made past where its next segment was read in."""
    h = harness()
    h.launch(Recorder("Other"))
    entry = lambda seg: bytes.fromhex(f"00003F3C{seg:04X}A9F0")     # noqa: E731
    table = entry(1) + entry(2)
    code0 = bytes.fromhex(f"{32 + len(table):08X}00000008{len(table):08X}00000020") + table
    main = bytes.fromhex("0000 0001"            # first entry 0, one entry
                         "4EAD 002A"            # JSR 42(A5): entry 1, in CODE 2
                         "42A7 2F3C 434F4445"   # CLR.L -(SP); MOVE.L #'CODE',-(SP)
                         "3F3C 0002 A9A0"       # MOVE.W #2,-(SP); _GetResource
                         "205F 2B50 FFFC"       # MOVEA.L (SP)+,A0; MOVE.L (A0),-4(A5)
                         "7010 A11E"            # MOVEQ #16,D0; _NewPtr
                         "2B48 FFF8"            # MOVE.L A0,-8(A5)
                         "A9F4")                # _ExitToShell
    sub = bytes.fromhex("0008 0001 4E75")       # first entry 8, one entry; RTS
    app = MacFile("Segs", rsrc=build_fork([
        Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),
        Resource(b"CODE", 1, main, b"Main", LOCKED_PRELOAD),
        Resource(b"CODE", 2, sub, b"Sub", 0x20)]), type=b"APPL", creator=b"SEGS")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Segs to quit")
    assert not h.d.system_errors
    master, ptr = mem.r32(a5 - 4), mem.r32(a5 - 8)
    assert master >> 24 & 0x80                  # locked
    assert master & 0xFFFFFF > ptr              # and above a pointer made after it


def test_a_jump_table_entry_called_with_flags_in_its_high_byte(harness):
    """A 68000 ignores an address's high byte: Macintosh Pascal calls jump
    table entries through addresses with a master pointer's flags in it,
    and _LoadSeg must still find the entry it came from."""
    h = harness()
    h.launch(Recorder("Other"))
    entry = lambda seg: bytes.fromhex(f"00003F3C{seg:04X}A9F0")     # noqa: E731
    table = entry(1) + entry(2)
    code0 = bytes.fromhex(f"{32 + len(table):08X}00000010{len(table):08X}00000020") + table
    main = bytes.fromhex("0000 0001"            # first entry 0, one entry
                         "41ED 002A"            # LEA 42(A5),A0: entry 2, in CODE 2
                         "2008 08C0 001F 2040"  # with bit 31 set
                         "4E90"                 # JSR (A0)
                         "3B40 FFF0"            # MOVE.W D0,-16(A5)
                         "A9F4")                # _ExitToShell
    sub = bytes.fromhex("0008 0001 7007 4E75")  # first entry 8, one entry; MOVEQ #7,D0; RTS
    app = MacFile("Segs", rsrc=build_fork([
        Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),
        Resource(b"CODE", 1, main, b"Main", 0x20),
        Resource(b"CODE", 2, sub, b"Sub", 0x20)]), type=b"APPL", creator=b"SEGS")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Segs to quit")
    assert not h.d.system_errors
    assert mem.r16s(a5 - 16) == 7                   # CODE 2 ran


def test_segment_ffff_is_code_minus_one(harness):
    """A segment number is an INTEGER: GraphicConverter's first entry
    loads segment $FFFF, its CODE -1."""
    h = harness()
    h.launch(Recorder("Other"))
    entry = lambda seg: bytes.fromhex(f"00003F3C{seg:04X}A9F0")     # noqa: E731
    table = entry(0xFFFF) + entry(1)
    code0 = bytes.fromhex(f"{32 + len(table):08X}00000010{len(table):08X}00000020") + table
    first = bytes.fromhex("0000 0001"           # first entry 0, one entry
                          "7007 3B40 FFF0"      # MOVEQ #7,D0; MOVE.W D0,-16(A5)
                          "A9F4")               # _ExitToShell
    main = bytes.fromhex("0008 0001 4E75")      # first entry 8, one entry; RTS
    app = MacFile("Segs", rsrc=build_fork([
        Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),
        Resource(b"CODE", -1, first, b"", 0x20),
        Resource(b"CODE", 1, main, b"Main", 0x20)]), type=b"APPL", creator=b"SEGS")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Segs to quit")
    assert not h.d.system_errors
    assert mem.r16s(a5 - 16) == 7                   # CODE -1 ran


def test_a_segment_is_read_in_while_resload_is_off(harness):
    """LoadSeg reads in a segment that isn't in memory whatever ResLoad
    is: SoundApp calls a routine in another segment between its
    SetResLoad(FALSE) and SetResLoad(TRUE)."""
    h = harness()
    h.launch(Recorder("Other"))
    entry = lambda seg: bytes.fromhex(f"00003F3C{seg:04X}A9F0")     # noqa: E731
    table = entry(1) + entry(2)
    code0 = bytes.fromhex(f"{32 + len(table):08X}00000010{len(table):08X}00000020") + table
    main = bytes.fromhex("0000 0001"            # first entry 0, one entry
                         "4267 A99B"            # CLR.W -(SP); _SetResLoad: FALSE
                         "4EAD 002A"            # JSR 42(A5): entry 1, in CODE 2
                         "3B40 FFF0"            # MOVE.W D0,-16(A5)
                         "1F3C 0001 A99B"       # MOVE.B #1,-(SP); _SetResLoad: TRUE
                         "A9F4")                # _ExitToShell
    sub = bytes.fromhex("0008 0001 7007 4E75")  # first entry 8, one entry; MOVEQ #7,D0; RTS
    app = MacFile("Segs", rsrc=build_fork([
        Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),
        Resource(b"CODE", 1, main, b"Main", 0x20),
        Resource(b"CODE", 2, sub, b"Sub", 0x20)]), type=b"APPL", creator=b"SEGS")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Segs to quit")
    assert not h.d.system_errors
    assert mem.r16s(a5 - 16) == 7                   # CODE 2 ran


def test_a_trap_through_the_roms_dispatcher(harness):
    """Lotus Jazz finds the ROM's A-line dispatcher by the code that puts
    it in vector $28, puts it back there, and a trap's exception frame
    stacked and jumped to there does the trap."""
    h = harness()
    h.launch(Recorder("Other"))
    code0 = bytes.fromhex("00000028 00000010 00000008 00000020"
                          "0000 3F3C 0001 A9F0")
    main = bytes.fromhex("0000 0001"            # first entry 0, one entry
                         "2278 02AE"            # MOVEA.L ROMBase,A1
                         "203C 21C8 0028"       # MOVE.L #$21C80028,D0
                         "72FF"                 # MOVEQ #-1,D1
                         "B091 6706"            # find: CMP.L (A1),D0; BEQ.S found
                         "5449 51C9 FFF8"       # ADDQ.W #2,A1; DBRA D1,find
                         "3021 5D49"            # found: MOVE.W -(A1),D0; SUBQ.W #6,A1
                         "3011 D2C0"            # MOVE.W (A1),D0; ADDA.W D0,A1: a 68000's
                         "2B49 FFF4"            # MOVE.L A1,-12(A5)
                         "42A7"                 # CLR.L -(A7)
                         "2F3C 0000 F0F0"       # MOVE.L #$F0F0,-(A7)
                         "2F3C 0000 FF00"       # MOVE.L #$FF00,-(A7)
                         "487A 0006"            # PEA trap(PC)
                         "40E7 4ED1"            # MOVE.W SR,-(A7); JMP (A1)
                         "A858"                 # trap: _BitAnd
                         "2B5F FFF0"            # MOVE.L (A7)+,-16(A5)
                         "A9F4")                # _ExitToShell
    app = MacFile("Jazzy", rsrc=build_fork([
        Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),
        Resource(b"CODE", 1, main, b"Main", 0x20)]), type=b"APPL", creator=b"JAZY")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Jazzy to quit")
    assert not h.d.system_errors
    assert mem.r32(a5 - 12) == mem.r32(0x28)
    assert mem.r32(a5 - 16) == 0xF000


@pytest.mark.parametrize("model", ["plus", "ii", "iix"])
def test_loadtrap_and_an_a_line_handler_of_the_programs_own(harness, model):
    """A program's own handler in vector $28 sees every A-line trap, and
    goes on to the ROM's dispatcher.  With LoadTrap set, _LoadSeg calls
    _Debugger with the routine it loaded on the stack, as Crystal Paint
    DEMO's handler expects, and goes on to it."""
    h = harness(model=model)
    h.launch(Recorder("Other"))
    entry = lambda seg: bytes.fromhex(f"00003F3C{seg:04X}A9F0")     # noqa: E731
    table = entry(1) + entry(2)
    code0 = bytes.fromhex(f"{32 + len(table):08X}00000010{len(table):08X}00000020") + table
    main = bytes.fromhex("0000 0001"            # first entry 0, one entry
                         "2B78 0028 FFF8"       # MOVE.L $28.W,-8(A5): the old vector
                         "41FA 001A 21C8 0028"  # LEA handler(PC),A0; MOVE.L A0,$28.W
                         "50F8 012D"            # ST LoadTrap
                         "4EAD 002A"            # JSR 42(A5): entry 2, in CODE 2
                         "3B40 FFF0"            # MOVE.W D0,-16(A5)
                         "21ED FFF8 0028"       # MOVE.L -8(A5),$28.W
                         "A9F4"                 # _ExitToShell
                         "2F08"                 # handler: MOVE.L A0,-(SP)
                         "206F 0006"            # MOVEA.L 6(SP),A0: where the trap is
                         "526D FFFE"            # ADDQ.W #1,-2(A5)
                         "0C50 A9FF 6614"       # CMPI.W #_Debugger,(A0); BNE.S chain
                         "4238 012D"            # CLR.B LoadTrap
                         "41EF 000A"            # LEA 10(SP),A0: above a 68000's frame
                         "4A38 012F 6702"       # TST.B CPUFlag; BEQ.S 1f
                         "5448"                 # ADDQ.W #2,A0: above a 68020's
                         "2B50 FFF4"            # 1: MOVE.L (A0),-12(A5)
                         "205F"                 # chain: MOVEA.L (SP)+,A0
                         "2F2D FFF8 4E75")      # MOVE.L -8(A5),-(SP); RTS
    sub = bytes.fromhex("0008 0001 7007 4E75")  # first entry 8, one entry; MOVEQ #7,D0; RTS
    app = MacFile("Segs", rsrc=build_fork([
        Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),
        Resource(b"CODE", 1, main, b"Main", 0x20),
        Resource(b"CODE", 2, sub, b"Sub", 0x20)]), type=b"APPL", creator=b"SEGS")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Segs to quit")
    assert not h.d.system_errors
    assert mem.r16s(a5 - 16) == 7                   # CODE 2 ran
    assert mem.r16(a5 - 2) == 2                     # _LoadSeg, then _Debugger
    assert mem.r32(a5 - 12) == mem.r32(a5 + 44)     # the routine, as the entry has it
    assert mem.r8(lowmem.LoadTrap) == 0


def test_quickdraw_goes_back_to_the_a5_before_an_init_graf_on_a_frame(harness):
    """DD Expand finds the screen's bounds by calling InitGraf with A5 on a
    stack frame, then puts A5 back: QuickDraw's globals are at 0(A5) again,
    and OpenPort makes its port thePort there, not in the frame."""
    h = harness()
    h.launch(Recorder("Other"))
    code0 = bytes.fromhex("00000028 00000200 00000008 00000020"
                          "0000 3F3C 0001 A9F0")
    main = bytes.fromhex("0000 0001"            # first entry 0, one entry
                         "486D FFFC A86E"       # PEA -4(A5); _InitGraf
                         "4E56 FF00 2F0D"       # LINK A6,#-256; MOVE.L A5,-(SP)
                         "4BEE FFE0"            # LEA -32(A6),A5: globals in the frame
                         "486D FFFC A86E"       # PEA -4(A5); _InitGraf
                         "2A5F 4E5E"            # MOVEA.L (SP)+,A5; UNLK A6
                         "203C 0000 006C A31E"  # MOVE.L #108,D0; _NewPtr,Clear
                         "2B48 FE10"            # MOVE.L A0,-496(A5)
                         "2F08 A86F"            # MOVE.L A0,-(SP); _OpenPort
                         "A9F4")                # _ExitToShell
    app = MacFile("Expand", rsrc=build_fork([
        Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),
        Resource(b"CODE", 1, main, b"Main", 0x20)]), type=b"APPL", creator=b"XPND")
    proc = h.d.launch(app.name, None, app=app)
    mem = proc.system.mem
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Expand to quit")
    assert not h.d.system_errors
    a5 = proc.world.r32(lowmem.CurrentA5)
    assert proc.partition.a5 == a5
    port = mem.r32(a5 - 496)
    assert port and mem.r32(mem.r32(a5)) == port


def test_a_segment_is_read_through_a_patch_on_getresource(harness):
    """FullPaint and Q-Sheet patch _GetResource to decrypt their segments
    as the Segment Loader reads them: the patch is called for CODE 2, and
    goes on to the original."""
    h = harness()
    h.launch(Recorder("Other"))
    entry = lambda seg: bytes.fromhex(f"00003F3C{seg:04X}A9F0")     # noqa: E731
    table = entry(1) + entry(2)
    code0 = bytes.fromhex(f"{32 + len(table):08X}00000010{len(table):08X}00000020") + table
    main = bytes.fromhex("0000 0001"            # first entry 0, one entry
                         "303C 01A0 A146"       # MOVE.W #$1A0,D0; _GetTrapAddress
                         "2B48 FFF4"            # MOVE.L A0,-12(A5): GetResource
                         "41FA 0012"            # LEA patch(PC),A0
                         "303C 01A0 A047"       # MOVE.W #$1A0,D0; _SetTrapAddress
                         "4EAD 002A"            # JSR 42(A5): entry 2, in CODE 2
                         "3B40 FFF0"            # MOVE.W D0,-16(A5)
                         "A9F4"                 # _ExitToShell
                         # patch:
                         "526D FFFE"            # ADDQ.W #1,-2(A5)
                         "2B6F 0006 FFFA"       # MOVE.L 6(SP),-6(A5): the type
                         "3B6F 0004 FFF8"       # MOVE.W 4(SP),-8(A5): the ID
                         "2F2D FFF4 4E75")      # MOVE.L -12(A5),-(SP); RTS: to GetResource
    sub = bytes.fromhex("0008 0001 7007 4E75")  # first entry 8, one entry; MOVEQ #7,D0; RTS
    app = MacFile("Segs", rsrc=build_fork([
        Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),
        Resource(b"CODE", 1, main, b"Main", 0x20),
        Resource(b"CODE", 2, sub, b"Sub", 0x20)]), type=b"APPL", creator=b"SEGS")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Segs to quit")
    assert not h.d.system_errors
    assert (mem.r16s(a5 - 2), mem.read(a5 - 6, 4), mem.r16s(a5 - 8)) == (1, b"CODE", 2)
    assert mem.r16s(a5 - 16) == 7                   # CODE 2 ran


def test_waitnextevent_gets_its_event_through_a_patch_on_getnextevent(harness):
    """HyperCard 2 keeps a click it sees while a script runs, and its patch
    on _GetNextEvent hands it to the main loop's _WaitNextEvent, which
    gets its events through that trap.  Here the patch answers itself."""
    h = harness()
    h.launch(Recorder("Other"))
    table = bytes.fromhex("00003F3C0001A9F0")
    code0 = bytes.fromhex(f"{32 + len(table):08X}00000040{len(table):08X}00000020") + table
    main = bytes.fromhex("0000 0001"            # first entry 0, one entry
                         "303C 0170 A146"       # MOVE.W #$170,D0; _GetTrapAddress
                         "2B48 FFF4"            # MOVE.L A0,-12(A5): GetNextEvent
                         "41FA 001E"            # LEA patch(PC),A0
                         "303C 0170 A047"       # MOVE.W #$170,D0; _SetTrapAddress
                         "4267 3F3C FFFF"       # CLR.W -(SP); MOVE.W #everyEvent,-(SP)
                         "486D FFD0"            # PEA -48(A5): the event
                         "42A7 42A7 A860"       # sleep 0, no mouse region: _WaitNextEvent
                         "1B5F FFEE"            # MOVE.B (SP)+,-18(A5)
                         "A9F4"                 # _ExitToShell
                         # patch:
                         "526D FFFE"            # ADDQ.W #1,-2(A5)
                         "206F 0004"            # MOVEA.L 4(SP),A0: the event
                         "30BC 000F"            # MOVE.W #osEvt,(A0)
                         "1F7C 0001 000A"       # MOVE.B #1,10(SP): true
                         "205F 5C4F 4ED0")      # MOVEA.L (SP)+,A0; ADDQ.W #6,SP; JMP (A0)
    app = MacFile("Patched", rsrc=build_fork([
        Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),
        Resource(b"CODE", 1, main, b"Main", 0x20)]), type=b"APPL", creator=b"PGNE")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Patched to quit")
    assert not h.d.system_errors
    assert mem.r16(a5 - 2) == 1                     # the patch was asked once
    assert mem.r8(a5 - 18) == 1                     # and its answer returned
    assert mem.r16(a5 - 48) == k.osEvt


def test_loadseg_from_an_a5_world_of_its_own(harness):
    """Cosmic Osmo's XCMD runs MacroMind Player's segments with A5 pointing
    at a world it made for them: _LoadSeg fills in that world's jump table,
    CurJTOffset above the A5 register, and leaves the application's be."""
    h = harness()
    h.launch(Recorder("Other"))
    entry = lambda seg: bytes.fromhex(f"00003F3C{seg:04X}A9F0")     # noqa: E731
    table = entry(1) + entry(2)
    code0 = bytes.fromhex(f"{32 + len(table):08X}00000200{len(table):08X}00000020") + table
    main = bytes.fromhex("0000 0001"            # first entry 0, one entry
                         "41ED FF28"            # LEA -$D8(A5),A0: the other world's entry 2
                         "20BC 0000 3F3C"       # MOVE.L #$00003F3C,(A0)
                         "217C 0002 A9F0 0004"  # MOVE.L #$0002A9F0,4(A0)
                         "2F0D 4BED FF00"       # MOVE.L A5,-(SP); LEA -$100(A5),A5
                         "4EA8 0002 2A5F"       # JSR 2(A0); MOVEA.L (SP)+,A5
                         "3B40 FFF0"            # MOVE.W D0,-16(A5)
                         "3B6D FF2A FFEE"       # MOVE.W -$D6(A5),-18(A5): its entry now
                         "3B6D 002A FFEC"       # MOVE.W 42(A5),-20(A5): ours, for CODE 2
                         "A9F4")                # _ExitToShell
    sub = bytes.fromhex("0008 0001 7007 4E75")  # first entry 8, one entry; MOVEQ #7,D0; RTS
    app = MacFile("Segs", rsrc=build_fork([
        Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),
        Resource(b"CODE", 1, main, b"Main", 0x20),
        Resource(b"CODE", 2, sub, b"Sub", 0x20)]), type=b"APPL", creator=b"SEGS")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Segs to quit")
    assert not h.d.system_errors
    assert mem.r16s(a5 - 16) == 7                   # CODE 2 ran
    assert mem.r16(a5 - 18) == 0x4EF9               # the other world's entry: JMP
    assert mem.r16(a5 - 20) == 0x3F3C               # ours still unloaded


def test_loadseg_goes_back_six_bytes_before_its_return_address(harness):
    """_LoadSeg goes on 6 bytes before the address it returns to, which
    for a jump table entry is the entry's MOVE.W, now a JMP. Easy View's
    patch on it calls the original with an address 6 past where it goes on."""
    h = harness()
    h.launch(Recorder("Other"))
    entry = lambda seg: bytes.fromhex(f"00003F3C{seg:04X}A9F0")     # noqa: E731
    table = entry(1) + entry(2)
    code0 = bytes.fromhex(f"{32 + len(table):08X}00000200{len(table):08X}00000020") + table
    main = bytes.fromhex("0000 0001"            # first entry 0, one entry
                         "303C A9F0 A746"       # MOVE.W #_LoadSeg,D0; _GetToolTrapAddress
                         "3F3C 0002"            # MOVE.W #2,-(SP)
                         "487A 000A 4ED0"       # PEA 6 past the JSR; JMP (A0)
                         "4EAD 002A 4E71"       # JSR 42(A5): entry 2, in CODE 2; NOP
                         "3B40 FFF0"            # MOVE.W D0,-16(A5)
                         "A9F4")                # _ExitToShell
    sub = bytes.fromhex("0008 0001 7007 4E75")  # first entry 8, one entry; MOVEQ #7,D0; RTS
    app = MacFile("Segs", rsrc=build_fork([
        Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),
        Resource(b"CODE", 1, main, b"Main", 0x20),
        Resource(b"CODE", 2, sub, b"Sub", 0x20)]), type=b"APPL", creator=b"SEGS")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Segs to quit")
    assert not h.d.system_errors
    assert mem.r16s(a5 - 16) == 7                   # the JSR ran, and CODE 2
    assert mem.r16(a5 + 42) == 0x4EF9               # its entry loaded


def test_fmswapfont_by_way_of_jswapfont(harness):
    """HyperCard calls FMSwapFont through the JSwapFont vector, with JSR
    and the trap's stack: room for the result, then the input record."""
    h = harness()
    h.launch(Recorder("Other"))
    code0 = bytes.fromhex("00000028 00000080 00000008 00000020"
                          "0000 3F3C 0001 A9F0")
    main = bytes.fromhex("0000 0001"            # first entry 0, one entry
                         "41ED FFC0"            # LEA -$40(A5),A0: an FMInput
                         "30BC 0003"            # MOVE.W #3,(A0): Geneva
                         "317C 000C 0002"       # MOVE.W #12,2(A0)
                         "217C 0001 0001 0008"  # MOVE.L #$00010001,8(A0): numer
                         "217C 0001 0001 000C"  # MOVE.L #$00010001,12(A0): denom
                         "2B4F FFF4"            # MOVE.L A7,-12(A5)
                         "42A7 4850"            # CLR.L -(SP); PEA (A0)
                         "2078 08E0 4E90"       # MOVEA.L JSwapFont,A0; JSR (A0)
                         "2B5F FFF0"            # MOVE.L (SP)+,-16(A5)
                         "2B4F FFEC"            # MOVE.L A7,-20(A5)
                         "A9F4")                # _ExitToShell
    app = MacFile("Swap", rsrc=build_fork([
        Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),
        Resource(b"CODE", 1, main, b"Main", 0x20)]), type=b"APPL", creator=b"SWAP")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Swap to quit")
    assert not h.d.system_errors
    assert mem.r32(a5 - 16) == lowmem.FMgrOutRec
    assert mem.r32(a5 - 20) == mem.r32(a5 - 12)     # the stack as it was
    assert mem.r8(lowmem.FMgrOutRec + 13) != 0      # FOutAscent, set


def test_moving_the_mouse_by_way_of_jcrsrtask(harness):
    """Museum Madness moves the mouse by writing MTemp and RawMouse,
    setting CrsrNew and calling the cursor's task through JCrsrTask,
    which puts the mouse there and clears CrsrNew."""
    h = harness()
    h.launch(Recorder("Other"))
    code0 = bytes.fromhex("00000028 00000010 00000008 00000020"
                          "0000 3F3C 0001 A9F0")
    main = bytes.fromhex("0000 0001"            # first entry 0, one entry
                         "203C 0050 0060"       # MOVE.L #$00500060,D0: v 80, h 96
                         "21C0 0828 21C0 082C"  # MOVE.L D0,MTemp; MOVE.L D0,RawMouse
                         "11FC 0001 08CE"       # MOVE.B #1,CrsrNew
                         "2078 08EE 4E90"       # MOVEA.L JCrsrTask,A0; JSR (A0)
                         "2B78 0830 FFFC"       # MOVE.L Mouse,-4(A5)
                         "1B78 08CE FFFB"       # MOVE.B CrsrNew,-5(A5)
                         "A9F4")                # _ExitToShell
    app = MacFile("Move", rsrc=build_fork([
        Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),
        Resource(b"CODE", 1, main, b"Main", 0x20)]), type=b"APPL", creator=b"MOVE")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Move to quit")
    assert not h.d.system_errors
    assert mem.r32(a5 - 4) == 0x00500060
    assert mem.r8(a5 - 5) == 0


def test_an_rte_follows_the_traps_code_as_in_the_rom(harness):
    """Downhill Racer looks past Dequeue's code, from GetTrapAddress, for
    the ROM's next RTE, to make the interrupts it doesn't want do nothing,
    and then checks it hasn't taken 3 seconds.  There's one soon after
    the traps' code, and it returns as an interrupt's handler does."""
    h = harness()
    h.launch(Recorder("Other"))
    code0 = bytes.fromhex("00000028 00000010 00000008 00000020"
                          "0000 3F3C 0001 A9F0")
    main = bytes.fromhex("0000 0001"            # first entry 0, one entry
                         "303C A96E A146"       # MOVE.W #$A96E,D0; _GetTrapAddress
                         "2B48 FFFC"            # MOVE.L A0,-4(A5): Dequeue's
                         "303C 4E73"            # MOVE.W #$4E73,D0: RTE
                         "B058 66FC"            # loop: CMP.W (A0)+,D0; BNE loop
                         "5588"                 # SUBQ.L #2,A0
                         "2B48 FFF8"            # MOVE.L A0,-8(A5)
                         "487A 0006"            # PEA back(PC)
                         "40E7 4ED0"            # MOVE.W SR,-(SP); JMP (A0): as an interrupt
                         "3B7C 0001 FFF6"       # back: MOVE.W #1,-10(A5)
                         "A9F4")                # _ExitToShell
    app = MacFile("Rte", rsrc=build_fork([
        Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),
        Resource(b"CODE", 1, main, b"Main", 0x20)]), type=b"APPL", creator=b"RTE ")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Rte to quit")
    assert not h.d.system_errors
    dequeue, rte = mem.r32(a5 - 4), mem.r32(a5 - 8)
    assert 0 < rte - dequeue < 0x1000 and mem.r16(rte) == 0x4E73
    assert mem.r16s(a5 - 10) == 1


def test_the_user_says_which_disk_goes_in_for_one_unmounted(harness):
    """Wizardry VI ejects and unmounts its disk and asks for another: the
    disks there are are listed, the next in turn chosen, and the one the
    user chooses is the one MountVol mounts."""
    import machfs

    from macrt.macfile import load_volume
    from macrt.ui import diskdlg

    disks = []
    for name in ("One", "Two", "Three"):
        v = machfs.Volume()
        v.name = name
        disks.append(load_volume(v.write(400 * 1024)))
    h = harness(disks=disks)
    h.launch(Recorder("Other"))
    code0 = bytes.fromhex("00000028 00000100 00000008 00000020"
                          "0000 3F3C 0001 A9F0")
    pb = "41ED FF80 317C 0001 0016"         # LEA -$80(A5),A0; MOVE.W #1,ioVRefNum(A0)
    main = bytes.fromhex("0000 0001"
                         + pb + "42A8 0012"     # CLR.L ioNamePtr(A0)
                         "A017 3B40 FFFE"       # _Eject; MOVE.W D0,-2(A5)
                         + pb + "A00E 3B40 FFF8"    # _UnmountVol; MOVE.W D0,-8(A5)
                         "303C 0080 41ED FF60"  # loop: MOVE.W #diskMask,D0; LEA -$A0(A5),A0
                         "A031 66F4"            # _GetOSEvent; BNE loop
                         + pb + "A00F 3B40 FFFC"    # _MountVol; MOVE.W D0,-4(A5)
                         "3B68 0016 FFFA"       # MOVE.W ioVRefNum(A0),-6(A5)
                         "A9F4")                # _ExitToShell
    app = MacFile("Swap", rsrc=build_fork([
        Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),
        Resource(b"CODE", 1, main, b"Main", 0x20)]), type=b"APPL", creator=b"SWAP")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5, fs = proc.system.mem, proc.partition.a5, proc.system.files
    one, two, three = fs.floppies()
    h.wait_until(lambda: h.d.disk_dialogs, timeout=5, what="the disk dialog")
    dlg = h.d.disk_dialogs[0]
    assert isinstance(dlg, diskdlg.DiskDialog)
    assert [dlg.list.item(i).text() for i in range(3)] == ["One", "Two", "Three"]
    assert dlg.list.currentRow() == 1
    dlg.list.setCurrentRow(2)
    QTest.mouseClick(dlg.ok, Qt.MouseButton.LeftButton)
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Swap to quit")
    assert not h.d.system_errors and not h.d.disk_dialogs
    assert [mem.r16s(a5 - i) for i in (2, 8, 4)] == [0, 0, 0]
    assert mem.r16s(a5 - 6) == three.vref
    assert (three.drive, one.drive) == (1, 4) and fs.offline == [one]


def test_vbl_tasks_run_every_tick_until_removed(harness):
    """VBL counts its VBL task's runs while it spins (it calls no trap, so
    only the vertical retrace gets the task in), and while it waits in
    Delay, the host having the processor: every other tick of those it
    waited, which may be a few more than 30 if the host was slow.  Then it
    masks interrupts, then removes the task, and the task doesn't run in
    either case.  A task that doesn't set its count again runs once and is
    taken out of the queue, and can be installed again.  The task's word
    above its return address lands in what the interrupt stacked, not in
    the spin's stack."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "VBL.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="VBL to finish")
    assert not h.d.system_errors
    assert mem.r16s(a5 - 16) >= 10                  # its runs
    assert mem.r32(a5 - 26) == 0x12345678           # the spin's D1, as it left it
    assert mem.r32(a5 - 36) == 0xCAFEF00D           # what it had on its stack
    runs, waited = mem.r16s(a5 - 28), mem.r16s(a5 - 38)
    assert waited >= 29 and abs(2 * runs - waited) <= 3     # every other tick
    assert mem.r16s(a5 - 18) == 0                   # none while masked
    assert mem.r16s(a5 - 20) == 0                   # VRemove: noErr
    assert mem.r16s(a5 - 22) == 0                   # none after
    assert mem.r16s(a5 - 30) == 2                   # once for each VInstall
    assert mem.r16s(a5 - 32) == -1                  # VRemove: qErr, it's gone
    assert proc.runner.unimplemented_calls == {}


def test_deferred_tasks_run_once_after_the_interrupts_tasks(harness):
    """A deferred task that a VBL task queues through jDTInstall runs
    after it, with dtParm in A1, once; one the application queues with
    DTInstall runs at a tick to come, with nothing else to run then; a
    record that isn't a deferred task's qType gets vTypErr (Inside
    Macintosh: Processes)."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "Deferred.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="Deferred to finish")
    assert not h.d.system_errors
    assert mem.r16s(a5 - 64) == -2                  # vTypErr
    assert mem.r16s(a5 - 66) == 0                   # jDTInstall: noErr
    assert mem.r16s(a5 - 58) == 1                   # the VBL task's runs
    assert mem.r16s(a5 - 56) == 1                   # the task's
    assert mem.r16s(a5 - 60) == 1                   # after the VBL task
    assert mem.r16s(a5 - 68) == 0                   # DTInstall: noErr
    assert mem.r16s(a5 - 62) == 1                   # the second task's runs
    assert mem.r32(a5 - 72) == 0                    # the queue is empty
    assert proc.runner.unimplemented_calls == {}


def test_a_late_retrace_makes_up_for_the_ticks_it_missed(monkeypatch):
    """The host late for the vertical retrace, a VBL task is counted down
    once for each tick it missed, up to MAX_LATE of them; after a break,
    interrupts masked or another application's turn, up to MAX_RETRACES;
    after a retrace that left nothing counting down, just once, so that a
    task installed since counts from then.  A longer gap counts once."""
    mem, task = RAM(SYSTEM_24, 0x10000), 0x1000
    mem.w32(lowmem.VBLQueue + 2, task)
    mem.w16(task + runtime.VBL_COUNT, 1000)
    clock = SimpleNamespace(now=5000)
    monkeypatch.setattr(runtime, "tick_count", lambda: clock.now)
    cpu = SimpleNamespace(mem=mem, mask=SYSTEM_24.addr_mask, sr=0x2000, context=lambda: None,
                          restore=lambda ctx: None,
                          sound_waiting=lambda: False)
    cpu.vbl_counting = lambda: CPU.vbl_counting(cpu)
    quiet = SimpleNamespace(retrace=lambda *args: None)
    r = SimpleNamespace(cpu=cpu, mem=mem, retrace_tick=0, catch_up=1, ctx=None,
                        machine=SimpleNamespace(sound=quiet, sound_manager=quiet, speaker=quiet),
                        process=SimpleNamespace(world=SimpleNamespace(switch_in=lambda: None)),
                        _interrupt_frame=lambda: None)

    def counted(gap):
        clock.now += gap
        before = mem.r16s(task + runtime.VBL_COUNT)
        Runner._retrace_tasks(r)
        return before - mem.r16s(task + runtime.VBL_COUNT)

    def masked():
        cpu.sr = 0x2700
        Runner._vertical_retrace(r)
        cpu.sr = 0x2000

    assert counted(0) == 1                          # the first: once
    assert [counted(1), counted(12), counted(runtime.MAX_LATE)] == [1, 12, runtime.MAX_LATE]
    assert [counted(runtime.MAX_LATE + 1), counted(12)] == [1, 12]
    masked()
    assert [counted(12), counted(12)] == [1, 12]
    masked()
    assert counted(runtime.MAX_RETRACES) == runtime.MAX_RETRACES
    other = SimpleNamespace(process=SimpleNamespace(world=SimpleNamespace(switch_out=lambda: None)))
    machine = SimpleNamespace(_lock=threading.Lock(), owner=other, cpu=cpu)
    runtime.Machine.acquire(machine, r)
    machine._lock.release()
    assert [counted(12), counted(12)] == [1, 12]
    mem.w16(task + runtime.VBL_COUNT, 0)            # waiting for nothing
    assert counted(1) == 0
    mem.w16(task + runtime.VBL_COUNT, 1000)
    assert [counted(3), counted(3)] == [1, 3]
    assert counted(0) == 0 and r.catch_up == runtime.MAX_LATE


def test_get_next_event_with_nothing_to_give_returns_at_once(harness):
    """Poll waits for the Ticks to change and then calls GetNextEvent, 30
    times: as on a Mac, where it returns at once with nothing to give, that
    takes 30 ticks, not 60.  Its 30 calls one after another take about 30
    ticks too: asked again in the same tick, it waits for the next.
    WaitNextEvent with no time to sleep does the same, in a loop that
    posts itself an event and takes it between too, as Lemmings' title
    does: 30 rounds of that take about 30 ticks, where they took none and
    kept the host's processor."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "Poll.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="Poll to finish")
    assert not h.d.system_errors
    assert mem.r32(a5 - 20) <= 45                   # a tick each, not two
    assert 25 <= mem.r32(a5 - 24) <= 45             # and no more than one a tick
    assert mem.r32(a5 - 28) <= 45                   # WaitNextEvent's
    assert 25 <= mem.r32(a5 - 32) <= 45
    assert 25 <= mem.r32(a5 - 36) <= 45             # posting, taking and asking
    assert proc.runner.unimplemented_calls == {}


@OWN_TIME
def test_a_program_asking_over_and_over_leaves_the_host_the_processor(harness):
    """Spin asks TickCount over and over for a second, then Button until
    the mouse goes down, with nothing else in its loops.  The ticks come
    as they would and it sees the press at once, but its thread is
    mostly waiting for them, not asking."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "Spin.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    clock = time.pthread_getcpuclockid(proc.thread.ident)
    shares = []
    for phase, what in ((1, "Spin to ask TickCount"), (2, "a second's ticks")):
        h.wait_until(lambda: mem.r16(a5 - 2) == phase, timeout=5, what=what)
        used, began = time.clock_gettime(clock), time.monotonic()
        if phase == 2:
            h.settle(500)
        else:
            h.wait_until(lambda: mem.r16(a5 - 2) == 2, timeout=5, what=what)
        shares.append((time.clock_gettime(clock) - used) / (time.monotonic() - began))
    pressed = tick_count()
    h.press(400, 400)
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Spin to see the press")
    h.release(400, 400)
    assert not h.d.system_errors
    assert 60 <= mem.r32(a5 - 10) <= 90             # the second, in ticks
    assert mem.r32(a5 - 6) >= 60                    # asking at each of them
    assert mem.r32(a5 - 14) - pressed <= 4          # and seeing the press
    assert all(share < 0.5 for share in shares), shares
    assert proc.runner.unimplemented_calls == {}


def test_asking_over_and_over_is_timed_by_the_programs_own_processor_time(monkeypatch):
    """The host may give the processor to something else between two of a
    program's calls to TickCount, as it does under load: that isn't the
    program doing something else, so once it's had SPIN_BUDGET of its own
    time asking it still waits for the next tick.  Its own time between
    them is something else, as is another trap.  FlushEvents between them,
    as Runaround and Run call it, isn't, but it doesn't wait itself; nor is
    KeyTranslate, as Space Junkie calls it, or GetPort, SetPort and
    PtInRect, as ArcMac and IAGO call them, or HLock, StripAddress, HUnlock
    and PrimeTime, as Opening Night calls them."""
    clock = SimpleNamespace(own=0.0, wall=0.0)
    naps, calls = [], []
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: clock.wall))
    monkeypatch.setattr(runtime, "dispatch", SimpleNamespace(
        trap_key=dispatch.trap_key, dispatch=lambda runner, word, pc: calls.append(word)))
    r = SimpleNamespace(cpu_time=lambda: clock.own, polled=-runtime.SPIN_GAP, spun_from=0.0,
                        holding=True,
                        machine=SimpleNamespace(ticking=lambda: False),
                        cpu=SimpleNamespace(rate=0, sr=0x2000), yielding=nullcontext,
                        process=SimpleNamespace(events=SimpleNamespace(
                            nap=lambda secs: naps.append((clock.own, len(calls))))))
    r._polling = lambda: Runner._polling(r)
    r._wait_for_tick = lambda: Runner._wait_for_tick(r)

    def ask(times, own, away=0.0, between=None):
        for _ in range(times):
            clock.own += own
            clock.wall += own + away
            Runner._dispatch(r, 0xA975, 0)          # TickCount
            if between:
                Runner._dispatch(r, between, 0)
    ask(30, 0.0001, away=0.001)
    assert len(naps) == 1 and naps[0][0] - 0.0001 >= runtime.SPIN_BUDGET
    naps.clear()
    ask(30, runtime.SPIN_GAP)
    ask(30, 0.0001, between=0xA122)                 # NewHandle
    assert naps == []
    for between in (0xA032, 0xA9C3,                 # FlushEvents, KeyTranslate
                    0xA874, 0xA873, 0xA8AD,         # GetPort, SetPort, PtInRect
                    0xA029, 0xA055, 0xA02A,         # HLock, StripAddress, HUnlock
                    0xA05A):                        # PrimeTime
        Runner._dispatch(r, 0xA122, 0)              # after something else
        ask(30, 0.0001, between=between)
        assert len(naps) == 1 and calls[naps[0][1]] == 0xA975
        naps.clear()
        for _ in range(60):
            clock.own += 0.0001
            Runner._dispatch(r, between, 0)
        assert naps == []


def test_where_the_host_cant_tell_a_threads_own_time_spinning_is_timed_by_the_clock(
        monkeypatch):
    """macOS has no clock for another thread's processor time: there the
    clock's time stands in, as before, and on Linux a thread's own."""
    if hasattr(time, "pthread_getcpuclockid"):
        assert abs(runtime.thread_clock()() - time.thread_time()) < 0.01
    monkeypatch.delattr(time, "pthread_getcpuclockid", raising=False)
    assert runtime.thread_clock() is time.monotonic


@OWN_TIME
def test_an_update_event_left_unanswered_comes_once_a_tick(harness):
    """Unanswered never draws its window, so GetNextEvent gives it the
    update event each time it asks, as on a Mac: but asked again in the
    same tick, it waits for the next, so 30 calls one after another take
    about 30 ticks.  Asking for a click after them, it sees the press at
    once, its thread mostly waiting."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "Unanswered.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    clock = time.pthread_getcpuclockid(proc.thread.ident)
    h.wait_until(lambda: mem.r16(a5 - 228) == 2, timeout=5, what="Unanswered to ask 30 times")
    used, began = time.clock_gettime(clock), time.monotonic()
    h.settle(500)
    share = (time.clock_gettime(clock) - used) / (time.monotonic() - began)
    pressed = tick_count()
    h.press(100, 100)
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Unanswered to see the press")
    h.release(100, 100)
    assert not h.d.system_errors
    assert mem.r32(a5 - 232) >= 28                  # the update, each time but the activate
    assert 25 <= mem.r32(a5 - 236) <= 45            # once a tick
    assert mem.r32(a5 - 240) - pressed <= 4         # and the press at once
    assert share < 0.5, share
    assert proc.runner.unimplemented_calls == {}


def test_a_task_that_waits_at_a_tick_keeps_the_processor():
    """A VBL task the ticker runs while its application waits, calling a
    trap that waits too (Delay): the ticker keeps the processor, so the
    application doesn't go on under the task, and leaves it waiting."""
    calls = []
    machine = SimpleNamespace(ticking=lambda: True, release=lambda: calls.append("release"),
                              idle=lambda runner: calls.append("idle"))
    runner = SimpleNamespace(machine=machine, holding=False)
    with Runner.yielding(runner):
        pass
    assert calls == []
    assert runner.holding is False


def test_time_manager_tasks_run_when_due(harness):
    """Timer's extended task primes itself every 10 ms, in microseconds
    negated: its 30 runs take about 18 ticks, not 30 (one a tick), and
    none come once it's removed.  _InsXTime, InsTime's trap with bit 10
    set, made it extended: PrimeTime set its tmWakeUp.
    One primed for a second and removed at once never runs, and is left
    with most of the second, in microseconds negated."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "Timer.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="Timer to finish")
    assert not h.d.system_errors
    assert mem.r16(a5 - 48) & 0x8000                # waiting, once primed
    assert mem.r32(a5 - 66) != 0                    # tmWakeUp: extended
    assert 15 <= mem.r32(a5 - 60) < 28              # the ticks its 30 runs took
    assert mem.r16s(a5 - 62) == 0                   # none once removed
    assert -1_000_000 <= mem.r32s(a5 - 52) < -800_000   # what the other had left
    assert not mem.r16(a5 - 54) & 0x8000            # and it's not waiting
    assert mem.r16s(a5 - 56) == 0                   # nor did it run
    assert proc.runner.unimplemented_calls == {}


# -- Threads: the Thread Manager -----------------------------------------------------
def test_threads_take_turns_as_they_yield(harness):
    """Threads' main thread makes a thread and yields to it until it's
    gone: the thread, given its parameter, yields back, once from inside
    a grow-zone function its NewPtr has the Memory Manager call, and its
    result lands where NewThread was told.  One made stopped can't be
    yielded to; made ready, and disposed of while it waits, its
    terminator runs and it gets the result given.  One disposes of
    itself, and one left waiting as the application quits is ended."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "Threads.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="Threads to finish")
    assert not h.d.system_errors
    assert proc.runner.unimplemented_calls == {}
    log = mem.read(a5 - 32, mem.r16(a5 - 34))
    assert log == b"MWMWGMWMwtMsMfM"
    assert mem.r32(a5 - 38) & 1                     # Gestalt 'thds'
    assert mem.r16s(a5 - 48) == 0                   # NewThread
    assert mem.r32(a5 - 42) == 3                    # the first's id
    assert mem.r32(a5 - 52) == 0x1234               # its parameter
    assert mem.r32(a5 - 56) == 3                    # GetCurrentThread in it
    assert mem.r32(a5 - 60) == 2                    # and back in the main thread
    assert 0 < mem.r32(a5 - 64) <= glue_threads.DEFAULT_STACK   # StackSpace: its own
    assert mem.r16(a5 - 66) == 1                    # the grow-zone function ran once
    assert mem.r32(a5 - 70) == 0                    # and freed nothing
    assert mem.r32(a5 - 46) == 0x5678               # the first's result
    assert mem.r16(a5 - 80) == glue_threads.STOPPED     # the second, made stopped
    assert mem.r16s(a5 - 82) == glue_threads.threadProtocolErr  # can't be yielded to
    assert mem.r16s(a5 - 84) == 0                   # DisposeThread
    assert mem.r32(a5 - 78) == 0x9ABC               # its result
    assert (mem.r32(a5 - 88), mem.r32(a5 - 92)) == (mem.r32(a5 - 74), 0x77)  # its terminator's
    assert mem.r32(a5 - 100) == 0xDEF0              # the third's, disposing of itself
    assert mem.r8(a5 - 102) == 0                    # and that call didn't return
    assert not [th for th in threading.enumerate() if th.name.startswith("Threads thread")]


# -- Driver: an application's own driver ----------------------------------------------
def test_an_applications_own_driver(harness):
    """Driver opens its DRVR 20, .Echo, and writes to it and reads back;
    leaves a control call going, asynchronously, for a status call to
    finish through IODone, then synchronously, waiting while a VBL task
    makes the status call; kills one left going; closes it, writes to it
    closed, and opens it again.  .Echo's open routine leaves D0 non-zero
    and .Shy's puts openErr in ioResult: an open's result is ioResult's
    (Inside Macintosh II, the Device Manager).  When it quits, the
    driver's gone."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "Driver.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    machine = proc.runner.machine
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="Driver to finish")
    assert not h.d.system_errors
    word = lambda at: mem.r16s(a5 - at)    # noqa: E731
    assert (word(182), word(184), word(186)) == (0, -21, -21)      # Open; unit 20's refnum
    assert (word(188), mem.r32(a5 - 192), word(194)) == (0, 5, 0)   # Write, 5 bytes; Read
    assert mem.read(a5 - 210, 5) == b"Hello"
    assert (word(212), word(214)) == (0, 1)             # queued, and going
    assert (word(216), word(218)) == (-36, -36)         # finished: ioErr, and completed
    assert word(220) == -36                             # the synchronous call's, waited for
    assert (word(222), word(224)) == (0, -27)           # KillIO; abortErr
    assert (word(232), word(226), mem.r32(a5 - 230)) == (2, -27, a5 - 100)
    assert (word(234), word(236)) == (0, -28)           # Close; notOpenErr after it
    assert (word(238), word(240)) == (0, -21)           # open again, the same unit
    assert word(242) == -43                             # .Nope: no such driver, or file
    assert word(244) == -23                             # .Shy: openErr
    table = mem.r32(lowmem.UTableBase)
    assert mem.r32(table + 4 * 20) == 0 and machine.drivers == {}
    assert proc.runner.unimplemented_calls == {}


def test_a_driver_in_the_disks_system_file(harness):
    """Driver's .Echo opened from the System file on the disk it came
    from, as on a Mac, with its device control entry under 32K, where a
    Mac's are: Speller Bee's .SAY goes to IODone with its DCE's address a
    word extended.  When Driver quits, the driver's still in the System."""
    f = open_application(APPS / "Driver.bin")
    fork = open_fork(f.rsrc)
    v = machfs.Volume()
    v.name = "Driver Disk"
    drivers = [e for e in fork.entries if e.type == b"DRVR"]
    for name, kind, entries in (("System", b"ZSYS", drivers),
                                ("Driver", b"APPL", [e for e in fork.entries if e not in drivers])):
        file = machfs.File()
        file.type, file.creator = kind, b"MACS" if kind == b"ZSYS" else b"????"
        file.rsrc = build_fork([Resource(e.type, e.id, fork.raw(e), e.name, e.attrs)
                                for e in entries])
        v[name] = file
    vol = load_volume(v.write(800 * 1024, desktopdb=False, bootable=False))
    h = harness(disks=[vol])
    app = next(a for a in volume_files(vol) if a.name == "Driver")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5 = proc.system.mem, proc.partition.a5
    machine = proc.runner.machine
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="Driver to finish")
    assert not h.d.system_errors
    word = lambda at: mem.r16s(a5 - at)    # noqa: E731
    assert (word(182), word(184), word(186)) == (0, -21, -21)      # Open; unit 20's refnum
    assert mem.read(a5 - 210, 5) == b"Hello"
    assert (word(238), word(240)) == (0, -21)           # open again, the same unit
    assert mem.r32(lowmem.UTableBase) < 0x8000
    assert all(mem.r32(dh) & 0xFFFFFF < 0x8000 for dh in machine.low_dces)
    assert sorted(machine.spare_dces) == sorted(machine.low_dces) and machine.drivers == {}
    e = proc.system.sys_map.find(b"DRVR", 20)
    assert e.name == b".Echo" and mem.r32(e.handle)     # kept: it's only in memory


# -- Desk: a desk accessory ----------------------------------------------------------
def test_a_desk_accessory(harness):
    """Desk's Apple menu lists its DRVR 12, Tally, which it opens.  Tally's
    window comes up in front, and GetNextEvent gives Tally the events for
    it, not Desk; SystemTask gives it time and the cursor, SystemEvent a
    key but not a Command key, SystemEdit Copy, SystemClick the click
    FindWindow says is in a system window, and MenuKey Command-T, from
    its own menu.  Each time, Desk's port is put back.  With Desk's window
    in front, it gets none of that but time.  Desk closes it, opens it
    again and quits, which closes it too."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "Desk.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    machine = proc.runner.machine
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="Desk to finish")
    assert not h.d.system_errors
    word = lambda at: mem.r16s(a5 - at)    # noqa: E731
    long = lambda at: mem.r32(a5 - at)     # noqa: E731
    flag = lambda at: mem.r8(a5 - at)      # noqa: E731
    assert word(492) == 1 and mem.read(a5 - 486, 7) == b"\x06\x00Tally"     # the Apple menu
    assert (word(494), word(500)) == (-13, -13)         # its refnum, and its window's kind
    assert word(502) == 1 and word(552) == 1            # Desk's port after OpenDeskAcc, SystemTask
    assert word(530) == 0                               # no events for its window were Desk's
    assert (word(514), word(516), word(518)) == (1, 1, 1)   # an update, an activate, a deactivate
    assert (word(506), word(508), word(550)) == (2, 1, 1)   # accRun each SystemTask; accCursor once
    assert (flag(532), flag(534), word(510), word(512)) == (0, 1, 1, 0x0778)
    assert (flag(536), word(526), flag(548)) == (1, 71, 0)  # SystemEdit: accCopy; not Desk's window
    assert (word(538), long(542)) == (2, long(498))     # inSysWindow, Tally's
    assert word(520) == 1                               # the click
    assert (long(546), long(524)) == (0, 0xFFF30001)    # MenuKey: Tally's, Count
    assert long(556) == 0 and long(560) == long(230)    # closed: gone, and Desk's window in front
    assert (word(504), word(528)) == (2, 2)             # opened twice, and closed when Desk quit
    table = mem.r32(lowmem.UTableBase)
    assert mem.r32(table + 4 * 12) == 0 and machine.drivers == {}
    assert proc.runner.unimplemented_calls == {}


# -- Panel: a control panel -----------------------------------------------------------
def launch_panel(h, monkeypatch, runs_here=1, extra=(), memory=None):
    """Panel, its 'cdev' answering macDev with runs_here, and a log of
    what the host sends it: (message, item, storage, the result).  extra
    are resources it has as well, or instead of its own; memory is its
    file's memory size, in bytes."""
    sent = []
    send = controlpanel.Host._send

    def logged(host, message, item=0):
        result = send(host, message, item)
        sent.append((message, item, host.storage, result))
        return result
    monkeypatch.setattr(controlpanel.Host, "_send", logged)
    f = open_application(APPS / "Panel.bin")
    fork = open_fork(f.rsrc)
    code = bytearray(fork.raw(fork.find(b"cdev", -4064)))
    code[2:4] = runs_here.to_bytes(2, "big")
    replaced = {(r.type, r.id) for r in extra}
    f = MacFile("Panel", type=b"cdev", creator=b"TEST", rsrc=build_fork([
        *(Resource(e.type, e.id, bytes(code) if e.type == b"cdev" else fork.raw(e))
          for e in fork.entries if (e.type, e.id) not in replaced), *extra]))
    h.launch(Recorder("Other"))
    return h.d.launch(f.name, None, app=f, memory=memory), sent


def test_a_control_panel(harness, monkeypatch):
    """Panel's window is its area's size less the outline round it, called
    what its file is.  It's asked with macDev whether it runs here, then
    sent initDev with cdevUnset; what it returns comes back to it each
    time after.  A click on its checkbox is hitDev with that item, and
    Command-W closes it, with closeDev, which ends it."""
    h = harness()
    proc, sent = launch_panel(h, monkeypatch)
    h.wait_until(lambda: proc.windows and proc.windows[0].visible, timeout=10,
                 what="Panel's window")
    h.wait_until(lambda: len(sent) >= 2, what="initDev")
    rec = proc.windows[0]
    c = rec.content
    assert (c.right - c.left, c.bottom - c.top) == (300 - 87 - 2, 80 + 1 - 2)
    assert rec.title == "Panel"
    assert (rec.tx_font, rec.tx_face, rec.tx_size) == (1, 0, 9)  # no 'finf': Geneva 9
    assert sent[0] == (controlpanel.macDev, 0, controlpanel.cdevUnset, 1)
    message, _, unset, storage = sent[1]
    assert (message, unset) == (controlpanel.initDev, controlpanel.cdevUnset)
    mem = proc.system.mem
    assert mem.r8(mem.r32(storage)) == 0xFF             # it saw cdevUnset
    h.click(c.left + 110 - 88, c.top + 48)              # the checkbox
    h.wait_until(lambda: any(m == controlpanel.hitDev for m, *_ in sent), what="hitDev")
    assert next(e for e in sent if e[0] == controlpanel.hitDev) == \
        (controlpanel.hitDev, 2, storage, storage)
    assert all(s == storage for m, _, s, _ in sent[2:])
    h.key(Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Panel to close")
    assert sent[-1][0] == controlpanel.closeDev and not h.d.system_errors
    assert proc.runner.unimplemented_calls == {}


def test_a_control_panel_that_fails_is_closed(harness, monkeypatch):
    """cdevGenErr, from a click on its button, closes it, with no
    closeDev."""
    h = harness()
    proc, sent = launch_panel(h, monkeypatch)
    h.wait_until(lambda: proc.windows and proc.windows[0].visible and len(sent) >= 2,
                 timeout=10, what="Panel's window")
    c = proc.windows[0].content
    h.click(c.left + 135 - 88, c.top + 20)              # Fail
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Panel to close")
    assert sent[-1][:2] == (controlpanel.hitDev, 1) and sent[-1][3] == controlpanel.cdevGenErr
    assert controlpanel.closeDev not in [m for m, *_ in sent]


def test_a_control_panel_for_another_macintosh(harness, monkeypatch):
    """One that says to macDev it can't run here isn't opened."""
    h = harness()
    proc, sent = launch_panel(h, monkeypatch, runs_here=0)
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="Panel to finish")
    assert [m for m, *_ in sent] == [controlpanel.macDev] and not h.d.system_errors


def test_a_control_panels_size_resource_is_not_its_partition(harness, monkeypatch):
    """A SIZE resource in its file, and its file's memory size, are for
    the application its CODE makes: hosted, it has the host's partition."""
    size = struct.pack(">HII", 0, 64 << 10, 64 << 10)
    h = harness()
    proc, sent = launch_panel(h, monkeypatch, extra=[Resource(b"SIZE", -1, size)],
                              memory=1024 << 10)
    h.wait_until(lambda: proc.windows and proc.windows[0].visible and len(sent) >= 2,
                 timeout=10, what="Panel's window")
    assert proc.partition.end - proc.partition.start == DEFAULT_PARTITION
    assert h.d.memory_sizes == {proc.app.key: 1024 << 10}
    assert not h.d.system_errors


def test_a_files_memory_size_is_for_its_later_launches(harness):
    """As Get Info's memory size is the file's: After Dark 4.0's panel
    launches the application in its own file, which needs it."""
    h = harness()
    app = MacFile("Sized", type=b"APPL", creator=b"TEST", rsrc=build_fork([]))
    first = h.d.launch(app.name, Recorder(app.name), app=app, memory=900 << 10)
    later = h.d.launch(app.name, Recorder(app.name), app=app)
    for proc in (first, later):
        assert proc.partition.end - proc.partition.start == (900 << 10) - SIZE_STACK + DEFAULT_STACK


def test_a_control_panels_text_is_in_its_finf_s_font(harness, monkeypatch):
    """The first font in its 'finf' -4049, Geneva 9 bold here, is the
    window's, and its edit text's."""
    items = ditl((ctrlItem | btnCtrl, (10, 100, 30, 170), b"Fail"),
                 (ctrlItem | chkCtrl, (40, 100, 56, 220), b"Checked"),
                 (editText, (60, 100, 76, 200), b"Text"))
    finf = struct.pack(">h3h3h", 2, 3, qdraw.bold, 9, 0, 0, 12)
    h = harness()
    proc, sent = launch_panel(h, monkeypatch, extra=[
        Resource(b"DITL", controlpanel.CDEV_ID, items),
        Resource(b"finf", controlpanel.FONT_ID, finf)])
    h.wait_until(lambda: proc.windows and proc.windows[0].visible and len(sent) >= 2,
                 timeout=10, what="Panel's window")
    w = proc.windows[0]
    assert (w.tx_font, w.tx_face, w.tx_size) == (3, qdraw.bold, 9)
    mem = proc.system.mem
    text_h = mem.r32(w.addr + 160)
    rec = te.TERec(mem, mem.r32(text_h) & 0xFFFFFF)
    assert (rec.tx_font, rec.tx_face, rec.tx_size) == (3, qdraw.bold, 9)
    assert proc.runner.unimplemented_calls == {} and not h.d.system_errors


def test_a_control_panels_inits_run_first(harness, monkeypatch):
    """Its 'INIT' is called first, as at startup, with its handle in A0
    and the panel's A5; one that calls InitGraf in an A5 world of its own
    leaves the panel's QuickDraw globals where they were."""
    scratch = lowmem.ApplScratch
    init = struct.pack(">HH HH H HH HH HH H HH H H", 0x21C8, scratch,  # MOVE.L A0,ApplScratch
                       0x21CD, scratch + 4,             # MOVE.L A5,ApplScratch+4
                       0x2F0D,                          # MOVE.L A5,-(SP)
                       0x4FEF, -210 & 0xFFFF,           # LEA -210(SP),SP
                       0x4BEF, 206,                     # LEA 206(SP),A5: its globals
                       0x486D, -4 & 0xFFFF,             # PEA -4(A5)
                       0xA86E,                          # _InitGraf
                       0x4FEF, 210,                     # LEA 210(SP),SP
                       0x2A5F,                          # MOVEA.L (SP)+,A5
                       0x4E75)                          # RTS
    h = harness()
    proc, sent = launch_panel(h, monkeypatch, extra=[Resource(b"INIT", 0, init)])
    h.wait_until(lambda: proc.windows and proc.windows[0].visible and len(sent) >= 2,
                 timeout=10, what="Panel's window")
    mem, a5 = proc.system.mem, proc.partition.a5
    handle = mem.r32(scratch)
    assert mem.read(mem.r32(handle) & 0xFFFFFF, len(init)) == init
    assert mem.r32(scratch + 4) == a5 and mem.r32(a5) == a5 - 4
    assert proc.runner.unimplemented_calls == {} and not h.d.system_errors


@pytest.mark.parametrize("attrs", [resSysHeap, 0])
def test_a_control_panels_init_patches_stay_for_the_applications_after(harness, monkeypatch,
                                                                       attrs):
    """The trap its INIT patched stays patched for an application launched
    after, when the patch is in the system heap: one in the panel's own
    partition goes with it."""
    init = struct.pack(">HH HH H H H", 0x41FA, 10,         # LEA patch(PC),A0
                       0x303C, 0xA9C8,                     # MOVE.W #_SysBeep,D0
                       0xA647,                             # _SetToolTrapAddress
                       0x4E75,                             # RTS
                       0x4E75)                             # patch: RTS
    h = harness()
    proc, sent = launch_panel(h, monkeypatch, extra=[Resource(b"INIT", 0, init, attrs=attrs)])
    h.wait_until(lambda: proc.windows and proc.windows[0].visible and len(sent) >= 2,
                 timeout=10, what="Panel's window")
    patch = proc.runner.patches[0xA9C8]
    in_system_heap = proc.system.heap_of(patch) is proc.system.sys_heap
    assert in_system_heap == bool(attrs)
    f = open_application(APPS / "Hello.bin")
    hello = h.d.launch(f.name, None, app=f)
    h.wait_until(lambda: hello.windows, timeout=10, what="Hello's window")
    assert hello.runner.patches.get(0xA9C8) == (patch if attrs else None)
    assert not h.d.system_errors


def test_a_control_panels_init_that_stops_is_left_out(harness, monkeypatch, caplog):
    """One that stops with a system error is as if it hadn't been loaded:
    the trap it patched is put back, and the panel opens without it."""
    init = struct.pack(">HH HH H H", 0x41FA, 0,            # LEA *+2(PC),A0
                       0x303C, 0xA9C8,                     # MOVE.W #_SysBeep,D0
                       0xA647,                             # _SetToolTrapAddress
                       0x4AFC)                             # ILLEGAL
    h = harness()
    proc, sent = launch_panel(h, monkeypatch, extra=[Resource(b"INIT", 0, init)])
    h.wait_until(lambda: proc.windows and proc.windows[0].visible and len(sent) >= 2,
                 timeout=10, what="Panel's window")
    assert "its INIT 1 stopped (system error 3" in caplog.text
    assert 0xA9C8 not in proc.runner.patches and not h.d.system_errors


@pytest.mark.parametrize("attrs", [0, resSysHeap])
def test_a_control_panel_that_stops_as_it_opens_with_its_init_opens_again_without(
        harness, monkeypatch, caplog, attrs):
    """Its INIT patches GetNextEvent with what the Mac here can't run, and
    the panel stops at its first event: it's opened again, as on a Mac
    started with its extension off, and the patch is gone, wherever the
    INIT was."""
    init = struct.pack(">HH HH H H H", 0x41FA, 10,         # LEA patch(PC),A0
                       0x303C, 0xA970,                     # MOVE.W #_GetNextEvent,D0
                       0xA647,                             # _SetToolTrapAddress
                       0x4E75,                             # RTS
                       0x4AFC)                             # patch: ILLEGAL
    h = harness()
    first, sent = launch_panel(h, monkeypatch, extra=[Resource(b"INIT", 0, init, attrs=attrs)])

    def again():
        return next((p for p in h.d.processes if p is not first and p.name == first.name
                     and p.windows and p.windows[0].visible), None)
    h.wait_until(lambda: again() is not None and first not in h.d.processes, timeout=10,
                 what="Panel opened again")
    assert "opening it again without" in caplog.text and not h.d.system_errors
    assert again().runner.patches == {} and first.system.init_patches == {}


def opened_again(h, first):
    return next((p for p in h.d.processes if p is not first and p.name == first.name
                 and p.windows and p.windows[0].visible), None)


def test_a_control_panel_that_stops_in_its_inits_patch_later_opens_again_without(
        harness, monkeypatch, caplog):
    """Its INIT's GetNextEvent patch answers the first WaitNextEvent's
    calls with no event, and stops at its third, once the panel has
    opened: it stopped in its extension's code."""
    init = struct.pack(">HH HH H H HH H HH H H H H H H H", 0x41FA, 10,  # LEA patch(PC),A0
                       0x303C, 0xA970,                     # MOVE.W #_GetNextEvent,D0
                       0xA647,                             # _SetToolTrapAddress
                       0x4E75,                             # RTS
                       0x43FA, 20,                         # patch: LEA count(PC),A1
                       0x5251,                             # ADDQ.W #1,(A1)
                       0x0C51, 3,                          # CMPI.W #3,(A1)
                       0x6708,                             # BEQ.S stop
                       0x205F,                             # MOVEA.L (SP)+,A0
                       0x5C8F,                             # ADDQ.L #6,SP
                       0x4257,                             # CLR.W (SP): FALSE
                       0x4ED0,                             # JMP (A0)
                       0x4AFC,                             # stop: ILLEGAL
                       0)                                  # count
    h = harness()
    first, sent = launch_panel(h, monkeypatch, extra=[Resource(b"INIT", 0, init)])
    h.wait_until(lambda: opened_again(h, first) is not None and first not in h.d.processes,
                 timeout=10, what="Panel opened again")
    assert "opening it again without" in caplog.text and not h.d.system_errors


def test_a_control_panel_that_stops_in_its_own_code_isnt_opened_again(
        harness, monkeypatch, caplog):
    """Opened with its INIT run, it stops in its own code: the error is its own."""
    send = controlpanel.Host._send

    def stops(host, message, item=0):
        if host.opened and message == controlpanel.nulDev:
            pc = host.t.system.heap_of(host.code).deref(host.code)
            raise SystemError68k(3, f"at ${pc:06X}", pc)
        return send(host, message, item)
    monkeypatch.setattr(controlpanel.Host, "_send", stops)
    h = harness()
    first, _ = launch_panel(h, monkeypatch, extra=[Resource(b"INIT", 0, b"\x4E\x75")])
    h.wait_until(lambda: h.d.system_errors, timeout=10, what="Panel's system error")
    assert h.d.system_errors[0][:2] == ("Panel", 3)
    assert "again without" not in caplog.text and opened_again(h, first) is None


# -- Vectors: a program's own exception handlers -------------------------------------
def test_a_programs_own_exception_handlers(harness):
    """Vectors sets off TRAP #1 and divides by zero, twice each, and CHK
    once, with its own handlers in their vectors: each goes back past the
    instruction.
    Then the VIA interrupts its own handler for it, which goes on to the
    ROM's with TRAP #0, as Flight Simulator's does; and when it quits, the
    vectors that pointed into it are the System's again."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "Vectors.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    machine = proc.runner.machine
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="Vectors to finish")
    assert not h.d.system_errors
    assert (mem.r16s(a5 - 2), mem.r16s(a5 - 4), mem.r16s(a5 - 6)) == (2, 2, -1)
    assert mem.r16(a5 - 8) == 7                     # what was there: no quotient
    assert mem.r16s(a5 - 26) == 1                   # CHK's
    assert mem.r16s(a5 - 12) >= 5                   # the VIA's interrupts
    assert mem.r16s(a5 - 14) == mem.r16s(a5 - 12)   # each with a flag to clear
    assert mem.r16s(a5 - 16) >= 1                   # which the ROM's handler did
    rom = mem.r32(a5 - 24)
    assert rom == mem.r32(lowmem.ROMBase) and mem.r16(rom + 8) == 0x0075
    assert mem.r32(a5 - 20) == machine.bomb_stub
    assert rom <= machine.bomb_stub and machine.bomb_stub > mem.r32(lowmem.ScrnBase)
    assert mem.r32(0x14) == mem.r32(0x18) == mem.r32(0x84) == machine.bomb_stub
    assert mem.r32(0x64) == machine.level_1
    assert proc.runner.unimplemented_calls == {}


# -- Tempo: a routine of its own in Lvl1DT ----------------------------------------
def test_a_programs_own_routine_in_lvl1dt_runs_each_time_the_via_flags_it(harness):
    """Tempo puts its own routine for timer 1 in Lvl1DT, as Master Tracks
    Pro does for its tempo's clock, and runs the timer free at 100 Hz: the
    ROM's handler calls it each time round, with A1 pointing at the VIA,
    and keeps D0-D3 and A0-A3 for the code it interrupted."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "Tempo.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    machine = proc.runner.machine
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="Tempo to finish")
    assert not h.d.system_errors
    assert mem.r16s(a5 - 2) >= 30                       # its runs
    assert mem.r16s(a5 - 4) == 0                        # each with the VIA in A1
    assert mem.r16s(a5 - 6) == 0                        # D2 and A2 as they were
    ticks = mem.r32(a5 - 14) - mem.r32(a5 - 10)
    assert 16 <= ticks <= 120                           # 30 times round: 18 ticks
    assert mem.r32(0x64) == machine.level_1             # the ROM's handler all along
    assert mem.r32(lowmem.Lvl1DT + 24) == machine.level_1_routines + 6 * LEVEL_1_ROUTINE
    assert proc.runner.unimplemented_calls == {}


# -- Sounds: the Sound Driver and the sound buffer -----------------------------------
def test_the_sound_driver_plays_its_three_synthesizers_and_the_buffer_is_heard(
        harness, tmp_path):
    """Sounds writes the Sound Driver a square wave, synchronously; four
    tones, asynchronously; and a free-form sound with a count of 0, which
    holds until KillIO stops it.  Then it fills the sound buffer itself
    and turns the speaker on.  What the speaker played is in the WAV
    file, a frame a tick."""
    wav = tmp_path / "sounds.wav"
    h = harness(record_sound=str(wav))
    h.launch(Recorder("Other"))
    f = open_application(APPS / "Sounds.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="Sounds to finish")
    assert not h.d.system_errors
    assert proc.runner.unimplemented_calls == {}
    word = lambda at: mem.r16s(a5 + at)             # noqa: E731
    assert word(-102) == 0                          # the square wave: noErr
    # Its 5 ticks, the first of them perhaps just after StartSound, as on a Mac.
    assert 4 <= mem.r32(a5 - 106) <= 60
    assert mem.r32(a5 - 110) == 20                  # all of it
    assert (word(-112), word(-114)) == (1, 0)       # four tones: done, noErr
    assert (word(-120), word(-116), word(-118)) == (0, 1, -27)  # held, then abortErr
    assert (word(-122), word(-124)) == (0, -27)     # KillIO, and the write's ioResult
    record = a5 - 174
    assert mem.r16s(record) == 0                    # its duration, counted down
    assert mem.r32(record + 6) == (4 * 370 & 0xFF) << 16    # the first tone's phase
    assert mem.r8(lowmem.SoundActive) == 0
    h.d.shutdown(quit_app=False)

    with wave.open(str(wav)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 1, 22200)
        samples = w.readframes(w.getnframes())
    frames = [samples[i:i + 370] for i in range(0, len(samples), 370)]
    levels = [frozenset(f) for f in frames]
    assert levels.count({28, 228}) == 3             # 200 about the middle, for 3 ticks
    assert levels.count({78, 178}) == 2             # then 100, for 2
    ramp = bytes((i & 0xFF) >> 2 for i in range(4 * 370))
    tones = [ramp[i:i + 370] for i in range(0, len(ramp), 370)]
    assert [f for f in frames if f in tones] == tones
    # The free-form sound, held for its five ticks: one may go by with no
    # frame of its own when the host is late with a tick.
    assert levels.count({0xC0}) >= 3
    assert levels.count({0x40}) >= 4                # the buffer, written straight


def test_the_sound_chip_written_between_traps_is_heard_as_the_program_runs_on(
        harness, tmp_path):
    """On a Mac II, a program fills the Apple Sound Chip's first FIFO
    itself and spins for five ticks before its next trap: what it wrote
    is played at a tick as it spins, not left for after it quits."""
    wav = tmp_path / "chip.wav"
    h = harness(model="ii", record_sound=str(wav))
    code = bytes.fromhex(f"2278 {lowmem.ASCBase:04X}"     # MOVEA.L ASCBase,A1
                         "323C 0171"                     # MOVE.W #369,D1
                         "12BC 0040"                     # 1: MOVE.B #$40,(A1)
                         "51C9 FFFA"                     # DBRA D1,1b
                         f"2038 {lowmem.Ticks:04X}"      # MOVE.L Ticks,D0
                         "5A80"                          # ADDQ.L #5,D0
                         f"B0B8 {lowmem.Ticks:04X}"      # 2: CMP.L Ticks,D0
                         "62FA"                          # BHI.S 2b
                         "A9F4")                         # _ExitToShell
    app = MacFile("Chip", rsrc=build_fork(code_resources(code, 4)), type=b"APPL",
                  creator=b"CHIP")
    proc = h.d.launch(app.name, None, app=app)
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="Chip to finish")
    assert not h.d.system_errors
    h.d.shutdown(quit_app=False)
    with wave.open(str(wav)) as w:
        samples = w.readframes(w.getnframes())
    frames = [frozenset(samples[i:i + 370]) for i in range(0, len(samples), 370)]
    assert frames == [{0x40}]


@OWN_TIME
def test_waiting_for_a_synchronous_sound_leaves_the_host_the_processor(harness):
    """A square wave written to the Sound Driver for 60 ticks and a half
    second of samples played with SndPlay, each synchronously: each waits
    its time in the ROM's loop, but the thread mostly waits for the host."""
    h = harness()
    h.launch(Recorder("Other"))
    square = struct.pack(">7h", -1, 1958, 200, 60, 0, 0, 0)
    head = bytes.fromhex(f"2B78 {lowmem.Ticks:04X} FFCA"   # MOVE.L Ticks,-54(A5)
                         "41ED FFCE"                     # LEA -50(A5),A0: the parameter block
                         "2248"                          # MOVEA.L A0,A1
                         "7018"                          # MOVEQ #24,D0
                         "4259"                          # 1: CLR.W (A1)+
                         "51C8 FFFC"                     # DBRA D0,1b
                         "317C FFFC 0018"                # MOVE.W #-4,ioRefNum(A0)
                         "43FA")                         # LEA square(PC),A1
    tail = bytes.fromhex("2149 0020"                     # MOVE.L A1,ioBuffer(A0)
                         f"217C {len(square):08X} 0024"  # MOVE.L #20,ioReqCount(A0)
                         "A003"                          # _Write
                         f"2B78 {lowmem.Ticks:04X} FFC6"  # MOVE.L Ticks,-58(A5)
                         "554F" "42A7"                   # SndPlay(NIL,
                         "594F" "2F3C 736E 6420" "3F3C 0080" "A9A0"  # GetResource('snd ', 128),
                         "4267" "A805"                   # FALSE)
                         "3B5F FFC0"                     # MOVE.W (SP)+,-64(A5)
                         f"2B78 {lowmem.Ticks:04X} FFC2"  # MOVE.L Ticks,-62(A5)
                         "207C 0000 001E" "A03B"         # Delay(30)
                         "A9F4")                         # _ExitToShell
    code = head + struct.pack(">h", len(tail) + 2) + tail + square
    samples = bytes(0x80 + (i & 0x1F) for i in range(11127))
    snd = struct.pack(">hhhHhI", 2, 0, 1, 0x8051, 0, 14) + struct.pack(
        ">5IBB", 0, len(samples), 0x56EE8BA3, 0, 0, 0, 60) + samples   # bufferCmd, 22 kHz
    app = MacFile("Wait", rsrc=build_fork(code_resources(code, 64) + [Resource(b"snd ", 128, snd)]),
                  type=b"APPL", creator=b"WAIT")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: mem.r32(a5 - 54), timeout=5, what="Wait to write")
    clock = time.pthread_getcpuclockid(proc.thread.ident)
    used, began = time.clock_gettime(clock), time.monotonic()
    h.wait_until(lambda: mem.r32(a5 - 62), timeout=5, what="Wait to play")
    share = (time.clock_gettime(clock) - used) / (time.monotonic() - began)
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Wait to finish")
    assert not h.d.system_errors
    assert proc.runner.unimplemented_calls == {}
    assert 58 <= mem.r32(a5 - 58) - mem.r32(a5 - 54) <= 90      # the square wave's 60 ticks
    assert mem.r16s(a5 - 64) == 0                               # SndPlay: noErr
    assert 28 <= mem.r32(a5 - 62) - mem.r32(a5 - 58) <= 60      # and its half second
    assert share < 0.5, share


# -- ResStub: a patch on _ResourceStub ---------------------------------------------
def test_a_patch_on_the_resource_stub_reads_resources_in(harness):
    """ResStub patches _ResourceStub and gets STR 128, which its patch
    leaves to the original; STR 129, which the patch makes; and STR 130
    with ResLoad off, which it then loads."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "ResStub.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="ResStub to finish")
    assert not h.d.system_errors
    assert mem.r16s(a5 - 30) == 4                       # one call for each, LoadResource too
    assert mem.r32(a5 - 8) == mem.r32(a5 - 12) != 0     # A4: the application's map
    assert mem.r16s(a5 - 32) == 128                     # A2: the entry
    assert mem.r16s(a5 - 34) == -1                      # A3 too
    assert mem.r16s(a5 - 36) == -1                      # and they all came back
    assert mem.read(a5 - 40, 4) == b"\xff\xff\x00\xff"   # ResLoad: on, on, off, LoadResource's on
    assert mem.read(a5 - 16, 4) == b"\x04rea"            # read
    assert mem.read(a5 - 20, 4) == b"\x04mad"            # made
    assert mem.r32(a5 - 28) == 0                        # empty with ResLoad off,
    assert mem.read(a5 - 24, 4) == b"\x06loa"            # and then loaded
    assert proc.runner.unimplemented_calls == {}


# -- OSPatch: patches on an OS trap ----------------------------------------------
def test_an_os_trap_patch_is_called_as_the_dispatcher_calls_it(harness):
    """OSPatch patches _GetHandleSize twice: once to look at what the trap
    dispatcher gives it and go on to the original, and once to go back to
    the caller itself, taking the dispatcher's frame off, as Microsoft
    Word's patch does."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "OSPatch.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="OSPatch to finish")
    assert not h.d.system_errors
    assert mem.r16(a5 - 10) == 0xA025                       # D1: the trap word
    assert mem.r16(a5 - 12) == 0x25 << 2                    # D2: its place in the table
    assert mem.r32(a5 - 16) == mem.r32(a5 - 52)             # A2: the patch
    assert mem.r32(a5 - 20) == mem.r32(a5 - 8)              # stacked: A0,
    assert mem.r32(a5 - 24) == 0x12345678                   # A1,
    assert mem.r16(a5 - 26) == 0xA025                       # the trap word,
    assert mem.r32(a5 - 30) & 0xFFFFFF == mem.r32(a5 - 34) & 0xFFFFFF    # where it goes back
    assert mem.r32(a5 - 38) == 10
    assert mem.r16s(a5 - 40) == -1                          # all put back
    assert mem.r16s(a5 - 42) == -1                          # the flags D0's
    assert mem.r32(a5 - 46) == 0x1234                       # the second patch's answer,
    assert mem.r16s(a5 - 48) == -1                          # all as they were
    assert proc.runner.unimplemented_calls == {}


# -- AEvents: the Apple Event Manager ---------------------------------------------
def test_apple_events_reach_their_handlers(harness):
    """AEvents says in its SIZE resource that it takes high-level events,
    so it's sent Open Application when it starts.  It opens a PPC port,
    looks at the event with GetSpecificHighLevelEvent and then has
    AEProcessAppleEvent call its handler, which sends itself an event
    with a list in it, waiting for the reply, and then Quit."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "AEvents.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="AEvents to quit")
    assert not h.d.system_errors
    assert proc.runner.unimplemented_calls == {}
    assert mem.r16s(a5 - 2) == 0                    # every call that should work did
    assert mem.r16(a5 - 4) == 1                     # the filter saw one event
    assert mem.r16(a5 - 6) == 0                     # and turned it down
    assert mem.read(a5 - 14, 8) == b"aevtoapp"      # Open Application
    assert mem.read(a5 - 18, 4) == b"OAPP"          # its handler's refcon
    assert mem.r16s(a5 - 20) == 3                   # from another process: kAELocalProcess
    assert mem.r16s(a5 - 22) == -1701               # no 'miss': errAEDescNotFound
    assert mem.r32(a5 - 32) == 2                    # the list's items
    assert mem.r32(a5 - 36) == 56                   # "56" as a long
    assert mem.r16s(a5 - 38) == 1234                # 1234 as a short
    assert mem.r32s(a5 - 26) == 1290                # the reply's 'rslt'
    assert mem.r16s(a5 - 28) == -1234               # and its 'errn', the handler's result
    assert mem.r16(a5 - 44) == 1                    # Quit came
    assert mem.read(a5 - 52, 4) == b"aevt"          # as the current event
    port = mem.read(a5 - 202, 72)                   # the Process Manager's name for its port
    assert port[:10] == b"\0\0\x07AEvents" and port[36:46] == b"\0\x01MACRep01"
    assert (mem.r32(a5 - 266), mem.r32(a5 - 262)) == (0, proc.serial_number)    # and back
    assert mem.r16s(a5 - 126) > 0                   # PPCOpen's port
    assert mem.r16s(a5 - 128) == -910               # opened again: portNameExistsErr
    assert mem.r16s(a5 - 130) == -903               # closed again: noPortErr


def test_opened_with_a_document_it_is_sent_open_documents(harness, tmp_path, monkeypatch):
    """Opened with a document, AEvents is sent Open Documents instead,
    whose direct object is a list of the documents' FSSpecs."""
    from macrt.m68k import glue_aevents as ae
    from macrt.macfile import open_disks
    sent = []
    post = ae.post
    monkeypatch.setattr(ae, "post", lambda t, c: (sent.append(c), post(t, c)))
    f = open_application(APPS / "AEvents.bin")
    v = machfs.Volume()
    v.name = "Work"
    app = machfs.File()
    app.type, app.creator, app.rsrc = f.type, f.creator, f.rsrc
    v["AEvents"] = app
    v["Letters"] = machfs.Folder()
    doc = machfs.File()
    doc.type, doc.creator, doc.data = b"TEXT", f.creator, b"Dear Sir"
    v["Letters"]["Dear Sir"] = doc
    image = tmp_path / "work.img"
    image.write_bytes(v.write(800 * 1024, desktopdb=False, bootable=False))
    apps, disks = open_disks([image], ["Dear Sir"])
    h = harness(disks=disks)
    h.launch(Recorder("Other"))
    proc = h.d.launch(apps[0].name, None, app=apps[0])
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: mem.r16(a5 - 4) == 1, timeout=10, what="AEvents's filter")
    assert mem.read(a5 - 14, 8) == b"aevtodoc"
    node = proc.system.files.node_for(apps[0].documents[0])
    spec = ae.fss(node.volume.vref, node.parent.cnid, "Dear Sir")
    assert len(spec) == 70 and spec[6:15] == b"\x08Dear Sir"
    direct = next(data for key, _, data in sent[0].items if key == ae.keyDirectObject)
    assert direct == ae.Composite(ae.typeAEList, [[ae.NO_KEY, ae.typeFSS, spec]]).flatten()


# -- Menus: an application's own menu bar definition procedure ------------------
MBDF_LOG, MBDF_SAVED, CHOICE = -300, -310, -238      # menus.s's globals, below A5


def mbdf_log(proc):
    """What Menus's MBDF has been sent: (message, param1, param2)."""
    mem, log = proc.system.mem, proc.partition.a5 + MBDF_LOG
    return [(mem.r16(log + 2 + 8 * i), mem.r16(log + 4 + 8 * i), mem.r32(log + 6 + 8 * i))
            for i in range(mem.r16(log))]


@pytest.mark.parametrize("how", ["drag", "click"])
def test_an_applications_mbdf_hears_each_menu_pulled_down(harness, how):
    """InitProcMenu installs Menus's MBDF.  As File is pulled down the
    MBDF is asked where it goes and told to save what's under it, and it
    enables an item there, which can then be chosen; when the menu's
    gone it's told to restore the screen.  A quick click leaves the menu
    open, as it does without an MBDF."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "Menus.bin")
    proc = h.d.launch(f.name, None, app=f)
    h.wait_until(lambda: proc.windows and proc.windows[0].visible, timeout=10,
                 what="Menus's window")
    mem, a5 = proc.system.mem, proc.partition.a5
    assert mbdf_log(proc) == [(k.mbInit, 0, 0)]
    assert proc.menus.mbdf_id == 128
    title = h.menu_title_point(h.d.default_bar(proc), "File")
    if how == "drag":
        h.press(*title)
    else:
        h.click(*title)
    h.wait_until(lambda: QApplication.activePopupWidget() is not None, what="pulldown")
    pd = QApplication.activePopupWidget()
    assert pd.menu.selectable(2)                    # (Later, enabled by the MBDF
    log = mbdf_log(proc)
    assert log[:2] == [(k.mbInit, 0, 0), (k.mbRect, 0, 6)]       # File's entry is at 6
    assert log[2][:2] == (k.mbSave, 6) and len(log) == 3
    top_left = h.d.mac_from_global(pd.geometry().topLeft())
    assert mem.r_rect(a5 + MBDF_SAVED) == Rect(
        top_left.v, top_left.h, top_left.v + pd.height() - theme.MENU_SHADOW,
        top_left.h + pd.width() - theme.MENU_SHADOW)
    x, y = h.item_point(2)
    if how == "drag":
        h.move(x, y)
        h.release(x, y)
    else:
        assert h.d.tracker.sticky
        h.click(x, y)
    h.wait_until(lambda: mem.r32(a5 + CHOICE) == 129 << 16 | 2, what="File > Later")
    assert mbdf_log(proc)[3:] == [(k.mbRestore, 0, 0)]
    assert proc.runner.unimplemented_calls == {}


# -- Frames: an application's own WDEF -------------------------------------------
WDEF_LOG = -700             # wdef.s's, below A5
FRAMES_CLICKS = -236
FRAMES_AT_NEW, FRAMES_AT_MOVE = -238, -240     # its WDEF's messages so far, then


def wdef_log(proc):
    """What Frames's WDEF has been sent: (message, param)."""
    mem, log = proc.system.mem, proc.partition.a5 + WDEF_LOG
    return [(mem.r16(log + 2 + 6 * i), mem.r32(log + 4 + 6 * i)) for i in range(mem.r16(log))]


def launch_frames(h):
    h.launch(Recorder("Other"))
    f = open_application(APPS / "Frames.bin")
    proc = h.d.launch(f.name, None, app=f)
    h.wait_until(lambda: proc.windows and proc.windows[0].frame_image is not None
                 and h.d.views[proc.windows[0]].isVisible(), timeout=10,
                 what="Frames's window, shaped and drawn by its WDEF")
    rec = proc.windows[0]
    h.wait_until(lambda: rec.hilited and striped(rec), what="its frame drawn active")
    return proc, rec


def striped(rec):
    """Does the frame its WDEF drew have an active window's title bar?"""
    f, _, (w, _) = frames_geometry(rec)
    img = rec.frame_image[2]
    return all(img.pixel(w // 2 + 40, y) & 0xFFFFFF == 0 for y in (4, 6))


def frames_geometry(rec):
    """The host's frame for a documentProc window, which WDEF 0 gives
    Frames's, and its structure's top left."""
    f = chrome.Frame.for_window(rec, definition=describe_proc(k.documentProc))
    l, t, _, _ = chrome.insets(f)
    c = rec.content
    return f, Point(c.top - t, c.left - l), chrome.structure_size(f, c.width, c.height)


def test_an_applications_wdef_shapes_and_draws_its_window(harness):
    """Frames's WDEF is told its window is new, then asked for its regions
    and to draw it, before GetNewWindow returns; it leaves those to WDEF 0,
    as Excel's do.  A MovePortTo marks the frame to be done again, at the
    end of the next call of the Window Manager's that macrt does it at, not
    in the BlockMove after it: Excel 2.2a's WDEF, p-code, was run inside a
    BlockMove its interpreter made, and left it returning to nowhere.  The
    window on the host takes the structure region's shape and shows what
    was drawn, and a click in it is where the WDEF says it is."""
    h = harness()
    proc, rec = launch_frames(h)
    mem = proc.system.mem
    assert wdef_log(proc)[:3] == [(k.wNew, 0), (k.wCalcRgns, 0), (k.wDraw, 0)]
    a5 = proc.partition.a5
    h.wait_until(lambda: mem.r16(a5 + FRAMES_AT_MOVE), what="the BlockMove")
    assert (mem.r16(a5 + FRAMES_AT_NEW), mem.r16(a5 + FRAMES_AT_MOVE)) == (3, 3)
    assert rec.own_wdef and rec.wdef_shaped
    f, o, (w, ht) = frames_geometry(rec)
    s = chrome.shadow(f)
    struc = qdraw.read_rgn(mem, rec.struc_rgn)
    assert struc.bbox == Rect(o.v, o.h, o.v + ht + s, o.h + w + s)
    assert qdraw.read_rgn(mem, rec.cont_rgn) == qdraw.Region.rect(rec.content)
    view = h.d.views[rec]
    assert h.mac(view, QPoint(0, 0)) == (o.h, o.v)
    assert (view.width(), view.height()) == (w + s, ht + s)
    assert not view.mask().contains(QPoint(w, 0))       # the shadow's offset
    assert view.bar is None
    left, top, img = rec.frame_image
    assert (left, top) == (o.h - rec.content.left, o.v - rec.content.top)
    assert img.pixel(0, 0) & 0xFFFFFF == 0              # the frame
    h.click(rec.content.left + 10, rec.content.top + 10)
    h.wait_until(lambda: mem.r16(proc.partition.a5 + FRAMES_CLICKS) == 1, what="a click")
    assert (k.wHit, (rec.content.top + 10) << 16 | rec.content.left + 10) in wdef_log(proc)
    assert proc.runner.unimplemented_calls == {}


def test_an_applications_wdef_window_drags_and_closes(harness):
    """Its WDEF says where its title bar and close box are: the window
    drags by the one, with its regions, and the other is highlighted by
    the WDEF as it's clicked, then the window goes, telling its WDEF."""
    h = harness()
    proc, rec = launch_frames(h)
    mem = proc.system.mem
    before, struc = rec.content, qdraw.read_rgn(mem, rec.struc_rgn)
    f, o, (w, _) = frames_geometry(rec)
    th, tv = o.h + w // 2 + 40, o.v + 5
    h.press(th, tv)
    h.wait_until(lambda: h.d.tracker is not None, what="the drag")
    h.move(th + 30, tv + 20)
    h.move(th + 60, tv + 40)
    h.release(th + 60, tv + 40)
    h.wait_until(lambda: rec.content == before.offset(60, 40), what="the window dragged")
    h.wait_until(lambda: qdraw.read_rgn(mem, rec.struc_rgn) == struc.offset(60, 40),
                 what="its structure region with it")
    assert h.mac(h.d.views[rec], QPoint(0, 0)) == (o.h + 60, o.v + 40)
    f, o, (w, _) = frames_geometry(rec)
    box = chrome.close_box_rect(f, w).center()
    h.click(o.h + box.x(), o.v + box.y())
    h.wait_until(lambda: not proc.windows, what="the window closed")
    log = wdef_log(proc)
    assert log.count((k.wDraw, k.wInGoAway)) == 2       # the box highlighted and back
    assert log[-1] == (k.wDispose, 0)
    assert rec not in h.d.views
    assert proc.runner.unimplemented_calls == {}


# -- Edit: TextEdit and the scrap ------------------------------------------------
TEH = -238                  # edit.s's edit record, below A5
TOP, LEFT = 4, 4            # its destRect's top left, local


def edit_record(proc):
    mem = proc.system.mem
    return te.TERec(mem, mem.r32(mem.r32(proc.partition.a5 + TEH)))


def edit_text(proc):
    rec = edit_record(proc)
    mem = proc.system.mem
    return mem.read(mem.r32(rec.h_text), rec.te_length)


def launch_edit(h):
    f = open_application(APPS / "Edit.bin")
    h.launch(Recorder("Other"))
    proc = h.d.launch(f.name, None, app=f)
    h.wait_until(lambda: proc.windows and proc.windows[0].visible, timeout=10,
                 what="Edit's window")
    h.wait_until(lambda: proc.system.mem.r32(proc.partition.a5 + TEH)
                 and edit_record(proc).active, what="Edit's record to be active")
    return proc, proc.windows[0]


def typed(h, proc, text, expect):
    QTest.keyClicks(h.d.screen_widget, text)
    h.wait_until(lambda: edit_text(proc) == expect, what=f"{expect!r} in the record")


@pytest.fixture
def clipboard(qapp):
    cb = QApplication.clipboard()
    cb.clear()
    yield cb
    cb.clear()


def test_edit_types_into_its_record(harness, clipboard):
    h = harness()
    proc, rec = launch_edit(h)
    typed(h, proc, "Hello world", b"Hello world")
    r = edit_record(proc)
    assert (r.sel_start, r.sel_end, r.n_lines) == (11, 11, 1)
    width = QFontMetrics(qdraw.Fonts(QFont()).font(4, 9, 0)).horizontalAdvance
    line = range(TOP, TOP + r.line_height)
    h.wait_until(lambda: any(black(rec, x, y) for x in range(LEFT, LEFT + width("H"))
                             for y in line), what="the text drawn")
    assert not any(black(rec, x, y) for x in range(LEFT + width("Hello world") + 2, 150)
                   for y in line)

    h.key(Qt.Key.Key_Backspace)
    h.key(Qt.Key.Key_Left)
    h.key(Qt.Key.Key_Left)
    typed(h, proc, "a", b"Hello woarl")
    h.key(Qt.Key.Key_Return)
    h.wait_until(lambda: edit_record(proc).n_lines == 2, what="a second line")
    assert edit_text(proc) == b"Hello woa\rrl"
    assert proc.runner.unimplemented_calls == {}


def test_edit_cuts_copies_and_pastes_through_the_host_clipboard(harness, clipboard):
    h = harness()
    proc, rec = launch_edit(h)
    typed(h, proc, "Hello world", b"Hello world")
    width = QFontMetrics(qdraw.Fonts(QFont()).font(4, 9, 0)).horizontalAdvance
    c, mid = rec.content, TOP + edit_record(proc).line_height // 2
    word = c.left + LEFT + (width("Hello ") + width("Hello wor")) // 2
    h.click(word, c.top + mid)
    h.click(word, c.top + mid)                         # a double-click selects the word
    h.wait_until(lambda: (edit_record(proc).sel_start, edit_record(proc).sel_end) == (6, 11),
                 what="the word selected")

    h.key(Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    h.wait_until(lambda: clipboard.text() == "world", what="the copy on the host clipboard")
    h.click(c.left + 200, c.top + mid)                 # past the end of the line
    h.wait_until(lambda: edit_record(proc).sel_start == 11, what="the insertion point")
    h.key(Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    h.wait_until(lambda: edit_text(proc) == b"Hello worldworld", what="the paste")

    clipboard.setText("café\nau lait")                 # from another host application
    h.key(Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    h.wait_until(lambda: edit_text(proc) == b"Hello worldworldcaf\x8e\rau lait",
                 what="the host's text pasted")
    assert edit_record(proc).n_lines == 2

    h.click(c.left + LEFT + 1, c.top + mid)            # select "Hello" and cut it
    h.click(c.left + LEFT + 1, c.top + mid)
    h.wait_until(lambda: edit_record(proc).sel_end == 5, what="Hello selected")
    h.key(Qt.Key.Key_X, Qt.KeyboardModifier.ControlModifier)
    h.wait_until(lambda: clipboard.text() == "Hello", what="the cut on the host clipboard")
    assert edit_text(proc) == b" worldworldcaf\x8e\rau lait"
    assert proc.runner.unimplemented_calls == {}


def test_edit_reads_the_scrap_again_when_it_comes_forward(harness, clipboard):
    """The resume event's convertClipboard: the scrap changed while Edit
    was in the background, so it brings it into TextEdit's scrap."""
    h = harness()
    proc, rec = launch_edit(h)
    other = next(p for p in h.d.processes if p is not proc)
    h.d.switch_to(other)
    h.wait_until(lambda: not edit_record(proc).active, what="Edit to be suspended")
    clipboard.setText("from the host")
    h.d.switch_to(proc)
    h.wait_until(lambda: te.te_scrap(proc.world, proc.system.mem) == b"from the host",
                 what="TextEdit's scrap")
    assert edit_text(proc) == b""
