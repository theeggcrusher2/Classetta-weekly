"""Standard File's traps as a program calls them, past what test_filemgr's
programs follow: a hook with no dialog of its own, a dialog of its own
with nothing of its own in it, no room for either, what's shown of an
application's items, a drawn item pressed, what the hook's answers do
(Open itself, Rebuild, Change Selection, the desktop), where a dialog
starts and what it leaves out, going up and making folders, and the
replies (Inside Macintosh: Files, chapter 3; IM I-519)."""

import struct
from concurrent.futures import Future
from contextlib import nullcontext

import pytest
from builders import ditl, dlog, pstr
from guest import Processor
from test_edition_traps import Host as EditionHost
from test_edition_traps import failing, publisher_reply
from test_editions import edition, new_section

from macrt.m68k import glue_dialogs as dm
from macrt.m68k import glue_editions as ed
from macrt.m68k import glue_stdfile as sf
from macrt.m68k.glue_files import ioNamePtr
from macrt.resfork import Resource
from macrt.toolbox import files as fm
from macrt.toolbox import lowmem
from macrt.toolbox.events import ProcessKilled
from macrt.toolbox.templates import (btnCtrl, chkCtrl, ctrlItem, editText, helpItem, itemDisable,
                                     resCtrl, userItem)
from macrt.toolbox.types import Point, Rect

WHERE = 0x00500050
HOOK, FILTER, DRAW = 0x3000, 0x3100, 0x3200
NONE = (userItem + itemDisable, (0, 0, 0, 0), b"")     # an item of Standard File's


class Host(EditionHost):
    """The display with Standard File's dialog up, which says which row
    of its list is selected at each step (rows, then none), and keeps
    what it was asked to show."""

    def __init__(self, *steps, typed="", rows=(), edits=None):
        super().__init__(*steps, typed=typed)
        self.rows, self.edits = list(rows), edits or {}
        self.places, self.resumed, self.finished = [], [], 0

    def show_place(self, place, extras):
        self.places.append(place)
        return super().show_place(place, extras)

    def resume(self, *args):
        self.resumed.append(args)
        return super().resume(*args)

    def state(self):
        return (self.rows.pop(0) if self.rows else -1), self.typed, self.edits

    def finish(self):
        self.finished += 1


def asking(t, *steps, **kw) -> Host:
    t.yielding = nullcontext
    t.tb._d = Host(*steps, **kw)
    return t.tb._d


def app(*resources):
    """An application with these resources, and room on its stack for a
    file filter's parameter block."""
    t = Processor(list(resources))
    t.stack_top = t.ptr(bytes(0x4000)) + 0x4000
    t.cpu.sp = t.stack_top
    return t


def docs(t):
    """Docs, a folder on the boot volume, where Standard File starts:
    Letter and Memo (TEXT), Sheet, Notes (invisible) and a folder, Old."""
    fs = t.system.files
    d = fs.make_dir(fs.boot.root, "Docs")
    for name, ftype in (("Letter", b"TEXT"), ("Memo", b"TEXT"), ("Sheet", b"XLS ")):
        fs.make_file(d, name, ftype)
    fs.make_file(d, "Notes", b"TEXT").flags = fm.kIsInvisible
    fs.make_dir(d, "Old")
    world = t.process.world
    world.w16(lowmem.SFSaveDisk, -fs.boot.vref)
    world.w32(lowmem.CurDirStore, d.cnid)
    return d


def names(place) -> list[str]:
    return [item.name for item in place.items]


def row(host, name) -> int:
    return names(host.shown[0][5]).index(name)


def item(t, w, n) -> tuple[int, int, Rect]:
    """A dialog item's kind, handle and rect, as GetDialogItem gives them."""
    kind, h, r = t.ptr(bytes(2)), t.ptr(bytes(4)), t.ptr(bytes(8))
    t("GetDialogItem", w, n, kind, h, r)
    return t.mem.r16(kind), t.mem.r32(h), t.mem.r_rect(r)


def cntl(rect, proc_id, title="", value=0, low=0, high=1) -> bytes:
    return struct.pack(">4hhBxhhhI", *rect, value, 1, high, low, proc_id, 0) + pstr(title)


def sf_get(t, hook=0, reply=None):
    reply = reply or t.ptr(bytes(sf.SF_REPLY))
    t("SFGetFile", WHERE, 0, 0, -1, 0, hook, reply)
    return reply


