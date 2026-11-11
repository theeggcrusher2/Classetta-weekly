"""
Resource Manager traps: getting, counting and changing resources, opening
and creating resource files, and _ResourceStub, which programs patch to
read in resources of their own making.  The Resource Manager itself is
macrt.toolbox.resources.
"""

from __future__ import annotations

from ..toolbox.memmgr import noErr
from ..toolbox.resources import Entry, ResourceManager, ResourceMap
from .dispatch import RESERR_FLAGS, SIGNATURES, raw, trap


# =========================================================================
# Resource Manager
# =========================================================================
def _rm(t) -> ResourceManager:
    return t.process.resources


@trap
def GetResource(t, rtype, rid):
    return _rm(t).get(rtype, rid)


@trap
def RGetResource(t, rtype, rid):
    """GetResource, which on a Mac goes on to the ROM's resources after
    the System file's: here they're the System file's."""
    return _rm(t).get(rtype, rid)


@trap
def Get1Resource(t, rtype, rid):
    return _rm(t).get(rtype, rid, one=True)


@trap
def GetNamedResource(t, rtype, name):
    return _rm(t).GetNamedResource(rtype, t.pstr(name))


@trap
def Get1NamedResource(t, rtype, name):
    return _rm(t).Get1NamedResource(rtype, t.pstr(name))


@trap
def GetIndResource(t, rtype, index):
    return _rm(t).GetIndResource(rtype, index)


@trap
def Get1IndResource(t, rtype, index):
    return _rm(t).Get1IndResource(rtype, index)


@trap
def CountResources(t, rtype):
    return _rm(t).CountResources(rtype)


@trap
def Count1Resources(t, rtype):
    return _rm(t).Count1Resources(rtype)


@trap
def CountTypes(t):
    return _rm(t).CountTypes()


@trap
def Count1Types(t):
    return _rm(t).Count1Types()


@trap
def GetIndType(t, type_p, index):
    t.mem.write(type_p, _rm(t).GetIndType(index) or bytes(4))


@trap
def Get1IndType(t, type_p, index):
    t.mem.write(type_p, _rm(t).Get1IndType(index) or bytes(4))


@trap
def GetResInfo(t, h, id_p, type_p, name_p):
    rid, rtype, name = _rm(t).GetResInfo(h)
    t.w16(id_p, rid)
    if type_p:
        t.mem.write(type_p, (rtype or b"").ljust(4, b"\0")[:4])
    t.set_str255(name_p, name or "")


@trap
def SetResInfo(t, h, rid, name):
    _rm(t).SetResInfo(h, rid, t.str255(name) if name else None)


@trap
def AddResource(t, h, rtype, rid, name):
    _rm(t).AddResource(h, rtype, rid, t.str255(name))


@trap
def OpenResFile(t, name):
    return _rm(t).OpenResFile(t.str255(name))


@trap
def HOpenResFile(t, vref, dir_id, name, permission):
    return _rm(t).HOpenResFile(vref, dir_id, t.str255(name), permission)


@trap
def OpenRFPerm(t, name, vref, permission):
    return _rm(t).OpenRFPerm(t.str255(name), vref, permission)


@trap
def CreateResFile(t, name):
    _rm(t).CreateResFile(t.str255(name))


@trap
def HCreateResFile(t, vref, dir_id, name):
    _rm(t).HCreateResFile(vref, dir_id, t.str255(name))


@trap
def SetResLoad(t, load):
    _rm(t).SetResLoad(bool(load))


@trap
def GetResourceSizeOnDisk(t, h):
    return _rm(t).SizeResource(h)


@trap
def ReadPartialResource(t, h, offset, buf, count):
    t.mem.write(buf, _rm(t).ReadPartialResource(h, offset, count))


@trap
def WritePartialResource(t, h, offset, buf, count):
    _rm(t).WritePartialResource(h, offset, t.mem.read(buf, count) if count > 0 else b"")


@trap
def SetResourceSize(t, h, size):
    _rm(t).SetResourceSize(h, size)


@trap
def GetMaxResourceSize(t, h):
    return _rm(t).MaxSizeRsrc(h)


@trap
def SetResPurge(t, install):
    pass


@trap
def RsrcZoneInit(t):
    pass


