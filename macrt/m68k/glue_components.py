"""
The Component Manager (Inside Macintosh: More Macintosh Toolbox, the
Component Manager): components registered from a 'thng' resource or
with an entry point, found by what they describe, opened, called and
closed, as the Sound control panel's own sub-panels are.

_ComponentDispatch's D0 picks the routine.  0 is a call to a component
instance, made with the ComponentCallNow glue: the instance's component
is called with the call's ComponentParameters and the instance's
storage, or, if it asked for fast dispatch, with the parameters as they
are on the stack and the storage in the instance's place.  $FF is
CallComponentFunction, and CallComponentFunctionWithStorage, the one
with the storage pushed first.

A component registered from a 'thng' has its code loaded the first time
it's opened, from the file the 'thng' came from.  The components an
application registered are unregistered once it has quit, their code
and resources having been in its heap and files, and the connections it
left open are closed.  A connection whose A5 is 0, a global component's
until it sets one, runs in its client's A5 world.
"""

from __future__ import annotations

import itertools
import struct
from dataclasses import dataclass
from weakref import WeakKeyDictionary

from ..toolbox.files import fsRdPerm
from ..toolbox.memmgr import noErr
from .dispatch import pop_args, put_result, raw

invalidComponentID = -3000
validInstancesExist = -3001
componentNotCaptured = -3002
badComponentInstance = 0x80008001
badComponentSelector = 0x80008002

# The requests the Component Manager makes of components
kComponentOpenSelect = -1
kComponentCloseSelect = -2
kComponentRegisterSelect = -5
kComponentUnregisterSelect = -7

cmpWantsRegisterMessage = 0x80000000        # in componentFlags
cmpFastDispatch = 0x40000000
registerCmpGlobal, registerCmpNoDuplicates, registerCompAfter = 1, 2, 4
componentDoAutoVersion, componentWantsUnregister, componentAutoVersionIncludeFlags = 1, 2, 4
defaultComponentAnyFlags, defaultComponentAnyManufacturer, defaultComponentAnySubType = 1, 2, 4

# What Gestalt's 'cpnt' says: a version with automatic version control,
# the unregister request and icon families
VERSION = 3

# A ComponentDescription: type, subtype, manufacturer, flags and the
# flags' mask.  A 'thng' is one, then the code's, name's, information
# string's and icon's resource types and IDs, then, optionally, the
# component's version, its registration flags and its icon family.
DESC = struct.Struct(">5I")
SPEC = struct.Struct(">4sh")
THNG = DESC.size + 4 * SPEC.size
EXTENSION = struct.Struct(">IIh")
NO_RESOURCE = bytes(4)


@dataclass(eq=False)
class Component:
    id: int
    desc: tuple                 # the ComponentDescription's five longs
    owner: object               # the process that registered it
    is_global: bool
    a5: int = 0                 # a local component's: its registrant's
    entry: int = 0              # where its code starts, once it's loaded
    code: tuple | None = None   # its 'thng's code resource: type and ID
    home: object = None         # the file its 'thng' came from
    name: bytes = b""
    info: bytes = b""
    icon: bytes = b""
    version: int = 0
    register_flags: int = 0
    icon_family: int = 0
    refcon: int = 0
    captured: bool = False
    code_handle: int = 0        # the copy of its code, which it was loaded into


@dataclass(eq=False)
class Instance:
    id: int
    component: Component
    owner: object               # the process that opened it
    a5: int
    storage: int = 0
    error: int = 0


class Manager:
    """The system's components, in the order they're searched, and the
    connections open to them."""

    def __init__(self):
        self.components: list[Component] = []
        self.by_id: dict[int, Component] = {}
        self.instances: dict[int, Instance] = {}
        self.seed = 1
        self.ids = itertools.count(0x10001)


_managers: WeakKeyDictionary = WeakKeyDictionary()     # System: Manager


def manager(t) -> Manager:
    return _managers.setdefault(t.system, Manager())


