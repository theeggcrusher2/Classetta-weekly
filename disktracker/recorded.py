"""Resolution of the media a Disk Tracker record points at (subtask 1.2).

A .dtc/.dt1 is a read-only record: a double-click on an item must find
the media the index recorded, or say so.  This module decides whether
the recorded source can be found, and where; mounting what it finds (or
browsing an already-mounted volume) is subtask 1.4's job.

Resolution order:

1. Suffix candidates (DT2 only; DT1 carries no suffix and falls through):
   a. the recorded segment-A ``13 00`` mount path, when it exists and
      opens (a listable directory — a mounted volume — or an image
      file macrt opens as a disk image);
   b. the reconstructed source-image path: join(segment-B ``13 00``
      mount point, segment-B ``12 00`` payload with any leading slash
      stripped).  Both a ``12 00`` payload with and without a leading
      slash are tried, and the mount point joined.  First candidate
      that opens wins;
   c. otherwise keep the recorded image basename (segment B) and the
      recorded volume name as match keys for steps 2 and 3.  Do not
      fail yet.
2. Volume-name match: normalize names (colon↔slash, case-insensitive,
   collapse whitespace) and search common mount roots for a
   directory/volume whose basename matches the recorded volume name —
   /Volumes (macOS), /media, /run/media, /mnt, and $HOME (depth-limited
   ~2, hidden directories skipped; the whole disk is never scanned).
   The roots are injectable for tests.  A match must open before it is
   accepted.
3. Nothing resolved: ``RecordedResolution.path`` is None and the caller
   reports failure to the prompt (subtask 1.3).  ``image_basename`` is
   exposed so the picker can offer it as a filter hint.

DT2 suffix grammar (as verified privately in subtask 1.1): a tagged
alias blob of up to two segments — segment A: mounted-volume info
(``10 00``/``11 00`` HFS dates; ``0e 00``/``0f 00`` UTF-16BE volume
name; ``12 00 <len:1> <path>`` item path relative to its volume, empty
for the volume alias itself; ``13 00 <len:1> <path>`` the volume's
POSIX mount path at index time, e.g. ``/Volumes/Developer CD`` or
``/``).  Segment B, present for image-sourced disks, is a second tag
stream with classic colon paths (``02 00``), the source image file's
basename (.toast/.iso/.cdr) and *its* volume's mount point (``13 00``)
and the image's path relative to that volume (``12 00``, usually no
leading slash).  UTF-8 payloads; curly apostrophes appear (E2 80 99).

Traps encoded below; worth recording for whoever touches this next:

- ``\\x13\\x00`` false-positives inside UTF-16BE name payloads (a name
  containing U+1300 contains exactly those bytes).  Every tag is
  anchored on the tag grammar — two tag bytes, a one-byte length, a
  payload that decodes and starts the right way (``/`` for a mount
  path) — never a bare ``find(b"\\x13\\x00")``.
- The ``0e 00``/``0f 00`` name tags' length encoding is not confirmed;
  they are read with the same one-byte-length layout as their sibling
  tags and accepted only if the payload decodes as printable
  UTF-16BE with at least one letter or digit.  If that fails the name
  is simply not available from the suffix and the disk record's own
  name is used instead.
- The recorded paths reflect the machine that built the catalog.  They
  are a candidate generator, never truth: every candidate is verified
  against the filesystem before it is accepted.
- DT1 (.dt1) suffixes are always empty.  No source information exists
  anywhere in the format; the volume name and dates are all it knows.
"""

import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("disktracker.recorded")

_TAG_CLASSIC = b"\x02\x00"   # colon-delimited (Classic Mac OS) path; opens segment B
_TAG_ITEM = b"\x12\x00"      # item path relative to its volume (empty for the volume alias)
_TAG_MOUNT = b"\x13\x00"     # the volume's POSIX mount path at index time
_TAG_NAMES = (b"\x0e\x00", b"\x0f\x00")   # UTF-16BE names, first plausible is the volume's

_TWO_BYTE_TAGS = frozenset((_TAG_CLASSIC, _TAG_ITEM, _TAG_MOUNT, *_TAG_NAMES))
_SCAN_LIMIT = 1 << 16        # a suffix is a few KB; never scan an unbounded blob


# ---------------------------------------------------------------- suffix parsing

@dataclass
class SuffixInfo:
    """What the suffix records about the source of the indexed media."""
    mount_path: str | None = None       # segment A: the volume's mount path
    image_mount: str | None = None      # segment B: mount point of the volume the image is on
    image_relpath: str | None = None    # segment B: image path relative to that volume
    image_basename: str | None = None   # segment B: the source image file's basename
    volume_name: str | None = None      # from either segment's name tag


def _decode_path(raw: bytes) -> str | None:
    """A path payload as text, or None when it is not one.

    Payloads are UTF-8 (curly apostrophes included); mac_roman is the
    fallback for older writers.  ASCII control bytes disqualify it: a
    real path never carries NULs, and bytes from an unrelated part of
    the blob (e.g. a UTF-16BE name) almost always do.
    """
    if not raw:
        return ""
    if any(b < 0x20 for b in raw):
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode("mac_roman")
        except UnicodeDecodeError:
            return None


