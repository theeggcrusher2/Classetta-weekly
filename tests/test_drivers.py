"""A program's own device control entries, DriverInstall and DriverRemove
(Inside Macintosh: Devices, the Device Manager); the video driver's entry;
what's set up for an application at launch; and SEvtEnb, which the THINK C
console clears so that GetNextEvent leaves its console's events to it
(Burtonia)."""

import struct

from macrt.m68k import glue_desk, glue_slots
from macrt.m68k import glue_devices as dev
from macrt.toolbox import lowmem
from macrt.toolbox.gdevice import VIDEO_DRIVER, VIDEO_SLOT

from guest import Guest, Processor

CONSOLE = -28                           # Burtonia's .console, at unit 27
INSTALL, INSTALL_KEEPING_A0 = b"\xa0\x3d", b"\xa1\x3d"
REMOVE = b"\xa0\x3e"
STATUS = b"\xa0\x05"
GET_GAMMA = 8


def processor(**system) -> Processor:
    """A Processor with an empty unit table, as the runtime sets it up."""
    p = Processor(**system)
    p.machine.system = p.system
    p.machine.io_done = p.ptr(b"\x4e\x75")
    p.machine.drivers = {}
    dev.init(p.machine)
    return p


def entry(p, refnum: int) -> int:
    """The unit table's handle for refnum."""
    return p.mem.r32(p.mem.r32(lowmem.UTableBase) + 4 * ~refnum)


def call(p, code: bytes, refnum: int, a0: int = 0) -> int:
    p.cpu.set_d(0, refnum & 0xFFFFFFFF)
    p.cpu.set_a(0, a0)
    p.run(code)
    return p.cpu.d(0) - (1 << 32 if p.cpu.d(0) & 0x80000000 else 0)


def test_drvr_install_puts_a_new_entry_in_the_unit_table(qapp):
    p = processor()
    driver = p.ptr(bytes(0x2A))
    assert call(p, INSTALL, CONSOLE, driver) == dev.noErr
    dh = entry(p, CONSOLE)
    assert p.system.sys_heap.is_handle(dh)
    assert p.cpu.a(0) == driver                     # A0 restored, without bit 8
    dce = p.mem.r32(dh)
    # The refnum and dRAMBased, and nothing else: not even the driver.
    assert p.mem.r16(dce + dev.dCtlFlags) == dev.dRAMBased
    assert p.mem.r16s(dce + dev.dCtlRefNum) == CONSOLE
    assert p.mem.read(dce, dev.DCE_SIZE).count(0) == dev.DCE_SIZE - 3
    assert p.machine.drivers[~CONSOLE] is p.process
    # Again, with bit 8 set, as Burtonia calls it: a new one in place of
    # that one, once filled in, and its handle left in A0.
    p.mem.w32(dce + dev.dCtlDriver, driver)
    assert call(p, INSTALL_KEEPING_A0, CONSOLE, driver) == dev.noErr
    dh = entry(p, CONSOLE)
    assert p.cpu.a(0) == dh and p.system.sys_heap.is_handle(dh)
    assert p.mem.r32(p.mem.r32(dh) + dev.dCtlDriver) == 0


def test_drvr_install_wants_a_refnum_in_the_table(qapp):
    p = processor()
    for refnum in (0, 5, -dev.UNITS - 1):
        assert call(p, INSTALL, refnum, p.ptr(bytes(0x2A))) == dev.badUnitErr
    assert p.machine.drivers == {}


def test_the_unit_table_goes_up_to_unit_127(qapp):
    """IM Devices' reserved unit numbers: 48 through 127 for slot and other
    drivers.  Disk Copy puts its own driver at 48 on, in the table."""
    p = processor()
    assert p.mem.r16(lowmem.UnitNtryCnt) == 128
    for refnum in (-49, -128):
        assert call(p, INSTALL, refnum, p.ptr(bytes(0x2A))) == dev.noErr
        assert p.system.sys_heap.is_handle(entry(p, refnum))


