"""
Standard File (Pack3; Inside Macintosh: Files, chapter 3; IM I-519 and
IV-71): SFGetFile, SFPutFile, SFPGetFile, SFPPutFile, StandardGetFile,
StandardPutFile, CustomGetFile and CustomPutFile, all with the dialog in
macrt.ui.stdfile.

A dialog starts where the last one finished, which SFSaveDisk and
CurDirStore say (at launch, the application's folder), and leaves them
where it finishes.  What a directory shows is worked out here, on the
application's thread, because the application's file filter may leave
things out: it gets a CInfoPBRec, as PBGetCatInfo fills one in, and
returns TRUE for something not to show.  It's asked about the files whose
types are in the type list; System 7's calls ask it about folders too.

The dialog is macrt's own, a Qt one, but an application's own part of
it is kept: the items its dialog (the ID it gives, or DLOG -3999 and
-4000 of its own) has after Standard File's own, any control it puts at
a number Standard File's dialog gives no part, and its dialog hook.
Items kept outside the dialog aren't shown.
The Dialog Manager makes the application's dialog, which is never shown;
its push buttons, check boxes, radio buttons, pop-up menus, static text
and edit text are shown as widgets, and what it draws itself (user
items, pictures, icons and controls with their own CDEF) is drawn into
an image of the item, which is shown.  The hook is called as a Mac calls
it, with the item the user chose (an edit text item each time it's
typed in, as ModalDialog returns it, with what's been typed in it by
then; or, for what isn't an item here, the item number System 7 gives
it), and what it returns is what's done.  For a drawn item the invisible
dialog is first moved to where the item is shown, so that what the hook
does with the mouse (a pop-up of its own, say) is where the user sees
it; a pop-up menu's value is set to the item chosen before the hook
hears of it.  The widgets show what the hook leaves the items as.
While a dialog is up, SFSaveDisk and CurDirStore say the folder shown,
the current directory ("the directory whose contents are listed in the
dialog box", Inside Macintosh: Files, "Setting the Current Directory"),
and a hook that changes them and returns 101 (sfHookRebuildList) has
the folder they name shown (Technical Note FL 33, "Standard File
Customization").  While System 7's dialog is up, its reply record says
what's selected in the list (with nothing selected, the folder shown),
as a hook that picks a folder reads it (Inside Macintosh: Files,
"Selecting a Directory"); a dialog the hook ends with Cancel leaves it
saying so, sfGood FALSE.  One the hook ends with Open itself, for
another item, while a folder or disk is selected, or nothing, returns
that or the folder shown: in System 7's reply, sfGood TRUE, and, as
macrt's own rule, in the original calls' SFReply, good TRUE, its name
with no file type in a working directory for the folder holding it (a
disk's, for the disk).  A hook can also ask for the folder holding what
it puts in the reply, with that selected (sfHookChangeSelection; on its first
call, where the dialog starts), or for the desktop (sfHookGoToDesktop,
as the Desktop button is to it).  The hook hears sfHookNullEvent when
a folder's list is shown and each time the selection changes, where a
Mac sends it whenever nothing else happens.  The list it asks to be
rebuilt (sfHookRebuildList) is made from the type list as the hook has
left it, so a hook that changes the types to be displayed, as IM Files
has it, changes what's listed.  The modal-dialog filter, the activation
list and procedure aren't used.  Without a display (no one to ask) every
dialog is cancelled.
"""

from __future__ import annotations

import struct
from contextlib import contextmanager
from dataclasses import dataclass

from PyQt6.QtGui import QColor, QImage

from ..memory import ADDRESS_MASK, from_mac, to_mac
from ..toolbox import files as fm
from ..toolbox import lowmem
from ..toolbox.dialogs import substitute
from ..toolbox.events import ProcessKilled
from ..toolbox.files import Dir, File, FileError
from ..toolbox.qdraw import ImageSurface, Region, read_rgn, write_rgn
from ..toolbox.templates import (btnCtrl, ctrlItem, editText, iconItem, itemDisable, parse_dlog,
                                 picItem, statText, userItem)
from ..toolbox.types import Point, Rect
from ..ui import stdfile
from . import glue_dialogs as dm
from . import popups
from .dispatch import trap
from .glue_controls import SetControlValue, _ctl, _proc_id
from .glue_files import ioFDirIndex, ioNamePtr, ioVRefNum, put_cat_info

CINFO_PB = 108                  # sizeof(CInfoPBRec)
MAX_TYPES = 64

