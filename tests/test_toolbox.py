"""Toolbox data structures that don't need a display."""

import math
import struct
import threading
import time
from types import SimpleNamespace

import pytest
from guest import Guest, Processor

from macrt.m68k import dispatch, runtime
from macrt.m68k.glue_os import Gestalt, SysEnvirons
from macrt.memory import RAM, BusError
from macrt.toolbox import constants as k
from macrt.toolbox import events, lowmem, system
from macrt.toolbox.events import EventQueue, EventRecord, ProcessKilled
from macrt.toolbox.memmgr import noErr
from macrt.toolbox.qdraw import Region
from macrt.toolbox.menus import Menu, parse_menu_items
from macrt.toolbox.types import Point, Rect
from macrt.toolbox.windows import WindowKind, describe_proc
from macrt.ui import theme

SYSTEM_24 = SimpleNamespace(mmu_32_bit=False, addr_mask=0xFFFFFF)


def test_rect_basics():
    r = Rect.from_xywh(10, 20, 100, 50)
    assert (r.top, r.left, r.bottom, r.right) == (20, 10, 70, 110)
    assert (r.width, r.height) == (100, 50)
    assert r.contains(Point(20, 10))
    assert not r.contains(Point(70, 50))          # bottom/right edges are outside
    assert r.moved_to(0, 0) == Rect(0, 0, 50, 100)
    assert r.sized(30, 40) == Rect(20, 10, 60, 40)


def test_append_menu_metacharacters():
    items = parse_menu_items("Open…/O;(-;(Save/S;Bold<B;Checked!\x12;Icon^1")
    assert [i.text for i in items] == ["Open…", "-", "Save", "Bold", "Checked", "Icon"]
    assert items[0].cmd == "O" and items[0].enabled
    assert items[1].is_separator and not items[1].enabled
    assert items[2].cmd == "S" and not items[2].enabled
    assert items[3].style
    assert items[4].mark == k.checkMark
    assert items[5].icon == 1


class MenuGuest(Guest):
    menu = runtime.Runner.menu


def test_an_items_icon_marks_and_keys_read_back(qapp):
    g = MenuGuest()
    g.tb.menus_changed = lambda menu=None: None                   # no display to show them on
    h = g("NewMenu", 200, g.ptr(b"\x05Disks"))
    g.menu(h).append(parse_menu_items("Slot 6^3!\x12/D;Eject"))      # as AppendMenu does
    out = g.ptr(bytes(2))

    def get(name, item):
        g.mem.w16(out, 0xFFFF)
        g(name, h, item, out)
        return g.mem.r16(out)
    assert [get("GetItemIcon", 1), get("GetItemMark", 1), get("GetItemCmd", 1)] == \
        [3, 0x12, ord("D")]
    assert [get("GetItemIcon", 2), get("GetItemMark", 2), get("GetItemCmd", 2)] == [0, 0, 0]
    assert get("GetItemIcon", 9) == 0                   # no such item


def test_the_help_menu_through_its_trap(qapp):
    g = MenuGuest()
    g.tb.menus_changed = lambda menu=None: None
    var = g.ptr(bytes(4))
    assert g("HMGetHelpMenuHandle", var) == 0               # noErr
    h = g.mem.r32(var)
    assert g.menu(h).id == k.kHMHelpMenuID
    assert g("CountMItems", h) == 4                         # the standard items, a line
    g.mem.w32(var, 0)
    g("HMGetHelpMenuHandle", var)
    assert g.mem.r32(var) == h                              # the same one each time
    assert g.process.bar_menus() == g.process.menus.own()    # not in the bar yet
    g.menu(h).append(parse_menu_items("Help\u2026"))
    assert g.process.bar_menus()[-1] == g.menu(h)
    assert not g("HMGetBalloons")


def test_balloons_are_never_shown_but_their_settings_are_kept(qapp):
    """Show Balloons is off and stays off; the font, size and 'hdlg' and
    'hmnu' IDs an application sets for its balloons it gets back."""
    g = Guest()
    word = g.ptr(bytes(2))

    def get(name, *args):
        assert g(name, *args, word) == 0
        return g.mem.r16s(word)

    assert g("HMSetBalloons", True) == 0 and not g("HMGetBalloons")
    assert g("HMShowBalloon", 0, 0, 0, 0, 0, 0, 0) == -850          # hmHelpDisabled
    assert g("HMRemoveBalloon") == -862                             # hmNoBalloonUp
    assert (get("HMGetFont"), get("HMGetFontSize")) == (3, 9)       # Geneva 9
    g("HMSetFont", 4)
    g("HMSetFontSize", 10)
    assert (get("HMGetFont"), get("HMGetFontSize")) == (4, 10)
    assert get("HMGetDialogResID") == -1
    g("HMSetDialogResID", 200)
    assert get("HMGetDialogResID") == 200
    assert g("HMGetMenuResID", 128, word) == -192                   # resNotFound
    g("HMSetMenuResID", 128, 300)
    assert get("HMGetMenuResID", 128) == 300


def test_help_menu_resource_ids_through_the_trap(qapp):
    """HMGetMenuResID(menuID, VAR resID) takes three words and
    HMSetMenuResID(menuID, resID) two, as their selectors say: each leaves
    its OSErr in the word pushed for it, and the stack as it was."""
    m = Processor()
    var = m.ptr(bytes(2))
    get = struct.pack(">HHHHIHHH", 0x4267, 0x3F3C, 128, 0x4879, var, 0x303C, 0x0314, 0xA830)
    put = struct.pack(">HHHHHHHH", 0x4267, 0x3F3C, 128, 0x3F3C, 300, 0x303C, 0x020D, 0xA830)
    m.run(get + put + get)                  # CLR.W -(SP); MOVE.W #128,-(SP); ...; _Pack14
    assert [m.cpu.pop16() for _ in range(3)] == [0, 0, 0x10000 - 192]      # resNotFound first
    assert m.cpu.sp == m.stack_top and m.mem.r16(var) == 300 and m.missing == []


