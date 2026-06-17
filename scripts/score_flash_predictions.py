"""Compare flash tags vs lookup table (built from pro) -> measure accuracy.

Reuses existing flash patterns in DB (no LLM call). For each match:
  - Get flash tags from match_patterns (model='deepseek-v4-flash')
  - Get actual FT outcome from matches
  - Apply lookup table to predict fav_cover (use tag with strongest edge)
  - Compare predicted vs actual

Run (in container):
    docker exec football_dashboard python3 /tmp/score_flash.py
"""
from __future__ import annotations
import json, sys
sys.path.insert(0, "/app")

from app.database import (
    _connect, _parse_open_line_to_float, _cover_score,
    get_match_pattern, get_match_by_id,
)

MODEL = "deepseek-v4-flash"
LOOKUP_PATH = "/tmp/lookup_table.json"


def cover(open_hc: str, side: str, hs: int, aws: int):
    line_f = _parse_open_line_to_float(open_hc)
    # Convention: open_hc < 0 = home chấp → home is fav
    #              open_hc > 0 = home được chấp → away is fav
    if side == "home":
        margin = hs - aws
    elif side == "away":
        margin = aws - hs
    elif side is None:
        # Derive from open_hc sign (when open_hc_side missing)
        if line_f < 0:
            side = "home"; margin = hs - aws
        elif line_f > 0:
            side = "away"; margin = aws - hs
        else:
            return None, 0  # level, can't compute cover
    else:
        return None, 0
    return _cover_score(line_f, margin) > 0.5, margin


def main():
    with open(LOOKUP_PATH) as f:
        lu = json.load(f)
    lookup_by_tag = {x["tag"]: x for x in lu["lookup"]}
    print(f"Loaded lookup table: {len(lookup_by_tag)} tags")

    # Get all flash patterns with parse_ok=TRUE
    # Note: flash may not have open_hc_side — derive from open_hc sign
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.match_id, p.tags, p.confidence, p.open_hc, p.open_hc_side,
                   m.home_score, m.away_score, m.home, m.away, m.competition
            FROM match_patterns p
            JOIN matches m ON m.id = p.match_id
            WHERE p.model = %s
              AND p.parse_ok = TRUE
              AND p.open_hc IS NOT NULL
              AND p.open_hc <> '0'
              AND m.home_score IS NOT NULL
              AND m.away_score IS NOT NULL
            ORDER BY p.updated_at DESC
            LIMIT 50
        """, (MODEL,))
        rows = cur.fetchall()

    print(f"Flash patterns to score: {len(rows)}")
    print()
    print(f"{'#':>2} {'match':<28} {'hc':>5} {'side':>5} {'FT':>5} "
          f"{'flash_tags':<35} {'top_signal':<25} {'pred':>5} {'act':>5} {'OK':>3}")
    print("=" * 130)

    n = 0
    correct = 0
    fav_actual = 0
    flash_no_lookup = 0
    rows_detail = []
    for i, (mid, tags, conf, ohc, ohcs, hs, aws, home, away, comp) in enumerate(rows, 1):
        # Actual outcome
        actual_cover, actual_margin = cover(ohc, ohcs, hs, aws)
        fav_actual += int(actual_cover)

        # Find matching tags in lookup
        tags_list = tags or []
        matched = [(t, lookup_by_tag[t]) for t in tags_list if t in lookup_by_tag]

        if not matched:
            flash_no_lookup += 1
            pred = None
            top_signal = "(no lookup tag)"
        else:
            # Pick strongest signal
            best_tag, best_lu = max(matched, key=lambda kv: abs(kv[1]["fav_cover_rate"] - 0.5))
            pred = best_lu["fav_cover_rate"] > 0.5
            top_signal = f"{best_tag}={best_lu['fav_cover_rate']*100:.0f}%(n={best_lu['valid_n']})"

        if pred is None:
            ok = "-"
        else:
            n += 1
            is_ok = pred == actual_cover
            correct += int(is_ok)
            ok = "✓" if is_ok else "✗"

        short = f"{home[:10]} vs {away[:10]}"
        tags_str = ",".join(tags_list[:6]) + ("..." if len(tags_list) > 6 else "")
        side_show = ohcs or "-"
        print(f"{i:>2}. {short:<28} {ohc:>5} {side_show:>5} {hs}-{aws:<3} "
              f"{tags_str[:35]:<35} {top_signal[:25]:<25} "
              f"{'fav' if pred else ('dog' if pred is False else '?'):>5} "
              f"{'fav' if actual_cover else 'dog':>5} {ok:>3}")
        rows_detail.append({
            "match_id": mid, "match": short,
            "open_hc": ohc, "side": ohcs, "ft": f"{hs}-{aws}",
            "actual_fav_cover": actual_cover,
            "flash_tags": tags_list,
            "matched_tags": [t for t, _ in matched],
            "top_signal": top_signal,
            "predicted_fav_cover": pred,
            "correct": pred == actual_cover if pred is not None else None,
        })

    print()
    print("=" * 130)
    print(f"VALIDATION SUMMARY — model: {MODEL}")
    print(f"  Total flash patterns: {len(rows)}")
    print(f"  Has matching lookup tag: {len(rows) - flash_no_lookup} ({flash_no_lookup} skipped)")
    print(f"  Predictions made: {n}")
    print(f"  Correct: {correct}/{n} = {correct/n*100:.1f}%" if n else "  No predictions")
    print(f"  Actual fav_cover rate: {fav_actual}/{len(rows)} = {fav_actual/len(rows)*100:.1f}%")
    # Baseline: if we always predicted 'fav', accuracy would be fav_actual/n
    if n:
        always_fav_acc = fav_actual / n
        always_dog_acc = (n - fav_actual) / n
        print(f"  Baseline (always fav): {fav_actual}/{n} = {always_fav_acc*100:.1f}%")
        print(f"  Baseline (always dog): {n-fav_actual}/{n} = {always_dog_acc*100:.1f}%")
        print(f"  Lookup advantage over best baseline: {(correct/n - max(always_fav_acc, always_dog_acc))*100:+.1f}%")

    with open("/tmp/validation_results.json", "w") as f:
        json.dump({
            "model": MODEL,
            "n_patterns_in_db": len(rows),
            "n_with_lookup_match": n,
            "n_correct": correct,
            "accuracy": correct/n if n else None,
            "actual_fav_cover_rate": fav_actual/len(rows),
            "always_fav_baseline": fav_actual/n if n else None,
            "always_dog_baseline": (n-fav_actual)/n if n else None,
            "rows": rows_detail,
        }, f, indent=2, default=str)
    print(f"\nResults: /tmp/validation_results.json")


main()
