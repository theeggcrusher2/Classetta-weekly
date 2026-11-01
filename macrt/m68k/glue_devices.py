"""
The Device Manager for drivers in 68k code (IM II, chapter 6): an
application's own DRVR resources, such as SmoothTalker's speech drivers.
macrt's own drivers are elsewhere: the Sound Driver is macrt.m68k.sound's,
.Print, .Sony and the serial ports' glue_files'.

A driver is in the unit table at UTableBase as a handle to its device
control entry (DCE), at its unit number; its reference number is
-(unit + 1).  Opening a driver by name loads the DRVR resource of that
name, whose ID is its unit number, makes it a DCE and calls its open
routine.  Read and Write call its prime routine, Control and KillIO its
control routine, Status its status routine and Close its close routine,
each with the parameter block in A0 and the DCE in A1.

Requests wait their turn in the DCE's queue, as on a Mac.  A driver says
it's done with one by jumping to IODone through JIODone, with the result
in D0: IODone takes it off the queue, sets its ioResult, calls an
asynchronous request's completion routine and starts the driver on the
next.  A synchronous call waits for that in 68k code, the Machine's
wait_for_io, so interrupts and VBL tasks run meanwhile; an immediate one
is done when the routine returns, with the result in D0.

The System file's drivers are found too, as on a Mac: its desk
accessories, which the Desk Manager opens (glue_desk) and sends Control
calls to from here, and the drivers that came on the disk a program
started from, such as Speller Bee's speech driver, .SAY (see
System._disk_drivers).  Not those macrt has its own of, nor AppleTalk's,
for hardware it hasn't got (UNLOADED_DRIVERS): opening one of those fails
as if there were no such driver.  A program can also make a device
control entry itself, with DrvrInstall, and open it by its driver's
name.  The drivers an application opens or installs go when it quits.

Of macrt's own drivers, two kinds have a device control entry in the
table.  The screen's video driver, on a colour machine, has one as a slot
card's driver has (Inside Macintosh V, the Device Manager): with the
card's slot, its sResource's ID and its device base after dCtlMenu.
Programs find it through the main GDevice's gdRefNum and take the slot
from it for SlotVInstall (Powerball).  And a CD-ROM's driver has one, at
the reference number its volume's drive gives, named as the Apple CD-ROM
driver is, .AppleCD (Technical Note DV 22, "CD-ROM Driver Calls"): Lode
Runner: The Legend Returns takes its volume's driver from its VCB's
vcbDRefNum, looks it up with GetDCtlEntry and compares the name with
that.  Each one's driver is a header with the driver's name, for
programs that look for a driver by it, and the calls to it are macrt's
own still, glue_video's and glue_files'.
"""

from __future__ import annotations

from contextlib import contextmanager

from ..memory import ADDRESS_MASK, from_mac
from ..toolbox import lowmem
from ..toolbox.gdevice import VIDEO_DRIVER, VIDEO_SLOT
from ..toolbox.resources import UNWRITTEN
from ..toolbox.system import UNLOADED_DRIVERS
from .dispatch import raw

# The unit table's length: IM Devices' reserved unit numbers go up to 127,
# 48 on being for slot and other drivers (Disk Copy's goes there).
UNITS = 128
FIRST_SPARE = 32                # where a driver goes whose own unit is taken
LOW_DCES = 16                   # the DCEs made with the unit table, low in memory
CD_DRIVER = b".AppleCD"         # a CD-ROM's driver's name (Technical Note DV 22)

noErr = 0
controlErr = -17
statusErr = -18
readErr = -19
writErr = -20
badUnitErr = -21
dRemovErr = -25
dInstErr = -26
abortErr = -27
notOpenErr = -28
unitTblFullErr = -29
memFullErr = -108

# Parameter blocks (IM II, chapter 6)
qLink = 0
qType = 4
ioTrap = 6
ioCmdAddr = 8
ioCompletion = 12
ioResult = 16
ioNamePtr = 18
ioRefNum = 24
csCode = 26
csParam = 28
ioPosMode = 44
ioPosOffset = 46
ioQType = 2
killCode = 1
fsFromStart, fsFromMark = 1, 3
PB_SIZE = 50

