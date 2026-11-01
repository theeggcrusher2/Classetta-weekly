"""MultiFinder's _OSDispatch calls, made for Hello (tests/apps/hello.s)
while it runs alongside a Python application."""

from contextlib import nullcontext
from types import SimpleNamespace

import machfs
import pytest
from guest import Guest, Processor
from test_dialogs import NoDisplay
from test_m68k_apps import launch_hello

from macrt.m68k import dispatch
from macrt.m68k.glue_files import FSMakeFSSpec
from macrt.m68k.glue_os import Gestalt, trap_address
from macrt.m68k.glue_process import (GetCurrentProcess, GetFrontProcess, GetNextProcess,
                                     GetProcessInformation, SameProcess, SetFrontProcess,
                                     TempDisposeHandle, TempFreeMem, TempHLock, TempHUnlock,
                                     TempMaxMem, TempNewHandle, TempTopMem, WakeUpProcess,
                                     kCurrentProcess, paramErr, procNotFound)
from macrt.macfile import VolumeFolder, load_volume
from macrt.resfork import Resource, build_fork
from macrt.toolbox.memmgr import memFullErr, memWZErr, noErr
from macrt.toolbox.system import RAM_SIZE

OSDISPATCH, UNIMPLEMENTED = 0xA88F, 0xA89F
gestaltTempMemSupport = 4


@pytest.fixture
def hello(harness):
    h = harness()
    proc, _ = launch_hello(h)
    t = proc.runner
    with proc.system.lock:
        scratch = proc.heap.new_ptr(128, clear=True)
    return h, proc, t, scratch


def psn(t, p):
    return t.mem.r32(p), t.mem.r32(p + 4)


def test_multifinder_is_there_but_hidden_from_get_trap_address(hello):
    _, _, t, _ = hello
    assert dispatch.implemented(OSDISPATCH)
    assert trap_address(t, OSDISPATCH) == trap_address(t, UNIMPLEMENTED)
    err, attrs = Gestalt(t, 0x6F732020)                 # 'os  '
    assert err == noErr and attrs & (1 << gestaltTempMemSupport)


def test_the_processes(hello):
    h, proc, t, p = hello
    other = next(q for q in h.d.launch_order if q is not proc)
    assert GetCurrentProcess(t, p) == noErr
    assert psn(t, p) == (0, proc.serial_number)
    assert GetFrontProcess(t, p + 8, -1) == noErr
    assert psn(t, p + 8) == (0, h.d.front_process.serial_number)

    seen = []
    t.mem.fill(p, 8)                                    # kNoProcess: start at the first
    while GetNextProcess(t, p) == noErr:
        seen.append(psn(t, p)[1])
    assert seen == [other.serial_number, proc.serial_number]
    assert psn(t, p) == (0, 0)

    t.mem.w32(p + 4, proc.serial_number)
    t.mem.w32(p + 12, kCurrentProcess)
    assert SameProcess(t, p, p + 8, p + 16) == noErr and t.mem.r8(p + 16) == 1
    t.mem.w32(p + 4, other.serial_number)
    assert SameProcess(t, p, p + 8, p + 16) == noErr and t.mem.r8(p + 16) == 0
    t.mem.w32(p + 4, 12345)
    assert GetProcessInformation(t, p, p + 16) == procNotFound


def test_process_information(hello):
    _, proc, t, p = hello
    info, name = p + 8, p + 80
    t.mem.w32(p + 4, kCurrentProcess)
    t.mem.w32(info, 56)                                 # no FSSpec
    t.mem.w32(info + 4, name)
    assert GetProcessInformation(t, p, info) == noErr
    assert t.mem.r_pstr(name) == b"Hello"
    assert psn(t, info + 8) == (0, proc.serial_number)
    assert t.mem.read(info + 16, 8) == b"APPLHELO"
    part = proc.partition
    assert (t.mem.r32(info + 28), t.mem.r32(info + 32)) == (part.start, part.end - part.start)
    assert 0 < t.mem.r32(info + 36) < part.end - part.start


def test_temporary_memory(hello):
    h, proc, t, p = hello
    free = TempFreeMem(t)
    th = TempNewHandle(t, 4000, p)
    heap = proc.system.temp_heap()
    assert th and t.mem.r16s(p) == noErr and heap.contains(th)
    assert TempFreeMem(t) <= free - 4000
    TempHLock(t, th, p)
    assert t.mem.r16s(p) == noErr and heap.get_state(th) & 0x80
    TempHLock(t, proc.heap.new_handle(4), p)            # not a temporary one
    assert t.mem.r16s(p) == memWZErr
    TempDisposeHandle(t, th, p)
    assert t.mem.r16s(p) == noErr and not t.temp_handles

    # What the application leaves allocated goes when it quits.
    left = TempNewHandle(t, 4000, p)
    assert t.temp_handles == {left}
    proc.kill()
    h.wait_until(lambda: proc not in h.d.processes, what="Hello to quit")
    proc.thread.join(2)
    assert not t.temp_handles and TempFreeMem(t) >= free - 256     # less its master pointers


