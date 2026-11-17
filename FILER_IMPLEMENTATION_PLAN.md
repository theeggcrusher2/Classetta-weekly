# Filer Implementation Plan

## Instructions for the coding agent

Work on **one task at a time**, in the order listed below. Do not start later tasks until their prerequisites are complete.

For each task:

1. Inspect the relevant existing code and tests before editing.
2. Implement only the task described; avoid unrelated refactors.
3. Add or update tests for the behavior.
4. Run the relevant tests and report their results.
5. Summarize the implementation, changed files, and any follow-up work.
6. Stop and wait for approval before beginning the next task.

Preserve existing behavior unless a task explicitly changes it. If the codebase differs from an assumption in this plan, report the discrepancy and propose the smallest adjustment before expanding scope.

## Design decisions

- **The Filer is one active application with many windows.** Filer windows as a
  whole are treated just like an active app: when any Filer window is
  frontmost, the Filer owns the menu bar and menu state (one menu bar for all
  Filer windows, as the Finder had); when an emulated process is frontmost, the
  bar shows that process's menus as it does today. Filer activation participates
  in the same paths as an app becoming active — `refresh_bars`, `_owner` /
  `bar_at` / `find_window` routing, `restack` — and Filer menu commands are
  dispatched host-side (the Application-menu precedent in `MenuTracker._chosen`).

## Completion checklist

### Foundations and menus

- [x] **1. Map existing launch, Filer, menu, and format paths**
  - Trace how applications are launched and how Filer windows are created.
  - Identify selection and frontmost-window handling.
  - Locate existing encodings, archive, and disk-image handlers.
  - Record relevant tests and the smallest suitable extension points.
  - Do not implement features in this task.

- [x] **2. Build the menu-bar scaffold**
  - Add a menu bar to the relevant application interface.
  - Ensure it reflects the features available to the running application.
  - Support updating the menu state when the active application or frontmost window changes.
  - Add tests or a documented manual verification procedure.

- [x] **3. Add the initial Finder 6.1-style menu structure**
  - Add the initial menu headings and planned command placeholders.
  - Keep commands disabled until their corresponding behavior is implemented.
  - Ensure disabled commands cannot be invoked through the UI.
  - Add tests for the initial menu structure and disabled state.

- [x] **4. Add About Classetta menu**
  - Add **About Classetta…**, modeled on Apple’s **About This Macintosh…**.
  - Verify the command opens the About window and can be dismissed.

- [x] **5. Define minimal item capabilities and selection context**
  - Provide a small, shared way for menu commands to identify the selected item and its supported actions.
  - Ensure commands operate on the selected item in the frontmost Filer window.
  - Avoid adding capabilities not needed by the tasks that follow.
  - Add tests for selection context and capability checks.

### Core Filer commands and host folders

- [x] **6. Implement File → Open**
  - Launch the selected item in the frontmost Filer window using the existing launch behavior.
  - Define and test behavior for no selection, unsupported items, and launch errors.
  - Enable the menu item only when it can be used.

- [x] **7. Implement basic Get Info**
  - Add **File → Get Info** for the selected item.
  - Display an Apple-like info window with the item’s icon, name, and other details already available in the project.
  - Define behavior for no selection and missing metadata.
  - Enable the menu item only when applicable.

- [x] **8. Extend Get Info with fork and metadata details**
  - Display whether the item has a data fork and/or resource fork.
  - Display its type and creator codes when available.
  - Display special attributes that can be read from existing metadata.
  - Keep the window usable when some metadata is absent.

- [x] **9. Display contents of data-fork-only files**
  - Add **File → Display Contents** for files with a data fork but no resource fork.
  - Open a hex viewer directly, without a resource-list window.
  - Add an option at the top of the viewer to switch to a text-only view.
  - Test empty, unreadable, and ordinary data forks.

