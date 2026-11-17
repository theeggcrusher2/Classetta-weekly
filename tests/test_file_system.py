"""The File Manager below the traps (macrt.toolbox.files): what its
volumes, catalog, access paths and working directories refuse, and how
the files in a host folder are found and written."""

import os

import machfs
import pytest
from builders import binhex
from macrt import macfile
from macrt.macfile import (APPLE_SINGLE, AS_DATA, AS_FINDER_INFO, AS_NAME, AS_RSRC,
                           HostFolder, MacFile, MemoryFolder, VolumeFolder, load_file, load_volume)
from macrt.resfork import Resource, build_fork
from macrt.toolbox import files as fm
from macrt.toolbox import lowmem
from macrt.toolbox.files import FileError, FileManager
from macrt.toolbox.system import System

FORK = build_fork([Resource(b"STR ", 1, b"\3hi!")])
NAME = 62                               # fcbCName, in an FCB's record
OTHER = 4                               # an AppleSingle entry macrt doesn't read


def refused(code, call, *args):
    with pytest.raises(FileError) as e:
        call(*args)
    assert e.value.code == code


def in_memory(*files):
    s = System()
    folder = MemoryFolder(list(files))
    return s, folder, FileManager(s.files, None, s.files.dir_for(folder))


def floppy(name):
    v = machfs.Volume()
    v.name = name
    f = machfs.File()
    f.data = b"on " + name.encode()
    v["File"] = f
    return load_volume(v.write(800 * 1024, desktopdb=False, bootable=False))


def on_host(root):
    s = System()
    return s, FileManager(s.files, None, s.files.dir_for(HostFolder(root)))


def test_a_file_moved_from_the_top_of_a_memory_folder_keeps_its_forks(qapp):
    """A MemoryFolder holds only the files at the top of its volume: one
    moved into a directory is kept in memory, and moved back is the
    folder's again, forks and all."""
    s, folder, files = in_memory(MacFile("Doc", b"doc", FORK, type=b"TEXT", creator=b"ttxt"))
    d = files.create(0, 0, "D", directory=True)
    files.move(0, 0, "Doc", d.cnid, None)
    assert folder.files() == []
    assert [bytes(f) for f in files.lookup_file(0, d.cnid, "Doc").forks()] == [b"doc", FORK]
    files.move(0, d.cnid, "Doc", fm.ROOT, None)
    [mf] = folder.files()
    assert (mf.name, mf.data, mf.rsrc, mf.type, mf.creator) == (
        "Doc", b"doc", FORK, b"TEXT", b"ttxt")


def test_names_the_catalog_refuses(qapp):
    s = System()
    fs = s.files
    files = FileManager(fs, None, fs.boot.root)
    a = files.create(0, 0, "A", directory=True)
    b = files.create(0, a.cnid, "B", directory=True)
    f = files.create(0, 0, "F")
    assert repr(f) == f"<File 'F' #{f.cnid}>" and repr(fs.boot) == "<Volume 'Macintosh HD' -1>"
    refused(fm.dirNFErr, files.lookup, 0, 0, "::")                  # above the root
    refused(fm.dirNFErr, files.lookup, 0, fm.ROOT_PARENT, None)     # what holds the root
    refused(fm.fnfErr, files.lookup_file, 0, 0, "Missing", True)    # nor in the System Folder
    refused(fm.dirNFErr, files.open_wd, 0, 0, "F", fm.ERIK)         # a file
    refused(fm.dirNFErr, files.open_wd, 0, 0, ":Nowhere:A", fm.ERIK)
    assert files.file_at(0, 0, 1) is f
    refused(fm.fnfErr, files.file_at, 0, 0, 2)                      # directories aren't counted
    # Making one: a name that names a directory, the root's neighbour, too long.
    refused(fm.bdNamErr, files.create, 0, 0, ":A:")
    refused(fm.bdNamErr, files.create, 0, fm.ROOT_PARENT, "Disk")
    refused(fm.bdNamErr, files.create, 0, 0, "x" * 32)
    # Renaming: a volume has 27 characters, a file 31 and no colon.
    refused(fm.bdNamErr, files.rename, 0, 0, "Macintosh HD:", "x" * 28)
    refused(fm.bdNamErr, files.rename, 0, 0, "F", "a:b")
    refused(fm.dupFNErr, files.rename, 0, 0, "F", "a")
    p = files.open(0, 0, "F", False, fm.fsRdPerm)
    assert repr(p) == f"<FCB {p.refnum} 'F'>"
    files.rename(0, 0, "F", "G")                                    # its open path's name too
    assert s.mem.read(fs.fcb_buffer + p.refnum + NAME, 2) == b"\1G"
    refused(fm.fBsyErr, files.delete, 0, 0, "G")
    # Moving: to a directory, not into itself, nor where the name is taken.
    refused(fm.dirNFErr, files.move, 0, 0, "G", a.cnid, "Nowhere")
    refused(fm.badMovErr, files.move, 0, 0, "A", fm.ROOT, "G")
    refused(fm.badMovErr, files.move, 0, 0, "A", b.cnid, None)
    refused(fm.badMovErr, files.move, 0, 0, "Macintosh HD:", a.cnid, None)
    files.move(0, 0, "G", fm.ROOT, None)                            # where it is already
    files.create(0, a.cnid, "G")
    refused(fm.dupFNErr, files.move, 0, 0, "G", a.cnid, None)
    files.move(0, a.cnid, "B", fm.ROOT, None)                       # out of what held it
    assert b.parent is fs.boot.root
    refused(fm.fBsyErr, files.delete, 0, 0, "A")                    # G is still in it
    refused(fm.fnfErr, files.lookup_file, 0, 0, "A")                # a folder is no file
    # Exchanging: on one volume only.
    _, _, other = in_memory(MacFile("Doc", b"doc"))
    refused(fm.diffVolErr, files.exchange, f, other.lookup_file(0, 0, "Doc"))