@pytest.mark.parametrize("selector", [0x40, 0x41])
def test_the_two_unnamed_selectors_around_opening_a_resource_file(qapp, selector):
    m = Processor()
    m.cpu.push16(0xFFFF)                                # the result
    m.cpu.push16(selector)
    m.run(b"\xa8\x8f")
    assert not m.missing
    assert m.cpu.sp == m.stack_top - 2 and m.mem.r16(m.cpu.sp) == noErr


def test_more_temporary_memory_than_there_is(qapp):
    g = Guest()
    g.temp_handles = set()
    err, grow = g.ptr(b"\xff\xff"), g.ptr(b"\xff" * 4)
    most = TempMaxMem(g, grow)
    assert most == g.system.temp_heap().max_block() and g.mem.r32(grow) == 0
    for size in (most + 1, -1):
        g.mem.w16(err, 0)
        assert TempNewHandle(g, size, err) == 0
        assert g.mem.r16s(err) == memFullErr and not g.temp_handles
    assert TempTopMem(g) == RAM_SIZE

    assert TempNewHandle(g, 0, err) and g.mem.r16s(err) == noErr     # an empty one
    g.mem.w16(err, -1)
    th = TempNewHandle(g, 100, err)
    heap = g.system.temp_heap()
    assert g.mem.r16s(err) == noErr and len(g.temp_handles) == 2
    TempHLock(g, th | 0xFF000000, err)                  # its high byte is no part of it
    assert g.mem.r16s(err) == noErr and heap.get_state(th) & 0x80
    TempHUnlock(g, th, err)
    assert g.mem.r16s(err) == noErr and not heap.get_state(th) & 0x80
    mine = g.heap.new_handle(4)
    g.heap.lock(mine)
    TempHUnlock(g, mine, err)                           # not a temporary one
    assert g.mem.r16s(err) == memWZErr and g.heap.get_state(mine) & 0x80


def size(flags: int) -> bytes:
    return flags.to_bytes(2, "big") + bytes(8)


def guests(*resources):
    """A Guest and another application alongside it, the Guest in front,
    on a display that brings forward what it's asked to."""
    g = Guest(resources)
    other = Guest(sharing=g).process
    other.app = None                                    # a Python application
    brought = []
    d = SimpleNamespace(launch_order=[other, g.process], front_process=g.process,
                        bridge=NoDisplay.bridge, switch_to=brought.append)
    g.tb._d = d
    g.yielding = nullcontext
    return g, other, brought


def test_naming_a_process_that_isnt_there(qapp):
    g, other, _ = guests()
    p = g.ptr(bytes(24))
    for high, low in ((1, g.process.serial_number), (0, 12345)):
        g.mem.w32(p, high)
        g.mem.w32(p + 4, low)
        assert GetNextProcess(g, p) == paramErr
        assert SameProcess(g, p, p, p + 16) == paramErr
        g.mem.write(p + 8, bytes(8))
        g.mem.w32(p + 12, kCurrentProcess)
        assert SameProcess(g, p + 8, p, p + 16) == paramErr
        assert GetProcessInformation(g, p, g.ptr(bytes(60))) == procNotFound
        assert SetFrontProcess(g, p) == procNotFound
        assert WakeUpProcess(g, p) == procNotFound
    g.mem.w32(p, 0)
    g.mem.w32(p + 4, other.serial_number)
    assert GetNextProcess(g, p) == noErr
    assert psn(g, p) == (0, g.process.serial_number)
    g.mem.write(p, b"\xff" * 8)
    assert GetCurrentProcess(g, p) == noErr and psn(g, p) == (0, g.process.serial_number)
    g.tb._d.front_process = None
    g.mem.write(p, b"\xff" * 8)
    assert GetFrontProcess(g, p, -1) == procNotFound and psn(g, p) == (0, 0)

    # One that isn't among those launched: no next one to it.
    g.tb._d.launch_order.remove(g.process)
    g.mem.write(p, bytes(8))
    g.mem.w32(p + 4, kCurrentProcess)
    assert GetNextProcess(g, p) == paramErr


