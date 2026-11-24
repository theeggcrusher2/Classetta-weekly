"""Save what a Filer window browses to a Disk Tracker 2 catalog.

The whole volume is indexed, not just the window's folder: one disk
record, then its files and folders beneath it in pre-order, depths set,
a folder's size the bytes of the files below it and its item counts its
children, nested folders included. Host directories carry filesystem
dates (Mac epoch: seconds since 1904-01-01); folders and files on a
volume carry the dates the volume keeps.

Read-only Disk Tracker catalog views refuse: the only thing they hold
is the catalog itself.
"""

import os
import machfs

from .macfile import ArchiveFolder, ContainerFolder, HostFolder, VolumeFolder, load_file
from disktracker.disktracker import HEADER_SIZE, Item
from disktracker.filer_adapter import DiskTrackerVolume
from disktracker.writer import write_dtc

MAC_EPOCH = 2082844800       # seconds from 1904-01-01 to 1970-01-01


def _host_time(seconds: float) -> int:
    return int(seconds) + MAC_EPOCH


def default_save_name(volume) -> str:
    """The file name the save dialog starts with."""
    if isinstance(volume, HostFolder):
        stem = volume.root.path.name or "Volume"
    elif isinstance(volume, ContainerFolder):
        stem = volume.name
    else:
        stem = getattr(volume, "name", None) or "Volume"
    return f"{stem}.dtc"


def _walk_volume(volume, path, depth):
    """One folder of an HFS volume as items: (items, file bytes below,
    direct children)."""
    items, total = [], 0
    folder = VolumeFolder(volume, path)
    node = folder._node(path)
    node_items = list(node.items())
    for name, child in node_items:
        if isinstance(child, machfs.Folder):
            kids, subtotal, direct = _walk_volume(volume, path + (name,), depth + 1)
            items.append(Item(kind="folder", name=name, size=subtotal, depth=depth,
                              items=direct, descendants=len(kids),
                              created=getattr(child, "crdate", 0),
                              modified=getattr(child, "mddate", 0)))
            items.extend(kids)
            total += subtotal
        elif isinstance(child, machfs.File):
            size = len(bytes(child.data)) + len(bytes(child.rsrc))
            items.append(Item(kind="file", name=name,
                              type=bytes(child.type), creator=bytes(child.creator),
                              size=size, depth=depth,
                              created=getattr(child, "crdate", 0),
                              modified=getattr(child, "mddate", 0)))
            total += size
    return items, total, len(node_items)


def _walk_host(folder, depth):
    """One host directory as items: (items, file bytes below, direct
    children).  Dotfiles are skipped, as the host listing skips them."""
    items, total, direct = [], 0, 0
    try:
        names = sorted(os.listdir(folder.path))
    except OSError:
        return items, total, direct
    for n in names:
        if n.startswith((".", "%")):
            continue
        if n.lower().endswith(".rsrc") and n[:-5] in names:
            continue                            # app's resource fork, beside app
        p = folder.path / n
        try:
            st = p.stat()
        except OSError:
            continue
        created, modified = _host_time(st.st_ctime), _host_time(st.st_mtime)
        if p.is_dir():
            kids, subtotal, direct_children = _walk_host(HostFolder(p, folder.root), depth + 1)
            items.append(Item(kind="folder", name=n, size=subtotal, depth=depth,
                              items=direct_children, descendants=len(kids),
                              created=created, modified=modified))
            items.extend(kids)
            direct += 1
            total += subtotal
        elif p.is_file():
            try:
                f = load_file(p, folder)
            except (ValueError, OSError):
                continue                        # damaged; the rest still save
            size = len(f.data) + len(f.rsrc)
            items.append(Item(kind="file", name=f.name, type=f.type, creator=f.creator,
                              size=size, depth=depth,
                              created=created, modified=modified))
            total += size
            direct += 1
    return items, total, direct


def build_catalog(volume) -> bytes:
    """One Disk Tracker 2 catalog for volume's whole tree, as bytes.

    Volumes and host directories give one disk record apiece with their
    contents below them in pre-order; an encoded container gives one
    disk record holding the one file it decodes to.
    """
    if isinstance(volume, HostFolder):
        root = volume.root
        name = root.path.name or "Volume"
        children, _, _ = _walk_host(root, 1)
        created = modified = 0
        try:
            st = root.path.stat()
            created, modified = _host_time(st.st_ctime), _host_time(st.st_mtime)
        except OSError:
            pass
    elif isinstance(volume, ContainerFolder):
        f = volume.contents
        name = volume.name
        size = len(f.data) + len(f.rsrc)
        children = [Item(kind="file", name=f.name, type=f.type, creator=f.creator,
                         size=size, depth=1, created=f.crdate, modified=f.mddate)]
        created = modified = 0
    elif isinstance(volume, machfs.Volume):
        name = getattr(volume, "name", None) or "Volume"
        children, _, _ = _walk_volume(volume, (), 1)
        created, modified = getattr(volume, "crdate", 0), getattr(volume, "mddate", 0)
    else:
        raise ValueError(f"nothing to save from {volume!r}")
    disk = Item(kind="disk", name=name, offset=HEADER_SIZE,
                created=created, modified=modified, items=len(children))
    return write_dtc([disk, *children])


def savable(volume) -> bool:
    """True for what the Save Catalog command can index: browse views,
    not read-only Disk Tracker catalog views."""
    return not isinstance(volume, (DiskTrackerVolume, ArchiveFolder))