# The calls' trap words
OPEN, CLOSE, CONTROL = 0xA000, 0xA001, 0xA004

# Trap word bits
IMMEDIATE = 0x200
ASYNC = 0x400

# The device control entry
dCtlDriver = 0
dCtlFlags = 4
dCtlQHead = 8                   # dCtlQHdr is at 6: its flags, then the queue's head and tail
dCtlQTail = 12
dCtlPosition = 16
dCtlRefNum = 24
dCtlCurTicks = 26
dCtlWindow = 30
dCtlDelay = 34
dCtlEMask = 36
dCtlMenu = 38
DCE_SIZE = 40
dOpened, dRAMBased, drvrActive = 0x20, 0x40, 0x80
# A slot device's goes on (Inside Macintosh V, the Device Manager)
dCtlSlot, dCtlSlotId, dCtlDevBase = 40, 41, 42
SLOT_DCE_SIZE = 52

# The driver's header
drvrFlags = 0
drvrDelay = 2
drvrEMask = 4
drvrMenu = 6
drvrOpen, drvrPrime, drvrCtl, drvrStatus, drvrClose = 8, 10, 12, 14, 16
drvrName = 18
dReadEnable, dWritEnable, dCtlEnable, dStatEnable = 0x100, 0x200, 0x400, 0x800
dNeedTime = 0x2000

# Each call: the routine it calls and the bit that lets a driver be asked, with
# the error if it can't be
ROUTINES = {
    2: (drvrPrime, dReadEnable, readErr),
    3: (drvrPrime, dWritEnable, writErr),
    4: (drvrCtl, dCtlEnable, controlErr),
    5: (drvrStatus, dStatEnable, statusErr),
}


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def init(machine) -> None:
    """An empty unit table, and IODone.  The table and LOW_DCES device
    control entries for drivers to come are made in the room the System
    kept for them at the bottom of its heap, under 32K, as a Mac's are:
    Speller Bee's .SAY driver saves and restores registers a word each, and
    goes to IODone with its DCE's address in A1 a word extended."""
    mem, system = machine.mem, machine.system
    heap = system.sys_heap
    heap.dispose_ptr(system.device_room)
    table = heap.new_ptr(4 * UNITS, clear=True)
    machine.low_dces = [heap.new_handle(DCE_SIZE, clear=True) for _ in range(LOW_DCES)]
    for dh in machine.low_dces:
        heap.lock(dh)
    machine.spare_dces = list(machine.low_dces)
    mem.w32(lowmem.UTableBase, table)
    mem.w16(lowmem.UnitNtryCnt, UNITS)
    mem.w32(lowmem.JIODone, machine.io_done)
    machine.own_units = set()
    if system.model.color:
        _video_dce(machine)
    files = system.files
    files.add_driver = lambda vol: _disk_driver(machine, vol)
    for vol in files.volumes + files.offline:
        _disk_driver(machine, vol)


def _own_dce(machine, refnum: int, name: bytes, size: int = DCE_SIZE) -> int:
    """A device control entry in the table at refnum for one of macrt's
    own drivers: open, for Control and Status calls, its driver a header
    that has only its name.  Its address."""
    mem, heap = machine.mem, machine.system.sys_heap
    flags = dCtlEnable | dStatEnable
    drvr = heap.new_ptr(drvrName + 2 + len(name) & ~1, clear=True)
    mem.w16(drvr + drvrFlags, flags)
    mem.w8(drvr + drvrName, len(name))
    mem.write(drvr + drvrName + 1, name)
    dh = heap.new_handle(size, clear=True)
    heap.lock(dh)
    dce = mem.r32(dh) & ADDRESS_MASK
    mem.w32(dce + dCtlDriver, drvr)
    mem.w16(dce + dCtlFlags, flags | dOpened)
    mem.w16(dce + dCtlRefNum, refnum)
    mem.w32((mem.r32(lowmem.UTableBase) & ADDRESS_MASK) + 4 * ~refnum, dh)
    machine.own_units.add(~refnum)
    return dce