def test_what_an_access_path_refuses(qapp):
    s = System()
    fs = s.files
    files = FileManager(fs, None, fs.boot.root)
    d = files.create(0, 0, "D", directory=True)
    f = files.create(0, 0, "F")
    reader = files.open(0, 0, "F", False, fm.fsRdPerm)
    writer = files.open(0, 0, "F", False, fm.fsRdWrPerm)
    refused(fm.paramErr, files.read, reader.refnum, -1)
    refused(fm.wrPermErr, files.set_eof, reader.refnum, 0)
    refused(fm.wrPermErr, files.allocate, reader.refnum, 10)
    refused(fm.paramErr, files.set_eof, writer.refnum, -1)
    assert files.allocate(writer.refnum, 1) == fm.BLOCK
    refused(fm.paramErr, fs.open_fork, f, False, 9, files)          # no such permission
    refused(fm.fnfErr, fs.open_fork, d, False, fm.fsRdPerm, files)  # a directory has no forks
    refused(fm.rfNumErr, files.close, 12345)
    paths = []
    with pytest.raises(FileError) as e:
        for _ in range(fm.MAX_FCBS):
            paths.append(files.open(0, 0, "F", False, fm.fsRdPerm))
    assert e.value.code == fm.tmfoErr and len(paths) < fm.MAX_FCBS - 2
    files.close_all()
    assert not fs.paths(f)


def test_working_directories_run_out_and_are_shared(qapp):
    s = System()
    fs = s.files
    files = FileManager(fs, None, fs.boot.root)
    other = FileManager(fs, None, fs.boot.root)
    d = files.create(0, 0, "D", directory=True)
    wd = files.open_wd(0, d.cnid, None, fm.ERIK)
    fs.share_wd(wd, other)
    fs.share_wd(fs.boot.vref, other)                    # a volume's number: nothing to share
    files.close_all()
    assert wd in fs.wds
    other.close_all()
    assert wd not in fs.wds
    with pytest.raises(FileError) as e:
        for procid in range(fm.MAX_WDS):
            files.open_wd(0, d.cnid, None, procid)
    assert e.value.code == fm.tmwdoErr
    files.close_all()
    assert list(fs.wds) == [fs.system_wd]
    # What GetWDInfo gives for a volume, and for the default directory.
    refused(fm.nsvErr, files.wd_info, 0, len(fs.wds) + 1)
    info = files.wd_info(fs.boot.vref)
    assert (info.refnum, info.volume, info.dirid, info.procid) == (fs.boot.vref, fs.boot,
                                                                   fm.ROOT, 0)
    info = files.wd_info(0)
    assert (info.refnum, info.dirid, info.procid) == (fs.boot.vref, fm.ROOT, 0)
    wd = files.open_wd(0, d.cnid, None, 1234)
    files.set_vol(wd, None)
    info = files.wd_info(0)
    assert (info.refnum, info.dirid, info.procid) == (wd, d.cnid, 1234)
    files.close_wd(fs.boot.vref)                        # a volume's: nothing to close
    refused(fm.nsvErr, files.close_wd, -12345)