def test_an_items_icon_is_its_words_low_byte(qapp):
    """SetItemIcon's iconIndex is a Byte, pushed as a word with the value
    in its low byte."""
    m = Processor()
    m.tb._d = SimpleNamespace(bridge=SimpleNamespace(call=lambda fn, *a: fn(*a)),
                              refresh_bars=lambda: None)
    h = m("NewMenu", 200, m.ptr(b"\x05Disks"))
    m("AppendMenu", h, m.ptr(b"\x04Open"))
    out = m.ptr(bytes(2))
    for word, icon in ((5, 5), (0x0107, 7)):
        m.run(struct.pack(">HIHHHHHHIHHHIH", 0x2F3C, h, 0x3F3C, 1, 0x3F3C, word, 0xA940,
                          0x2F3C, h, 0x3F3C, 1, 0x4879, out, 0xA93F))   # Set-, GetItemIcon
        assert m.mem.r16(out) == icon and m.cpu.sp == m.stack_top


def _selector_bytes(dispatcher: str, selector: int) -> int | None:
    """The bytes of arguments a routine's selector says it takes, where
    its dispatcher's selectors count them, as the interfaces' do."""
    if dispatcher in ("DialogDispatch", "DisplayDispatch", "FSDispatch", "IconDispatch",
                      "MenuDispatch", "Pack8", "Pack11", "Pack14", "Pack15"):
        return (selector >> 8 & 0xFF) * 2               # words, in bits 8-15
    if dispatcher == "PrGlue" or dispatcher == "PaletteDispatch" and selector >> 8:
        return selector >> 8 & 0xFF                     # bytes, in bits 8-15
    if dispatcher == "QDExtensions":
        return selector >> 16                           # bytes, in bits 16-31
    if dispatcher == "SoundDispatch" and selector >> 24:
        return (selector >> 24) * 2                     # words, in bits 24-31
    return None


def test_every_signature_takes_what_its_selector_says():
    """A signature that takes more or less than its selector says leaves
    the stack unbalanced (HMGetMenuResID's took a word too many).
    ScriptUtil's selectors count their results too (see test_script).
    CountVoices's counts a word for its VAR's pointer, which is two."""
    checked = 0
    for sig in dispatch.SIGNATURES.values():
        want = None if sig.selector is None else _selector_bytes(sig.dispatcher, sig.selector)
        if want is not None and sig.name != "CountVoices":
            assert sig.stack_bytes == want, sig.name
            checked += 1
    assert checked > 250


def test_pascal_arguments_come_off_the_stack_in_declaration_order():
    """The last pushed is at the top of the stack; a byte is in its word's
    high byte; a Point is its v, then its h."""
    mem = RAM(SYSTEM_24, 0x1000)
    sp = 0x800
    mem.w32(sp, 0xABCDEF)               # p
    mem.w32(sp + 4, -2)                 # l
    mem.w16(sp + 8, 0xFFFF)             # H
    mem.w16(sp + 10, -2)                # h
    mem.w16(sp + 12, 0x80FF)            # B
    mem.w16(sp + 14, 0xFF00)            # b
    mem.w16(sp + 16, -10)               # P's v
    mem.w16(sp + 18, 20)                # and h
    assert dispatch.pop_args(mem, sp, "PbBhHlp") == (
        [Point(-10, 20), -1, 0x80, -2, 0xFFFF, -2, 0xABCDEF], sp + 20)
    assert dispatch.pop_args(mem, sp, "") == ([], sp)
    assert dispatch.pop_args(mem, sp + 16, "P") == ([Point(-10, 20)], sp + 20)
    with pytest.raises(BusError):
        dispatch.pop_args(mem, 0xFFC, "lh")


def test_open_paren_anywhere_disables_the_item():
    # Same as the real AppendMenu, which is why the demo avoids "(".
    (item,) = parse_menu_items("Modal Dialog (dBoxProc)")
    assert item.text == "Modal Dialog dBoxProc)"
    assert not item.enabled


def test_menu_selectable():
    m = Menu(128, "File", parse_menu_items("New;(-;(Gone;Quit"))
    assert [m.selectable(i) for i in range(0, 6)] == [False, True, False, False, True, False]
    m.enabled = False
    assert not m.selectable(1)


@pytest.mark.parametrize("proc_id, kind, grow, zoom", [
    (k.documentProc, WindowKind.DOCUMENT, True, False),
    (k.zoomDocProc, WindowKind.DOCUMENT, True, True),
    (k.noGrowDocProc, WindowKind.DOCUMENT, False, False),
    (k.zoomNoGrow, WindowKind.DOCUMENT, False, True),
    (k.dBoxProc, WindowKind.DBOX, False, False),
    (k.plainDBox, WindowKind.PLAIN, False, False),
    (k.altDBoxProc, WindowKind.ALT_DBOX, False, False),
    (k.movableDBoxProc, WindowKind.MOVABLE_DBOX, False, False),
    (k.rDocProc, WindowKind.ROUNDED, False, False),
])
def test_describe_proc(proc_id, kind, grow, zoom):
    d = describe_proc(proc_id)
    assert (d.kind, d.grow, d.zoom) == (kind, grow, zoom)


def test_rounded_corner_from_variation():
    assert describe_proc(k.rDocProc).corner == 16
    assert describe_proc(k.rDocProc + 1).corner == 4


def _queue(updates=()):
    pending = list(updates)
    q = EventQueue(lambda: (Point(0, 0), 0))
    return q, (lambda: pending.pop(0) if pending else None)


def test_event_priority_activate_then_input_then_update():
    q, next_update = _queue(["win-needing-update"])
    q.post(EventRecord(k.mouseDown, 0, 0, Point(5, 5), 0))
    q.post_activate("win", True)
    order = [q.get(k.everyEvent, 0, next_update)[1].what for _ in range(3)]
    assert order == [k.activateEvt, k.mouseDown, k.updateEvt]
    got, ev = q.get(k.everyEvent, 0, next_update)
    assert not got and ev.what == k.nullEvent


def test_a_mouse_region_the_mouse_is_out_of_gives_mouse_moved_events():
    """After any other event and ahead of a null one; an empty region,
    which the mouse is never in, too.  In it, the sleep runs out."""
    q, next_update = _queue(["win-needing-update"])
    got, ev = q.get(k.everyEvent, 0, next_update, mouse_rgn=Region())
    assert got and ev.what == k.updateEvt
    got, ev = q.get(k.everyEvent, 0, next_update, mouse_rgn=Region())
    assert got and (ev.what, ev.message) == (k.osEvt, k.mouseMovedMessage << 24)
    q, next_update = _queue()
    got, ev = q.get(k.everyEvent, 0, next_update, mouse_rgn=Region.rect(Rect(10, 10, 20, 20)))
    assert got and (ev.what, ev.message) == (k.osEvt, k.mouseMovedMessage << 24)
    got, ev = q.get(k.everyEvent, 3, next_update, mouse_rgn=Region.rect(Rect(-1, -1, 1, 1)))
    assert not got and ev.what == k.nullEvent


