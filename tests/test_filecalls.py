"""The File Manager's parameter-block, HFSDispatch and FSSpec calls from
68k code, on the volume a System keeps in memory (Inside Macintosh: Files,
chapter 2; IM IV, chapter 19)."""

import struct

import pytest
from guest import Processor
from test_cdrom import image

from macrt.m68k import glue_files as gf
from macrt.toolbox import files as fm
from macrt.toolbox import sysres

# The calls' trap words; the HFS forms add $200.
Open, Close, Write = 0xA000, 0xA001, 0xA003
Create, Delete, Rename = 0xA008, 0xA009, 0xA00B
GetFInfo, SetFInfo, Eject = 0xA00C, 0xA00D, 0xA017
Allocate, GetVol, SetVol, GetFPos = 0xA010, 0xA014, 0xA015, 0xA018
GetVInfo = 0xA007
SetFVers, SetFPos, FlushFile = 0xA043, 0xA044, 0xA045
HFS = 0x200
HFSDispatch = 0xA260
OpenWD, CloseWD, CatMove, DirCreate, GetWDInfo = 1, 2, 5, 6, 7
GetFCBInfo, GetCatInfo, SetCatInfo, SetVInfo = 8, 9, 10, 11
ExchangeFiles, GetVolParms = 0x17, 0x30

LONGS = {"ioMisc", "ioBuffer", "ioReqCount", "ioPosOffset", "ioDirID", "ioWDProcID",
         "ioWDDirID", "csParam", "ioNewName", "ioNewDirID", "ioDestNamePtr", "ioDestDirID",
         "ioVBkUp"}


class Calls(Processor):
    def name(self, s):
        """A Str255 the File Manager can write a name back into."""
        b = s.encode("mac_roman")
        return self.ptr(bytes([len(b)]) + b + bytes(255 - len(b)))

    def pb(self, name=None, **fields):
        p = self.ptr(bytes(128))
        if name is not None:
            self.mem.w32(p + gf.ioNamePtr, self.name(name))
        for k, v in fields.items():
            (self.mem.w32 if k in LONGS else self.mem.w16)(p + getattr(gf, k), v)
        return p

    def call(self, word, pb, selector=None):
        """MOVEA.L #pb,A0; [MOVE.W #selector,D0]; the trap.  What D0 says,
        which ioResult says too."""
        code = struct.pack(">HI", 0x207C, pb)
        if selector is not None:
            code += struct.pack(">HH", 0x303C, selector)
        self.run(code + struct.pack(">H", word))
        err = self.cpu.d(0) & 0xFFFF
        assert self.mem.r16(pb + gf.ioResult) == err
        return err - 0x10000 if err & 0x8000 else err

    def long(self, pb, field):
        return self.mem.r32(pb + getattr(gf, field))

    def w(self, pb, field):
        return self.mem.r16s(pb + getattr(gf, field))

    def named(self, pb):
        return self.str255(self.long(pb, "ioNamePtr"))

    def default(self):
        """The default volume, as its reference number."""
        return self.process.files.get_vol()[0].vref

    def node(self, name, dirid=fm.ROOT):
        try:
            return self.process.files.lookup(self.default(), dirid, name)
        except fm.FileError:
            return None

    def open(self, name, data=b""):
        assert self.call(Create, self.pb(name)) == 0
        p = self.pb(name)
        self.mem.w8(p + gf.ioPermssn, fm.fsRdWrPerm)
        assert self.call(Open, p) == 0
        refnum = self.w(p, "ioRefNum")
        if data:
            p = self.pb(None, ioRefNum=refnum, ioBuffer=self.ptr(data), ioReqCount=len(data))
            assert self.call(Write, p) == 0
        return refnum


@pytest.fixture
def g(qapp):
    return Calls()


