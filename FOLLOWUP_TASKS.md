# Follow-up tasks

Identified while closing out the Filer implementation plan, and not done
there. One open task; the point is the user-facing behavior, not the
internal design.

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