def test_the_tick_clock_starts_a_minute_in():
    """TickCount counts from startup, and a program launched as macrt starts
    sees a minute gone by, not a clock that has only just begun: Castles
    Demo, under 180, ran past the end of a list of its own."""
    ticks = events.tick_count()
    assert ticks - int((time.monotonic() - events._T0) * 60) in (3599, 3600)


def test_a_mouse_that_stays_put_has_mouse_moved_and_null_events_by_turns(monkeypatch):
    """Asked again after a mouse-moved event, WaitNextEvent gives a null
    event once its sleep runs out, by the next tick at the latest, even
    when that tick has come: Dr. Quandary starts only on one, and takes
    more than a tick to ask again.  Asked again in the null event's tick,
    it waits for the next tick first, so that SimpleText and Darkwood,
    which pass an empty region and ask again at once, leave the host the
    processor.  A mouse that has moved is given its event at once."""
    # A clock whose tick ends only when the queue waits
    ticks = [100]
    monkeypatch.setattr(events, "tick_count", lambda: ticks[0])
    monkeypatch.setattr(events, "to_next_tick", lambda: 0.5)
    at = [Point(0, 0)]
    q = EventQueue(lambda: (at[0], 0))

    def wait(timeout):
        time.sleep(timeout)
        ticks[0] += 1

    q._cond.wait = wait
    whens = []
    for i in range(6):
        got, ev = q.get(k.everyEvent, 0, lambda: None, mouse_rgn=Region())
        assert got and (ev.what, ev.where) == (k.osEvt, Point(0, 0))
        whens.append(ev.when)
        if i % 2:
            ticks[0] += 2
        got, ev = q.get(k.everyEvent, 0, lambda: None, mouse_rgn=Region())
        assert not got and ev.what == k.nullEvent
    assert len(set(whens)) == 6 and q.waited_at > float("-inf")
    got, ev = q.get(k.everyEvent, 600, lambda: None, mouse_rgn=Region())
    assert got and ev.what == k.osEvt
    before = time.monotonic()
    got, ev = q.get(k.everyEvent, 600, lambda: None, mouse_rgn=Region())
    assert not got and ev.what == k.nullEvent and time.monotonic() - before < 5
    at[0] = Point(5, 6)
    waited = q.waited_at
    got, ev = q.get(k.everyEvent, 0, lambda: None, mouse_rgn=Region())
    assert got and ev.where == Point(5, 6) and q.waited_at == waited


def test_the_queue_says_when_the_program_last_asked_and_waited():
    """By the host's clock, so that a survey can tell a program waiting
    for input from a busy one: asking is each look in the queue, and
    waiting the wait in WaitNextEvent's sleep for something to come."""
    q, next_update = _queue()
    never = float("-inf")
    assert (q.asked_at, q.waited_at, q.timed_at, q.waiting) == (never, never, never, False)
    before = time.monotonic()
    q.peek(k.everyEvent, next_update)
    assert q.asked_at >= before and q.waited_at == never
    before = time.monotonic()
    q.get(k.everyEvent, 0, next_update)
    assert q.asked_at >= before and q.waited_at == never
    got = []
    t = threading.Thread(target=lambda: got.append(q.get(k.everyEvent, 600, next_update)))
    t.start()
    deadline = time.monotonic() + 5
    while not q.waiting and time.monotonic() < deadline:
        time.sleep(0.01)
    assert q.waiting and q.waited_at == never
    posted = time.monotonic()
    q.post(EventRecord(k.keyDown, 0x61, 0, Point(0, 0), 0))
    t.join(5)
    assert got[0][0] and got[0][1].what == k.keyDown
    assert not q.waiting and q.waited_at >= posted and q.asked_at >= posted
    before = time.monotonic()
    q.asked()
    q.timed()
    assert q.asked_at >= before and q.timed_at >= before


def test_reading_the_clock_and_the_keys_is_stamped(qapp):
    """TickCount and Delay are a program reading the clock; GetKeys, one
    asking whether a key is down."""
    g = Guest()
    events = g.process.events
    before = time.monotonic()
    g("TickCount")
    assert events.timed_at >= before and events.asked_at == float("-inf")
    events.timed_at, slept = float("-inf"), []
    g.sleep = slept.append
    g("Delay", 1)
    assert events.timed_at >= before and events.asked_at == float("-inf") and slept == [1]
    g("GetKeys", g.ptr(bytes(16)))
    assert events.asked_at >= before


def test_activate_then_deactivate_before_reading_cancels_out():
    q, next_update = _queue()
    q.post_activate("win", True)
    q.post_activate("win", False)
    assert not q.get(k.everyEvent, 0, next_update)[0]


def test_a_second_activate_the_same_way_isnt_doubled():
    q, next_update = _queue()
    q.post_activate("win", True)
    q.post_activate("win", True)
    got, ev = q.get(k.everyEvent, 0, next_update)
    assert got and ev.what == k.activateEvt and ev.modifiers & k.activeFlag
    assert not q.get(k.everyEvent, 0, next_update)[0]


def test_activate_events_a_program_stores_for_itself(qapp):
    """SuperPaint 2.0 activates its document window behind its palettes as
    the Window Manager would: it stores the windows in CurDeactive and
    CurActivate.  The Event Manager reports the deactivate first."""
    g = Guest()
    ev = g.ptr(bytes(16))
    g.mem.w32(lowmem.CurDeactive, 0x1234)
    g.mem.w32(lowmem.CurActivate, 0x5678)
    assert g("EventAvail", k.everyEvent, ev)
    assert g.mem.r32(lowmem.CurDeactive) == g.mem.r32(lowmem.CurActivate) == 0
    events = [g.process.events.get(k.everyEvent, 0, lambda: None)[1] for _ in range(2)]
    assert [(e.what, e.message, e.modifiers & k.activeFlag) for e in events] == [
        (k.activateEvt, 0x1234, 0), (k.activateEvt, 0x5678, k.activeFlag)]
    assert not g("EventAvail", k.everyEvent, ev)


