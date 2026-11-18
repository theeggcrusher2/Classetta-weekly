# Follow-up tasks

Identified while closing out the Filer implementation plan and afterwards.
The point is the user-facing behavior, not the internal design. This file
carries its own rules, below, which hold for every task in it — for this
project, going forward.

## Rules

Standing rules for this project. They apply to every task here and to any
follow-up work this file spawns.

- **Small, low-context subtasks.** Every task is broken into subtasks a
  background agent can pick up with only the subtask's text and the
  repository. A subtask carries its own file references, its own
  acceptance check, and its own explicit dependencies. It does not
  require reading the whole file first — though reading the task's
  background is always allowed.
- **Concurrent unless ordered.** Subtasks with no shared files, no
  ordering constraint and no open question between them may run in
  parallel, in the background, each reporting independently. Only a
  subtask marked *blocked by* waits.
- **Tick as they land.** Change `- [ ]` to `- [x]` in the same commit that
  lands the subtask. Tasks and this file are dropped when everything in
  them is done — like the Filer plan, which was removed when it closed.
- **Open questions stop the subtask.** A subtask that meets an open
  question stops there and asks, rather than deciding silently. The
  question is then settled in the task text, and the subtask resumes.
- **Invariants are load-bearing.** Where a task names an invariant, a
  subtask that breaks it is not done, however else it looks finished.
- **Generated fixtures only.** All fixtures are generated or in-memory.
  `dt2/` (real ADC catalogs) is never committed, never wired into tests,
  never referenced from outside the repository, and never copied anywhere.
- **Push only with approval.** Local commits are fine; `git push` asks
  the user first.

## How to read a task

Each task has background (what and why, with file references) and
subtasks (the work, as checkboxes). Subtasks are numbered `<task>.<step>`.
Dependencies are named, never implied. If a subtask says *report only*,
it produces an analysis for the user to decide on, not a code change.

Subtasks 1.3 and 2.x and 5.1 have no ordering against anything; they may
start immediately. `_read_iso`'s placement is decided jointly by tasks
3 and 4 — neither starts 3.1/4.3 until it's settled.

---

## 1. Disk Tracker: open what the index recorded, or offer to locate it

Background: a `.dtc`/`.dt1` opened in the Filer is a read-only record: a
file row offers Get Info only, and a double-click says the item is
recorded in the index rather than stored in it. That is honest but
dead-ended. A double-click on a file row should instead attempt to load
the media the index recorded, and when that media is not where the index
says it was, prompt the user to insert the disk or locate the original
image.

Mounting an image goes through the existing disk-image path
(`macrt.macfile.image_volume`, `open_targets`), not a new reader.
Cancelling leaves the index untouched — resolution only, never mutation
of the index. Fixtures are generated; no test reads from outside the
repository.

Subtasks:

- [x] **1.1** *Report only.* Verify what DT2/DT1 catalogs record about a
      disk's origin, privately. The parser keeps a disk record's
      `suffix` (see `_find_first_item` in `disktracker/disktracker.py`: "mount
      path, source location, network info") but macrt's own `index_images`
      never sets it. Look at `dt2/` — never copy or commit any of it —
      decode what `suffix` holds in real catalogs, and record findings for
      the user. If `dt2/` is absent, say so and stop. Does not decide
      anything; feeds 1.2.
- [x] **1.2** Define the resolution order in the Filer adapter
      (`disktracker/filer_adapter.py`): the recorded suffix (if 1.1
      verifies it) → the disk's volume name matched against known mount
      points → ask the user. *Blocked by 1.1.*
- [x] **1.3** The unresolved prompt: when the resolved path does not
      exist or does not open, offer to insert the disk or pick the
      original image (a file picker). Cancelling does nothing; the index
      stays untouched. Independent of 1.1; can run alongside.
- [x] **1.4** Mounting: a picked image that verifies against the record
      mounts as though the disk had been there all along, and opens the
      same item the double-click was on. Uses
      `macrt.macfile.image_volume`/`open_targets`. *Blocked by 1.2 and
      1.3.*
- [x] **1.5** Tests: a catalog built from a generated image, with the
      image moved away → the prompt appears; the picked image → opens;
      Get Info unchanged. *Blocked by 1.4.*

Open questions, to be settled before designing further:

- If the suffix proves unusable, the fallback is matching on the disk's
  volume name against known mount points, or asking via the picker, with
  a clear message when nothing matches. (1.1 answers this.)

---

## 2. BIN/CUE support

Background: BIN/CUE is how a great deal of the CD-ROM software in
circulation was imaged — one sheet, a data track, and (for CDs that had
them) audio tracks. Today the sheet is never read: a `.bin` keeping
2352-byte sectors whole opens through the raw-CD path whatever its name
(`docs/titles.md` documents several, data track first at 00:02:00), and a
plain 2048-byte data image opens as HFS or ISO 9660 through the existing
fallback. The sheet buys nothing — no track split, no audio awareness, no
validation of what a track's mode is, and a one-file-per-track layout
(separate files per audio track) is unsupported too. Interleaved
subchannel at 2448 bytes a sector is read for `.mdf`/`.mds` pairs but not
for `.bin`. Audio tracks are never played, the same as for every other
format here.

What it looks like when done: a `.cue` sheet beside its sibling `.bin` —
or a `.bin` with a `.cue` beside it — is a first-class image: Mode 1 and
Mode 2 Form 1 data tracks, with any audio tracks present but never
exposed to the running program. The data track's sectors mount as a
CD-ROM through the existing path, exactly as `.mdf`/`.mds` mounts today
(see the `.mdf`/`.mds` row in `docs/disk-images.md`), so the running
program can't tell the image format apart from any other.

Open questions, to settle before 2.3 — stop and ask:

- A bare `.bin` with no sheet: refuse until the `.cue` turns up, or open
  it with a clear "it is unknown what disc this is" message? `.mdf`
  requires both halves today.
- One `.bin` holding all tracks (the common shape for a data disc) and
  one `.bin` per track — both in scope, or the multi-file shape first?
- Whether the pair is discovered from the sheet alone; an
  ISO-9660-native disc inside `.bin`/`.cue` already browses today via the
  ISO fallback in `load_volume`, and a UDF-native one will not until task
  3 lands — decide the order of the two tasks before writing refusal
  messages that assume HFS inside the data track.

Subtasks:

- [x] **2.1** `macrt/cue.py`: a parser for the cue sheet, tested against
      hand-written cue bytes (FILE/TRACK/INDEX lines, one FILE per track,
      and the single-FILE-many-TRACKs shape). Reports sheet structure;
      performs no disk I/O.
- [x] **2.2** The BIN reader, modelled on `macrt/mdf.py`: the data
      track's 2048-byte user sectors, Mode 1 and Mode 2 Form 1; refuse
      DVD structure, Mode 2 Form 2 / raw sectors, and multiple data
      tracks by name, the way `.mdf` refuses its cases today.
- [x] **2.3** Detection and wiring in `macrt/macfile.py`: a `.cue` sheet
      discovers its sibling `.bin` (and nested pairs of the `_mdf_pair`
      kind), mounts the data track through the CD-ROM path. *Blocked by
      2.1, 2.2 and the open questions above.*
- [x] **2.4** Fixtures are generated — an HFS volume as the data track,
      plus one silent audio track, in a pair written by the test tooling;
      refusal cases tested by name. *Blocked by 2.3.*
- [x] **2.5** `docs/disk-images.md` moves BIN/CUE from the Unsupported
      list to the supported table, with the refusal list spelled out as
      `.mdf`'s is. *Blocked by 2.3.*

---

## 3. UDF and Rock Ridge support

Background: ISO 9660 itself is read today — an earlier draft of this task
said it wasn't, which was wrong. `load_volume` (`macrt/macfile.py`) falls
back to `disktracker.index._read_iso` when the HFS readers fail, and the
Filer browses an ISO-only disc's files through it: `.iso`, `.cdr`,
`.toast` and raw-sector CDs with no Mac volume open as a mount holding
the files the directories list (`tests/test_files.py`,
`test_an_iso_9660_disc`). The ISO reader is `_read_iso` / `_iso_children`
in `disktracker/index.py` — latin-1 names with `;1` stripped, no SUSP, no
Rock Ridge. Two gaps remain:

- **Rock Ridge** (SUSP; the NM entries that carry the real names) is not
  parsed. A disc mastered with Rock Ridge shows its ISO 9660 names —
  `HELLO.TXT`, uppercase, without `;1` — not the names the disc was
  created with.
- **UDF** (ECMA-167 / ISO/IEC 13346) — common on DVDs carrying no Mac
  volume — is not parsed anywhere. A UDF-only disc mounts as a CD-ROM
  with nothing to browse.

Both leave HFS/HFS+ precedence alone (an HFS disc is read as HFS, as
today). Fixtures are generated — the ISO fixture in
`disktracker/tests/test_index.py` is the model for the ISO side, and a
UDF fixture built byte by byte the same way; no external images, nothing
read from outside the repository.

Open questions, to settle before implementing:

- The ISO fallback lives in macrt but imports upward from disktracker
  (`from disktracker.index import _read_iso`, inside `load_volume`) — the
  wrong direction for a library. Moving `_read_iso` into macrt (or a
  shared location) alongside task 4 gives one reader for both callers —
  **decided jointly with task 4**, see the note before task 4's subtasks.
- How far SUSP parsing goes: NM only, or NM plus CL/PL/RE (relocated deep
  directories)? NM-only is the 90% case; CL/PL/RE is what discs with
  paths deeper than 8 ISO levels need. Decide before 3.2.
- UDF versions: how far up the ECMA-167 ladder the reader goes before
  refusing by name; whether packed-sector DVDs are in scope. Decide
  before 3.4.

Subtasks:

- [x] **3.1** *Decide and move `_read_iso`.* Move `disktracker.index`'s
      `_read_iso`/`_iso_children` into macrt (shared location or
      `macrt/macfile.py`), both callers importing downward. *Blocked by
      the joint task-3/4 decision; coordinate with 4.3.*
- [x] **3.2** SUSP parsing in the ISO reader: an NM entry gives the
      entry's real name (case and long names kept), falling back to the
      ISO 9660 name field when a record carries none. CL/PL/RE parsing
      included only if the open question settles that way. *Blocked by
      the SUSP open question.*
- [x] **3.3** The names flow to both readers: the Filer's fallback and
      the Disk Tracker's catalogs get Rock Ridge names from the one
      reader. *Blocked by 3.2.*
- [x] **3.4** `macrt/udf.py`: volume descriptors, file set, directory
      tree, presented as a machfs view — names, sizes, dates, files read
      on demand; UDF versions past the decided ladder refused by name;
      packed-sector DVDs refused by name if out of scope. *Blocked by the
      UDF open question.*
- [x] **3.5** Fixtures and tests: the ISO side byte-by-byte like
      `disktracker/tests/test_index.py`'s generated fixture; the UDF side
      built the same way; both Filer-and-tracker listing checks.
      *Blocked by 3.3 and 3.4.*
- [x] **3.6** `docs/disk-images.md` updated for whatever lands, fixing
      its stale claim that ISO 9660 is "not parsed" in the same pass.
      *Blocked by 3.3 and 3.4.*

---

## 4. Reuse macrt's HFS and HFS Plus readers in the Disk Tracker

Background: `disktracker/index.py` carries its own copy of two catalog
walks. The canonical readers are the ones the running emulator uses; the
tracker's copies exist because it needs name, dates, type/creator, size
and invisibility, and no forks.

Duplication map, as of this writing:

- HFS: `disktracker/index.py::_read_hfs` (line 52) ≈
  `macrt/macfile.py::_read_hfs` (line 857) — byte-for-byte modulo two
  helpers (`_extent_pairs` vs machfs's `unpack_extent_record`,
  `_decode_mac_roman` vs `bytes.decode("mac_roman")`) and the Desktop
  files. macrt's copy pops `Desktop`/`Desktop DB`/`Desktop DF`; the
  tracker's does not, so DT catalogs today index the Finder's Desktop
  files and the Filer doesn't show them.
- HFS Plus: `disktracker/index.py::_read_hfsplus` (line 274) and its
  `_hfsplus_node`, `_hfsplus_leaf_records`, `_hfsplus_spans`,
  `_hfsplus_fork_data`, `_hfsplus_overflow`, `_hfsplus_text`,
  `_hfsplus_record` (lines 146–273) ≈ `macrt/hfsplus.py::read_hfsplus`
  (line 147), which `macrt/macfile.py::load_volume` calls (lines 1179 and
  1180). The tracker's drops invisible entries (fdFlags bit 0x4000); the
  canonical reader drops invisible entries, the HFS+ Private Data catalog
  directory and the Desktop files — so the tracker's view and the Filer's
  view differ by the Desktop files today.
- `macrt/macfile.py::_read_hfs_plus` (line 989) is dead — nothing calls
  it; `_read_hfs` (line 857) stays.

Joint decision with task 3: where `_read_iso` lives after the move.
Decide this before 3.1 or 4.3 starts.

Subtasks:

- [x] **4.1** Delete the dead `macrt/macfile.py::_read_hfs_plus`
      (line 989). `_read_hfs` (line 857) stays. Independent of everything
      else in this task; can run alongside tasks 1–3.
- [x] **4.2** Write one adapter in `disktracker/index.py`,
      `_volume_tree(vol: machfs.Volume) -> (name, crdate, mddate, root)`:
      walk `vol`'s `Folder`s and `File`s; a folder's dict keeps `kind`,
      `name`, `crdate`, `mddate`, `children`; a file's dict adds `type`,
      `creator`, and `size = sum(f.sizes())` (both readers build files as
      `macrt.macfile.OnDisk`, whose `sizes()` reports data and resource
      lengths without reading the forks). Skip nothing — the canonical
      readers already filtered invisible and Desktop entries. HFS offers
      no `INVISIBLE` fdFlags; whatever macrt's `_read_hfs` pops is what
      the tracker gets. Note in a comment that HFS+ dates arrive local,
      not raw GMT (see 4.5). *Blocked by 4.1 only insofar as 4.1 is
      trivial; may proceed in parallel.*
- [x] **4.3** Rewrite `_read_fs` to:
      - HFS: `macrt.macfile._read_hfs(data)` — it scans for the MDB
        itself. If it raises, try HFS Plus (today's order: a hybrid disc's
        Mac view is its HFS one). The embedded HFS+-in-an-HFS-wrapper
        case is unchanged from today (the HFS copy inside the wrapper is
        indexed, not the embedded volume) unless deliberately addressed.
      - HFS Plus: keep `_find_hfsplus(data)` (the 512-step scan for
        `H+`/`HX` whose fields hold up) to locate the volume, and read it
        with `read_hfsplus(data[start:])`; its `vol.name`, `vol.crdate`,
        `vol.mddate` are set.
      - ISO 9660: as decided with task 3 (3.1); until then `_read_iso`
        stays untouched here.
      *Blocked by 4.2 and the joint `_read_iso` decision.*
- [x] **4.4** Delete the tracker's copies (`_read_hfs`, `_read_hfsplus`,
      `_hfsplus_*`, `_extent_pairs`, `_records`, `_node`, `_leaf_records`)
      and `_find_hfs` if unused after 4.3. Rewrite `index.py`'s "Standard
      library only" header note ("MFS volumes are recognized and refused")
      to say what DT now depends on: `machfs` and `macrt.hfsplus` —
      `machfs` is already a hard dependency of the emulator, both live in
      this project. *Blocked by 4.3.*
