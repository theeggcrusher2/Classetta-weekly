"""
Mac files on the host: both forks plus the Finder's type, creator and
flags, from whichever form they were kept in.

    MacBinary I/II/III      app.bin
    BinHex 4.0              app.hqx
    AppleSingle             one file, both forks
    AppleDouble             the data fork, with ._app, .AppleDouble/app or
                            app.rsrc (what The Unarchiver's unar writes)
    Basilisk II             app, with .rsrc/app and .finf/app
    Executor                app, with %app
    a bare resource fork    app.rsrc, or a file that is one
    macOS                   app/..namedfork/rsrc
    HFS disk images         raw or DiskCopy 4.2, through machfs
    MFS disk images         the 400K floppies of System 1 to 4, read here
    MOOF disk images        Applesauce's, of a floppy's tracks: macrt.moof

A Folder finds files by Mac name, case-insensitively, the way the File
Manager will: OpenResFile looks up names through the folder the
application came from.

open_application() is what the command line uses: any of the above, or
an application on a disk image; open_disks() several of them, and the
disk images among them, to be put in the drives.
"""

from __future__ import annotations

import binascii
import copy
import io
import mmap
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Protocol

import machfs
import machfs.main
from machfs.btree import unpack_extent_record

from . import moof
from .resfork import InvalidForkError, is_fork, open_fork

APPLE_SINGLE = 0x00051600
APPLE_DOUBLE = 0x00051607
AS_DATA, AS_RSRC, AS_NAME, AS_FINDER_INFO = 1, 2, 3, 9

BINHEX_BANNER = b"(This file must be converted with BinHex"
BINHEX_CHARS = b"!\"#$%&'()*+,-012345689@ABCDEFGHIJKLMNPQRSTUVXYZ[`abcdefhijklmpqr"
BINHEX_RUN = 0x90

DISKCOPY_HEADER = 84
APM_SIGNATURE = b"ER"                   # block 0 of a partitioned disk
APM_ENTRY = b"PM"                       # each partition map entry
HFS_SIGNATURE = b"BD"
MFS_SIGNATURE = b"\xd2\xd7"
SECTOR, TAG = 512, 12                   # a floppy sector, and the tag the drive keeps with it
MAPPED = 16 << 20                       # an image this big is mapped, not read


@dataclass(eq=False)
class MacFile:
    name: str
    data: bytes = b""
    rsrc: bytes = b""
    type: bytes = b"????"
    creator: bytes = b"????"
    flags: int = 0                      # Finder flags
    key: str = ""                       # identity: a host path or volume path
    folder: Folder | None = field(default=None, repr=False)
    # The documents it's launched with, as when they're opened in the
    # Finder: the Finder information it's given lists them.
    documents: tuple[MacFile, ...] = field(default=(), repr=False)
    crdate: int = 0                     # creation date (Mac epoch)
    mddate: int = 0                     # modification date (Mac epoch)

    def __post_init__(self):
        if not self.key:
            self.key = self.name

    @property
    def is_application(self) -> bool:
        return self.type == b"APPL"

    def sizes(self) -> tuple[int, int]:
        """Its data and resource forks' lengths."""
        return len(self.data), len(self.rsrc)

    def head(self, size: int = 65536) -> bytes:
        """Its data fork's first size bytes, for telling what it is
        without reading a big file whole."""
        return bytes(self.data[:size])

    def tail(self, size: int = 65536) -> bytes:
        """Its data fork's last size bytes, for finding a zip's end of
        central directory without reading the fork whole."""
        return bytes(self.data[-size:])


class Folder(Protocol):
    def find(self, name: str) -> MacFile | None: ...

    def files(self) -> list[MacFile]: ...


def _split_path(name: str) -> tuple[bool, list[str]]:
    """A Mac pathname: (absolute, components).  "Disk:Folder:File" is
    absolute, ":Folder:File" and "File" are relative."""
    if ":" not in name:
        return False, [name]
    absolute = not name.startswith(":")
    parts = [p for p in name.split(":") if p]
    return absolute, parts[1:] if absolute else parts


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------
class MemoryFolder:
    """Files held in Python; handy for tests and Python guests."""

    def __init__(self, files: list[MacFile] = ()):
        self._files: dict[str, MacFile] = {}
        for f in files:
            self.add(f)

    def add(self, f: MacFile) -> MacFile:
        self._files[f.name.lower()] = f
        f.folder = self
        return f

    def remove(self, f: MacFile) -> None:
        if self._files.get(f.name.lower()) is f:
            del self._files[f.name.lower()]

    def find(self, name: str) -> MacFile | None:
        parts = _split_path(name)[1]
        return self._files.get(parts[-1].lower()) if parts else None

    def files(self) -> list[MacFile]:
        return list(self._files.values())


class HostFolder:
    """A host directory.  Each Mac file may be spread over several host
    files; find() puts it back together."""

    def __init__(self, path: str | os.PathLike, root: HostFolder | None = None):
        self.path = Path(path)
        self.root = root or self

    def _entry(self, name: str) -> Path | None:
        try:
            names = os.listdir(self.path)
        except OSError:
            return None
        want = name.lower()
        hit = next((n for n in names if n == name), None) or \
            next((n for n in names if n.lower() == want), None)
        return self.path / hit if hit else None

    def find(self, name: str) -> MacFile | None:
        absolute, parts = _split_path(name)
        if not parts:
            return None
        folder = self.root if absolute else self
        for part in parts[:-1]:
            sub = folder._entry(part)
            if sub is None or not sub.is_dir():
                return None
            folder = HostFolder(sub, self.root)
        leaf = parts[-1]
        for candidate in (leaf, leaf + ".bin", leaf + ".rsrc"):
            p = folder._entry(candidate)
            if p is not None and p.is_file():
                return load_file(p, folder)
        # Only the resource fork is here, in one of the side files.
        for side in ("._" + leaf, "%" + leaf):
            p = folder._entry(side)
            if p is not None and p.is_file():
                return load_file(p, folder)
        return None

    def files(self) -> list[MacFile]:
        try:
            names = sorted(os.listdir(self.path))
        except OSError:
            return []
        out = []
        for n in names:
            p = self.path / n
            if n.startswith((".", "%")):
                continue
            if p.is_dir():
                sub_folder = HostFolder(p, self.root)
                out.append(MacFile(n, type=b"fldr", key=str(p), folder=sub_folder))
                continue
            if not p.is_file():
                continue
            if n.lower().endswith(".rsrc") and n[:-5] in names:
                continue                                # app's resource fork, beside app
            try:
                out.append(load_file(p, self))
            except ValueError:
                continue                                # damaged; the rest are still here
        return out


class VolumeFolder:
    """A folder on an HFS volume read by machfs."""

    def __init__(self, volume: machfs.Volume, path: tuple[str, ...] = ()):
        self.volume = volume
        self.path = path

    def _node(self, path: tuple[str, ...]):
        node = self.volume
        for part in path:
            node = node[part]
        return node

    @staticmethod
    def _child(folder, name: str) -> tuple[str, object]:
        """A folder's item and its name as the volume spells it, which is
        what keys and paths use whatever case it was asked for in."""
        node = folder[name]
        return next((n for n in folder if n.lower() == name.lower()), name), node

    def find(self, name: str) -> MacFile | None:
        absolute, parts = _split_path(name)
        if not parts:
            return None
        path = () if absolute else self.path
        try:
            folder = self._node(path)
            for part in parts[:-1]:
                stored, folder = self._child(folder, part)
                path += (stored,)
            stored, node = self._child(folder, parts[-1])
        except (KeyError, TypeError):
            return None
        if not isinstance(node, machfs.File):
            return None
        return _from_machfs(self.volume, VolumeFolder(self.volume, path), stored, node)

    def files(self) -> list[MacFile]:
        folder = self._node(self.path)
        return [_from_machfs(self.volume, self, n, f) for n, f in folder.items()
                if isinstance(f, machfs.File)]