def _tags(blob: bytes):
    """Yield (tag, offset, payload) for every grammar-shaped tag in blob.

    Tags that fail validation are skipped over one byte at a time, so a
    tag nested inside rejected junk can still surface.
    """
    n = min(len(blob), _SCAN_LIMIT)
    i = 0
    while i + 3 <= n:
        two = blob[i:i + 2]
        if two in _TWO_BYTE_TAGS:
            ln = blob[i + 2]
            end = i + 3 + ln
            if end <= n:
                yield two, i, blob[i + 3:end]
                i = end
                continue
        i += 1


def _path_tags(blob: bytes):
    """Validated (tag, payload) pairs for the path tags, in blob order."""
    out = []
    for tag, _off, payload in _tags(blob):
        if tag == _TAG_ITEM:
            s = _decode_path(payload)          # empty payload is legal (the volume alias)
        elif tag == _TAG_MOUNT:
            s = _decode_path(payload)
            if s is not None and not s.startswith("/"):
                s = None                        # a mount path is absolute, always
        else:
            continue
        if s is not None:
            out.append((tag, s))
    return out


def _name_tags(blob: bytes) -> list[str]:
    out = []
    for tag, _off, payload in _tags(blob):
        if tag not in _TAG_NAMES or len(payload) < 2 or len(payload) % 2:
            continue
        try:
            s = payload.decode("utf-16-be")
        except UnicodeDecodeError:
            continue
        if any(unicodedata.category(c).startswith("C") for c in s):
            continue                            # control characters: not a name
        if not any(c.isalnum() for c in s):
            continue
        out.append(s)
    return out


def _classic_basename(blob: bytes) -> str | None:
    for tag, _off, payload in _tags(blob):
        if tag != _TAG_CLASSIC:
            continue
        s = _decode_path(payload)
        if s and ":" in s:
            base = s.rstrip(":").split(":")[-1]
            if base:
                return base
    return None


def parse_suffix(blob: bytes) -> SuffixInfo:
    """Split a DT2 suffix into its segments and release their keys.

    Segment B is located by the first colon-delimited classic path tag;
    everything before it is segment A.  Without one there is no source
    image and the blob is treated as segment A alone.
    """
    info = SuffixInfo()
    if not blob:
        return info
    tags = list(_tags(blob))
    split = next((off for tag, off, payload in tags
                  if tag == _TAG_CLASSIC and b":" in payload), None)
    seg_a = blob[:split] if split is not None else blob
    seg_b = blob[split:] if split is not None else b""

    paths_a = _path_tags(seg_a)
    paths_b = _path_tags(seg_b)
    mounts_a = [s for tag, s in paths_a if tag == _TAG_MOUNT]
    items_a = [s for tag, s in paths_a if tag == _TAG_ITEM]
    mounts_b = [s for tag, s in paths_b if tag == _TAG_MOUNT]
    items_b = [s for tag, s in paths_b if tag == _TAG_ITEM]

    info.mount_path = mounts_a[0] if mounts_a else None
    info.image_mount = mounts_b[0] if mounts_b else None
    # An empty segment-A item path is the volume alias itself; the image
    # path is segment B's, never segment A's.
    info.image_relpath = next((s for s in items_b), None)

    names = _name_tags(seg_a) + _name_tags(seg_b)
    info.volume_name = names[0] if names else None

    if info.image_relpath:
        rel = info.image_relpath.replace(":", "/")
        info.image_basename = rel.rstrip("/").split("/")[-1] or None
    if info.image_basename is None:
        info.image_basename = _classic_basename(seg_b)
    return info


# ------------------------------------------------------------ resolution keys

def _normalize(name: str) -> str:
    s = name.replace(":", "/").casefold()
    return re.sub(r"\s+", " ", s).strip()


def _rel_join(mount: str, rel: str) -> str:
    return str(Path(mount) / rel.lstrip("/"))


def source_hint(suffix: bytes, disk_name: str | None = None) -> tuple[str | None, str | None]:
    """(volume name, image basename) without touching the filesystem.

    The picker uses the basename as a filter hint; the volume name is
    the step-2 match key.  The disk record's own name is the fallback
    for the volume name (DT1, or a suffix with no readable name tag).
    """
    info = parse_suffix(suffix or b"")
    return (info.volume_name or disk_name), info.image_basename


def image_candidates(info: SuffixInfo) -> list[str]:
    """Plausible source-image paths, best first; first that opens wins."""
    out: list[str] = []
    rel = info.image_relpath
    if rel:
        if rel.startswith("/"):
            out.append(rel)                     # recorded absolute: try as-is first
        if info.image_mount:
            out.append(_rel_join(info.image_mount, rel))
    elif info.image_mount and info.image_basename:
        out.append(_rel_join(info.image_mount, info.image_basename))
    seen, unique = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


# ------------------------------------------------------------------ verifier

