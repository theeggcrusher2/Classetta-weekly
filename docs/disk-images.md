# Disk image formats

The Filer opens disk images through `macrt.macfile`. Each supported format
has its own reader; unsupported formats are listed below so it is clear what
will and will not open.

## Supported

| Format | Reader | Notes |
| --- | --- | --- |
| Raw HFS/MFS/HFS+ volume, Apple partition map, CD-ROM | `macrt.macfile` | Detected from the volume header or partition map. |
| DiskCopy 4.2 (`.dsk`) | `macrt.macfile` | The 84-byte header is stripped. |
| MOOF/NDIF track images | `macrt.moof` | Tags and tracks are read as stored. |
| CHD (`.chd`) | `macrt.chd` | Versions 3, 4 and 5; compression none, zlib and LZMA. |
| qcow2 | `macrt.qcow2` | Versions 2 and 3; raw, deflate-compressed and zero/unallocated clusters. |
| VHD (`.vhd`, `.vpc`) | `macrt.vhd` | Fixed (type 2) and dynamic (type 3) disks. |
| VMDK (`.vmdk`) | `macrt.vmdk` | Version 4 sparse extents (raw and zlib-compressed grains, with or without grain markers, grain directory in the footer) and version 3 COWD extents. |

All of these are read in full into memory before the volume readers see
them; files inside a volume are still read on demand.

## Unsupported

These formats are recognised as out of scope for now. They are not opened,
and a file in one of them will not browse as a volume.

**QEMU / virtual machine formats**

- **qcow** (version 1): the predecessor of qcow2. Not implemented; planned
  as a later batch if capacity allows.
- **QCOW3 / VHD differencing (type 4)**: refuse by name. A differencing
  VHD depends on a parent image whose bytes live in another file, and qcow
  versions beyond 3 are not read.
- **VHDX** (Hyper-V): the successor to VHD, with a different layout and
  log/journal metadata. Not implemented.
- **parallels (PRD)**: Parallels' own disk format. Not implemented.
- **bochs, cloop, dmg (UDIF), qcow (other versions)**: not implemented.
  UDIF/`.dmg` is tracked separately as task 26b (NDIF/UDIF).

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

Corrupt or unsupported images raise an error the Filer reports as an alert;
they never crash the application.