# SFReply (IM I-519)
rGood, rCopy, rFType, rVRefNum, rVersion, rFName = 0, 1, 2, 6, 8, 10
SF_REPLY = 74
# StandardFileReply (Inside Macintosh: Files, chapter 3)
sfGood, sfReplacing, sfType, sfFile, sfScript, sfFlags, sfIsFolder, sfIsVolume = (
    0, 1, 2, 6, 76, 78, 80, 81)
STANDARD_REPLY = 88

MORE = object()                 # a step that leaves the dialog up, somewhere else
SAME = object()                 # one that leaves it up where it is

# What a dialog hook is told besides its items (Inside Macintosh: Files 3-30)
sfHookFirstCall, sfHookLastCall = -1, -2
sfHookNullEvent, sfHookRebuildList, sfHookFolderPopUp, sfHookOpenFolder = 100, 101, 102, 103
sfHookGoToDesktop, sfHookChangeSelection = 105, 110
sfMainDialogRefCon = 0x73746466             # 'stdf'


@dataclass(frozen=True)
class Numbers:
    """A Standard File dialog's own items, as its hook knows them."""
    ok: int                     # Open or Save
    cancel: int
    eject: int
    drive: int                  # Drive; System 7's Desktop
    name: int                   # the name to save as, 0 for none
    new_folder: int             # 0 for none
    count: int                  # how many are Standard File's; the application's follow


# IM I-523 and Inside Macintosh: Files 3-19, by put.
ORIGINAL = {False: Numbers(1, 3, 5, 6, 0, 0, 9), True: Numbers(1, 2, 5, 6, 7, 0, 8)}
NEW = {False: Numbers(1, 2, 5, 6, 0, 0, 9), True: Numbers(1, 2, 5, 6, 10, 12, 12)}
DIALOG_IDS = {False: -4000, True: -3999}    # the original calls' dialogs
CDEF_KINDS = ("button", "check", "radio")   # CDEF 0's variations
DRAWN = (userItem, picItem, iconItem)       # the items a dialog draws itself, but controls
OUTSET = 2                  # what's drawn round an item: a shadow, a frame outside its rect


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _standard_items(numbers: Numbers, ok: bytes = b"Open") -> bytes:
    """An item list for a dialog with Standard File's items and none of
    the application's, for a hook to find them in: the buttons, the name
    to save as, and user items for the rest."""
    titles = {numbers.ok: ok, numbers.cancel: b"Cancel", numbers.eject: b"Eject",
              numbers.drive: b"Drive"}
    out = struct.pack(">h", numbers.count - 1)
    for n in range(1, numbers.count + 1):
        if n in titles:
            kind, data = ctrlItem + btnCtrl, titles[n]
        elif n == numbers.name:
            kind, data = editText, b""
        else:
            kind, data = itemDisable, b""
        out += struct.pack(">I4hBB", 0, 0, 0, 0, 0, kind, len(data)) + data
        out += bytes(len(data) & 1)
    return out


