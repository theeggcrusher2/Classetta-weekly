"""Index disk images into DiskTracker .dtc catalogs.

Accepts a list of disk images — an HFS volume (raw, Apple-partitioned,
or a hybrid), an HFS Plus volume, or an ISO 9660 image — reads each
one's directory tree, and emits the pre-order item list a .dtc
catalog holds, so write_dtc can turn it into a catalog indexing every
image's contents.

Standard library only: the library stays reusable outside this
project.  MFS volumes are recognized and refused: their folders live
in the Finder's Desktop file, not in the File Manager's catalog.
"""

import struct
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import machfs

from macrt.iso9660 import _read_iso
from macrt.hfsplus import read_hfsplus
# macrt.macfile._read_hfs, not the tracker's copy above (task 4.4 deletes
# that one): the canonical reader, imported under an alias so it does not
# clash with the local definition until 4.4 lands.
from macrt.macfile import _read_hfs as _macrt_read_hfs

from .disktracker import HEADER_SIZE, Item, _decode_mac_roman
from .writer import write_dtc

ISO_SECTOR = 2048
MAC_EPOCH = 2082844800       # seconds from 1904-01-01 to 1970-01-01


# ---------------------------------------------------------------------------
# HFS (Inside Macintosh IV, the File Manager)
# ---------------------------------------------------------------------------
def _find_hfs(data: bytes) -> int | None:
    """The image offset of the first plausible HFS volume, None if it
    has none: a scan for the MDB's 'BD' whose fields then hold up."""
    for at in range(0, max(0, len(data) - 1026), 512):
        if data[at + 1024:at + 1026] != b'BD':
            continue
        blocks, = struct.unpack_from('>H', data, at + 1042)      # drNmAlBlks
        block_size, = struct.unpack_from('>I', data, at + 1044)  # drAlBlkSiz
        if not 512 <= block_size <= 1 << 20 or block_size % 512 or not blocks:
            continue
        catalog_len, = struct.unpack_from('>I', data, at + 1170)  # drCTFlSize
        if 0 < catalog_len <= len(data) and data[at + 1060] <= 27:
            return at
    return None


def _extent_pairs(rec: bytes) -> Iterator[tuple[int, int]]:
    """An extent record's (start block, block count) pairs."""
    vals = struct.unpack('>6H', rec[:12])
    return iter(zip(vals[0::2], vals[1::2]))