def _video_dce(machine) -> None:
    """The video driver's device control entry, with the card's slot
    fields as the Slot Manager has them (glue_slots)."""
    from .glue_slots import VIDEO, VIDEO_NAME
    mem = machine.mem
    dce = _own_dce(machine, VIDEO_DRIVER, b"." + VIDEO_NAME, SLOT_DCE_SIZE)
    mem.w8(dce + dCtlSlot, VIDEO_SLOT)
    mem.w8(dce + dCtlSlotId, VIDEO)
    mem.w32(dce + dCtlDevBase, machine.system.screen_bits.base)


def _disk_driver(machine, vol) -> None:
    """A CD-ROM's driver's device control entry, the first time a volume
    on one is mounted; it stays when the volume goes."""
    unit = ~vol.driver
    table = machine.mem.r32(lowmem.UTableBase) & ADDRESS_MASK
    if vol.cdrom and 0 <= unit < UNITS and not machine.mem.r32(table + 4 * unit):
        _own_dce(machine, vol.driver, CD_DRIVER)


def _dce(t, unit: int) -> int:
    """The unit's DCE, 0 if it has none: for macrt's own drivers, but
    the video driver and CD-ROMs', it hasn't."""
    m = t.mem
    table = m.r32(lowmem.UTableBase) & ADDRESS_MASK
    if not table or not 0 <= unit < m.r16(lowmem.UnitNtryCnt):
        return 0
    h = m.r32(table + 4 * unit) & ADDRESS_MASK
    return m.r32(h) & ADDRESS_MASK if h else 0


def _driver(t, dce: int) -> int:
    d = t.mem.r32(dce + dCtlDriver) & ADDRESS_MASK
    if t.mem.r16(dce + dCtlFlags) & dRAMBased:
        d = t.mem.r32(d) & ADDRESS_MASK
    return d


def _dce_for(t, pb: int) -> int:
    """The DCE of the driver the call is for, 0 if it's not one of these."""
    unit = ~t.mem.r16s(pb + ioRefNum)
    return _dce(t, unit) if unit >= 0 else 0


def call(t, w: int, built_in) -> bool:
    """Do Device Manager call w, A0 its parameter block, if it's for a
    driver in 68k code; False if it isn't, for the File Manager's or
    macrt's own drivers.  built_in has those, by name: their refnums."""
    pb = t.cpu.a(0) & ADDRESS_MASK
    n = w & 0xFF
    if n == 0:
        return _open(t, pb, w, built_in)
    dce = _dce_for(t, pb)
    refnum = t.mem.r16s(pb + ioRefNum)
    if not dce or refnum in built_in.values() or ~refnum in t.machine.own_units:
        return False
    if n == 1:
        _close(t, pb, w, dce)
    elif not t.mem.r16(dce + dCtlFlags) & dOpened:
        finish(t, pb, w, notOpenErr)
    elif n == 6:
        _kill(t, pb, w, dce)
    else:
        _request(t, pb, w, dce, *ROUTINES[n])
    return True


# =========================================================================
# Opening and closing
# =========================================================================
def _open(t, pb: int, w: int, built_in) -> bool:
    m = t.mem
    p = m.r32(pb + ioNamePtr) & ADDRESS_MASK
    name = m.r_pstr(p) if p else b""
    if not name.startswith(b".") or from_mac(name).lower() in built_in:
        return False
    unit = _find(t, name)
    if unit is None:
        unit = _install(t, name, {~r for r in built_in.values()},
                        system_too=from_mac(name).lower() not in UNLOADED_DRIVERS)
        if unit is None:
            return False                    # no such driver: perhaps a file
    if unit < 0:
        finish(t, pb, w, unit)             # an error
        return True
    dce = _dce(t, unit)
    m.w16(pb + ioRefNum, ~unit)
    err = noErr
    flags = m.r16(dce + dCtlFlags)
    if not flags & dOpened:
        err = _call_routine(t, pb, w, dce, drvrOpen)
        if err == noErr:
            m.w16(dce + dCtlFlags, m.r16(dce + dCtlFlags) | dOpened)
    finish(t, pb, w, err)
    return True


