"""
The Edition Manager (Pack11; Inside Macintosh: Interapplication
Communication, chapter 2): publishers and subscribers, the editions they
share, and the dialogs that make them.

A section is registered system-wide.  The sections of one edition
container share a control block, a handle in the system heap that a
program may only compare.  Section events (Section Read, Section Write
and Section Scroll) are Apple events, sent only to an application whose
SIZE says it takes high-level events, with the section's handle as the
direct object.

Editions are opened, read, written and closed through the application's
edition opener, as on a Mac: the Edition Manager never opens a container
itself.  The standard opener and its format I/O routine are macrt's own,
in Python.  GetEditionOpenerProc gives them as odd values that aren't
addresses, which CallEditionOpenerProc, CallFormatIOProc and an opener
parameter block's ioProc understand.  An application's own opener and
I/O routine are called as Pascal functions:
FUNCTION(selector; VAR PB): OSErr.

The data fork of an edition container is in macrt's own format, since
Inside Macintosh doesn't document Apple's: 'EDTN', a version and a count,
an entry for each format (its type, offset and length), then the
formats' data.  macrt finds no formats in a container a real Mac wrote.

The publisher and subscriber dialogs are Standard File's, with Publish
or Subscribe for the default button.  The publisher's shows the preview;
the subscriber's shows none.  An expansion DITL goes below Standard
File's own items, and the itemOffset an expanded dialog's hook is given
counts those items: 12 for a publisher, 9 for a subscriber.  The options
dialog is macrt's own.  The itemOffset its hook is given counts the
dialog's OK and Cancel (2); the hook sees the expansion's items, but the
dialog doesn't show them.  None of the dialogs uses where or a filter
procedure.

GoToPublisherSection scrolls to a publisher that's registered.  It opens
an unregistered publisher's document only if the document's application
is already running, since macrt doesn't launch applications itself.
"""

from __future__ import annotations

import datetime
import itertools
import struct
from contextlib import contextmanager
from dataclasses import dataclass, field
from weakref import WeakKeyDictionary

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QImage, QPainter

from ..memory import from_mac
from ..toolbox import aliases as al
from ..toolbox import files as fm
from ..toolbox import pict
from ..toolbox.files import (Dir, File, FileError, fBsyErr, fLckdErr, fnfErr, noErr, paramErr,
                             permErr, rfNumErr, wrPermErr)
from ..toolbox.memmgr import memFullErr
from ..toolbox.types import Rect
from ..ui import editiondlg, theme
from . import glue_aevents as ae
from . import glue_stdfile as sf
from .dispatch import trap
from .glue_aliases import _record, _replace
from .glue_files import _files, _fs_call, _spec, put_spec
from .glue_os import _zone
from .glue_print import _draw_on

abortErr = -27
eofErr = -39
noTypeErr = -102
badSectionErr = -451
notRegisteredSectionErr = -452
badSubPartErr = -454
multiplePublisherWrn = -460
containerNotFoundWrn = -461
notThePublisherWrn = -463

stSubscriber, stPublisher = 0x01, 0x0A
sumAutomatic, sumManual = 0, 1          # a publisher's pumOnSave and pumManual are the same
kPartsNotUsed = 0
kPreviewWidth = kPreviewHeight = 120
kFormatLengthUnknown = -1
kPICTformatMask, kTEXTformatMask, ksndFormatMask = 1, 2, 4

eoOpen, eoClose, eoOpenNew, eoCloseNew, eoCanSubscribe = range(5)
ioHasFormat, ioReadFormat, ioNewFormat, ioWriteFormat = range(4)

sectionEventMsgClass = b"sect"
sectionReadMsgID, sectionWriteMsgID, sectionScrollMsgID = b"read", b"writ", b"scrl"
typeSectionH = b"sect"
emOptionsDialogRefCon = 0x6F70746E      # 'optn'
emHookCancelSection, emHookGoToPublisher, emHookEditionNow = 160, 161, 162
emHookManualUpdateMode, emHookAutoUpdateMode = 163, 164

STANDARD_OPENER = 1                     # odd: not routines, macrt's own
STANDARD_IO = 3

FMTS, ALIS, PRVW = b"fmts", b"alis", b"prvw"
PICT, TEXT, SND = b"PICT", b"TEXT", b"snd "
EDITION_TYPES = {PICT: b"edtp", TEXT: b"edtt", SND: b"edts"}
UNKNOWN_TYPE = b"edtu"
CONTAINER_TYPES = {*EDITION_TYPES.values(), UNKNOWN_TYPE}
MASKS = {PICT: kPICTformatMask, TEXT: kTEXTformatMask, SND: ksndFormatMask}
TYPE_MASKS = {EDITION_TYPES[f]: m for f, m in MASKS.items()}

# A SectionRecord
SECTION_RECORD = 36
srVersion, srKind, srMode, srMdDate, srSectionID, srRefCon = 0, 1, 2, 4, 8, 12
srAlias, srSubPart, srNextSection, srControlBlock, srRefNum = 16, 20, 24, 28, 32
# An EditionContainerSpec: an FSSpec, its script, the part and its name
CONTAINER_SPEC = 110
ecTheFileScript, ecThePart, ecThePartName, ecThePartScript = 70, 72, 76, 108
# An EditionInfoRecord
EDITION_INFO = 126
eiCrDate, eiMdDate, eiFdCreator, eiFdType, eiContainer = 0, 4, 8, 12, 16
# NewPublisherReply, NewSubscriberReply, SectionOptionsReply
npCanceled, npReplacing, npUsePart, npPreview, npPreviewFormat, npContainer = 0, 1, 2, 4, 8, 12
nsCanceled, nsFormatsMask, nsContainer = 0, 1, 2
soCanceled, soChanged, soSectionH, soAction = 0, 1, 2, 6
# An EditionOpenerParamBlock
OPENER_PB = 148
eoInfo, eoSectionH, eoDocument, eoFdCreator = 0, 126, 130, 134
eoIoRefNum, eoIoProc, eoSuccess, eoFormatsMask = 138, 142, 146, 147
# A FormatIOParamBlock
FORMAT_PB = 24
ioRefNum, ioFormat, ioFormatIndex, ioOffset, ioBuffPtr, ioBuffLen = 0, 4, 8, 12, 16, 20

CHUNK = 4096                # what GetStandardFormats reads at a time

# The options dialog's own items, for an expanded one's hook: OK, Cancel
OPTIONS = sf.Numbers(1, 2, 0, 0, 0, 0, 2)
OPTION_ITEMS = {"ok": 1, "cancel": 2, "cancel section": emHookCancelSection,
                "goto": emHookGoToPublisher, "now": emHookEditionNow}