def test_the_mark_moves_and_space_is_allocated(g):
    refnum = g.open("Doc", b"hello world")
    p = g.pb(None, ioRefNum=refnum)
    assert g.call(GetFPos, p) == 0 and g.long(p, "ioPosOffset") == 11

    p = g.pb(None, ioRefNum=refnum, ioPosMode=fm.fsFromStart, ioPosOffset=6)
    assert g.call(SetFPos, p) == 0 and g.long(p, "ioPosOffset") == 6
    # Past the end: the mark stops at the end, with eofErr.
    p = g.pb(None, ioRefNum=refnum, ioPosMode=fm.fsFromMark, ioPosOffset=100)
    assert g.call(SetFPos, p) == fm.eofErr and g.long(p, "ioPosOffset") == 11

    p = g.pb(None, ioRefNum=refnum, ioReqCount=5000)
    assert g.call(Allocate, p) == 0 and g.long(p, "ioActCount") == 2 * fm.BLOCK
    assert g.call(FlushFile, g.pb(None, ioRefNum=refnum)) == 0
    # A driver's number names no file.
    assert g.call(GetFPos, g.pb(None, ioRefNum=-4)) == gf.unitEmptyErr
    # Version numbers are MFS's.
    assert g.call(SetFVers, g.pb("Doc")) == gf.wrgVolTypErr


def test_what_is_known_of_an_open_file(g):
    refnum = g.open("Doc", b"hello world")
    p = g.pb("", ioRefNum=refnum)
    assert g.call(HFSDispatch, p, GetFCBInfo) == 0
    assert g.named(p) == "Doc"
    cnid, flags, _, eof, plen, mark, vref, clump, parid = struct.unpack(
        ">IHHIIIhII", g.mem.read(p + gf.ioFCBFlNm, 30))
    doc = g.node("Doc")
    assert (cnid, eof, plen, mark, vref, clump, parid) == (
        doc.cnid, 11, fm.BLOCK, 11, g.default(), fm.BLOCK, fm.ROOT)
    assert g.w(p, "ioVRefNum") == g.default()

    # By index, among the volume's paths, and none past the last.
    assert (refnum, "Doc") in paths(g)
    assert g.call(Close, g.pb(None, ioRefNum=refnum)) == 0
    assert (refnum, "Doc") not in paths(g)


def paths(g):
    """GetFCBInfo's access paths on the default volume, by index."""
    found = []
    while True:
        p = g.pb("", ioFCBIndx=len(found) + 1, ioVRefNum=g.default())
        err = g.call(HFSDispatch, p, GetFCBInfo)
        if err:
            assert err == fm.fnOpnErr
            return found
        found.append((g.w(p, "ioRefNum"), g.named(p)))


def test_renaming_and_finder_information(g):
    assert g.call(Create, g.pb("A")) == 0
    assert g.call(Rename, g.pb("A", ioMisc=g.name("B"))) == 0
    assert g.node("A") is None and g.node("B") is not None
    assert g.call(Rename, g.pb("B")) == fm.bdNamErr     # no new name

    p = g.pb("B")
    assert g.call(GetFInfo, p) == 0
    g.mem.write(p + gf.ioFlFndrInfo, b"TEXTMACR" + bytes(8))
    g.mem.write(p + gf.ioFlCrDat, struct.pack(">II", 1000, 2000))
    assert g.call(SetFInfo, p) == 0
    q = g.pb("B")
    assert g.call(GetFInfo | HFS, q) == 0
    assert g.mem.read(q + gf.ioFlFndrInfo, 8) == b"TEXTMACR"
    assert struct.unpack(">II", g.mem.read(q + gf.ioFlCrDat, 8)) == (1000, 2000)
    node = g.node("B")
    assert (node.type, node.creator) == (b"TEXT", b"MACR")