class _VolumeFile(MacFile):
    """A file on a volume, its forks read from it the first time they're
    wanted: looking through a CD for one application reads no other's."""

    def __init__(self, node: machfs.File, *args, **kw):
        self._node, self._forks = node, {}
        super().__init__(*args, **kw)

    def _get(self, fork: str) -> bytes:
        if self._forks.get(fork) is None:
            self._forks[fork] = bytes(getattr(self._node, fork))
        return self._forks[fork]

    def _set(self, fork: str, value: bytes | None) -> None:
        self._forks[fork] = value                       # None: what's on the volume

    def sizes(self) -> tuple[int, int]:
        """Its forks' lengths, without reading them when the disk has them."""
        if hasattr(self._node, "sizes"):
            return self._node.sizes()
        return super().sizes()

    def tail(self, size: int = 65536) -> bytes:
        """Its data fork's last size bytes, reading only that much of
        the disk image it's on."""
        if isinstance(self._node, OnDisk):
            _length, spans = self._node._forks.get("data", (0, []))
            out = bytearray()
            for at, end in reversed(spans):
                want = size - len(out)
                if want <= 0:
                    break
                out = self._node._disk[max(at, end - want):end] + out
            return bytes(out[-size:])
        return super().tail(size)

    def head(self, size: int = 65536) -> bytes:
        """Its data fork's first size bytes, reading only that much of
        the disk image it's on."""
        if isinstance(self._node, OnDisk):
            _length, spans = self._node._forks.get("data", (0, []))
            out = bytearray()
            for at, end in spans:
                want = size - len(out)
                if want <= 0:
                    break
                out += self._node._disk[at:min(end, at + want)]
            return bytes(out[:size])
        return super().head(size)

    data = property(lambda self: self._get("data"), lambda self, v: self._set("data", v))
    rsrc = property(lambda self: self._get("rsrc"), lambda self, v: self._set("rsrc", v))


def _from_machfs(volume: machfs.Volume, folder: VolumeFolder, name: str,
                 f: machfs.File) -> MacFile:
    key = ":".join((volume.name, *folder.path, name))
    return _VolumeFile(f, name, None, None, bytes(f.type), bytes(f.creator), f.flags, key, folder,
                       crdate=getattr(f, 'crdate', 0), mddate=getattr(f, 'mddate', 0))


# ---------------------------------------------------------------------------
# Alias files
# ---------------------------------------------------------------------------
ALIAS_FLAG = 0x8000                  # the Finder's isAlias flag


def _alias_record(f: MacFile):
    """The alias record in f's resource fork, if it's one there."""
    rsrc = getattr(f, "rsrc", b"")
    if not rsrc:
        return None
    try:
        fork = open_fork(bytes(rsrc))
    except Exception:
        return None
    e = fork.find(b"alis", 0) or next((e for e in fork.entries if e.type == b"alis"), None)
    if e is None:
        return None
    try:
        from .toolbox import aliases as al
        return al.Alias.parse(fork.raw(e))
    except Exception:
        return None


def is_alias(f: MacFile) -> bool:
    """Whether f is an alias file: isAlias set, and an 'alis' resource."""
    return bool(f.flags & ALIAS_FLAG) and _alias_record(f) is not None


def resolve_alias(f: MacFile, fs):
    """Where an alias file's target is, as the Toolbox's Alias Manager
    finds it; None when f isn't an alias or its original can't be found."""
    from .toolbox import aliases as al
    record = _alias_record(f)
    if record is None or fs is None:
        return None
    try:
        return al.resolve(fs, record)
    except Exception:
        return None


def alias_original(f: MacFile, fs=None) -> str | None:
    """The Mac pathname of an alias file's original, if it can be said."""
    from .toolbox import aliases as al
    found = resolve_alias(f, fs)
    if found is None:
        return None
    if found.node is not None:
        return al.path_of(found.node)
    record = _alias_record(f)
    if record is not None and record.path:
        return record.path
    return f"{found.volume.name}:{found.name}"


def alias_open(f: MacFile, fs):
    """What opening an alias file opens: the file, or a volume and the
    path of the folder it points at — both None when its original can't
    be found."""
    found = resolve_alias(f, fs)
    if found is None or found.node is None:
        return None, None, ()
    node = found.node
    from .toolbox import files as fm
    from .toolbox import aliases as al
    if isinstance(node, (fm.File, fm.Dir)):
        # The Alias Manager resolved it to a mounted catalog node: a File's
        # own bytes are in its .ref (a machfs file a disk image was read
        # into, or a MacFile when the volume is a host folder), and a
        # Dir's is its name there.
        path = tuple(n for n in al.path_of(node).split(":")[1:] if n)
        if isinstance(node, fm.Dir):
            machfs_volume = _machfs_volume(found.volume)
            if machfs_volume is not None:
                return None, machfs_volume, path       # a folder on a disk image
            where = _host_path(found.volume, node)
            return ((None, HostFolder(where), ()) if where is not None
                    else (None, None, ()))
        ref = node.ref
        if isinstance(ref, MacFile):
            return ref, None, ()                      # a file on a host folder
        machfs_volume = _machfs_volume(found.volume)
        if machfs_volume is not None:
            folder = VolumeFolder(machfs_volume, path[:-1])
            return (_from_machfs(machfs_volume, folder, node.name, ref), None, ())
        return (MacFile(node.name, bytes(ref.data), bytes(ref.rsrc), bytes(ref.type),
                        bytes(ref.creator), ref.flags, node.name), None, ())
    if not isinstance(node, (machfs.File, machfs.Folder)):
        return None, None, ()

    def path_of(node) -> tuple[str, ...]:
        names = []
        while node.parent is not None:
            names.append(node.name)
            node = node.parent
        return tuple(reversed(names))

    if node.is_dir:
        return None, found.volume, path_of(node)
    return (_from_machfs(found.volume, VolumeFolder(found.volume, path_of(node.parent)),
                         node.name, node), None, ())


def _machfs_volume(volume):
    """The machfs volume a mounted one was read from, if it was one."""
    source = getattr(getattr(volume, "store", None), "source", None)
    return source if isinstance(source, machfs.Volume) else None


def _host_path(volume, node):
    """Where a mounted directory is on the host, if its volume's store
    keeps its files on the host."""
    store = getattr(volume, "store", None)
    try:
        return store.path(node)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Disk images
# ---------------------------------------------------------------------------
@dataclass(eq=False)
class Disk:
    """A disk image as its driver reads it, by sector rather than by file:
    each sector's 512 bytes, its 12-byte tag if the image kept them (none
    if it didn't), and what's on its tracks: a MOOF image's, or a floppy's
    as a Mac's format writes them, from a raw image of one."""
    sectors: bytearray | mmap.mmap
    tags: bytearray
    tracks: moof.Tracks | None = None

    def unreadable(self, sector: int) -> str | None:
        """Why the sector can't be read, None if it can."""
        return self.tracks.unreadable.get(sector) if self.tracks else None


def _sectors(data: bytes) -> tuple[bytes, bytes]:
    """The sectors a disk image holds, and their tags if it kept them,
    else b"": a DiskCopy 4.2 image has them after its sectors, and some
    raw images keep each with its sector, in 524-byte records."""
    if len(data) > DISKCOPY_HEADER and data[82:84] == b"\x01\x00" and data[0] < 64:
        size, tag_size = struct.unpack_from(">II", data, 64)
        if DISKCOPY_HEADER + size <= len(data):
            tags = data[DISKCOPY_HEADER + size:DISKCOPY_HEADER + size + tag_size]
            data = data[DISKCOPY_HEADER:DISKCOPY_HEADER + size]
            return data, tags if len(tags) == len(data) // SECTOR * TAG else b""
    record = SECTOR + TAG
    if not data or len(data) % record or _volume_start(data) is not None:
        return data, b""
    for skip in (0, TAG):                   # the tag after its sector, or before it
        if data[2 * record + skip:2 * record + skip + 2] in (HFS_SIGNATURE, MFS_SIGNATURE):
            tag = 0 if skip else SECTOR
            return (b"".join(data[i + skip:i + skip + SECTOR] for i in range(0, len(data), record)),
                    b"".join(data[i + tag:i + tag + TAG] for i in range(0, len(data), record)))
    return data, b""