# =========================================================================
# An edition container's data fork
# =========================================================================
MAGIC = b"EDTN"
HEADER = struct.Struct(">4sHH")         # magic, version, count
ENTRY = struct.Struct(">4sII")          # format, offset, length


def parse(data: bytes) -> list[list] | None:
    """The formats in a container's data fork, [type, data] in the order
    they were written; [] for an empty fork, None for one in a format
    that isn't macrt's."""
    if not data:
        return []
    if len(data) < HEADER.size:
        return None
    magic, version, count = HEADER.unpack_from(data)
    if magic != MAGIC or version != 1:
        return None
    out = []
    for i in range(count):
        at = HEADER.size + i * ENTRY.size
        if at + ENTRY.size > len(data):
            return None
        ftype, offset, length = ENTRY.unpack_from(data, at)
        if offset + length > len(data):
            return None
        out.append([ftype, bytearray(data[offset:offset + length])])
    return out


def pack(formats: list) -> bytes:
    at = HEADER.size + ENTRY.size * len(formats)
    head, body = [HEADER.pack(MAGIC, 1, len(formats))], []
    for ftype, data in formats:
        head.append(ENTRY.pack(ftype, at, len(data)))
        body.append(bytes(data))
        at += len(data)
    return b"".join(head + body)


# =========================================================================
# What the Edition Manager keeps
# =========================================================================
@dataclass(eq=False)
class Control:
    """What the sections of one edition container share."""
    node: File
    handle: int                         # the control block programs see
    sections: list = field(default_factory=list)
    writer: Open | None = None          # the edition being written


@dataclass(eq=False)
class Section:
    handle: int
    process: object
    control: Control | None
    busy: int = 0                       # editions open for it


@dataclass(eq=False)
class Open:
    """An open edition, whose EditionRefNum is a handle of its own."""
    refnum: int
    section: Section
    process: object
    publisher: bool
    opener: int
    io_refnum: int
    io_proc: int
    info: bytes                         # the EditionInfoRecord it was opened with
    marks: dict = field(default_factory=dict)       # format: mark
    indexes: dict = field(default_factory=dict)     # format: formatIndex


@dataclass(eq=False)
class Std:
    """An edition the standard opener has open."""
    node: File
    writing: bool
    formats: list                       # [type, bytearray]
    generation: int = 0                 # editions written before it was opened
    creator: bytes = b"????"


@dataclass(eq=False)
class Editions:
    sections: dict = field(default_factory=dict)    # section handle: Section
    controls: dict = field(default_factory=dict)    # File: Control
    opens: dict = field(default_factory=dict)       # EditionRefNum: Open
    last: tuple | None = None           # the last container used: vref, parid, name
    scrolls: list = field(default_factory=list)     # (File, section ID) to scroll to
    std: dict = field(default_factory=dict)         # the standard opener's ioRefNum: Std
    tokens: itertools.count = field(default_factory=lambda: itertools.count(1))
    commits: dict = field(default_factory=dict)     # File: editions written to it
    openers: WeakKeyDictionary = field(default_factory=WeakKeyDictionary)   # process: opener


_managers: WeakKeyDictionary = WeakKeyDictionary()     # System: Editions


def _alive(t, p) -> bool:
    if p is t.process:
        return True
    if p.events.killed:
        return False
    d = sf.display_of(t)
    return d is None or p in d.launch_order


def _state(t) -> Editions:
    """The system's, without the sections and editions of applications
    that have quit."""
    st = _managers.get(t.system)
    if st is None:
        st = _managers[t.system] = Editions()
    for o in [o for o in st.opens.values() if not _alive(t, o.process)]:
        _forget_open(t, st, o)
    for s in [s for s in st.sections.values() if not _alive(t, s.process)]:
        _unregister(t, st, s)
    return st


def _forget_open(t, st: Editions, o: Open) -> None:
    st.opens.pop(o.refnum, None)
    if o.opener == STANDARD_OPENER:
        st.std.pop(o.io_refnum, None)
    control = o.section.control
    if control is not None and control.writer is o:
        control.writer = None
    o.section.busy -= 1
    _dispose(t, o.refnum)


def _unregister(t, st: Editions, s: Section) -> None:
    st.sections.pop(s.handle, None)
    control = s.control
    if control is None:
        return
    if s in control.sections:
        control.sections.remove(s)
    if not control.sections and control.writer is None:
        st.controls.pop(control.node, None)
        _dispose(t, control.handle)


def _dispose(t, h: int) -> None:
    t.system.sys_heap.dispose_handle(h)     # refnums and control blocks are the system's


def _opener(t, st: Editions) -> int:
    return st.openers.get(t.process, STANDARD_OPENER)


def _exists(node: fm.Node | None) -> bool:
    return node is not None and node.parent is not None and node.parent.child(node.name) is node


def _publishers(t, control: Control) -> list[Section]:
    out = []
    for s in control.sections:
        p = _record_of(t, s.handle)
        if p is not None and _is_publisher(t, p):
            out.append(s)
    return out


# =========================================================================
# Records
# =========================================================================
def _record_of(t, h: int) -> int | None:
    """The address of the section record h holds; None if it isn't one."""
    h &= t.mem.system.addr_mask
    if not h:
        return None
    try:
        heap = t.system.heap_of(h)
        if heap.handle_size(h) < SECTION_RECORD:
            return None
        p = heap.deref(h)
    except ValueError:
        return None
    if t.mem.r8(p + srVersion) != 1 or t.mem.r8(p + srKind) not in (stSubscriber, stPublisher):
        return None
    return p


def _is_publisher(t, p: int) -> bool:
    return t.mem.r8(p + srKind) == stPublisher


def _put_container(t, p: int, vref: int, parid: int, name: str) -> None:
    m = t.mem
    m.fill(p, CONTAINER_SPEC)
    put_spec(t, p, vref, parid, name)


def _put_node(t, p: int, node: fm.Node) -> None:
    _put_container(t, p, node.volume.vref, node.parid, node.name)


def _put_info(t, p: int, node: File) -> None:
    m = t.mem
    m.fill(p, EDITION_INFO)
    m.w32(p + eiCrDate, node.crdate)
    m.w32(p + eiMdDate, node.mddate)
    m.write(p + eiFdCreator, node.creator)
    m.write(p + eiFdType, node.type)
    _put_node(t, p + eiContainer, node)


def _lookup(t, vref: int, parid: int, name: str) -> fm.Node | None:
    try:
        return _files(t).lookup(vref, parid, name)
    except FileError:
        return None