- [x] **10. Use application resource-driven icons throughout the Filer UI**
  - Show an item’s own icon, read from its resource fork, wherever the Filer draws a generic icon for it: application and custom-icon file rows in the list, folder rows whose `Icon` file carries one, Get Info’s icon and title bar, the Content Viewer’s title bar, and the running-application rows of About Classetta.
  - Keep the existing generic icons as the fallback when an item has no icon of its own or its resources are unreadable.
  - Share the extraction with the title-bar path: suite icons first, then a bare `ICON`; halved to 16 for rows and title bars, full size for Get Info.
  - Test an item that wears its own icon, one that keeps its generic icon, and unreadable resources.

- [x] **11. Display resource forks and their contents**
  - For a file with a resource fork, show a window listing its resources.
  - Include a **DATA** item when the file also has a data fork.
  - Opening a resource or **DATA** opens it in the hex viewer with the text-only option.
  - Test resource-only and resource-plus-data files.

- [x] **11b. Update Get Info for minimum and desired heap sizes**
  - Update Get Info window to display application's minimum reserved heap and desired reserved heap, like in System 7 Finder.

- [x] **12. Open host-system folders in Filer windows**
  - Allow a host-system folder to be opened as a Filer window.
  - Support browsing, selection, and the applicable item commands.
  - Preserve existing behavior for other kinds of Filer windows.
  - Test empty folders, ordinary folders, and inaccessible folders.

### Existing formats and nested browsing

- [x] **13. Add Open to Filer View for existing non-disk-image file types**
  - Add an explicit way to request Filer view, potentially a `--filer` flag.
  - Keep the current action as the default for each existing file type.
  - Start with file types the application already handles.
  - Test both the default launch action and explicit Filer-view behavior.

- [x] **14. Bring existing encodings and containers into the Filer UI**
  - Reuse existing handlers for formats already supported by the application.
  - Open their contents in Filer windows.
  - Do not add new format support in this task.
  - Test representative existing formats and invalid inputs.

- [x] **15. Enable drill-down through existing containers**
  - Allow supported containers to be opened from within a Filer window.
  - Support nested browsing for existing encodings and container types.
  - Handle unsupported or corrupt nested items without crashing the Filer window.
  - Test at least one nested-container case.

- [x] **16. Launch applications found in existing nested containers**
  - Allow an application found through the supported nested-browsing path to be launched.
  - Test a directly contained application and one found inside multiple existing containers.
  - Preserve the normal launch behavior for non-nested items.

### Process lifecycle

- [x] **17. Implement graceful Shutdown**
  - Add **Special → Shutdown**.
  - Close all windows and request a graceful quit from all running subprocesses.
  - Terminate the root process only after the shutdown sequence completes.
  - Test subprocesses that exit normally and handle shutdown errors without skipping cleanup.

- [x] **18. Implement Restart**
  - Add **Special → Restart**.
  - Reuse the graceful shutdown sequence from task 17.
  - After shutdown, rerun the original command with the same parameters.
  - Test argument preservation and ensure relaunch occurs only after shutdown completes.

### Disk Tracker 1 and 2

- [x] **19. Export Filer contents to Disk Tracker 2 format**
  - Export the current Filer contents to the Disk Tracker 2 file format.
  - Preserve the metadata supported by the export format.
  - Test representative contents and handle export failures cleanly.

- [x] **19b. Add support for Disk Tracker 1.x format**
  - Add support for reading and handling the original Disk Tracker 1.x format in the `disktracker` library.
  - Omit newer catalog features (Finder comments, item notes, large-volume extensions) absent in the original format.
  - Ensure clear detection and differentiation between Disk Tracker 1.x and 2.x files.
  - Add tests covering Disk Tracker 1.x files and error/fallback paths.

- [x] **20. Display Disk Tracker 1 and 2 files as read-only Filer windows**
  - Open Disk Tracker 1 and 2 files in a Filer-style display-only window.
  - Show a distinct window-bar icon to indicate display-only status.
  - Disable opening selected files.
  - Keep **Get Info** available for recorded metadata.
  - Test missing metadata and attempts to invoke disabled actions.

