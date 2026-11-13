"""The Resource Manager over maps in guest memory, purging, resource
templates, and the Toolbox calls built on them."""

import struct
from types import SimpleNamespace

import pytest
from guest import Guest

from macrt.macfile import MacFile, MemoryFolder
from macrt.memory import RAM
from macrt.process import Process
from macrt.resfork import (
    Resource, build_fork, mapReadOnly, read_fork, resLocked, resPreload, resProtected,
    resPurgeable, resSysHeap,
)
from macrt.toolbox import constants as k
from macrt.toolbox import lowmem
from macrt.toolbox import resources as rm
from macrt.toolbox import sysres
from macrt.toolbox.files import (
    CATALOG_FILE, EXTENTS_FILE, fsRdPerm, fsRdWrPerm, vcbXTRef)
from macrt.toolbox.events import EventQueue
from macrt.toolbox.memmgr import LOCKED, PURGEABLE, RESOURCE, Heap
from macrt.toolbox.menus import MENU_PROC, read_mbdf_id, read_menu_list
from macrt.toolbox.resources import ResourceMap
from macrt.toolbox.system import DEFAULT_STACK, MIN_HEAP, SIZE_STACK, System
from macrt.toolbox.templates import (
    alertPositionMainScreen, btnCtrl, centerMainScreen, ctrlItem, editText, iconItem,
    noAutoCenter, parse_alrt, parse_cntl, parse_ditl, parse_dlog, parse_mbar, parse_str_list,
    parse_wind, place, statText,
)
from macrt.toolbox.api import Toolbox
from macrt.toolbox.types import Rect
from macrt.toolbox.windows import WindowRecord

from builders import ditl_item, menu, pstr, size_res, wind
from conftest import Recorder
from test_dialogs import NoDisplay

SYSTEM_24 = SimpleNamespace(mmu_32_bit=False, addr_mask=0xFFFFFF)


APP_FORK = build_fork([
    Resource(b"STR ", 128, pstr("Hello"), b"Greeting", resPurgeable),
    Resource(b"STR ", 129, pstr("Bye")),
    Resource(b"STR#", 128, struct.pack(">h", 2) + pstr("one") + pstr("two")),
    Resource(b"WIND", 128, wind(Rect(60, 40, 220, 340), "From WIND", k.zoomDocProc)),
    Resource(b"WIND", 129, wind(Rect(0, 0, 100, 200), "Centered", k.documentProc,
                                position=centerMainScreen)),
    Resource(b"MENU", 129, menu(129, "File", [("New", "N"), ("Close", "W"), ("(-", ""),
                                              ("Quit", "Q")])),
    Resource(b"MENU", 130, menu(130, "Edit", [("(Undo", "Z"), ("Cut", "X"), ("Copy", "C")])),
    Resource(b"MBAR", 128, struct.pack(">3h", 2, 129, 130)),
    Resource(b"LOCK", 1, b"locked", None, resLocked),
    Resource(b"SYSH", 1, b"in the system heap", None, resSysHeap),
    Resource(b"PREL", 1, b"preloaded", None, resPreload),
    Resource(b"PROT", 1, b"protected", None, resProtected),
    Resource(b"SIZE", -1, size_res(640 * 1024, 384 * 1024)),
])

OTHER_FORK = build_fork([
    Resource(b"STR ", 128, pstr("Other hello")),
    Resource(b"STR ", 300, pstr("Only in other"), b"Other Name"),
])


def make(app_fork=APP_FORK, others=(), system=None, **kw):
    system = system or System()
    app = MacFile("Test App", rsrc=app_fork, type=b"APPL", creator=b"TEST")
    MemoryFolder([app, *others])                      # sets app.folder
    proc = Process(system, "Test App", lambda tb: None, EventQueue(lambda: (None, 0)), app=app,
                   **kw)
    return system, proc, proc.resources


def files_left_open(system):
    """The open files, less the volumes' own paths to their catalog trees."""
    return [n for n, p in system.fcbs.items() if p.node.cnid not in (EXTENTS_FILE, CATALOG_FILE)]


def fill(heap):
    """Take every free block."""
    taken = []
    while heap.max_block() > 0:
        taken.append(heap.new_ptr(heap.max_block()))
    return taken


def contents(system, h):
    heap = system.heap_of(h)
    return system.mem.read(heap.deref(h), heap.handle_size(h))


# -- purging ----------------------------------------------------------------
def test_purgeable_blocks_go_when_space_runs_out():
    heap = Heap(RAM(SYSTEM_24, 0x10000), 0x1000, 0x9000)
    keep = heap.new_handle(0x1000)
    gone = heap.new_handle(0x1000)
    pinned = heap.new_handle(0x1000)
    heap.set_purgeable(gone)
    heap.set_state(pinned, PURGEABLE | LOCKED)
    heap.new_ptr(heap.max_block())                  # nothing left at the end
    assert heap.new_ptr(0x800)                      # fits where gone was
    assert heap.is_empty(gone) and heap.is_handle(gone)
    assert not heap.is_empty(keep) and not heap.is_empty(pinned)
    assert not heap.realloc_handle(gone, 0x1000)    # the rest of the hole is too small
    assert heap.realloc_handle(gone, 0x400) and heap.handle_size(gone) == 0x400


def test_empty_and_realloc_handle():
    heap = Heap(RAM(SYSTEM_24, 0x10000), 0x1000, 0x9000)
    h = heap.new_handle(100)
    heap.empty_handle(h)
    assert heap.is_empty(h) and heap.handle_size(h) == 0
    assert heap.set_handle_size(h, 10) is False                 # nilHandleErr, as on a Mac
    assert heap.realloc_handle(h, 64) and heap.handle_size(h) == 64
    heap.lock(h)
    heap.empty_handle(h)
    assert not heap.is_empty(h)                                 # locked blocks stay


def test_purging_never_takes_the_block_being_grown():
    heap = Heap(RAM(SYSTEM_24, 0x10000), 0x1000, 0x5000)
    h = heap.new_handle(0x800)
    heap.set_purgeable(h)
    heap.new_ptr(heap.max_block() - 0x10)
    assert not heap.set_handle_size(h, 0x2000)
    assert not heap.is_empty(h)


