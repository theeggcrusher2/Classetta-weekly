# Follow-up tasks

Identified while closing out the Filer implementation plan and afterwards.
One open task when the plan closed; several more since. The point is the
user-facing behavior, not the internal design.

## 1. Disk Tracker: open what the index recorded, or offer to locate it

A `.dtc`/`.dt1` opened in the Filer is a read-only record: a file row
offers Get Info only, and a double-click says the item is recorded in the
index rather than stored in it. That is honest but dead-ended. A
double-click on a file row should instead attempt to load the media the
index recorded, and when that media is not where the index says it was,
prompt the user to insert the disk or locate the original image.

Acceptance:

- A double-click, or File ▸ Open, on a file in a Disk Tracker window
  attempts to resolve and mount the recorded source. Mounting an image
  goes through the existing disk-image path
  (`macrt.macfile.image_volume`, `open_targets`), not a new reader.
- When the resolved path does not exist or does not open, a prompt offers
  to insert the disk or pick the original image (a file picker). Choosing
  an image that verifies against the record mounts it and opens the same
  item, as though the disk had been there all along.
- Cancelling leaves the index untouched — it stays a read-only record;
  this task adds resolution, never mutation of the index.
- Tests: a catalog built from a generated image, with the image moved
  away → the prompt appears; the picked image → opens; Get Info is
  unchanged. Fixtures are generated, never external recorded material, and
  no test reads from outside the repository.

Open questions, to be investigated before designing further:

- Catalogs macrt builds carry no source path: `index_images` never sets a
  disk record's `suffix`. Real-world DT2 catalogs may hold the mount path
  there — the parser keeps it as `disk.suffix` (see `_find_first_item`:
  "mount path, source location, network info") — but the encoding is
  unverified, and DT1 records expose nothing comparable.
- If the suffix proves unusable, the fallback is matching on the disk's
  volume name against known mount points, or asking via the picker, with
  a clear message when nothing matches.

## 2. BIN/CUE support

BIN/CUE is how a great deal of the CD-ROM software in circulation was
imaged — one sheet, a data track, and (for CDs that had them) audio
tracks. Today the sheet is never read: a `.bin` keeping 2352-byte
sectors whole opens through the raw-CD path whatever its name
(`docs/titles.md` documents several, data track first at 00:02:00), and
a plain 2048-byte data image opens as HFS or ISO 9660 through the
existing fallback. The sheet buys nothing — no track split, no audio
awareness, no validation of what a track's mode is, and a
one-file-per-track layout (separate files per audio track) is
unsupported too. Interleaved subchannel at 2448 bytes a sector is read
for `.mdf`/`.mds` pairs but not for `.bin`. Audio tracks are never
played, the same as for every other format here.

What it looks like when done:

- A `.cue` sheet beside its sibling `.bin` — or a `.bin` with a `.cue`
  beside it — is a first-class image: Mode 1 and Mode 2 Form 1 data
  tracks, with any audio tracks present but never exposed to the running
  program. The data track's sectors mount as a CD-ROM through the existing
  path, exactly as `.mdf`/`.mds` mounts today (see the `.mdf`/`.mds` row
  in `docs/disk-images.md`), so the running program can't tell the image
  format apart from any other.
- Multiple data tracks, DVD structure, Mode 2 Form 2 / raw sectors, or a
  missing or mismatching `.bin` are refused by name, the way `.mdf`
  refuses its cases today.
- Fixtures are generated — an HFS volume as the data track, plus one
  silent audio track, in a pair written by the test tooling. No external
  material, nothing read from outside the repository.
- `docs/disk-images.md` moves BIN/CUE from the Unsupported list to the
  supported table, with the refusal list spelled out as `.mdf`'s is.

Open questions, to settle before implementing:

- A bare `.bin` with no sheet: refuse until the `.cue` turns up, or open
  it with a clear "it is unknown what disc this is" message? `.mdf`
  requires both halves today.
- One `.bin` holding all tracks (the common shape for a data disc) and
  one `.bin` per track — both in scope, or the multi-file shape first?