def custom_get(t, hook, reply=None, data=0):
    reply = reply or t.ptr(bytes(sf.STANDARD_REPLY))
    t("CustomGetFile", 0, -1, 0, reply, 0, WHERE, hook, 0, 0, 0, data)
    return reply


# =========================================================================
# The application's part of the dialog
# =========================================================================
def test_a_hook_with_no_dialog_of_its_own_finds_standard_files_items(qapp):
    """It's given a dialog with Standard File's items, the name to save as
    in item 7; a name it changes there is the one saved as."""
    t = app()
    docs(t)
    seen = []

    def hook(n, w):
        kinds = [item(t, w, i)[0] for i in range(1, 9)]
        _, h, _ = item(t, w, 7)
        seen.append((n, t.contents(h)))
        if n == 1:
            t("SetDialogItemText", h, t.ptr(pstr("Renamed")))
            title = t.ptr(bytes(256))
            t("GetControlTitle", item(t, w, 1)[1], title)
            seen.append(t.mem.r_pstr(title))
        assert kinds == [ctrlItem + btnCtrl] * 2 + [userItem + itemDisable] * 2 + [
            ctrlItem + btnCtrl] * 2 + [editText, userItem + itemDisable]
        return n
    t.procs[HOOK] = hook
    host = asking(t, ("save", "Letter 2", False), typed="Letter 2")
    reply = t.ptr(bytes(sf.SF_REPLY))
    t("SFPutFile", WHERE, 0, t.ptr(pstr("Untitled")), HOOK, reply)
    assert seen == [(sf.sfHookFirstCall, b"Untitled"), (1, b"Letter 2"), b"Save"]
    assert host.shown[0][9] and host.finished == 1
    assert t.mem.r8(reply + sf.rGood) == 1 and t.mem.r_pstr(reply + sf.rFName) == b"Renamed"


def _item_lists(monkeypatch, t, room=True) -> tuple[list, list]:
    """The item lists made for the application's dialog, and the handles
    disposed of; with no room, its dialog isn't made."""
    lists, gone = [], []
    copy, new_dialog, dispose = dm._copy_resource, dm.new_dialog, t.heap.dispose_handle

    def copied(*args):
        lists.append(copy(*args))
        return lists[-1]

    def dialog(*args):
        lists.append(args[-1])
        return new_dialog(*args) if room else None
    monkeypatch.setattr(dm, "_copy_resource", copied)
    monkeypatch.setattr(dm, "new_dialog", dialog)
    monkeypatch.setattr(t.heap, "dispose_handle", lambda h: gone.append(h) or dispose(h))
    return lists, gone


def test_a_dialog_of_its_own_with_nothing_of_its_own_is_standard_files(qapp, monkeypatch):
    """No hook, and no items past Standard File's: its item list is let
    go and the dialog has nothing of the application's."""
    t = app(Resource(b"DLOG", -4000, dlog((0, 0, 200, 348), "", 200, visible=False)),
            Resource(b"DITL", 200, ditl(*[NONE] * 9)))
    docs(t)
    lists, gone = _item_lists(monkeypatch, t)
    host = asking(t, ("cancel",))
    reply = sf_get(t)
    assert not host.shown[0][9] and len(lists) == 1 and lists[0] in gone
    assert t.mem.r8(reply + sf.rGood) == 0


@pytest.mark.parametrize("short", ["new_handle", "new_dialog"])
def test_no_room_for_the_applications_part_leaves_it_out(qapp, monkeypatch, short):
    """The dialog's item list, or its dialog: the dialog is Standard File's
    alone, its hook isn't called, and what was made is let go."""
    t = app()
    docs(t)
    seen = []
    t.procs[HOOK] = lambda *args: seen.append(args[-2]) or args[-2]   # the item
    for call in ("SFPutFile", "NewPublisherExpDialog"):
        lists, gone = _item_lists(monkeypatch, t, room=False)
        if short == "new_handle":
            failing(monkeypatch, t.heap, "new_handle")
        host = asking(t, ("cancel",))
        if call == "SFPutFile":
            t(call, WHERE, 0, 0, HOOK, t.ptr(bytes(sf.SF_REPLY)))
        else:
            assert t(call, publisher_reply(t), WHERE, 0, HOOK, 0, 0) == fm.noErr
        assert not host.shown[0][9] and len(lists) == (short == "new_dialog")
        assert set(lists) <= set(gone)
        monkeypatch.undo()
    assert seen == []


