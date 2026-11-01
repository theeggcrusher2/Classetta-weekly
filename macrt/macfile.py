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
    HFS disk images         raw or DiskCopy 4.2, through machfs; a CD's
                            also as raw 2352-byte sectors
    HFS Plus disk images    read here, into the same form as HFS ones
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
import mmap
import os
import struct
import time
import unicodedata
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
HFS_PLUS_SIGNATURES = (b"H+", b"HX")    # HFS Plus, and HFSX, its case-sensitive kind
MFS_SIGNATURE = b"\xd2\xd7"
VOLUME_SIGNATURES = (HFS_SIGNATURE, *HFS_PLUS_SIGNATURES, MFS_SIGNATURE)
MAC_1904 = 2082844800                   # seconds from 1904 to 1970
SECTOR, TAG = 512, 12                   # a floppy sector, and the tag the drive keeps with it
MAPPED = 16 << 20                       # an image this big is mapped, not read
CD_IMAGES = (".iso", ".cdr", ".toast")  # the names an image macrt mounts as a CD-ROM ends in
ISO_9660_AT = 16 * 2048 + 1             # the first volume descriptor's "CD001" (ECMA-119)
CD_SECTOR, CD_DATA = 2352, 2048         # a CD sector whole, and the data in it (ECMA-130)
CD_SYNC = b"\x00" + b"\xff" * 10 + b"\x00"  # what each whole sector starts with


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

    def __post_init__(self):
        if not self.key:
            self.key = self.name

    @property
    def is_application(self) -> bool:
        return self.type == b"APPL"

    def cut_short(self, fork: str) -> bool:
        """Whether the disk image it's on ends before all of the fork."""
        return False


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
        names = sorted(os.listdir(self.path))
        out = []
        for n in names:
            p = self.path / n
            if n.startswith((".", "%")) or not p.is_file():
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

    def cut_short(self, fork: str) -> bool:
        return isinstance(self._node, OnDisk) and self._node.cut_short(fork)

    data = property(lambda self: self._get("data"), lambda self, v: self._set("data", v))
    rsrc = property(lambda self: self._get("rsrc"), lambda self, v: self._set("rsrc", v))


def _from_machfs(volume: machfs.Volume, folder: VolumeFolder, name: str,
                 f: machfs.File) -> MacFile:
    key = ":".join((volume.name, *folder.path, name))
    return _VolumeFile(f, name, None, None, bytes(f.type), bytes(f.creator), f.flags, key, folder)


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


def _raw_cd(data: bytes) -> bool:
    """Whether it's a raw CD image, one that keeps each of the disc's
    2352-byte sectors whole: 12 bytes of sync, a 4-byte header whose last
    byte is the sector's mode, and then its data (ECMA-130)."""
    return len(data) >= CD_SECTOR and not len(data) % CD_SECTOR and data[:12] == CD_SYNC