# -- the system file ---------------------------------------------------------
def test_system_file_heads_every_chain():
    system, proc, res = make()
    sys_map = system.mem.r32(lowmem.SysMapHndl)
    assert system.sys_heap.contains(sys_map)
    assert system.mem.r16(lowmem.SysMap) == res.sys_refnum
    assert system.mem.r_pstr(lowmem.SysResName) == b"System"
    assert [m.refnum for m in res.maps()][-1] == res.sys_refnum
    assert list(res.maps())[-1].handle == sys_map


def test_defprocs_are_system_resources():
    system, proc, res = make()
    wdef = system.defproc("WDEF", 0)
    assert res.GetResource("WDEF", 0) == wdef
    assert res.HomeResFile(wdef) == res.sys_refnum
    assert res.GetResInfo(wdef)[:2] == (0, b"WDEF")
    assert system.defproc_id(wdef) == ("WDEF", 0)
    assert system.defproc_id(res.GetResource("STR ", 128)) == ("", 0)
    assert system.mem.r8(wdef) & (LOCKED | RESOURCE) == LOCKED | RESOURCE
    # One made on demand joins the system file too.
    odd = system.defproc("WDEF", 7)
    assert res.GetResource("WDEF", 7) == odd


def test_an_applications_own_wdef_and_mdef_come_first(qapp):
    fork = build_fork([Resource(b"WDEF", 128, b"\x4e\x75"), Resource(b"MDEF", 128, b"\x4e\x75"),
                       Resource(b"MENU", 129, menu(129, "Odd", [("Item", "")], mdef=128))])
    system, proc, res = make(fork)
    res.GetNamedResource("WDEF", "None")
    rec = WindowRecord.new(system, proc, proc.heap, 0, Rect(40, 40, 140, 240), "Custom", True,
                           128 * 16 + k.noGrowDocProc, True, 0)
    assert res.ResError() == rm.resNotFound                   # NewWindow leaves it alone
    wdp = rec.window_def_proc
    assert wdp & 0xFFFFFF == res.GetResource("WDEF", 128)
    assert res.HomeResFile(wdp & 0xFFFFFF) == res.CurResFile()
    assert wdp >> 24 == k.noGrowDocProc and rec.proc_id == 128 * 16 + k.noGrowDocProc
    assert system.sys_map.find(b"WDEF", 128) is None          # no stand-in needed
    menu_h = Toolbox(None, proc).GetMenu(129).handle
    mdef = system.mem.r32(system.heap_of(menu_h).deref(menu_h) + MENU_PROC)
    assert mdef == res.GetResource("MDEF", 128) and res.HomeResFile(mdef) == res.CurResFile()


def test_dispose_menu_releases_a_menu_thats_a_resource(qapp):
    """GetMenu then reads it afresh: the MacPaint demo deletes, disposes of
    and gets again its Goodies menu, and a handle the map still had came
    back, its master pointer by then another block's."""
    g = Guest([Resource(b"MENU", 129, menu(129, "Goodies", [("Grid", ""), ("Zoom", "")]))])
    g.tb._d = NoDisplay()
    first = g("GetMenu", 129)
    g("DisposeMenu", first)
    assert not g.heap.is_handle(first)
    other = g.handle(b"\0" * 64)               # may take its master pointer
    again = g("GetMenu", 129)
    assert again != other
    p = g.heap.deref(again)
    assert g.mem.r16(p) == 129 and g.mem.read(p + 14, 8) == b"\x07Goodies"
    made = g("NewMenu", 300, g.ptr(b"\x04Made"))
    g("DisposeMenu", made)                      # not a resource: disposed of
    assert not g.heap.is_handle(made)


def test_a_real_system_file_is_used():
    sysfork = build_fork([Resource(b"CURS", 4, b"watch" * 13, None, resSysHeap)])
    system = System(system_file=MacFile("System", rsrc=sysfork))
    proc = Process(system, "A", lambda tb: None, EventQueue(lambda: (None, 0)))
    h = proc.resources.GetResource("CURS", 4)
    assert system.sys_heap.contains(system.mem.r32(h))
    assert proc.resources.GetResource("MDEF", 0) == system.defproc("MDEF", 0)


def test_the_fonts_of_the_disks_system_file_join_macrts_own():
    """Programs drew in the fonts of the System they started up with: Mac
    Attack's tanks are characters of a font in its disk's System.  macrt's
    own System gets the families it hasn't got from the System on the
    disk the applications come from, and the strikes of its monospaced
    ones, to measure the host font standing in with; a real System file
    brings its own."""
    disk = MacFile("System", type=b"ZSYS", rsrc=build_fork([
        Resource(b"FONT", 225 << 7, b"", b"Tank"),
        Resource(b"FONT", 225 << 7 | 12, b"tanks"),
        Resource(b"FONT", 3 << 7 | 12, b"Geneva 12"),          # a host font stands in
        Resource(b"FONT", 4 << 7, b"", b"Monaco"),             # macrt's FOND names it
        Resource(b"FONT", 4 << 7 | 9, b"Monaco 9"),            # and is measured with it
        Resource(b"FOND", 4, sysres.fond(4, (9,)), b"Monaco"),
        Resource(b"FOND", 300, sysres.fond(300, (9,), (1000,)), b"Hi-Res"),
        Resource(b"NFNT", 1000, b"its 9"),
        Resource(b"NFNT", 1001, b"no family's"),
    ]))
    m = System(disk_system=disk).sys_map
    found = {(e.type, e.id) for kind in (b"FONT", b"FOND", b"NFNT") for e in m.entries(kind)}
    assert found - {(b"FOND", f) for f in sysres.FONTS} == {
        (b"FONT", 225 << 7), (b"FONT", 225 << 7 | 12), (b"FOND", 300), (b"NFNT", 1000),
        (b"FONT", 4 << 7 | 9)}
    assert m.find_named(b"FONT", b"Tank").id == 225 << 7
    real = MacFile("System", rsrc=build_fork([Resource(b"FONT", 3 << 7 | 9, b"Geneva 9")]))
    m = System(system_file=real, disk_system=disk).sys_map
    assert [e.id for e in m.entries(b"FONT")] == [3 << 7 | 9]