def _container_file(t, spec: int) -> File | None:
    node = _lookup(t, *_spec(t, spec))
    return node if isinstance(node, File) else None


def _document(t, spec: int) -> fm.Node | None:
    """The node an FSSpecPtr gives, if it's there."""
    spec &= t.mem.system.addr_mask
    return _lookup(t, *_spec(t, spec)) if spec else None


def _alias_for(t, spec: int, document: fm.Node | None) -> al.Alias:
    """An alias to the container spec gives, which needn't exist yet."""
    vref, parid, name = _spec(t, spec)
    node = _lookup(t, vref, parid, name)
    if node is not None:
        return al.make(node, document)
    try:
        d = _files(t).lookup(vref, parid, None)
    except FileError:
        return al.Alias(name=name)
    return al.from_path(t.system.files, al.path_of(d).rstrip(":") + ":" + name)


def _target(t, p: int, document: fm.Node | None = None) -> tuple[al.Alias | None, al.Found | None]:
    """A section's alias and where it leads, None for either it hasn't."""
    alias_h = t.mem.r32(p + srAlias) & t.mem.system.addr_mask
    try:
        alias = _record(t, alias_h)
    except FileError:
        return None, None
    try:
        return alias, al.resolve(t.system.files, alias, document)
    except FileError:
        return alias, None


def _edition_alias(data: bytes) -> tuple[al.Alias, int] | None:
    """An 'alis' format's alias to the publisher's document and the
    publisher's section ID; None if it isn't one, which a publisher that
    writes the format itself may make."""
    if len(data) <= 4:
        return None
    try:
        a = al.Alias.parse(data[:-4])
    except FileError:
        return None
    return a, struct.unpack(">l", data[-4:])[0]


# =========================================================================
# Section events
# =========================================================================
def _send(t, process, eid: bytes, section_h: int) -> None:
    """A section event for one of process's sections."""
    if not ae.aware(process):
        return
    psn = struct.pack(">II", 0, process.serial_number)
    c = ae._new_event(t, sectionEventMsgClass, eid, (ae.typeProcessSerialNumber, psn))
    c.put(ae.keyEventSourceAttr, ae.typeShortInteger, struct.pack(">h", ae.kAELocalProcess),
          attr=True)
    c.put(ae.keyDirectObject, typeSectionH, struct.pack(">I", section_h))
    ae.post(t, c, process)


def _open_documents(t, process, node: fm.Node) -> None:
    if not ae.aware(process):
        return
    psn = struct.pack(">II", 0, process.serial_number)
    c = ae._new_event(t, ae.kCoreEventClass, ae.kAEOpenDocuments,
                      (ae.typeProcessSerialNumber, psn))
    c.put(ae.keyEventSourceAttr, ae.typeShortInteger, struct.pack(">h", ae.kAELocalProcess),
          attr=True)
    spec = [[ae.NO_KEY, ae.typeFSS, ae.fss(node.volume.vref, node.parid, node.name)]]
    c.put(ae.keyDirectObject, ae.typeAEList, ae.Composite(ae.typeAEList, spec).flatten())
    ae.post(t, c, process)


def _read_subscribers(t, control: Control, mddate: int) -> None:
    """Section Read to each automatic subscriber an edition is new to."""
    for s in control.sections:
        p = _record_of(t, s.handle)
        if (p is not None and not _is_publisher(t, p) and t.mem.r16(p + srMode) == sumAutomatic
                and t.mem.r32(p + srMdDate) != mddate):
            _send(t, s.process, sectionReadMsgID, s.handle)


# =========================================================================
# Openers and format I/O
# =========================================================================
@contextmanager
def _on_stack(t, size: int):
    """A zeroed block on the 68k stack, for a parameter block."""
    cpu = t.cpu
    sp = cpu.sp
    p = (sp - size) & ~3
    cpu.sp = p
    t.mem.fill(p, size)
    try:
        yield p
    finally:
        cpu.sp = sp


def _call_opener(t, opener: int, verb: int, pb: int) -> int:
    if opener == STANDARD_OPENER:
        return _standard_opener(t, verb, pb)
    opener &= t.mem.system.addr_mask
    if not opener or opener & 1:
        return paramErr
    return t.call(opener, (("B", verb), ("p", pb)), "e")


def _call_io(t, proc: int, verb: int, pb: int) -> int:
    if proc == STANDARD_IO:
        return _standard_io(t, verb, pb)
    proc &= t.mem.system.addr_mask
    if not proc or proc & 1:
        return paramErr
    return t.call(proc, (("B", verb), ("p", pb)), "e")


def _formats_mask(node: File) -> int:
    """The formats in an edition that a subscriber's formatsMask names."""
    formats = parse(bytes(node.fork(False)))
    if formats is None:
        return TYPE_MASKS.get(node.type, 0)
    mask = 0
    for ftype, _ in formats:
        mask |= MASKS.get(ftype, 0)
    return mask


def _standard_opener(t, verb: int, pb: int) -> int:
    st = _state(t)
    m = t.mem
    if verb in (eoClose, eoCloseNew):
        std = st.std.pop(m.r32(pb + eoIoRefNum), None)
        if std is None:
            return rfNumErr
        if verb == eoCloseNew and m.r8(pb + eoSuccess) and std.writing:
            return _fs_call(t, lambda: _commit(t, std))
        return noErr
    if verb not in (eoOpen, eoOpenNew, eoCanSubscribe):
        return paramErr
    node = _container_file(t, pb + eoInfo + eiContainer)
    if node is None:
        return fnfErr
    if verb == eoCanSubscribe:
        if node.type not in CONTAINER_TYPES:
            return noTypeErr
        return noErr if _formats_mask(node) & m.r8(pb + eoFormatsMask) else noTypeErr
    if verb == eoOpen:
        std = Std(node, False, parse(bytes(node.fork(False))) or [], st.commits.get(node, 0))
    else:
        if node.locked or node.volume.lock_error:
            return fLckdErr
        std = Std(node, True, [], creator=m.read(pb + eoFdCreator, 4))
    token = next(st.tokens)
    st.std[token] = std
    m.w32(pb + eoIoRefNum, token)
    m.w32(pb + eoIoProc, STANDARD_IO)
    return noErr


def _commit(t, std: Std) -> None:
    """The new edition, in the container."""
    node = std.node
    t.system.files.set_fork(node, False, pack(std.formats))
    kinds = [EDITION_TYPES[f] for f, _ in std.formats if f in EDITION_TYPES]
    node.type = kinds[0] if kinds else UNKNOWN_TYPE
    node.creator = std.creator
    _files(t).metadata_changed(node)