def _read_hfs(data: bytes, at: int):
    """The volume at the image's offset at: its name, its dates, and
    its root folder as a tree of dicts."""
    disk, mdb = data[at:], 1024
    crdate, mddate = struct.unpack_from('>II', disk, mdb + 2)
    block_size, = struct.unpack_from('>I', disk, mdb + 20)
    first, = struct.unpack_from('>H', disk, mdb + 28)
    nlen = disk[mdb + 36]
    name = _decode_mac_roman(bytes(disk[mdb + 37:mdb + 37 + min(nlen, 27)]))
    overflow: dict[tuple[int, int, int], bytes] = {}

    def spans(length: int, extents: bytes, cnid: int, kind: int) -> list[tuple[int, int]]:
        """Where a fork's bytes are: its first extents', then the
        extents overflow file's."""
        out, blocks, wanted = [], 0, -(-length // block_size)
        looked: set[int] = set()
        while True:
            for start, count in _extent_pairs(extents):
                where = first * 512 + start * block_size
                out.append((where, where + count * block_size))
                blocks += count
            if blocks >= wanted:
                return out
            if blocks in looked:
                raise ValueError(f"file {cnid}'s extents end at block {blocks}")
            looked.add(blocks)
            extents = overflow[kind, cnid, blocks]

    def fork(at_off: int, cnid: int) -> bytes:  # a volume file named in the MDB
        length, extents = struct.unpack_from('>I12s', disk, mdb + at_off)
        return b"".join(disk[a:b] for a, b in spans(length, extents, cnid, 0))[:length]

    for rec in _records(fork(130, 3)):         # the extents overflow file
        if rec[0] == 7:
            kind, cnid, start, extents = struct.unpack_from('>xBLH12s', rec)
            if kind in (0, 0xFF):              # a data fork's, a resource fork's
                overflow[kind, cnid, start] = extents
    cnids: dict[int, dict] = {}
    order: list[tuple[int, dict]] = []
    for rec in _records(fork(146, 4)):         # the catalog
        if not rec[0]:
            continue
        parent, length = struct.unpack_from('>LB', rec, 2)
        name_b = rec[7:min(7 + length, 1 + rec[0])]
        value = rec[(rec[0] + 2) & ~1:]
        if value[:1] == b'\x01':               # a folder
            cnid, cdate, mdate, _bk = struct.unpack_from('>LLLL', value, 6)
            node = {'kind': 'folder', 'name': _decode_mac_roman(bytes(name_b)),
                    'crdate': cdate, 'mddate': mdate, 'children': []}
        elif value[:1] == b'\x02':             # a file
            _flags, info, cnid, dlen, rlen, *dates, _dext, _rext = \
                struct.unpack_from(">xxBx16sL2xL4x2xL4xLLL16x2x12s12s", value)
            node = {'kind': 'file', 'name': _decode_mac_roman(bytes(name_b)),
                    'type': bytes(info[:4]), 'creator': bytes(info[4:8]),
                    'size': dlen + rlen, 'crdate': dates[0], 'mddate': dates[1],
                    'children': []}
        else:                                  # a thread: names its folder
            continue
        cnids[cnid] = node
        order.append((parent, node))
    for parent, node in order:
        if parent == 1:                        # the volume itself
            continue
        holder = cnids.get(parent)
        if holder is not None and holder['kind'] == 'folder':
            holder['children'].append(node)
    root = cnids.get(2)
    if root is None:
        raise ValueError("the HFS volume's catalog has no root folder")
    return name, crdate, mddate, root


# ---------------------------------------------------------------------------
# HFS Plus ('H+'/'HX' in the volume header at offset 1024)
# ---------------------------------------------------------------------------
def _find_hfsplus(data: bytes) -> int | None:
    """The image offset of the first plausible HFS Plus volume, None if
    it has none: a scan for 'H+' or 'HX' whose fields then hold up."""
    for at in range(0, max(0, len(data) - 1026), 512):
        if data[at + 1024:at + 1026] not in (b'H+', b'HX'):
            continue
        version, = struct.unpack_from('>H', data, at + 1026)
        block_size, total_blocks = struct.unpack_from('>II', data, at + 1024 + 40)
        catalog_len = struct.unpack_from('>Q', data, at + 1024 + 272)[0]
        if version not in (4, 5):
            continue
        if not 512 <= block_size <= 1 << 16 or block_size % 512 or not total_blocks:
            continue
        if catalog_len > len(data) - at:
            continue
        return at
    return None


def _hfsplus_node(tree: bytes, n: int, node_size: int) -> tuple[int, int, list[bytes]]:
    """A B-tree node's forward link, its kind (0: index, 1: header,
    2: map, -1: leaf) and its records."""
    at = n * node_size
    if at + node_size > len(tree):
        raise ValueError("its B-tree's node's links reach past its end")
    kind = struct.unpack_from('>b', tree, at + 8)[0]
    count = struct.unpack_from('>H', tree, at + 10)[0]
    link = struct.unpack_from('>I', tree, at)[0]
    table = at + node_size - 2 * (count + 1)
    if table < at + 14:
        raise ValueError("its B-tree's node's record table reaches into its descriptor")
    offsets = struct.unpack_from(f'>{count + 1}H', tree, table)[::-1]
    if any(a >= b or b > node_size for a, b in zip(offsets, offsets[1:])):
        raise ValueError("its B-tree's node's records are out of order")
    return link, kind, [bytes(tree[at + a:at + b]) for a, b in zip(offsets, offsets[1:])]


def _hfsplus_leaf_records(tree: bytes) -> Iterator[bytes]:
    """A B-tree's leaf records in order, from its first leaf node through
    each node's forward link to its last.  A damaged tree whose links go
    round is a damaged tree, not one read for ever."""
    if len(tree) < 14 + 20:
        raise ValueError("its B-tree is too small to hold a header node")
    if struct.unpack_from('>b', tree, 8)[0] != 1:
        raise ValueError("its B-tree's first node is not its header node")
    first, last = struct.unpack_from('>II', tree, 14 + 10)
    node_size, = struct.unpack_from('>H', tree, 14 + 18)
    if not 512 <= node_size <= 32768 or node_size & 511:
        raise ValueError(f"its B-tree's node size ({node_size}) is not one")
    nodes = len(tree) // node_size
    if first >= nodes or last >= nodes:
        raise ValueError("its B-tree's first or last leaf node does not exist")
    seen: set[int] = set()
    n = first
    while n:
        if n in seen:
            raise ValueError("its B-tree's leaves link round in a circle")
        seen.add(n)
        link, kind, records = _hfsplus_node(tree, n, node_size)
        if kind != -1:
            raise ValueError("its B-tree's leaf link reaches a node that is not a leaf")
        yield from records
        if n == last:
            return
        n = link


def _hfsplus_spans(block_size: int, logical: int,
                   extents: list[tuple[int, int]],
                   overflow: dict[tuple[int, int, int], list[tuple[int, int]]],
                   cnid: int, kind: int) -> list[tuple[int, int]]:
    """Where a fork's bytes are: its inline extents', then the extents
    overflow file's."""
    wanted = -(-logical // block_size)
    out, all_extents, idx, blocks = [], list(extents), 0, 0
    while blocks < wanted:
        if idx < len(all_extents):
            start, count = all_extents[idx]
            idx += 1
            if count:
                out.append((start * block_size, (start + count) * block_size))
                blocks += count
            continue
        more = overflow.get((kind, cnid, blocks))
        if more is None:
            raise ValueError(
                f"its extents end at block {blocks}, but file {cnid} wants {wanted} blocks")
        if not any(count for _, count in more):
            raise ValueError(f"its extents overflow file has no blocks for file {cnid}")
        all_extents += more
    return out


def _hfsplus_fork_data(disk: bytes, fork_at: int) -> tuple[int, list[tuple[int, int]]]:
    """An HFSPlusForkData's logical size and its extents (block, count)."""
    at = fork_at
    logical = struct.unpack_from('>Q', disk, at)[0]
    extents = [e for e in (struct.unpack_from('>II', disk, at + 16 + i * 8)
                           for i in range(8)) if e[1]]
    return logical, extents


def _hfsplus_overflow(disk: bytes, block_size: int) -> dict:
    """The extents overflow file's records, as (fork type, file, start
    block) -> extents, read through its inline extents (it is tiny)."""
    logical, extents = _hfsplus_fork_data(disk, 1024 + 192)
    if not logical:
        return {}
    if sum(count for _, count in extents) * block_size < logical:
        raise ValueError("its extents overflow file spans past its inline extents")
    tree = b"".join(disk[a:b] for a, b in _hfsplus_spans(block_size, logical, extents,
                                                         {}, 3, 0))[:logical]
    overflow = {}
    for rec in _hfsplus_leaf_records(tree):
        key_length, = struct.unpack_from('>H', rec, 0)
        at = 2 + key_length
        if at + 64 > len(rec):
            continue
        fork_type = rec[2]
        cnid, = struct.unpack_from('>I', rec, 4)
        start, = struct.unpack_from('>I', rec, 8)
        inline = [struct.unpack_from('>II', rec, at + i * 8) for i in range(8)]
        overflow[fork_type, cnid, start] = [e for e in inline if e[1]]
    return overflow


def _hfsplus_text(raw: bytes) -> str:
    """An HFSUniStr255's text: UTF-16 big-endian, decomposed as the Mac
    wrote it, normalized to composed for display."""
    return unicodedata.normalize('NFC', raw.decode('utf-16-be'))


def _hfsplus_record(rec: bytes):
    """A catalog record's key's parent ID, its name, its type and where
    its data begins."""
    if len(rec) < 8:
        raise ValueError("its catalog holds a record too small for a key")
    key_length, parent, name_length = struct.unpack_from('>HIH', rec, 0)
    if 8 + 2 * name_length > len(rec):
        raise ValueError("its catalog holds a record whose name reaches past it")
    name = _hfsplus_text(rec[8:8 + 2 * name_length])
    at = 2 + key_length
    if at + 2 > len(rec):
        raise ValueError("its catalog holds a record whose key reaches past it")
    return parent, name, struct.unpack_from('>H', rec, at)[0], at


def _read_hfsplus(data: bytes, at: int):
    """The HFS Plus volume at the image's offset at: its name, its
    dates, and its root folder as a tree of dicts.  Entries the Finder
    marks invisible (the journal, Desktop files) are left out, so the
    catalog matches the Filer's view."""
    disk = data[at:]
    if len(disk) < 1024 + 512:
        raise ValueError("it is too small to hold an HFS Plus volume header")
    crdate, mddate = struct.unpack_from('>II', disk, 1024 + 16)
    block_size, total_blocks = struct.unpack_from('>II', disk, 1024 + 40)
    if not block_size or not total_blocks:
        raise ValueError("its HFS Plus volume header holds no blocks")
    overflow = _hfsplus_overflow(disk, block_size)
    logical, catalog_inline = _hfsplus_fork_data(disk, 1024 + 272)
    catalog = b"".join(disk[a:b] for a, b in _hfsplus_spans(block_size, logical, catalog_inline,
                                                            overflow, 4, 0))[:logical]
    cnids: dict[int, dict] = {}
    order: list[tuple[int, dict]] = []
    volume_name: str | None = None
    for rec in _hfsplus_leaf_records(catalog):
        try:
            parent, name, record_type, rec_at = _hfsplus_record(rec)
        except (UnicodeDecodeError, struct.error, ValueError) as e:
            raise ValueError(f"its catalog holds a record it cannot read ({e})") from None
        if record_type in (3, 4):              # threads: the root's names the volume
            if parent == 2:
                length, = struct.unpack_from('>H', rec, rec_at + 8)
                volume_name = _hfsplus_text(rec[rec_at + 10:rec_at + 10 + 2 * length])
            continue
        fdflags, = struct.unpack_from('>H', rec, rec_at + 56)
        if fdflags & 0x4000:                   # invisible: the journal and friends
            continue
        cnid, = struct.unpack_from('>I', rec, rec_at + 8)
        cdate, mdate = struct.unpack_from('>II', rec, rec_at + 12)
        if record_type == 1:                   # a folder
            node = {'kind': 'folder', 'name': name,
                    'crdate': cdate, 'mddate': mdate, 'children': []}
        elif record_type == 2:                 # a file
            ftype, fcreator = bytes(rec[rec_at + 48:rec_at + 52]), bytes(rec[rec_at + 52:rec_at + 56])
            data_len = struct.unpack_from('>Q', rec, rec_at + 88)[0]
            rsrc_len = struct.unpack_from('>Q', rec, rec_at + 168)[0]
            node = {'kind': 'file', 'name': name, 'type': ftype, 'creator': fcreator,
                    'size': data_len + rsrc_len, 'crdate': cdate, 'mddate': mdate,
                    'children': []}
        else:
            continue
        cnids[cnid] = node
        order.append((parent, node))
    for parent, node in order:
        if parent == 1:                        # the volume itself
            continue
        holder = cnids.get(parent)
        if holder is not None and holder['kind'] == 'folder':
            holder['children'].append(node)
    root = cnids.get(2)
    if root is None:
        raise ValueError("the HFS Plus volume's catalog has no root folder")
    return volume_name or 'untitled', crdate, mddate, root


# ---------------------------------------------------------------------------
# A machfs Volume as a tree of dicts (the shape the emitter reads)
# ---------------------------------------------------------------------------
def _volume_tree(vol: machfs.Volume):
    """vol's name, its dates, and its root folder as a tree of dicts, the
    same shape the dict readers above produce and _emit consumes: a
    folder's dict keeps kind, name, crdate, mddate, children; a file's
    dict adds type, creator, size and keeps an (empty) children list.

    size is the sum of the file's forks as f.sizes() reports it — both
    canonical readers build files as macrt.macfile.OnDisk, whose sizes()
    reports the data and resource lengths without reading the forks.

    Nothing is skipped here: the canonical readers (macrt.macfile._read_hfs,
    macrt.hfsplus.read_hfsplus) already pop the Desktop files and drop
    invisible entries (and, for HFS+, the HFS+ Private Data directory), so
    whatever they let through is what the catalog gets.  HFS offers no
    INVISIBLE fdFlags; whatever macrt's _read_hfs pops is what the
    tracker gets.

    Note: HFS+ dates arrive LOCAL, not raw GMT as the tracker's own
    reader produced — macrt.hfsplus.read_hfsplus applies _gmt_to_local
    (and to vol.mddate; vol.crdate it leaves raw), so callers see local
    dates now.  This is the deliberate 4.5 move, not an accident."""
    def node_dict(name, node):
        if isinstance(node, machfs.Folder):
            return {'kind': 'folder', 'name': name,
                    'crdate': getattr(node, 'crdate', 0),
                    'mddate': getattr(node, 'mddate', 0),
                    'children': [node_dict(n, c) for n, c in node.items()]}
        return {'kind': 'file', 'name': name,
                'type': bytes(getattr(node, 'type', b'????')),
                'creator': bytes(getattr(node, 'creator', b'????')),
                'size': sum(node.sizes()),
                'crdate': getattr(node, 'crdate', 0),
                'mddate': getattr(node, 'mddate', 0),
                'children': []}

    root = {'kind': 'folder', 'name': vol.name,
            'crdate': getattr(vol, 'crdate', 0),
            'mddate': getattr(vol, 'mddate', 0),
            'children': [node_dict(n, c) for n, c in vol.items()]}
    return vol.name, getattr(vol, 'crdate', 0), getattr(vol, 'mddate', 0), root


# ---------------------------------------------------------------------------
# Reading a tree, writing items
# ---------------------------------------------------------------------------
def _records(tree: bytes) -> Iterator[bytes]:
    """A B*-tree's leaf records in order; an empty tree has none."""
    if len(tree) < 512:
        return
    yield from _leaf_records(tree)


def _node(tree: bytes, n: int) -> tuple[int, list[bytes]]:
    """A B*-tree node's forward link (ndFLink) and its records."""
    at = 512 * n
    link, count = struct.unpack_from(">L6xH", tree, at)
    offsets = struct.unpack_from(f">{count + 1}H", tree,
                                 at + 512 - 2 * (count + 1))[::-1]
    return link, [bytes(tree[at + a:at + b]) for a, b in zip(offsets, offsets[1:])]


def _leaf_records(tree: bytes) -> Iterator[bytes]:
    """A B*-tree's leaf records in order, from bthFNode through each
    leaf's ndFLink to bthLNode.  No node is read twice, so a damaged
    tree whose links go round is a damaged tree, not one read for ever."""
    header = _node(tree, 0)[1][0]
    first, last = struct.unpack_from(">LL", header, 10)
    seen: set[int] = set()
    n = first
    while first:                               # 0: no leaves
        if n in seen:
            raise ValueError("its B*-tree's leaves link round in a circle")
        seen.add(n)
        n_next, records = _node(tree, n)
        yield from records
        if n == last:
            return
        n = n_next


def _read_fs(data: bytes, stem: str):
    """The image's filesystem as (name, created, modified, root tree):
    HFS first (a hybrid disc's Mac view is the HFS one), then
    HFS Plus, then ISO 9660.  The HFS and HFS Plus readers are
    macrt's canonical ones; MFS is recognized and refused.

    Note on dates: HFS+ catalog entries come back with LOCAL dates —
    macrt.hfsplus.read_hfsplus applies _gmt_to_local to the catalog's
    crdate/mddate (and to vol.mddate), so the catalog's dates are the
    local ones the Filer's Get Info shows, not the raw GMT the tracker's
    own reader used to produce.  vol.crdate is the exception: read_hfsplus
    leaves it raw GMT.  Whether that asymmetry stands is task 4.5's
    open question, not settled here."""
    try:
        vol = _macrt_read_hfs(data)     # scans for the MDB itself
        return _volume_tree(vol)
    except Exception:
        pass                            # no HFS volume on it: try HFS Plus
    at = _find_hfsplus(data)
    if at is not None:
        vol = read_hfsplus(data[at:])   # expects the volume from offset 0
        return _volume_tree(vol)
    if len(data) >= 1026 and data[1024:1026] == b'\xd2\xd7':
        raise ValueError("MFS volumes are not supported (their folders "
                         "live in the Finder's Desktop file)")
    if len(data) > 16 * ISO_SECTOR and data[16 * ISO_SECTOR] == 1 \
            and data[16 * ISO_SECTOR + 1:16 * ISO_SECTOR + 6] == b'CD001':
        return _read_iso(data, stem)
    raise ValueError("unsupported disk image (it is neither HFS, HFS Plus nor ISO 9660)")


def _emit(node: dict, depth: int) -> list[Item]:
    """One tree level as items: a folder first, then its children a
    level deeper.  A folder's size is the files' below it summed, its
    items count its direct children, its descendants count all below."""
    if node['kind'] == 'file':
        return [Item(kind='file', name=node['name'], type=node['type'],
                     creator=node['creator'], size=node['size'], depth=depth,
                     created=node['crdate'], modified=node['mddate'])]
    kids: list[Item] = []
    for child in node['children']:
        kids.extend(_emit(child, depth + 1))
    folder = Item(kind='folder', name=node['name'],
                  size=sum(k.size for k in kids), depth=depth,
                  items=len(node['children']), descendants=len(kids),
                  created=node['crdate'], modified=node['mddate'])
    return [folder, *kids]


def index_image(path: str | Path) -> list[Item]:
    """One disk image's catalog: its disk record, then every file and
    folder beneath it in pre-order with its depth set."""
    path = Path(path)
    name, crdate, mddate, root = _read_fs(path.read_bytes(), path.stem)
    # The disk record stands for the root folder, so its children are
    # the items at depth 1.
    children: list[Item] = []
    for child in root['children']:
        children.extend(_emit(child, 1))
    disk = Item(kind='disk', name=name or path.stem, offset=HEADER_SIZE,
                created=crdate, modified=mddate, items=len(children))
    return [disk, *children]


def index_images(paths: list[str | Path]) -> list[Item]:
    """Every image's catalog records in sequence: one disk record and
    its items per image, as a multi-disk .dtc holds them."""
    out: list[Item] = []
    for path in paths:
        out.extend(index_image(path))
    return out


def build_dtc(paths: list[str | Path]) -> bytes:
    """A .dtc catalog indexing every image's contents."""
    return write_dtc(index_images(paths))