def test_the_drivers_of_the_disks_system_file_join_macrts_own():
    """A program opens a driver by name, which a Mac loads from the System
    file if it isn't in the unit table: Speller Bee speaks through .SAY
    in its disk's System, which loads its code from OVLY resources there.
    macrt's own System gets the disk System's drivers, but not its desk
    accessories, nor those macrt answers or hasn't the hardware for; and
    the resources of the types no Apple System has, which are theirs."""
    disk = MacFile("System", type=b"ZSYS", rsrc=build_fork([
        Resource(b"DRVR", 20, b"say", b".say"),
        Resource(b"DRVR", 9, b"AppleTalk", b".MPP"),
        Resource(b"DRVR", 2, b"printing", b".Print"),
        Resource(b"DRVR", 14, b"desk accessory", b"\x00Key Caps"),
        Resource(b"OVLY", 136, b"its code", b"init"),
        Resource(b"INIT", 31, b"code run at startup"),
    ]))
    m = System(disk_system=disk).sys_map
    assert {e.id: e.name for e in m.entries(b"DRVR")} == {2: b".Print", 20: b".say"}
    assert m.find(b"DRVR", 2).handle != System(disk_system=None).sys_map.find(b"DRVR", 2).handle
    assert m.find_named(b"OVLY", b"init").id == 136
    assert m.find(b"INIT", 31) is None
    alone = MacFile("System", type=b"ZSYS", rsrc=build_fork([Resource(b"OVLY", 1, b"x")]))
    assert System(disk_system=alone).sys_map.find(b"OVLY", 1) is None   # no driver for it
    real = MacFile("System", rsrc=build_fork([Resource(b"FONT", 3 << 7 | 9, b"Geneva 9")]))
    m = System(system_file=real, disk_system=disk).sys_map
    assert [e.id for e in m.entries(b"DRVR")] == [2] and m.find(b"OVLY", 136) is None


# -- the application's file -------------------------------------------------
def test_application_file_is_open_and_current():
    system, proc, res = make()
    refnum = res.CurResFile()
    assert refnum not in (0, res.sys_refnum) and proc.world.r16(lowmem.CurApRefNum) == refnum
    top = ResourceMap(system.mem, proc.world.r32(lowmem.TopMapHndl))
    assert top.refnum == refnum and top.next == system.mem.r32(lowmem.SysMapHndl)
    assert proc.heap.contains(top.handle)
    # The map in memory starts with a copy of the fork's header.
    assert system.mem.read(top.ptr, 16) == APP_FORK[:16]


def test_size_resource_sets_the_partition():
    system, proc, res = make()
    # What it asks for, and room for macrt's bigger stack.
    size = proc.partition.end - proc.partition.start
    assert size == 640 * 1024 - SIZE_STACK + DEFAULT_STACK
    # The heap is the one the SIZE leaves room for, with its 8K stack and
    # the A5 world: GNU Chess Pro halves its tables to fit that heap.
    appl_limit, stack_base = (proc.world.r32(a) for a in (lowmem.ApplLimit, lowmem.CurStackBase))
    assert stack_base - appl_limit - DEFAULT_STACK in range(4)
    heap, world = appl_limit - proc.partition.start, proc.partition.end - stack_base
    assert 640 * 1024 - (heap + SIZE_STACK + world) in range(8)
    # Short of the preferred size, an app gets what's free if that's its minimum.
    free = system.largest_free()
    greedy = build_fork([Resource(b"SIZE", -1, size_res(
        64 << 20, free - DEFAULT_STACK + SIZE_STACK - 1024))])
    _, proc, _ = make(greedy, system=system)
    assert proc.partition.end - proc.partition.start == free
    too_big = build_fork([Resource(b"SIZE", -1, size_res(64 << 20, 32 << 20))])
    with pytest.raises(MemoryError, match="needs 32768K of memory"):
        make(too_big)
    # One that asks for less than its A5 world and the least heap there is
    # gets that much: Beyond Dark Castle's launcher asks for 20K.
    tiny = build_fork([Resource(b"SIZE", -1, size_res(20 << 10, 20 << 10))])
    _, proc, _ = make(tiny)
    assert proc.heap.limit - proc.heap.start >= MIN_HEAP


def test_get_resource_loads_once():
    system, proc, res = make()
    h = res.GetResource("STR ", 128)
    assert proc.heap.contains(h) and contents(system, h) == pstr("Hello")
    assert system.mem.r8(h) == RESOURCE | PURGEABLE
    assert res.GetResource(b"STR ", 128) == h
    assert res.GetResource(0x53545220, 128) == h
    assert res.GetResource("STR ", 999) == 0 and res.ResError() == rm.noErr


def test_rget_resource_is_get_resource(qapp):
    """RGetResource, as PicottHouse calls it: the chain down to the
    System file's, where a Mac's ROM resources would be looked in next."""
    g = Guest([Resource(b"STR ", 128, pstr("Hello"))])
    rm_ = g.process.resources
    h = g("RGetResource", b"STR ", 128)
    assert h and h == rm_.GetResource("STR ", 128) and g.contents(h) == pstr("Hello")
    assert g("RGetResource", b"WDEF", 0) == rm_.GetResource("WDEF", 0) != 0


def test_a_resource_goes_in_the_current_zone_if_its_the_system_heap_or_one_made(qapp):
    """As an INIT sets TheZone to the system heap to keep what it reads
    there (TN ME510); in a zone InitZone made, the same.  Otherwise it goes
    in its file's heap, and resSysHeap puts it in the system heap anyway."""
    strings = [Resource(b"STR ", 128 + i, pstr(f"String {i}")) for i in range(3)]
    g = Guest(strings + [Resource(b"STR ", 131, pstr("System's"), attrs=resSysHeap)])
    sys_heap, appl = g.system.sys_heap.start, g.process.heap.start
    block = g("NewPtr", 0x2000, 0, 0)
    g("InitZone", g.ptr(struct.pack(">IIHI", block, block + 0x2000, 16, 0)))
    for zone, rid in ((sys_heap, 128), (block, 129), (appl, 130)):
        g("SetZone", zone)
        h = g("GetResource", b"STR ", rid)
        assert h and g("HandleZone", h) == zone
    assert g("HandleZone", g("GetResource", b"STR ", 131)) == sys_heap