def test_special_folders(qapp):
    s = System()
    fs = s.files
    files = FileManager(fs, None, fs.boot.root)
    assert files.find_folder(0, fm.kSystemFolderType, False) is fs.system_folder
    refused(fm.fnfErr, files.find_folder, 0, b"zzzz", True)
    refused(fm.fnfErr, files.find_folder, 0, b"font", False)    # not made unless asked
    files.create(0, fs.system_folder.cnid, "Fonts")
    refused(fm.fnfErr, files.find_folder, 0, b"font", True)     # a file has the name


def test_the_search_path_passes_over_a_system_folder_that_isnt_there(qapp):
    s, _, files = in_memory(MacFile("Doc", b"doc"))
    assert files.lookup_file(0, 0, "System", search=True).parent is s.files.system_folder
    s.files.boot.blessed = 9999
    refused(fm.fnfErr, files.lookup_file, 0, 0, "System", True)


def test_the_finder_beside_a_real_system_file_is_in_the_system_folder(qapp):
    """As on the disk the System file came from, so that a program finds it
    by name from its own folder: FinderColors reads and changes the colours
    of the Finder's Color menu in it."""
    v = machfs.Volume()
    v.name = "Disk"
    v["System Folder"] = machfs.Folder()
    system, finder = machfs.File(), machfs.File()
    system.type, system.creator, system.rsrc = b"ZSYS", b"MACS", FORK
    finder.type, finder.creator, finder.rsrc = b"FNDR", b"MACS", FORK
    v["System Folder"]["System"] = system
    v["System Folder"]["Finder"] = finder
    real = macfile.system_on(load_volume(v.write(800 * 1024, desktopdb=False, bootable=False)))
    s = System(system_file=real)
    files = FileManager(s.files, None, s.files.dir_for(MemoryFolder([MacFile("Doc", b"doc")])))
    node = files.lookup_file(0, 0, "Finder", search=True)
    assert node.parent is s.files.system_folder and node.type == b"FNDR"
    _, _, files = in_memory(MacFile("Doc", b"doc"))     # macrt's own System has none
    refused(fm.fnfErr, files.lookup_file, 0, 0, "Finder", True)


def test_an_unmounted_volume_is_no_default(qapp):
    """Nor does unmounting it mind a program having taken its VCB out of
    the queue itself."""
    s, _, files = in_memory(MacFile("Doc", b"doc"))
    fs = s.files
    vol = files.get_vol()[0]
    s.mem.w32(fs.boot.vcb, 0)                           # the boot volume's link to it
    fs.unmount(vol)
    assert s.mem.r32(lowmem.VCBQHdr + 2) == fs.boot.vcb
    refused(fm.nsvErr, files.lookup, 0, 0, "Doc")


def test_changing_a_disk_that_isnt_out_changes_nothing(qapp):
    s = System()
    fs = s.files
    one, two = (fs.mount_folder(VolumeFolder(floppy(n))) for n in ("One", "Two"))
    assert (one.drive, two.drive) == (1, 2)
    fs.change_disk(1)                                   # still mounted
    fs.unmount(one)
    fs.change_disk(1, one)                              # the one taken out
    assert (one.drive, two.drive) == (1, 2) and one in fs.offline
    fs.change_disk(1)
    assert (one.drive, two.drive) == (2, 1) and two in fs.offline


def test_files_the_store_has_nothing_for(qapp):
    """A file whose store has no forks for it has empty ones; one it has
    gives its sizes without its forks being read."""
    s = System()
    for store in (fm.Store(), fm.ImageStore(floppy("Disk"))):
        f = fm.File("Nothing")
        f.volume = fm.Volume("V", -9, 9, store)
        assert f.forks() == [b"", b""]
    node = s.files.in_system_folder(MacFile("Printer", b"pr", FORK))
    assert node.sizes() == (2, len(FORK)) and node._forks is None


