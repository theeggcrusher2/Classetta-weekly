"""Associate a document with the application that made it, from that
application's own resources.

A document's creator says which application made it.  The icon a
document shows, and the kind string beside its name in a Filer window,
are declared by that application in three resources:

- 'FREF' binds a file type to a local icon id,
- 'BNDL' binds that local id to the resource id of its icon family,
- 'kind' carries custom kind strings for types it creates, with the
  special type 'apnm' (ftApplicationName) holding the application's
  own name, for strings like "<name> document".

Apple never published the Desktop Database's format, and its files are
a cache of exactly this data, so this reads the application itself,
the way the Finder falls back to an uncataloged application.
"""

from __future__ import annotations

import os
import struct
import weakref
from functools import lru_cache

from .macfile import HostFolder, MacFile, VolumeFolder, load_file, volume_files
from .resfork import InvalidForkError, is_fork, read_fork
from .toolbox.icons import LARGE, MINI, SMALL, bitmap_icon, color_suite_icon, suite_icon

FT_APPLICATION_NAME = b"apnm"      # the ftApplicationName entry in a 'kind' resource
REGION_US = 0                      # verUS: the only region macrt reads

WRAPPER_SUFFIXES = (".bin", ".hqx", ".cpt", ".sit", ".mac")


def _u16(data: bytes, off: int) -> int:
    return struct.unpack_from(">H", data, off)[0]


def _i16(data: bytes, off: int) -> int:
    """Resource ids and local ids are signed 16-bit values."""
    return struct.unpack_from(">h", data, off)[0]


try:
    b"\xa5".decode("mac_roman")

    def _decode_mac_roman(data: bytes) -> str:
        return data.decode("mac_roman", "replace")
except LookupError:
    def _decode_mac_roman(data: bytes) -> str:
        return data.decode("latin-1")


def _split_bundle(data: bytes, minus_one: bool) -> dict[bytes, dict[int, int]] | None:
    """One reading of a 'BNDL'.  Apple's compilers store each count as
    the number of entries minus one, though the Inside Macintosh prose
    describes the Rez source that writes them; the reading is returned
    only when it consumes the resource exactly, else None."""
    arrays = _u16(data, 6) + (1 if minus_one else 0)
    off = 8
    out: dict[bytes, dict[int, int]] = {}
    for _ in range(arrays):
        if off + 6 > len(data):
            return None
        rtype = data[off:off + 4]
        if not rtype.isascii() or not all(32 <= c < 127 for c in rtype):
            return None
        pairs = _u16(data, off + 4) + (1 if minus_one else 0)
        off += 6
        if off + 4 * pairs > len(data):
            return None
        mapping: dict[int, int] = {}
        for _ in range(pairs):
            mapping[_i16(data, off)] = _i16(data, off + 2)
            off += 4
        out[rtype] = mapping
    return out if off == len(data) else None


def parse_bundle(data: bytes) -> dict[bytes, dict[int, int]]:
    """A 'BNDL' resource as {resource type: {local id: resource id}}.

    Signature (4), signature resource id (2), array count (2), then per
    array: the resource type (4), a pair count (2), and that many
    (local id, resource id) pairs.  The counts as Apple's compilers
    write them are one less than the entries, so both readings are
    tried and the one that consumes the resource exactly wins.
    """
    out: dict[bytes, dict[int, int]] = {}
    if len(data) < 8:
        return out
    for minus_one in (True, False):
        maps = _split_bundle(data, minus_one)
        if maps is not None:
            return maps
    return out


def parse_fref(data: bytes) -> tuple[bytes, int] | None:
    """A 'FREF' resource as (file type, local icon id); None if short."""
    if len(data) < 6:
        return None
    return data[:4], _i16(data, 4)


def parse_kind(data: bytes) -> dict[bytes, str]:
    """A 'kind' resource as {file type: kind string}.  Signature (4)
    and region code (2) first; then a file type (4) and a Pascal
    string per entry."""
    out: dict[bytes, str] = {}
    if len(data) < 6:
        return out
    off = 6
    while off + 5 <= len(data):
        ftype = data[off:off + 4]
        length = data[off + 4]
        off += 5
        if off + length > len(data):
            break
        out[ftype] = _decode_mac_roman(data[off:off + length])
        off += length
    return out


def kind_region(data: bytes) -> int | None:
    return _u16(data, 4) if len(data) >= 6 else None