# What the System does as it starts up, and no application should
# (Inside Macintosh I-114).  Mombasa does: it gets the System file's
# reference number back, with the files open left as they are.
@trap
def InitResources(t):
    return _rm(t).sys_refnum


def _forward(name: str):
    def call(t, *args):
        return getattr(_rm(t), name)(*args)
    call.__name__ = name
    return call


for _name in ("CloseResFile", "CurResFile", "UseResFile", "HomeResFile", "RsrcMapEntry",
              "ResError", "UpdateResFile", "GetResFileAttrs", "SetResFileAttrs", "UniqueID",
              "Unique1ID", "LoadResource", "ReleaseResource", "DetachResource", "GetResAttrs",
              "SetResAttrs", "RemoveResource", "ChangedResource", "WriteResource"):
    trap(_forward(_name))


# References: entries in the current file's map for resources in other
# files, which the 64K ROM kept and later ones don't.  MacWrite adds one
# for each font a document uses, to the document it saves.  Here they're
# not kept.
@trap
def AddReference(t, h, rid, name):
    _rm(t).error = noErr


@trap
def RmveReference(t, h):
    _rm(t).error = noErr


# The Resource Manager's procedures end by storing ResErr from D0, which
# leaves it there and the flags as TST.W would.  MacWrite branches on them
# after CreateResFile without testing anything itself; Magic Castle tests
# D0 after LoadResource.  ReleaseResource leaves D0 alone (see KEEPS_D0).
for _name in ("CreateResFile", "HCreateResFile", "CloseResFile", "UseResFile",
              "UpdateResFile", "SetResFileAttrs", "LoadResource", "DetachResource",
              "SetResAttrs", "SetResInfo", "AddResource", "RemoveResource", "ChangedResource",
              "WriteResource", "AddReference", "RmveReference"):
    RESERR_FLAGS.add(SIGNATURES[_name].trap)


# The Resource Manager reads in each resource it's asked for by calling
# _ResourceStub's address with the map's handle in A4 and the reference
# list entry at A2 (and A3); the resource's handle comes back in A0.
# Applications patch it to read in resources of their own making, such as
# compressed ones, and leave the rest to the original.
RESOURCE_STUB = 0xA0FC


@raw(RESOURCE_STUB)
def ResourceStub(t, word):
    """The original, which reads the resource in if ResLoad says to."""
    cpu, rm = t.cpu, _rm(t)
    m = ResourceMap(t.mem, cpu.a(4))
    cpu.set_a(0, rm.read_in(m, Entry(m, cpu.a(2) & cpu.mask, b""), rm.res_load))


GET_RESOURCE, GET1_RESOURCE = 0xA9A0, 0xA81F


def get_resource(t, rtype: bytes, rid: int, one: bool) -> int | None:
    """The Resource Manager's get hook: GetResource, or Get1Resource if
    one, for macrt's own managers, through the application's patch on the
    trap, as LoadSeg's is (macrt.m68k.segload): Prince of Destruction's
    expands the dialogs and pictures it keeps compressed.  None if it hasn't
    patched it, or the application isn't the one running."""
    patch = t.patches.get(GET1_RESOURCE if one else GET_RESOURCE)
    if patch is None or not t.on_own_thread():
        return None
    return t.call(patch, (("l", int.from_bytes(rtype, "big")), ("h", rid)), "p") & t.mem.system.addr_mask


def load_resource(t, m: ResourceMap, e: Entry, force: bool) -> int | None:
    """The Resource Manager's load hook: the application's patch on
    _ResourceStub reads the resource in, with ResLoad set to force while
    it does.  None if there's no patch, or the application isn't the one
    running."""
    patch = t.patches.get(RESOURCE_STUB)
    if patch is None or not t.on_own_thread():
        return None
    cpu, rm = t.cpu, _rm(t)
    ctx = cpu.context()
    res_load = rm.res_load
    rm.SetResLoad(force)
    rm.error = noErr
    cpu.set_a(4, m.handle)
    cpu.set_a(3, e.addr)
    cpu.set_a(2, e.addr)
    cpu.set_a(0, patch)
    t._jsr(patch)
    h = cpu.a(0) & cpu.mask
    cpu.restore(ctx)
    rm.SetResLoad(res_load)
    return h