def test_an_expansion_with_no_items_or_no_hook(qapp):
    """The items of a DITL that isn't there are none; with no hook either,
    the dialog is still the application's, its items added at the bottom."""
    t = app(Resource(b"DITL", 300, ditl((ctrlItem + chkCtrl, (10, 10, 26, 120), b"Keep"))))
    docs(t)
    for ditl_id, extras in ((999, []), (300, [(sf.NEW[True].count + 1, "check", "Keep")])):
        host = asking(t, ("cancel",))
        assert t("NewPublisherExpDialog", publisher_reply(t), WHERE, ditl_id, 0, 0, 0) == fm.noErr
        assert host.shown[0][9]
        assert [(x.number, x.kind, x.title) for x in host.shown[0][6]] == extras
    # nor is the section options dialog short of memory for it
    spec, _ = edition(t)
    _, pub = new_section(t, spec, ed.stPublisher)
    reply = t.ptr(bytes(10))
    t.mem.w32(reply + ed.soSectionH, pub)
    host = asking(t, ("button", "cancel"))
    assert t("SectionOptionsExpDialog", reply, WHERE, 999, 0, 0, 0) == fm.noErr
    assert t.mem.r8(reply + ed.soCanceled) == 1


def _own_dialog():
    """SFGetFile's dialog of the application's own: a check box (10), a
    scroll bar (11), a user item (12), a help item (13) and a button
    (14) after Standard File's nine."""
    items = [NONE] * 9 + [
        (ctrlItem + chkCtrl, (150, 20, 166, 120), b"Show all"),
        (ctrlItem + resCtrl, (20, 300, 120, 316), struct.pack(">h", 128)),
        (userItem, (130, 200, 150, 260), b""),
        (helpItem, (170, 280, 190, 340), struct.pack(">hh", 1, 128)),
        (ctrlItem + btnCtrl, (170, 20, 190, 80), b"Gone")]
    return [Resource(b"DLOG", 300, dlog((0, 0, 200, 348), "", 300, visible=False)),
            Resource(b"DITL", 300, ditl(*items)),
            Resource(b"CNTL", 128, cntl((20, 300, 120, 316), 16, high=10))]


def test_what_is_shown_of_an_applications_own_items(qapp, monkeypatch):
    """Its check box as a check box; its scroll bar and its user item as
    the dialog draws them; not its help item, nor a control item whose
    handle the hook makes NIL.  A drawn item pressed has the dialog moved
    so that the item is where it's shown, until it's pressed there again."""
    t = app(*_own_dialog())
    d = docs(t)
    seen, drawn, moves = [], [], []
    move = t.tb.MoveWindow
    monkeypatch.setattr(t.tb, "MoveWindow", lambda w, h, v: moves.append((h, v)) or move(w, h, v))
    t.procs[DRAW] = lambda w, n: drawn.append(n)

    def hook(n, w):
        if n == sf.sfHookFirstCall:
            _, _, r = item(t, w, 12)
            rect = t.rect_ptr(tuple(r))
            t("SetDialogItem", w, 12, userItem, DRAW, rect)
            _, _, r = item(t, w, 14)
            t("SetDialogItem", w, 14, ctrlItem + btnCtrl, 0, t.rect_ptr(tuple(r)))
        elif n == 12:
            p = t.ptr(bytes(4))
            t.mem.w_point(p, Point(128, 198))       # the top left of what's shown of it
            t("LocalToGlobal", p)
            seen.append(t.mem.r_point(p))
        elif n == 10:
            t("HiliteControl", item(t, w, 11)[1], 255)
        return n
    t.procs[HOOK] = hook
    at = Point(250, 300)
    host = asking(t, ("item", 12, at), ("item", 12, at), ("item", 10), ("cancel",))
    reply = t.ptr(bytes(sf.SF_REPLY))
    t("SFPGetFile", WHERE, 0, 0, -1, 0, HOOK, reply, 300, 0)
    extras = host.shown[0][6]
    assert [(x.number, x.kind) for x in extras] == [(11, "drawn"), (12, "drawn"), (10, "check")]
    bar, user = extras[0], extras[1]
    assert bar.enabled and bar.image is not None and user.rect == (128, 198, 152, 262)
    assert bar.image.pixelColor(2, 2).black() > 0 and drawn and set(drawn) == {12}
    assert seen == [at, at] and moves == [(102, 122)]
    assert len(host.resumed) == 3 and t.mem.r8(reply + sf.rGood) == 0
    assert [x.enabled for x in host.resumed[2][0][:1]] == [False]    # the bar made inactive
    assert d.child("Letter") is not None


