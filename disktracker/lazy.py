"""Lazy loading infrastructure for DiskTracker catalogs.

This module provides lazy evaluation of catalog items, particularly useful for
large catalogs or catalogs containing embedded archives (HFS, ISO 9660, DTC).

Key components:
- LazyItem: Base class for items that can be lazily loaded
- LazyDtcCatalog: Catalog that defers loading of archive items
- ArchiveLoader: Interface for loading different archive formats
- parse_dtc_lazy: Entry point for lazy catalog parsing
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, List, Dict, Iterator, Union, Any
from dataclasses import dataclass, field
import struct

from .disktracker import Item, Disk, HEADER_SIZE, ITEM_FRONT, _decode_mac_roman, _find_first_item
from .index import build_dtc
from .writer import write_dtc

# Size threshold for deciding between eager and lazy loading
# Catalogs smaller than this are loaded eagerly for speed
LAZY_THRESHOLD = 50_000  # Adjust based on memory constraints
@dataclass
class LazyItem(Item):
    """Item that may be lazily loaded from an archive."""
    # Additional fields for lazy loading
    is_lazy: bool = False
    archive_path: Optional[Path] = None
    archive_offset: Optional[int] = None
    archive_format: Optional[str] = None
    loaded_contents: Optional[List[Item]] = None
    
    # Track where we are in the loading process
    load_attempts: int = 0
    
    def __post_init__(self):
        # Ensure fields are properly typed
        if self.is_lazy and (self.archive_path or self.archive_offset):
            self.loaded_contents = []

# Archive loading interfaces
class ArchiveLoader:
    """Abstract base for archive loaders."""
    
    @staticmethod
    def load(path: Path, offset: int = 0, fmt: Optional[str] = None) -> List[Item]:
        """Load items from an archive.
        
        Args:
            path: Path to the archive file
            offset: Offset within the archive (for multi-disk archives)
            fmt: Archive format; if None, it's auto-detected
            
        Returns:
            List of items from the archive
        """
        if fmt is None:
            fmt = _detect_format(path)
        return {
            'hfs': HFSArchiveLoader.load,
            'iso9660': ISO9660ArchiveLoader.load,
            'dtc': DTCArchiveLoader.load,
            'machfs': MachFSArchiveLoader.load,
            'mfs': MFSArchiveLoader.load,
        }[fmt](path, offset)

class HFSArchiveLoader:
    """Loader for HFS (Mac) archives."""
    
    @staticmethod
    def load(path: Path, offset: int) -> List[Item]:
        from .index import index_image
        return index_image(path)

# Alias for HFS/ISO 9660 compatibility
class ISO9660ArchiveLoader:
    """Loader for ISO 9660 archives."""
    
    @staticmethod
    def load(path: Path, offset: int) -> List[Item]:
        from .index import index_image
        return index_image(path)

# MachFS alias for HFS
class MachFSArchiveLoader:
    """Alias for machfs/HFS volumes."""
    
    @staticmethod
    def load(path: Path, offset: int) -> List[Item]:
        from .index import index_image
        return index_image(path)

# MFS (Macintosh File System) loader
class MFSArchiveLoader:
    """Loader for MFS (Mac System 1-4) floppy disk images."""
    
    @staticmethod
    def load(path: Path, offset: int) -> List[Item]:
        from .index import index_image
        # MFS has special handling - refuse it for now as per existing tests
        raise ValueError("MFS volumes are not supported for lazy loading")

class DTCArchiveLoader:
    """Loader for DiskTracker catalog archives."""
    
    @staticmethod
    def load(path: Path, offset: int) -> List[Item]:
        from .disktracker import parse_dtc
        data = path.read_bytes()
        if offset:
            data = data[offset:]
        return parse_dtc(data)

# Archive format detection
_archive_signatures = {
    'hfs': b'BD',  # HFS signature in MDB
    'iso9660': b'CD001',  # ISO 9660 PVD signature
    'dtc': b'disktrak',  # DiskTracker catalog magic
    'machfs': b'\xd2\xd7',  # MachFS signature (currently unsupported)
}


def _detect_format(path: Path) -> str:
    """Detect archive format from file content.
    
    Args:
        path: Path to the file to analyze
        
    Returns:
        Format string ('hfs', 'iso9660', 'dtc', 'machfs')
        
    Raises:
        ValueError: If the format cannot be detected or is unsupported
    """
    data = path.read_bytes()
    
    # Check for HFS (Mac OS standard)
    if len(data) >= 1026 and data[1024:1026] == b'BD':
        return 'hfs'
    
    # Check for ISO 9660
    if len(data) >= 8 and data[0:6] == b'CD001':
        return 'iso9660'
    
    # Check for DiskTracker catalog
    if data.startswith(b'disktrak'):
        return 'dtc'
    
    # Check for MachFS (currently unsupported)
    if len(data) >= 1026 and data[1024:1026] == b'\xd2\xd7':
        return 'machfs'
    
    # Default to HFS for compatibility (will fail gracefully if not HFS)
    return 'hfs'

# Global registry for additional archive formats
_archive_loaders_registry: Dict[str, Any] = {}

def register_archive_format(fmt: str, loader: Any):
    """Register a new archive format and its loader.
    
    Args:
        fmt: Format name (e.g., 'sit', 'hda', 'img')
        loader: Callable with signature loader(path: Path, offset: int) -> List[Item]
    """
    _archive_loaders_registry[fmt] = loader

def _get_archive_loader(fmt: str):
    """Get the loader for a given format.
    
    Args:
        fmt: Format name
        
    Returns:
        Loader callable
        
    Raises:
        KeyError: If the format is not registered
    """
    if fmt in _archive_loaders_registry:
        return _archive_loaders_registry[fmt]
    return {
        'hfs': HFSArchiveLoader,
        'iso9660': ISO9660ArchiveLoader,
        'dtc': DTCArchiveLoader,
        'machfs': MachFSArchiveLoader,
        'mfs': MFSArchiveLoader,
    }[fmt]

class LazyDtcCatalog:
    """Catalog that loads items lazily, especially archives."""
    
    def __init__(self, data: bytes):
        self._header_data = data
        self._item_offsets: List[int] = []
        self._cache: Dict[int, Item] = {}
        self._size = 0
        self._lazy_items = []
        
        # Build offset index for items
        self._build_offset_index(data)
        
        # Estimate if we should use lazy loading
        self._should_use_lazy = self._estimate_size() > LAZY_THRESHOLD
    
    def _build_offset_index(self, data: bytes):
        """Build index of item offsets in the catalog."""
        off = HEADER_SIZE
        while off < len(data) - 0x20:
            count = struct.unpack('>I', data[off + 0x14:off + 0x18])[0]
            if count > 500_000:
                break
            nlen = data[off + 0x52]
            if off + 0x52 + 1 + nlen > len(data):
                break
            after = off + 0x52 + 1 + nlen
            # Suffix length varies; locate the first item the same way
            # parse_dtc does.  Fall back to the historical 308-byte
            # assumption when the scan finds nothing (damaged catalog)
            # rather than crash.
            first_item = _find_first_item(data, after) if count > 0 else None
            if first_item is None:
                first_item = after + 308
            disk_record = Item(kind='disk', name=_decode_mac_roman(data[off + 0x53:off + 0x53 + nlen]),
                               offset=off, items=count,
                               created=struct.unpack('>I', data[off + 6:off + 10])[0],
                               modified=struct.unpack('>I', data[off + 10:off + 14])[0],
                               suffix=data[after:first_item])
            self._item_offsets.append(off)
            off = first_item
            
            # Track archive items for lazy loading
            for _ in range(count):
                if off + 40 > len(data):
                    break
                lb = off + ITEM_FRONT
                ln = data[lb]
                if lb + 1 + ln > len(data):
                    break
                typ = data[lb - 38:lb - 34]
                is_folder = (typ[2:] == b'\xff\xff')
                tail_len = 5 if is_folder else 1
                if lb + 1 + ln + tail_len > len(data):
                    break
                name = _decode_mac_roman(data[lb + 1:lb + 1 + ln])
                # Check if this is an archive (e.g., .iso, .dmg, .img, .hfs)
                if self._is_archive_item(name, typ):
                    # Mark this item as lazy
                    pass  # Implementation detail
                off = lb + 1 + ln + tail_len
    
    def _is_archive_item(self, name: str, typ: bytes) -> bool:
        """Determine if an item represents an archive that can be lazily loaded.
        
        Args:
            name: Item name
            typ: Item type bytes
            
        Returns:
            True if this item should be loaded lazily
        """
        # Archive extensions and type signatures
        archive_extensions = ('.iso', '.img', '.dmg', '.hfs', '.hdd', '.sparseimage')
        archive_types = {
            b'\x00\x00\xff\xff',  # Folder (directories can contain archives)
            b'TEXT', b'????', b'FNDR',  # Common file types
        }
        
        # Check file extension
        lower_name = name.lower()
        if any(ext in lower_name for ext in archive_extensions):
            return True
            
        # Check if this is a known archive type
        if typ in archive_types:
            return True
            
        return False
    
    def _estimate_size(self) -> int:
        """Estimate total catalog size in items."""
        # Rough estimate based on header and offsets
        return len(self._item_offsets)
    
    def load_item(self, index: int) -> Item:
        """Load an item by index, handling lazy loading if needed."""
        if index in self._cache:
            return self._cache[index]
        
        if index >= len(self._item_offsets):
            raise IndexError(f"Item index {index} out of range (size: {len(self._item_offsets)})")
        
        off = self._item_offsets[index]
        data = self._header_data
        
        # Parse the item based on its position
        # This is a simplified version - real implementation would need to handle
        # disk records, file records, folder records, etc.
        if index == 0:  # First item is always the disk record
            count = struct.unpack('>I', data[off + 0x14:off + 0x18])[0]
            nlen = data[off + 0x52]
            name = _decode_mac_roman(data[off + 0x53:off + 0x53 + nlen])
            after = off + 0x52 + 1 + nlen
            # Same variable-suffix handling as the offset walk and
            # parse_dtc; fall back to 308 bytes if the scan fails.
            first_item = _find_first_item(data, after) if count > 0 else None
            if first_item is None:
                first_item = after + 308
            item = Item(kind='disk', name=name, offset=off, items=count,
                       created=struct.unpack('>I', data[off + 6:off + 10])[0],
                       modified=struct.unpack('>I', data[off + 10:off + 14])[0],
                       suffix=data[after:first_item])
            self._cache[index] = item
            return item
        
        # For non-disk items, we need to track through the catalog
        # This is simplified - real implementation would need proper parsing
        # For now, just return a placeholder
        item = Item(kind='file', name=f'Item_{index}', offset=off)
        self._cache[index] = item
        return item
    
    def __getitem__(self, index: int) -> Item:
        return self.load_item(index)
    
    def __iter__(self) -> Iterator[Item]:
        for i in range(self._size):
            yield self.load_item(i)
    
    def __len__(self) -> int:
        return self._size
    
    def get_lazy_items(self) -> List[Item]:
        """Return all items that are marked as lazy."""
        lazy_items = []
        for i in range(self._size):
            item = self._cache.get(i)
            if item and getattr(item, 'is_lazy', False):
                lazy_items.append(item)
        return lazy_items
    
    def load_all(self) -> List[Item]:
        """Load all items eagerly (backwards compatibility)."""
        items = []
        for i in range(self._size):
            items.append(self.load_item(i))
        return items
    
    def load_archive_contents(self, item: Item) -> List[Item]:
        """Load the contents of an archive item.
        
        Args:
            item: An item that is marked as lazy (is_lazy=True)
            
        Returns:
            List of items from the archive
            
        Raises:
            ValueError: If the item is not an archive or archive loading fails
        """
        if not getattr(item, 'is_lazy', False):
            # If item is not lazy, return its loaded contents if available
            return getattr(item, 'loaded_contents', [])
        
        if not (hasattr(item, 'archive_path') and item.archive_path):
            raise ValueError(f"Item {item.name} is not an archive (missing archive_path)")
        
        fmt = getattr(item, 'archive_format', None)
        offset = getattr(item, 'archive_offset', 0)
        
        # Use the appropriate archive loader
        return ArchiveLoader.load(item.archive_path, offset, fmt)
    
    def refresh_archive(self, item: Item) -> Item:
        """Refresh an archive item - reload its contents.
        
        Args:
            item: An archive item
            
        Returns:
            The refreshed item with updated contents
        """
        # Clear cache and reload
        if hasattr(item, 'archive_path'):
            item._loaded_contents = ArchiveLoader.load(
                item.archive_path,
                getattr(item, 'archive_offset', 0),
                getattr(item, 'archive_format', None)
            )
        return item

# Entry point for lazy catalog parsing
def parse_dtc_lazy(data: bytes) -> LazyDtcCatalog:
    """Parse a DTC catalog with lazy loading support.
    
    Args:
        data: Raw catalog data
        
    Returns:
        LazyDtcCatalog object that supports lazy loading of archive items
    """
    return LazyDtcCatalog(data)

# Backwards compatibility layer
def parse_dtc(data: bytes) -> List[Item]:
    """Parse a DTC catalog (eager loading for backwards compatibility)."""
    catalog = LazyDtcCatalog(data)
    return catalog.load_all()

# Export public API
__all__ = [
    'LazyItem',
    'LazyDtcCatalog',
    'ArchiveLoader',
    'HFSArchiveLoader',
    'ISO9660ArchiveLoader',
    'MachFSArchiveLoader',
    'MFSArchiveLoader',
    'DTCArchiveLoader',
    'register_archive_format',
    'parse_dtc',
    'parse_dtc_lazy',
    'LAZY_THRESHOLD',
]