def test_folders_moves_and_catalog_information(g):
    d = g.pb("Folder")
    assert g.call(HFSDispatch, d, DirCreate) == 0
    folder = g.long(d, "ioDirID")
    assert g.node("Folder").cnid == folder

    a, b = g.open("A", b"aaaa"), g.open("B", b"bb")
    assert g.call(HFSDispatch, g.pb("A", ioNewDirID=folder), CatMove) == 0
    assert g.node("A") is None
    c = g.pb("A", ioDirID=folder)
    assert g.call(HFSDispatch, c, GetCatInfo) == 0 and g.long(c, "ioFlParID") == folder

    g.mem.write(c + gf.ioFlCrDat, struct.pack(">III", 1, 2, 3))
    g.mem.write(c + gf.ioFlXFndrInfo, b"x" * 16)
    g.mem.w32(c + gf.ioDirID, folder)
    assert g.call(HFSDispatch, c, SetCatInfo) == 0
    node = g.node("A", folder)
    assert (node.crdate, node.mddate, node.bkdate, bytes(node.xinfo)) == (1, 2, 3, b"x" * 16)

    # The forks trade places, and the open paths go with the data.
    p = g.pb("B", ioDestNamePtr=g.name("A"), ioDestDirID=folder)
    assert g.call(HFSDispatch, p, ExchangeFiles) == 0
    assert g.node("B").sizes()[0] == 4 and node.sizes()[0] == 2
    assert g.system.files.fcb(a).node is g.node("B")
    assert g.system.files.fcb(b).node is node

    # A selector no one has is unimplemented, and paramErr.
    assert g.call(HFSDispatch, g.pb("B"), 0x7F) == fm.paramErr
    assert g.missing == ["HFSDispatch selector $7F"]


def test_the_default_directory_and_working_directories(g):
    vref = g.default()
    d = g.pb("Folder")
    assert g.call(HFSDispatch, d, DirCreate) == 0
    folder = g.long(d, "ioDirID")

    h = g.pb("")
    assert g.call(GetVol | HFS, h) == 0
    assert (g.w(h, "ioVRefNum"), g.long(h, "ioWDDirID"), g.w(h, "ioWDVRefNum")) == (
        vref, fm.ROOT, vref)
    assert g.named(h) == g.process.files.get_vol()[0].name
    assert g.call(SetVol | HFS, g.pb(None, ioVRefNum=vref, ioDirID=folder)) == 0
    assert g.call(GetVol | HFS, h) == 0 and g.long(h, "ioWDDirID") == folder

    w = g.pb(None, ioVRefNum=vref, ioDirID=folder, ioWDProcID=0x54455354)
    assert g.call(HFSDispatch, w, OpenWD) == 0
    wd = g.w(w, "ioVRefNum")
    assert wd != vref
    i = g.pb("", ioVRefNum=wd)
    assert g.call(HFSDispatch, i, GetWDInfo) == 0
    assert (g.w(i, "ioWDVRefNum"), g.long(i, "ioWDDirID"), g.long(i, "ioWDProcID")) == (
        vref, folder, 0x54455354)

    # SetVol to the working directory: GetVol gives its number back.
    assert g.call(SetVol, g.pb(None, ioVRefNum=wd)) == 0
    v = g.pb("")
    assert g.call(GetVol, v) == 0 and g.w(v, "ioVRefNum") == wd
    assert g.call(GetVol | HFS, h) == 0 and g.long(h, "ioWDProcID") == 0x54455354

    # The default directory's stays open while it's the default.
    assert g.call(HFSDispatch, g.pb(None, ioVRefNum=wd), CloseWD) == 0
    assert wd in g.system.files.wds
    assert g.call(SetVol, g.pb(None, ioVRefNum=vref)) == 0
    assert g.call(HFSDispatch, g.pb(None, ioVRefNum=wd), CloseWD) == 0
    assert g.call(HFSDispatch, g.pb(None, ioVRefNum=wd), CloseWD) == fm.nsvErr