# =========================================================================
# What the hook's answers do
# =========================================================================
def test_a_hook_that_opens_what_is_selected_itself(qapp):
    """Open, from the hook: the file selected, or the folder selected or,
    with nothing selected, the folder shown; in the original calls' reply
    with no file type, in a working directory for the folder holding it
    (macrt's own rule), and in System 7's, as a folder."""
    t = app()
    d = docs(t)
    boot = t.system.files.boot
    t.procs[HOOK] = lambda n, w, *data: 1 if n == sf.sfHookNullEvent else n
    for rows, chosen, ftype, holder in (([1], d.child("Memo"), b"TEXT", d),
                                        ([2], d.child("Old"), bytes(4), d),
                                        ([], d, bytes(4), boot.root)):
        host = asking(t, ("select",), rows=rows)
        reply = sf_get(t, HOOK)
        assert names(host.shown[0][5]) == ["Letter", "Memo", "Old", "Sheet"]
        assert t.mem.r8(reply + sf.rGood) == 1
        assert t.mem.r_pstr(reply + sf.rFName) == chosen.name.encode()
        assert t.mem.read(reply + sf.rFType, 4) == ftype
        wd = t.mem.r16s(reply + sf.rVRefNum)
        assert t.process.files.lookup(wd, 0, chosen.name) is chosen
        assert t.process.files.lookup(wd, 0, "") is holder
    t.process.world.w32(lowmem.CurDirStore, fm.ROOT)
    asking(t, ("select",))
    reply = sf_get(t, HOOK)
    assert t.mem.r_pstr(reply + sf.rFName) == boot.name.encode()
    assert t.process.files.lookup(t.mem.r16s(reply + sf.rVRefNum), 0, "") is boot.root
    docs(t)

    for rows, chosen in (([2], "Old"), ([], "Docs")):
        asking(t, ("select",), rows=rows)
        reply = custom_get(t, HOOK)
        assert t.mem.r8(reply + sf.sfGood) == 1 and t.mem.r8(reply + sf.sfIsFolder) == 1
        assert t.mem.r_pstr(reply + sf.sfFile + 6) == chosen.encode()


def test_what_a_hooks_other_answers_do(qapp):
    """Rebuild shows the folder again; Change Selection the folder of what
    the hook put in the reply (not, if there's no such disk), or the
    desktop with a disk selected; Go To Desktop the desktop; any other
    item leaves the dialog as it is."""
    t = app()
    d = docs(t)
    old = d.child("Old")
    boot = t.system.files.boot
    reply = t.ptr(bytes(sf.STANDARD_REPLY))
    m = t.mem

    def points_at(vref, parid, name):
        def put():
            m.w16(reply + sf.sfFile, vref & 0xFFFF)
            m.w32(reply + sf.sfFile + 2, parid)
            m.w_pstr(reply + sf.sfFile + 6, name)
            return sf.sfHookChangeSelection
        return put
    plan = [lambda: sf.sfHookRebuildList,
            points_at(boot.vref, old.cnid, b""),
            points_at(999, fm.ROOT, b"Gone"),              # no such disk
            lambda: sf.sfHookGoToDesktop,
            points_at(boot.vref, fm.ROOT_PARENT, boot.name.encode()),
            lambda: 42]
    seen = []

    def hook(n, w, data):
        seen.append(n)
        return plan.pop(0)() if n == sf.sfHookNullEvent else n
    t.procs[HOOK] = hook
    host = asking(t, *[("select",)] * 6, ("cancel",))
    custom_get(t, HOOK, reply)
    assert [(p.path[:1], p.selected) for p in host.places] == [
        (["Docs"], -1), (["Old"], -1), ([], -1), ([], 0)]
    assert len(host.resumed) == 2
    assert seen == [sf.sfHookFirstCall] + [sf.sfHookNullEvent] * 6 + [2, sf.sfHookLastCall]
    assert m.r8(reply + sf.sfGood) == 0


