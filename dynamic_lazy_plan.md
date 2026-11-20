# Implementation Plan: Dynamic Lazy-Loading & Volume Eviction

## Objective
Implement a memory guard and LRU volume cache with dynamic fallback from in-memory loading to lazy streaming/mmap loading when memory limits are reached.

## Proposed Changes
1. **Volume Manager / Cache (`macrt/macfile.py`)**:
   - Track total memory used by loaded volume buffers.
   - Set a high-water mark memory guard (e.g., 512 MB).
   - Implement an LRU eviction policy for volumes when the limit is hit, converting them to lazy stream/mmap-backed access instead of dropping them entirely or keeping them fully buffered in RAM.
2. **Comparison & Testing (`tests/`)**:
   - Add a benchmark or survey script test comparing memory footprint and read speeds with and without the memory guard.
3. **Review**:
   - Inspect memory consumption during recursive Filer browsing of large archives/images.