def test_a_missing_resource_is_not_an_error():
    fork = build_fork([Resource(b"FORM", 0, b"form")])
    system, proc, res = make(fork)
    assert res.GetResource("FORM", 0) and res.ResError() == rm.noErr
    # Microsoft File's datafiles take a nil FORM 1 with noErr as having no
    # second form.
    assert res.GetResource("FORM", 1) == 0 and res.ResError() == rm.noErr
    assert res.GetResource("NONE", 1) == 0 and res.ResError() == rm.noErr
    # Nor where the files have the type: Director's projectors ask for a
    # VWCI for every cast member, and few have one.
    res.GetIndResource("FORM", 9)
    assert res.ResError() == rm.resNotFound
    assert res.GetResource("WDEF", 999) == 0 and res.ResError() == rm.noErr
    assert res.Get1Resource("FORM", 1) == 0 and res.ResError() == rm.noErr
    assert res.Get1Resource("WDEF", 999) == 0 and res.ResError() == rm.noErr


def test_a_missing_named_resource_is_an_error_if_the_last_file_has_its_type():
    fork = build_fork([Resource(b"FORM", 0, b"form")])
    system, proc, res = make(fork)
    assert res.GetNamedResource("FORM", "Report") == 0 and res.ResError() == rm.noErr
    assert res.GetNamedResource("WDEF", "None") == 0 and res.ResError() == rm.resNotFound
    assert res.Get1NamedResource("FORM", "Report") == 0 and res.ResError() == rm.resNotFound


def test_attributes_decide_heap_and_flags():
    system, proc, res = make()
    assert system.mem.r8(res.GetResource("LOCK", 1)) == RESOURCE | LOCKED
    sysh = res.GetResource("SYSH", 1)
    assert system.sys_heap.contains(system.mem.r32(sysh))
    entry = next(top for top in res.maps()).find(b"PREL", 1)
    assert entry.handle and contents(system, entry.handle) == b"preloaded"


def test_an_entry_follows_its_map_when_the_map_moves():
    """As it can while the entry's in use: a program's own _ResourceStub,
    called for each resource read in, can move it."""
    system, proc, res = make()
    h = res.GetResource("STR ", 129)
    m = next(m for m in res.maps() if m.find(b"STR ", 129))
    e = m.find(b"STR ", 129)
    heap = system.heap_of(m.handle)
    was = heap.deref(m.handle)
    heap.move_high(m.handle)
    assert heap.deref(m.handle) > was
    assert (e.id, e.handle) == (129, h) and m.find_handle(h).addr == e.addr


def test_get1_and_use_res_file():
    system, proc, res = make()
    app = res.CurResFile()
    assert res.Get1Resource("WDEF", 0) == 0
    res.UseResFile(0)
    assert res.CurResFile() == res.sys_refnum
    assert res.Get1Resource("WDEF", 0) == system.defproc("WDEF", 0)
    assert res.GetResource("STR ", 128) == 0          # the app's file is above the current one
    res.UseResFile(app)
    assert res.GetResource("STR ", 128)
    res.UseResFile(12345)
    assert res.ResError() == rm.resFNotFound and res.CurResFile() == app
    trees = struct.unpack(">hh", system.mem.read(system.files.boot.vcb + vcbXTRef, 4))
    for tree in trees:                                # open, but no resource file
        res.UseResFile(tree)
        assert res.ResError() == rm.resFNotFound and res.CurResFile() == app
    assert 2 in trees                                 # Poly-Dol's UseResFile(machineType)


def test_counting_and_indexing():
    system, proc, res = make()
    # The application's two, and the System's printer's name.
    assert res.CountResources("STR ") == 3 and res.Count1Resources("MENU") == 2
    assert res.GetIndResource("STR ", 2) == res.GetResource("STR ", 129)
    assert res.GetIndResource("STR ", 3) == res.GetResource("STR ", -8192)
    assert res.GetIndResource("STR ", 4) == 0 and res.ResError() == rm.resNotFound
    types = [res.GetIndType(i) for i in range(1, res.CountTypes() + 1)]
    assert types[:3] == [b"STR ", b"STR#", b"WIND"] and b"WDEF" in types
    assert res.Count1Types() == 10
    assert res.UniqueID("STR ") == 130 and res.Unique1ID("WIND") == 130


def test_counting_looks_in_every_open_file():
    """CountResources, GetIndResource, CountTypes and UniqueID take in the
    files above the current one too (IM I-118 to I-121): Widget Workshop
    makes its own file current again after opening its puzzles, then
    counts the puzzles."""
    system, proc, res = make()
    app = res.CurResFile()
    res.UseResFile(0)                                   # the System, below the app
    assert res.GetResource("STR ", 129) == 0
    assert res.CountResources("STR ") == 3 and res.Count1Resources("STR ") == 1
    h = res.GetIndResource("STR ", 2)
    res.UseResFile(app)
    assert h and h == res.GetResource("STR ", 129)
    res.UseResFile(0)
    assert b"WIND" in [res.GetIndType(i) for i in range(1, res.CountTypes() + 1)]
    assert res.UniqueID("STR ") == 130


def test_named_resources():
    system, proc, res = make()
    h = res.GetNamedResource("STR ", "GREETING")
    assert h == res.GetResource("STR ", 128)
    assert res.GetResInfo(h) == (128, b"STR ", "Greeting")
    assert res.Get1NamedResource("STR ", "nope") == 0