def test_the_video_driver_has_a_slot_devices_entry(qapp):
    """On a colour machine the screen's video driver has an entry in the
    unit table, with its card's slot fields (Inside Macintosh V, the Device
    Manager): Powerball takes the slot from it for SlotVInstall.  Its calls
    are still macrt's own."""
    assert entry(processor(), VIDEO_DRIVER) == 0          # a Plus has none
    p = processor(model="ii")
    m = p.mem
    dce = m.r32(entry(p, VIDEO_DRIVER)) & m.system.addr_mask
    assert m.r16s(dce + dev.dCtlRefNum) == VIDEO_DRIVER
    assert m.r16(dce + dev.dCtlFlags) & dev.dOpened
    assert m.r8(dce + dev.dCtlSlot) == VIDEO_SLOT
    assert m.r8(dce + dev.dCtlSlotId) == glue_slots.VIDEO
    assert m.r32(dce + dev.dCtlDevBase) == p.system.screen_bits.base
    driver = m.r32(dce + dev.dCtlDriver)
    assert m.r_pstr(driver + dev.drvrName) == b".Display_Video_Apple_TFB"
    pb, record = p.ptr(bytes(50)), p.ptr(bytes(4))
    m.write(pb + 24, struct.pack(">hhL", VIDEO_DRIVER, GET_GAMMA, record))
    assert call(p, STATUS, 0, pb) == dev.noErr
    assert m.r32(record) != 0                              # the driver's gamma table


def test_drvr_remove_takes_out_a_closed_driver_but_not_an_open_one(qapp):
    p = processor()
    code = p.system.sys_heap.new_handle(0x2A)
    p.system.sys_heap.lock(code)
    call(p, INSTALL, CONSOLE, p.ptr(bytes(0x2A)))
    dh = entry(p, CONSOLE)
    dce = p.mem.r32(dh)
    p.mem.w32(dce + dev.dCtlDriver, code)
    p.mem.w16(dce + dev.dCtlFlags, dev.dRAMBased | dev.dOpened)
    assert call(p, REMOVE, CONSOLE) == dev.dRemovErr
    assert entry(p, CONSOLE) == dh
    p.mem.w16(dce + dev.dCtlFlags, dev.dRAMBased)
    assert call(p, REMOVE, CONSOLE) == dev.noErr
    assert entry(p, CONSOLE) == 0 and p.machine.drivers == {}
    assert not p.system.sys_heap.is_handle(dh)
    # The program's own code, not a resource, is left as it was.
    assert p.system.sys_heap.is_handle(code)
    assert p.system.sys_heap.get_state(code) == 0x80        # locked, not purgeable
    assert call(p, REMOVE, 1) == dev.badUnitErr


def test_an_application_starts_with_quickdraw_and_the_window_manager_to_set_up(qapp):
    """A 68k application's world, with no QuickDraw globals made for it,
    has WWExist and QDExist non-zero until it calls InitGraf and
    InitWindows: Burtonia sets up the Toolbox only then."""
    g = Guest()
    part = g.system.open_partition("Burtonia", qd_globals=False)
    assert part.world.read(lowmem.WWExist, 2) == b"\xff\xff"      # and QDExist
    part = g.system.open_partition("Python", qd_globals=True)
    assert part.world.read(lowmem.WWExist, 2) == b"\0\0"
    g.mem.w16(lowmem.WWExist, 0xFFFF)
    port = g.mem.r32(lowmem.WMgrPort)
    g("InitWindows")
    assert g.mem.r8(lowmem.WWExist) == 0
    assert g.mem.r32(lowmem.WMgrPort) == port       # still there: kept


def test_sevtenb_clear_stops_only_get_next_events_system_event(qapp, monkeypatch):
    """Burtonia's THINK C console clears SEvtEnb, so that GetNextEvent
    doesn't give the events for its console window to its console driver,
    and calls SystemEvent itself for them."""
    g = Guest()
    g.patches, g.in_patch = {}, set()
    g.machine.drivers = {~CONSOLE: g.process}
    sent = []
    monkeypatch.setattr(glue_desk, "_accessory", lambda t, w: ~CONSOLE)
    monkeypatch.setattr(glue_desk, "_wants", lambda t, unit, what: True)
    monkeypatch.setattr(glue_desk, "_send", lambda t, unit, code, e: sent.append(unit))
    event = g.ptr(bytes(16))
    g.mem.w16(event, 3)                             # keyDown
    g.mem.w8(lowmem.SEvtEnb, 0xFF)
    assert glue_desk.give_event(g, event) and sent == [~CONSOLE]
    g.mem.w8(lowmem.SEvtEnb, 0)
    assert not glue_desk.give_event(g, event) and sent == [~CONSOLE]
    assert g("SystemEvent", event) and sent == [~CONSOLE] * 2