class Custom:
    """An application's own part of a Standard File dialog: its dialog
    hook, and its dialog, which has its own items after Standard File's.
    The dialog is made as the Dialog Manager makes one, invisible."""

    def __init__(self, t, w, numbers: Numbers, hook: int, new: bool, data: int):
        self.t = t
        self.w = w
        self.numbers = numbers
        self.hook = hook & ADDRESS_MASK
        self.new = new                      # System 7's: the hook gets yourDataPtr too
        self.data = data
        self.offset: int | None = None      # an expandable hook's itemOffset
        self.item_rect = {}                 # each item's rect at first, to lay them out

    @classmethod
    def make(cls, t, put: bool, hook: int, dlg_id: int, new: bool = False,
             data: int = 0) -> Custom | None:
        """The application's part, if it has one: a hook, or a dialog with
        items of its own."""
        numbers = (NEW if new else ORIGINAL)[put]
        hook &= ADDRESS_MASK
        h = t.process.resources.GetResource("DLOG", dlg_id) if dlg_id else 0
        if h:
            tmpl = parse_dlog(t.tb.contents(h))
            item_list = dm._copy_resource(t, "DITL", tmpl.items_id)
            bounds = tmpl.bounds
            proc_id, go_away, ref_con, title = (tmpl.proc_id, tmpl.go_away, tmpl.ref_con,
                                                tmpl.title)
        else:
            item_list = 0
        if item_list and not hook and len(dm.items(t.mem, item_list)) <= numbers.count:
            t.system.heap_of(item_list).dispose_handle(item_list)
            return None
        if not item_list:
            if not hook:
                return None
            data_ = _standard_items(numbers, b"Save" if put else b"Open")
            item_list = t.process.heap.new_handle_with(data_)
            if not item_list:
                return None
            bounds, proc_id, go_away, ref_con, title = Rect(0, 0, 200, 348), 1, False, 0, ""
        w = dm.new_dialog(t, 0, bounds, title, False, proc_id, dm.IN_FRONT, go_away,
                          sfMainDialogRefCon if new else ref_con, item_list)
        if w is None:
            t.system.heap_of(item_list).dispose_handle(item_list)
            return None
        out = cls(t, w, numbers, hook, new, data)
        out.item_rect = {item.number: item.rect for item in dm._items(t, w)}
        return out

    @classmethod
    def expanded(cls, t, numbers: Numbers, ditl_id: int, hook: int, data: int,
                 bounds: Rect = Rect(0, 0, 200, 348), refcon: int = sfMainDialogRefCon,
                 ok: bytes = b"Open") -> Custom | None:
        """A dialog with a hook that's told the item offset, as the Edition
        Manager's are: the standard items (Standard File's, or as numbers
        says), then the items of DITL ditl_id (none if there's no such DITL),
        kept where they are to each other, at the bottom."""
        hook &= ADDRESS_MASK
        h = t.process.resources.GetResource("DITL", ditl_id) if ditl_id else 0
        expansion = t.tb.contents(h) if h else b""
        data_ = _standard_items(numbers, ok)
        item_list = t.process.heap.new_handle(len(data_))
        if not item_list:
            return None
        t.mem.write(t.process.heap.deref(item_list), data_)
        w = dm.new_dialog(t, 0, bounds, "", False, 1, dm.IN_FRONT, False, refcon, item_list)
        if w is None:
            t.system.heap_of(item_list).dispose_handle(item_list)
            return None
        if expansion:
            dm.append_ditl(t, w, expansion, dm.appendDITLBottom)
        out = cls(t, w, numbers, hook, True, data)
        out.offset = numbers.count
        out.item_rect = {item.number: item.rect for item in dm._items(t, w)}
        return out

    def call(self, item: int) -> int:
        """The hook's answer to an item; the item itself without a hook."""
        if not self.hook:
            return item
        t = self.t
        args = (("h", item), ("p", self.w.addr))
        if self.offset is not None:
            args = (("h", self.offset),) + args
        if self.new:
            args += (("p", self.data),)
        with dm._in_port(t, self.w):
            return _s16(t.call(self.hook, args, "h"))

    # The name to save as, in its item for the hook to read and change.
    def _name_item(self):
        return dm._item(self.t, self.w, self.numbers.name) if self.numbers.name else None

    def set_name(self, name: str) -> None:
        item = self._name_item()
        if item is not None and item.handle & ADDRESS_MASK:
            dm._set_handle(self.t, item.handle & ADDRESS_MASK, to_mac(name)[:255])

    def name(self) -> str | None:
        item = self._name_item()
        if item is None or not item.handle & ADDRESS_MASK:
            return None
        return from_mac(dm._text(self.t, item.handle))

    def set_edits(self, edits: dict[int, str]) -> None:
        """What the user has typed in the application's edit text items,
        for its hook to read."""
        for n, text in edits.items():
            item = dm._item(self.t, self.w, n)
            if item is not None and item.type == editText and item.handle & ADDRESS_MASK:
                dm._set_handle(self.t, item.handle & ADDRESS_MASK, to_mac(text)[:255])

    def _theirs(self, item) -> bool:
        """Whether an item is the application's own: one after Standard
        File's, or a control at a number Standard File gives no part (as
        Idealiner's New button is SFGetFile's item 2)."""
        n = self.numbers
        if item.number > n.count:
            return True
        roles = (n.ok, n.cancel, n.eject, n.drive, n.name, n.new_folder)
        return item.is_control and item.number not in roles

    def extras(self) -> list[stdfile.Extra]:
        """The application's own items, as they are now, but those kept
        outside its dialog."""
        t, w = self.t, self.w
        port = w.port_rect
        params = dm._params(t)
        out, drawn = [], []
        for item in dm._items(t, w):
            if not self._theirs(item):
                continue
            first = self.item_rect.get(item.number, item.rect)
            if first.sect(port).empty and item.rect.sect(port).empty:
                continue
            choices = ()
            if item.is_control:
                c = _ctl(t, item.handle)
                if c is None:
                    continue
                proc_id = _proc_id(t, c)
                if popups.is_popup(proc_id):
                    menu = popups.menu_of(t, c)
                    kind, title, value = "popup", c.title, c.value
                    choices = tuple(it.shown for it in menu.items) if menu is not None else ()
                elif proc_id >> 4 != 0 or proc_id & 3 == 3:
                    drawn.append(item)          # a scroll bar, or a CDEF of its own
                    continue
                else:
                    kind, title, value = CDEF_KINDS[proc_id & 3], c.title, c.value
                enabled = item.enabled and c.active
                visible = c.visible and not item.hidden and not c.rect.sect(port).empty
            elif item.type in DRAWN:
                if item.type != userItem or item.handle & ADDRESS_MASK:
                    drawn.append(item)
                continue
            elif item.type == statText:
                kind, title, value = "text", from_mac(substitute(dm._text(t, item.handle),
                                                                 params)), 0
                enabled, visible = True, not item.hidden and not item.rect.sect(port).empty
            elif item.type == editText:             # disabled, it's still typed in
                kind, title, value = "edit", from_mac(dm._text(t, item.handle)), 0
                enabled, visible = True, not item.hidden and not item.rect.sect(port).empty
            else:
                continue
            out.append(stdfile.Extra(item.number, kind, title, tuple(first), value, enabled,
                                     visible, choices))
        return self._drawn(drawn) + out

    # What the dialog draws itself: the application's user items, with
    # its own procedures, its pictures and icons, and controls of its own.
    def _place(self, r: Rect) -> Rect:
        """Where an item drawn in r is shown: with what's drawn round it,
        in the dialog."""
        return r.inset(-OUTSET, -OUTSET).sect(self.w.port_rect)

    def _drawn(self, items: list) -> list[stdfile.Extra]:
        """Each item as the dialog draws it now, in an image."""
        if not items:
            return []
        t, w = self.t, self.w
        port = w.port_rect
        img = QImage(max(port.width, 1), max(port.height, 1), QImage.Format.Format_RGB32)
        img.fill(QColor(255, 255, 255))
        with self._drawing(img):
            for item in sorted(items, key=lambda item: not item.is_control):
                dm._draw_item(t, w, item)
        out = []
        for item in items:
            first = self._place(self.item_rect.get(item.number, item.rect))
            now = self._place(item.rect)
            visible = not item.hidden and not now.empty
            image = None
            if visible:
                image = img.copy(now.left - port.left, now.top - port.top,
                                 min(now.width, first.width), min(now.height, first.height))
            enabled = item.enabled
            if item.is_control:
                c = _ctl(t, item.handle)
                enabled = enabled and c is not None and c.active
            out.append(stdfile.Extra(item.number, "drawn", "", tuple(first), 0, enabled,
                                     visible, image=image))
        return out

    @contextmanager
    def _drawing(self, img: QImage):
        """The dialog drawing in img, as if it were shown: its visRgn is
        all of it meanwhile."""
        t, w = self.t, self.w
        surfaces = t.tb.qd.surfaces
        vis = w.vis_rgn & ADDRESS_MASK
        kept = read_rgn(t.mem, vis) if vis else None
        if vis:
            write_rgn(t.system.heap_of(vis), vis, Region.rect(w.port_rect))
        surfaces[w.addr] = ImageSurface(img, w.port_rect)
        try:
            yield
        finally:
            surfaces.pop(w.addr, None)          # (the dialog is never shown: it had none)
            if kept is not None:
                write_rgn(t.system.heap_of(vis), vis, kept)

    def pressed(self, n: int, where: Point | None) -> None:
        """A drawn item pressed, shown with its top left at where on the
        screen: the dialog is moved there, unseen, so that what the
        application pops up there, and the mouse, are where it's shown."""
        item = dm._item(self.t, self.w, n)
        if where is None or item is None:
            return
        at = self._place(self.item_rect.get(n, item.rect))
        port = self.w.port_rect
        content = self.w.content
        h, v = where.h - (at.left - port.left), where.v - (at.top - port.top)
        if (h, v) != (content.left, content.top):
            self.t.tb.MoveWindow(self.w, h, v)

    def pick(self, n: int, value: int) -> None:
        """A pop-up's item chosen: the control's value, as TrackControl
        leaves it."""
        item = dm._item(self.t, self.w, n)
        if item is not None and item.is_control and _ctl(self.t, item.handle) is not None:
            SetControlValue(self.t, item.handle, value)

    def dispose(self) -> None:
        dm.close_dialog(self.t, self.w, dispose=True)


