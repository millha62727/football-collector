"""Honest evaluation of tag-outcome relationship WITHOUT leakage.

Leakage test: re-run analyze_and_store() with env STRIP_EFFECTIVE_SCORE=1
so the LLM never sees the score-at-cutoff. Then aggregate tags and compare
their fav_cover rates to the leaked baseline (built earlier from pro patterns
with effective_score visible).

If rates drop dramatically (e.g. fav_cover from 97.9% → 55%), confirms the
high rate was hindsight-driven. If they hold, the model has a real signal.

Usage (in container):
    STRIP_EFFECTIVE_SCORE=1 docker exec -e STRIP_EFFECTIVE_SCORE=1 \
        football_dashboard python3 /tmp/honest_eval.py
"""
from __future__ import annotations
import asyncio, json, os, sys, time
sys.path.insert(0, "/app")

# Sanity check: must be invoked with STRIP_EFFECTIVE_SCORE=1
if os.getenv("STRIP_EFFECTIVE_SCORE") != "1":
    print("ERROR: must set STRIP_EFFECTIVE_SCORE=1 before running this script")
    print("       docker exec -e STRIP_EFFECTIVE_SCORE=1 football_dashboard \\")
    print("           python3 /tmp/honest_eval.py")
    sys.exit(1)

from app.analyzer.ai_pattern import analyze_and_store
from app.database import (
    _connect, _parse_open_line_to_float, _cover_score,
    get_match_by_id,
)

MODEL = "deepseek-v4-flash"
N_MATCHES = 20
LOOKUP_PATH = "/tmp/lookup_table.json"  # built from LEAKED pro patterns


def cover(open_hc: str, hs: int, aws: int):
    """Derive side from open_hc sign (since flash may not store open_hc_side)."""
    line_f = _parse_open_line_to_float(open_hc)
    if line_f is None or line_f == 0:
        return None, 0
    if line_f < 0:
        side = "home"; margin = hs - aws
    else:
        side = "away"; margin = aws - hs
    return _cover_score(line_f, margin) > 0.5, margin