def _find(t, name: bytes, owner=None) -> int | None:
    """The unit of the driver of that name, if one's in the table: one
    owner opened, if it's given."""
    want = from_mac(name).lower()
    for unit in range(t.mem.r16(lowmem.UnitNtryCnt)):
        dce = _dce(t, unit)
        if (dce and from_mac(t.mem.r_pstr(_driver(t, dce) + drvrName)).lower() == want
                and (owner is None or t.machine.drivers.get(unit) is owner)):
            return unit
    return None


def _install(t, name: bytes, taken, system_too: bool = False) -> int | None:
    """Load the application's DRVR resource of that name, or the System
    file's too if system_too, into the unit table: its unit, None if
    there's no such driver, or an error.  If its own unit is taken, or
    one of those in taken, it goes in a spare one."""
    m, system = t.mem, t.system
    rm = t.process.resources
    with system.lock:
        err = rm.error
        h = rm.GetNamedResource("DRVR", name)
        found = h and (system_too or rm.HomeResFile(h) != rm.sys_refnum)
        unit = rm.GetResInfo(h)[0] if found else 0
        rm.error = err                      # the program's ResError isn't for this
        if not found:
            return None
        table = m.r32(lowmem.UTableBase) & ADDRESS_MASK
        count = m.r16(lowmem.UnitNtryCnt)
        if not 0 <= unit < count or unit in taken or m.r32(table + 4 * unit):
            free = [u for u in range(FIRST_SPARE, count)
                    if u not in taken and not m.r32(table + 4 * u)]
            if not free:
                return unitTblFullErr
            unit = free[0]
        heap = system.heap_of(h)
        heap.set_purgeable(h, False)
        heap.lock(h)
        spare = t.machine.spare_dces
        dh = spare.pop(0) if spare else system.sys_heap.new_handle(DCE_SIZE, clear=True)
        if not dh:
            return memFullErr
        system.sys_heap.lock(dh)
        dce = m.r32(dh) & ADDRESS_MASK
        m.fill(dce, DCE_SIZE)
        drvr = m.r32(h) & ADDRESS_MASK
        m.w32(dce + dCtlDriver, h)
        m.w16(dce + dCtlFlags, m.r16(drvr + drvrFlags) & 0xFF00 | dRAMBased)
        m.w16(dce + dCtlRefNum, ~unit)
        m.w16(dce + dCtlDelay, m.r16(drvr + drvrDelay))
        m.w16(dce + dCtlEMask, m.r16(drvr + drvrEMask))
        m.w16(dce + dCtlMenu, m.r16(drvr + drvrMenu))
        m.w32(table + 4 * unit, dh)
        t.machine.drivers[unit] = t.process
        return unit


def _close(t, pb: int, w: int, dce: int) -> None:
    m = t.mem
    err = noErr
    if m.r16(dce + dCtlFlags) & dOpened:
        err = _call_routine(t, pb, w, dce, drvrClose)
        if err == noErr:
            m.w16(dce + dCtlFlags, m.r16(dce + dCtlFlags) & ~dOpened)
    finish(t, pb, w, err)


def forget(t) -> None:
    """t's application is quitting: take the drivers it opened out of
    the unit table.  (Its resource file, which they came from, closes.)"""
    for unit in units_of(t):
        _remove(t, unit)


def units_of(t) -> list[int]:
    """The units of the drivers t's application has in the table."""
    return [unit for unit, process in list(t.machine.drivers.items()) if process is t.process]


def _remove(t, unit: int, code: bool = True) -> None:
    """Take the driver at unit out of the table, and, if code, let its
    code go: it can be purged once no other driver in the table runs it."""
    m, system = t.mem, t.system
    table = m.r32(lowmem.UTableBase) & ADDRESS_MASK
    with system.lock:
        t.machine.drivers.pop(unit, None)
        t.machine.own_units.discard(unit)
        dh = m.r32(table + 4 * unit) & ADDRESS_MASK
        m.w32(table + 4 * unit, 0)
        if not dh:
            return
        h = m.r32((m.r32(dh) & ADDRESS_MASK) + dCtlDriver) & ADDRESS_MASK
        if dh in t.machine.low_dces:
            t.machine.spare_dces.append(dh)
        else:
            system.sys_heap.dispose_handle(dh)
        if not code:
            return
        others = (_dce(t, u) for u in range(m.r16(lowmem.UnitNtryCnt)))
        if any(dce and m.r32(dce + dCtlDriver) & ADDRESS_MASK == h for dce in others):
            return
        e = system.sys_map.find_handle(h)
        if e is not None and e.offset == UNWRITTEN:
            return                          # a disk System's, only in memory: kept
        heap = system.find_heap(h)
        if heap is not None and heap.is_handle(h):
            heap.unlock(h)
            heap.set_purgeable(h, True)


