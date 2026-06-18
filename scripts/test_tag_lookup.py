"""Smoke test for P20 tag_lookup() function.

Verifies:
  1. tag_lookup("fav_cover") returns expected base rate
  2. open_hc filter works
  3. open_ou filter works
  4. tier_breakdown populated
  5. year_breakdown populated
  6. insufficient_data flag works for tags with n < min_n
  7. nonexistent tag returns reason="no_rows"
"""
from __future__ import annotations
import json
import sys
sys.path.insert(0, "/app")

from app.database import tag_lookup


def show(label, data, expected_keys=None):
    print(f"\n--- {label} ---")
    if expected_keys:
        missing = [k for k in expected_keys if k not in data]
        if missing:
            print(f"  ❌ MISSING KEYS: {missing}")
        else:
            print(f"  ✓ all {len(expected_keys)} expected keys present")
    if data.get("insufficient_data"):
        print(f"  ⚠ insufficient_data: {data.get('reason')}")
    print(f"  n_total={data['n_total']}, valid_n={data['valid_n']}, rate={data['fav_cover_rate_actual']}")
    print(f"  CI95=[{data['ci95_low']}, {data['ci95_high']}]")
    if data.get("avg_confidence") is not None:
        print(f"  avg_confidence={data['avg_confidence']}")
    if data.get("tier_breakdown"):
        print(f"  tier_breakdown:")
        for t in data["tier_breakdown"]:
            print(f"    {t['tier']:<10} n={t['n']:>3} valid={t['valid_n']:>3} rate={t['fav_cover_rate_actual']} insufficient={t['insufficient_data']}")
    if data.get("year_breakdown"):
        print(f"  year_breakdown:")
        for y in data["year_breakdown"]:
            print(f"    {y['year']}  n={y['n']:>3} valid={y['valid_n']:>3} rate={y['fav_cover_rate_actual']} insufficient={y['insufficient_data']}")


def main():
    expected = ["tag", "filters", "n_total", "valid_n", "fav_cover_rate_actual",
                "ci95_low", "ci95_high", "ci_half_width", "avg_margin",
                "avg_total_goals", "avg_confidence", "insufficient_data",
                "tier_breakdown", "year_breakdown"]

    # 1. No filter
    show("1. tag=fav_cover (no filter)", tag_lookup("fav_cover"), expected)

    # 2. With hc filter
    show("2. tag=fav_cover, open_hc=-0.5", tag_lookup("fav_cover", open_hc="-0.5"), expected)

    # 3. With ou filter
    show("3. tag=fav_cover, open_ou=2.5", tag_lookup("fav_cover", open_ou="2.5"), expected)

    # 4. With both filters
    show("4. tag=clean_sheet_away, hc=-0.5, ou=2.5", tag_lookup("clean_sheet_away", open_hc="-0.5", open_ou="2.5"), expected)

    # 5. Insufficient data
    show("5. tag=small_sample, min_n=999 (force insufficient)", tag_lookup("small_sample", min_n=999), expected)

    # 6. Nonexistent tag
    show("6. tag=does_not_exist (should be no_rows)", tag_lookup("does_not_exist_zzz"), expected)

    # 7. Empty tag handling — should not crash
    try:
        result = tag_lookup("", min_n=1)
        print(f"\n--- 7. tag='' (empty) ---")
        print(f"  n_total={result['n_total']}, insufficient={result['insufficient_data']}, reason={result.get('reason')}")
    except Exception as e:
        print(f"\n--- 7. tag='' (empty) --- ❌ raised {type(e).__name__}: {e}")

    print("\n\n✓ All smoke tests complete.")


if __name__ == "__main__":
    main()