def _hfs_partition(data: bytes) -> tuple[int, int] | None:
    """Where the first HFS partition in an Apple partition map (a hard disk
    or CD-ROM image) starts and ends, if data starts with one."""
    if data[:2] != APM_SIGNATURE or len(data) < 1024:
        return None
    for size in dict.fromkeys((struct.unpack_from(">H", data, 2)[0], 512)):
        if size < 512 or data[size:size + 2] != APM_ENTRY:
            continue
        count = struct.unpack_from(">I", data, size + 4)[0]
        for i in range(1, min(count, 256) + 1):
            entry = bytes(data[i * size:i * size + 80])
            if entry[:2] != APM_ENTRY or len(entry) < 80:
                break
            start, blocks = struct.unpack_from(">II", entry, 8)
            start, end = start * size, min((start + blocks) * size, len(data))
            if (entry[48:80].split(b"\0")[0] == b"Apple_HFS"
                    and data[start + 1024:start + 1026] in (HFS_SIGNATURE, b"H+", b"HX")):
                return start, end
    return None


def _volume_start(data: bytes) -> tuple[int, bytes] | None:
    """Where the volume starts (its boot blocks, two before the MDB) and
    its signature."""
    for i in range(0, min(len(data), 64 * 512), 512):
        if data[i + 1024:i + 1026] in (HFS_SIGNATURE, MFS_SIGNATURE, b"H+", b"HX"):
            return i, bytes(data[i + 1024:i + 1026])
    return None