def test_event_mask_leaves_other_events_queued():
    q, next_update = _queue()
    q.post(EventRecord(k.mouseDown, 0, 0, Point(0, 0), 0))
    q.post(EventRecord(k.keyDown, 0x0061, 0, Point(0, 0), 0))
    got, ev = q.get(k.event_mask(k.keyDown), 0, next_update)
    assert got and ev.what == k.keyDown
    got, ev = q.get(k.everyEvent, 0, next_update)
    assert got and ev.what == k.mouseDown


def test_flush_events_by_mask_up_to_the_stop_mask():
    q, next_update = _queue()
    for what in (k.keyDown, k.mouseDown, k.keyDown, k.mouseUp, k.keyDown):
        q.post(EventRecord(what, 0, 0, Point(0, 0), 0))
    q.flush(k.event_mask(k.keyDown), k.event_mask(k.mouseUp))
    left = [q.get(k.everyEvent, 0, next_update)[1].what for _ in range(3)]
    assert left == [k.mouseDown, k.mouseUp, k.keyDown]
    q.flush()
    assert not q.get(k.everyEvent, 0, next_update)[0]


def test_flush_events_leaves_high_level_events():
    """They're in a queue of their own on a Mac."""
    q, next_update = _queue()
    for what in (k.keyDown, k.kHighLevelEvent, k.mouseDown):
        q.post(EventRecord(what, 0, 0, Point(0, 0), 0))
    q.flush(k.everyEvent, k.highLevelEventMask)
    got, ev = q.get(k.everyEvent, 0, next_update)
    assert got and ev.what == k.kHighLevelEvent
    assert not q.get(k.everyEvent, 0, next_update)[0]


def test_killed_queue_raises_in_guest():
    q, next_update = _queue()
    q.kill()
    with pytest.raises(ProcessKilled):
        q.get(k.everyEvent, 60, next_update)


def test_key_translate_by_the_us_layout(qapp):
    g = Guest()
    state = g.ptr(b"\xFF" * 4)

    def key(code, mods=0):
        return g("KeyTranslate", 0, (mods & 0xFF00) | code, state)
    assert key(0x00) == ord("a")
    assert key(0x00, k.shiftKey) == key(0x00, k.alphaLock) == ord("A")
    assert key(0x12, k.shiftKey) == ord("!") and key(0x12, k.alphaLock) == ord("1")
    assert key(0x00, k.controlKey) == 0x01
    assert key(0x80 | 0x24) == 0x0D                         # key up: the same key
    assert (key(0x7B), key(0x53), key(0x4C)) == (0x1C, ord("1"), 0x03)
    assert (key(0x46), key(0x46, k.shiftKey)) == (0x1C, ord("+"))     # a Mac Plus's Left
    assert key(0x3F) == 0                                   # no such key
    assert g.mem.r32(state) == 0                            # no dead key pending


def test_the_system_s_keyboard_layout_is_the_us_one(qapp):
    """'KCHR' 0, which Keyboard lists by name: a table number for each
    modifier byte, and its table the characters KeyTranslate gives."""
    g = Guest()
    rm = g.process.resources
    h = rm.GetResource("KCHR", 0)
    assert rm.GetResInfo(h)[2] == "U.S."
    data = g.contents(h)
    index, count = data[2:258], struct.unpack_from(">H", data, 258)[0]
    tables = [data[260 + 128 * n:388 + 128 * n] for n in range(count)]
    for mods in (0, k.shiftKey, k.alphaLock, k.controlKey, k.cmdKey | k.shiftKey):
        table = tables[index[mods >> 8]]
        assert all(table[code] == g("KeyTranslate", 0, mods | code, g.ptr(bytes(4)))
                   for code in range(128))
    assert data[260 + 128 * count:] == b"\0\0"              # no dead keys


def test_key1trans_and_key2trans_are_keytranslate(qapp):
    # Programs that call the ROM's translation from low memory themselves:
    # the key code in D2 (a keypad key's less $40 for Key2Trans), the
    # modifiers' high byte in D1.
    m = Processor()
    key2 = m.ptr(runtime.KEY_TRANS)
    key1 = key2 + 4

    def trans(at, code, mods=0):
        m.cpu.set_d(3, 0x1234)
        m.run(struct.pack(">HHHI", 0x7200 | mods, 0x7400 | code, 0x4EB9, at))
        assert m.cpu.d(3) == 0x1234 and m.cpu.sp == m.stack_top
        return m.cpu.d(0)
    assert trans(key1, 0x12) == ord("1")
    assert trans(key1, 0x12, k.shiftKey >> 8) == ord("!")
    assert trans(key1, 0x30) == 0x09                    # Tab
    assert trans(key2, 0x53 - 0x40) == ord("1")         # the keypad's
    # Key Caps turns dead keys off with the flag word after Key1Trans's
    # first instruction while it asks about a key.
    assert m.mem.r16(key1 + 2) == 0xFFFF
    m.mem.w16(key1 + 2, 0)
    assert trans(key1, 0x00) == ord("a")


def test_the_cursor_vectors_shield_a_global_rectangle(qapp, monkeypatch):
    """JShieldCursor's caller pushes the rectangle's top left, then its
    bottom right, as the Alarm Clock does, and it's popped."""
    m = Processor()
    vectors = m.ptr(runtime.CURSOR_VECTORS)
    shielded = []
    monkeypatch.setitem(dispatch.IMPLEMENTATIONS, "ShieldCursor", lambda t, r, offset: (
        shielded.append((struct.unpack(">4h", t.mem.read(r, 8)), offset))))
    m.cpu.set_d(0, 0x1234)
    m.run(struct.pack(">HhhHhhHI", 0x2F3C, 10, 20, 0x2F3C, 30, 40, 0x4EB9, vectors + 16))
    assert shielded == [((10, 20, 30, 40), Point(0, 0))]
    assert m.cpu.sp == m.stack_top and m.cpu.d(0) == 0x1234
    for vector in range(4):                             # Hide, Show, Init and Obscure
        m.run(struct.pack(">HI", 0x4EB9, vectors + 4 * vector))
        assert m.cpu.sp == m.stack_top
    assert m.missing == []


def test_delta_point_is_a_less_b(qapp):
    g = Guest()
    assert g("DeltaPoint", Point(10, 20), Point(3, 25)) == 0x0007FFFB