def test_res_load_off_gives_an_empty_handle():
    system, proc, res = make()
    assert proc.world.read(lowmem.ResLoad, 1) == b"\xff"    # 68k code tests the byte
    res.SetResLoad(False)
    assert proc.world.read(lowmem.ResLoad, 1) == b"\0"
    h = res.GetResource("STR ", 129)
    assert h and proc.heap.is_empty(h)
    res.SetResLoad(True)
    res.LoadResource(h)
    assert contents(system, h) == pstr("Bye")


def test_a_purged_resource_comes_back_in_the_same_handle():
    system, proc, res = make()
    h = res.GetResource("STR ", 128)
    fillers = fill(proc.heap)
    assert proc.heap.new_ptr(len(pstr("Hello")))      # only room after purging
    assert proc.heap.is_empty(h)
    for p in fillers:
        proc.heap.dispose_ptr(p)
    assert res.GetResource("STR ", 128) == h
    assert contents(system, h) == pstr("Hello")
    assert system.mem.r8(h) == RESOURCE | PURGEABLE


def test_release_and_detach():
    system, proc, res = make()
    h = res.GetResource("STR ", 129)
    res.ReleaseResource(h)
    assert not proc.heap.is_handle(h)
    again = res.GetResource("STR ", 129)
    res.DetachResource(again)
    assert proc.heap.is_handle(again) and not system.mem.r8(again) & RESOURCE
    assert res.HomeResFile(again) == -1 and res.ResError() == rm.resNotFound
    assert res.GetResource("STR ", 129) != again


def test_a_released_system_resource_is_read_in_again():
    """macrt's own System resources are only in memory, but one that's
    released is read in again, as GetCursor needs when a program has
    released the watch (Aladdin Activity Centre)."""
    system, proc, res = make()
    h = res.GetResource("CURS", 4)
    assert h and contents(system, h) == sysres.WATCH_CURSOR
    res.ReleaseResource(h)
    assert res.ResError() == rm.noErr and not system.sys_heap.is_handle(h)
    res.SetResLoad(False)
    empty = res.GetResource("CURS", 4)
    assert res.SizeResource(empty) == len(sysres.WATCH_CURSOR)
    res.SetResLoad(True)
    again = res.GetResource("CURS", 4)
    assert again == empty and contents(system, again) == sysres.WATCH_CURSOR
    # Given another ID it's still read in again; removed, it's gone.
    res.SetResInfo(again, 300)
    res.ReleaseResource(again)
    renamed = res.GetResource("CURS", 300)
    assert contents(system, renamed) == sysres.WATCH_CURSOR
    assert res.GetResource("CURS", 4) == 0
    res.UseResFile(res.sys_refnum)
    res.RemoveResource(renamed)
    assert res.ResError() == rm.noErr and res.GetResource("CURS", 300) == 0


def test_rsrc_map_entry_is_where_the_resource_is_in_its_map():
    system, proc, res = make()
    h = res.GetResource("STR ", 129)
    at = res.RsrcMapEntry(h)
    assert res.ResError() == rm.noErr
    m = ResourceMap(system.mem, proc.world.r32(lowmem.TopMapHndl))
    assert system.mem.r16s(m.ptr + at) == 129               # its ID
    assert system.mem.r32(m.ptr + at + 8) & 0xFFFFFF == h   # and its handle
    res.DetachResource(h)
    assert res.RsrcMapEntry(h) == 0 and res.ResError() == rm.resNotFound
    assert res.RsrcMapEntry(0) == 0 and res.ResError() == rm.noErr


def test_a_resource_that_wont_expand():
    bent = b"\xa8\x9f\x65\x72" + bytes(12)          # the compressed tag, then nonsense
    fork = build_fork([*read_fork(APP_FORK), Resource(b"BENT", 1, bent, None, 0x01)])
    assert [(r.data, r.attrs) for r in read_fork(fork) if r.type == b"BENT"] == [(bent, 0x01)]
    system, proc, res = make(fork)
    assert res.GetResource("BENT", 1) == 0 and res.ResError() == rm.CantDecompress
    assert res.GetResource("STR ", 129)


# -- more files --------------------------------------------------------------
def test_open_search_and_close_another_file():
    other = MacFile("Other", rsrc=OTHER_FORK)
    system, proc, res = make(others=[other])
    app = res.CurResFile()
    n = res.OpenResFile("other")
    assert n not in (app, 2) and res.CurResFile() == n
    assert [m.refnum for m in res.maps()] == [n, app, res.sys_refnum]
    newest = res.GetResource("STR ", 128)
    assert contents(system, newest) == pstr("Other hello") and res.HomeResFile(newest) == n
    assert res.CountResources("STR ") == 5           # with the System's printer's name
    # Opened again, it has a read-only map of its own, made current, as
    # Tristan Pinball looks for its sounds; asked for writing, its number
    # is answered and made current (Technote 1120).
    res.UseResFile(app)
    copy = res.OpenResFile("Other")
    assert copy not in (n, app, -1) and res.CurResFile() == copy
    assert [m.refnum for m in res.maps()] == [copy, n, app, res.sys_refnum]
    assert res.HomeResFile(res.GetResource("STR ", 128)) == copy
    res.CloseResFile(copy)
    assert res.HomeResFile(newest) == n
    res.UseResFile(app)
    assert res.GetResource("STR ", 300) == 0          # above the current file
    assert res.OpenRFPerm("Other", 0, fsRdWrPerm) == n and res.CurResFile() == n
    res.UseResFile(app)
    res.UseResFile(n)
    res.CloseResFile(n)
    assert [m.refnum for m in res.maps()] == [app, res.sys_refnum]
    assert res.CurResFile() == app and not proc.heap.is_handle(newest)
    assert n not in system.fcbs