class Dialog:
    """One Standard File dialog, from the application's side."""

    def __init__(self, t, put: bool, prompt: str = "", name: str = "", file_filter: int = 0,
                 type_list: tuple[int, int] = (-1, 0), data: int | None = None,
                 folders: bool = False, hook: int = 0, dlg_id: int = 0, new: bool = False,
                 reply: int = 0):
        self.t = t
        self.files: fm.FileManager = t.process.files
        self.fs: fm.FileSystem = t.system.files
        self.put = put
        self.prompt = prompt
        self.name = name
        self.filter = file_filter & ADDRESS_MASK
        self.type_list = type_list          # numTypes, and where the types are
        self.types = _types(t, *type_list)  # None: every type
        self.data = data                    # CustomGetFile's yourDataPtr, for its filter
        self.folders = folders              # does the filter see folders
        self.dir: Dir | None = self._start()        # None: the desktop
        self.entries: list = []             # what each item in the list is
        self.message = ""
        self.custom_args = (hook, dlg_id, new)
        self.custom: Custom | None = None
        self.new_name: str | None = None    # what a hook changed the name to save as to
        self.ok = ""                        # the default button's title, if not Open or Save
        self.preview: QImage | None = None  # shown beside the list
        self.reply = reply & ADDRESS_MASK   # System 7's, to say what's selected
        self.described = False              # has it said so
        self.select: fm.Node | None = None  # what to select when the next place is shown
        self.saved: tuple[int, int] | None = None   # what SFSaveDisk and CurDirStore were set to

    def _start(self) -> Dir:
        d = self._current()
        if d is None:
            vol, _, dirid = self.files.get_vol()
            d = vol.dir(dirid)
        return d

    def _current(self) -> Dir | None:
        """The folder SFSaveDisk and CurDirStore name, if there is one."""
        world = self.t.process.world
        try:
            vol = self.fs.volume(-world.r16(lowmem.SFSaveDisk))
            d = vol.dir(world.r32(lowmem.CurDirStore) or fm.ROOT)
        except FileError:
            return None
        return vol.root if d is vol.top else d

    # =====================================================================
    # What the dialog shows
    # =====================================================================
    def place(self) -> stdfile.Place:
        self._remember()
        message, self.message = self.message, ""
        place = self._place(message)
        select, self.select = self.select, None
        place.selected = next((i for i, node in enumerate(self.entries) if node is select), -1)
        return place

    def _place(self, message: str) -> stdfile.Place:
        d = self.dir
        if d is None:
            self.entries = list(self.fs.volumes)
            return stdfile.Place([], [stdfile.Item(v.name, "volume") for v in self.entries],
                                 message=message)
        self.entries, items = [], []
        for index, node in enumerate(d.items(), start=1):
            if node.flags & fm.kIsInvisible:
                continue
            if isinstance(node, Dir):
                if self.folders and self._filtered(node, index):
                    continue
                item = stdfile.Item(node.name, "folder")
            else:
                if not self.put and not self._wanted(node, index):
                    continue
                kind = "application" if node.type == b"APPL" else "document"
                item = stdfile.Item(node.name, kind, enabled=not self.put)
            self.entries.append(node)
            items.append(item)
        path, n = [], d
        while n is not None:
            path.append(n.volume.name if n is n.volume.root else n.name)
            n = n.parent
        return stdfile.Place(path, items, d.volume.name, not d.volume.lock_error, message)

    def _wanted(self, node: File, index: int) -> bool:
        if self.types is not None and node.type not in self.types:
            return False
        return not self._filtered(node, index)

    def _filtered(self, node: fm.Node, index: int) -> bool:
        """Does the application's file filter leave it out?"""
        if not self.filter:
            return False
        t = self.t
        cpu, m = t.cpu, t.mem
        sp = cpu.sp
        pb = (sp - CINFO_PB - 64) & ~3          # the block and its name, on the stack
        cpu.sp = pb
        try:
            m.fill(pb, CINFO_PB + 64)
            m.w_pstr(pb + CINFO_PB, to_mac(node.name)[:63])
            m.w32(pb + ioNamePtr, pb + CINFO_PB)
            m.w16(pb + ioVRefNum, node.volume.vref)
            m.w16(pb + ioFDirIndex, index)
            with t.system.lock:
                put_cat_info(t, pb, node)
            args = (("p", pb),) if self.data is None else (("p", pb), ("p", self.data))
            return bool(t.call(self.filter, args, "B"))
        finally:
            cpu.sp = sp

    # =====================================================================
    # Running it
    # =====================================================================
    def run(self):
        """("open", file), ("save", directory, name, replacing) or, for a
        hook that picks a folder or disk, ("chosen", it); None if the user
        cancelled."""
        t = self.t
        display = display_of(t)
        if display is None:
            return None
        custom = self.custom = self._make_custom()
        try:
            if custom is not None:
                custom.set_name(self.name)
                self._describe(-1)
                got = custom.call(sfHookFirstCall)
                if got == sfHookChangeSelection:
                    self._go_to_reply()
                elif got == sfHookGoToDesktop and custom.new:
                    self.dir = None
                if self.put and custom.name() is not None:
                    self.name = custom.name()
            with t.tb.host_dialog():
                result = self._run(display.bridge)
            if custom is not None and custom.new:
                custom.call(sfHookLastCall)
        finally:
            if custom is not None:
                custom.dispose()
                self.custom = None
        self._remember()
        return result

    def _make_custom(self) -> Custom | None:
        hook, dlg_id, new = self.custom_args
        return Custom.make(self.t, self.put, hook, dlg_id, new, self.data or 0)

    def _extras(self) -> list[stdfile.Extra]:
        return self.custom.extras() if self.custom is not None else []

    def _run(self, bridge):
        t = self.t
        dlg, fut = bridge.call(stdfile.open_dialog, display_of(t), t.process, self.put,
                               self.prompt, self.name, self.place(), self._extras(), self.ok,
                               self.preview, self.custom is not None)
        try:
            while True:
                action = self._wait(bridge, fut)
                if self.custom is not None:
                    action = self._consult(bridge, dlg, action)
                result = self._step(action)
                if result is MORE:
                    (fut,) = bridge.call(dlg.show_place, self.place(), self._extras())
                elif result is SAME:
                    name, self.new_name = self.new_name, None
                    (fut,) = bridge.call(dlg.resume, self._extras(), name, self.message)
                    self.message = ""
                else:
                    break
        except BaseException:
            bridge.post(dlg.finish)
            raise
        bridge.post(dlg.finish)
        return result

    def _consult(self, bridge, dlg, action: tuple) -> tuple:
        """What the application's hook makes of the user's step: the step
        to take."""
        custom = self.custom
        n = custom.numbers
        kind = action[0]
        if kind == "pick":
            custom.pick(action[1], action[2])
            kind, action = "item", ("item", action[1])
        elif kind == "item" and len(action) > 2:
            custom.pressed(action[1], action[2])
            action = ("item", action[1])
        if kind == "item":
            item = action[1]
            typed = dm._item(self.t, custom.w, item)
            if typed is not None and typed.type == editText and not typed.enabled:
                item = sfHookNullEvent      # ModalDialog doesn't return it; the hook sees the text
        elif kind == "save":
            item = n.ok
        elif kind == "cancel":
            item = n.cancel
        elif kind == "open":
            item = n.ok if isinstance(self.entries[action[1]], File) else sfHookOpenFolder
        elif kind == "up":
            item = sfHookFolderPopUp
        elif kind == "desktop":
            item = sfHookGoToDesktop if custom.new else n.drive
        elif kind == "select":
            item = sfHookNullEvent
        else:
            item = n.new_folder
        if not item:
            return action
        row, name, edits = bridge.call(dlg.state)
        custom.set_name(name)
        custom.set_edits(edits)
        self._describe(row)
        got = custom.call(item)
        changed = custom.name()
        if changed is not None and changed != name:
            name = self.new_name = changed
        if got == item:
            return ("save", name, action[2]) if kind == "save" else action
        if got == n.ok:                     # the hook chose Open or Save itself
            if self.put:
                if name and self.dir is not None:
                    return ("save", name, isinstance(self.dir.child(name), File))
            elif 0 <= row < len(self.entries):
                node = self.entries[row]
                if isinstance(node, File):
                    return ("open", row)
                return ("chosen", node)     # a folder or disk, for a hook that picks one
            elif self.dir is not None:
                return ("chosen", self.dir)
        elif got == n.cancel:
            return ("cancel",)
        elif got == sfHookRebuildList:
            return ("rebuild",)
        elif got == sfHookChangeSelection and self.reply:
            return ("change",)
        elif got == sfHookGoToDesktop and custom.new:
            return ("desktop",)
        return ("item", got)

    def _wait(self, bridge, fut):
        return wait(self.t, bridge, fut)

    def _step(self, action: tuple):
        kind = action[0]
        if kind == "cancel":
            return None
        if kind in ("item", "select"):
            return SAME
        if kind == "chosen":
            return action
        if kind == "rebuild":               # the hook may have changed the types, or the folder
            self.types = _types(self.t, *self.type_list)
            self._follow()
        if kind == "change":
            return MORE if self._go_to_reply() else SAME
        if kind == "desktop":
            self.dir = None
        elif kind == "up":
            for _ in range(action[1]):
                self.dir = self.dir.parent if self.dir is not None else None
        elif kind == "open":
            node = self.entries[action[1]]
            if isinstance(node, fm.Volume):
                self.dir = node.root
            elif isinstance(node, Dir):
                self.dir = node
            else:
                return ("open", node)
        elif kind == "new_folder":
            d = self.dir
            try:
                self.dir = self.files.create(d.volume.vref, d.cnid, action[1], directory=True)
            except FileError as e:
                self.message = ("That name is already taken." if e.code == fm.dupFNErr else
                                f"The folder couldn’t be created (error {e.code}).")
        elif kind == "save":
            return ("save", self.dir, action[1], action[2])
        return MORE

    def _describe(self, row: int) -> None:
        """System 7's reply record, while its dialog with a hook is up:
        the file, folder or volume selected in the list, or the folder
        shown; nothing on the desktop with nothing selected."""
        if not self.reply or self.custom is None or not self.custom.new:
            return
        self.t.mem.fill(self.reply, STANDARD_REPLY)
        node = self.entries[row] if 0 <= row < len(self.entries) else self.dir
        if node is not None:
            self.described = True
            _describe(self.t.mem, self.reply, node)

    def _go_to_reply(self) -> bool:
        """Show the folder holding what the reply record describes, that
        selected; with no name, the folder its parent directory ID gives
        (Inside Macintosh: Files, "Setting the Current Directory")."""
        if not self.reply:
            return False
        m, reply = self.t.mem, self.reply
        try:
            vol, d, _ = self.files.resolve(m.r16s(reply + sfFile), m.r32(reply + sfFile + 2), "")
        except FileError:
            return False
        name = from_mac(m.r_pstr(reply + sfFile + 6))
        node = d.child(name) if name else None
        if d is vol.top:
            self.dir, self.select = None, vol if node is vol.root else None
        else:
            self.dir, self.select = d, node
        return True

    def _remember(self) -> None:
        """The folder shown, and so where the next dialog starts."""
        d = self.dir
        if d is not None:
            world = self.t.process.world
            world.w16(lowmem.SFSaveDisk, -d.volume.vref)
            world.w32(lowmem.CurDirStore, d.cnid)
            self.saved = (-d.volume.vref & 0xFFFF, d.cnid)

    def _follow(self) -> None:
        """Show the folder SFSaveDisk and CurDirStore name, if the hook has
        changed them."""
        world = self.t.process.world
        if (world.r16(lowmem.SFSaveDisk), world.r32(lowmem.CurDirStore)) != self.saved:
            self.dir = self._current() or self.dir