def _component(m: Manager, ref: int) -> Component | None:
    """The component an ID names, or an instance's."""
    inst = m.instances.get(ref)
    return inst.component if inst is not None else m.by_id.get(ref)


def _visible(t, m: Manager) -> list[Component]:
    """What a search finds: the global components and the application's
    own, but not those another component has captured."""
    return [c for c in m.components
            if not c.captured and (c.is_global or c.owner is t.process)]


def _matches(c: Component, looking: tuple) -> bool:
    kind, sub, maker, flags, mask = looking
    return ((not kind or kind == c.desc[0]) and (not sub or sub == c.desc[1])
            and (not maker or maker == c.desc[2]) and c.desc[3] & mask == flags & mask)


def _description(t, p: int) -> tuple:
    return DESC.unpack(t.mem.read(p & t.mem.system.addr_mask, DESC.size))


def _handle_bytes(t, h: int) -> bytes:
    h &= t.mem.system.addr_mask
    heap = t.system.find_heap(h) if h else None
    p = t.mem.r32(h) & t.mem.system.addr_mask if heap is not None else 0
    return t.mem.read(p, heap.handle_size(h)) if p else b""


def _fill(t, h: int, data: bytes) -> None:
    """The handle made to hold data, emptied or not as it was."""
    heap = t.system.find_heap(h)
    if heap is None:
        return
    resized = (heap.set_handle_size(h, len(data)) if heap.deref(h)
               else heap.realloc_handle(h, len(data)))
    if resized and data:
        t.mem.write(heap.deref(h), data)


# =========================================================================
# Calling components
# =========================================================================
def _request(t, inst: Instance, params: int) -> int:
    """Send inst's component the request in the ComponentParameters at
    params: its result."""
    comp, cpu = inst.component, t.cpu
    a5 = cpu.a(5)
    if inst.a5:
        cpu.set_a(5, inst.a5)
    try:
        if comp.desc[3] & cmpFastDispatch:
            return _fast(t, comp.entry, inst.storage, params)
        return t.call(comp.entry, (("p", params), ("L", inst.storage)), "l") & 0xFFFFFFFF
    finally:
        cpu.set_a(5, a5)


def _fast(t, entry: int, storage: int, params: int) -> int:
    """A fast-dispatch component's entry, called as a client's call reaches
    it: the parameters, the storage where the instance was, and a return
    address, with the request in D0 and where the storage is in A0."""
    cpu, mem = t.cpu, t.mem
    size, what = mem.r8(params + 1), mem.r16s(params + 2)
    sp = cpu.sp
    storage_at = sp - 8                     # under the result
    mem.w32(storage_at, storage)
    cpu.sp = storage_at - size
    mem.write(cpu.sp, mem.read(params + 4, size))
    t.call_for(entry, (), d0=what & 0xFFFFFFFF, a0=storage_at)
    result = mem.r32(sp - 4)
    cpu.sp = sp
    return result


def _send(t, inst: Instance, what: int, *params: int) -> int:
    """One of the Component Manager's own requests, with its long
    parameters."""
    cpu = t.cpu
    sp = cpu.sp
    for value in params:
        cpu.push32(value)
    cpu.push16(what & 0xFFFF)
    cpu.push16(4 * len(params))             # the flags and the parameters' size
    try:
        return _request(t, inst, cpu.sp)
    finally:
        cpu.sp = sp


def _client_call(t) -> None:
    """ComponentCallNow's call: the ComponentParameters at SP, the instance
    above them and the result above that."""
    cpu, mem = t.cpu, t.mem
    sp = cpu.sp
    size = mem.r8(sp + 1)
    inst = manager(t).instances.get(mem.r32(sp + 4 + size))
    result = _request(t, inst, sp) if inst is not None else badComponentInstance
    cpu.sp = sp + 8 + size
    mem.w32(cpu.sp, result)
    cpu.set_d(0, result)


