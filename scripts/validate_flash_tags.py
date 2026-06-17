"""Validate deepseek-v4-flash tags against lookup table built from pro patterns.

Logic:
  - Take 10 random FT matches (have pro pattern already)
  - Run analyze_and_store(model="deepseek-v4-flash") on each
  - For each flash tag, lookup the base_rate from /tmp/lookup_table.json
  - Aggregate tags -> pick dominant signal (highest edge)
  - Compare prediction with actual outcome (fav_cover yes/no)

Goal: Measure whether lookup table (built from pro) generalizes to flash tags.

Run (in container):
    docker exec football_dashboard python3 /tmp/validate_flash.py
"""
from __future__ import annotations
import asyncio, json, os, sys, time
sys.path.insert(0, "/app")

from app.analyzer.ai_pattern import analyze_and_store
from app.database import get_match_by_id, _parse_open_line_to_float, _cover_score

MODEL = "deepseek-v4-flash"
LOOKUP_PATH = "/tmp/lookup_table.json"


def parse_match_score(line: str, side: str, hs: int, aws: int) -> tuple[bool, float]:
    """Return (fav_covered: bool, margin_from_fav: float)."""
    line_f = _parse_open_line_to_float(line)
    margin = (hs - aws) if side == "home" else (aws - hs)
    return _cover_score(line_f, margin) > 0.5, margin


async def main():
    # Load lookup table
    with open(LOOKUP_PATH) as f:
        lu = json.load(f)
    lookup_by_tag = {x["tag"]: x for x in lu["lookup"]}
    print(f"Lookup table loaded: {len(lookup_by_tag)} tags")
    print()

    # Query 10 random FT matches that have pro pattern (parse_ok=TRUE + open_hc)
    from app.database import _connect
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT m.id FROM matches m
            JOIN match_patterns p ON p.match_id = m.id
            WHERE p.parse_ok = TRUE
              AND p.open_hc IS NOT NULL
              AND p.open_hc_side IN ('home','away')
              AND p.open_hc <> '0'
              AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
            ORDER BY random()
            LIMIT 10
        """)
        match_ids = [r[0] for r in cur.fetchall()]
    print(f"Validating {len(match_ids)} matches with {MODEL}")
    print(f"Validating {len(match_ids)} matches with {MODEL}")
    print("=" * 100)
    print(f"{'#':>2} {'match':<30} {'hc':>5} {'side':>5} {'FT':>5} {'flash_tags':<30} {'top_signal':<15} {'pred':>5} {'act':>5} {'OK':>3}")
    print("=" * 100)

    rows = []
    t_start = time.time()
    for i, mid in enumerate(match_ids, 1):
        try:
            r = await analyze_and_store(mid, model=MODEL)
            tags = r.get("tags") or []
            flash_ok = r.get("parse_ok", False)
            finish = r.get("finish_reason", "?")
        except Exception as e:
            print(f"{i:>2}. {mid[:30]:<30} ERROR: {e}")
            rows.append({"match_id": mid, "error": str(e)})
            continue

        # Skip if flash failed to parse
        if not flash_ok or not tags:
            print(f"{i:>2}. {mid[:30]:<30} (flash no parse, finish={finish})")
            rows.append({"match_id": mid, "tags": tags, "flash_ok": False})
            continue

        # Get match data + open_hc from stored pattern (pro version has it)
        from app.database import get_match_pattern
        pro_pattern = get_match_pattern(mid, model="deepseek-v4-pro")
        match = get_match_by_id(mid)
        if not pro_pattern or not match:
            print(f"{i:>2}. {mid[:30]:<30} (no pro pattern or match)")
            continue

        open_hc = pro_pattern.get("open_hc")
        open_hc_side = pro_pattern.get("open_hc_side")
        hs = match.get("home_score")
        aws = match.get("away_score")

        # Actual fav_cover outcome
        actual_fav_cover, actual_margin = parse_match_score(open_hc, open_hc_side, hs, aws)

        # Find tags in lookup
        matched_tags = [(t, lookup_by_tag[t]) for t in tags if t in lookup_by_tag]

        # Predict: use tag with strongest edge
        if matched_tags:
            best_tag, best_lu = max(matched_tags, key=lambda kv: abs(kv[1]["fav_cover_rate"] - 0.5))
            pred_fav_cover = best_lu["fav_cover_rate"] > 0.5
            top_signal = f"{best_tag} ({best_lu['fav_cover_rate']*100:.0f}%)"
        else:
            pred_fav_cover = None  # base rate = 50%
            top_signal = "(none)"

        ok = "✓" if pred_fav_cover == actual_fav_cover else ("-" if pred_fav_cover is None else "✗")
        tags_str = ",".join(tags[:5]) + ("..." if len(tags) > 5 else "")

        short_match = f"{match.get('home','')[:12]} vs {match.get('away','')[:12]}"
        print(f"{i:>2}. {short_match:<30} {open_hc or '?':>5} {open_hc_side or '?':>5} {hs}-{aws:<3} "
              f"{tags_str:<30} {top_signal:<15} "
              f"{'fav' if pred_fav_cover else ('dog' if pred_fav_cover is False else '?'):>5} "
              f"{'fav' if actual_fav_cover else 'dog':>5} {ok:>3}")

        rows.append({
            "match_id": mid,
            "match_short": short_match,
            "open_hc": open_hc,
            "open_hc_side": open_hc_side,
            "actual_score": f"{hs}-{aws}",
            "actual_fav_cover": actual_fav_cover,
            "actual_margin": actual_margin,
            "flash_tags": tags,
            "matched_tags": [t for t, _ in matched_tags],
            "top_signal": top_signal,
            "predicted_fav_cover": pred_fav_cover,
            "correct": pred_fav_cover == actual_fav_cover,
        })

    elapsed = time.time() - t_start
    print()
    print("=" * 100)
    valid = [r for r in rows if "correct" in r]
    correct = sum(1 for r in valid if r["correct"])
    n = len(valid)
    print(f"VALIDATION SUMMARY ({MODEL}) — elapsed {elapsed:.0f}s")
    print(f"  Flash parsed OK: {sum(1 for r in rows if r.get('flash_ok', True))}/{len(rows)}")
    print(f"  Predictions: {n}")
    print(f"  Correct: {correct}/{n} = {correct/n*100:.1f}%" if n else "  No valid predictions")
    print(f"  Baseline (always fav): see 'actual_fav_cover' column above")
    fav_count = sum(1 for r in valid if r["actual_fav_cover"])
    print(f"  Actual fav_cover rate: {fav_count}/{n} = {fav_count/n*100:.1f}%" if n else "")

    # Save raw results
    with open("/tmp/validation_results.json", "w") as f:
        json.dump({
            "model": MODEL,
            "lookup_source": "deepseek-v4-pro patterns (385 OK, 16 tags)",
            "elapsed_sec": round(elapsed, 1),
            "n_matches": len(rows),
            "n_parsed": sum(1 for r in rows if r.get("flash_ok", True)),
            "n_predicted": n,
            "n_correct": correct,
            "accuracy": correct / n if n else None,
            "actual_fav_cover_rate": fav_count / n if n else None,
            "rows": rows,
        }, f, indent=2, default=str)
    print(f"\nResults saved to /tmp/validation_results.json")


asyncio.run(main())