async def main():
    # Load lookup (LEAKED baseline — built from pro patterns WITH effective_score)
    with open(LOOKUP_PATH) as f:
        lu = json.load(f)
    lookup_by_tag = {x["tag"]: x for x in lu["lookup"]}
    print(f"Lookup table (LEAKED baseline): {len(lookup_by_tag)} tags")

    # Pick 20 random FT matches with odds history at 75'
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT m.id, m.home, m.away FROM matches m
            WHERE m.status='FT'
              AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM match_odds_history o
                  WHERE o.match_id = m.id
                    AND o.minute IS NOT NULL AND o.minute >= 75
              )
              AND (SELECT COUNT(*) FROM match_odds_history o WHERE o.match_id=m.id) >= 20
            ORDER BY random() LIMIT %s
        """, (N_MATCHES,))
        match_ids = [r[0] for r in cur.fetchall()]
    print(f"Selected {len(match_ids)} matches\n")

    print(f"{'#':>2} {'match':<24} {'hc':>5} {'FT':>5} "
          f"{'flash_tags':<35} {'top_signal':<22} {'pred':>4} {'act':>4} {'OK':>3}")
    print("=" * 115)

    rows = []
    t0 = time.time()
    for i, mid in enumerate(match_ids, 1):
        try:
            r = await analyze_and_store(mid, model=MODEL)
        except Exception as e:
            print(f"{i:>2}. {mid[:20]:<24} ERROR: {e}")
            continue

        tags = r.get("tags") or []
        flash_ok = r.get("parse_ok", False)
        finish = r.get("finish_reason")

        match = get_match_by_id(mid)
        if not match:
            continue
        hs, aws = match.get("home_score"), match.get("away_score")

        # Get open_hc from the pattern we just stored (it includes open_hc from features)
        from app.database import get_match_pattern
        new_pat = get_match_pattern(mid, model=MODEL)
        if not new_pat:
            continue
        ohc = new_pat.get("open_hc")
        actual_cover, _ = cover(ohc, hs, aws) if ohc else (None, 0)

        # Find matched tags in LEAKED lookup
        matched = [(t, lookup_by_tag[t]) for t in tags if t in lookup_by_tag]
        if matched:
            best_tag, best_lu = max(matched, key=lambda kv: abs(kv[1]["fav_cover_rate"] - 0.5))
            pred = best_lu["fav_cover_rate"] > 0.5
            top_signal = f"{best_tag}={best_lu['fav_cover_rate']*100:.0f}%(n={best_lu['valid_n']})"
        else:
            pred = None
            top_signal = "(no lookup tag)"

        if pred is None or actual_cover is None:
            ok = "-"
        else:
            ok = "✓" if pred == actual_cover else "✗"

        short = f"{match['home'][:10]} vs {match['away'][:10]}"
        tags_str = ",".join(tags[:6]) + ("..." if len(tags) > 6 else "")
        print(f"{i:>2}. {short:<24} {ohc or '?':>5} {hs}-{aws:<3} "
              f"{tags_str[:35]:<35} {top_signal[:22]:<22} "
              f"{'fav' if pred else ('dog' if pred is False else '?'):>4} "
              f"{'fav' if actual_cover else ('dog' if actual_cover is False else '?'):>4} {ok:>3}")

        rows.append({
            "match_id": mid, "match": short, "open_hc": ohc,
            "ft": f"{hs}-{aws}", "actual_fav_cover": actual_cover,
            "flash_tags": tags, "matched_tags": [t for t, _ in matched],
            "top_signal": top_signal, "predicted_fav_cover": pred,
            "correct": (pred == actual_cover) if (pred is not None and actual_cover is not None) else None,
            "flash_parse_ok": flash_ok, "finish_reason": finish,
        })

    elapsed = time.time() - t0
    print()
    print("=" * 115)
    print(f"HONEST VALIDATION — model: {MODEL}, STRIP_EFFECTIVE_SCORE=1, elapsed {elapsed:.0f}s")
    valid = [r for r in rows if r["correct"] is not None]
    correct = sum(1 for r in valid if r["correct"])
    n = len(valid)
    fav_actual = sum(1 for r in valid if r["actual_fav_cover"])
    print(f"  Predictions: {n}")
    print(f"  Correct: {correct}/{n} = {correct/n*100:.1f}%" if n else "  N/A")
    print(f"  Actual fav_cover rate: {fav_actual}/{n} = {fav_actual/n*100:.1f}%" if n else "")
    if n:
        baseline_fav = fav_actual / n
        baseline_dog = (n - fav_actual) / n
        best_baseline = max(baseline_fav, baseline_dog)
        print(f"  Best baseline: {best_baseline*100:.1f}%")
        print(f"  Lookup advantage: {(correct/n - best_baseline)*100:+.1f}%")

    # Compare with leaked run (the 10/10 we ran earlier)
    print("\nCOMPARISON WITH LEAKED RUN (effective_score visible):")
    print("  Leaked run:  8/10 = 80.0% (vs baseline 60%, +20%)")
    print(f"  Honest run:  {correct}/{n} = {correct/n*100:.1f}% (vs baseline {best_baseline*100:.0f}%, {(correct/n-best_baseline)*100:+.1f}%)")

    with open("/tmp/honest_results.json", "w") as f:
        json.dump({
            "model": MODEL,
            "leakage_test": True,
            "env_STRIP_EFFECTIVE_SCORE": "1",
            "elapsed_sec": round(elapsed, 1),
            "n": n,
            "correct": correct,
            "accuracy": correct/n if n else None,
            "actual_fav_cover_rate": fav_actual/n if n else None,
            "rows": rows,
        }, f, indent=2, default=str)
    print("\nSaved /tmp/honest_results.json")


asyncio.run(main())