def _call_function(t) -> None:
    """CallComponentFunction(params, func), or ...WithStorage(storage,
    params, func): func is called with the parameters params has, under
    the storage if it was pushed, as its first; it returns to the caller
    with its result where the caller left room for it."""
    cpu, mem = t.cpu, t.mem
    func, params = mem.r32(cpu.sp) & mem.system.addr_mask, mem.r32(cpu.sp + 4) & mem.system.addr_mask
    size = mem.r8(params + 1)
    sp = cpu.sp + 8 - size
    mem.write(sp, mem.read(params + 4, size))
    cpu.sp = sp
    cpu.push32(cpu.pc)
    cpu.pc = func


# =========================================================================
# Opening and closing
# =========================================================================
def _home_resource(t, comp: Component, kind: bytes, rid: int) -> bytes | None:
    """A resource of the file comp's 'thng' came from, which is opened for
    a moment if it's been closed."""
    if kind == NO_RESOURCE or comp.home is None:
        return None
    rm = comp.owner.resources
    err, current = rm.error, rm.CurResFile()
    refnum = next((m.refnum for m in rm.maps() if rm.node_of(m.refnum) is comp.home), None)
    opened = refnum is None
    if opened:
        refnum = rm.open_node(comp.home, fsRdPerm)
    data = None
    if rm.error == noErr:
        rm.UseResFile(refnum)
        h = rm.get(kind, rid, one=True)
        data = _handle_bytes(t, h) if h else None
        if opened:
            rm.CloseResFile(refnum)
    rm.UseResFile(current)
    rm.error = err
    return data


def _load(t, comp: Component) -> bool:
    """A component from a 'thng' has its code copied into its registrant's
    heap when first it's needed."""
    if comp.entry:
        return True
    code = _home_resource(t, comp, *comp.code) if comp.code else None
    if not code:
        return False
    heap = comp.owner.heap
    h = heap.new_handle(len(code))
    if not h:
        return False
    heap.lock(h)
    t.mem.write(heap.deref(h), code)
    comp.code_handle, comp.entry = h, heap.deref(h)
    return True


def _open(t, comp: Component) -> int:
    """A new connection, which the component's open request can refuse:
    then it's closed, and there's none."""
    m = manager(t)
    if not _load(t, comp):
        return 0
    inst = Instance(next(m.ids), comp, t.process, comp.a5)
    m.instances[inst.id] = inst
    if _send(t, inst, kComponentOpenSelect, inst.id):
        _close(t, inst)
        return 0
    return inst.id


def _close(t, inst: Instance) -> int:
    try:
        return _send(t, inst, kComponentCloseSelect, inst.id)
    finally:
        manager(t).instances.pop(inst.id, None)


def _connected(t, comp: Component, what: int) -> int | None:
    """A request made of comp through a connection opened for it: open,
    the request and close.  None if it won't open."""
    ci = _open(t, comp)
    if not ci:
        return None
    inst = manager(t).instances[ci]
    result = _send(t, inst, what)
    _close(t, inst)
    return result


# =========================================================================
# Registering
# =========================================================================
def _place(m: Manager, comp: Component, after: bool) -> int:
    """Where a component goes: before those it's identical to, or, asked
    to be after, after all those of its type."""
    if after:
        same = [i for i, c in enumerate(m.components) if c.desc[0] == comp.desc[0]]
        return same[-1] + 1 if same else len(m.components)
    return next((i for i, c in enumerate(m.components) if c.desc[:4] == comp.desc[:4]),
                len(m.components))