def test_pt_to_angle_goes_round_from_12_oclock_as_the_rect_has_it(qapp):
    g = Guest()
    r = g.ptr(struct.pack(">4h", 0, 0, 100, 200))    # twice as wide as it's high
    angle = g.ptr(b"\xFF\xFF")

    def at(v, h):
        g("PtToAngle", r, Point(v, h), angle)
        return g.mem.r16(angle)
    assert [at(0, 100), at(0, 200), at(50, 200), at(100, 100), at(100, 0), at(50, 0)] == \
        [0, 45, 90, 180, 225, 270]
    assert at(50, 100) == 0                         # the centre
    assert at(-50, 150) == 14                       # atan(1/4), above the rect


def test_inset_rect_moves_only_the_sides(qapp):
    # PhrazeCraze insets a letter's square to nothing, then out again.
    g = Guest()
    r = g.ptr(struct.pack(">4h", 104, 261, 122, 275))

    def inset(dh, dv):
        g("InsetRect", r, dh, dv)
        return struct.unpack(">4h", g.mem.read(r, 8))
    assert inset(7, 0) == (104, 268, 122, 268)
    assert inset(0, 10) == (114, 268, 112, 268)     # inside out, as the ROM leaves it
    assert inset(-7, -10) == (104, 261, 122, 275)


def test_a_toolbox_routine_with_no_result_leaves_d0_clear(qapp):
    """BMAPEdit's bitmap routine, a C function, falls off its end after
    SetPort and returns what D0 holds; the registers stack-based traps
    keep, A2-A6 and D3-D7, are kept."""
    m = Processor()
    port = m.ptr(bytes(108))
    m.cpu.set_d(3, 0x5678)
    # MOVE.L #$000BF460,D0; MOVE.L #port,-(SP); _SetPort
    m.run(struct.pack(">HIHIH", 0x203C, 0x000BF460, 0x2F3C, port, 0xA873))
    assert m.cpu.d(0) == 0 and m.cpu.d(3) == 0x5678 and m.cpu.sp == m.stack_top
    # A function's result is still where Pascal puts it, and D0 is its own.
    m.run(struct.pack(">HIHH", 0x203C, 0x1234, 0x42A7, 0xA924))    # CLR.L -(SP); _FrontWindow
    assert m.cpu.d(0) == 0x1234 and m.cpu.sp == m.stack_top - 4


def test_release_resource_leaves_d0_as_it_was(qapp):
    """DeadEnd's checksum routine, a C function, falls off its end after
    ReleaseResource and returns the sum it left in D0."""
    m = Processor()
    # MOVE.L #$0000022B,D0; CLR.L -(SP); _ReleaseResource
    m.run(struct.pack(">HIHH", 0x203C, 0x022B, 0x42A7, 0xA9A3))
    assert m.cpu.d(0) == 0x022B and m.cpu.sp == m.stack_top


def test_hwpriv_has_no_caches_to_mind(qapp):
    m = Processor()
    assert dispatch.implemented(dispatch.trap_key(0xA198))
    m.run(b"\x70\x01\xa1\x98")                  # MOVEQ #1,D0; _HWPriv: FlushInstructionCache
    assert m.cpu.d(0) == 0
    m.run(b"\x70\x07\xa1\x98")                  # selector 7, not a cache's
    assert m.cpu.d(0) == 0xFFFFFE0A                 # hwParamErr
    assert m.missing == ["HWPriv selector 7"]


def test_swap_mmu_mode_stays_in_24_bit_mode(qapp):
    """Maelstrom asks for 32-bit mode around its drawing and puts back the
    mode it's given: D0's low byte, the rest of D0 clear, which Prince of
    Persia 2's sprite code counts on."""
    m = Processor()
    m.run(bytes.fromhex("203C12345601 A05D"))      # MOVE.L #$12345601,D0; _SwapMMUMode
    assert m.cpu.d(0) == 0 and m.mem.r8(lowmem.MMU32Bit) == 0
    m.run(bytes.fromhex("7000 A05D"))              # back to 24-bit
    assert m.cpu.d(0) == 0 and m.missing == []


def test_gestalt_says_its_own_version_and_the_sound_managers(qapp):
    """Jetpack says it needs a 68020 when Gestalt has no 'vers'; Maelstrom
    wants the Sound Manager's SndPlayDoubleBuffer."""
    g = Guest()
    assert Gestalt(g, 0x76657273) == (noErr, 1)                # 'vers'
    err, snd = Gestalt(g, 0x736E6420)                           # 'snd '
    assert err == noErr and snd & 1 << 10                       # gestaltSndPlayDoubleBuffer
    assert snd & 1 << 11                                        # gestaltMultiChannels
    assert snd & 1 << 3                                         # gestaltSoundIOMgrPresent
    assert not snd & 1 << 5                                     # no sound input device


def test_each_model_says_which_processor_and_coprocessor_it_has(qapp):
    """Fine Artist wants a colour Mac with a 68030: a Mac IIx says it has
    one, and a 68882, to Gestalt, SysEnvirons and in CPUFlag."""
    for model, proc, fpu, mach, env in (("plus", 1, 0, 4, 2), ("ii", 3, 1, 6, 4),
                                        ("iix", 4, 2, 7, 5)):
        g = Guest(model=model)
        assert Gestalt(g, int.from_bytes(b"proc", "big")) == (noErr, proc)
        assert Gestalt(g, int.from_bytes(b"fpu ", "big")) == (noErr, fpu)
        assert Gestalt(g, int.from_bytes(b"mach", "big")) == (noErr, mach)
        p = g.ptr(bytes(16))
        assert SysEnvirons(g, 2, p) == noErr
        machine, _, processor, has_fpu = struct.unpack_from(">hhhB", g.mem.read(p + 2, 7))
        assert (machine, processor, has_fpu) == (env, proc, fpu > 0)


def test_gestalt_says_there_are_system_7s_help_manager_ppc_and_launch(qapp):
    """BMAPEdit wants the Help Manager, the PPC Toolbox and Launch with
    an FSSpec before it starts."""
    g = Guest()
    assert Gestalt(g, int.from_bytes(b"help", "big")) == (noErr, 1)
    err, ppc = Gestalt(g, int.from_bytes(b"ppc ", "big"))
    assert err == noErr and ppc & 0x1000 and not ppc & 3       # initialised; no network
    err, os = Gestalt(g, int.from_bytes(b"os  ", "big"))
    assert err == noErr and os & 0b1110 == 0b1110               # return, FSSpec, control