def test_opened_again_for_reading_a_file_has_a_map_of_its_own():
    """Read-only, an application can open its own file and let go of what
    it reads there, and close it, and keep its own resources (Technote TB 19;
    PowerPCheck scans itself so)."""
    system, proc, res = make()
    app = res.CurResFile()
    mine = res.GetResource("STR ", 128)
    again = res.OpenRFPerm("Test App", 0, fsRdPerm)
    assert again not in (app, -1) and res.CurResFile() == again
    assert [m.refnum for m in res.maps()] == [again, app, res.sys_refnum]
    assert not system.fcbs[again].write
    copy = res.Get1Resource("STR ", 128)
    assert copy != mine and res.HomeResFile(copy) == again
    assert contents(system, copy) == contents(system, mine)
    res.ReleaseResource(copy)
    res.CloseResFile(again)
    assert [m.refnum for m in res.maps()] == [app, res.sys_refnum] and res.CurResFile() == app
    assert proc.heap.is_handle(mine) and res.GetResource("STR ", 128) == mine


def test_the_system_file_opened_again_answers_its_number():
    """Font/DA Mover writes to the System file through the number
    OpenResFile answers for it (Inside Macintosh I-115)."""
    system, proc, res = make()
    app = res.CurResFile()
    assert res.OpenResFile("System") == res.sys_refnum and res.CurResFile() == app
    assert [m.refnum for m in res.maps()] == [app, res.sys_refnum]


def test_open_res_file_errors():
    system, proc, res = make(others=[MacFile("Data Only", data=b"x")])
    assert res.OpenResFile("Missing") == -1 and res.ResError() == rm.fnfErr
    assert res.OpenResFile("Data Only") == -1 and res.ResError() == rm.eofErr
    res.CloseResFile(0)                                # never closes the System file
    assert [m.refnum for m in res.maps()][-1] == res.sys_refnum


def test_process_close_frees_its_files():
    other = MacFile("Other", rsrc=OTHER_FORK)
    system, proc, res = make(others=[other])
    res.OpenResFile("Other")
    assert len(files_left_open(system)) == 3
    proc.close()
    assert files_left_open(system) == [res.sys_refnum]


def test_process_close_frees_its_files_when_the_map_chain_is_broken():
    other = MacFile("Other", rsrc=OTHER_FORK)
    system, proc, res = make(others=[other])
    res.OpenResFile("Other")
    top = next(res.maps())
    system.mem.w32(top.ptr + 16, 0xFC0000)            # a next map past the end of RAM
    proc.close()
    assert files_left_open(system) == [res.sys_refnum]


# -- changing resources ------------------------------------------------------
def test_add_change_and_update_write_the_fork():
    system, proc, res = make()
    app = proc.app
    h = proc.heap.new_handle(4)
    system.mem.write(proc.heap.deref(h), b"new!")
    res.AddResource(h, "TEST", 5, "Added")
    assert res.ResError() == 0 and res.GetResource("TEST", 5) == h
    assert system.mem.r8(h) & RESOURCE
    assert res.GetResFileAttrs(res.CurResFile()) & rm.mapChanged
    greeting = res.GetResource("STR ", 128)
    system.mem.write(proc.heap.deref(greeting), pstr("Howdy"))
    res.ChangedResource(greeting)
    res.UpdateResFile(res.CurResFile())
    written = {(r.type, r.id): r for r in read_fork(app.rsrc)}
    assert written[b"TEST", 5].data == b"new!" and written[b"TEST", 5].name == b"Added"
    assert written[b"STR ", 128].data == pstr("Howdy")
    assert written[b"WIND", 128].data == wind(Rect(60, 40, 220, 340), "From WIND", k.zoomDocProc)
    assert not res.GetResAttrs(greeting) & rm.resChanged
    # Unchanged resources still load from the rewritten fork.
    assert contents(system, res.GetResource("STR#", 128))[:2] == b"\x00\x02"


def test_partial_resources_read_and_write_the_file():
    other = MacFile("Other", rsrc=OTHER_FORK)
    system, proc, res = make(others=[other])
    app = proc.app
    res.SetResLoad(False)
    h = res.GetResource("STR ", 128)
    res.SetResLoad(True)
    assert res.ReadPartialResource(h, 1, 3) == b"Hel" and res.ResError() == 0
    # Neither the offset nor the count is held to the resource's size.
    past = res.ReadPartialResource(h, 0, 10)
    assert len(past) == 10 and past[:6] == pstr("Hello")
    res.WritePartialResource(h, 1, b"J")
    assert res.ResError() == 0
    assert {(r.type, r.id): r for r in read_fork(app.rsrc)}[b"STR ", 128].data == pstr("Jello")
    res.SetResourceSize(h, 3)
    assert res.ResError() == 0
    written = {(r.type, r.id): r for r in read_fork(app.rsrc)}
    assert written[b"STR ", 128].data == b"\x05Je" and written[b"STR ", 129].data == pstr("Bye")
    # A copy in memory stays as it was, and the file's the one that changes.
    bye = res.GetResource("STR ", 129)
    res.WritePartialResource(bye, 1, b"S")
    res.SetResourceSize(bye, 6)
    assert res.ResError() == rm.resourceInMemory
    assert {(r.type, r.id): r for r in read_fork(app.rsrc)}[b"STR ", 129].data == b"\x03Sye\0\0"
    assert contents(system, bye) == pstr("Bye")
    assert res.ReadPartialResource(bye, 1, 3) == b"Sye"
    assert contents(system, res.GetResource("STR#", 128))[:2] == b"\x00\x02"
    assert res.ReadPartialResource(proc.heap.new_handle(4), 0, 1) == b""
    assert res.ResError() == rm.resNotFound
    n = res.OpenRFPerm("other", 0, fsRdPerm)
    res.WritePartialResource(res.Get1Resource("STR ", 300), 0, b"x")
    assert res.ResError() == -61 and other.rsrc == OTHER_FORK
    res.CloseResFile(n)


def test_add_resource_refuses_a_resource():
    system, proc, res = make()
    res.AddResource(res.GetResource("STR ", 128), "DUPE", 1)
    assert res.ResError() == rm.addResFailed


