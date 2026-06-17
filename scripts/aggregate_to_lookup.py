"""Build lookup table từ aggregate_patterns() — dùng cho formula betting.

Step:
  1. aggregate_patterns(prestigious_only=False)
  2. Lọc tag: valid_n >= MIN_N (=15) AND ci_half_width < MAX_WIDTH (=0.15)
  3. Save thành lookup table in-memory + dump ra JSON để dùng cho validation

Usage (chạy trong container football_dashboard):
    docker exec football_dashboard python3 /tmp/build_lookup.py
    -> in ra JSON lookup + save /tmp/lookup_table.json
"""
from __future__ import annotations
import json, os, sys
sys.path.insert(0, "/app")

from app.database import aggregate_patterns

# Tuning — phải đủ statistical power
MIN_VALID_N = 15     # min 15 patterns có tag + đủ data
MAX_CI_HALF = 0.15   # CI95 width / 2 < 15% → khoảng tin cậy hẹp

print("=" * 70)
print("BUILD LOOKUP TABLE từ aggregate_patterns()")
print("=" * 70)

result = aggregate_patterns(prestigious_only=False, limit=2000)
top_tags = result.get("top_tags") or []

print(f"\nn_patterns (parse_ok=TRUE): {result['n_patterns']}")
print(f"avg_confidence: {result['avg_confidence']}")
print(f"\nFilter: valid_n >= {MIN_VALID_N} AND ci_half_width < {MAX_CI_HALF}")
print()

# Lọc + build lookup
lookup = []
for entry in top_tags:
    if not isinstance(entry, dict):
        continue
    vn = entry.get("valid_n") or 0
    hw = entry.get("ci_half_width")
    if vn < MIN_VALID_N or hw is None or hw >= MAX_CI_HALF:
        continue
    tag = entry["tag"]
    rate = entry["fav_cover_rate_actual"]
    lo = entry["ci95_low"]
    hi = entry["ci95_high"]
    lookup.append({
        "tag": tag,
        "n": entry["n"],
        "valid_n": vn,
        "fav_cover_rate": rate,
        "ci95_low": lo,
        "ci95_high": hi,
        "ci_half_width": hw,
        "avg_margin": entry.get("avg_margin"),
        "avg_total_goals": entry.get("avg_total_goals"),
    })

# Sort by |rate - 0.5| desc (tín hiệu mạnh nhất trước)
lookup.sort(key=lambda x: abs(x["fav_cover_rate"] - 0.5), reverse=True)

print(f"{'TAG':<25} {'n':>4} {'valid':>5} {'fav_cov%':>9} {'CI95':>17} {'edge%':>7}")
print("-" * 75)
for x in lookup:
    edge = (x["fav_cover_rate"] - 0.5) * 100
    print(f"{x['tag']:<25} {x['n']:>4} {x['valid_n']:>5} "
          f"{x['fav_cover_rate']*100:>8.1f}% "
          f"[{x['ci95_low']*100:>3.0f}-{x['ci95_high']*100:>3.0f}%] "
          f"{edge:>+6.1f}%")

print(f"\n=> {len(lookup)} tags pass filter")

# Save as JSON (in-container)
with open("/tmp/lookup_table.json", "w") as f:
    json.dump({
        "built_from": {
            "n_patterns": result["n_patterns"],
            "model_filter": "all patterns (parse_ok=TRUE)",
        },
        "filters": {"min_valid_n": MIN_VALID_N, "max_ci_half": MAX_CI_HALF},
        "lookup": lookup,
    }, f, indent=2)
print(f"\nSaved to /tmp/lookup_table.json ({os.path.getsize('/tmp/lookup_table.json')} bytes)")