def _find(std: Std, fmt: bytes) -> tuple[int, bytes | bytearray | None]:
    if fmt == FMTS:
        return 0, b"".join(struct.pack(">4sl", f, len(d)) for f, d in std.formats)
    for i, (f, data) in enumerate(std.formats, start=1):
        if f == fmt:
            return i, data
    return 0, None


def _standard_io(t, verb: int, pb: int) -> int:
    st = _state(t)
    m = t.mem
    std = st.std.get(m.r32(pb + ioRefNum))
    if std is None:
        return rfNumErr
    fmt = m.read(pb + ioFormat, 4)
    if verb == ioHasFormat:
        index, data = _find(std, fmt)
        if data is None:
            return noTypeErr
        m.w32(pb + ioFormatIndex, index)
        m.w32(pb + ioBuffLen, len(data))
        return noErr
    if verb == ioReadFormat:
        if not std.writing and st.commits.get(std.node, 0) != std.generation:
            return abortErr                 # a publisher has written a new edition since
        _, data = _find(std, fmt)
        if data is None:
            return noTypeErr
        offset, n = m.r32(pb + ioOffset), max(m.r32s(pb + ioBuffLen), 0)
        if n and offset >= len(data):
            m.w32(pb + ioBuffLen, 0)
            return eofErr
        got = bytes(data[offset:offset + n])
        m.write(m.r32(pb + ioBuffPtr) & t.mem.system.addr_mask, got)
        m.w32(pb + ioBuffLen, len(got))
        return noErr
    if verb not in (ioNewFormat, ioWriteFormat):
        return paramErr
    if not std.writing:
        return wrPermErr
    if fmt == FMTS:
        return paramErr                     # it's made, not written
    index, data = _find(std, fmt)
    if verb == ioNewFormat:
        if data is None:
            std.formats.append([fmt, bytearray()])
            index = len(std.formats)
        m.w32(pb + ioFormatIndex, index)
        return noErr
    if data is None:
        return noTypeErr
    offset, n = m.r32(pb + ioOffset), max(m.r32s(pb + ioBuffLen), 0)
    if offset + n > max(len(data), fm.ROOM):
        return fm.dskFulErr                 # more than the edition's volume can hold
    if len(data) < offset:
        data.extend(bytes(offset - len(data)))
    data[offset:offset + n] = m.read(m.r32(pb + ioBuffPtr) & t.mem.system.addr_mask, n)
    return noErr


def _io(t, o: Open, verb: int, fmt: bytes, index: int = 0, offset: int = 0, buff: int = 0,
        length: int = 0) -> tuple[int, int, int]:
    """A call of an open edition's I/O routine: its result, the
    formatIndex and the buffLen it leaves."""
    m = t.mem
    with _on_stack(t, FORMAT_PB) as pb:
        m.w32(pb + ioRefNum, o.io_refnum)
        m.write(pb + ioFormat, fmt)
        m.w32(pb + ioFormatIndex, index)
        m.w32(pb + ioOffset, offset)
        m.w32(pb + ioBuffPtr, buff)
        m.w32(pb + ioBuffLen, length)
        err = _call_io(t, o.io_proc, verb, pb)
        return err, m.r32(pb + ioFormatIndex), m.r32s(pb + ioBuffLen)


def _index(t, o: Open, fmt: bytes, new: bool = False) -> int | tuple[int, int]:
    """A format's formatIndex in an open edition, asking its I/O routine
    if need be: the error if it isn't there and can't be made."""
    if fmt in o.indexes:
        return o.indexes[fmt]
    err, index, _ = _io(t, o, ioNewFormat if new else ioHasFormat, fmt)
    if err:
        return (err,)
    o.indexes[fmt] = index
    o.marks.setdefault(fmt, 0)
    return index


def _write_all(t, o: Open, fmt: bytes, data: bytes) -> int:
    index = _index(t, o, fmt, new=True)
    if isinstance(index, tuple):
        return index[0]
    heap = t.process.heap
    buff = heap.new_ptr(max(len(data), 1))
    if not buff:
        return memFullErr
    try:
        t.mem.write(buff, data)
        err, _, _ = _io(t, o, ioWriteFormat, fmt, index, o.marks[fmt], buff, len(data))
    finally:
        heap.dispose_ptr(buff)
    if not err:
        o.marks[fmt] += len(data)
    return err


def _read_all(t, o: Open, fmt: bytes) -> bytes | int:
    """A format's data, read a chunk at a time; the error if it can't be."""
    err, index, size = _io(t, o, ioHasFormat, fmt)
    if err:
        return err
    heap = t.process.heap
    buff = heap.new_ptr(CHUNK)
    if not buff:
        return memFullErr
    out = bytearray()
    try:
        while size < 0 or len(out) < size:
            n = CHUNK if size < 0 else min(CHUNK, size - len(out))
            err, _, got = _io(t, o, ioReadFormat, fmt, index, len(out), buff, n)
            got = max(0, min(got, n))
            out += t.mem.read(buff, got)
            if err == eofErr or (size < 0 and got < n):
                break
            if err:
                return err
            if not got:
                break
    finally:
        heap.dispose_ptr(buff)
    return bytes(out)


def _open(t, st: Editions, section: Section, node: File, verb: int, document: int = 0,
          creator: bytes = b"????") -> Open | int:
    """An edition opened through the current opener."""
    m = t.mem
    opener = _opener(t, st)
    with _on_stack(t, OPENER_PB) as pb:
        _put_info(t, pb + eoInfo, node)
        m.w32(pb + eoSectionH, section.handle)
        m.w32(pb + eoDocument, document)
        m.write(pb + eoFdCreator, creator)
        err = _call_opener(t, opener, verb, pb)
        if err:
            return err
        info = m.read(pb + eoInfo, EDITION_INFO)
        io_refnum, io_proc = m.r32(pb + eoIoRefNum), m.r32(pb + eoIoProc)
    refnum = t.system.sys_heap.new_handle(4, clear=True)
    o = Open(refnum, section, t.process, verb == eoOpenNew, opener, io_refnum, io_proc, info)
    if not refnum:
        _close(t, o, False)
        return memFullErr
    st.opens[refnum] = o
    section.busy += 1
    return o


def _close(t, o: Open, success: bool) -> int:
    m = t.mem
    with _on_stack(t, OPENER_PB) as pb:
        m.write(pb + eoInfo, o.info)
        m.w32(pb + eoSectionH, o.section.handle)
        m.w32(pb + eoIoRefNum, o.io_refnum)
        m.w32(pb + eoIoProc, o.io_proc)
        m.w8(pb + eoSuccess, int(success))
        return _call_opener(t, o.opener, eoCloseNew if o.publisher else eoClose, pb)