def test_a_read_only_file_changes_only_in_memory():
    """With mapReadOnly set the map still takes new resources, and
    WriteResource and UpdateResFile write nothing, without an error
    (Worm 1.0 keeps its settings this way on a CD)."""
    system, proc, res = make()
    before = proc.app.rsrc
    res.SetResFileAttrs(res.CurResFile(), mapReadOnly)
    res.RemoveResource(res.GetResource("STR ", 129))
    assert res.ResError() == 0
    h = proc.heap.new_handle(1)
    res.AddResource(h, "PREF", 1)
    assert res.ResError() == 0 and res.GetResource("PREF", 1) == h
    res.WriteResource(h)
    assert res.ResError() == 0
    res.UpdateResFile(res.CurResFile())
    assert res.ResError() == 0 and proc.app.rsrc == before
    assert res.GetResource("PREF", 1) == h and res.GetResource("STR ", 129) == 0


def test_remove_and_rename():
    system, proc, res = make()
    h = res.GetResource("STR ", 129)
    res.SetResInfo(h, 140, "Farewell")
    assert res.GetResInfo(h) == (140, b"STR ", "Farewell")
    assert res.GetNamedResource("STR ", "farewell") == h
    res.RemoveResource(h)
    assert res.GetResource("STR ", 140) == 0 and proc.heap.is_handle(h)
    assert not system.mem.r8(h) & RESOURCE
    prot = res.GetResource("PROT", 1)
    res.RemoveResource(prot)
    assert res.ResError() == rm.rmvResFailed
    res.SetResInfo(prot, 2)
    assert res.ResError() == rm.resAttrErr


def test_create_res_file_in_a_memory_folder():
    system, proc, res = make()
    res.CreateResFile("Prefs")
    n = res.OpenResFile("Prefs")
    h = proc.heap.new_handle(2)
    res.AddResource(h, "PREF", 128)
    res.CloseResFile(n)
    prefs = proc.resources.folder.find("Prefs")
    assert [(r.type, r.id) for r in read_fork(prefs.rsrc)] == [(b"PREF", 128)]
    res.CreateResFile("Prefs")
    assert res.ResError() == rm.dupFNErr


# -- templates ----------------------------------------------------------------
def test_wind_and_dlog():
    t = parse_wind(wind(Rect(40, 20, 200, 300), "Hi", k.noGrowDocProc, False, True, 7,
                        centerMainScreen))
    assert (t.bounds, t.title, t.proc_id, t.visible, t.go_away, t.ref_con, t.position) == \
        (Rect(40, 20, 200, 300), "Hi", k.noGrowDocProc, False, True, 7, centerMainScreen)
    assert parse_wind(wind(Rect(0, 0, 1, 1), "Old")).position == noAutoCenter
    d = parse_dlog(struct.pack(">4hhBxBxIh", 50, 60, 150, 360, k.dBoxProc, 1, 0, 0, 200)
                   + pstr("Dialog"))
    assert (d.bounds, d.proc_id, d.visible, d.go_away, d.items_id, d.title) == \
        (Rect(50, 60, 150, 360), k.dBoxProc, True, False, 200, "Dialog")


def test_alrt_stages():
    a = parse_alrt(struct.pack(">4hhH", 40, 40, 140, 340, 129, 0x7654))
    assert a.items_id == 129
    assert [(s.bold_item, s.drawn, s.sounds) for s in a.stages] == \
        [(1, True, 0), (1, True, 1), (1, True, 2), (1, True, 3)]
    b = parse_alrt(struct.pack(">4hhHH", 0, 0, 1, 1, 1, 0x8888, alertPositionMainScreen))
    assert b.stages[0].bold_item == 2 and not b.stages[0].drawn
    assert b.position == alertPositionMainScreen


def test_ditl_items():
    item = ditl_item
    data = struct.pack(">h", 3) + item(ctrlItem + btnCtrl, (10, 10, 30, 70), b"OK") \
        + item(statText | 128, (40, 10, 60, 200), b"Static") \
        + item(editText, (70, 10, 90, 200), b"odd") \
        + item(iconItem, (5, 200, 37, 232), struct.pack(">h", 2))
    items = parse_ditl(data)
    assert [(i.type, i.rect, i.enabled, i.text, i.res_id) for i in items] == [
        (ctrlItem, Rect(10, 10, 30, 70), True, "OK", 0),
        (statText, Rect(40, 10, 60, 200), False, "Static", 0),
        (editText, Rect(70, 10, 90, 200), True, "odd", 0),
        (iconItem, Rect(5, 200, 37, 232), True, "", 2),
    ]
    assert items[0].is_control and not items[1].is_control


def test_cntl_mbar_str_list():
    c = parse_cntl(struct.pack(">4hhBxhhhI", 10, 10, 30, 90, 1, 1, 1, 0, 1, 42) + pstr("Check"))
    assert (c.bounds, c.value, c.visible, c.max, c.min, c.proc_id, c.ref_con, c.title) == \
        (Rect(10, 10, 30, 90), 1, True, 1, 0, 1, 42, "Check")
    assert parse_mbar(struct.pack(">3h", 2, 128, 129)) == [128, 129]
    assert parse_str_list(struct.pack(">h", 2) + pstr("a") + pstr("bc")) == ["a", "bc"]


def test_place():
    desk = Rect(20, 0, 480, 640)
    r = Rect(0, 0, 100, 200)
    assert place(r, noAutoCenter, desk) == r
    c = place(r, centerMainScreen, desk, title_bar=20)
    assert (c.width, c.height) == (200, 100)
    assert c.left == 220 and c.top - 20 - desk.top == desk.bottom - c.bottom
    a = place(r, alertPositionMainScreen, desk)
    assert a.left == 220 and (a.top - desk.top) * 2 == desk.bottom - a.bottom


# -- through the Toolbox, in a running display --------------------------------
class FromResources(Recorder):
    """Recorder, but its menus and window come from its resource file."""

    def __call__(self, tb):
        self.tb = tb
        mbar = tb.GetNewMBar(128)
        tb.SetMenuBar(mbar)
        tb.DrawMenuBar()
        self.window = tb.GetNewWindow(128)
        self.centered = tb.GetNewWindow(129)
        self.strings = [tb.string_of(tb.GetString(128)), tb.GetIndString(128, 2),
                        tb.GetIndString(128, 3)]
        self.menu_again = tb.GetMenu(129)
        self.ready.set()
        while True:
            got, ev = tb.WaitNextEvent(k.everyEvent, 5)
            if got and self.handle(ev):
                return