def test_the_current_directory_while_a_dialog_is_up(qapp):
    """SFSaveDisk and CurDirStore say the folder shown (Inside Macintosh:
    Files, "Setting the Current Directory"); a hook that changes them and
    returns 101 has the folder they name shown (Technical Note FL 33), and
    one that returns 101 leaving them as they are, the same folder."""
    t = app()
    d = docs(t)
    old = d.child("Old")
    world = t.process.world
    seen = []

    def hook(n, w):
        if n != sf.sfHookNullEvent:
            return n
        seen.append((world.r16(lowmem.SFSaveDisk), world.r32(lowmem.CurDirStore)))
        if len(seen) == 2:
            world.w32(lowmem.CurDirStore, d.cnid)
        return sf.sfHookRebuildList if len(seen) in (2, 3) else n
    t.procs[HOOK] = hook
    host = asking(t, ("open", 2), *[("select",)] * 4, ("cancel",))
    sf_get(t, HOOK)
    vref = -t.system.files.boot.vref & 0xFFFF
    assert seen == [(vref, old.cnid), (vref, old.cnid), (vref, d.cnid), (vref, d.cnid)]
    assert [p.path[0] for p in host.places] == ["Old", "Docs", "Docs"]
    assert world.r32(lowmem.CurDirStore) == d.cnid


def test_a_list_rebuilt_after_the_hook_changes_the_types(qapp):
    """A hook that changes the types in the type list and asks for the
    list to be rebuilt has the files of the new types listed."""
    t = app()
    docs(t)
    types = t.ptr(b"TEXT")

    def hook(n, w):
        if n != sf.sfHookNullEvent or t.mem.read(types, 4) != b"TEXT":
            return n
        t.mem.write(types, b"XLS ")
        return sf.sfHookRebuildList
    t.procs[HOOK] = hook
    host = asking(t, ("select",), ("cancel",))
    t("SFGetFile", WHERE, 0, 0, 1, types, HOOK, t.ptr(bytes(sf.SF_REPLY)))
    assert names(host.shown[0][5]) == ["Letter", "Memo", "Old"]
    assert [names(p) for p in host.places] == [["Old", "Sheet"]]


def test_a_hooks_first_answer(qapp):
    """Go To Desktop starts System 7's dialog on the desktop.  Change
    Selection starts it in the folder holding what the reply describes,
    that selected: before the hook's first call, the reply describes the
    folder it would have started in.  With no reply (the original
    calls'), or one naming no disk there is, it starts where it would
    have."""
    t = app()
    docs(t)
    reply = t.ptr(bytes(sf.STANDARD_REPLY))
    answers, starts = [], []

    def hook(n, w, *data):
        if n != sf.sfHookFirstCall:
            return n
        first, vref = answers.pop(0)
        if vref is not None:
            t.mem.w16(reply + sf.sfFile, vref)
        return first
    t.procs[HOOK] = hook
    for first, vref, call in ((sf.sfHookGoToDesktop, None, custom_get),
                              (sf.sfHookChangeSelection, None, sf_get),
                              (sf.sfHookChangeSelection, 999, custom_get),
                              (sf.sfHookChangeSelection, None, custom_get)):
        answers.append((first, vref))
        host = asking(t, ("cancel",))
        call(t, HOOK, reply if call is custom_get else None)
        place = host.shown[0][5]
        starts.append((place.path[:1], place.selected))
    assert starts == [([], -1), (["Docs"], -1), (["Docs"], -1),
                      (["Macintosh HD"], names(place).index("Docs"))]


def test_a_dialog_the_application_is_stopped_in(qapp):
    """It's taken down."""
    t = app()
    docs(t)
    host = asking(t)
    host._next = Future                         # the user never does anything
    host.bridge.closed = True
    with pytest.raises(ProcessKilled):
        sf_get(t)
    assert host.finished == 1