# =========================================================================
# A program's own device control entries (Inside Macintosh: Devices,
# Device Manager, "Writing and Installing Device Drivers")
# =========================================================================
@raw(0xA03D)
def DrvrInstall(t, word):
    """DriverInstall: a new device control entry for the driver at A0, in
    the system heap and in the unit table at refnum D0, in place of any
    that was there, with that refnum and dRAMBased set and nothing else:
    the program fills in the rest and opens it.  Bit 10 of the trap
    word, DriverInstallReserveMem's, asks for room to be made low in the
    heap first.  Burtonia, which calls it with bit 8 set, so that A0
    isn't restored, takes the new entry's handle from A0 afterwards;
    it's left there for it."""
    m, cpu, system = t.mem, t.cpu, t.system
    refnum = _s16(cpu.d(0))
    unit = ~refnum
    table = m.r32(lowmem.UTableBase) & ADDRESS_MASK
    if refnum >= 0 or not table or unit >= m.r16(lowmem.UnitNtryCnt):
        cpu.set_d(0, badUnitErr & 0xFFFFFFFF)
        return
    heap = system.sys_heap
    with system.lock:
        if m.r32(table + 4 * unit):
            _remove(t, unit, code=False)
        if word & 0x400:
            heap.reserve(DCE_SIZE)
        dh = heap.new_handle(DCE_SIZE, clear=True)
        if not dh:
            cpu.set_d(0, memFullErr & 0xFFFFFFFF)
            return
        dce = m.r32(dh) & ADDRESS_MASK
        m.w16(dce + dCtlFlags, dRAMBased)
        m.w16(dce + dCtlRefNum, refnum)
        m.w32(table + 4 * unit, dh)
        t.machine.drivers[unit] = t.process
    if word & 0x100:
        cpu.set_a(0, dh)
    cpu.set_d(0, noErr)


@raw(0xA03E)
def DrvrRemove(t, word):
    """DriverRemove: take the closed driver at refnum D0 out of the unit
    table and dispose of its device control entry; one that's open
    stays, with dRemovErr.  Its code is let go if it's a resource, as
    ReleaseResource would; a block of the program's own it keeps."""
    m, cpu = t.mem, t.cpu
    unit = ~_s16(cpu.d(0))
    if not 0 <= unit < m.r16(lowmem.UnitNtryCnt):
        cpu.set_d(0, badUnitErr & 0xFFFFFFFF)
        return
    dce = _dce(t, unit)
    flags = m.r16(dce + dCtlFlags) if dce else 0
    if flags & dOpened:
        cpu.set_d(0, dRemovErr & 0xFFFFFFFF)
        return
    code = bool(flags & dRAMBased) and t.process.resources.is_resource(
        m.r32(dce + dCtlDriver) & ADDRESS_MASK)
    _remove(t, unit, code=code)
    cpu.set_d(0, noErr)


# =========================================================================
# Calls from macrt: the Desk Manager's
# =========================================================================
def open_driver(t, name: bytes, taken=()) -> int:
    """Open the driver of that name, the System file's or the application's,
    for the application, as OpenDeskAcc does: its unit, or an error.  One
    it already has open is just found.  The units in taken aren't to be
    used: macrt's own drivers have them."""
    m = t.mem
    unit = _find(t, name, t.process)
    if unit is None:
        unit = _install(t, name, taken, system_too=True)
        if unit is None:
            return dInstErr
    if unit < 0:
        return unit
    dce = _dce(t, unit)
    if m.r16(dce + dCtlFlags) & dOpened:
        return unit
    with on_stack(t, PB_SIZE) as pb:
        m.w16(pb + ioRefNum, ~unit)
        err = _call_routine(t, pb, OPEN, dce, drvrOpen)
    if err != noErr:
        _remove(t, unit)
        return err
    m.w16(dce + dCtlFlags, m.r16(dce + dCtlFlags) | dOpened)
    return unit