def test_applications_of_the_same_name_from_nowhere(qapp):
    """Python applications have no folder: each is put at the top of the
    boot volume, under a name of its own."""
    s = System()
    fs = s.files
    one, two = MacFile("Tool", b"1"), MacFile("Tool", b"2")
    assert [fs.node_for(f).name for f in (one, two, one)] == ["Tool", "Tool 2", "Tool"]
    assert fs.macfile_of(fs.node_for(two)) is two
    assert fs.macfile_of(FileManager(fs, None, fs.boot.root).create(0, 0, "Made")) is None


def test_the_forms_a_host_file_is_in(qapp, tmp_path):
    """AppleSingle stays AppleSingle with its name inside, a bare resource
    fork stays one, BinHex is read only, and a resource fork with no data
    file beside it is a file with an empty data fork."""
    finder = b"TEXTttxt" + bytes(24)
    (tmp_path / "single.as").write_bytes(fm._apple_single(APPLE_SINGLE, {
        AS_DATA: b"data", AS_RSRC: FORK, AS_NAME: b"Single", AS_FINDER_INFO: finder,
        OTHER: b"kept"}))
    (tmp_path / "Res").write_bytes(FORK)
    (tmp_path / "Text.hqx").write_bytes(binhex("Hex", b"hex", b"", b"TEXT", b"ttxt"))
    (tmp_path / "._Lone").write_bytes(fm._apple_double_bytes({AS_RSRC: FORK}))
    (tmp_path / "%Exec").write_bytes(FORK)
    (tmp_path / "._Empty").write_bytes(b"")            # no resource fork: no file
    os.mkfifo(tmp_path / "pipe")
    s, files = on_host(tmp_path)
    assert [n.name for n in files.get_vol()[0].root.items()] == [
        "Exec", "Hex", "Lone", "Res", "Single"]
    assert [files.lookup_file(0, 0, n).ref.form for n in ("Single", "Res", "Hex", "Lone")] == [
        "applesingle", "fork", "binhex", "plain"]
    for name, rsrc, data in (("Single", False, b"DATA"), ("Res", True, build_fork([]))):
        p = files.open(0, 0, name, rsrc, fm.fsRdWrPerm)
        files.write(p.refnum, data, fm.fsFromStart, 0)
        files.set_eof(p.refnum, len(data))
        files.close(p.refnum)
    f = load_file(tmp_path / "single.as")
    assert fm._entries((tmp_path / "single.as").read_bytes())[OTHER] == b"kept"
    assert (f.name, f.data, f.rsrc, f.type) == ("Single", b"DATA", FORK, b"TEXT")
    assert (tmp_path / "Res").read_bytes() == build_fork([])
    refused(fm.permErr, files.open, 0, 0, "Hex", False, fm.fsRdWrPerm)
    for name in ("Lone", "Exec"):
        assert [bytes(x) for x in files.lookup_file(0, 0, name).forks()] == [b"", FORK]
    files.rename(0, 0, "Lone", "Alone")                 # only its resource fork to move
    assert not (tmp_path / "Alone").exists()
    assert load_file(tmp_path / "._Alone").rsrc == FORK


def test_a_host_folder_named_as_a_volume_already_mounted(qapp, tmp_path):
    """Its volume has a number after the name, so a full pathname of its
    own finds it, and one of the other volume's finds that; names are
    told apart as far as the 27 characters a VCB holds of them."""
    s = System()
    fs = s.files
    disk = fs.mount_folder(VolumeFolder(floppy("Shared")))
    for name in ("Shared", "L" * 27, "L" * 28):
        (tmp_path / name).mkdir()
    (tmp_path / "Shared" / "Good").write_bytes(b"good")
    files = FileManager(fs, None, fs.dir_for(HostFolder(tmp_path / "Shared")))
    host = fs.mount_folder(HostFolder(tmp_path / "Shared"))
    assert (disk.name, host.name) == ("Shared", "Shared 2")
    assert files.lookup(0, 0, "Shared 2:Good").volume is host
    assert files.lookup(0, 0, "Shared:File").volume is disk
    assert fs.mount_folder(HostFolder(tmp_path / "Shared")) is host
    assert fs.mount_folder(HostFolder(tmp_path / ("L" * 27))).name == "L" * 27
    assert fs.mount_folder(HostFolder(tmp_path / ("L" * 28))).name == "L" * 25 + " 2"