- [x] **20b. Add disk image batch indexing to disktracker library**
  - Add support in the `disktracker` library to accept a list of disk images (e.g., HFS/ISO/raw volumes).
  - Parse their filesystem contents and output a fully structured `.dtc` catalog file indexing their contents.
  - Test batch generation against sample disk images.

- [x] **21. Implement color icons and other elements with --depth greater than 2**
  - When the Filer runs with the `--depth` option set to a value greater than 2, enable color icons and other high-colour display elements.
  - For `--depth 8` (true colour), display full 256-colour icons where available, and enable colorised UI elements where supported.
  - For `--depth 4` (high-colour), use a reduced palette of up to 16 colours for icons and UI elements.
  - Ensure fallback to 1-bit icons when `--depth` is 1 or when colour icons are unavailable.
  - Update Get Info windows, application icons in the list, and other UI elements to respect the depth setting.
  - Test that the Filer correctly switches between colour and monochrome modes based on the depth option.

- [x] **20c. Index HFS Plus disk images in disktracker**
  - Extend the `disktracker` library's image indexing (`disktracker/index.py`'s `_read_fs`, which today knows HFS and ISO 9660) to read HFS Plus / HFSX volumes ('H+'/'HX'), the format task 27 added to the Filer path (`macrt.hfsplus`).
  - Reuse the `macrt.hfsplus` reader (or its parsing pieces) rather than duplicating B-tree walking; cover raw volumes and Apple-partition-map-wrapped ones, as in task 27.
  - Emit `.dtc` catalog items the same shape as the HFS and ISO paths, matching by volume and file names.
  - Report corrupt or unsupported images with a clear error without crashing the batch.
  - Test with generated HFS Plus fixtures (no external sample images committed to the repo).

### New format support

- [x] **22. Establish the smallest reusable format-opening interface**
  - Base the interface on the existing format handlers integrated in task 14.
  - Support opening a recognized container in a Filer window.
  - Include a clear unsupported-format and invalid-input path.
  - Avoid implementing unrelated formats or speculative generality.