# =========================================================================
# Where it starts, what it shows, going up and making folders
# =========================================================================
def test_where_a_dialog_starts_and_what_it_leaves_out(qapp):
    """With no disk where the last one finished, it's the default
    folder.  Invisible files aren't shown, nor what the file filter leaves
    out: System 7's is asked about folders too."""
    t = app()
    d = docs(t)
    world = t.process.world
    world.w16(lowmem.SFSaveDisk, 77)
    host = asking(t, ("cancel",))
    sf_get(t)
    vol, _, _ = t.process.files.get_vol()
    assert host.shown[0][5].path == [vol.name]
    world.w16(lowmem.SFSaveDisk, -t.system.files.boot.vref)
    world.w32(lowmem.CurDirStore, d.cnid)
    m = t.mem
    t.procs[FILTER] = lambda pb: m.r_pstr(m.r32(pb + ioNamePtr)) in (b"Old", b"Sheet")
    host = asking(t, ("cancel",))
    t("StandardGetFile", FILTER, -1, 0, t.ptr(bytes(sf.STANDARD_REPLY)))
    assert names(host.shown[0][5]) == ["Letter", "Memo"]
    host = asking(t, ("cancel",))
    sf_get(t)
    assert names(host.shown[0][5]) == ["Letter", "Memo", "Old", "Sheet"]


def test_going_up_and_making_folders(qapp):
    """Up some levels, past the disk to the desktop; a folder that can't
    be made says why, and the dialog stays where it was."""
    t = app()
    docs(t)
    host = asking(t, ("new_folder", "Old"), ("new_folder", "a:b"),     # on a disk a, not there
                  ("up", 1), ("up", 5), ("cancel",))
    t("StandardPutFile", 0, 0, t.ptr(bytes(sf.STANDARD_REPLY)))
    assert [(p.path[:1], p.message) for p in host.places] == [
        (["Docs"], "That name is already taken."),
        (["Docs"], f"The folder couldn’t be created (error {fm.nsvErr})."),
        (["Macintosh HD"], ""), ([], "")]


def test_a_hook_hears_of_going_up_and_new_folders(qapp):
    """The original dialogs have no New Folder item, so their hooks don't
    hear of one; System 7's hooks do."""
    t = app()
    d = docs(t)
    seen = []
    t.procs[HOOK] = lambda n, w, *data: seen.append(n) or n
    asking(t, ("new_folder", "Stuff"), ("up", 1), ("cancel",))
    t("SFPutFile", WHERE, 0, 0, HOOK, t.ptr(bytes(sf.SF_REPLY)))
    assert seen == [sf.sfHookFirstCall, sf.sfHookFolderPopUp, sf.ORIGINAL[True].cancel]
    assert isinstance(d.child("Stuff"), fm.Dir)

    del seen[:]
    asking(t, ("new_folder", "More"), ("save", "Report", False), typed="Report")
    reply = t.ptr(bytes(sf.STANDARD_REPLY))
    t("CustomPutFile", 0, 0, reply, 0, WHERE, HOOK, 0, 0, 0, 0)
    n = sf.NEW[True]
    assert seen == [sf.sfHookFirstCall, n.new_folder, n.ok, sf.sfHookLastCall]
    more = d.child("More")
    assert t.mem.r8(reply + sf.sfGood) == 1 and t.mem.r32(reply + sf.sfFile + 2) == more.cnid
    assert t.mem.r_pstr(reply + sf.sfFile + 6) == b"Report"


# =========================================================================
# Replies
# =========================================================================
def test_the_replies_of_the_calls_with_their_own_dialogs(qapp):
    """SFPGetFile and SFPPutFile with no dialog of their own are SFGetFile
    and SFPutFile; a name saved as is the file's own if there's one."""
    t = app()
    docs(t)
    asking(t, ("cancel",))
    reply = t.ptr(bytes(sf.SF_REPLY))
    t("SFPGetFile", WHERE, 0, 0, -1, 0, 0, reply, 0, 0)
    assert t.mem.r8(reply + sf.rGood) == 0
    asking(t, ("save", "Draft", False))
    t("SFPPutFile", WHERE, 0, t.ptr(pstr("Untitled")), 0, reply, 0, 0)
    assert t.mem.r8(reply + sf.rGood) == 1 and t.mem.r_pstr(reply + sf.rFName) == b"Draft"
    asking(t, ("save", "letter", False))
    reply = t.ptr(bytes(sf.STANDARD_REPLY))
    t("StandardPutFile", 0, 0, reply)
    assert (t.mem.r8(reply + sf.sfGood), t.mem.r8(reply + sf.sfReplacing)) == (1, 1)
    assert t.mem.r_pstr(reply + sf.sfFile + 6) == b"Letter"