def test_host_folders(qapp, tmp_path, monkeypatch):
    """Junk the Mac doesn't see is cleared out of a folder deleted, but a
    file it doesn't know of stops it; a folder gone from the host since it
    was seen is empty; a file that can't be read is left out."""
    root = tmp_path / "shared"
    for d in ("Box", "Kept", "Later", "Sub"):
        (root / d).mkdir(parents=True)
    (root / "Box" / "._x").write_bytes(b"")
    (root / "Box" / ".DS_Store").write_bytes(b"")
    (root / "Kept" / ".gitkeep").write_bytes(b"")
    (root / "Sub" / "Tool").write_bytes(b"tool")
    (root / "Bad").write_bytes(b"bad")
    (root / "Good").write_bytes(b"good")
    (tmp_path / "outside").mkdir()
    real = fm.load_file

    def load(p, *args, **kw):
        if p.name == "Bad":
            raise ValueError("damaged")
        return real(p, *args, **kw)
    monkeypatch.setattr(fm, "load_file", load)
    s, files = on_host(root)
    fs = s.files
    vol = files.get_vol()[0]
    assert [n.name for n in vol.root.items()] == ["Box", "Good", "Kept", "Later", "Sub"]
    files.rename(0, 0, "Box", "Crate")
    assert (root / "Crate").is_dir() and not (root / "Box").exists()
    files.delete(0, 0, "Crate")
    assert not (root / "Crate").exists()
    refused(fm.ioErr, files.delete, 0, 0, "Kept")
    assert files.lookup(0, 0, "Kept").is_dir
    (root / "Later").rmdir()
    refused(fm.fnfErr, files.lookup, 0, 0, ":Later:Anything")
    refused(fm.ioErr, files.create, 0, 0, ":Later:Inner", True)
    # The directory a host folder inside it is, and the file there.
    top = HostFolder(root)
    tool = load_file(root / "Sub" / "Tool", HostFolder(root / "Sub", top))
    assert fs.node_for(tool).parent is fs.dir_for(tool.folder) is files.lookup(0, 0, "Sub")
    assert fs.macfile_of(fs.node_for(tool)).data == b"tool"
    (root / "Link").symlink_to(tmp_path / "outside")
    assert fs.dir_for(HostFolder(root / "Nowhere", top)) is None
    assert fs.dir_for(HostFolder(root / "Link", top)) is None

    def unreadable(*args, **kw):
        raise OSError("unreadable")
    monkeypatch.setattr(macfile, "load_file", unreadable)
    assert fs.macfile_of(fs.node_for(tool)) is None


def test_a_host_that_refuses_a_write(qapp, tmp_path):
    """A folder where the file was: the path is closed all the same, and
    the Finder information can't be written; nothing of the writes is
    left behind."""
    (tmp_path / "Notes").write_bytes(b"notes")
    s, files = on_host(tmp_path)
    node = files.lookup_file(0, 0, "Notes")
    p = files.open(0, 0, "Notes", False, fm.fsRdWrPerm)
    files.write(p.refnum, b"more")
    (tmp_path / "Notes").unlink()
    (tmp_path / "Notes").mkdir()
    files.close_all()
    assert p.refnum not in s.files.fcbs
    refused(fm.ioErr, files.metadata_changed, node)
    assert sorted(q.name for q in tmp_path.iterdir()) == ["Notes"]


def test_host_files_written_in_place_and_compared(tmp_path):
    """macOS's own resource fork is written in place, there being no
    temporary file to put there; a file that isn't there is no other's."""
    fork = tmp_path / "F" / "..namedfork"
    fork.mkdir(parents=True)
    fm._write(fork / "rsrc", b"rsrc")
    assert [q.name for q in fork.iterdir()] == ["rsrc"] and (fork / "rsrc").read_bytes() == b"rsrc"
    assert not fm._same(tmp_path / "missing", tmp_path / "F")
    assert fm._same(tmp_path / "F", tmp_path / "F" / "..namedfork" / "..")
    vol = fm.Volume("Gone", -9, 9, fm.HostStore(tmp_path / "gone"))
    made = vol.crdate
    vol.store.mount(vol)                                # its folder isn't there to date it
    assert vol.crdate == made and not vol.root.scanned
