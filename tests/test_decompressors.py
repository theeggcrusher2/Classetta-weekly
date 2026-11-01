"""Compressed resources rsrcfork can't expand, which the Resource Manager
expands by running the 'dcmp' their header names (System 7.5's utilities),
the System version the disk's System file gives, and the other resources
macrt's System takes from it."""

import struct
import threading

import pytest
from guest import Processor

from macrt.m68k import decompressors, dispatch, segload
from macrt.macfile import MacFile, load_volume, system_on
from macrt.resfork import Resource, build_fork, open_fork, resCompressed, resPurgeable
from macrt.toolbox import resources as rm
from macrt.toolbox.system import LAST_SYSTEM_7, SYSTEM_VERSION, System, system_version

from builders import mfs_image

# Two 'dcmp's that double each byte of the data: a version 9 one, which
# starts at the offset its second word gives, and a version 8 one, with
# its type in bytes 4 to 7, which starts at its first.
DOUBLE_9 = bytes.fromhex(
    "0000 0008 0000 0000"
    "206F000C 226F0008 246F0004"        # source, dest, header
    "202A0008 E288"                     # the header's size, halved
    "6006 1218 12C1 12C1 5380 6AF6"     # each byte, twice
    "205F 4FEF000C 4ED0")               # pop 12 and return
DOUBLE_8 = bytes.fromhex(
    "6006 0000 64636D70"
    "206F0010 226F000C 202F0004"        # source, dest, length
    "6006 1218 12C1 12C1 5380 6AF6"
    "205F 4FEF0010 4ED0")               # pop 16 and return
DATA = b"Note Pad!"


def compressed(version: int, dcmp_id: int, data: bytes = DATA) -> bytes:
    size = 2 * len(data)
    if version == 9:
        rest = struct.pack(">hHxx", dcmp_id, 0)
    else:
        rest = struct.pack(">BBhxx", 0x80, 0, dcmp_id)
    return b"\xa8\x9f\x65\x72" + struct.pack(">HBBI", 18, version, 1, size) + rest + data


class Runner(Processor):
    """A Processor whose calls to 68k code run it, as the runtime's do,
    with the Resource Manager's decompressor the runtime gives it."""

    expanding = False

    def __init__(self, resources=(), **system):
        super().__init__(resources, **system)
        self.stop = self.ptr(bytes(4))
        self.thread_id = threading.get_ident()
        self.process.resources.decompressor = lambda data: decompressors.expand(self, data)

    def call(self, addr, args=(), result=""):
        cpu = self.cpu
        sp = cpu.sp
        for code, value in args:
            (cpu.push32 if dispatch.SIZES[code] == 4 else cpu.push16)(value)
        cpu.push32(self.stop)
        cpu.pc = addr
        while (ev := cpu.run(self.stop)).kind == "trap":
            auto_pop = dispatch.is_toolbox(ev.code) and ev.code & 0x400
            cpu.pc = cpu.pop32() if auto_pop else ev.pc + 2
            dispatch.dispatch(self, ev.code, ev.pc)
        assert ev.kind == "until", ev
        cpu.sp = sp                     # with what the routine left, as the runtime does
        return 0


def runner(*resources) -> tuple[Runner, rm.ResourceManager]:
    g = Runner(list(resources))
    return g, g.process.resources


def test_a_version_9_resource_is_expanded_by_its_dcmp(qapp):
    g, res = runner(Resource(b"dcmp", 200, DOUBLE_9),
                    Resource(b"TEXT", 128, compressed(9, 200), None, resCompressed))
    h = res.GetResource("TEXT", 128)
    assert res.ResError() == rm.noErr and g.contents(h) == bytes(b for b in DATA for _ in "xx")
    assert res.SizeResource(h) == 2 * len(DATA)
    assert g.cpu.sp == g.stack_top


def test_a_version_8_resource_is_expanded_by_its_dcmp(qapp):
    g, res = runner(Resource(b"dcmp", 201, DOUBLE_8),
                    Resource(b"TEXT", 128, compressed(8, 201), None, resCompressed))
    h = res.GetResource("TEXT", 128)
    assert g.contents(h) == bytes(b for b in DATA for _ in "xx")