def display_of(t):
    return getattr(t.tb, "_d", None)


def wait(t, bridge, fut):
    """The user's next step in a dialog of macrt's own; other applications
    run meanwhile."""
    with t.yielding():
        while True:
            try:
                return fut.result(timeout=0.1)
            except TimeoutError:
                if t.process.events.killed or bridge.closed:
                    raise ProcessKilled() from None


def _types(t, count: int, p: int) -> set[bytes] | None:
    if count < 0 or not p:
        return None
    return {t.mem.read(p + 4 * i, 4) for i in range(min(count, MAX_TYPES))}


def _str(t, p: int) -> str:
    return t.str255(p) if p else ""


# =========================================================================
# The original calls: an SFReply, with a working directory for the folder
# =========================================================================
def _sf_reply(t, reply: int, result) -> None:
    m = t.mem
    if result is None:
        m.w8(reply + rGood, 0)
        return
    if result[0] == "save":
        _, d, name, _ = result
        ftype = bytes(4)
    else:                                   # a file opened, or a folder or disk a hook chose
        node = result[1]
        if isinstance(node, fm.Volume):
            node = node.root
        name = node.name
        if isinstance(node, File):
            d, ftype = node.parent, node.type
        else:
            d, ftype = (node if node is node.volume.root else node.parent), bytes(4)
    files = t.process.files
    m.fill(reply, SF_REPLY)
    m.w8(reply + rGood, 1)
    m.write(reply + rFType, ftype)
    m.w16(reply + rVRefNum, files.fs.open_wd(d.volume, d.cnid, fm.ERIK, files))
    m.w_pstr(reply + rFName, to_mac(name)[:63])


