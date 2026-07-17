#!/usr/bin/env python3
"""Run detection on fixture #6 PDF and print ground truth for freezing."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from detect import run_detection_fast, parse_whitelist
import fitz

PDF_PATH = "/home/ubuntu/upload/SKM_C250i26071015110(1).pdf"
WHITELIST_RAW = "01053,299198,298404,300588,300871,300291"

whitelist = parse_whitelist(WHITELIST_RAW)
print(f"Whitelist ({len(whitelist)}): {whitelist}")

doc = fitz.open(PDF_PATH)
total_pages = len(doc)
print(f"Pages: {total_pages}")
doc.close()

# Run detection
results = run_detection_fast(
    pdf_path=PDF_PATH,
    whitelist=whitelist,
    progress_callback=lambda p, n: print(f"  progress: {p}/{n}"),
)

print(f"\nPink pages detected: {sorted([p for p,r in results.items() if r.get('is_pink')])}")
print(f"\nAll page results:")
for page_num in sorted(results.keys()):
    r = results[page_num]
    print(f"  p{page_num}: is_pink={r.get('is_pink')} ticket={r.get('ticket')} conf={r.get('confidence','?')}")