def _register(t, comp: Component, flags: int) -> int:
    m = manager(t)
    visible = _visible(t, m)
    if flags & registerCmpNoDuplicates and any(c.desc[:4] == comp.desc[:4] for c in visible):
        return 0
    comp.id = next(m.ids)
    older = []
    if comp.register_flags & componentDoAutoVersion:
        if not comp.version:
            comp.version = (_connected(t, comp, -4) or 0) & 0xFFFFFFFF
        fields = 4 if comp.register_flags & componentAutoVersionIncludeFlags else 3
        same = [c for c in visible if c.desc[:fields] == comp.desc[:fields]]
        if any(c.version > comp.version for c in same):
            _free_code(comp)                # loaded to be asked its version
            return 0
        older = [c for c in same if c.version < comp.version]
    if comp.desc[3] & cmpWantsRegisterMessage:
        if _connected(t, comp, kComponentRegisterSelect) != 0:
            _free_code(comp)
            return 0
    for c in older:
        _unregister(t, c)
    m.components.insert(_place(m, comp, bool(flags & registerCompAfter)), comp)
    m.by_id[comp.id] = comp
    m.seed += 1
    return comp.id


def _unregister(t, comp: Component, request: bool = True) -> int:
    m = manager(t)
    if any(i.component is comp for i in m.instances.values()):
        return validInstancesExist
    if request and comp.register_flags & componentWantsUnregister:
        _connected(t, comp, kComponentUnregisterSelect)
    m.components.remove(comp)
    m.by_id.pop(comp.id, None)
    if request:
        _free_code(comp)
    m.seed += 1
    return noErr


def _free_code(comp: Component) -> None:
    if comp.code_handle:
        comp.owner.heap.dispose_handle(comp.code_handle)
        comp.code_handle = comp.entry = 0


def _new(t, desc: tuple, flags: int) -> Component:
    is_global = bool(flags & registerCmpGlobal)
    return Component(0, desc, t.process, is_global, 0 if is_global else t.cpu.a(5))


def _register_resource(t, cr: int, flags: int) -> int:
    rm = t.process.resources
    err = rm.error
    refnum = rm.HomeResFile(cr & t.mem.system.addr_mask)
    home = rm.node_of(refnum) if rm.error == noErr else None
    rm.error = err
    data = _handle_bytes(t, cr)
    if home is None or len(data) < THNG:
        return 0
    comp = _new(t, DESC.unpack_from(data), flags)
    specs = [SPEC.unpack_from(data, DESC.size + i * SPEC.size) for i in range(4)]
    comp.code, comp.home = specs[0], home
    comp.name, comp.info, comp.icon = (_home_resource(t, comp, *spec) or b"" for spec in specs[1:])
    if len(data) >= THNG + EXTENSION.size:
        comp.version, comp.register_flags, comp.icon_family = EXTENSION.unpack_from(data, THNG)
    return _register(t, comp, flags)


# =========================================================================
# The routines
# =========================================================================
def RegisterComponent(t, cd, entry, flags, name, info, icon):
    comp = _new(t, _description(t, cd), flags)
    comp.entry = entry & t.mem.system.addr_mask
    comp.name, comp.info, comp.icon = (_handle_bytes(t, h) for h in (name, info, icon))
    return _register(t, comp, flags)


def UnregisterComponent(t, ref):
    comp = _component(manager(t), ref)
    return _unregister(t, comp) if comp is not None else invalidComponentID


def CountComponents(t, looking):
    looking = _description(t, looking)
    return sum(_matches(c, looking) for c in _visible(t, manager(t)))


def FindNextComponent(t, ref, looking):
    looking = _description(t, looking)
    visible = _visible(t, manager(t))
    start = 0
    if ref:
        comp = _component(manager(t), ref)
        if comp not in visible:
            return 0
        start = visible.index(comp) + 1
    return next((c.id for c in visible[start:] if _matches(c, looking)), 0)


def GetComponentInfo(t, ref, cd, name, info, icon):
    comp = _component(manager(t), ref)
    if comp is None:
        return invalidComponentID
    if cd:
        t.mem.write(cd & t.mem.system.addr_mask, DESC.pack(*comp.desc))
    for h, data in ((name, comp.name), (info, comp.info), (icon, comp.icon)):
        if h:
            _fill(t, h & t.mem.system.addr_mask, data)
    return noErr


def GetComponentListModSeed(t):
    return manager(t).seed


def OpenComponent(t, ref):
    comp = _component(manager(t), ref)
    return _open(t, comp) if comp is not None else 0