def launch_from_resources(h):
    guest = FromResources("Test App")
    app = MacFile("Test App", rsrc=APP_FORK, type=b"APPL", creator=b"TEST")
    proc = h.d.launch(guest.name, guest, app=app)
    h.wait_until(guest.ready.is_set, timeout=5, what="the app's window")
    h.settle()
    return guest, proc


def test_get_new_window_and_menus_from_resources(harness):
    h = harness()
    guest, proc = launch_from_resources(h)
    w = guest.window
    assert (w.title, w.content, w.proc_id) == ("From WIND", Rect(60, 40, 220, 340), k.zoomDocProc)
    assert list(proc.windows) == [guest.centered, w]
    c = guest.centered.content
    sw, sh = h.d.size
    assert (c.width, c.height) == (200, 100) and c.left == (sw - 200) // 2
    assert guest.strings == ["Hello", "two", ""]

    menus = proc.menus.own()
    assert [(m.id, m.title) for m in menus] == [(129, "File"), (130, "Edit")]
    file_menu = menus[0]
    assert guest.menu_again == file_menu
    assert file_menu.handle == proc.resources.GetResource("MENU", 129)
    assert h.d.system.mem.r32(file_menu.ptr + MENU_PROC) == h.d.system.defproc("MDEF", 0)
    assert [(i.text, i.cmd, i.enabled) for i in file_menu.items] == [
        ("New", "N", True), ("Close", "W", True), ("-", "", False), ("Quit", "Q", True)]
    assert not h.d.system.mem.r8(file_menu.handle) & PURGEABLE


def test_menus_from_resources_work(harness):
    h = harness()
    guest, proc = launch_from_resources(h)
    bar = h.d.default_bar(proc)
    assert [m.title for m, _ in bar.cells()][-2:] == ["File", "Edit"]
    h.choose(bar, "File", 1)
    h.wait_until(lambda: len(proc.windows) == 3, what="File > New")
    h.choose(bar, "File", 4)
    h.wait_until(lambda: proc not in h.d.processes, what="File > Quit")
    assert files_left_open(h.d.system) == [h.d.system.mem.r16(lowmem.SysMap)]


def test_get_menu_bar_is_a_copy(harness):
    h = harness()
    guest, proc = launch_from_resources(h)
    tb = guest.tb
    copy = tb.GetMenuBar()
    assert copy != proc.menus.handle
    assert read_menu_list(h.d.system.mem, copy) == proc.menus.handles()


def test_the_menu_list_keeps_its_mbdf(harness):
    """InitProcMenu's MBDF ID stays with the menu bar as menus come and
    go, and goes with copies of it."""
    h = harness()
    guest, proc = launch_from_resources(h)
    tb, mem = guest.tb, h.d.system.mem
    proc.menus.mbdf_id = 300
    tb.DeleteMenu(130)
    copy = tb.GetMenuBar()
    tb.ClearMenuBar()
    assert proc.menus.mbdf_id == 300 and read_mbdf_id(mem, copy) == 300
    assert read_mbdf_id(mem, tb.GetNewMBar(128)) == 300
    proc.menus.mbdf_id = 0
    tb.SetMenuBar(copy)
    assert proc.menus.mbdf_id == 300 and [m.id for m in proc.menus.own()] == [129]
    assert proc.menus.offset(129) == 6 and proc.menus.offset(130) == 0


def test_the_users_size_comes_first():
    """SIZE 0 is what the user set in Get Info; it beats the developer's SIZE -1."""
    fork = build_fork([Resource(b"SIZE", -1, size_res(32 * 1024, 32 * 1024)),
                       Resource(b"SIZE", 0, size_res(800 * 1024, 32 * 1024))])
    _, proc, _ = make(fork)
    assert proc.partition.end - proc.partition.start == 800 * 1024 - SIZE_STACK + DEFAULT_STACK


def test_a_memory_size_given_at_launch_beats_the_size_resource():
    """--memory, as Get Info's memory size: Race Car Simulator's SIZE asks
    for 96K, too little for its FORTH dictionary."""
    fork = build_fork([Resource(b"SIZE", -1, size_res(96 * 1024, 96 * 1024))])
    _, proc, _ = make(fork, memory=384 * 1024)
    assert proc.partition.end - proc.partition.start == 384 * 1024 - SIZE_STACK + DEFAULT_STACK
    # And an application with no SIZE resource gets it too.
    _, proc, _ = make(build_fork([Resource(b"STR ", 128, pstr("Hello"))]), memory=200 * 1024)
    assert proc.partition.end - proc.partition.start == 200 * 1024 - SIZE_STACK + DEFAULT_STACK


def test_d0_and_the_flags_after_a_resource_call_are_reserrs(qapp, tmp_path, monkeypatch):
    """The Resource Manager's procedures end by storing ResErr from D0:
    MacWrite branches on the flags that leaves after CreateResFile, and
    Magic Castle tests D0 after LoadResource."""
    from guest import Processor
    monkeypatch.chdir(tmp_path)
    m = Processor()
    name = m.ptr(b"\x03New")
    create = f"2F3C{name:08X}A9B1"                  # CreateResFile(name)
    m.cpu.set_d(0, 0x000C1C30)
    m.run(bytes.fromhex(create + "56C7" + "2800" + create + "56C65BC5"))
    # SNE D7; MOVE.L D0,D4; ...; SNE D6; SMI D5
    assert m.cpu.d(7) & 0xFF == 0 and m.cpu.d(4) == 0     # it made it: Z set, D0 noErr
    assert m.process.world.r16(lowmem.ResErr) == rm.dupFNErr      # then:
    assert (m.cpu.d(6) & 0xFF, m.cpu.d(5) & 0xFF) == (0xFF, 0xFF)   # SNE and SMI
    assert m.cpu.d(0) == rm.dupFNErr & 0xFFFFFFFF