def _head_is_image(head: bytes, size: int) -> bool:
    """Cheap pre-probe so a non-image file is never read whole."""
    from macrt.macfile import CD_SECTOR, CD_SYNC, is_disk_image
    if is_disk_image(head):
        return True
    # A raw-CD image's sectors are a multiple of CD_SECTOR with sync at
    # sector 0; a truncated head read is not a multiple, so is_disk_image
    # alone cannot see it.
    return (size >= CD_SECTOR and size % CD_SECTOR == 0
            and head[:12] == CD_SYNC)


def default_opener(path: Path) -> bool:
    """True when the recorded media is present at path.

    A directory that lists is a mounted volume: macrt browses host
    folders as volumes, so the media is there — it needs no mounting.
    Otherwise path must be a file macrt opens as a disk image (a head
    probe first, so a large non-image candidate is not read whole).
    """
    try:
        if path.is_dir():
            with os.scandir(path):
                return True
        if not path.is_file():
            return False
        size = path.stat().st_size
        with path.open("rb") as fh:
            head = fh.read(1 << 20)
        if not _head_is_image(head, size):
            return False
        from macrt.macfile import open_container
        open_container(path)
        return True
    except Exception:
        return False


def _kind_of(path: Path) -> str:
    return "volume" if path.is_dir() else "image"


# ----------------------------------------------------------------- resolution

@dataclass
class RecordedResolution:
    """Where the recorded media is, or None when it was not found.

    path: the directory of an already-mounted volume, or the image file
    to mount.  kind: 'volume' (browse as-is) or 'image' (1.4 mounts).
    source: which step found it — 'suffix-mount', 'suffix-image' or
    'volume-name'.  volume_name and image_basename are the step-3 match
    keys, carried through even on failure.
    """
    path: Path | None = None
    kind: str | None = None             # 'volume' | 'image'
    source: str | None = None           # 'suffix-mount' | 'suffix-image' | 'volume-name'
    volume_name: str | None = None
    image_basename: str | None = None

    @property
    def resolved(self) -> bool:
        return self.path is not None


def _default_roots(home: Path | None) -> list[Path]:
    home = home if home is not None else Path.home()
    return [Path("/Volumes"), Path("/media"), Path("/run/media"), Path("/mnt"), home]


def _name_matches(roots: list[Path], want: str):
    """Directories under roots whose normalized basename matches want.

    Depth-limited to 2 (so /media/<user>/<volume> and ~/dir/<volume> are
    found), hidden directories skipped.  The whole disk is never scanned.
    """
    for root in roots:
        try:
            if not root.is_dir():
                continue
            level1 = list(root.iterdir())
        except OSError:
            continue
        for entry in level1:
            yield from _check_entry(entry, want)
            if entry.is_dir() and not entry.name.startswith("."):
                try:
                    level2 = list(entry.iterdir())
                except OSError:
                    continue
                for sub in level2:
                    yield from _check_entry(sub, want)


def _check_entry(entry: Path, want: str):
    if entry.name.startswith("."):
        return                              # hidden: skip
    try:
        if not entry.is_dir():
            return
    except OSError:
        return
    if _normalize(entry.name) == want:
        yield entry


def resolve_recorded_media(suffix: bytes | None, *, disk_name: str | None = None,
                           mount_roots: list[Path] | None = None,
                           home: Path | None = None,
                           opener=None) -> RecordedResolution:
    """Where the recorded media is, per the 1.2 order.  Never raises."""
    try:
        info = parse_suffix(suffix or b"")
    except Exception:
        log.debug("suffix did not parse", exc_info=True)
        info = SuffixInfo()
    volume_name = info.volume_name or disk_name
    basename = info.image_basename
    res = RecordedResolution(volume_name=volume_name, image_basename=basename)
    opener = opener or default_opener

    # 1a: the recorded mount path, if it opens.
    if info.mount_path:
        cand = Path(info.mount_path)
        try:
            if opener(cand):
                res.path, res.kind, res.source = cand, _kind_of(cand), "suffix-mount"
                return res
        except Exception:
            log.debug("suffix mount path %r did not open", info.mount_path, exc_info=True)

    # 1b: the reconstructed source-image path(s).
    for cand_s in image_candidates(info):
        cand = Path(cand_s)
        try:
            if opener(cand):
                res.path, res.kind, res.source = cand, _kind_of(cand), "suffix-image"
                return res
        except Exception:
            log.debug("suffix image candidate %r did not open", cand_s, exc_info=True)

    # 1c: nothing from the suffix — keep the keys and fall through.

    # 2: a volume under a common mount root named like the recorded one.
    want = _normalize(volume_name) if volume_name else ""
    if want:
        roots = mount_roots if mount_roots is not None else _default_roots(home)
        for cand in _name_matches(roots, want):
            try:
                if opener(cand):
                    res.path, res.kind, res.source = cand, "volume", "volume-name"
                    return res
            except Exception:
                log.debug("volume-name match %r did not open", cand, exc_info=True)

    # 3: unresolved — the caller shows the prompt.
    log.debug("recorded media unresolved (volume=%r image=%r)", volume_name, basename)
    return res