class AppInfo:
    def __init__(self):
        self.icon_map: dict[int, int] = {}          # local id -> ICN#-family resource id
        self.frefs: dict[bytes, int] = {}           # file type -> local icon id
        self.kinds: dict[bytes, str] = {}           # file type -> custom kind string
        self.app_name: str | None = None            # ftApplicationName, if declared
        self.by_id: dict[tuple[bytes, int], bytes] = {}

    def icon_resource_id(self, file_type: bytes) -> int | None:
        local = self.frefs.get(file_type)
        if local is None:
            return None
        return self.icon_map.get(local)


@lru_cache(maxsize=64)
def _app_info(fork: bytes) -> AppInfo | None:
    """Everything a fork declares of its application's bundle; None if
    the fork can't be read."""
    try:
        if not is_fork(fork):
            return None
        resources = read_fork(fork)
    except (InvalidForkError, ValueError, Exception):
        return None
    info = AppInfo()
    icon_maps: list[dict[int, int]] = []
    kind_entries: list[tuple[int | None, dict[bytes, str]]] = []
    for r in resources:
        info.by_id[(r.type, r.id)] = r.data
        if r.type == b"FREF":
            parsed = parse_fref(r.data)
            if parsed is not None:
                info.frefs.setdefault(parsed[0], parsed[1])
        elif r.type == b"BNDL":
            maps = parse_bundle(r.data)
            icon_lists = {t: m for t, m in maps.items() if t != b"FREF"}
            chosen = icon_lists.get(b"ICN#") or next(iter(icon_lists.values()), None)
            if chosen:
                icon_maps.append(chosen)
        elif r.type == b"kind":
            kind_entries.append((kind_region(r.data), parse_kind(r.data)))
    for mapping in icon_maps:
        info.icon_map.update(mapping)
    # verUS strings win; a non-US resource is better than silence.
    usable = [e for region, e in kind_entries if region == REGION_US]
    if not usable:
        usable = [e for _, e in kind_entries]
    for entries in usable:
        info.kinds.update(entries)
    info.app_name = info.kinds.get(FT_APPLICATION_NAME)
    info.kinds = {t: s for t, s in info.kinds.items() if t != FT_APPLICATION_NAME}
    return info


def _strip_wrapper(name: str) -> str:
    lower = name.lower()
    for suffix in WRAPPER_SUFFIXES:
        if lower.endswith(suffix):
            return name[: -len(suffix)]
    return name


_host_app_cache: dict[tuple, MacFile | None] = {}


class _VolumeAppCache:
    """creator -> app per live volume.  Keyed by id(volume), validated
    by a weakref, so a freed volume's id is never trusted."""

    def __init__(self, limit: int = 256):
        self._map: dict[tuple[int, bytes], tuple[weakref.ref, MacFile | None]] = {}
        self._limit = limit

    def get(self, volume, creator: bytes) -> MacFile | None:
        key = (id(volume), creator)
        entry = self._map.get(key)
        if entry is not None and entry[0]() is volume:
            return entry[1]
        apps = [f for f in volume_files(volume) if f.is_application and f.creator == creator]
        app = min(apps, key=lambda f: f.key.count(":"), default=None)
        if len(self._map) >= self._limit:
            self._map.clear()
        self._map[key] = (weakref.ref(volume), app)
        return app


_volume_app_cache = _VolumeAppCache()


def _volume_app(volume, creator: bytes) -> MacFile | None:
    """The app on volume with that creator, nearest the top of the disk."""
    return _volume_app_cache.get(volume, creator)


def _host_app(root, creator: bytes) -> MacFile | None:
    """The app under a host directory with that creator, nearest the
    top; None without Finder metadata to identify one."""
    key = (str(root.path), creator)
    if key in _host_app_cache:
        return _host_app_cache[key]
    found = None
    try:
        walk = os.walk(root.path)
    except OSError:
        return None
    for dirpath, dirs, files in walk:
        dirs[:] = [d for d in dirs if not d.startswith((".", "%"))]
        parts = len(os.path.relpath(dirpath, root.path).split(os.sep))
        for name in files:
            if name.startswith((".", "%")):
                continue
            lower = name.lower()
            if lower.endswith((".rsrc",)) or ".rsrc/" in dirpath.lower():
                continue
            if found is not None and parts >= found[0]:
                continue
            try:
                path = os.path.join(dirpath, name)
                info = os.getxattr(path, "com.apple.FinderInfo", follow_symlinks=False) \
                    if hasattr(os, "getxattr") else b""
            except OSError:
                info = b""
            if len(info) < 8 or info[4:8] != creator or info[0:4] != b"APPL":
                continue
            try:
                found = (parts, load_file(path, root))
            except (ValueError, OSError):
                continue
    _host_app_cache[key] = found[1] if found else None
    return _host_app_cache[key]