def test_gestalt_says_there_is_one_script_and_the_pop_up_menu_control(qapp):
    """Chess++, Mille Bornes and Tarantula ask how many scripts there are;
    BrickBounce, Spoydworks Solitaire and NCSA Mosaic whether there's the
    pop-up menu control."""
    g = Guest()
    assert Gestalt(g, int.from_bytes(b"scr#", "big")) == (noErr, 1)
    assert Gestalt(g, int.from_bytes(b"pop!", "big")) == (noErr, 1)


def test_there_is_no_default_user(qapp):
    """Netscape Navigator 2.02 asks the PPC Toolbox for the owner Sharing
    Setup names; there's none to give."""
    m = Processor()
    pb = m.ptr(bytes(64))
    m.run(struct.pack(">HIHHH", 0x207C, pb, 0x303C, 13, 0xA0DD))   # GetDefaultUser(pb)
    assert m.cpu.d(0) & 0xFFFF == 0x10000 - 922                     # noDefaultUserErr
    assert m.mem.r16s(pb + 16) == -922                              # ioResult
    assert m.missing == []


def test_a_boolean_result_fills_its_whole_word(qapp):
    # MacWrite leaves the slot as it was and then tests the word, not the byte.
    m = Processor()
    r = m.ptr(bytes.fromhex("0000 0000 0010 0010"))         # not empty
    m.cpu.push16(0xFFFF)
    m.cpu.push32(r)
    m.run(b"\xa8\xae")                                      # _EmptyRect
    assert m.cpu.pop16() == 0x0000
    m.cpu.push16(0x00FF)
    m.cpu.push32(m.ptr(bytes(8)))
    m.run(b"\xa8\xae")
    assert m.cpu.pop16() == 0x0100                          # true, in the high byte


def test_packages_need_no_loading(qapp):
    m = Processor()
    m.cpu.push16(7)
    m.run(b"\xa9\xe5\xa9\xe6")                  # InitPack(7), InitAllPacks
    for selector in (2, 4):                         # DILoad, DIUnload
        m.cpu.push16(selector)
        m.run(b"\xa9\xe9")                        # _Pack2
    assert m.missing == [] and m.cpu.sp == m.stack_top


def test_init_resources_gives_the_system_files_refnum(qapp):
    m = Processor()
    rm = m.process.resources
    current = rm.CurResFile()
    m.cpu.push16(0xFFFF)
    m.run(b"\xa9\x95")                          # _InitResources
    assert m.cpu.pop16() == rm.sys_refnum & 0xFFFF
    assert rm.CurResFile() == current and m.missing == []


def test_flushing_the_code_cache_drops_translated_code(qapp, monkeypatch):
    m = Processor()
    flushed = []
    monkeypatch.setattr(m.cpu, "flush_code", lambda: flushed.append(True))
    m.run(b"\xa0\xbd")                          # _FlushCodeCache
    m.run(b"\x70\x01\xa1\x98")                  # _HWPriv: FlushInstructionCache
    m.run(b"\x70\x03\xa1\x98")                  # FlushDataCache: no code in that one
    assert len(flushed) == 2 and m.missing == []


def test_method_dispatch_finds_the_method_of_the_class_or_its_nearest_superclass(qapp):
    """Q-Sheet's Object Pascal methods are called through _MethodDispatch,
    a call's own stub followed by the classes with the method: the class
    first that's found for, then each class, highest numbered first.
    Class 2 is the root, 4 and 8 its subclasses, 6 4's."""
    m = Processor()
    mem = m.mem
    a5 = m.ptr(bytes(0x40))
    mem.write(a5 + 0x10, bytes.fromhex("7002 4e75"))        # class 2's: MOVEQ #2,D0; RTS
    mem.write(a5 + 0x20, bytes.fromhex("7004 4e75"))        # class 4's
    m.cpu.set_a(5, a5)
    classes = m.ptr(struct.pack(">5H", 0, 0, 2, 4, 2))      # each one's superclass
    mem.w32(lowmem.ClassTable, m.ptr(struct.pack(">I", classes)))
    mem.w32(lowmem.MethodNotFound, m.ptr(bytes.fromhex("70ff 4e75")))   # MOVEQ #-1,D0
    both = m.ptr(bytes.fromhex("a9f8 0001 0000 0000 0004 0020 0002 0010 0000"))
    only_4 = m.ptr(bytes.fromhex("a9f8 0000 0000 0000 0004 0020 0000"))

    def call(stub, cls):
        obj = m.handle(struct.pack(">H", cls))
        m.cpu.set_d(0, 0x1234)
        m.run(struct.pack(">HIHIH", 0x2F3C, obj, 0x4EB9, stub, 0x588F))
        assert m.cpu.sp == m.stack_top
        return m.cpu.d(0) & 0xFFFF
    assert [call(both, cls) for cls in (6, 4, 2, 8)] == [4, 4, 2, 2]
    assert mem.read(both + 2, 6) == bytes.fromhex("0001 0008 0010")   # the last found
    assert call(both, 8) == 2                               # from there
    assert call(only_4, 6) == 4
    assert call(only_4, 8) == 0xFFFF                        # none
    assert mem.r32(only_4 + 4) == 0
    assert m.missing == []


def test_slopes_and_angles_as_inside_macintosh_draws_them(qapp):
    """IM I-475: the slope is dh/dv, v growing downward, of a line that many
    degrees clockwise from 12 o'clock; each angle comes back from its slope."""
    g = Guest()
    assert g("SlopeFromAngle", 45) == -0x10000 and g("AngleFromSlope", -0x10000) == 45
    assert g("SlopeFromAngle", 0) == 0 and g("AngleFromSlope", 0) == 180
    assert g("SlopeFromAngle", -45) == g("SlopeFromAngle", 135) == 0x10000
    assert all(g("AngleFromSlope", g("SlopeFromAngle", a)) == a for a in range(1, 181))


def test_string_to_num_takes_each_characters_last_four_bits(qapp):
    """IM I-490's examples: no character is checked, a space is a zero
    (Idealiner Jr reads "WGF" as 776, its startup dialog's size)."""
    g = Guest()

    def num(s):
        return g("StringToNum", g.ptr(bytes([len(s)]) + s))[1]
    assert [num(s) for s in (b"12", b"-23", b"-0", b"055", b"2:", b"CAT", b"+CAT")] == \
        [12, 0xFFFFFFE9, 0, 55, 30, 314, 314]
    assert num(b"-CAT") == 0x100000000 - 314 and num(b"12 ") == 120
    assert num(b"2147483648") == num(b"-2147483648") == 0x80000000
    assert num(b"4294967295") == 0xFFFFFFFF and num(b"-4294967295") == 1
    assert num(b"WGF") == 776