def _sf_get(t, reply, file_filter, num_types, type_list, hook=0, dlg_id=DIALOG_IDS[False]):
    dialog = Dialog(t, False, file_filter=file_filter, type_list=(num_types, type_list),
                    hook=hook, dlg_id=dlg_id)
    _sf_reply(t, reply & ADDRESS_MASK, dialog.run())


def _sf_put(t, reply, prompt, orig_name, hook=0, dlg_id=DIALOG_IDS[True]):
    dialog = Dialog(t, True, _str(t, prompt), _str(t, orig_name), hook=hook, dlg_id=dlg_id)
    _sf_reply(t, reply & ADDRESS_MASK, dialog.run())


@trap
def SFGetFile(t, where, prompt, file_filter, num_types, type_list, dlg_hook, reply):
    _sf_get(t, reply, file_filter, num_types, type_list, dlg_hook)


@trap
def SFPGetFile(t, where, prompt, file_filter, num_types, type_list, dlg_hook, reply, dlg_id,
               filter_proc):
    _sf_get(t, reply, file_filter, num_types, type_list, dlg_hook, dlg_id)


@trap
def SFPutFile(t, where, prompt, orig_name, dlg_hook, reply):
    _sf_put(t, reply, prompt, orig_name, dlg_hook)


@trap
def SFPPutFile(t, where, prompt, orig_name, dlg_hook, reply, dlg_id, filter_proc):
    _sf_put(t, reply, prompt, orig_name, dlg_hook, dlg_id)