# =========================================================================
# Registering sections
# =========================================================================
def _register(t, st: Editions, h: int, document: fm.Node | None) -> tuple[int, bool]:
    """Register a section: its result, and whether its alias changed."""
    m = t.mem
    p = _record_of(t, h)
    if p is None:
        return badSectionErr, False
    if h in st.sections:
        return noErr, False
    publisher = _is_publisher(t, p)
    alias, found = _target(t, p, document)
    if alias is None:
        return paramErr, False
    updated = False
    node = found.node if found is not None and isinstance(found.node, File) else None
    if node is not None:
        fresh = al.updated(alias, node, document)
        if fresh is not None and not _replace(t, m.r32(p + srAlias) & t.mem.system.addr_mask, fresh):
            updated = True
    err = noErr
    wrote = False
    if node is None and publisher and found is not None:
        # an empty container where the last one was, for the publisher to write
        def create():
            new = _files(t).create(found.volume.vref, found.parid, found.name)
            new.type = UNKNOWN_TYPE
            new.creator = t.process.app.creator if t.process.app is not None else b"????"
            _files(t).metadata_changed(new)
            return new
        try:
            with t.system.lock:
                node = create()
            wrote = True
        except FileError:
            node = None
    elif node is None and not publisher:
        err = containerNotFoundWrn
    control = None
    if node is not None:
        control = st.controls.get(node)
        if control is None:
            ch = t.system.sys_heap.new_handle(4, clear=True)
            if not ch:
                return memFullErr, updated
            control = st.controls[node] = Control(node, ch)
    section = Section(h, t.process, control)
    st.sections[h] = section
    m.w32(p + srControlBlock, control.handle if control is not None else 0)
    if control is None:
        return err, updated
    control.sections.append(section)
    if publisher:
        if [s for s in _publishers(t, control) if s is not section]:
            err = multiplePublisherWrn
        elif not _last_publisher(t, node, m.r32s(p + srSectionID), document):
            err = notThePublisherWrn
        if wrote:
            _send(t, t.process, sectionWriteMsgID, h)
        sid = m.r32s(p + srSectionID)
        for pending in [s for s in st.scrolls if s[0] is node and s[1] in (None, sid)]:
            st.scrolls.remove(pending)
            _send(t, t.process, sectionScrollMsgID, h)
    elif m.r16(p + srMode) == sumAutomatic and m.r32(p + srMdDate) != node.mddate:
        _send(t, t.process, sectionReadMsgID, h)
    return err, updated


def _last_publisher(t, node: File, section_id: int, document: fm.Node | None) -> bool:
    """Could the publisher that wrote an edition (that its 'alis' format
    names, in a container of macrt's) be this one?"""
    formats = parse(bytes(node.fork(False)))
    data = next((d for f, d in formats or [] if f == ALIS), None)
    if data is None:
        return True
    got = _edition_alias(bytes(data))
    if got is None:
        return True
    alias, sid = got
    if sid != section_id:
        return False
    if document is None:
        return True
    try:
        return al.resolve(t.system.files, alias, node).node is document
    except FileError:
        return False


# =========================================================================
# The calls
# =========================================================================
@trap
def InitEditionPackVersion(t, version):
    _state(t)
    return noErr


@trap
def NewSection(t, container, section_document, kind, section_id, initial_mode, section_h):
    m = t.mem
    st = _state(t)
    m.w32(section_h, 0)
    kind &= 0xFF
    if kind not in (stSubscriber, stPublisher):
        return badSectionErr
    if m.r32(container + ecThePart) != kPartsNotUsed:
        return badSubPartErr
    document = _document(t, section_document)
    alias = _alias_for(t, container, document).pack()
    zone = _zone(t, False)
    h = zone.new_handle(SECTION_RECORD, clear=True)
    alias_h = zone.new_handle(len(alias)) if h else 0
    if not alias_h:
        if h:
            zone.dispose_handle(h)
        return memFullErr
    m.write(zone.deref(alias_h), alias)
    p = zone.deref(h)
    m.w8(p + srVersion, 1)
    m.w8(p + srKind, kind)
    m.w16(p + srMode, initial_mode)
    m.w32(p + srMdDate, fm.now() if kind == stPublisher else 0)
    m.w32(p + srSectionID, section_id)
    m.w32(p + srAlias, alias_h)
    err, _ = _register(t, st, h, document)
    if err in (badSectionErr, paramErr, memFullErr):
        zone.dispose_handle(alias_h)
        zone.dispose_handle(h)
        return err
    st.last = _spec(t, container)
    m.w32(section_h, h)
    return err


@trap
def RegisterSection(t, section_document, section_h, alias_was_updated):
    st = _state(t)
    t.mem.w8(alias_was_updated, 0)
    err, updated = _register(t, st, section_h & t.mem.system.addr_mask, _document(t, section_document))
    t.mem.w8(alias_was_updated, int(updated))
    return err


@trap
def UnRegisterSection(t, section_h):
    st = _state(t)
    s = st.sections.get(section_h & t.mem.system.addr_mask)
    if s is None:
        return notRegisteredSectionErr
    if s.busy:
        return fBsyErr
    _unregister(t, st, s)
    p = _record_of(t, s.handle)
    if p is not None:
        t.mem.w32(p + srControlBlock, 0)
    return noErr


@trap
def IsRegisteredSection(t, section_h):
    return noErr if section_h & t.mem.system.addr_mask in _state(t).sections else notRegisteredSectionErr


@trap
def AssociateSection(t, section_h, new_section_document):
    """The section's alias, made relative to the document it's in now."""
    p = _record_of(t, section_h)
    if p is None:
        return paramErr
    st = _state(t)
    document = _document(t, new_section_document)
    s = st.sections.get(section_h & t.mem.system.addr_mask)
    node = s.control.node if s is not None and s.control is not None else None
    if not _exists(node):
        alias, found = _target(t, p, document)
        if alias is None:
            return paramErr
        node = found.node if found is not None else None
    if not isinstance(node, File):
        return fnfErr
    return _replace(t, t.mem.r32(p + srAlias) & t.mem.system.addr_mask, al.make(node, document))


@trap
def CreateEditionContainerFile(t, edition_file, fd_creator, script):
    creator = (fd_creator & 0xFFFFFFFF).to_bytes(4, "big")

    def create():
        node = _files(t).create(*_spec(t, edition_file))
        node.type = UNKNOWN_TYPE
        node.creator = creator
        _files(t).metadata_changed(node)
    return _fs_call(t, create)