def CloseComponent(t, ci):
    inst = manager(t).instances.get(ci)
    return _close(t, inst) if inst is not None else invalidComponentID


def _instance_routine(fn):
    """One of the routines on a connection's own values: an instance that
    isn't one, as a component instead, has none."""
    def routine(t, ci, *args):
        inst = manager(t).instances.get(ci)
        return fn(inst, *args) if inst is not None else 0
    return routine


def _take_error(inst):
    err, inst.error = inst.error, noErr
    return err


def _set(attr):
    return lambda inst, value: setattr(inst, attr, value)


def _component_value(fn):
    def routine(t, ref, *args):
        comp = _component(manager(t), ref)
        return fn(comp, *args) if comp is not None else 0
    return routine


def RegisterComponentResource(t, cr, flags):
    return _register_resource(t, cr, flags)


def CountComponentInstances(t, ref):
    comp = _component(manager(t), ref)
    return sum(i.component is comp for i in manager(t).instances.values())


def RegisterComponentResourceFile(t, refnum, flags):
    """Every 'thng' in the file; -1 if any of them isn't registered."""
    rm = t.process.resources
    current = rm.CurResFile()
    rm.UseResFile(refnum)
    if rm.error != noErr:
        return rm.error
    things = [rm.Get1IndResource(b"thng", i + 1) for i in range(rm.Count1Resources(b"thng"))]
    rm.UseResFile(current)
    registered = sum(bool(h and _register_resource(t, h, flags)) for h in things)
    return registered if registered == len(things) else -1


def OpenComponentResFile(t, ref):
    """Its file, opened for reading with a map of its own and made
    current: 0 if it hasn't one, -1 if it won't open."""
    comp = _component(manager(t), ref)
    if comp is None or comp.home is None:
        return 0
    rm = t.process.resources
    refnum = rm.open_node(comp.home, fsRdPerm)
    return refnum if rm.error == noErr else -1


def CloseComponentResFile(t, refnum):
    rm = t.process.resources
    rm.CloseResFile(refnum)
    return rm.error


def CaptureComponent(t, captured, capturing):
    """Taken out of searches, with a new ID to reach it by; none if it's
    captured already."""
    m = manager(t)
    comp = _component(m, captured)
    if comp is None or comp.captured:
        return 0
    del m.by_id[comp.id]
    comp.id, comp.captured = next(m.ids), True
    m.by_id[comp.id] = comp
    m.seed += 1
    return comp.id


def UncaptureComponent(t, ref):
    comp = _component(manager(t), ref)
    if comp is None:
        return invalidComponentID
    if not comp.captured:
        return componentNotCaptured
    comp.captured = False
    manager(t).seed += 1
    return noErr


def SetDefaultComponent(t, ref, flags):
    """Moved in front of the first of the others its flags say are like
    it."""
    m = manager(t)
    comp = _component(m, ref)
    if comp is None:
        return invalidComponentID
    fields = [0, 1, 2, 3]
    for flag, field in ((defaultComponentAnySubType, 1), (defaultComponentAnyManufacturer, 2),
                        (defaultComponentAnyFlags, 3)):
        if flags & flag:
            fields.remove(field)
    first = next(i for i, c in enumerate(m.components)
                 if all(c.desc[f] == comp.desc[f] for f in fields))
    m.components.remove(comp)
    m.components.insert(first, comp)
    m.seed += 1
    return noErr


def OpenDefaultComponent(t, kind, sub):
    looking = (kind, sub, 0, 0, 0)
    comp = next((c for c in _visible(t, manager(t)) if _matches(c, looking)), None)
    return _open(t, comp) if comp is not None else 0


def DelegateComponentCall(t, params, ci):
    inst = manager(t).instances.get(ci)
    return _request(t, inst, params & t.mem.system.addr_mask) if inst is not None else badComponentInstance


