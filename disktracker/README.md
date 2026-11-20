# disktracker

A standalone library for the Disk Tracker catalog format (`.dtc`/`.dt1`):
read catalogs, write them, and build new ones by indexing disk images.
Pure standard library, except `filer_adapter` (which needs `machfs` to
hand a catalog to Classetta's Filer as a read-only volume).  Nothing
here imports `macrt`, so it works as its own package elsewhere.

## Install

It ships inside this project (`pip install -e .`); on its own, put the
`disktracker/` directory on the path.  No third-party dependencies for
the core reader, writer, and indexer.

## Build a catalog from disk images

```python
from disktracker import build_dtc

# One .dtc indexing every image's contents: HFS volumes (raw,
# Apple-partitioned, or hybrid), HFS Plus volumes, and ISO 9660 images
# are read; MFS is recognized and refused.
data = build_dtc(['MyDisk.dsk', 'Install CD.iso', 'Tools.hdv'])
open('catalog.dtc', 'wb').write(data)
```

`index_image(path)` returns the same records without writing them: a
disk record first, then every file and folder beneath it in pre-order,
each with its `depth`, size, type/creator, and created/modified dates
set.  `index_images(paths)` concatenates several images into one
multi-disk list.

## Read a catalog

```python
from disktracker import parse_dtc

items = parse_dtc(open('catalog.dtc', 'rb').read())
for item in items:
    print(item.kind, item.name, item.depth, item.size)
```

`parse_dtc` auto-detects Disk Tracker 2 (magic `disktrak`) and 1.x
files.  Each `Item` carries `kind` (`disk`/`folder`/`file`), `name`,
`type`, `creator`, `size`, `parent`, `created`, `modified`, `depth`,
`items` (a folder's direct children), and `descendants`.

## Write a catalog from items

```python
from disktracker import Item, write_dtc

items = [
    Item(kind='disk', name='My Disk', created=0, modified=0),
    Item(kind='folder', name='Documents', depth=1, items=1, descendants=1),
    Item(kind='file', name='notes.txt', depth=2, size=1200,
         type=b'TEXT', creator=b'ttxt'),
]
open('out.dtc', 'wb').write(write_dtc(items))
```

Each disk record owns the non-disk items that follow it, so a list
with several disk records becomes a multi-disk `.dtc`.

## Open a catalog as a read-only Filer volume

```python
from disktracker.filer_adapter import DiskTrackerVolume

volume = DiskTrackerVolume('catalog.dtc')   # a machfs.Volume-shaped tree
```

The volume root lists only the indexed disks, one folder per disk
record, in catalog order; a disk's recorded items are inside its
folder, so a multi-disk catalog stays per-disk.  Disks listed under the
same name appear as separate folders: the first keeps the plain name,
later copies get ' (2)', ' (3)', … suffixes.  Files report their
catalog sizes without any fork data existing; the window gets a distinct
catalog icon in its title bar.

## Command line

```
python -m disktracker index disk1.iso disk2.dsk -o catalog.dtc
python -m disktracker parse catalog.dtc
```

## Tests

```
python3 -m pytest disktracker/tests/
```