- [x] **4.5** Dates: `read_hfsplus` applies `_gmt_to_local`, so
      created/modified in HFS+-sourced catalogs move from raw GMT (today)
      to local time — the same values the Filer's Get Info shows, and
      what a real Mac displays. Accept this; the adapter's comment from
      4.2 carries the note. If any caller of `build_dtc` depends on raw
      GMT, this must be an adapter-side decision, not an accident — check
      before landing 4.3.
- [x] **4.6** Invariants test, in `disktracker/tests/test_index.py`, on
      the generated fixtures, asserting:
      - catalog contains no invisible entries and no `Desktop`,
        `Desktop DB` or `Desktop DF` (HFS and HFS+ images both);
      - a file's `size` is the sum of its forks, without reading them;
      - names match the Filer's view (HFS+: `_mac_name`'s result).
      *Blocked by 4.4.*
- [x] **4.7** Acceptance sweep: `disktracker/index.py` contains no copy
      of either catalog walk; `macrt/macfile.py` contains no
      `_read_hfs_plus`; `disktracker/tests/test_index.py` passes with the
      4.6 assertions (the HFS+ fixture's expected dates change by the
      GMT offset of the test machine's timezone — tests normalize, or CI
      runs at UTC); a catalog built from a generated HFS+ image and the
      same image browsed in the Filer list the same items; no behavior
      change for ISO catalogs; the generated-ISO fixture unchanged.
      *Blocked by 4.4 and 4.6.*

