"""The Resource Manager's traps: what they take from the program and leave
in its memory, over macrt.toolbox.resources."""

import threading

from builders import pstr
from guest import Guest, Processor

from macrt.m68k.glue_resources import (GET1_RESOURCE, GET_RESOURCE, RESOURCE_STUB,
                                       get_resource, load_resource)
from macrt.resfork import Resource
from macrt.toolbox import resources as rm
from macrt.toolbox.files import fsRdWrPerm

STR, ICON = (int.from_bytes(t, "big") for t in (b"STR ", b"ICON"))
FORK = [Resource(b"STR ", 128, pstr("Hello"), b"Greeting"),
        Resource(b"STR ", 129, pstr("Bye")),
        Resource(b"ICON", 1, bytes(128))]


def test_getting_and_counting_resources(qapp):
    g = Guest(FORK)
    name = g.ptr(pstr("Greeting"))
    h = g("GetNamedResource", STR, name)
    assert h and g.contents(h) == pstr("Hello")
    assert g("Get1NamedResource", STR, name) == h == g("RGetResource", STR, 128)
    assert g("GetNamedResource", STR, 0) == 0           # NIL: no name
    bye = g("Get1Resource", STR, 129)
    assert {g("GetIndResource", STR, i) for i in (1, 2)} == {h, bye}
    assert {g("Get1IndResource", STR, i) for i in (1, 2)} == {h, bye}
    assert g("Count1Resources", STR) == 2 <= g("CountResources", STR)
    assert g("Count1Types") == 2 <= g("CountTypes")

    p = g.ptr(b"\xee" * 4)
    types = set()
    for i in (1, 2):
        g("Get1IndType", p, i)
        types.add(g.mem.read(p, 4))
    assert types == {b"STR ", b"ICON"}
    g("Get1IndType", p, 3)
    assert g.mem.read(p, 4) == bytes(4)
    g("GetIndType", p, 1)
    assert g.mem.read(p, 4) == g.process.resources.GetIndType(1)
    g("GetIndType", p, 999)
    assert g.mem.read(p, 4) == bytes(4)


def test_information_about_a_resource(qapp):
    g = Guest(FORK)
    res = g.process.resources
    h = g("GetResource", STR, 128)
    rid, rtype, name = g.ptr(b"\xee" * 2), g.ptr(b"\xee" * 4), g.ptr(b"\xee" * 256)
    g("GetResInfo", h, 0, 0, 0)                         # NIL: none wanted
    assert res.ResError() == 0
    g("GetResInfo", h, rid, rtype, name)
    assert (g.mem.r16s(rid), g.mem.read(rtype, 4), g.mem.r_pstr(name)) == \
        (128, b"STR ", b"Greeting")
    g("GetResInfo", g.heap.new_handle(4), rid, rtype, name)
    assert (g.mem.r16s(rid), g.mem.read(rtype, 4), g.mem.r_pstr(name)) == (0, bytes(4), b"")
    assert res.ResError() == rm.resNotFound

    g("SetResInfo", h, 300, 0)                          # NIL: the name as it was
    assert res.GetResInfo(h) == (300, b"STR ", "Greeting")
    g("SetResInfo", h, 301, g.ptr(pstr("Salutation")))
    assert res.GetResInfo(h) == (301, b"STR ", "Salutation")

    mine = g.handle(b"mine")
    g("AddResource", mine, ICON, 2, 0)                  # NIL: no name
    assert res.ResError() == 0 and res.GetResInfo(mine) == (2, b"ICON", "")
    assert g("Get1Resource", ICON, 2) == mine


def test_partial_resources(qapp):
    g = Guest(FORK)
    res = g.process.resources
    h = g("GetResource", STR, 128)
    assert g("GetResourceSizeOnDisk", h) == g("GetMaxResourceSize", h) == 6
    buf = g.ptr(b"\xee" * 8)
    g("ReadPartialResource", h, 1, buf, 3)
    assert g.mem.read(buf, 4) == b"Hel\xee"
    g("ReadPartialResource", h, 0, buf + 4, 0)
    assert g.mem.read(buf + 4, 4) == b"\xee" * 4

    g.mem.write(buf, b"Jel")
    g("WritePartialResource", h, 1, buf, 3)
    assert res.ResError() == 0 and res.ReadPartialResource(h, 0, 6) == pstr("Jello")
    g("WritePartialResource", h, 1, 0, 0)               # nothing, from nowhere
    assert res.ResError() == 0 and res.ReadPartialResource(h, 0, 6) == pstr("Jello")
    g("SetResourceSize", h, 4)
    assert g("GetResourceSizeOnDisk", h) == 4


