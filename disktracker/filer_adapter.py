"""Adapter to open Disk Tracker .dtc/.dt1 files as read-only Filer volumes."""

from pathlib import Path
import machfs
from disktracker import parse_dtc

class CatalogFile(machfs.File):
    """A file whose size comes from the catalog index, not fork contents.

    `disk` is the name of the disk record the item was cataloged on,
    for prompts that have to say which disk to go looking for; None for
    items that precede the first disk record (a malformed catalog)."""
    def __init__(self, type=b'????', creator=b'????', size=0, crdate=0, mddate=0,
                 disk: str | None = None):
        super().__init__()
        self.type = type[:4] if len(type) >= 4 else b'????'
        self.creator = creator[:4] if len(creator) >= 4 else b'????'
        self._catalog_size = size
        self.crdate = crdate
        self.mddate = mddate
        self.disk = disk

    def sizes(self):
        return (self._catalog_size, 0)


class DiskTrackerVolume(machfs.Volume):
    """Wraps a parsed Disk Tracker file into a read-only machfs.Volume structure for Filer display.

    The volume root lists only the indexed disks, one folder per disk
    record, in catalog order: opening a catalog in the Filer starts at
    the list of disk images it indexes.  Each disk's items are inside its
    folder, so a multi-disk catalog stays per-disk instead of merging
    every disk's top level into the root.  Disks listed under the same
    name are separate folders: the first keeps the plain name and later
    copies get ' (2)', ' (3)', … suffixes."""
    def __init__(self, path: str | Path):
        super().__init__()
        path = Path(path)
        self.path = path
        self.name = path.stem
        data = path.read_bytes()
        items = parse_dtc(data)

        counts = {}
        used = set()
        stack = [self]  # folders the current item can hang off, by depth
        disk_name: str | None = None     # the disk record the items belong to
        for item in items:
            if item.kind == 'disk':
                name = item.name
                disk_name = name
                count = counts.get(name, 0) + 1
                while True:
                    display = name if count == 1 else f'{name} ({count})'
                    if display not in used:
                        break
                    count += 1
                counts[name] = count
                used.add(display)
                folder = machfs.Folder()
                try:
                    self[display] = folder
                except Exception:
                    pass
                # depth is relative to each disk, so restart the stack
                # there; items seen before the first disk (a malformed
                # catalog) hang off the root rather than being dropped
                stack = [folder]
                continue

            # Ensure stack is long enough
            while len(stack) <= item.depth:
                stack.append(machfs.Folder())

            parent = stack[item.depth - 1]

            if item.kind == 'folder':
                f = machfs.Folder()
                try:
                    parent[item.name] = f
                except Exception:
                    pass
                # Push onto stack for children
                if len(stack) > item.depth:
                    stack[item.depth] = f
                else:
                    stack.append(f)
            elif item.kind == 'file':
                f = CatalogFile(
                    type=item.type,
                    creator=item.creator,
                    size=item.size,
                    crdate=item.created,
                    mddate=item.modified,
                    disk=disk_name
                )
                try:
                    parent[item.name] = f
                except Exception:
                    pass