def close_driver(t, unit: int) -> int:
    """Close the driver at unit, as CloseDeskAcc does, and take it out of
    the table: its close routine's result."""
    m = t.mem
    dce = _dce(t, unit)
    if not dce:
        return notOpenErr
    err = noErr
    if m.r16(dce + dCtlFlags) & dOpened:
        with on_stack(t, PB_SIZE) as pb:
            m.w16(pb + ioRefNum, ~unit)
            err = _call_routine(t, pb, CLOSE, dce, drvrClose)
    if err == noErr:
        _remove(t, unit)
    return err


def control(t, unit: int, code: int, param: int = 0) -> int:
    """A Control call to the driver at unit, csCode code with param at
    csParam, as the Desk Manager makes one: its result.  A driver that's
    busy isn't asked, as one isn't that's called SystemTask in the middle
    of a request."""
    m = t.mem
    dce = _dce(t, unit)
    flags = m.r16(dce + dCtlFlags) if dce else 0
    if not flags & dOpened:
        return notOpenErr
    if not flags & dCtlEnable or flags & drvrActive:
        return controlErr
    with on_stack(t, PB_SIZE) as pb:
        m.w16(pb + ioTrap, CONTROL)
        m.w16(pb + ioRefNum, m.r16(dce + dCtlRefNum))
        m.w16(pb + csCode, code)
        m.w32(pb + csParam, param)
        _queue(t, dce, pb, CONTROL, drvrCtl)
        _start(t, dce)
        if m.r32(dce + dCtlQHead) & ADDRESS_MASK != pb:
            return m.r16s(pb + ioResult)
        # Its control routine returned without going through IODone: so
        # it's done, since nothing will wait for it here.
        _dequeue(t, dce, pb)
        m.w16(dce + dCtlFlags, m.r16(dce + dCtlFlags) & ~drvrActive)
        return noErr


@contextmanager
def on_stack(t, size: int):
    """size bytes, cleared, below the stack pointer: a parameter block for
    a call made from here, which only lasts as long as it does."""
    cpu = t.cpu
    sp = cpu.sp
    p = (sp - size) & ~3
    t.mem.write(p, bytes(size))
    cpu.sp = p
    try:
        yield p
    finally:
        cpu.sp = sp


# =========================================================================
# Requests
# =========================================================================
def _request(t, pb: int, w: int, dce: int, routine: int, enable: int, refused: int) -> None:
    m = t.mem
    if not m.r16(dce + dCtlFlags) & enable:
        finish(t, pb, w, refused)
        return
    if routine == drvrPrime:
        mode, offset = m.r16(pb + ioPosMode) & 3, m.r32s(pb + ioPosOffset)
        if mode == fsFromStart:
            m.w32(dce + dCtlPosition, offset)
        elif mode == fsFromMark:
            m.w32(dce + dCtlPosition, m.r32s(dce + dCtlPosition) + offset)
    if w & IMMEDIATE:
        finish(t, pb, w, _call_routine(t, pb, w, dce, routine))
        return
    _queue(t, dce, pb, w, routine)
    if not m.r16(dce + dCtlFlags) & drvrActive:
        _start(t, dce)
    wait(t, pb, w)


def _queue(t, dce: int, pb: int, w: int, routine: int) -> None:
    """A request for the routine waits its turn in the driver's queue."""
    m = t.mem
    m.w16(pb + ioTrap, w)
    m.w32(pb + ioCmdAddr, _driver(t, dce) + m.r16(_driver(t, dce) + routine))
    m.w16(pb + ioResult, 1)                 # in progress
    m.w32(pb + qLink, 0)
    m.w16(pb + qType, ioQType)
    tail = m.r32(dce + dCtlQTail) & ADDRESS_MASK
    m.w32(tail + qLink if tail else dce + dCtlQHead, pb)
    m.w32(dce + dCtlQTail, pb)