def test_files(qapp):
    g = Guest(FORK)
    res = g.process.resources
    app = res.CurResFile()
    assert g("InitResources") == res.sys_refnum and res.CurResFile() == app

    g("CreateResFile", g.ptr(pstr("One")))
    assert res.ResError() == 0
    one = g("OpenResFile", g.ptr(pstr("One")))
    assert one > 0 and res.CurResFile() == one
    g("HCreateResFile", 0, 0, g.ptr(pstr("Two")))
    assert res.ResError() == 0
    two = g("HOpenResFile", 0, 0, g.ptr(pstr("Two")), fsRdWrPerm)
    assert two > 0 and two != one and res.CurResFile() == two
    g("CloseResFile", two)
    again = g("OpenRFPerm", g.ptr(pstr("Two")), 0, fsRdWrPerm)
    assert again > 0 and res.CurResFile() == again
    assert g("OpenResFile", g.ptr(pstr("Missing"))) == -1 and res.ResError() == rm.fnfErr


def test_the_calls_with_little_behind_them(qapp):
    g = Guest(FORK)
    res = g.process.resources
    g("SetResLoad", 0)
    assert res.res_load is False
    h = g("GetResource", STR, 129)
    assert h and not g.mem.r32(h)                       # not read in
    g("SetResLoad", 1)
    assert res.res_load is True
    g("SetResPurge", 1)
    g("RsrcZoneInit")
    for name, args in (("AddReference", (h, 5, 0)), ("RmveReference", (h,))):
        res.error = rm.resNotFound
        g(name, *args)
        assert res.ResError() == 0


def test_a_patch_on_the_resource_stub_is_called_to_read_resources_in(qapp):
    """With the map's handle in A4, the entry at A2 and A3, and ResLoad
    set to whether it's to be read in; the registers and ResLoad as they
    were after, and the patch's handle the resource's."""
    g = Processor(FORK)
    res = g.process.resources
    g.thread_id = threading.get_ident()
    patch = 0x1234
    g.patches = {RESOURCE_STUB: patch}
    res.load_hook = lambda m, e, force: load_resource(g, m, e, force)
    seen = []

    def jsr(addr):
        """The patch: STR 129 is of its own making, the rest it leaves to
        the original."""
        cpu = g.cpu
        seen.append((addr, cpu.a(0), cpu.a(4), cpu.a(2), cpu.a(3), res.res_load))
        m = rm.ResourceMap(g.mem, cpu.a(4))
        e = rm.Entry(m, cpu.a(2), b"")
        cpu.set_a(0, g.handle(b"made") if e.id == 129 else res.read_in(m, e, res.res_load))
    g._jsr = jsr
    for n in range(5):
        g.cpu.set_a(n, 0x100 + n)

    h = g("GetResource", STR, 129)
    assert g.contents(h) == b"made" and len(seen) == 1
    addr, a0, a4, a2, a3, loading = seen[0]
    assert (addr, a0, loading) == (patch, patch, True)
    assert a2 == a3 == rm.ResourceMap(g.mem, a4).find(b"STR ", 129).addr
    assert [g.cpu.a(n) for n in range(5)] == [0x100 + n for n in range(5)]

    g("SetResLoad", 0)
    h = g("GetResource", STR, 128)
    assert seen[-1][-1] is False
    assert h and not g.mem.r32(h)
    g("LoadResource", h)
    assert seen[-1][-1] is True and res.res_load is False
    assert g.contents(h) == pstr("Hello")

    # The application isn't the one running: the Resource Manager reads it.
    g("SetResLoad", 1)
    calls = len(seen)
    g.holding = False
    assert g.contents(g("GetResource", ICON, 1)) == bytes(128) and len(seen) == calls


def test_the_toolbox_gets_resources_through_a_patch_on_getresource(qapp):
    """macrt's own managers get a resource through the application's patch
    on GetResource, or on Get1Resource, with its type and ID: Prince of
    Destruction's expands the dialogs and pictures it keeps compressed.
    The trap itself is the original the patch calls on to."""
    g = Guest(FORK)
    res = g.process.resources
    g.thread_id = threading.get_ident()
    get, get1 = 0x1234, 0x2345
    g.patches = {GET_RESOURCE: get, GET1_RESOURCE: get1}
    res.get_hook = lambda rtype, rid, one: get_resource(g, rtype, rid, one)
    made = g.handle(pstr("Made"))
    g.procs = {get: lambda rtype, rid: made if rid == 129 else g("GetResource", rtype, rid),
               get1: lambda rtype, rid: made}

    assert g.contents(g("GetString", 129)) == pstr("Made")
    assert g.calls == [(get, (("l", STR), ("h", 129)))]
    assert g.contents(g("GetString", 128)) == pstr("Hello") and len(g.calls) == 2
    assert g.contents(g("GetResource", STR, 129)) == pstr("Bye") and len(g.calls) == 2
    assert res.Get1Resource("STR ", 128) == made and g.calls[-1] == (get1, (("l", STR), ("h", 128)))
    assert g.contents(g("Get1Resource", STR, 128)) == pstr("Hello") and len(g.calls) == 3

    # The application isn't the one running: the Resource Manager gets it.
    g.holding = False
    assert g.contents(g("GetString", 129)) == pstr("Bye") and len(g.calls) == 3