# =========================================================================
# System 7's: a StandardFileReply, with an FSSpec
# =========================================================================
def _describe(m, reply: int, node: fm.Node | fm.Volume) -> None:
    """A StandardFileReply's file, folder or volume, as sfFile and what it
    says of it."""
    if isinstance(node, fm.Volume):
        node = node.root
    m.w16(reply + sfFile, node.volume.vref)
    m.w32(reply + sfFile + 2, node.parid)
    m.w_pstr(reply + sfFile + 6, to_mac(node.name)[:63])
    if isinstance(node, File):
        m.write(reply + sfType, node.type)
        m.w16(reply + sfFlags, node.flags)
    else:
        m.w8(reply + (sfIsVolume if node is node.volume.root else sfIsFolder), 1)


def _standard_reply(t, reply: int, result, described: bool = False) -> None:
    """The reply to an open or save, or to a folder or disk a hook chose;
    cancelled (result None), it's left saying what the dialog last had
    selected, if it said so."""
    m = t.mem
    reply &= ADDRESS_MASK
    if result is None and described:
        m.w8(reply + sfGood, 0)
        return
    m.fill(reply, STANDARD_REPLY)
    if result is None:
        return
    m.w8(reply + sfGood, 1)
    if result[0] in ("open", "chosen"):
        _describe(m, reply, result[1])
        return
    _, d, name, replacing = result
    existing = d.child(name)
    replacing = replacing or isinstance(existing, File)
    if existing is not None:
        name = existing.name
    m.w8(reply + sfReplacing, int(replacing))
    m.w16(reply + sfFile, d.volume.vref)
    m.w32(reply + sfFile + 2, d.cnid)
    m.w_pstr(reply + sfFile + 6, to_mac(name)[:63])