def test_a_nil_string_is_the_one_at_address_0(qapp):
    """SCSI Evaluator has NumToString put its numbers at NIL and draws
    them from there."""
    g = Guest()
    port, _ = g.port(64, 16)
    g("NumToString", 1234, 0)
    assert g.mem.read(0, 5) == b"\x041234"
    assert g("StringWidth", 0) == g("StringWidth", g.ptr(b"\x041234")) > 0
    g("MoveTo", 2, 12)
    g("DrawString", 0)
    assert port.pn_loc.h == 2 + g("StringWidth", 0)


def extended(v: float) -> bytes:
    m, e = math.frexp(abs(v))
    return struct.pack(">HQ", (v < 0) << 15 | (e + 16382 if m else 0), int(m * 2 ** 64))


def test_fixed_point_routines_give_inside_macintoshs_examples(qapp):
    """The table of examples in Inside Macintosh IV-65, its values made
    with X2Fix and X2Frac."""
    g = Guest()

    def fix(v):
        return g("R_X2Fix", g.ptr(extended(v)))

    def frac(v):
        return g("R_X2Frac", g.ptr(extended(v)))

    def u(name, *args):
        return g(name, *args) & 0xFFFFFFFF
    pi = 3.1416015625                       # 4 * FixATan2(1, 1)
    for s in (1, -1):
        assert u("FixDiv", fix(s * 1.95), fix(1.30)) == (0x00018000, 0xFFFE8000)[s < 0]
        assert u("FracDiv", frac(s * 1.95), frac(1.30)) == (0x60000000, 0xA0000000)[s < 0]
        assert u("FracMul", frac(s * 1.50), frac(1.30)) == (0x7CCCCCCD, 0x83333333)[s < 0]
        assert u("FracSin", fix(s * pi)) == 0
        assert u("FracCos", fix(s * pi)) == 0xC0000000
        assert u("Fix2Long", fix(s * 1.75)) == (2, 0xFFFFFFFE)[s < 0]
        assert u("Fix2Frac", fix(s * 1.75)) == (0x70000000, 0x90000000)[s < 0]
        assert u("Frac2Fix", frac(s * 1.75)) == (0x0001C000, 0xFFFE4000)[s < 0]
        assert u("FixATan2", fix(s * 1.0), fix(s * 1.0)) == (0x0000C910, 0xFFFDA4D0)[s < 0]
    assert u("FracSqrt", frac(1.96)) == 0x5999999A
    assert u("FracSqrt", 0xFFFFFFFF) == 0x80000000          # unsigned, just under 4
    assert u("FracSin", fix(pi / 2)) == 0x40000000
    assert u("FixMul", fix(1.5), fix(-2.0)) == 0xFFFD0000
    assert u("FixMul", 0x7FFF0000, 0x7FFF0000) == 0x7FFFFFFF  # held
    assert u("FixDiv", -1, 0) == 0x80000000 and u("FracDiv", 0, 0) == 0x7FFFFFFF
    assert u("FixRatio", 1, 3) == 0x5555 and u("FixRatio", -2, 3) == 0xFFFF5555
    assert u("Long2Fix", -3) == 0xFFFD0000 and u("Long2Fix", 0x8000) == 0x7FFFFFFF


def test_inval_menu_bar_draws_the_bar_again(qapp, monkeypatch):
    g = Guest()
    drawn = []
    monkeypatch.setattr(g.tb, "DrawMenuBar", lambda: drawn.append(True))
    g("InvalMenuBar")
    assert drawn == [True]


def test_app_packs_has_the_packages_handles(qapp):
    """Date & Time looks there for Pack7's before it calls it."""
    g = Guest()
    rm = g.process.resources
    assert all(g.mem.r32(lowmem.AppPacks + 4 * n) == rm.GetResource("PACK", n)
               for n in range(system.APP_PACKS))
    g.mem.fill(lowmem.AppPacks, 4 * system.APP_PACKS)
    g("InitPack", 3)
    assert g.mem.r32(lowmem.AppPacks + 12) == rm.GetResource("PACK", 3)
    assert g.mem.r32(lowmem.AppPacks + 8) == 0
    after = g.mem.read(lowmem.SysResName, 20)
    g("InitPack", system.APP_PACKS)                    # past the table: nothing
    assert g.mem.read(lowmem.SysResName, 20) == after
    g("InitAllPacks")
    assert g.mem.r32(lowmem.AppPacks + 8) == rm.GetResource("PACK", 2)


def test_the_menu_manager_keeps_a_system_menus_size(qapp):
    """IM I-361: CalcMenuSize is called after every routine that changes a
    menu, and MDEF 0's menus are the size they're pulled down at: an
    application drawing its own pop-up box goes by menuWidth (MorseTrainer)."""
    g = MenuGuest()
    g.tb._d = SimpleNamespace(bridge=SimpleNamespace(call=lambda fn, *a: fn(*a)),
                              refresh_bars=lambda: None)
    h = g("NewMenu", 200, g.ptr(b"\x05Train"))
    g("AppendMenu", h, g.ptr(b"\x05Short"))
    short = g.menu(h).size
    assert short[0] > 0 and short[1] == theme.MENU_ITEM_HEIGHT
    g("InsertMenuItem", h, g.ptr(b"\x14A much longer item;-"), 0)
    wide = g.menu(h).size
    assert wide[0] > short[0] and wide[1] == 3 * theme.MENU_ITEM_HEIGHT
    g("DeleteMenuItem", h, 1)
    assert g.menu(h).size[1] == 2 * theme.MENU_ITEM_HEIGHT
    g.mem.w32(g.menu(h).ptr + 2, 0)                     # a program clears it
    g("CalcMenuSize", h)
    assert g.menu(h).size == (short[0], 2 * theme.MENU_ITEM_HEIGHT)