@trap
def DeleteEditionContainerFile(t, edition_file):
    st = _state(t)
    node = _container_file(t, edition_file)
    control = st.controls.get(node) if node is not None else None
    if control is not None and (control.writer is not None or _publishers(t, control)):
        return fBsyErr
    return _fs_call(t, lambda: _files(t).delete(*_spec(t, edition_file)))


@trap
def GetLastEditionContainerUsed(t, container):
    st = _state(t)
    if st.last is not None:
        vref, parid, name = st.last
        node = _lookup(t, vref, parid, name)
        if isinstance(node, File):
            _put_node(t, container, node)
            return noErr
        _put_container(t, container, vref, parid, name)
        return fnfErr
    vol, _, dirid = _files(t).get_vol()
    _put_container(t, container, vol.vref, dirid, "")
    return fnfErr


@trap
def GetEditionInfo(t, section_h, edition_info):
    st = _state(t)
    s = st.sections.get(section_h & t.mem.system.addr_mask)
    if s is None or s.control is None or not _exists(s.control.node):
        return fnfErr
    _put_info(t, edition_info, s.control.node)
    return noErr


@trap
def GetEditionOpenerProc(t, opener):
    t.mem.w32(opener, _opener(t, _state(t)))
    return noErr


@trap
def SetEditionOpenerProc(t, opener):
    _state(t).openers[t.process] = opener & 0xFFFFFFFF or STANDARD_OPENER
    return noErr


@trap
def CallEditionOpenerProc(t, selector, pb, routine):
    return _call_opener(t, routine, selector, pb)


@trap
def CallFormatIOProc(t, selector, pb, routine):
    return _call_io(t, routine, selector, pb)


# =========================================================================
# Reading and writing editions
# =========================================================================
def _section_file(t, st: Editions, h: int, p: int) -> File | None:
    s = st.sections.get(h)
    if s is not None and s.control is not None and _exists(s.control.node):
        return s.control.node
    _, found = _target(t, p)
    return found.node if found is not None and isinstance(found.node, File) else None


def _opened(t, st: Editions, h: int, p: int, verb: int, refnum_p: int, document: int = 0,
            creator: bytes = b"????") -> int:
    section = st.sections.get(h) or Section(h, t.process, None)
    node = _section_file(t, st, h, p)
    if node is None:
        return fnfErr
    o = _open(t, st, section, node, verb, document, creator)
    if isinstance(o, int):
        return o
    t.mem.w32(p + srRefNum, o.refnum)
    t.mem.w32(refnum_p, o.refnum)
    return noErr


@trap
def OpenEdition(t, subscriber_section_h, refnum):
    st = _state(t)
    t.mem.w32(refnum, 0)
    h = subscriber_section_h & t.mem.system.addr_mask
    p = _record_of(t, h)
    if p is None or _is_publisher(t, p):
        return permErr
    return _opened(t, st, h, p, eoOpen, refnum)


@trap
def OpenNewEdition(t, publisher_section_h, fd_creator, publisher_section_document, refnum):
    st = _state(t)
    m = t.mem
    m.w32(refnum, 0)
    h = publisher_section_h & t.mem.system.addr_mask
    p = _record_of(t, h)
    if p is None or not _is_publisher(t, p):
        return wrPermErr
    s = st.sections.get(h)
    if s is not None and s.control is not None and s.control.writer is not None:
        return fLckdErr                     # another publisher is writing it
    creator = (fd_creator & 0xFFFFFFFF).to_bytes(4, "big")
    document = publisher_section_document & t.mem.system.addr_mask
    err = _opened(t, st, h, p, eoOpenNew, refnum, document, creator)
    if err:
        return err
    o = st.opens[m.r32(refnum)]
    if s is not None and s.control is not None:
        s.control.writer = o
    if document:
        doc = _document(t, document)
        edition = _section_file(t, st, h, p)
        if doc is not None:
            a = al.make(doc, edition)
        else:
            vref, parid, name = _spec(t, document)
            a = al.Alias(name=name)
            d = _lookup(t, vref, parid, None)
            if d is not None:
                a = al.from_path(t.system.files, al.path_of(d).rstrip(":") + ":" + name)
        err = _write_all(t, o, ALIS, a.pack() + struct.pack(">l", m.r32s(p + srSectionID)))
        if err:
            _close(t, o, False)
            _forget_open(t, st, o)
            m.w32(p + srRefNum, 0)
            m.w32(refnum, 0)
            return err
    return noErr


def _open_edition(t, st: Editions, refnum: int) -> Open | None:
    return st.opens.get(refnum & t.mem.system.addr_mask)


@trap
def EditionHasFormat(t, which_edition, which_format, format_size):
    st = _state(t)
    o = _open_edition(t, st, which_edition)
    if o is None:
        return rfNumErr
    fmt = (which_format & 0xFFFFFFFF).to_bytes(4, "big")
    err, index, size = _io(t, o, ioHasFormat, fmt)
    if err:
        return err
    o.indexes[fmt] = index
    o.marks.setdefault(fmt, 0)
    if format_size:
        t.mem.w32(format_size, size)
    return noErr


@trap
def ReadEdition(t, which_edition, which_format, buff_ptr, buff_len):
    st = _state(t)
    m = t.mem
    o = _open_edition(t, st, which_edition)
    if o is None or o.publisher:
        return rfNumErr
    fmt = (which_format & 0xFFFFFFFF).to_bytes(4, "big")
    index = _index(t, o, fmt)
    if isinstance(index, tuple):
        return index[0]
    n = m.r32s(buff_len)
    err, _, got = _io(t, o, ioReadFormat, fmt, index, o.marks[fmt], buff_ptr, n)
    got = max(0, min(got, n))
    o.marks[fmt] += got
    m.w32(buff_len, got)
    return err


@trap
def WriteEdition(t, which_edition, which_format, buff_ptr, buff_len):
    st = _state(t)
    o = _open_edition(t, st, which_edition)
    if o is None or not o.publisher:
        return rfNumErr
    fmt = (which_format & 0xFFFFFFFF).to_bytes(4, "big")
    index = _index(t, o, fmt, new=True)
    if isinstance(index, tuple):
        return index[0]
    n = max(buff_len, 0)
    err, _, _ = _io(t, o, ioWriteFormat, fmt, index, o.marks[fmt], buff_ptr, n)
    if not err:
        o.marks[fmt] += n
    return err