@trap
def StandardGetFile(t, file_filter, num_types, type_list, reply):
    dialog = Dialog(t, False, file_filter=file_filter, type_list=(num_types, type_list),
                    folders=True)
    _standard_reply(t, reply, dialog.run())


@trap
def StandardPutFile(t, prompt, default_name, reply):
    _standard_reply(t, reply, Dialog(t, True, _str(t, prompt), _str(t, default_name)).run())


@trap
def CustomGetFile(t, file_filter, num_types, type_list, reply, dlg_id, where, dlg_hook,
                  filter_proc, active_list, activate_proc, your_data):
    dialog = Dialog(t, False, file_filter=file_filter, type_list=(num_types, type_list),
                    data=your_data, folders=True, hook=dlg_hook, dlg_id=dlg_id, new=True,
                    reply=reply)
    _standard_reply(t, reply, dialog.run(), dialog.described)


@trap
def CustomPutFile(t, prompt, default_name, reply, dlg_id, where, dlg_hook, filter_proc,
                  active_list, activate_proc, your_data):
    dialog = Dialog(t, True, _str(t, prompt), _str(t, default_name), data=your_data,
                    hook=dlg_hook, dlg_id=dlg_id, new=True, reply=reply)
    _standard_reply(t, reply, dialog.run(), dialog.described)