Open questions:

- Where the dict translation lives: disktracker as a local adapter
  (chosen, in 4.2), or a shared helper in macrt both sides use. The
  adapter matches "a Volume is a Volume"; task 3's ISO/UDF readers may
  argue for the shared helper — revisit if they need it.
- Whether opening every image through `macrt.macfile.load_volume` instead
  of the signature scans is worth doing here: it would also fix the
  embedded HFS+-in-HFS wrapper case, make DT index UDIF/DART/MOOF images
  it can't open today, and need a new wrapper-case test. Out of scope
  unless the wrapper case matters enough to want it.
- Whether 4.5's GMT→local move breaks any caller of `build_dtc`.

---

## 5. Colour icons in the window titles, the About Classetta app list, and the large machine logo

Background: at depth > 2, chrome draws `title_pixmap` instead of
`title_icon` (`chrome.Frame`, `macrt/ui/chrome.py`; the callers gate on
depth, chrome itself does not). Get Info and the Content Viewer already
build a colour pixmap via `item_icon_qicon` and pass it. Three other
surfaces still draw 1-bit:

- **Filer window titles.** `FilerWindow.frame` (`macrt/ui/filer.py` line
  577) passes only `_title_icon`, always 1-bit — the generic disk,
  folder, archive, document and catalog icons from `CONTAINER_ICONS`
  (1-bit only), or a custom volume icon through `_container_icon`
  (`_custom_icon`, 1-bit only). Get Info's windows already title colour
  at depth > 2 in the same app.