def test_volume_parameters_and_information(g):
    vref = g.default()
    buf = g.ptr(bytes(32))
    p = g.pb(None, ioVRefNum=vref, ioBuffer=buf, ioReqCount=32)
    assert g.call(HFSDispatch, p, GetVolParms) == 0
    assert g.long(p, "ioActCount") == 20 and g.mem.r16(buf) == 2     # version 2
    p = g.pb(None, ioVRefNum=vref, ioBuffer=buf, ioReqCount=2)
    assert g.call(HFSDispatch, p, GetVolParms) == 0 and g.long(p, "ioActCount") == 2

    count = len(g.system.files.volumes)
    assert g.call(GetVInfo, g.pb("", ioVolIndex=count + 1)) == fm.nsvErr
    # The boot volume was made on the same date each time macrt starts.
    p = g.pb("", ioVolIndex=1)
    assert g.call(GetVInfo, p) == 0 and g.long(p, "ioVCrDate") == fm.BOOT_CREATED
    assert g.call(Eject, g.pb(None, ioVRefNum=-99)) == fm.nsvErr

    assert g.call(Create, g.pb("Doc")) == 0
    v = g.pb("Renamed", ioVRefNum=vref, ioVAtrb=0x8000, ioVBkUp=77)
    assert g.call(HFSDispatch, v, SetVInfo) == 0
    vol = g.system.files.volume(vref)
    assert (vol.name, vol.locked, vol.bkdate) == ("Renamed", True, 77)
    # A locked volume's files keep their information.
    assert g.call(SetFInfo, g.pb("Doc")) == fm.vLckdErr
    assert g.call(HFSDispatch, g.pb("Doc"), SetCatInfo) == fm.vLckdErr


def test_a_cd_takes_no_changes(g, tmp_path):
    # Locked by hardware, as a CD-ROM is: what would change it says wPrErr,
    # and its files open to be read (Treasure Galaxy tells a hard disk from
    # its CD by whether it can make a file on it).
    vref = g.system.files.mount("Disc", fm.ImageStore(image(tmp_path, "Disc.iso"))).vref
    p = g.pb("", ioVRefNum=vref)
    assert g.call(GetVInfo, p) == 0 and g.w(p, "ioVAtrb") == 0x0080
    assert g.call(Create, g.pb("Test", ioVRefNum=vref)) == fm.wPrErr
    assert g.call(HFSDispatch, g.pb("Folder", ioVRefNum=vref), DirCreate) == fm.wPrErr
    assert g.call(Delete, g.pb("Program", ioVRefNum=vref)) == fm.wPrErr
    assert g.call(Rename, g.pb("Program", ioVRefNum=vref, ioMisc=g.name("B"))) == fm.wPrErr
    assert g.call(SetFInfo, g.pb("Program", ioVRefNum=vref)) == fm.wPrErr
    assert g.call(HFSDispatch, g.pb("Program", ioVRefNum=vref), SetCatInfo) == fm.wPrErr
    assert g("FSpSetFInfo", spec(g, "Program", vref=vref), g.ptr(bytes(16))) == fm.wPrErr
    for perm in (fm.fsWrPerm, fm.fsRdWrPerm, fm.fsRdWrShPerm):
        p = g.pb("Program", ioVRefNum=vref)
        g.mem.w8(p + gf.ioPermssn, perm)
        assert g.call(Open, p) == fm.wPrErr
    p = g.pb("Program", ioVRefNum=vref)
    assert g.call(Open, p) == 0                         # fsCurPerm: to be read
    w = g.pb(None, ioRefNum=g.w(p, "ioRefNum"), ioBuffer=g.ptr(b"x"), ioReqCount=1)
    assert g.call(Write, w) == fm.wrPermErr
    assert g.system.files.volume(vref).root.child("Program").fork(False) == b""


def spec(g, name, parid=fm.ROOT, vref=None):
    p = g.ptr(bytes(70))
    gf.put_spec(g, p, g.default() if vref is None else vref, parid, name)
    return p