def _start(t, dce: int) -> None:
    """Start the driver on the request at the head of its queue."""
    m = t.mem
    pb = m.r32(dce + dCtlQHead) & ADDRESS_MASK
    if pb:
        m.w16(dce + dCtlFlags, m.r16(dce + dCtlFlags) | drvrActive)
        t.call_with(m.r32(pb + ioCmdAddr) & ADDRESS_MASK, a0=pb, a1=dce)


def wait(t, pb: int, w: int) -> None:
    """The caller's D0: noErr for an asynchronous request, or a synchronous
    one's result once it's done, which it waits for in 68k code if it's
    still going."""
    cpu = t.cpu
    if w & ASYNC:
        cpu.set_d(0, noErr)
    elif t.mem.r16s(pb + ioResult) <= 0:
        cpu.set_d(0, t.mem.r16s(pb + ioResult) & 0xFFFFFFFF)
    else:
        cpu.push32(cpu.pc)
        cpu.pc = t.machine.wait_for_io


def _kill(t, pb: int, w: int, dce: int) -> None:
    """KillIO: the control routine's killCode, then every request still
    waiting is done, with abortErr."""
    m = t.mem
    m.w16(pb + csCode, killCode)
    err = _call_routine(t, pb, w | IMMEDIATE, dce, drvrCtl)
    if err == noErr:
        m.w16(dce + dCtlFlags, m.r16(dce + dCtlFlags) & ~drvrActive)
        while (head := m.r32(dce + dCtlQHead) & ADDRESS_MASK):
            _dequeue(t, dce, head)
            done(t, head, abortErr)
    finish(t, pb, w, err)


def _dequeue(t, dce: int, pb: int) -> None:
    m = t.mem
    after = m.r32(pb + qLink) & ADDRESS_MASK
    m.w32(dce + dCtlQHead, after)
    if not after:
        m.w32(dce + dCtlQTail, 0)


def io_done(t) -> None:
    """IODone, where a driver's routine goes through JIODone when it's done
    with the request at the head of its queue, with its DCE in A1 and the
    result in D0; and then back to whoever called the routine."""
    cpu, m = t.cpu, t.mem
    dce = cpu.a(1) & ADDRESS_MASK
    err = _s16(cpu.d(0))
    cpu.pc = cpu.pop32()
    flags = m.r16(dce + dCtlFlags)
    pb = m.r32(dce + dCtlQHead) & ADDRESS_MASK
    if not flags & drvrActive or not pb:
        return                              # an immediate request's: D0 has it
    _dequeue(t, dce, pb)
    m.w16(dce + dCtlFlags, flags & ~drvrActive)
    done(t, pb, err)
    _start(t, dce)


# =========================================================================
# Calling a driver
# =========================================================================
def _call_routine(t, pb: int, w: int, dce: int, routine: int) -> int:
    """Call one of the driver's routines on the spot: its result.  An open
    routine's is what it leaves in ioResult, noErr if nothing, not D0: the
    Device Manager sets D0 to 0 when one returns (Inside Macintosh II, the
    Device Manager), so RamDisk+'s leaves in it what its caller had."""
    m = t.mem
    drvr = _driver(t, dce)
    addr = drvr + m.r16(drvr + routine)
    m.w16(pb + ioTrap, w)
    m.w32(pb + ioCmdAddr, addr)
    opening = routine == drvrOpen
    m.w16(pb + ioResult, noErr if opening else 1)
    d0 = _s16(t.call_with(addr, a0=pb, a1=dce))
    return m.r16s(pb + ioResult) if opening else d0


def done(t, pb: int, err: int) -> None:
    """A request's done: its result, and its completion routine if it was asynchronous."""
    m = t.mem
    m.w16(pb + ioResult, err)
    completion = m.r32(pb + ioCompletion) & ADDRESS_MASK
    if m.r16(pb + ioTrap) & ASYNC and completion:
        t.call_with(completion, a0=pb, d0=err & 0xFFFFFFFF)


def finish(t, pb: int, w: int, err: int) -> None:
    """A call done on the spot: its result in ioResult and D0."""
    t.mem.w16(pb + ioTrap, w)
    done(t, pb, err)
    t.cpu.set_d(0, err & 0xFFFFFFFF)