- **The About Classetta app list.** Application rows draw colour at
  depth > 2 (`_fork_color_icon`, `macrt/ui/filer.py` line 1198), but the
  System row is always `theme.GENERIC_APP_ICON` with no pixmap — 1-bit at
  any depth.
- **The large machine logo.** `theme.MAC_II_ICON` when the model's name
  is "Macintosh II", `theme.MAC_PLUS_ICON` otherwise — both 1-bit, drawn
  via `theme.draw_bitmap`; selection is a name substring match, so IIx
  and IIci fall back to the Plus logo, and both constants are the only
  two logos that exist.

Settled (all Recommended, user, via 5.1):

- The colour generic art is original in-repo art under `macrt/ui/icons/`,
  native 16/32 pairs. No colour map on 1-bit at draw time, no
  runtime-generated art.
- The new art carries a licence file next to it, the way `fonts/` carries
  `OFL-1.1.txt` — scope: the art only, not a project-wide relicence.
- The About Classetta System row colours come from the running System
  file's own icon through the same `_fork_color_icon` path the
  application rows use; a bundled stand-in is drawn when there is none
  (the built-in empty System always takes the stand-in).
- `CATALOG_ICON` gets colour art on the same rule as the other container
  icons, and the viewer windows' 16-pixel pixmaps set the size for all
  of this (16x16 for titles/rows; the About logo stays 32x32).
- IIx and IIci get their own art, 1-bit and colour both, drawn
  in-repo — the Plus-logo 1-bit fallback at depth <= 2 ends.
- `GENERIC_APP_ICON` gains a native 16x16 colour sibling (slight art
  redraw for the even width); the 1-bit 15x15 stays untouched.