- Whether the pair is discovered from the sheet alone; an
  ISO-9660-native disc inside `.bin`/`.cue` already browses today via the
  ISO fallback in `load_volume`, and a UDF-native one will not until
  task 3 lands — decide the order of the two tasks before writing
  refusal messages that assume HFS inside the data track.

## 3. UDF and Rock Ridge support

ISO 9660 itself is read today — an earlier draft of this task said it
wasn't, which was wrong. `load_volume` (`macrt/macfile.py`) falls back to
`disktracker.index._read_iso` when the HFS readers fail, and the Filer
browses an ISO-only disc's files through it: `.iso`, `.cdr`, `.toast` and
raw-sector CDs with no Mac volume open as a mount holding the files the
directories list (`tests/test_files.py`, `test_an_iso_9660_disc`). The
ISO reader is `_read_iso` / `_iso_children` in `disktracker/index.py` —
latin-1 names with `;1` stripped, no SUSP, no Rock Ridge. Two gaps
remain:

- **Rock Ridge** (SUSP; the NM entries that carry the real names) is not
  parsed. A disc mastered with Rock Ridge shows its ISO 9660 names —
  `HELLO.TXT`, uppercase, without `;1` — not the names the disc was
  created with.
- **UDF** (ECMA-167 / ISO/IEC 13346) — common on DVDs carrying no Mac
  volume — is not parsed anywhere. A UDF-only disc mounts as a CD-ROM
  with nothing to browse.

What it looks like when done:

- The ISO reader parses SUSP: an NM entry gives the entry's real name
  (case and long names kept), falling back to the ISO 9660 name field
  when a record carries none. Both the Filer's fallback and the Disk
  Tracker's catalogs get this from the one reader.
- A UDF reader — volume descriptors, file set, directory tree — gives
  the same view: names, sizes, dates, files read on demand.
- Both leave the Filer browsing with no special cases, and both leave
  HFS/HFS+ precedence alone (an HFS disc is read as HFS, as today).
- Fixtures are generated — the ISO fixture in `disktracker/tests/test_index.py`
  is the model for the ISO side, and a UDF fixture built byte by byte the
  same way; no external images, nothing read from outside the repository.
- `docs/disk-images.md` updated for whatever lands; the same pass should
  fix its stale claim that ISO 9660 is "not parsed".

Open questions, to settle before implementing:

- The ISO fallback lives in macrt but imports upward from disktracker
  (`from disktracker.index import _read_iso`, inside `load_volume`) — the
  wrong direction for a library. Task 4 already wants the Disk Tracker to
  reuse macrt's readers; moving `_read_iso` into macrt (or a shared
  location) alongside it gives one reader for both callers — decide the
  two tasks together.
- How far SUSP parsing goes: NM only, or NM plus CL/PL/RE (relocated
  deep directories)? NM-only is the 90% case; CL/PL/RE is what discs with
  paths deeper than 8 ISO levels need.
- UDF versions: how far up the ECMA-167 ladder the reader goes before
  refusing by name; whether packed-sector DVDs are in scope.

## 4. Reuse macrt.hfsplus for the Disk Tracker's duplicated walk

`disktracker/index.py` carries its own copy of the HFS+ catalog walk —
`_read_hfsplus` (line 274) and its `_hfsplus_*` helpers (lines 146–273) —
duplicating what `macrt/hfsplus.py` `read_hfsplus` (line 147) does. The
HFS walk (`_read_hfs`, line 52) duplicates `macrt/macfile.py` `_read_hfs`
(line 857) the same way. `macrt/macfile.py` itself still carries a dead
`_read_hfs_plus` (line 989); nothing calls it since lines 1179–1180
switched to `macrt.hfsplus.read_hfsplus`. The canonical readers are the
ones the running emulator uses; the tracker's copies exist because it
needs name, dates, type/creator, size and invisibility, and no forks.

What it looks like when done:

- The tracker reads images through the canonical readers and translates
  what they return into the dict tree `_emit` consumes (the volume's
  depth-1 children, depth set, pre-order); nothing about the catalog
  changes — same tree shape, same dates, same invisibility rule.
- The dead `_read_hfs_plus` in `macrt/macfile.py` is removed.
- The invisible-entry filtering matches the Filer's view exactly: the
  tracker's catalog shows no invisible entries, just as the Filer shows
  none.