def associated_app(f: MacFile) -> MacFile | None:
    """The application on f's own disk that made f; None for a program,
    for a document with no Finder metadata, or when no such app is on
    that disk."""
    if f.is_application or f.creator == b"????":
        return None
    if isinstance(f.folder, VolumeFolder) and f.folder.volume is not None:
        return _volume_app(f.folder.volume, f.creator)
    if isinstance(f.folder, HostFolder):
        return _host_app(f.folder.root, f.creator)
    return None


def associated_kind(f: MacFile) -> str | None:
    """f's kind string: its creating app's custom 'kind' entry for f's
    type if it has one, else "<app> document"; None without an app."""
    app = associated_app(f)
    if app is None:
        return None
    info = _app_info(bytes(app.rsrc)) if app.rsrc else None
    if info is not None:
        custom = info.kinds.get(f.type)
        if custom:
            return custom
        if info.app_name:
            return f"{info.app_name} document"
    return f"{_strip_wrapper(app.name)} document"


def _rows_for(info: AppInfo, resid: int | None, large: bool) -> tuple[str, ...] | None:
    """The rows ('#'/'.') of an icon family at resource id resid, as
    declared in info's fork; None without one."""
    if resid is None:
        return None

    def get(kind: str) -> bytes | None:
        return info.by_id.get((kind.encode("ascii"), resid))

    order = (LARGE, SMALL, MINI) if large else (SMALL, LARGE, MINI)
    icon = None
    for size in order:
        icon = suite_icon(get, size)
        if icon is not None:
            break
    if icon is None:
        one = info.by_id.get((b"ICON", resid))
        if one is not None:
            icon = bitmap_icon(one, LARGE if large else SMALL, masked=False)
    if icon is None:
        return None
    rows = _bitmap(icon)
    if not large and len(rows[0]) > 16:
        rows = [row[::2] for row in rows[::2]]
    return tuple(rows)


def _info_for_fork(fork: bytes) -> AppInfo | None:
    if not fork:
        return None
    return _app_info(fork)


def _color_for(info: AppInfo, resid: int | None, size: tuple[int, int],
                depth: int):
    """The colour icon family at resid in info's fork, for depth; None
    without one."""
    if info is None or resid is None:
        return None

    def get(kind: str) -> bytes | None:
        return info.by_id.get((kind.encode("ascii"), resid))

    return color_suite_icon(get, size, depth)


def declared_color_icon(f: MacFile, *, large: bool = False, depth: int = 1):
    """The colour icon f's own bundle resources declare for its own
    type, for depth; None without one."""
    info = _info_for_fork(bytes(f.rsrc) if f.rsrc else b"")
    size = LARGE if large else SMALL
    return _color_for(info, info.icon_resource_id(f.type) if info else None,
                      size, depth)


def associated_color_icon(f: MacFile, *, large: bool = False, depth: int = 1):
    """The colour icon the creating app declares for f's type, for
    depth; None without one."""
    app = associated_app(f)
    if app is None:
        return None
    info = _info_for_fork(bytes(app.rsrc))
    size = LARGE if large else SMALL
    return _color_for(info, info.icon_resource_id(f.type) if info else None,
                      size, depth)


def associated_icon_rows(f: MacFile, *, large: bool = False) -> tuple[str, ...] | None:
    """The rows ('#'/'.') of the icon the creating app declares for f's
    type, through its BNDL/FREF resources; None without one."""
    app = associated_app(f)
    if app is None:
        return None
    info = _info_for_fork(bytes(app.rsrc))
    return None if info is None else _rows_for(info, info.icon_resource_id(f.type), large)


def declared_icon_rows(f: MacFile, *, large: bool = False) -> tuple[str, ...] | None:
    """The rows ('#'/'.') of the icon f's own bundle resources declare
    for f's own type: an extension's or a control panel's family, which
    the Finder shows for the file itself; None without one."""
    info = _info_for_fork(bytes(f.rsrc) if f.rsrc else b"")
    return None if info is None else _rows_for(info, info.icon_resource_id(f.type), large)


def _bitmap(icon) -> list[str]:
    """An icon as '#' and '.' rows, only where its mask lets it show."""
    mask = icon.mask or [b"\xFF" * icon.row_bytes] * icon.height
    return ["".join("#" if irow[x >> 3] & 0x80 >> (x & 7) and mrow[x >> 3] & 0x80 >> (x & 7)
                    else "." for x in range(icon.width))
            for irow, mrow in zip(icon.rows, mask)]