def test_an_expanded_resource_is_kept(qapp):
    """Its 'dcmp' runs once: read again, as a purged resource is, it's the
    same."""
    g, res = runner(Resource(b"dcmp", 200, DOUBLE_9),
                    Resource(b"TEXT", 128, compressed(9, 200), None, resCompressed),
                    Resource(b"TEXT", 129, compressed(9, 200), None, resCompressed))
    calls = []
    run = g.call
    g.call = lambda *a: calls.append(a[0]) or run(*a)
    h = res.GetResource("TEXT", 128)
    res.ReleaseResource(h)
    again = res.GetResource("TEXT", 128)
    assert g.contents(again) == bytes(b for b in DATA for _ in "xx") and len(calls) == 1
    assert g.contents(res.GetResource("TEXT", 129)) == g.contents(again) and len(calls) == 1


def test_a_purgeable_dcmp_is_locked_before_its_buffers_are_made(qapp):
    """Made with a purgeable 'dcmp' unlocked, the buffers it runs with
    could purge it, and leave it to run from address 0."""
    data = bytes(range(256)) * 8
    g, res = runner(Resource(b"dcmp", 200, DOUBLE_9, None, resPurgeable),
                    Resource(b"TEXT", 128, compressed(9, 200, data), None, resCompressed))
    heap = g.process.heap
    d = res.GetResource("dcmp", 200)
    state = heap.get_state(d)
    heap.grow(heap.limit)
    full = []
    while heap.max_block(grow=False) >= 64:
        full.append(heap.new_ptr(min(heap.max_block(grow=False) - 16, 0x10000)))
        assert full[-1]
    heap.dispose_ptr(full.pop())            # room for the buffers only without the dcmp
    need = len(compressed(9, 200, data)) + 2 * len(data)
    assert heap.new_ptr(heap.max_block(grow=False) - need + len(DOUBLE_9) // 2 - 16)
    h = res.GetResource("TEXT", 128)
    assert g.contents(h) == bytes(b for b in data for _ in "xx")
    assert g.mem.r32(d) and heap.get_state(d) == state


def test_without_its_dcmp_a_resource_wont_expand(qapp):
    g, res = runner(Resource(b"TEXT", 128, compressed(9, 200), None, resCompressed))
    assert res.GetResource("TEXT", 128) == 0 and res.ResError() == rm.CantDecompress


def test_a_dcmp_past_the_end_of_its_fork_isnt_run(qapp):
    g, res = runner()
    fork = bytearray(build_fork([Resource(b"dcmp", 200, DOUBLE_9)]))
    at = struct.unpack_from(">I", fork)[0] + open_fork(bytes(fork)).find(b"dcmp", 200).offset
    fork[at:at + 4] = struct.pack(">I", 0x7FFFFF)
    g.call = None                       # not to be called
    assert decompressors.expand(g, compressed(9, 200), bytes(fork)) is None


def test_a_dcmp_isnt_run_from_another_thread(qapp):
    """Only the application's own thread runs its 68k code."""
    g, res = runner(Resource(b"dcmp", 200, DOUBLE_9))
    g.call = None                       # not to be called
    g.thread_id = None
    assert decompressors.expand(g, compressed(9, 200)) is None
    g.thread_id, g.expanding = threading.get_ident(), True
    assert decompressors.expand(g, compressed(9, 200)) is None     # nor while one runs


def test_the_disks_system_brings_its_decompressors():
    """System 7's resources are compressed with the 'dcmp's in its System
    file, which macrt's System gets from the disk's."""
    disk = MacFile("System", type=b"zsys", rsrc=build_fork([
        Resource(b"dcmp", 3, DOUBLE_9),
        Resource(b"dcmp", 4, compressed(9, 3), None, resCompressed),   # not one to run
    ]))
    m = System(disk_system=disk).sys_map
    assert [e.id for e in m.entries(b"dcmp")] == [3]
    real = MacFile("System", rsrc=build_fork([Resource(b"FONT", 3 << 7 | 9, b"Geneva 9")]))
    assert next(System(system_file=real, disk_system=disk).sys_map.entries(b"dcmp"), None) is None


def test_the_disks_system_brings_its_folder_names_and_control_panels_resources():
    """'fld#' 0, the 'itl2's, and what control panels keep in the System
    file with the IDs they own (Extensions Manager's sets), but not the
    System's own resources in that range."""
    disk = MacFile("System", type=b"zsys", rsrc=build_fork([
        Resource(b"fld#", 0, b"folders"), Resource(b"ESet", -4034, b"sets", b"EM Sets"),
        Resource(b"DLOG", -4040, b"a dialog"), Resource(b"ESet", -4000, b"not a panel's"),
        Resource(b"itl2", 0, b"sorting", b"English"), Resource(b"itl2", 1, b"tri", b"French"),
    ]))
    m = System(disk_system=disk).sys_map
    kinds = (b"fld#", b"ESet", b"DLOG", b"itl2")
    assert [(e.type, e.id) for kind in kinds for e in m.entries(kind)] == \
        [(b"fld#", 0), (b"ESet", -4034), (b"itl2", 0), (b"itl2", 1)]
    assert m.find(b"ESet", -4034).name == b"EM Sets"
    assert m.find(b"itl2", 1).name == b"French"


def vers(major: int, minor: int) -> bytes:
    return bytes([major, minor, 0x80, 0, 0, 0]) + b"\x05" + b"7.5.3" + b"\x00"


def test_the_system_version_is_the_system_files():
    """System 7.5's utilities look for 7.5; macrt says it's at least 7.0.1
    and no later than the last System 7."""
    def system(*resources):
        return MacFile("System", type=b"zsys", rsrc=build_fork(list(resources)))
    assert system_version(None) == SYSTEM_VERSION == 0x0701
    assert system_version(system()) == SYSTEM_VERSION
    assert system_version(system(Resource(b"vers", 1, vers(7, 0x53)))) == 0x0753
    assert system_version(system(Resource(b"vers", 1, vers(6, 0x08)))) == SYSTEM_VERSION
    assert system_version(system(Resource(b"vers", 1, vers(8, 0x10)))) == LAST_SYSTEM_7
    assert system_version(system(Resource(b"vers", 2, vers(7, 0x53)))) == SYSTEM_VERSION
    assert system_version(MacFile("System", rsrc=b"not a fork")) == SYSTEM_VERSION
    disk = system(Resource(b"vers", 1, vers(7, 0x53)))
    assert System(disk_system=disk).version == 0x0753
    assert System(system_file=system(), disk_system=disk).version == SYSTEM_VERSION


def test_a_system_7_disks_system_file_is_found():
    """System 7's System file is a 'zsys', earlier ones a 'ZSYS'."""
    for kind in (b"ZSYS", b"zsys"):
        vol = load_volume(mfs_image("Disk", [
            ("Notes", b"TEXT", b"ttxt", b"notes", b"", 0, (0, 0)),
            ("System", kind, b"MACS", b"", build_fork([]), 0, (0, 0)),
        ]))
        found = system_on(vol)
        assert found is not None and found.type == kind
    vol = load_volume(mfs_image("Disk", [("System", b"APPL", b"MACS", b"", b"", 0, (0, 0))]))
    assert system_on(vol) is None


def test_a_compressed_code_0_is_expanded(monkeypatch):
    """The Segment Loader reads CODE 0 before there's a 68k to run a
    'dcmp' on, so expands it as rsrcfork can (Script Editor's)."""
    entry = bytes.fromhex("0000 3F3C 0001 A9F0")
    code0 = bytes.fromhex("00000028 00000000 00000008 00000020") + entry

    def app(data):
        return MacFile("App", type=b"APPL", rsrc=build_fork([
            Resource(b"CODE", 0, data, b"", resCompressed)]))
    with pytest.raises(segload.SegmentError, match="compressed"):
        segload.code0(app(compressed(9, 200)))
    monkeypatch.setattr(segload, "decompress", lambda data: code0)
    layout = segload.code0(app(compressed(9, 0)))
    assert (layout.above_a5, layout.jt_offset, layout.jump_table) == (0x28, 0x20, entry)


def test_a_code_0_past_the_end_of_its_fork_cant_be_launched():
    fork = bytearray(build_fork([Resource(b"CODE", 0, bytes(24))]))
    data_at = struct.unpack_from(">I", fork)[0]
    at = data_at + open_fork(bytes(fork)).find(b"CODE", 0).offset
    fork[at:at + 4] = struct.pack(">I", 0x7FFFFF)        # its length, past the fork's end
    with pytest.raises(segload.SegmentError, match="can't be read"):
        segload.code0(MacFile("App", type=b"APPL", rsrc=bytes(fork)))