def test_the_fsspec_calls(g):
    dirid = g.ptr(bytes(4))
    assert g("FSpDirCreate", spec(g, "Folder"), 0, dirid) == 0
    folder = g.mem.r32(dirid)
    assert g.node("Folder").cnid == folder
    assert g("FSpCreate", spec(g, "Doc"), 0x4D414352, 0x54455854, 0) == 0

    refnum = g.ptr(bytes(2))
    assert g("FSpOpenRF", spec(g, "Doc"), fm.fsRdWrPerm, refnum) == 0
    assert g.system.files.fcb(g.mem.r16s(refnum)).rsrc
    g.process.files.close(g.mem.r16s(refnum))

    info = g.ptr(bytes(16))
    assert g("FSpGetFInfo", spec(g, "Doc"), info) == 0
    assert g.mem.read(info, 8) == b"TEXTMACR"
    g.mem.write(info, b"APPLMACR")
    assert g("FSpSetFInfo", spec(g, "Doc"), info) == 0
    assert g.node("Doc").type == b"APPL"

    assert g("FSpSetFLock", spec(g, "Doc")) == 0 and g.node("Doc").locked
    assert g("FSpRename", spec(g, "Doc"), g.name("Locked")) == fm.fLckdErr
    assert g("FSpRstFLock", spec(g, "Doc")) == 0 and not g.node("Doc").locked
    assert g("FSpRename", spec(g, "Doc"), g.name("Note")) == 0

    assert g("FSpCatMove", spec(g, "Note"), spec(g, "Folder")) == 0
    assert g.node("Note") is None and g.node("Note", folder) is not None
    other = next(v for v in g.system.files.volumes if v.vref != g.default())
    assert g("FSpCatMove", spec(g, "Note", folder),
             spec(g, "", vref=other.vref)) == fm.diffVolErr

    assert g("FSpCreate", spec(g, "Other"), 0x4D414352, 0x54455854, 0) == 0
    note, other = g.node("Note", folder), g.node("Other")
    note.mddate, other.mddate = 1, 2
    assert g("FSpExchangeFiles", spec(g, "Note", folder), spec(g, "Other")) == 0
    assert (note.mddate, other.mddate, note.type) == (2, 1, b"APPL")

    assert g("FSpDelete", spec(g, "Note", folder)) == 0
    assert g("FSpGetFInfo", spec(g, "Note", folder), info) == fm.fnfErr


def test_making_an_fsspec(g):
    vref = g.default()
    dirid = g.ptr(bytes(4))
    assert g("FSpDirCreate", spec(g, "Folder"), 0, dirid) == 0
    folder = g.mem.r32(dirid)
    assert g("FSpCreate", spec(g, "Doc", folder), 0x4D414352, 0x54455854, 0) == 0

    p = g.ptr(bytes(70))
    # The catalog's own spelling, and fnfErr, filled in, for what isn't there yet.
    assert g("FSMakeFSSpec", vref, folder, g.name("doc"), p) == 0
    assert gf._spec(g, p) == (vref, folder, "Doc")
    assert g("FSMakeFSSpec", vref, fm.ROOT, g.name(":Folder:New"), p) == fm.fnfErr
    assert gf._spec(g, p) == (vref, folder, "New")
    # A directory is said by its parent; the root's parent has none.
    assert g("FSMakeFSSpec", vref, folder, 0, p) == 0
    assert gf._spec(g, p) == (vref, fm.ROOT, "Folder")
    assert g("FSMakeFSSpec", vref, fm.ROOT_PARENT, 0, p) == fm.dirNFErr
    assert g("FSMakeFSSpec", vref, fm.ROOT, g.name("x" * 64), p) == fm.bdNamErr

    # PBGetFInfo by index names the directory's files.
    i = g.pb("", ioFDirIndex=1, ioDirID=folder)
    assert g.call(GetFInfo | HFS, i) == 0 and g.named(i) == "Doc"


