"""DiskTracker 1.x and 2 format reader/writer, and a disk image indexer.

The public entry points, in the order they're usually reached for:

    from disktracker import build_dtc, index_image, parse_dtc

    # A .dtc indexing a list of disk images (HFS, HFS Plus or ISO 9660)
    open('catalog.dtc', 'wb').write(build_dtc(['disk1.iso', 'disk2.dsk']))

    # One image's records without writing them: a disk record first,
    # then its files and folders in pre-order with depth set
    items = index_image('disk1.iso')

    # Read a catalog back (Disk Tracker 2 and 1.x, auto-detected)
    items = parse_dtc(open('catalog.dtc', 'rb').read())

`index_images(paths)` batches several images into one multi-disk list;
`write_dtc(items)` writes any item list back out.  To open a catalog
as a read-only Filer volume, see `disktracker.filer_adapter`:

    from disktracker.filer_adapter import DiskTrackerVolume
    volume = DiskTrackerVolume('catalog.dtc')

Or from the shell:

    python -m disktracker index disk1.iso disk2.dsk -o catalog.dtc
    python -m disktracker parse catalog.dtc

For large catalogs or catalogs containing embedded archives, use the lazy
loading API:

    from disktracker.lazy import parse_dtc_lazy, LazyDtcCatalog, ArchiveLoader
    
    # Parse with lazy loading support
    catalog = parse_dtc_lazy(open('big.dtc', 'rb').read())
    
    # Access items lazily
    for i in range(len(catalog)):
        item = catalog[i]
        if item.is_lazy:
            # Archive contents loaded on-demand
            contents = item._loaded_contents
            # Process the archive contents

See disktracker/README.md for the full tour.
"""

from .disktracker import parse_dtc, Disk, Item, parse_dtc_v1
from .writer import write_dtc
from .index import build_dtc, index_image, index_images
from .lazy import (
    parse_dtc_lazy,
    LazyDtcCatalog,
    ArchiveLoader,
    HFSArchiveLoader,
    ISO9660ArchiveLoader,
    MachFSArchiveLoader,
    MFSArchiveLoader,
    DTCArchiveLoader,
    register_archive_format,
    LAZY_THRESHOLD,
)

__all__ = [
    "parse_dtc", "parse_dtc_v1", "write_dtc", "Disk", "Item",
    "build_dtc", "index_image", "index_images",
    "parse_dtc_lazy", "LazyDtcCatalog", "ArchiveLoader",
    "HFSArchiveLoader", "ISO9660ArchiveLoader", "MachFSArchiveLoader",
    "MFSArchiveLoader", "DTCArchiveLoader", "register_archive_format",
    "LAZY_THRESHOLD",
]
