"""Index disk images into DiskTracker .dtc catalogs.

Accepts a list of disk images — an HFS volume (raw, Apple-partitioned,
or a hybrid), an HFS Plus volume, or an ISO 9660 image — reads each
one's directory tree, and emits the pre-order item list a .dtc
catalog holds, so write_dtc can turn it into a catalog indexing every
image's contents.

Not standalone: built on macrt's readers and machfs.  machfs builds
the Volume/Folder/File trees on top of them; the tracker reads HFS
volumes through macrt.macfile._read_hfs, HFS Plus through
macrt.hfsplus.read_hfsplus, and ISO 9660 through macrt.iso9660.
MFS volumes are recognized and refused: their folders live
in the Finder's Desktop file, not in the File Manager's catalog.
"""

import struct
from pathlib import Path

import machfs

from macrt.iso9660 import _read_iso
from macrt.hfsplus import read_hfsplus
from macrt.macfile import _read_hfs

from .disktracker import HEADER_SIZE, Item
from .writer import write_dtc

ISO_SECTOR = 2048


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


# ---------------------------------------------------------------------------
# A machfs Volume as a tree of dicts (the shape the emitter reads)
# ---------------------------------------------------------------------------
def _volume_tree(vol: machfs.Volume):
    """vol's name, its dates, and its root folder as a tree of dicts, the
    same shape _emit consumes: a folder's dict keeps kind, name, crdate,
    mddate, children; a file's
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
        vol = _read_hfs(data)           # scans for the MDB itself
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
    try:
        iso_ok = len(data) > 16 * ISO_SECTOR and data[16 * ISO_SECTOR] == 1 \
            and data[16 * ISO_SECTOR + 1:16 * ISO_SECTOR + 6] == b'CD001'
        if iso_ok:
            try:
                return _read_iso(data, stem)
            except Exception:
                pass                            # an ISO this reader can't read: try UDF
        from macrt.udf import read_udf
        return _volume_tree(read_udf(bytes(data)))
    except Exception as e:
        # A named UDF refusal (2.x, a virtual allocation table, …) reaches
        # the caller; anything else is not a disc this tracker reads.
        if isinstance(e, ValueError) and str(e).startswith("the UDF disc"):
            raise
        raise ValueError("unsupported disk image (it is neither HFS, HFS Plus, ISO 9660 nor UDF)") from None


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