- `_read_iso` stays as the shared ISO reader — the Filer's fallback in
  `load_volume` imports it from here today; task 3 may move it into
  macrt so macrt stops importing upward from disktracker.
- `disktracker/tests/test_index.py` stays green; no new fixtures needed
  beyond generated ones.

Open questions:

- Where the dict translation lives: disktracker as a local adapter, or a
  shared helper in macrt both sides use. Task 3 will want the same shape
  for ISO/UDF, which argues for macrt; the tracker arguing for keeping
  macrt at "a Volume is a Volume" argues against.
- Whether the HFS copy moves the same way — the HFS duplication is
  byte-for-byte modulo two helpers, so reusing `macrt/macfile.py`
  `_read_hfs` drops it for free; a generated HFS fixture is needed either
  way.

## 5. Colour icons in the window titles, the About Classetta app list, and the large machine logo

At depth > 2, chrome draws `title_pixmap` instead of `title_icon` (
`chrome.Frame`, `macrt/ui/chrome.py`; the callers gate on depth, chrome
itself does not). Get Info and the Content Viewer already build a colour
pixmap via `item_icon_qicon` and pass it. Three other surfaces still draw
1-bit:

- **Filer window titles.** `FilerWindow.frame` (`macrt/ui/filer.py` line
  577) passes only `_title_icon`, always 1-bit — the generic disk,
  folder, archive, document and catalog icons from `CONTAINER_ICONS`
  (1-bit only), or a custom volume icon through `_container_icon`
  (`_custom_icon`, 1-bit only). Get Info's windows already title colour
  at depth > 2 in the same app.
- **The About Classetta app list.** Application rows draw colour at
  depth > 2 (`_fork_color_icon`, `macrt/ui/filer.py` line 1654), but the
  System row is always `theme.GENERIC_APP_ICON` with no pixmap — 1-bit
  at any depth.
- **The large machine logo.** `theme.MAC_II_ICON` when the model's name
  is "Macintosh II", `theme.MAC_PLUS_ICON` otherwise — both 1-bit,
  drawn via `theme.draw_bitmap`; selection is a name substring match,
  so IIx and IIci fall back to the Plus logo, and both constants are the
  only two logos that exist.

What it looks like when done:

- Filer window titles: at depth > 2, titles carry colour icons — the
  container's own colour icon where it has one (as Get Info's title does,
  via `item_icon_qicon` / `_fork_color_icon`), and colour stand-ins for
  the generic disk, folder, archive, document and catalog icons; 1-bit
  at depth ≤ 2, and when colour can't be built, with no other behavior
  changed.
- The About Classetta app list: the System row draws in colour at
  depth > 2 like the application rows do — source of that art (below)
  resolved as part of the work.
- The About Classetta logo: one colour logo per `MODELS` entry (`plus`,
  `ii`, `iix`, `iici`), selected by model id, not by name substring;
  IIx and IIci get their own art; 1-bit stays at depth ≤ 2 and as the
  fallback. Each 1-bit constant gains its colour sibling rather than
  being replaced.
- Machine icon additions for new machine types: nothing may grow
  `MODELS` without a logo. Add a test that fails when a `MODELS` entry
  has no About logo.
- Tests: paint smoke tests at depth 8 for each surface (a pixmap is
  handed to chrome, or the rows draw one); depth 1 unchanged; the
  `MODELS` test above; About opens on every model in `MODELS`.

Open questions:

- Where the colour generic art comes from: new bundled assets with
  recorded licensing (like `fonts/`), a colour map applied to the 1-bit
  art at draw time, or runtime-generated art. The generic disk, folder,
  archive, document and catalog icons have no colour counterparts
  anywhere today.
- The System row's colour source: the running System file's own icon —
  it is a Mac file with an icon, like any other — or a bundled stand-in.
- Whether Disk Tracker catalog titles (`CATALOG_ICON`) get colour art on
  the same rule, and whether the viewer windows' 16-pixel pixmaps set the
  size for all of this.

Note: `AboutClassettaWindow` is built with no title icon ("Build the
window - no title icon"); that stays as it is unless the work says
otherwise.