Note: `AboutClassettaWindow` is built with no title icon ("Build the
window - no title icon"); that stays as it is unless the work says
otherwise.

Subtasks:

- [x] **5.1** *Report only.* The colour generic art source: what the
      options cost (licensing, draw-time mapping, generated art), which
      icons it covers (generic disk, folder, archive, document, catalog),
      and a recommendation. No code. Blocks 5.2–5.4.
- [x] **5.2** Filer window titles: at depth > 2, titles carry colour
      icons — the container's own colour icon where it has one (as Get
      Info's title does, via `item_icon_qicon` / `_fork_color_icon`), and
      colour stand-ins for the generic disk, folder, archive, document
      and catalog icons; 1-bit at depth ≤ 2, and when colour can't be
      built, with no other behavior changed. `macrt/ui/filer.py` line 577.
      *Blocked by 5.1.* Done in `d76e634`.
- [x] **5.3** The About Classetta app list: the System row draws in
      colour at depth > 2 like the application rows do — art source per
      the System-row decision. `macrt/ui/filer.py` lines 1198 and 1642 and
      `theme.GENERIC_APP_ICON`. *Blocked by 5.1 and the System-row
      decision.* Done in `ae6a373`.
- [x] **5.4** The About Classetta logo: one colour logo per `MODELS`
      entry (`plus`, `ii`, `iix`, `iici`, in
      `macrt/toolbox/system.py`), selected by model id, not by name
      substring; IIx and IIci get their own art; 1-bit stays at depth
      ≤ 2 and as the fallback; each 1-bit constant gains its colour
      sibling rather than being replaced. Blocks 5.5.
      *Blocked by 5.1.* Done in `6c788a3`.
- [ ] **5.5** Machine icon rule, as a test: nothing may grow `MODELS`
      without a logo — a test that fails when a `MODELS` entry has no
      About logo. *Blocked by 5.4.*
- [ ] **5.6** Tests: paint smoke tests at depth 8 for each surface (a
      pixmap is handed to chrome, or the rows draw one); depth 1
      unchanged; About opens on every model in `MODELS`.
      *Blocked by 5.2, 5.3, 5.4, 5.5.*

## 6. lazy.py parses the disk-record suffix with a hardcoded 308 bytes

Background, found by task 1.1's survey of real catalogs: the disk-record
suffix is not a fixed size. Image-sourced DT2 disk records carry
724–1755-byte suffixes (the tagged alias blob includes a second
"source image" segment), while physical-media disks carry ~308-byte
ones. `disktracker/lazy.py` hardcodes `suffix=data[after:after + 308]`
and advances its offset walk by 308 in two places (the lazy offset walk
near its `_build` code, and `load_item` for index 0), so for
image-sourced catalogs the walk silently mis-lands and items parse
garbage. The Filer's path is unaffected — `filer_adapter.py` uses
`parse_dtc` in `disktracker/disktracker.py`, which finds the first
item with `_find_first_item` and handles variable suffixes.

Subtasks:

- [x] **6.1** Make `lazy.py`'s disk-record walk use the same
      `_find_first_item` logic as `parse_dtc` (import it or factor a
      shared helper), so the suffix length varies per record.
- [x] **6.2** Test: hand-written catalog bytes with a disk record whose
      suffix is larger than 308 bytes (an image-sourced record), built
      inline in the test; assert the suffix round-trips intact and the
      items after it parse. Also assert the physical-media size (308)
      still parses.

---

## Cross-task decisions and contacts

- `_read_iso`'s placement — decided jointly by task 3 (3.1) and task 4
  (4.3). Neither proceeds on it alone.
- 5.1's report blocks 5.2, 5.3 and 5.4.
- Tasks 2, 3 and 4 all edit `docs/disk-images.md` and (task 3) its stale
  ISO claim. Landing any two in one pass avoids merge churn; they are
  otherwise independent in code.
- Task 2 and task 4 are fully independent. Task 1 is independent of all
  of them.
- `dt2/`: never committed, never wired into tests, never referenced from
  outside the repository — applies to task 1 in particular.