def _read_mfs(data: bytes, first: int = 0) -> machfs.Volume:
    """An MFS volume as a machfs one, so the rest of macrt can't tell.

    MFS (Inside Macintosh II, "Data Organization on Volumes") has one flat
    directory: the folders a user sees exist only in the Finder's Desktop
    file, and the File Manager never saw them either, so every file goes at
    the top.  Forks are chains of allocation blocks, linked by the 12-bit
    entries of the block map that follows the MDB.  Each file's sectors
    are kept, its data fork's and its resource fork's, as numbers on the
    disk, the volume starting at sector first: the File Manager reads a
    fork through the disk driver, a sector at a time."""
    mdb = 1024
    if len(data) < mdb + 64:
        raise ValueError("the disk image is damaged (it ends inside the MDB)")
    (crdate, bkdate, _atrb, files, dir_start, dir_blocks, blocks, block_size, _clump,
     first_block) = struct.unpack_from(">IIHHHHHIIH", data, mdb + 2)
    if not block_size or block_size % 512 or not blocks:
        raise ValueError("the disk image is damaged (its MDB makes no sense)")
    vol = machfs.Volume()
    vol.name = data[mdb + 37:mdb + 37 + min(data[mdb + 36], 27)].decode("mac_roman")
    vol.crdate, vol.bkdate = crdate, bkdate

    def link(n: int) -> int:                # entry n of the block map; n counts from 2
        i = n - 2
        at = mdb + 64 + i * 3 // 2
        if at + 2 > len(data):
            return 1
        if i % 2 == 0:
            return data[at] << 4 | data[at + 1] >> 4
        return (data[at] & 0xF) << 8 | data[at + 1]

    def fork(start: int, length: int) -> tuple[bytearray, list[int]]:
        out, sectors = bytearray(), []
        n, seen = start, 0
        while 2 <= n < blocks + 2 and len(out) < length and seen <= blocks:
            at = (first_block * 512) + (n - 2) * block_size
            out += data[at:at + block_size]
            sectors += range(first + at // 512, first + (at + block_size) // 512)
            n, seen = link(n), seen + 1
        if len(out) < length:
            raise ValueError("the disk image is damaged (a file runs off its blocks)")
        return out[:length], sectors[:-(-length // 512)]

    for b in range(dir_start, dir_start + dir_blocks):
        at, end = b * 512, b * 512 + 512
        while at + 51 <= min(end, len(data)) and data[at] & 0x80:
            e = data[at:at + 51 + data[at + 50]]
            if at + len(e) > end:
                break
            f = machfs.File()
            f.type, f.creator = bytes(e[2:6]), bytes(e[6:10])
            f.flags, v, h = struct.unpack_from(">Hhh", e, 10)
            f.y, f.x = v, h
            f.locked = bool(e[0] & 1)
            f.attributes = e[0] & 0x7E      # bit 7 only says the entry is in use
            dstart, dlen, _, rstart, rlen, _, f.crdate, f.mddate = \
                struct.unpack_from(">HII" "HII" "II", e, 22)
            (f.data, data_sectors), (f.rsrc, rsrc_sectors) = fork(dstart, dlen), fork(rstart, rlen)
            f.sectors = data_sectors, rsrc_sectors
            name = e[51:].decode("mac_roman")
            if name and ":" not in name:
                vol[name] = f
            at += (len(e) + 1) & ~1
    if files and not len(vol):
        raise ValueError("the disk image is damaged (its directory is empty)")
    return vol


def _startup_name(boot: bytes) -> str | None:
    """The program a Mac starting up from the volume runs, named in its
    boot blocks (bbHelloName): the Finder, unless something took its place."""
    if boot[:2] != b"LK" or len(boot) < 106:
        return None
    return boot[91:91 + min(boot[90], 15)].decode("mac_roman") or None


def is_disk_image(data: bytes) -> bool:
    if moof.is_moof(data):
        return True
    data = _sectors(data)[0]
    return (_hfs_partition(data) is not None or _volume_start(data) is not None or 
            (len(data) > 16 * 2048 and data[16 * 2048:16 * 2048 + 6] == b'CD001\x01'))


def _link_aliases(vol_cr_date, cnids):
    """machfs's, leaving out alias files whose resource fork isn't one:
    machfs gives up on the whole volume over one, and The Secret of Monkey
    Island's disk has an alias in its Recent Applications whose resource
    fork stops short."""
    machfs.main._link_aliases(vol_cr_date, {
        cnid: f for cnid, f in cnids.items()
        if not isinstance(f, machfs.File) or not f.flags & 0x8000 or is_fork(f.rsrc)})


class OnDisk(machfs.File):
    """A file on an HFS disk image, its forks left on the disk until
    they're wanted: the thousands of files on a CD aren't all read when
    it's put in a drive, only the ones that are opened."""

    def __init__(self, disk, forks: dict[str, tuple[int, list[tuple[int, int]]]]):
        self._read: dict[str, bytes] = {}
        super().__init__()
        self._read.clear()                  # machfs's empty forks
        self._disk = disk                   # the volume's bytes
        self._forks = forks                 # each fork's length, and where its bytes are

    def _fork(self, fork: str) -> bytes:
        got = self._read.get(fork)
        if got is None:
            length, spans = self._forks[fork]
            got = self._read[fork] = b"".join(self._disk[a:b] for a, b in spans)[:length]
        return got

    def _length(self, fork: str) -> int:
        if fork in self._read:
            return len(self._read[fork])
        length, spans = self._forks[fork]
        n = len(self._disk)                 # an image cut short has less of it
        return min(length, sum(len(range(a, min(b, n))) for a, b in spans))

    def sizes(self) -> tuple[int, int]:
        """Its forks' lengths, without reading them."""
        return self._length("data"), self._length("rsrc")

    @property
    def data(self) -> bytes:
        return self._fork("data")

    @data.setter
    def data(self, value: bytes) -> None:
        self._read["data"] = value

    @property
    def rsrc(self) -> bytes:
        return self._fork("rsrc")

    @rsrc.setter
    def rsrc(self, value: bytes) -> None:
        self._read["rsrc"] = value


def _node(tree, n: int) -> tuple[int, list[bytes]]:
    """A B*-tree node's forward link (ndFLink) and its records."""
    at = 512 * n
    link, count = struct.unpack_from(">L6xH", tree, at)
    offsets = struct.unpack_from(f">{count + 1}H", tree, at + 512 - 2 * (count + 1))[::-1]
    return link, [bytes(tree[at + a:at + b]) for a, b in zip(offsets, offsets[1:])]


def _leaf_records(tree) -> Iterator[bytes]:
    """A B*-tree's leaf records in order, as machfs's dump_btree reads them:
    from bthFNode through each leaf's ndFLink to bthLNode.  But no node is
    read twice, so a damaged tree whose links go round is a damaged tree,
    not one read for ever."""
    header = _node(tree, 0)[1][0]
    first, last = struct.unpack_from(">LL", header, 10)
    seen: set[int] = set()
    n = first
    while first:                                        # 0: no leaves
        if n in seen:
            raise ValueError("its B*-tree's leaves link round in a circle")
        seen.add(n)
        n_next, records = _node(tree, n)
        yield from records
        if n == last:
            return
        n = n_next


def _read_hfs(data) -> machfs.Volume:
    """An HFS volume (Inside Macintosh IV, the File Manager, "Data
    Organization on Volumes") as machfs reads one, without its Desktop
    files, but with each file's forks left on the disk until they're
    wanted (OnDisk), its location in its folder as the MFS reader has it,
    and its flags (filFlags, which machfs reads and drops) kept as the MFS
    reader keeps its directory entry's: bit 0 is locked, and the File
    Manager reports the rest in ioFlAttrib all the same.  Microsoft File's
    master disk sets bit 6 on two files, and File looks for it there."""
    at = next((i for i in range(0, len(data), 512)
               if data[i + 1024:i + 1026] == HFS_SIGNATURE), None)
    if at is None:
        raise ValueError("it has no MDB")
    disk, mdb = data[at:], 1024             # the volume, from its boot blocks
    crdate, mddate = struct.unpack_from(">II", disk, mdb + 2)
    block_size, = struct.unpack_from(">I", disk, mdb + 20)
    first, = struct.unpack_from(">H", disk, mdb + 28)
    bkdate, = struct.unpack_from(">I", disk, mdb + 64)
    overflow: dict[tuple[int, int, int], bytes] = {}    # (fork type, file, block): extents

    def spans(length: int, extents: bytes, cnid: int, kind: int) -> list[tuple[int, int]]:
        """Where a fork's bytes are on the disk: its first extents', then
        the extents overflow file's."""
        out, blocks, wanted = [], 0, -(-length // block_size)
        looked: set[int] = set()
        while True:
            for start, count in unpack_extent_record(extents):
                where = first * 512 + start * block_size
                out.append((where, where + count * block_size))
                blocks += count
            if blocks >= wanted:
                return out
            if blocks in looked:                # an overflow record of no blocks
                raise ValueError(f"file {cnid}'s extents end at block {blocks}")
            looked.add(blocks)
            extents = overflow[kind, cnid, blocks]

    def fork(at: int, cnid: int) -> bytes:  # one of the volume's own files, named in the MDB
        length, extents = struct.unpack_from(">I12s", disk, mdb + at)
        return b"".join(disk[a:b] for a, b in spans(length, extents, cnid, 0))[:length]

    for rec in _leaf_records(fork(130, 3)):             # the extents overflow file
        if rec[0] == 7:
            kind, cnid, start, extents = struct.unpack_from(">xBLH12s", rec)
            if kind in (0, 0xFF):                       # a data fork's, a resource fork's
                overflow[kind, cnid, start] = extents
    cnids: dict[int, machfs.Folder | machfs.File] = {}
    children = []
    for rec in _leaf_records(fork(146, 4)):             # the catalog
        if not rec[0]:
            continue
        parent, length = struct.unpack_from(">LB", rec, 2)
        name, value = rec[7:min(7 + length, 1 + rec[0])], rec[(rec[0] + 2) & ~1:]
        if value[:1] == b"\x01":                        # a folder
            cnid, *dates = struct.unpack_from(">LLLL", value, 6)
            node = machfs.Folder()
        elif value[:1] == b"\x02":                      # a file
            flags, info, cnid, dlen, rlen, *dates, dext, rext = \
                struct.unpack_from(">xxBx16sL2xL4x2xL4xLLL16x2x12s12s", value)
            node = OnDisk(disk, {"data": (dlen, spans(dlen, dext, cnid, 0)),
                                 "rsrc": (rlen, spans(rlen, rext, cnid, 0xFF))})
            node.type, node.creator, node.flags, v, h = struct.unpack_from(">4s4sHhh", info)
            node.y, node.x = v, h
            node.locked, node.attributes = bool(flags & 1), flags & 0x7E
        else:                                           # a thread
            continue
        node.crdate, node.mddate, node.bkdate = dates
        cnids[cnid] = node
        children.append((parent, name, node))
    for parent, name, node in children:
        if parent != 1:
            cnids[parent][name] = node
    seen, todo = set(), [cnids[2]]
    while todo:                                         # a folder that is in itself?
        folder = todo.pop()
        if id(folder) in seen:
            raise ValueError("a folder on it is inside itself")
        seen.add(id(folder))
        todo.extend(v for v in folder.values() if isinstance(v, machfs.Folder))
    vol = machfs.Volume()
    vol.update(cnids[2])
    for name in ("Desktop", "Desktop DB", "Desktop DF"):
        vol.pop(name, None)
    _link_aliases(crdate, cnids)
    vol.crdate, vol.mddate, vol.bkdate = crdate, mddate, bkdate
    vol.name = bytes(disk[mdb + 37:mdb + 37 + min(disk[mdb + 36], 27)]).decode("mac_roman")
    return vol


def _image(path: Path) -> bytes | mmap.mmap:
    """A disk image's bytes.  A big one, a hard disk's or a CD's, is mapped
    copy-on-write rather than read: only what's looked at is in memory, and
    what the disk driver writes stays there, as it does in a copy read."""
    with path.open("rb") as f:
        size = os.fstat(f.fileno()).st_size
        if size < MAPPED:
            return f.read()
        return mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_COPY)


def load_volume(source: str | os.PathLike | bytes | mmap.mmap) -> machfs.Volume:
    """The volume on a disk image; its disk, for the disk driver, is its
    .disk."""
    data = source if isinstance(source, (bytes, bytearray, mmap.mmap)) else _image(Path(source))
    if moof.is_moof(data):
        tracks = moof.Disk(bytes(data))
        disk = Disk(bytearray(tracks.image), bytearray(tracks.tags), tracks)
    else:
        sectors, tags = _sectors(data)
        disk = Disk(sectors if isinstance(sectors, mmap.mmap) else bytearray(sectors),
                    bytearray(tags))
        disk.tracks = moof.Formatted.of(disk.sectors, disk.tags)
    data = memoryview(disk.sectors)     # read through, not copied
    part = _hfs_partition(data)
    if part is not None:
        data = data[part[0]:part[1]]
    start = _volume_start(data)
    startup = _startup_name(bytes(data[start[0]:start[0] + 1024])) if start else None
    if start and start[1] in (b"H+", b"HX"):
        from .hfsplus import read_hfsplus
        vol = read_hfsplus(data[start[0]:] if start[0] else data)
        vol.size, vol.startup, vol.disk = len(data), startup, disk
        return vol
    if start and start[1] == MFS_SIGNATURE:
        vol = _read_mfs(bytes(data[start[0]:]), start[0] // SECTOR)
        vol.size, vol.startup, vol.disk = len(data), startup, disk
        return vol
    stem = Path(source).stem if not isinstance(source, (bytes, bytearray, mmap.mmap)) else "CDROM"
    try:
        vol = _read_hfs(data)
    except Exception as hfs_e:
        try:
            from disktracker.index import _read_iso
            vol_name, cr, md, root = _read_iso(bytes(data), stem)
            vol = machfs.Volume()
            vol.name = vol_name
            def populate(node, folder):
                for child in node['children']:
                    if child['kind'] == 'folder':
                        sub = machfs.Folder()
                        folder[child['name']] = sub
                        populate(child, sub)
                    else:
                        f = machfs.File()
                        f.data = bytes(data[child['lba'] * 2048 : child['lba'] * 2048 + child['size']])
                        f.type, f.creator = child.get('type', b'????'), child.get('creator', b'????')
                        f.crdate, f.mddate = child.get('crdate', 0), child.get('mddate', 0)
                        folder[child['name']] = f
            populate(root, vol)
        except Exception as e2:
            raise ValueError(f"the disk image is damaged ({type(hfs_e).__name__}: {hfs_e})") from None
    vol.size = len(data)                # the disk's: whether it's a floppy's
    vol.startup, vol.disk = startup, disk
    return vol


def volume_files(volume: machfs.Volume) -> Iterator[MacFile]:
    """Every file on the volume, folders included."""
    for path, node in volume.iter_paths():
        if isinstance(node, machfs.File):
            yield _from_machfs(volume, VolumeFolder(volume, path[:-1]), path[-1], node)


def list_folder(volume: machfs.Volume | HostFolder | ContainerFolder | ArchiveFolder,
                path: tuple[str, ...] = ()) -> list[MacFile]:
    """One folder's contents, as the Finder's window lists them: its
    files, and its subfolders as MacFiles of type 'fldr' with no forks,
    each keeping its place in the folder it's in."""
    if isinstance(volume, HostFolder):
        folder = volume
        for part in path:
            sub = folder._entry(part)
            if sub is None or not sub.is_dir():
                return []
            folder = HostFolder(sub, folder.root)
        return folder.files()
    if isinstance(volume, (ContainerFolder, ArchiveFolder)):
        return volume.files()            # flat: the decoded file / the archive's entries
    folder = VolumeFolder(volume, path)
    node = folder._node(folder.path)
    out = []
    for name, child in node.items():
        if isinstance(child, machfs.Folder):
            key = ":".join((volume.name, *folder.path, name))
            out.append(MacFile(name, type=b"fldr", key=key, folder=folder))
        else:
            out.append(_from_machfs(volume, folder, name, child))
    return out


# ---------------------------------------------------------------------------
# Single files
# ---------------------------------------------------------------------------
def _parse_macbinary(data: bytes) -> MacFile | None:
    if len(data) < 128 or data[0] != 0 or data[74] != 0 or not 1 <= data[1] <= 63:
        return None
    if data[:3] == b"\x00\x05\x16":     # AppleSingle/Double can pass for MacBinary I
        return None
    h = data[:128]
    data_len, rsrc_len = struct.unpack_from(">II", h, 83)
    if max(data_len, rsrc_len) >= 0x1000000:
        return None
    crc_ok = binascii.crc_hqx(h[:124], 0) == struct.unpack_from(">H", h, 124)[0]
    if not crc_ok and (h[82] != 0 or any(h[99:126])):
        return None                                     # not MacBinary I either
    if not crc_ok and (b":" in h[2:2 + h[1]] or min(h[2:2 + h[1]]) < 0x20):
        return None                                     # no Mac name, so not MacBinary I
    sec_len = struct.unpack_from(">H", h, 120)[0] if crc_ok else 0
    pad = lambda n: (n + 127) & ~127                    # noqa: E731
    data_at = 128 + pad(sec_len)
    rsrc_at = data_at + pad(data_len)
    if rsrc_at + rsrc_len > len(data):
        if data_at + data_len + rsrc_len == len(data):
            rsrc_at = data_at + data_len                # the data fork wasn't padded
        elif crc_ok:
            raise ValueError("MacBinary file is cut short")
        else:
            return None
    if not crc_ok and len(data) > rsrc_at + pad(rsrc_len) + 1024:
        return None                                     # far too long for what it says it holds
    flags = h[73] << 8 | (h[101] if crc_ok else 0)
    name = h[2:2 + h[1]].decode("mac_roman")
    return MacFile(name, data[data_at:data_at + data_len], data[rsrc_at:rsrc_at + rsrc_len],
                   h[65:69], h[69:73], flags)


def _parse_binhex(data: bytes) -> MacFile | None:
    """A BinHex 4.0 file: six bits a character, then run-length encoding,
    then a header and the two forks, each with a CRC."""
    banner = data.find(BINHEX_BANNER, 0, 4096)
    if banner < 0:
        return None
    start = data.find(b":", banner)                     # the banner has no colon
    end = data.find(b":", start + 1)
    if start < 0 or end < 0:
        raise ValueError("BinHex file is cut short")
    table = {c: i for i, c in enumerate(BINHEX_CHARS)}
    packed, bits, nbits = bytearray(), 0, 0
    for c in data[start + 1:end]:
        if c in b" \t\r\n":
            continue
        if c not in table:
            raise ValueError(f"BinHex file has a stray {chr(c)!r}")
        bits, nbits = bits << 6 | table[c], nbits + 6
        if nbits >= 8:
            nbits -= 8
            packed.append(bits >> nbits & 0xFF)
            bits &= (1 << nbits) - 1                    # or it grows with the file
    out, i, last = bytearray(), 0, 0
    while i < len(packed):
        c = packed[i]
        i += 1
        if c != BINHEX_RUN:
            out.append(c)
            last = c
        elif i < len(packed):
            n = packed[i]
            i += 1
            if n == 0:
                out.append(BINHEX_RUN)
                last = BINHEX_RUN
            else:
                out += bytes([last]) * (n - 1)

    def take(at: int, n: int) -> tuple[bytes, int]:
        body = bytes(out[at:at + n])
        crc = struct.unpack_from(">H", out, at + n)[0] if at + n + 2 <= len(out) else None
        if len(body) < n or crc != binascii.crc_hqx(body, 0):
            raise ValueError("BinHex file is damaged (bad CRC)")
        return body, at + n + 2

    if not out:
        raise ValueError("BinHex file is empty")
    n = out[0]
    header, at = take(0, 1 + n + 1 + 4 + 4 + 2 + 4 + 4)
    name = header[1:1 + n].decode("mac_roman")
    ftype, creator = header[n + 2:n + 6], header[n + 6:n + 10]
    flags, data_len, rsrc_len = struct.unpack_from(">HII", header, n + 10)
    fork_data, at = take(at, data_len)
    fork_rsrc, at = take(at, rsrc_len)
    return MacFile(name, fork_data, fork_rsrc, ftype, creator, flags)


def _parse_apple_single(data: bytes) -> tuple[int, dict[int, bytes]] | None:
    if len(data) < 26:
        return None
    magic, = struct.unpack_from(">I", data, 0)
    if magic not in (APPLE_SINGLE, APPLE_DOUBLE):
        return None
    n, = struct.unpack_from(">H", data, 24)
    if 26 + 12 * n > len(data):
        raise ValueError("AppleSingle/AppleDouble file is cut short")
    entries = {}
    for i in range(n):
        eid, off, length = struct.unpack_from(">III", data, 26 + 12 * i)
        if off + length > len(data):
            raise ValueError("AppleSingle/AppleDouble file is cut short")
        entries[eid] = data[off:off + length]
    return magic, entries


def _apply_finder_info(f: MacFile, info: bytes) -> None:
    if len(info) >= 10:
        f.type, f.creator = info[0:4], info[4:8]
        f.flags = struct.unpack_from(">H", info, 8)[0]


def _host_finder_info(path: Path) -> bytes:
    try:
        return os.getxattr(path, "com.apple.FinderInfo")
    except (AttributeError, OSError):
        return b""


def _side_files(path: Path) -> list[tuple[Path, str | None]]:
    """Where path's resource fork may be, and in what form: None for a
    bare fork, "double" for AppleDouble, "either" for whichever it is."""
    name = path.name
    return [(path / "..namedfork" / "rsrc", None),
            (path.with_name("._" + name), "double"),
            (path.parent / ".AppleDouble" / name, "double"),
            (path.with_name(name + ".rsrc"), "either"),
            (path.parent / ".rsrc" / name, None),
            (path.with_name("%" + name), "either")]


def load_file(path: str | os.PathLike, folder: Folder | None = None, *,
              links: bool = True) -> MacFile:
    """One Mac file from the host, whatever form it's in.  path may also
    name one of the side files (._app, %app, app.rsrc).  With links False,
    no side file is read through a symbolic link: one may lead anywhere."""
    path = Path(path)
    folder = folder or HostFolder(path.parent)
    name = path.name
    top = path.parent.resolve()

    def readable(p: Path) -> bool:
        return links or (not p.is_symlink() and p.resolve().is_relative_to(top))
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        # An empty data fork: unar writes only app.rsrc for most applications.
        if not any(side.is_file() for side, _ in _side_files(path)):
            raise
        raw = b""

    mb = _parse_macbinary(raw) or _parse_binhex(raw)
    if mb is not None:
        mb.key, mb.folder = str(path), folder
        return mb

    single = _parse_apple_single(raw)
    if single is not None:
        magic, entries = single
        if magic == APPLE_DOUBLE:                       # the header for a data file
            if name.startswith("._"):
                name = name[2:]
            elif name.startswith("%"):
                name = name[1:]
            elif name.lower().endswith(".rsrc"):
                name = name[:-5]
            data_path = path.with_name(name)
            data = (data_path.read_bytes() if data_path != path and data_path.is_file()
                    and readable(data_path) else b"")
            path = data_path
        else:
            data = entries.get(AS_DATA, b"")
        real = entries.get(AS_NAME)
        f = MacFile(real.decode("mac_roman") if real else name, data, entries.get(AS_RSRC, b""),
                    key=str(path), folder=folder)
        _apply_finder_info(f, entries.get(AS_FINDER_INFO, b""))
        return f

    if name.startswith("%"):                            # Executor's resource fork
        data_path = path.with_name(name[1:])
        data = data_path.read_bytes() if data_path.is_file() and readable(data_path) else b""
        return MacFile(name[1:], data, raw, key=str(data_path), folder=folder)

    f = MacFile(name, raw, key=str(path), folder=folder)
    for side_path, kind in _side_files(path):
        try:
            if not readable(side_path):
                continue
            blob = side_path.read_bytes()
            parsed = _parse_apple_single(blob) if kind else None
        except (OSError, ValueError):
            continue                                    # missing, or a damaged side file
        if kind == "either":
            kind = "double" if parsed else None
        if kind == "double":
            if parsed is None:
                continue
            f.rsrc = parsed[1].get(AS_RSRC, b"")
            _apply_finder_info(f, parsed[1].get(AS_FINDER_INFO, b""))
        else:
            f.rsrc = blob
        if f.rsrc:
            break
    finf = path.parent / ".finf" / name                 # Basilisk II's Finder info
    if finf.is_file() and readable(finf):
        _apply_finder_info(f, finf.read_bytes())
    elif f.type == b"????":
        _apply_finder_info(f, _host_finder_info(path))

    if not f.rsrc and name.lower().endswith(".rsrc") and is_fork(raw):
        f.name, f.data, f.rsrc = name[:-5], b"", raw    # a bare resource fork
    elif not f.rsrc and is_fork(raw):
        f.data, f.rsrc = b"", raw
    return f


# ---------------------------------------------------------------------------
# What the command line opens
# ---------------------------------------------------------------------------
def archive_kind(f: MacFile) -> str | None:
    """The kind of archive f is, if it's one: something to unpack first.
    Any format in macextract's registry counts — add one there and it
    is recognised (and browsable) here with no change."""
    d = getattr(f, "data", b"")
    if d[:4] == b"SIT!" and d[10:14] == b"rLau" or d[:16] == b"StuffIt (c)1997-":
        return "StuffIt"
    if d[:4] == b"PK\x03\x04":
        return "zip"
    if not d:
        return None
    try:
        from macextract import formats
    except ImportError:
        return None
    for module in formats.FORMATS:
        try:
            if module.probe(d):
                return "archive"
        except Exception:
            continue
    return None


# Each macextract module's name for itself, by its module name: the Kind
# column's archive names.  Containers (MacBinary, BinHex, AppleSingle and
# the text encodings) are left out: they hold one file, they aren't
# archives to unpack.
_ARCHIVE_KINDS = {
    "abk": "Apple Backup", "arc": "ARC", "bzip2": "bzip2", "cpt": "Compact Pro",
    "dd": "DiskDoubler", "gzip": "gzip", "lha": "LHA", "mar": "MAR",
    "now": "Now Compress", "packit": "PackIt", "segments": "split archive",
    "sit": "StuffIt", "sit5": "StuffIt 5", "sqp": "AppleLink package",
    "tar": "tar", "tome": "Apple Installer Tome", "z": "Unix compress", "xz": "xz",
    "zip": "ZIP",
}


def archive_name(f: MacFile) -> str | None:
    """The name of the archive f is, if it's one macextract knows: the
    Kind column's word for it, without opening it."""
    d = f.head()
    if not d:
        return None
    if d[:4] == b"PK\x03\x04":
        # A bare PK header isn't enough to call it a zip: the end of
        # central directory at the file's end is what says it's one.
        return "ZIP" if _eocd_in(f.tail(65536)) else None
    if d[:4] == b"SIT!" and d[10:14] == b"rLau" or d[:16] == b"StuffIt (c)1997-":
        return "StuffIt"
    return archive_name_on(d)


def image_kind(f: MacFile) -> str | None:
    """The name of the disk image f is, if it's one macrt opens: from its
    first half-megabyte, so a big image isn't read whole for a listing."""
    if getattr(f, "name", "").lower().endswith((".dtc", ".dt1")):
        return "Disk Tracker catalog"
    d = f.head(524296)
    if not d:
        return None
    if moof.is_moof(d):
        return "MOOF disk image"
    if len(d) > DISKCOPY_HEADER and d[82:84] == b"\x01\x00" and d[0] < 64:
        return "DiskCopy 4.2 image"
    if _hfs_partition(d) is not None:
        return "Apple partition map"
    start = _volume_start(d)
    if start is not None:
        signature = start[1]
        if signature == HFS_SIGNATURE:
            return "HFS disk image"
        if signature == MFS_SIGNATURE:
            return "MFS disk image"
        return "HFS+ disk image"
    if len(d) > 16 * 2048 and d[16 * 2048:16 * 2048 + 6] == b"CD001\x01":
        return "CD-ROM image"
    sectors = _sectors(d)[0]
    if sectors is not d and _volume_start(sectors) is not None:
        return "Mac disk image"
    return None


def _container_kind(raw: bytes) -> str | None:
    """Which encoding raw is, if it's one load_file decodes — MacBinary,
    BinHex or AppleSingle — else None for plain bytes.  A container that's
    there but damaged raises, as its parser does."""
    if _parse_macbinary(raw) is not None:
        return "MacBinary"
    if _parse_binhex(raw) is not None:
        return "BinHex"
    if _parse_apple_single(raw) is not None:
        return "AppleSingle"
    return None


def container_kind(path: str | os.PathLike) -> str | None:
    """The encoding at path, when load_file decodes it as one — MacBinary,
    BinHex or AppleSingle — else None for a plain file.  Damaged or
    unreadable reads as None too: load_file already said what was wrong."""
    try:
        return _container_kind(Path(path).read_bytes())
    except (OSError, ValueError):
        return None


def container_contents(data: bytes, name: str, folder: Folder | None = None) -> MacFile | None:
    """The file an encoded container's bytes hold — MacBinary, BinHex or
    AppleSingle — else None when data isn't one of those.  A container
    that's there but damaged raises, as load_file's parsers do."""
    mb = _parse_macbinary(data) or _parse_binhex(data)
    if mb is not None:
        mb.folder = folder
        return mb
    single = _parse_apple_single(data)
    if single is None:
        return None
    magic, entries = single
    if magic == APPLE_DOUBLE:             # a sidecar header: no data fork of its own
        real = entries.get(AS_NAME)
        return MacFile(real.decode("mac_roman") if real else name,
                       b"", entries.get(AS_RSRC, b""), folder=folder)
    real = entries.get(AS_NAME)
    f = MacFile(real.decode("mac_roman") if real else name,
                entries.get(AS_DATA, b""), entries.get(AS_RSRC, b""), folder=folder)
    _apply_finder_info(f, entries.get(AS_FINDER_INFO, b""))
    return f


class ContainerFolder:
    """The contents of an encoded container — MacBinary, BinHex,
    AppleSingle — as the folder holding the one Mac file it decodes to,
    for a Filer window to list."""

    def __init__(self, contents: MacFile, name: str):
        self.contents = contents
        self.name = name                      # the container's own name, for the title

    def files(self) -> list[MacFile]:
        return [self.contents]

    def find(self, name: str) -> MacFile | None:
        return self.contents if self.contents.name.lower() == name.lower() else None


class ArchiveFolder:
    """The contents of a macextract-recognised archive, as the flat
    folder holding its entries — for a Filer window to list.  raw keeps
    the archive's own bytes, so Extract… can re-read it with its macextract
    format, Finder info included."""

    def __init__(self, entries: list[MacFile], name: str, raw: bytes):
        self.entries = entries
        self.name = name                      # the archive's own name, for the title
        self.raw = raw

    def files(self) -> list[MacFile]:
        return self.entries

    def find(self, name: str) -> MacFile | None:
        folded = name.lower()
        return next((f for f in self.entries if f.name.lower() == folded), None)


def archive_entries(data: bytes, name: str = "archive") -> list[MacFile] | None:
    """macextract's entries as MacFiles — one per file in the archive —
    or None when macextract does not recognise the bytes.  Recognised
    but damaged raises ValueError, as the other parsers do; an archive
    that wants a password says so the same way."""
    if not data:
        return None
    try:
        import macextract
    except ImportError:
        return None
    try:
        archive = macextract.open(io.BytesIO(data))
    except macextract.UnsupportedError:
        return None
    except macextract.PasswordRequiredError:
        raise ValueError(f"{name} needs a password, which the Filer cannot ask for yet")
    except macextract.BadPasswordError:
        raise ValueError(f"{name}: the password did not open it")
    except macextract.MacExtractError as e:
        raise ValueError(f"{name}: {e}")
    out: list[MacFile] = []
    for entry in archive.entries:
        out.append(MacFile(entry.name,
                           archive.read(entry, "data"),
                           archive.read(entry, "rsrc"),
                           entry.type, entry.creator,
                           flags=entry.finder_flags,
                           crdate=entry.created, mddate=entry.modified))
    return out


def extract_archive(raw: bytes, destination, fork_mode: str = "appledouble") -> list[str]:
    """Extract an archive's contents under destination, macextract-style:
    data fork to <name>, and a macOS-style ._<name> AppleDouble beside it
    when there is a resource fork or Finder info.  Returns the paths
    written; raises ValueError with a clear message when the archive is
    damaged or wants a password."""
    try:
        import macextract
    except ImportError:
        raise ValueError("the macextract.py library is not installed")
    try:
        with macextract.open(io.BytesIO(raw)) as archive:
            return [str(p) for p in archive.extract(destination, fork_mode=fork_mode)]
    except macextract.UnsupportedError:
        raise ValueError("macextract does not recognise this archive")
    except macextract.PasswordRequiredError:
        raise ValueError("the archive needs a password, which the Filer cannot ask for yet")
    except macextract.BadPasswordError:
        raise ValueError("the password did not open the archive")
    except macextract.MacExtractError as e:
        raise ValueError(str(e))


def open_application(path: str | os.PathLike, name: str | None = None) -> MacFile:
    """The Mac file at path, in any form load_file reads.  If path is a disk
    image, the application on it called name (or with that Mac path), or
    the only application on it."""
    volume, f = _volume_or_file(path)
    if volume is None:
        return f
    if name is None:
        return _only_application(volume, path)
    found = _find_on(volume, name)
    if found is None:
        raise ValueError(f"nothing called {name!r} on {Path(path).name}")
    return found


def open_applications(paths, names=()) -> list[MacFile]:
    return open_disks(paths, names)[0]


def open_disks(paths, names=(), *, browse: bool = False) -> tuple[list[MacFile], list[machfs.Volume]]:
    """The applications to launch, from several files, and the disk images
    among them, in the order they came, to be put in the drives.  Each file
    that isn't a disk image is an application; from the images, the names
    found on them (each name is looked for on the images in turn, and
    taken from the first that has it), or with no names each image's only
    application.  A name that's a document's is the application that made
    it (its creator), launched with it, as when it's opened in the Finder;
    documents of the same application are opened by one launch of it, and
    one whose creator is an application named before it is opened by that
    one, as the Finder hands it to the application running: HyperCard 2.4
    rather than the HyperCard Player beside it.  An
    image nothing is taken from, one with no application or, when there
    are names, none of them, is a disk of documents, in a drive for the
    applications to open: Beyond Dark Castle's second disk.

    With browse and no names, nothing is taken from the images: they are
    the caller's to show, in Filer windows, and only the files that
    aren't images are applications here."""
    opened = [(Path(p), *_volume_or_file(p)) for p in paths]
    images = [(path, volume) for path, volume, _ in opened if volume is not None]
    if names and not images:
        raise ValueError("--app picks an application on a disk image, and there's none here")
    where = ", ".join(path.name for path, _ in images)
    picked: dict[int, list[MacFile]] = {}
    for name in names:
        for i, (_, volume) in enumerate(images):
            found = _find_on(volume, name)
            if found is not None:
                break
        else:
            raise ValueError(f"nothing called {name!r} on {where}")
        if found.type == b"cdev" and not is_control_panel(found):
            raise ValueError(f"{name!r} is a control panel with no code of its own "
                             "('cdev' -4064) for macrt to run")
        if _is_document(found):
            doc = found
            running = next((f for apps in picked.values() for f in apps
                            if f.creator == doc.creator), None)
            if running is not None:
                running.documents += (doc,)
                continue
            # Its creator, on the disk it's on first.
            for i, (_, volume) in [(i, images[i]), *enumerate(images)]:
                found = _creator_on(volume, doc.creator)
                if found is not None:
                    break
            else:
                raise ValueError(f"{name!r} is a document, and no application on {where} "
                                 f"made it (its creator is {_ostype(doc.creator)})")
            found.documents = (doc,)
        picked.setdefault(i, []).append(found)
    apps: list[MacFile] = []
    i = 0
    for path, volume, f in opened:
        if volume is None:
            apps.append(f)
            continue
        if i in picked:
            apps += picked[i]
        elif not names and not browse and not _runs_nothing(volume):
            apps.append(_only_application(volume, path))
        i += 1
    if not apps and images and not browse:
        _only_application(images[0][1], images[0][0])     # says there's nothing on it to run
    return apps, [volume for _, volume in images]


def launch_target(f: MacFile) -> MacFile:
    """What the Finder's opening f launches: f itself, if it's a program
    (an application, a control panel with code of its own, or a program
    in the Finder's place), or the application that made a document, with
    the document to open.  ValueError if nothing here opens it."""
    if not _is_document(f):
        return f
    volume = f.folder.volume if isinstance(f.folder, VolumeFolder) else None
    made = _creator_on(volume, f.creator) if volume is not None else None
    if made is None:
        where = volume.name if volume is not None else "here"
        raise ValueError(f"{f.name} is a document, and no application on {where} "
                         f"made it (its creator is {_ostype(f.creator)})")
    made = copy.copy(made)
    made.documents = (f,)
    return made


def open_container(path: str | os.PathLike) -> machfs.Volume:
    """Open a recognized container (.dtc, .dt1, disk image) into a machfs.Volume,
    raising ValueError with a clear message for unsupported formats or invalid input."""
    vol, f = _volume_or_file(path)
    if vol is not None:
        return vol
    raise ValueError(f"{Path(path).name}: not a recognized volume container")


def archive_file(path: str | os.PathLike) -> ArchiveFolder | None:
    """A Filer window's worth of an archive file on the host, or None when
    it isn't one macextract reads.  Only the head of the file — and the
    end, for a zip's central directory — is read to tell."""
    path = Path(path)
    try:
        with path.open("rb") as f:
            head = f.read(65536)
            f.seek(0, os.SEEK_END)
            size = f.tell()
            tail = f.read() if size <= 65536 else f.read()[-65536:]
    except OSError:
        return None
    if is_disk_image(head):
        return None                               # a volume, not an archive
    d = head if size <= 65536 else head + tail      # a small file, whole
    if not d:
        return None
    if not (archive_name_on(d) or (d[:4] == b"PK\x03\x04" and _eocd_in(tail or head))):
        return None
    with path.open("rb") as f:
        raw = f.read()
    entries = archive_entries(raw, path.name)
    return None if entries is None else ArchiveFolder(entries, path.name, raw)


def _eocd_in(tail: bytes) -> bool:
    """Whether a zip's end-of-central-directory record is in its last bytes."""
    at = tail.rfind(b"PK\x05\x06")
    return at >= 0 and len(tail) - at >= 22


def archive_name_on(d: bytes) -> str | None:
    """archive_name, on bytes already read."""
    if not d:
        return None
    try:
        from macextract import formats
    except ImportError:
        return None
    for module in formats.FORMATS:
        name = module.__name__.rsplit(".", 1)[-1]
        kind = _ARCHIVE_KINDS.get(name)
        if kind is None:
            continue
        try:
            if module.probe(d):
                return kind
        except Exception:
            continue
    return None


def _volume_or_file(path: str | os.PathLike) -> tuple[machfs.Volume | None, MacFile | None]:
    path = Path(path)
    if path.suffix.lower() in ('.dtc', '.dt1'):
        try:
            from disktracker.filer_adapter import DiskTrackerVolume
            return DiskTrackerVolume(path), None
        except Exception as e:
            raise ValueError(f"{path.name}: could not load Disk Tracker catalog: {e}")
    raw = _image(path) if path.exists() else b""          # app with only app.rsrc
    volume, problem = None, None
    if is_disk_image(raw):
        try:
            volume = load_volume(raw)
        except ValueError as e:
            problem = e
    if volume is not None:
        return volume, None
    f = load_file(path)
    if problem is not None and not f.rsrc:
        raise ValueError(f"{path.name}: {problem}")     # an image, but one that can't be read
    kind = archive_kind(f)
    if kind in ("StuffIt", "zip"):
        raise ValueError(f"{path.name} is a {kind} archive: unpack it first, for example "
                         f"with The Unarchiver (unar {path.name})")
    # Archive formats macextract recognises pass through: the Filer can
    # browse them, and an attempt to launch one is refused downstream.
    return None, f


def _find_on(volume: machfs.Volume, name: str) -> MacFile | None:
    """The file on the volume called name or with that Mac path,
    applications first, and of those the one nearest the top of the disk."""
    files = sorted(volume_files(volume), key=lambda f: f.key.count(":"))
    want = name.lower()
    for f in [f for f in files if f.is_application] + files:
        if want in (f.name.lower(), f.key.lower()) or f.key.lower().endswith(":" + want):
            return f
    return None


def is_control_panel(f: MacFile) -> bool:
    """A control panel with code of its own, its 'cdev' -4064 (System
    7.5's Labels and Views have none)."""
    if f.type != b"cdev":
        return False
    try:
        return open_fork(f.rsrc).find(b"cdev", -4064) is not None
    except InvalidForkError:
        return False


def _is_document(f: MacFile) -> bool:
    """Not an application, nor a program of another type (a Finder, a
    control panel)."""
    if f.is_application or is_control_panel(f):
        return False
    try:
        return open_fork(f.rsrc).find(b"CODE", 0) is None
    except InvalidForkError:
        return True


def _creator_on(volume: machfs.Volume, creator: bytes) -> MacFile | None:
    """The application on the volume with that creator, nearest the top."""
    apps = [f for f in volume_files(volume) if f.is_application and f.creator == creator]
    return min(apps, key=lambda f: f.key.count(":"), default=None)


def _ostype(code: bytes) -> str:
    return "'" + code.decode("mac_roman") + "'"


def _only_application(volume: machfs.Volume, image: str | os.PathLike) -> MacFile:
    """What the disk starts up into, if it isn't Apple's Finder, else its
    only application: Mac Attack's disk has the game in the Finder's
    place, and an application for starting it on a Lisa."""
    startup = _startup_program(volume)
    if startup is not None:
        return startup
    apps = [f for f in volume_files(volume) if f.is_application]
    if len(apps) == 1:
        return apps[0]
    listing = "\n".join(f"  {f.key}" for f in apps) or "  (none)"
    raise ValueError(f"{Path(image).name} holds {len(apps)} applications; pick one with "
                     f"--app:\n{listing}")


def _runs_nothing(volume: machfs.Volume) -> bool:
    return (not any(f.is_application for f in volume_files(volume))
            and _startup_program(volume) is None)


def system_on(volume: machfs.Volume) -> MacFile | None:
    """The disk's System file, the one nearest the top of the disk if it
    has more than one, or None.  System 7's is a 'zsys', earlier ones a
    'ZSYS'."""
    found = [f for f in volume_files(volume)
             if f.name == "System" and f.type in (b"ZSYS", b"zsys")]
    return min(found, key=lambda f: f.key.count(":"), default=None)


def fonts_beside(system: MacFile | None) -> list[MacFile]:
    """The font suitcases in the Fonts folder of the folder system, a
    disk's System file, is in: System 7.5's Chicago 12 is in one there,
    not in the System file."""
    folder = system.folder if system is not None else None
    if not isinstance(folder, VolumeFolder):
        return []
    try:
        stored, node = folder._child(folder._node(folder.path), "Fonts")
    except (KeyError, TypeError):
        return []
    if isinstance(node, machfs.File):
        return []
    return [f for f in VolumeFolder(folder.volume, folder.path + (stored,)).files()
            if f.type == b"FFIL"]


def _startup_program(volume: machfs.Volume) -> MacFile | None:
    """The program the disk starts up into, if it isn't Apple's Finder.
    Some early games took the Finder's place on their disk, so that a Mac
    started from it went straight into the game.  Apple's Finder has its
    signature resource, FNDR or (from System 6) MACS; they don't."""
    name = getattr(volume, "startup", None)
    f = _find_on(volume, name) if name else None
    if f is None:
        return None
    try:
        types = {e.type for e in open_fork(f.rsrc).entries}
    except InvalidForkError:
        return None
    if b"CODE" not in types or types & {b"FNDR", b"MACS"}:
        return None
    return f
