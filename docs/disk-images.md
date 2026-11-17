# Disk image formats

The Filer opens disk images through `macrt.macfile`. Each supported format
has its own reader; unsupported formats are listed below so it is clear what
will and will not open.

## Supported

| Format | Reader | Notes |
| --- | --- | --- |
| Raw HFS/MFS/HFS+ volume, Apple partition map, CD-ROM | `macrt.macfile` | Detected from the volume header or partition map; a raw CD image (`.iso`, `.cdr`, `.toast`, or 2352-byte Mode-1 sectors) mounts as a CD-ROM: its HFS/HFS+ contents, or — for a disc with no Mac volume — its ISO 9660 files, read through the `disktracker.index` ISO reader (ISO names, not Rock Ridge's). |
| DiskCopy 4.2 (`.dsk`) | `macrt.macfile` | The 84-byte header is stripped. |
| MOOF/NDIF track images | `macrt.moof` | Tags and tracks are read as stored. |
| CHD (`.chd`) | `macrt.chd` | Versions 3, 4 and 5; compression none, zlib and LZMA. |
| qcow2 | `macrt.qcow2` | Versions 2 and 3; raw, deflate-compressed and zero/unallocated clusters. |
| VHD (`.vhd`, `.vpc`) | `macrt.vhd` | Fixed (type 2) and dynamic (type 3) disks. |
| VMDK (`.vmdk`) | `macrt.vmdk` | Version 4 sparse extents (raw and zlib-compressed grains, with or without grain markers, grain directory in the footer) and version 3 COWD extents. |
| UDIF (`.dmg`, `.smi`) | `macrt.dmg` | Uncompressed, zlib, ADC and sparse chunks; single-partition images; the `koly` trailer at the file's end, or at offset 0 in early ones. |
| NDIF (Disk Copy 6, `.img`/`.smi`) | `macrt.dmg` | Raw, ADC and zero chunks, single-partition images whose chunk map lives in the `bcem` resource 128. |
| MDF/MDS (Alcohol 1200, `.mds` + `.mdf`) | `macrt.mdf` | Mode 1 and Mode 2 Form 1 data tracks; 2352-byte stride, or 2448 with interleaved subchannel; the data track's 2048-byte user sectors are extracted into a raw CD volume. |
| DART / DiskMaker (`DMdf`/`DskM`, `DMd*`/`DART`) | `macrt.dart` | 'Fast' RLE and stored chunks; the 12-byte Apple II tag-RAM bytes are stripped; Mac 400K (MFS), 800K and 1440K (HFS) disks. |

All of these are read in full into memory before the volume readers see
them; files inside a volume are still read on demand.

NDIF images are detected through their resource fork, not their data fork
or name: the chunk map lives in the `bcem` resource 128. The image opens
on its own when the file keeps both forks on the host, or nested in a
MacBinary or AppleDouble container. A bare data fork with the chunk map
stripped (copied through something that kept only the data fork) cannot
be told from any other blob and will not open.

An MDF/MDS pair is detected from either file of the pair: an `.mds`
carries the `MEDIA DESCRIPTOR` signature, while a bare `.mdf` has no
marker of its own, so it is found through a sibling `.mds` with the same
stem on the host. The filename the `.mds` footer names is ignored. The
same holds for a pair nested in an archive: opening either member finds
the other member with the same stem in the same archive.

A DART or DiskMaker image is detected from its header and chunk-length
table, not its name; Apple used it for the guided-tour disks on its
Developer CDs (`DMdf`/`DskM`), and DART (`DMd*`/`DART`) is the same file
written by its successor. Each chunk holds 40 512-byte sectors followed
by 40 sets of 12 tag bytes — the Apple II tag-RAM bytes, which is also
why DiskMaker archived ProDOS disks — and the tags are stripped, so the
volume readers see the plain 512-byte sector stream. Mac 400K disks
mount as MFS, 800K and 1440K disks as HFS; a chunk that would not
compress is stored raw (block length `0xffff`) and read the same way.

## Unsupported

These formats are recognised as out of scope for now. They are not opened,
and a file in one of them will not browse as a volume.

**CD images**

- **BIN/CUE**: the `.cue` sheet is never read; the `.bin` half opens
  through the raw-CD path when it keeps 2352-byte sectors whole (one
  data track starting at 00:02:00, as a data-first mixed-mode disc is —
  its audio tracks are never played), or as a plain 2048-byte data
  image (HFS/ISO). Nothing about the sheet is parsed or validated: a
  one-file-per-track layout, interleaved subchannel at 2448 bytes a
  sector (`.mdf` supports it), Mode 2 Form 2, multiple data tracks and
  DVDs are all unsupported — and none are refused by name.
- **Rock Ridge extensions and UDF**: not parsed. ISO 9660 itself is read:
  a disc with no Mac volume opens with the files its directories hold
  (via the `disktracker.index` ISO reader, the same one the Disk Tracker
  uses), but as ISO 9660 — names in latin-1 with `;1` stripped, no
  Rock Ridge NM entries, so a disc mastered with them shows its ISO
  names. UDF (ECMA-167), which common DVDs with no Mac volume carry, is
  not read at all; such a disc mounts with nothing to browse.
- **Multi-track CD images with audio tracks**: not played or read. The
  driver answers the audio calls with `paramErr`, and a disc's audio
  tracks are never exposed to the running program.

**QEMU / virtual machine formats**

- **qcow** (version 1): the predecessor of qcow2. Not implemented; declined
  — the format is rare, and reading it is not worth the effort.
- **QCOW3 / VHD differencing (type 4)**: refuse by name. A differencing
  VHD depends on a parent image whose bytes live in another file, and qcow
  versions beyond 3 are not read.
- **VHDX** (Hyper-V): the successor to VHD, with a different layout and
  log/journal metadata. Not implemented.
- **parallels (PRD)**: Parallels' own disk format. Not implemented.
- **bochs, cloop, qcow (other versions)**: not implemented.

**MAME**

- MAME's disk images are CHD files, which are supported above. MAME has no
  additional disk container format of its own beyond CHD.

**Refused by the readers that exist**

- Encrypted qcow2 images (AES or LUKS).
- qcow2 images that need a backing file or an external data file.
- qcow2 images with the dirty, corrupt, extended-L2 or non-deflate
  compression incompatible features set.
- VHD images saved with `saved_state == 1` (a suspended VM's image).
- VHD images whose footer or dynamic-header checksum does not match.
- CHD images with codecs or metadata this build does not carry.
- VMDK images that are a text descriptor naming other files, or that name a
  parent image.
- VMDK images of versions past 3, or compressed with anything but deflate.
- UDIF images with bzip2 (UDBZ), LZFSE (ULFO) or lzma (ULMO) chunks, or with
  any other chunk type this build does not carry.
- UDIF images that are split across files (segmented), or that carry more
  than one `blkx` block table, or whose version is not 4.
- UDIF images larger than 8 GB.
- NDIF images compressed with KenCode, or whose version is not 10 to 12.
- NDIF images without the `bcem` resource 128 in their resource fork —
  NDIF keeps its chunk map there, so the data fork alone cannot be read.
- Encrypted UDIF images are not detected as such: they fail as damaged
  images and are reported as such.
- MDF/MDS images that are DVDs, or whose data tracks are Mode 2 (2336
  bytes a sector) or Mode 2 Form 2 (2324), or that carry more than one
  data track.
- MDF/MDS images whose tracks are all audio.
- DART images with 'best' LZHUF compression, not implemented.
- DART images of Lisa, Apple II (ProDOS) or MS-DOS disk types: they are
  detected (the Kind column says so) but the sector layout is not one a
  Mac mounts.

Corrupt or unsupported images raise an error the Filer reports as an alert;
they never crash the application.