@trap
def GetEditionFormatMark(t, which_edition, which_format, current_mark):
    st = _state(t)
    o = _open_edition(t, st, which_edition)
    if o is None:
        return rfNumErr
    fmt = (which_format & 0xFFFFFFFF).to_bytes(4, "big")
    if fmt not in o.indexes and (o.publisher or isinstance(_index(t, o, fmt), tuple)):
        return noTypeErr
    t.mem.w32(current_mark, o.marks[fmt])
    return noErr


@trap
def SetEditionFormatMark(t, which_edition, which_format, set_mark_to):
    st = _state(t)
    o = _open_edition(t, st, which_edition)
    if o is None:
        return rfNumErr
    fmt = (which_format & 0xFFFFFFFF).to_bytes(4, "big")
    index = _index(t, o, fmt, new=o.publisher)
    if isinstance(index, tuple):
        return noTypeErr if not o.publisher else index[0]
    o.marks[fmt] = set_mark_to & 0xFFFFFFFF
    return noErr


@trap
def CloseEdition(t, which_edition, successful):
    st = _state(t)
    m = t.mem
    o = _open_edition(t, st, which_edition)
    if o is None:
        return rfNumErr
    success = bool(successful & 0xFF)
    err = _close(t, o, success)
    _forget_open(t, st, o)
    section, control = o.section, o.section.control
    p = _record_of(t, section.handle)
    if p is not None:
        m.w32(p + srRefNum, 0)
    if err or not success or p is None:
        return err
    if not o.publisher:
        m.w32(p + srMdDate, struct.unpack_from(">I", o.info, eiMdDate)[0])
        return noErr
    node = control.node if control is not None else _section_file(t, st, section.handle, p)
    if node is None:
        return noErr
    mddate = m.r32(p + srMdDate)

    def stamp():
        node.mddate = mddate
        _files(t).metadata_changed(node)
    _fs_call(t, stamp)
    st.commits[node] = st.commits.get(node, 0) + 1
    if control is not None:
        _read_subscribers(t, control, mddate)
    return noErr


@trap
def GetStandardFormats(t, container, preview_format, preview, publisher_alias, formats):
    """The preview, the publisher's alias and the list of formats of an
    edition, into the handles given for them."""
    st = _state(t)
    m = t.mem
    if m.r32(container + ecThePart) != kPartsNotUsed:
        return badSubPartErr
    node = _container_file(t, container)
    if node is None:
        return fnfErr
    o = _open(t, st, Section(0, t.process, None), node, eoOpen)
    if isinstance(o, int):
        return o
    result = noErr
    try:
        wanted = [(h & t.mem.system.addr_mask, kinds) for h, kinds in
                  ((preview, (PRVW, PICT, TEXT, SND)), (publisher_alias, (ALIS,)),
                   (formats, (FMTS,)))]
        for h, kinds in wanted:
            if not h:
                continue
            errors = []
            for fmt in kinds:
                data = _read_all(t, o, fmt)
                if not isinstance(data, int):
                    break
                errors.append(data)
            else:
                # none there, or why one that is couldn't be read
                result = next((e for e in errors if e != noTypeErr), noTypeErr)
                continue
            heap = t.system.heap_of(h)
            if not heap.set_handle_size(h, len(data)):
                result = memFullErr
                continue
            m.write(heap.deref(h), data)
            if h == preview & t.mem.system.addr_mask and preview_format:
                m.write(preview_format, fmt)
    finally:
        _close(t, o, True)
        _forget_open(t, st, o)
    return result


@trap
def GoToPublisherSection(t, container):
    st = _state(t)
    if t.mem.r32(container + ecThePart) != kPartsNotUsed:
        return badSubPartErr
    node = _container_file(t, container)
    if node is None:
        return fnfErr
    control = st.controls.get(node)
    publishers = _publishers(t, control) if control is not None else []
    if publishers:
        _send(t, publishers[0].process, sectionScrollMsgID, publishers[0].handle)
        return noErr
    # Open the publisher's document, or the edition, in its application
    alias_h = t.system.sys_heap.new_handle(0)
    if not alias_h:
        return memFullErr
    try:
        got = None
        if GetStandardFormats(t, container, 0, 0, alias_h, 0) == noErr:
            got = _edition_alias(t.tb.contents(alias_h))
    finally:
        t.system.sys_heap.dispose_handle(alias_h)
    target, sid = node, None
    if got is not None:
        try:
            target = al.resolve(t.system.files, got[0], node).node
        except FileError:
            target = None
        sid = got[1]
    if not isinstance(target, File):
        return fnfErr
    d = sf.display_of(t)
    running = [p for p in (d.launch_order if d is not None else ())
               if _alive(t, p) and p.app is not None and p.app.creator == target.creator]
    if not running:
        return fnfErr
    if target is not node:
        st.scrolls.append((node, sid))
    _open_documents(t, running[0], target)
    return noErr


# =========================================================================
# The dialogs
# =========================================================================
class EditionDialog(sf.Dialog):
    """A Standard File dialog for a new publisher or subscriber, starting
    at the container the reply gives."""

    def __init__(self, t, put: bool, container: int, ok: str, prompt: str = "",
                 hook: int = 0, ditl: int = 0, data: int = 0, mask: int = 0,
                 preview: QImage | None = None):
        vref, parid, name = _spec(t, container)
        self.start = (vref, parid)
        self.mask = mask
        self.opener = _opener(t, _state(t))
        self.ditl = ditl
        super().__init__(t, put, prompt, name, data=data or None, hook=hook, new=True)
        self.ok = ok
        self.preview = preview

    def _start(self) -> Dir:
        try:
            d = self.files.lookup(*self.start, None)
        except FileError:
            d = None
        if isinstance(d, Dir):
            return d
        return super()._start()

    def _wanted(self, node: File, index: int) -> bool:
        """An edition the opener says the subscriber can read."""
        t = self.t
        with _on_stack(t, OPENER_PB) as pb:
            _put_info(t, pb + eoInfo, node)
            t.mem.w8(pb + eoFormatsMask, self.mask)
            return _call_opener(t, self.opener, eoCanSubscribe, pb) == noErr

    def _make_custom(self) -> sf.Custom | None:
        hook, _, _ = self.custom_args
        if not hook and not self.ditl:
            return None
        ok = b"Publish" if self.put else b"Subscribe"
        return sf.Custom.expanded(self.t, sf.NEW[self.put], self.ditl, hook, self.data or 0,
                                  ok=ok)


