"""
Event Manager traps: WaitNextEvent, GetNextEvent and EventAvail, the
mouse, the keyboard and the tick count.  WaitNextEvent and GetNextEvent
let the other applications run while they wait.
"""

from __future__ import annotations

from ..memory import ADDRESS_MASK
from ..toolbox import constants as k
from ..toolbox import lowmem
from ..toolbox.events import MODIFIER_KEYS, tick_count, to_next_tick
from ..toolbox.windows import WindowRecord
from ..ui import keymap
from . import glue_desk, glue_os, glue_sony
from .cpu import SETTLE
from .dispatch import trap
from .glue_common import _port
from .glue_quickdraw import _rgn
from .glue_windows import window_pictures


# =========================================================================
# Event Manager
# =========================================================================
@trap
def WaitNextEvent(t, mask, event_p, sleep, mouse_rgn):
    # As under MultiFinder, where a program that calls this needn't call
    # SystemTask: desk accessories get their time.
    glue_desk.give_time(t)
    glue_os.respond(t)
    glue_sony.put_back(t)
    sleep = glue_desk.time_wanted(t, max(sleep, 0))
    if not sleep and t.null_tick == tick_count():
        sleep = to_next_tick()          # as GetNextEvent does
    patch = t.patches.get(GET_NEXT_EVENT)
    if patch is not None and not t.in_gne_patch:
        got = _through_patch(t, patch, mask, event_p, sleep)
        if not got:
            t.null_tick = tick_count()
        return got
    rgn = _rgn(t, mouse_rgn) if mouse_rgn else None
    _stored_activates(t)
    window_pictures(t)
    _unanswered(t, mask)
    with t.yielding():
        got, ev = t.tb.WaitNextEvent(mask & 0xFFFF, sleep, rgn)
    if not got:
        t.null_tick = ev.when
    return _returned(t, event_p, got, ev)


GET_NEXT_EVENT = 0xA970


def _through_patch(t, patch: int, mask: int, event_p: int, sleep: int) -> bool:
    """WaitNextEvent gets its event through the GetNextEvent trap, so a
    program's patch of it sees them all.  HyperCard 2 counts on it: a
    click it notices while a script runs is kept, and its patch hands it
    back to the main loop's WaitNextEvent.  The patch is asked at once,
    and again when the sleep is up or an event comes first."""
    def ask() -> bool:
        t.in_gne_patch = True
        try:
            return bool(t.call(patch, (("h", mask), ("p", event_p)), "b"))
        finally:
            t.in_gne_patch = False
    got = ask()
    if got or sleep <= 0:
        return got
    with t.yielding():
        t.process.events.get(mask & 0xFFFF, sleep, t.tb._next_update, remove=False)
    return ask()


@trap
def GetNextEvent(t, mask, event_p):
    # Nothing to do returns at once, as on a Mac, where a program may wait
    # for the Ticks to change and then look once, as Hacker II does as it
    # types.  Asked again in the same tick, it waits for the next: programs
    # call this in a loop, and the host's other applications want the
    # processor too.
    glue_os.respond(t)
    glue_sony.put_back(t)
    _stored_activates(t)
    window_pictures(t)
    _unanswered(t, mask)
    wait = to_next_tick() if t.null_tick == tick_count() else 0
    with t.yielding():
        got, ev = t.process.events.get(mask & 0xFFFF, wait, t.tb._next_update)
    if not got:
        t.null_tick = ev.when
    t.tb._end_grow_preview()
    return _returned(t, event_p, got, ev)


def _unanswered(t, mask: int) -> None:
    """An update event comes until the window's drawn (BeginUpdate and
    EndUpdate) or its update region's emptied, so a program that passes
    over it is given it each time it asks, as on a Mac.  Cap'n Magneto's
    title screen looks only for a click, and never answers its window's.
    Asked again in the same tick for one it was given and hasn't begun,
    this waits for the next tick first, or for input."""
    window, tick, begun = t.updated
    if window is None or tick != tick_count() or window.updates_begun != begun:
        return
    got, ev = t.process.events.peek(mask & 0xFFFF, t.tb._next_update)
    if got and ev.what == k.updateEvt and ev.message is window:
        with t.yielding():
            t.process.events.nap(to_next_tick() / 60 + SETTLE)


def _stored_activates(t) -> None:
    """Activate events a program has generated itself, storing the windows
    in CurDeactive and CurActivate as the Window Manager does (Inside
    Macintosh I, Window Manager): SuperPaint 2.0 activates its document
    windows that way, behind its palettes.  The deactivate comes first."""
    mem = t.mem
    for addr, active in ((lowmem.CurDeactive, False), (lowmem.CurActivate, True)):
        p = mem.r32(addr) & ADDRESS_MASK
        if p:
            mem.w32(addr, 0)
            t.process.events.post_activate(t.window(p) or p, active)


def _returned(t, event_p: int, got: bool, ev) -> bool:
    """Write the event GetNextEvent returns, mount the disk of a
    disk-inserted one, and give a desk accessory one that's its own:
    whether it's the application's."""
    t.write_event(event_p, ev)
    t.updated = (None, -1, 0)
    if got and ev.what == k.updateEvt and isinstance(ev.message, WindowRecord):
        t.updated = (ev.message, ev.when, ev.message.updates_begun)
    if got and ev.what == k.diskEvt:
        from .glue_files import disk_inserted
        disk_inserted(t, event_p)
    return got and not glue_desk.give_event(t, event_p)


@trap
def EventAvail(t, mask, event_p):
    glue_sony.put_back(t)
    _stored_activates(t)
    window_pictures(t)
    got, ev = t.process.events.peek(mask & 0xFFFF, t.tb._next_update)
    t.write_event(event_p, ev)
    return got


@trap
def TickCount(t):
    """Ticks, as the ROM's has it: what the program reads there itself
    goes on from it."""
    t.process.events.timed()
    return t.mem.r32(lowmem.Ticks)


@trap
def Button(t):
    return t.tb.Button()


@trap
def StillDown(t):
    return t.tb.StillDown()


@trap
def WaitMouseUp(t):
    if t.tb.StillDown():
        return True
    t.take_mouse_up()
    return False


@trap
def GetMouse(t, p):
    # Mouse, as the ROM has it: under the host's pointer, unless the
    # program has moved the mouse itself
    pt = t.mem.r_point(lowmem.Mouse)
    port = _port(t)
    t.set_point(p, pt if port is None else port.global_to_local(pt))


@trap
def GetKeys(t, p):
    """The KeyMap, with the modifier keys as they are now."""
    keys = bytearray(t.mem.read(lowmem.KeyMap, 16))
    t.process.events.asked()
    _, mods = t.process.events.input_state()
    for bit, code in MODIFIER_KEYS:
        if mods & bit:
            keys[code >> 3] |= 1 << (code & 7)
    t.mem.write(p, bytes(keys))


@trap
def KeyTranslate(t, trans_data, keycode, state_p):
    """A key code and modifiers to characters, by the US layout whatever
    the KCHR, with no dead keys: the character in the low byte."""
    t.w32(state_p, 0)
    return keymap.translate(keycode & 0x7F, keycode >> 8)


@trap
def SysBeep(t, duration):
    if t.machine.sound_manager.sys_beep:
        t.tb.SysBeep(duration)