def _cd_data(data: bytes) -> bytearray:
    """The data in each of a raw CD image's sectors, as the disc's driver
    reads it: 2048 bytes after the header in a mode 1 sector, and after
    8 more, the subheader, in a mode 2 one."""
    out = bytearray(len(data) // CD_SECTOR * CD_DATA)
    for n, i in enumerate(range(0, len(data), CD_SECTOR)):
        at = i + (16 if data[i + 15] == 1 else 24)
        out[n * CD_DATA:(n + 1) * CD_DATA] = data[at:at + CD_DATA]
    return out


def _sectors(data: bytes) -> tuple[bytes, bytes]:
    """The sectors a disk image holds, and their tags if it kept them,
    else b"": a DiskCopy 4.2 image has them after its sectors, and some
    raw images keep each with its sector, in 524-byte records."""
    if _raw_cd(data):
        return _cd_data(data), b""
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
    """Where the first HFS or HFS Plus partition in an Apple partition map
    (a hard disk or CD-ROM image) starts and ends, if data starts with one.

    The map starts at block 1 (Inside Macintosh: Devices, SCSI Manager),
    in blocks of the size block 0's driver descriptor map gives.  Some CD-ROM
    images have nothing in block 0; their map is read in 512-byte blocks."""
    if len(data) < 1024:
        return None
    if data[:2] == APM_SIGNATURE:
        sizes = dict.fromkeys((struct.unpack_from(">H", data, 2)[0], 512))
    elif data[512:514] == APM_ENTRY:
        sizes = {512: None}
    else:
        return None
    for size in sizes:
        if size < 512 or data[size:size + 2] != APM_ENTRY:
            continue
        count = struct.unpack_from(">I", data, size + 4)[0]
        for i in range(1, min(count, 256) + 1):
            entry = bytes(data[i * size:i * size + 80])
            if entry[:2] != APM_ENTRY or len(entry) < 80:
                break
            start, blocks = struct.unpack_from(">II", entry, 8)
            start, end = start * size, min((start + blocks) * size, len(data))
            if (entry[48:80].split(b"\0")[0] in (b"Apple_HFS", b"Apple_HFSX")
                    and data[start + 1024:start + 1026] in VOLUME_SIGNATURES[:-1]):
                return start, end
    return None


def _volume_start(data: bytes) -> tuple[int, bytes] | None:
    """Where the volume starts (its boot blocks, two before the MDB) and
    its signature."""
    for i in range(0, min(len(data), 64 * 512), 512):
        if data[i + 1024:i + 1026] in VOLUME_SIGNATURES:
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
    if moof.is_moof(data) or _raw_cd(data):     # load_volume says what's on a CD
        return True
    data = _sectors(data)[0]
    return (_hfs_partition(data) is not None or _volume_start(data) is not None
            or _iso_9660(data))


def _iso_9660(data: bytes) -> bool:
    """Whether it's an ISO 9660 disc: its volume descriptors start at the
    sixteenth 2048-byte sector, each with "CD001" after its type (ECMA-119)."""
    return bytes(data[ISO_9660_AT:ISO_9660_AT + 5]) == b"CD001"


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

    def _on_disk(self, fork: str) -> int:
        length, spans = self._forks[fork]
        n = len(self._disk)                 # an image cut short has less of it
        return min(length, sum(len(range(a, min(b, n))) for a, b in spans))

    def _length(self, fork: str) -> int:
        return len(self._read[fork]) if fork in self._read else self._on_disk(fork)

    def cut_short(self, fork: str) -> bool:
        """Whether the image ends before all of the fork: an image copied
        only in part."""
        return self._on_disk(fork) < self._forks[fork][0]

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


def _node(tree, n: int, size: int = 512) -> tuple[int, list[bytes]]:
    """A B*-tree node's forward link (ndFLink) and its records."""
    at = size * n
    link, count = struct.unpack_from(">L6xH", tree, at)
    offsets = struct.unpack_from(f">{count + 1}H", tree, at + size - 2 * (count + 1))[::-1]
    return link, [bytes(tree[at + a:at + b]) for a, b in zip(offsets, offsets[1:])]


def _leaf_records(tree, size: int = 512) -> Iterator[bytes]:
    """A B*-tree's leaf records in order, as machfs's dump_btree reads them:
    from bthFNode through each leaf's ndFLink to bthLNode.  But no node is
    read twice, so a damaged tree whose links go round is a damaged tree,
    not one read for ever.  An HFS tree's nodes are 512 bytes; an HFS Plus
    one's are the size its header node gives."""
    header = _node(tree, 0, size)[1][0]
    first, last = struct.unpack_from(">LL", header, 10)
    seen: set[int] = set()
    n = first
    while first:                                        # 0: no leaves
        if n in seen:
            raise ValueError("its B*-tree's leaves link round in a circle")
        seen.add(n)
        n_next, records = _node(tree, n, size)
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


def _embedded(volume) -> int | None:
    """Where the HFS Plus volume an HFS one holds starts in it, if it holds
    one: the HFS wrapper of Technote 1150, whose MDB names the embedded
    volume's signature (drEmbedSigWord) and its extent (drEmbedExtent)."""
    mdb = 1024
    if volume[mdb + 0x7C:mdb + 0x7E] != b"H+":
        return None
    block_size, = struct.unpack_from(">I", volume, mdb + 20)
    first, = struct.unpack_from(">H", volume, mdb + 28)
    start, = struct.unpack_from(">H", volume, mdb + 0x7E)
    return first * 512 + start * block_size


def _gmt_to_local(when: int) -> int:
    """An HFS Plus date, in GMT (TN1150's "HFS Plus Dates"), in the local
    time an HFS one is in."""
    if not when:
        return 0
    try:
        offset = time.localtime(when - MAC_1904).tm_gmtoff
    except (OverflowError, OSError, ValueError):
        offset = 0
    return (when + offset) & 0xFFFFFFFF


def _mac_name(name: str, cnid: int, folder: machfs.Folder, limit: int = 31) -> str:
    """An HFS Plus name, in Unicode, as the File Manager hands it to a
    program: Mac Roman, at most 31 characters.  One that won't go, or that
    another file or folder in its folder already has, is cut short and
    given its catalog node ID after a "#": macrt's own way to keep it
    apart, as the volume does."""
    name = unicodedata.normalize("NFC", name)
    try:
        mac = name.encode("mac_roman")
        fits = len(mac) <= limit and ":" not in name and name not in folder
    except UnicodeEncodeError:
        mac, fits = name.encode("mac_roman", "replace"), False
    if fits:
        return name
    tag = f"#{cnid:X}".encode()
    return (mac.replace(b":", b"?")[:limit - len(tag)] + tag).decode("mac_roman")


def _read_hfs_plus(disk) -> machfs.Volume:
    """An HFS Plus volume (Technote 1150, "HFS Plus Volume Format") as the
    HFS reader has one, so the rest of macrt can't tell them apart: its
    catalog's folders and files, their Finder information, dates (in GMT
    on the disk, in local time as HFS keeps them) and flags, each file's
    forks left on the disk until they're wanted, and its names as
    _mac_name has them.  A hard link is the file it links to, and the
    folder that keeps those, the metadata directory, isn't seen, as the
    Technote has it of an implementation that follows hard links."""
    vh = 1024
    crdate, mddate, bkdate = struct.unpack_from(">III", disk, vh + 16)
    block_size, = struct.unpack_from(">I", disk, vh + 40)
    if not block_size or block_size % 512:
        raise ValueError(f"its allocation blocks are {block_size} bytes")
    overflow: dict[tuple[int, int, int], bytes] = {}    # (fork type, file, block): extents

    def spans(length: int, extents: bytes, cnid: int, kind: int) -> list[tuple[int, int]]:
        """Where a fork's bytes are on the disk: its first eight extents',
        then the extents overflow file's."""
        out, blocks, wanted = [], 0, -(-length // block_size)
        looked: set[int] = set()
        while True:
            for i in range(0, 64, 8):
                start, count = struct.unpack_from(">LL", extents, i)
                if count:
                    out.append((start * block_size, (start + count) * block_size))
                    blocks += count
            if blocks >= wanted:
                return out
            if blocks in looked:                # an overflow record of no blocks
                raise ValueError(f"file {cnid}'s extents end at block {blocks}")
            looked.add(blocks)
            extents = overflow[kind, cnid, blocks]

    def fork(value: bytes, at: int, cnid: int, kind: int) -> tuple[int, list[tuple[int, int]]]:
        """A fork's length, and where its bytes are, from its HFSPlusForkData."""
        length, = struct.unpack_from(">Q", value, at)
        return length, spans(length, value[at + 16:at + 80], cnid, kind)

    def tree(at: int, cnid: int) -> tuple[bytes, int]:
        """One of the volume's own B-tree files, and its node size."""
        length, where = fork(bytes(disk[vh:vh + 512]), at, cnid, 0)
        data = b"".join(disk[a:b] for a, b in where)[:length]
        return data, struct.unpack_from(">H", data, 14 + 18)[0]

    for rec in _leaf_records(*tree(192, 3)):            # the extents overflow file
        klen, kind, cnid, start = struct.unpack_from(">HBxLL", rec)
        if klen >= 10 and kind in (0, 0xFF):            # a data fork's, a resource fork's
            overflow[kind, cnid, start] = rec[2 + klen:2 + klen + 64]
    cnids: dict[int, machfs.Folder | machfs.File] = {}
    children, links = [], []
    for rec in _leaf_records(*tree(272, 4)):            # the catalog
        klen, parent, length = struct.unpack_from(">HLH", rec)
        name = rec[8:8 + 2 * length].decode("utf-16-be", "replace")
        value = rec[(2 + klen + 1) & ~1:]
        kind = struct.unpack_from(">h", value)[0] if len(value) >= 2 else 0
        if kind not in (1, 2):                          # a thread
            continue
        flags, cnid, *dates = struct.unpack_from(">2xH4xLLL8xL", value)
        if kind == 1:                                   # a folder
            node = machfs.Folder()
        elif kind == 2:                                 # a file
            node = OnDisk(disk, {"data": fork(value, 88, cnid, 0),
                                 "rsrc": fork(value, 168, cnid, 0xFF)})
            node.type, node.creator, node.flags, v, h = struct.unpack_from(">4s4sHhh", value, 48)
            node.y, node.x = v, h
            node.locked = bool(flags & 1)               # its other flags aren't HFS's
            if (node.type, node.creator) == (b"hlnk", b"hfs+"):
                links.append((node, struct.unpack_from(">L", value, 44)[0]))
        node.crdate, node.mddate, node.bkdate = map(_gmt_to_local, dates)
        cnids[cnid] = node
        children.append((parent, name, cnid, node))
    hidden = next((cnid for parent, name, cnid, node in children if parent == 2
                   and name == "\0\0\0\0HFS+ Private Data"
                   and isinstance(node, machfs.Folder)), None)
    inodes = {name: node for parent, name, cnid, node in children if parent == hidden}
    targets = {id(link): inodes.get(f"iNode{n}", link) for link, n in links}
    vol_name = "Untitled"
    for parent, name, cnid, node in children:
        if parent == 1:                                 # the top folder: the volume's name
            vol_name = _mac_name(name, cnid, {}, 27)
        elif cnid != hidden and parent != hidden:
            folder = cnids[parent]
            folder[_mac_name(name, cnid, folder)] = targets.get(id(node), node)
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
    vol.crdate, vol.mddate, vol.bkdate = crdate, _gmt_to_local(mddate), _gmt_to_local(bkdate)
    vol.name = vol_name
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
        own = (isinstance(sectors, mmap.mmap)          # the disk's own already: not copied
               or sectors is not data and isinstance(sectors, bytearray))
        disk = Disk(sectors if own else bytearray(sectors), bytearray(tags))
        disk.tracks = moof.Formatted.of(disk.sectors, disk.tags)
    data = memoryview(disk.sectors)     # read through, not copied
    part = _hfs_partition(data)
    if part is not None:
        data = data[part[0]:part[1]]
    start = _volume_start(data)
    if part is None and start is None and _iso_9660(data):
        raise ValueError("it's an ISO 9660 disc, with no HFS, HFS Plus or MFS volume "
                         "on it for macrt to read")
    startup = _startup_name(bytes(data[start[0]:start[0] + 1024])) if start else None
    allocation = _allocation(data, start)
    if start and start[1] == MFS_SIGNATURE:
        vol = _read_mfs(bytes(data[start[0]:]), start[0] // SECTOR)
        vol.size, vol.startup, vol.disk, vol.allocation = len(data), startup, disk, allocation
        return vol
    try:
        if start and start[1] == HFS_SIGNATURE and _embedded(data[start[0]:]) is not None:
            start = start[0] + _embedded(data[start[0]:]), b"H+"
        if start and start[1] in HFS_PLUS_SIGNATURES:
            vol = _read_hfs_plus(data[start[0]:])
        else:
            vol = _read_hfs(data)
    except Exception as e:      # noqa: BLE001 - a damaged catalog trips over anything
        raise ValueError(f"the disk image is damaged ({type(e).__name__}: {e})") from None
    vol.size = len(data)                # the disk's: whether it's a floppy's
    vol.startup, vol.disk, vol.allocation = startup, disk, allocation
    return vol


def _allocation(data, start: tuple[int, bytes] | None) -> tuple[int, int, int] | None:
    """An MFS or HFS volume's allocation blocks, their size and how many are
    free, as its MDB has them (drNmAlBlks, drAlBlkSiz and drFreeBks, at the
    same places in both): an HFS Plus volume's wrapper's, if it has one."""
    if not start or start[1] not in (HFS_SIGNATURE, MFS_SIGNATURE):
        return None
    mdb = bytes(data[start[0] + 1024:start[0] + 1024 + 36])
    if len(mdb) < 36:
        return None
    (blocks, size), (free,) = struct.unpack_from(">HI", mdb, 18), struct.unpack_from(">H", mdb, 34)
    return (blocks, size, free) if size and not size % 512 else None


def volume_files(volume: machfs.Volume) -> Iterator[MacFile]:
    """Every file on the volume, folders included."""
    for path, node in volume.iter_paths():
        if isinstance(node, machfs.File):
            yield _from_machfs(volume, VolumeFolder(volume, path[:-1]), path[-1], node)


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
    """The kind of archive f is, if it's one: something to unpack first."""
    d = f.data
    if d[:4] == b"SIT!" and d[10:14] == b"rLau" or d[:16] == b"StuffIt (c)1997-":
        return "StuffIt"
    if d[:4] == b"PK\x03\x04":
        return "zip"
    return None


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


def open_disks(paths, names=()) -> tuple[list[MacFile], list[machfs.Volume]]:
    """The applications to launch, from several files, and the disk images
    among them, in the order they came, to be put in the drives.  Each file
    that isn't a disk image is an application; from the images, the names
    found on them (each name is looked for on the images in turn, and
    taken from the first that has it), or with no names each image's only
    application, unless an application file is among them, whose disks the
    images then are.  A name that's a document's is the application that made
    it (its creator), launched with it, as when it's opened in the Finder;
    documents of the same application are opened by one launch of it, and
    one whose creator is an application named before it is opened by that
    one, as the Finder hands it to the application running: HyperCard 2.4
    rather than the HyperCard Player beside it.  An
    image nothing is taken from, one with no application or, when there
    are names, none of them, is a disk of documents, in a drive for the
    applications to open: Beyond Dark Castle's second disk."""
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
    disks_only = len(images) < len(opened)  # an application file's disks, with no names
    i = 0
    for path, volume, f in opened:
        if volume is None:
            apps.append(f)
            continue
        if i in picked:
            apps += picked[i]
        elif not names and not disks_only and not _runs_nothing(volume):
            apps.append(_only_application(volume, path))
        i += 1
    if not apps and images:
        _only_application(images[0][1], images[0][0])     # says there's nothing on it to run
    return apps, [volume for _, volume in images]


def _volume_or_file(path: str | os.PathLike) -> tuple[machfs.Volume | None, MacFile | None]:
    path = Path(path)
    raw = _image(path) if path.exists() else b""          # app with only app.rsrc
    volume, problem = None, None
    if is_disk_image(raw):
        try:
            volume = load_volume(raw)
        except ValueError as e:
            problem = e
    if volume is not None:
        volume.cdrom = path.suffix.lower() in CD_IMAGES or _raw_cd(raw)
        return volume, None
    f = load_file(path)
    if problem is not None and not f.rsrc:
        raise ValueError(f"{path.name}: {problem}")     # an image, but one that can't be read
    kind = archive_kind(f)
    if kind is not None:
        raise ValueError(f"{path.name} is a {kind} archive: unpack it first, for example "
                         f"with The Unarchiver (unar {path.name})")
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


def finder_beside(system: MacFile | None) -> MacFile | None:
    """The Finder in the folder system, a disk's System file, is in."""
    folder = system.folder if system is not None else None
    finder = folder.find("Finder") if folder is not None else None
    return finder if finder is not None and finder.type == b"FNDR" else None


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
