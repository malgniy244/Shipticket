"""Quick grouping preview for SKM_C250i26070816530.pdf (18 pages)."""
import json, sys
sys.path.insert(0, '/home/ubuntu/ship-ticket-splitter')
from grouping import group_detections, Candidate, PageDetection

WHITELIST = ["300574", "300600", "300573", "253027-1"]

with open('/tmp/detect_file3_results_v2.json') as f:
    raw = json.load(f)

# group_detections expects raw dicts as produced by detect.py
result = group_detections(raw, WHITELIST)

print("=== BLOCKS ===")
for b in result.blocks:
    print(f"  ticket={b.ticket!r:15s}  pages={b.pages}  flags={b.flags}")
    if b.unmatched_raw:
        print(f"    unmatched_raw={b.unmatched_raw}")
    if b.neighbor_suggestion:
        print(f"    neighbor_suggestion={b.neighbor_suggestion!r}")

print(f"\n=== MISSING TICKETS: {result.missing_tickets} ===")
print(f"=== UNMATCHED VALUES: {result.unmatched_values} ===")
