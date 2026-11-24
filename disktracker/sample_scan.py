\"\"\"Sample script demonstrating disktracker library usage.\"\"\"

from pathlib import Path
from disktracker import build_dtc, parse_dtc, write_dtc, Item

def main():
    print("DiskTracker sample script running...")
    
    # 1. Build a catalog from a sample HFS image or test volume if present
    sample_img = Path("/tmp/opencode/test.hfs")
    if sample_img.exists():
        print(f"Indexing {sample_img}...")
        catalog_bytes = build_dtc([sample_img])
        out_dtc = Path("/tmp/opencode/sample_output.dtc")
        out_dtc.write_bytes(catalog_bytes)
        print(f"Wrote {len(catalog_bytes)} bytes to {out_dtc}")
        
        # 2. Parse the catalog back
        items = parse_dtc(catalog_bytes)
        print(f"Parsed {len(items)} records from catalog:")
        for it in items[:10]:
            print(f"  {it.kind:7} {it.name!r:20} depth={it.depth} size={it.size}")
            
    else:
        print("No sample HFS image at /tmp/opencode/test.hfs. Creating minimal item list.")
        items = [
            Item(kind='disk', name='Sample Disk', created=0, modified=0, items=2),
            Item(kind='folder', name='Documents', depth=1, items=1, descendants=1),
            Item(kind='file', name='ReadMe.txt', depth=2, size=1024, type=b'TEXT', creator=b'ttxt')
        ]
        data = write_dtc(items)
        out_dtc = Path("/tmp/opencode/minimal.dtc")
        out_dtc.write_bytes(data)
        print(f"Wrote minimal DTC: {len(data)} bytes to {out_dtc}")

if __name__ == '__main__':
    main()