def _preview(t, h: int, fmt: bytes) -> QImage | None:
    """A publisher's preview as the dialog shows it, 120 by 120."""
    h &= t.mem.system.addr_mask
    if not h or fmt not in (PRVW, PICT, TEXT) or sf.display_of(t) is None:
        return None
    try:
        data = t.tb.contents(h)
    except ValueError:
        return None
    img = QImage(kPreviewWidth, kPreviewHeight, QImage.Format.Format_RGB32)
    img.fill(Qt.GlobalColor.white)
    if fmt == TEXT:
        painter = QPainter(img)
        painter.setFont(theme.body_font())
        painter.drawText(QRect(4, 4, kPreviewWidth - 8, kPreviewHeight - 8),
                         Qt.TextFlag.TextWordWrap, from_mac(data).replace("\r", "\n"))
        painter.end()
        return img
    frame = pict.picture_frame(data)
    if frame.empty:
        return img
    if fmt == PRVW:
        dst = Rect(0, 0, kPreviewHeight, kPreviewWidth)
    else:                                   # scaled to fit, keeping its shape
        s = min(kPreviewWidth / frame.width, kPreviewHeight / frame.height)
        w, ht = max(1, round(frame.width * s)), max(1, round(frame.height * s))
        top, left = (kPreviewHeight - ht) // 2, (kPreviewWidth - w) // 2
        dst = Rect(top, left, top + ht, left + w)
    _draw_on(t, img, Rect(0, 0, kPreviewHeight, kPreviewWidth),
             lambda port: pict.draw_picture(t.tb.qd, port, frame, pict.Reader.of(data, 10), dst))
    return img


def _publisher_dialog(t, reply, ditl=0, hook=0, data=0) -> int:
    m = t.mem
    reply &= t.mem.system.addr_mask
    if m.r8(reply + npUsePart):
        return badSubPartErr
    img = _preview(t, m.r32(reply + npPreview), m.read(reply + npPreviewFormat, 4))
    dialog = EditionDialog(t, True, reply + npContainer, "Publish", "Name of new edition:",
                           hook, ditl, data, preview=img)
    result = dialog.run()
    m.w8(reply + npCanceled, int(result is None))
    m.w8(reply + npReplacing, 0)
    if result is None:
        return noErr
    _, d, name, replacing = result
    existing = d.child(name)
    if existing is not None:
        name = existing.name
    m.w8(reply + npReplacing, int(replacing or isinstance(existing, File)))
    _put_container(t, reply + npContainer, d.volume.vref, d.cnid, name)
    return noErr


def _subscriber_dialog(t, reply, ditl=0, hook=0, data=0) -> int:
    m = t.mem
    reply &= t.mem.system.addr_mask
    dialog = EditionDialog(t, False, reply + nsContainer, "Subscribe", hook=hook, ditl=ditl,
                           data=data, mask=m.r8(reply + nsFormatsMask))
    result = dialog.run()
    m.w8(reply + nsCanceled, int(result is None))
    if result is not None:
        _put_node(t, reply + nsContainer, result[1])
    return noErr


def _date(secs: int) -> str:
    d = datetime.datetime(1904, 1, 1) + datetime.timedelta(seconds=secs)
    hour = d.hour % 12 or 12
    return f"{d:%A, %B} {d.day}, {d.year} {hour}:{d:%M:%S} {'AM' if d.hour < 12 else 'PM'}"


def _options_dialog(t, reply, ditl=0, hook=0, data=0) -> int:
    m = t.mem
    reply &= t.mem.system.addr_mask
    st = _state(t)
    h = m.r32(reply + soSectionH) & t.mem.system.addr_mask
    m.w8(reply + soCanceled, 1)
    m.w8(reply + soChanged, 0)
    m.write(reply + soAction, b"    ")
    p = _record_of(t, h)
    if p is None:
        return paramErr
    display = sf.display_of(t)
    if display is None:
        return noErr
    publisher = _is_publisher(t, p)
    mode = m.r16(p + srMode)
    node = _section_file(t, st, h, p)
    if node is not None:
        name, latest = node.name, _date(node.mddate)
    else:
        alias, _ = _target(t, p)
        name, latest = (alias.name if alias is not None else ""), "None"
    custom = None
    if hook or ditl:
        custom = sf.Custom.expanded(t, OPTIONS, ditl, hook, data, refcon=emOptionsDialogRefCon,
                                    ok=b"OK")
        if custom is None:
            return memFullErr
    manual = mode == sumManual
    bridge = display.bridge
    try:
        if custom is not None:
            custom.call(sf.sfHookFirstCall)
        with t.tb.host_dialog():
            dlg, (fut,) = bridge.call(editiondlg.open_options, display, publisher, name, latest,
                                      manual)
            try:
                while True:
                    step = sf.wait(t, bridge, fut)
                    if step[0] == "mode":
                        item = emHookManualUpdateMode if step[1] else emHookAutoUpdateMode
                    else:
                        item = OPTION_ITEMS[step[1]]
                    if custom is not None:
                        item = custom.call(item)
                    if item in (emHookManualUpdateMode, emHookAutoUpdateMode):
                        manual = item == emHookManualUpdateMode
                    elif item in (1, 2, emHookCancelSection, emHookGoToPublisher,
                                  emHookEditionNow):
                        break
                    (fut,) = bridge.call(dlg.resume, manual)
            finally:
                bridge.post(dlg.finish)
        if custom is not None:
            custom.call(sf.sfHookLastCall)
    finally:
        if custom is not None:
            custom.dispose()
    if item == 2:
        return noErr
    m.w8(reply + soCanceled, 0)
    action = {1: b"    ", emHookCancelSection: b"cncl", emHookGoToPublisher: b"goto",
              emHookEditionNow: b"writ" if publisher else b"read"}[item]
    m.write(reply + soAction, action)
    new_mode = sumManual if manual else sumAutomatic
    if new_mode != mode:
        m.w16(p + srMode, new_mode)
        m.w8(reply + soChanged, 1)
    return noErr


@trap
def NewPublisherDialog(t, reply):
    return _publisher_dialog(t, reply)


@trap
def NewPublisherExpDialog(t, reply, where, ditl, hook, filter_proc, data):
    return _publisher_dialog(t, reply, ditl, hook, data)


@trap
def NewSubscriberDialog(t, reply):
    return _subscriber_dialog(t, reply)


@trap
def NewSubscriberExpDialog(t, reply, where, ditl, hook, filter_proc, data):
    return _subscriber_dialog(t, reply, ditl, hook, data)


@trap
def SectionOptionsDialog(t, reply):
    return _options_dialog(t, reply)


@trap
def SectionOptionsExpDialog(t, reply, where, ditl, hook, filter_proc, data):
    return _options_dialog(t, reply, ditl, hook, data)