# D0: name, the arguments' codes, the result's, and the routine
ROUTINES = {
    0x01: ("RegisterComponent", "pLhLLL", "L", RegisterComponent),
    0x02: ("UnregisterComponent", "L", "e", UnregisterComponent),
    0x03: ("CountComponents", "p", "l", CountComponents),
    0x04: ("FindNextComponent", "Lp", "L", FindNextComponent),
    0x05: ("GetComponentInfo", "LpLLL", "e", GetComponentInfo),
    0x06: ("GetComponentListModSeed", "", "l", GetComponentListModSeed),
    0x07: ("OpenComponent", "L", "L", OpenComponent),
    0x08: ("CloseComponent", "L", "e", CloseComponent),
    0x0A: ("GetComponentInstanceError", "L", "e", _instance_routine(_take_error)),
    0x0B: ("SetComponentInstanceError", "Lh", "", _instance_routine(_set("error"))),
    0x0C: ("GetComponentInstanceStorage", "L", "L",
           _instance_routine(lambda inst: inst.storage)),
    0x0D: ("SetComponentInstanceStorage", "LL", "", _instance_routine(_set("storage"))),
    0x0E: ("GetComponentInstanceA5", "L", "L", _instance_routine(lambda inst: inst.a5)),
    0x0F: ("SetComponentInstanceA5", "LL", "", _instance_routine(_set("a5"))),
    0x10: ("GetComponentRefcon", "L", "L", _component_value(lambda c: c.refcon)),
    0x11: ("SetComponentRefcon", "LL", "", _component_value(_set("refcon"))),
    0x12: ("RegisterComponentResource", "Lh", "L", RegisterComponentResource),
    0x13: ("CountComponentInstances", "L", "l", CountComponentInstances),
    0x14: ("RegisterComponentResourceFile", "hh", "l", RegisterComponentResourceFile),
    0x15: ("OpenComponentResFile", "L", "h", OpenComponentResFile),
    0x18: ("CloseComponentResFile", "h", "e", CloseComponentResFile),
    0x1C: ("CaptureComponent", "LL", "L", CaptureComponent),
    0x1D: ("UncaptureComponent", "L", "e", UncaptureComponent),
    0x1E: ("SetDefaultComponent", "Lh", "e", SetDefaultComponent),
    0x21: ("OpenDefaultComponent", "LL", "L", OpenDefaultComponent),
    0x24: ("DelegateComponentCall", "pL", "L", DelegateComponentCall),
}


@raw(0xA82A)
def ComponentDispatch(t, word):
    cpu = t.cpu
    selector = cpu.d(0) & 0xFF
    if selector == 0:
        _client_call(t)
        return
    if selector == 0xFF:
        _call_function(t)
        return
    routine = ROUTINES.get(selector)
    if routine is None:
        t.unimplemented(f"Component Manager routine ${selector:02X}", word)
        cpu.set_d(0, badComponentSelector)
        return
    _, codes, result, fn = routine
    args, at = pop_args(t.mem, cpu.sp, codes)
    value = fn(t, *args)
    cpu.sp = at
    put_result(t.mem, at, result, value)


# =========================================================================
# An application quitting
# =========================================================================
def close_all(t) -> None:
    """As an application quits: the connections it left open are closed
    and the components it registered unregistered, with their own and
    others' connections to them."""
    m = _managers.get(t.system)
    if m is None:
        return
    for inst in [i for i in m.instances.values() if i.owner is t.process]:
        _close(t, inst)
    for comp in [c for c in m.components if c.owner is t.process]:
        for inst in [i for i in m.instances.values() if i.component is comp]:
            _close(t, inst)
        _unregister(t, comp)


def forget(t) -> None:
    """Once it's gone, what close_all didn't get to, its code not being
    called again."""
    m = _managers.get(t.system)
    if m is None:
        return
    for ci in [ci for ci, i in m.instances.items()
               if i.owner is t.process or i.component.owner is t.process]:
        del m.instances[ci]
    for comp in [c for c in m.components if c.owner is t.process]:
        _unregister(t, comp, request=False)