def test_bringing_a_process_forward_and_waking_it(qapp, monkeypatch):
    g, other, brought = guests()
    p = g.ptr(bytes(8))
    g.mem.w32(p + 4, other.serial_number)
    woken = []
    monkeypatch.setattr(other.events, "wake", lambda: woken.append(other))
    assert WakeUpProcess(g, p) == noErr and woken == [other]
    assert SetFrontProcess(g, p) == noErr and brought == [other]


@pytest.mark.parametrize("sizes, mode", [
    (((-1, size(0x5880)),), 0x5880),
    (((-1, size(0x5880)), (0, size(0x1000))), 0x1000),     # the one the user changed
    (((-1, b"\x58\x80"),), 0x5880),
    (((-1, b"\x58"),), 0),
    ((), 0),
])
def test_the_mode_is_the_size_resources_flags(qapp, sizes, mode):
    g, _, _ = guests(*(Resource(b"SIZE", n, data) for n, data in sizes))
    p, info = g.ptr(bytes(8)), g.ptr(bytes(60))
    g.mem.w32(p + 4, kCurrentProcess)
    assert GetProcessInformation(g, p, info) == noErr
    assert g.mem.r32(info + 24) == mode


def test_an_application_with_no_resource_fork_has_no_mode(qapp):
    g, _, _ = guests()
    g.process.app = SimpleNamespace(rsrc=b"", creator=b"WHAT", name="Forkless")
    p, info = g.ptr(bytes(8)), g.ptr(bytes(60))
    g.mem.w32(p + 4, kCurrentProcess)
    assert GetProcessInformation(g, p, info) == noErr
    assert g.mem.read(info + 16, 8) == b"APPLWHAT" and g.mem.r32(info + 24) == 0


def test_a_process_named_in_its_fsspec(qapp):
    g, other, _ = guests()
    other.name = "A Python application named at rather greater length than a Str63 holds"
    p, info, spec = g.ptr(bytes(8)), g.ptr(bytes(60)), g.ptr(b"\xee" * 80)
    g.mem.w32(info, 60)
    g.mem.w32(info + 56, spec | 0xFF000000)
    g.mem.w32(p + 4, kCurrentProcess)
    assert GetProcessInformation(g, p, info) == noErr
    node = g.system.files.node_for(g.process.app)
    assert g.mem.r16s(spec) == node.volume.vref and g.mem.r32(spec + 2) == node.parid
    assert g.mem.r_pstr(spec + 6) == b"Test App"

    # A Python application has no file: it's named, on no volume.
    g.mem.w32(p + 4, other.serial_number)
    g.mem.w32(info + 4, name := g.ptr(b"\xee" * 40))
    assert GetProcessInformation(g, p, info) == noErr
    assert g.mem.read(info + 16, 8) == b"APPL????" and g.mem.r32(info + 24) == 0
    assert g.mem.r_pstr(name) == other.name[:31].encode()
    assert g.mem.r_pstr(spec + 6) == other.name[:63].encode()
    assert g.mem.read(spec + 70, 10) == b"\xee" * 10     # past the Str63


def test_the_fsspec_is_where_the_applications_file_is(qapp):
    """Play-Doh Creations makes an FSSpec of "Play-Doh Creations
    CD-ROM:PDAPP" and checks it's on the volume its own FSSpec names."""
    v = machfs.Volume()
    v.name = "Disc"
    v["Game"] = machfs.Folder()
    f = machfs.File()
    f.type, f.creator, f.rsrc = b"APPL", b"GAME", build_fork([])
    v["Game"]["Play"] = f
    g, _, _ = guests()
    disc = load_volume(v.write(800 * 1024, desktopdb=False, bootable=False))
    g.process.app = VolumeFolder(disc, ("Game",)).find("Play")
    p, info, spec = g.ptr(bytes(8)), g.ptr(bytes(60)), g.ptr(bytes(70))
    g.mem.w32(info, 60)
    g.mem.w32(info + 56, spec)
    g.mem.w32(p + 4, kCurrentProcess)
    assert GetProcessInformation(g, p, info) == noErr
    vol = g.system.files.volume_named("Disc")
    assert g.mem.r16s(spec) == vol.vref and g.mem.r32(spec + 2) == vol.root.child("Game").cnid
    assert g.mem.r_pstr(spec + 6) == b"Play"
    found = g.ptr(bytes(70))
    assert FSMakeFSSpec(g, 0, 0, g.ptr(b"\x0eDisc:Game:Play"), found) == noErr
    assert g.mem.read(found, 70) == g.mem.read(spec, 70)