def test_a_menu_item_call_that_changes_nothing_doesnt_wait_for_the_gui(qapp, monkeypatch):
    """CheckItem of a checked item, SetMenuItemText with the item's text
    and either of an item past the last leave the menu as it is, and don't
    wait for the GUI thread: Dr. Quandary makes such calls on each pass of
    its event loop.  The menu's size is still set again, as it was worked
    out for the same items."""
    from macrt.ui import menubar
    sized, menu_size = [], menubar.menu_size
    monkeypatch.setattr(menubar, "menu_size", lambda m, s=None: sized.append(1) or menu_size(m, s))
    g = MenuGuest()
    calls = []
    bridge = SimpleNamespace(call=lambda fn, *a: calls.append(fn) or fn(*a))
    g.tb._d = SimpleNamespace(bridge=bridge, refresh_bars=lambda: None)
    h = g("NewMenu", 200, g.ptr(b"\x05Train"))
    g("AppendMenu", h, g.ptr(b"\x05Short"))
    calls.clear()
    g("CheckItem", h, 1, 1)
    assert len(calls) == 1 and g.menu(h).item(1).mark == k.checkMark
    g("CheckItem", h, 1, 1)
    g("SetMenuItemText", h, 1, g.ptr(b"\x05Short"))
    g("CheckItem", h, 9, 0)
    assert len(calls) == 1
    g("SetMenuItemText", h, 1, g.ptr(b"\x04Long"))
    g("CheckItem", h, 1, 0)
    it = g.menu(h).item(1)
    assert len(calls) == 3 and (it.text, it.mark) == ("Long", k.noMark)
    size, worked_out = g.menu(h).size, len(sized)
    g.mem.w32(g.menu(h).ptr + 2, 0)                     # a program clears it
    g("CheckItem", h, 1, 0)
    assert g.menu(h).size == size and len(calls) == 3 and len(sized) == worked_out
    g("CheckItem", h, 1, 1)                             # back to items it was worked out for
    assert len(calls) == 4 and len(sized) == worked_out


def test_only_enabling_or_disabling_a_menu_has_the_bar_drawn_again(qapp):
    """EnableItem and DisableItem post a refresh of the menu bar only for a
    change to the menu's own bit, the one the bar shows: Dr. Quandary
    disables and enables items on each pass of its event loop, and the bar
    was drawn again fifty times a second."""
    g = MenuGuest()
    posted = []
    bridge = SimpleNamespace(call=lambda fn, *a: fn(*a), post=posted.append)
    g.tb._d = SimpleNamespace(bridge=bridge, refresh_bars=lambda: None)
    h = g("NewMenu", 200, g.ptr(b"\x05Train"))
    g("AppendMenu", h, g.ptr(b"\x05Short"))
    g("DisableItem", h, 1)
    g("EnableItem", h, 1)
    g("DisableItem", h, 1)
    assert posted == [] and not g.menu(h).enable_flags & 2
    g("EnableItem", h, 0)
    assert posted == []
    g("DisableItem", h, 0)
    g("DisableItem", h, 0)
    assert len(posted) == 1 and not g.menu(h).enabled
    posted[0]()                                         # the bar shows it
    g("DisableItem", h, 0)
    g("EnableItem", h, 0)
    assert len(posted) == 2 and g.menu(h).enabled


def test_a_menus_items_read_from_its_bytes_in_one_go(qapp, monkeypatch):
    """menuData is read in chunks, more than one for a long menu, and its
    items come out as reading it byte by byte gives them."""
    from macrt.toolbox import menus
    g = MenuGuest()
    g.tb.menus_changed = lambda menu=None: None
    h = g("NewMenu", 200, g.ptr(b"\x05Train"))
    g.menu(h).append(parse_menu_items(";".join(f"Item number {i}/{chr(65 + i % 26)}"
                                               for i in range(100))))
    menu = g.menu(h)
    title, items = menu._parse()
    assert title == b"Train" and len(items) == 100 and items[99][0] == b"Item number 99"
    assert sum(1 + len(text) + 4 for text, _ in items) > menus.PARSE_CHUNK
    monkeypatch.setattr(menus, "PARSE_CHUNK", g.mem.size)      # more than RAM: byte by byte
    assert menu._parse() == (title, items)
    data = g.mem.read(menu.ptr + menus.MENU_DATA, 2000)
    assert menus._parse_menu_data(data[:100]) is None


def test_the_event_manager_calls_system_task_and_system_event_through_their_patches(qapp):
    from macrt.m68k import glue_desk
    g = Guest()
    task, event = 0xF00000, 0xF00010
    g.patches = {glue_desk.SYSTEM_TASK: task, glue_desk.SYSTEM_EVENT: event}
    g.in_patch, g.machine.drivers = set(), {}           # no desk accessories open
    g.mem.w8(lowmem.SEvtEnb, 0xFF)                      # as the runtime starts
    g.procs[task] = lambda: glue_desk.give_time(g)      # calls back in, and gets the System's
    g.procs[event] = lambda p: 1
    glue_desk.give_time(g)
    assert g.calls == [(task, ())]
    assert glue_desk.give_event(g, 0x1234) is True
    assert g.calls[-1] == (event, (("p", 0x1234),))
    g.patches.clear()
    assert glue_desk.give_event(g, g.ptr(bytes(16))) is False and len(g.calls) == 2


def test_still_down_is_false_with_a_mouse_event_queued_and_wait_mouse_up_takes_it(qapp):
    """Inside Macintosh I, Toolbox Event Manager: StillDown is true if the
    button is still down from the press, with no more mouse events in the
    queue, and WaitMouseUp, when it isn't, takes the mouse-up out."""
    g = Guest()
    events, tb = g.process.events, g.tb
    state = SimpleNamespace(down=True, presses=1, releases=0)
    tb._d = SimpleNamespace(input=state)
    assert tb.StillDown() and tb.WaitMouseUp()
    events.post(EventRecord(k.mouseUp))
    assert not tb.StillDown()
    assert not tb.WaitMouseUp()
    assert not events.peek(k.everyEvent, tb._next_update)[0]
    state.down, state.releases = False, 1               # up, and its mouse-up taken
    assert not tb.WaitMouseUp()


def test_the_system_heap_grows_as_gestalt_says(qapp):
    """SoundApp wants the system heap to grow and all of HFSDispatch
    (gestaltSysZoneGrowable, gestaltFullExtFSDispatching) before it
    starts."""
    g = Guest()
    err, os = Gestalt(g, int.from_bytes(b"os  ", "big"))
    assert err == noErr and os & 1
    err, fs = Gestalt(g, int.from_bytes(b"fs  ", "big"))
    assert err == noErr and fs & 0b11 == 0b11
    heap = g.system.sys_heap
    end = heap.end
    p = heap.new_ptr(end - heap.start)                          # more than it has
    assert p and heap.end > end and heap.end <= heap.limit
    assert g.mem.r32(heap.start) & 0xFFFFFF == heap.end         # bkLim