def test_more_calls_by_name(g):
    vref = g.default()
    refnum = g.open("Doc")
    p = g.pb(None, ioRefNum=refnum, ioBuffer=g.ptr(bytes(4)), ioReqCount=-1)
    assert g.call(Write, p) == fm.paramErr
    assert g.call(Close, g.pb(None, ioRefNum=refnum)) == 0
    o = g.pb("Doc")
    assert g.call(HFSDispatch, o, 0x1A) == 0                # PBHOpenDF
    assert g.system.files.fcb(g.w(o, "ioRefNum")).node is g.node("Doc")

    assert g.call(0xA041, g.pb("Doc")) == 0 and g.node("Doc").locked     # SetFLock
    assert g.call(Delete, g.pb("Doc")) == fm.fLckdErr
    assert g.call(0xA042, g.pb("Doc")) == 0                             # RstFLock
    assert g.call(Delete, g.pb("Doc")) == fm.fBsyErr                   # open still
    assert g.call(Close, o) == 0
    assert g.call(Delete, g.pb("Doc")) == 0 and g.node("Doc") is None

    # PBClose of a resource file is CloseResFile.
    g("FSpCreateResFile", spec(g, "Res"), 0x3F3F3F3F, 0x3F3F3F3F, 0)
    res = g("FSpOpenResFile", spec(g, "Res"), fm.fsRdWrPerm)
    assert res in g.system.files.fcbs
    assert g.call(Close, g.pb(None, ioRefNum=res)) == 0
    assert res not in g.system.files.fcbs

    # A volume by its name, and by its place in the queue.
    name = g.process.files.get_vol()[0].name
    v = g.pb(name + ":", ioVolIndex=-1)
    assert g.call(GetVInfo, v) == 0 and g.w(v, "ioVRefNum") == vref
    v = g.pb("", ioVolIndex=1)
    assert g.call(GetVInfo, v) == 0
    assert g.named(v) == g.system.files.volumes[0].name

    # Working directories by index.
    d = g.pb("Folder")
    assert g.call(HFSDispatch, d, DirCreate) == 0
    w = g.pb(None, ioVRefNum=vref, ioDirID=g.long(d, "ioDirID"), ioWDProcID=7)
    assert g.call(HFSDispatch, w, OpenWD) == 0
    i = g.pb("", ioWDIndex=1, ioWDVRefNum=vref, ioWDProcID=7)
    assert g.call(HFSDispatch, i, GetWDInfo) == 0
    assert (g.w(i, "ioVRefNum"), g.long(i, "ioWDDirID")) == (
        g.w(w, "ioVRefNum"), g.long(d, "ioDirID"))


def test_the_device_managers_own_drivers(g):
    Control, Status = 0xA004, 0xA005
    disk = next(v for v in g.system.files.volumes if not v.floppy)
    p = g.pb(None, ioRefNum=disk.driver, csCode=gf.kDriveIcon)
    assert g.call(Control, p) == 0
    icon = g.long(p, "csParam")
    assert g.mem.read(icon, len(sysres.HARD_DISK_ICON)) == sysres.HARD_DISK_ICON
    p = g.pb(None, ioRefNum=disk.driver, csCode=gf.kMediaIcon)
    assert g.call(Control, p) == 0 and g.long(p, "csParam") == icon        # made once
    assert g.call(Control, g.pb(None, ioRefNum=disk.driver, csCode=1)) == gf.controlErr
    assert g.call(Control, g.pb(None, ioRefNum=-30)) == gf.unitEmptyErr
    assert g.call(Control, g.pb(None, ioRefNum=30)) == gf.badUnitErr

    # The serial ports: nothing has come in, and nothing else to say.
    port = gf.SERIAL_DRIVERS[".ain"]
    p = g.pb(None, ioRefNum=port, csCode=gf.SerGetBuf)
    g.mem.write(p + gf.csParam, b"\xff" * 6)
    assert g.call(Status, p) == 0 and g.mem.read(p + gf.csParam, 6) == bytes(6)
    assert g.call(Control, g.pb(None, ioRefNum=port, csCode=8)) == 0
    assert g.call(Status, g.pb(None, ioRefNum=port, csCode=99)) == gf.statusErr