- [x] **23. Port MacExtract to a Python archive library**
  - Reimplement [uzTools/MacExtract](https://github.com/uzTools/MacExtract) in Python as a standalone, reusable library, not tied to this project; it replaces the planned `unar` integration. Published as [macextract.py](https://github.com/adespoton/macextract.py), with the import package `macextract`; the Filer depends on it via the git URL in `pyproject.toml`.
  - Provide two library operations: list an archive's contents, and extract an archive's contents.
  - Use the library in the Filer to list and extract archive contents. The Filer sniffs with the library's format registry (`macextract.formats.FORMATS`), so every registered format — and each format added later — is browsable and extractable with no Filer change. File ▸ Extract (or the selected archive, in any listing) extracts to a folder you pick; resource forks travel as `._name` AppleDoubles.
  - Format coverage (small tested batches, valid/corrupt/password-protected/unsupported): MacBinary I/II/III, BinHex 4.0, AppleSingle/Double, uuencode, btoa, base64, MIME attachments, PackIt, AppleLink `.pkg`. The remaining formats are task 23a.
  - Test valid, corrupt, password-protected, and unsupported archives.

- [x] **23a. Finish the macextract.py format ports**
  - The remaining formats from uzTools/MacExtract's README, in small tested batches, each shipped list + extract with tests before the next begins: StuffIt classic → StuffIt 5 → StuffIt arsenic → Compact Pro → DiskDoubler → Now Compress → MacCompress (.Z) → ARC → LZH/LHA → PackIt splits → DiskDoubler/StuffIt split volumes → MAR → Apple Backup → Tomes → zip/gzip/tar → RAR, 7-Zip, bzip2 and xz (decompression only, the Python port taking the PC/general compressors the C project never had) → StuffIt SpaceSaver (with the Aladdin tables, attribution per its NOTICE). StuffIt X never.
  - Every batch is automatically browsable in the Filer once its format is registered in macextract.py; no per-format Filer work.

- [x] **23b. List archives from their directories only**
  - Where a format states its members in a directory or in per-member headers, opening an archive for listing reads only that index: no member body is decoded until the entry is read, opened or extracted. A format with no directory at all (a bare `.Z`, a single BinHex stream) reads only its header for the listing.
  - The Filer's `archive_entries` stops reading every fork while building a listing: names, sizes and Finder info come from headers, and a MacFile materialises its forks on first use.
  - CRC and password checks that need decoded bytes move to first read, and the tests that grade them at `open()` move with them; a structurally damaged directory must still fail at list time.
  - Verify the completed disk-image paths (raw, HFS, HFS+, ISO, Toast, BIN/CUE) list from filesystem directory structures only and read file contents on demand, and keep them that way as formats are added.
  - Tests: for every directory-bearing format, a listing succeeds even when a member body is corrupt — reading that entry then fails — with generated fixtures only.

- [x] **24. Add CHD support**
  - Add CHD handling through an appropriate reader or adapter.
  - Open supported contents in a Filer window.
  - Add tests or representative fixtures for detection and browsing.

- [x] **25. Add qcow2 support**
  - Add qcow2 handling through an appropriate reader or adapter.
  - Open supported contents in a Filer window.
  - Add tests or representative fixtures for detection and browsing.

- [x] **26. Add other selected QEMU and MAME formats**
  - Add explicitly identified formats in small, independently testable batches.
  - Use an appropriate reader or adapter for each batch.
  - Do not treat "all QEMU and MAME formats" as one task.
  - Document formats that are not supported.
  - [x] Batch 1: VHD (fixed and dynamic) — reader, Filer test, tests, docs/disk-images.md
  - [x] Batch 2: VMDK (version 4 sparse and version 3 COWD) — macrt.vmdk reader, Filer test, tests/test_vmdk.py, docs/disk-images.md
  - Batch 3: qcow version 1 — declined; rare format, high effort

- [x] **26b. Add NDIF and UDIF disk-image reading**
  - Read compressed Apple disk images: NDIF (DiskCopy 6) and UDIF (`.dmg`, `.smi`).
  - Open their contents through the existing disk-image path (`macrt.macfile`), raw and DiskCopy 4.2 images staying as they are.
  - Support at least single-partition images first; handle segmented/multi-part images in a later increment if needed.
  - Report unsupported UDIF encodings and corrupt images without crashing.
  - Test detection, extraction to a readable volume, and failure paths.
  - Done: `macrt.dmg` reads UDIF (uncompressed, zlib, ADC, sparse) and NDIF (raw, ADC, zero);
    bzip2/LZFSE/lzma, KenCode, segmented, multi-table and version-≠4 images are refused by name.
    tests/test_dmg.py plus two Filer tests; docs/disk-images.md updated. Encrypted UDIF images
    are not specially detected; they fail as damaged images.

- [x] **27. CD-image support incrementally**
  - Add one tested CD-image path first, then expand in separate increments.
  - Cover ISO, Toast, and BIN/CUE images.
  - Cover the requested HFS, HFS+, UDF, and Rock Ridge contents as separate, verifiable format/filesystem combinations.
  - Handle unsupported filesystems and malformed images without crashing.

- [x] **27b. Add MDF/MDS (Alcohol 1200) CD-image reading**
  - Read MDF/MDS pairs: the `.mdf` sector image with its `.mds` sidecar describing tracks, sessions and sector format.
  - Open their contents through the existing disk-image path (`macrt.macfile`); the completed ISO, Toast and BIN/CUE images stay as they are.
  - Detect from either file of the pair; a missing sidecar, an unrecognised sector format or malformed metadata is reported without crashing the Filer.
  - Test detection, extraction to a readable volume, and failure paths with generated fixtures only. Manual sanity material: the 7-zipped ADC Developer CD MDF pairs in the gitignored `dt2/` folder — local checks only, never committed and never wired into the test scripts; unwrapping them also serves as manual sanity material for the planned 7z decompression batch.
  - Done: `macrt.mdf` reads the descriptor's sessions and tracks, takes the one data track (Mode 1 or Mode 2 Form 1, 2352 or 2448 stride with or without interleaved subchannel), and hands its 2048-byte user sectors to the existing volume path; DVD, Mode 2, Mode 2 Form 2, multi-data-track and audio-only images are refused by name, a missing sidecar, junk descriptor or truncated `.mdf` raise specific errors.
    tests/test_mdf.py plus two Filer tests; docs/disk-images.md updated. A bare `.mdf` has no marker, so it is detected through a sibling `.mds` with the same stem; the footer's filename is ignored. A pair inside an archive opens from either member, the sibling member found in the same archive.

- [x] **27c. Add DART/DiskMaker disk-image reading**
  - Read the `DMdf` (DiskMaker, `DskM`) and `DMd*` (DART, `DART`) floppy-image format Apple used on its Developer CDs: a 4-byte header (compression, disk type, size in kiB), a 16-bit chunk-length table, then 40-sector chunks of 512-byte blocks plus their 12 tag bytes (the Apple II tag-RAM area; zero on a Mac's disk).
  - 'Fast' RLE and stored chunks are read; the tags are stripped and the plain 512-byte sector stream goes through the existing volume path (400K MFS, 800K and 1440K HFS mount).
  - Done: `macrt.dart` decodes 'fast' RLE (word counts, a negative count repeating the following word) and stored (`0xffff`) chunks; the Kind column says "DART disk image" and a double-click opens the volume instead of launching the DiskMaker application. The 'best' LZHUF compression and the Lisa, Apple II (ProDOS) and MS-DOS disk types are recognised and refused by name. tests/test_dart.py plus a Filer double-click test; docs/disk-images.md updated. The nine guided-tour images on the ADC Developer CD (Volume 01) decode and mount as volumes (MFS for the 400K one, HFS elsewhere).

- [x] **28. Extend drill-down to newly supported formats**
  - Allow newly supported archives and disk images to participate in nested browsing.
  - Add one format at a time or in small tested batches.
  - Track nesting and report failures without losing access to the parent Filer window.

- [x] **29. Launch applications executable multiple levels deep**
  - Launch an application reached through nested encodings, archives, disk images, or filesystems.
  - Test progressively deeper nesting, including a case with multiple container types.
  - Ensure failures identify the level or item that could not be opened.

- [x] **30. Show version resources in Get Info windows**
  - When a file's resource fork carries `vers` resources, show the short version string (ID 1) and the long version string (ID 2) in its Get Info window, as the Finder's Get Info does.
  - Files without `vers` resources show neither field; the window is unchanged, and missing or malformed resources never crash it.
  - Works for files reached any way: on a volume, inside a disk image, inside an archive.
  - Test with generated fixtures only.

- [ ] **31. Show archive contents as nested folders, not flat files with unix filepaths**
  - Archive member names that contain "/" appear as folders containing the members beneath them, so the Filer window shows a tree, not one flat list of full unix paths.
  - Drill-down, selection, extraction and the sanitiser keep working through the nesting; names that escape the archive ("..", leading "/") stay refused.
  - Single-file and single-folder archives look the same as they do today.
  - Test with generated fixtures only; no real archives committed.

## General acceptance criteria

A task is complete when:

- Its stated behavior works without breaking existing behavior.
- Relevant automated tests pass, or a specific manual test procedure is documented when automated coverage is not practical.
- The corresponding menu item is enabled only when the feature is usable.
- Errors such as unsupported formats, corrupt input, missing metadata, or inaccessible folders are handled without crashing unrelated windows or the application.
- The change is small enough to review independently.
