"""Deterministic momentum features evaluation.

Hypothesis: certain pre-FT features predict whether fav covers,
without any LLM leakage.

Features (computed ONLY from data <= 75'):
  - score_at_75: home, away (separate feature, not leaked to model)
  - minutes_since_last_goal: from match_goals.minute
  - hc_drift_at_75: home_handicap_odds change from opening to 75'
  - ou_drift_at_75: over_odds change from opening to 75'
  - total_goals_so_far: home_score + away_score at 75'

Target: did fav cover? (compute from FT score vs open_hc)

Validation: aggregate over matches, see if any feature has signal.

Usage (in container):
    docker exec football_dashboard python3 /tmp/momentum_eval.py
"""
from __future__ import annotations
import json, sys, statistics
sys.path.insert(0, "/app")

from app.database import _connect, _parse_open_line_to_float, _cover_score


def get_features_at_minute(match_id: str, minute: int) -> dict | None:
    """Pull odds_history + goals features, all <= minute cutoff.

    Returns dict or None if insufficient data.
    """
    with _connect() as conn:
        cur = conn.cursor()

        # 1. Get opening line (first snapshot)
        cur.execute("""
            SELECT home_handicap, home_handicap_odds, away_handicap_odds,
                   ou_line, over_odds, under_odds
            FROM match_odds_history
            WHERE match_id = %s AND home_handicap IS NOT NULL AND minute IS NOT NULL
            ORDER BY minute ASC, captured_at ASC LIMIT 1
        """, (match_id,))
        first = cur.fetchone()
        if not first:
            return None
        open_hc, open_hh, open_ah, open_ou, open_over, open_under = first

        # 2. Get last snapshot <= minute
        cur.execute("""
            SELECT home_score, away_score, home_handicap_odds, away_handicap_odds,
                   over_odds, under_odds
            FROM match_odds_history
            WHERE match_id = %s AND minute <= %s
              AND home_score IS NOT NULL AND away_score IS NOT NULL
            ORDER BY minute DESC, captured_at DESC LIMIT 1
        """, (match_id, minute))
        last = cur.fetchone()
        if not last:
            return None
        score_h, score_a, hh_75, ah_75, over_75, under_75 = last

        # 3. Time since last goal (from match_goals)
        cur.execute("""
            SELECT minute FROM match_goals
            WHERE match_id = %s AND minute <= %s AND minute IS NOT NULL
            ORDER BY minute DESC LIMIT 1
        """, (match_id, minute))
        last_goal_row = cur.fetchone()
        last_goal_min = last_goal_row[0] if last_goal_row else None
        mins_since_goal = (minute - last_goal_min) if last_goal_min is not None else minute

        # 4. Total goals so far
        total_goals = score_h + score_a

        return {
            "score_at_75": f"{score_h}-{score_a}",
            "total_goals": total_goals,
            "minutes_since_last_goal": mins_since_goal,
            "hc_drift": (hh_75 - open_hh) if (hh_75 and open_hh) else None,
            "ou_drift": (over_75 - open_over) if (over_75 and open_over) else None,
            "open_hc": open_hc,
            "open_hh_odds": open_hh,
            "open_ah_odds": open_ah,
        }


def cover_outcome(open_hc: str, ft_h: int, ft_a: int) -> tuple[bool | None, str]:
    line_f = _parse_open_line_to_float(open_hc)
    if line_f is None or line_f == 0:
        return None, "level"
    side = "home" if line_f < 0 else "away"
    margin = (ft_h - ft_a) if side == "home" else (ft_a - ft_h)
    return _cover_score(line_f, margin) > 0.5, side


def main():
    # Pull all FT matches with >= 75 snapshot + FT score
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT m.id, m.home_score, m.away_score, m.competition
            FROM matches m
            WHERE m.status = 'FT'
              AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM match_odds_history o
                  WHERE o.match_id = m.id AND o.minute <= 75
                    AND o.home_handicap IS NOT NULL
                    AND o.home_score IS NOT NULL
              )
              AND EXISTS (
                  SELECT 1 FROM match_odds_history o
                  WHERE o.match_id = m.id AND o.home_handicap IS NOT NULL
              )
              AND (m.home_score + m.away_score) >= 1
            ORDER BY random()
            LIMIT 200
        """)
        matches = cur.fetchall()

    print(f"Matches with pre-75 data + FT scores: {len(matches)}")

    rows = []
    for mid, ft_h, ft_a, comp in matches:
        feats = get_features_at_minute(mid, 75)
        if not feats:
            continue
        cover, side = cover_outcome(feats["open_hc"], ft_h, ft_a)
        if cover is None:
            continue
        rows.append({
            **feats,
            "ft_score": f"{ft_h}-{ft_a}",
            "fav_side": side,
            "fav_covered": cover,
            "match_id": mid,
        })

    print(f"Valid matches: {len(rows)}\n")

    # === ANALYSIS 1: minutes_since_last_goal ===
    print("=== FEATURE 1: minutes_since_last_goal ===")
    buckets = [(0, 5), (5, 10), (10, 20), (20, 45), (45, 75)]
    for lo, hi in buckets:
        sub = [r for r in rows if lo <= r["minutes_since_last_goal"] < hi]
        if not sub:
            continue
        n = len(sub)
        fav_cov = sum(1 for r in sub if r["fav_covered"])
        rate = fav_cov / n
        print(f"  [{lo:>3}-{hi:>3} min): n={n:>3}, fav_cover_rate={rate*100:>5.1f}% ({fav_cov}/{n})")
    print()

    # === ANALYSIS 2: total_goals at 75 ===
    print("=== FEATURE 2: total_goals at 75' ===")
    for tg in [0, 1, 2, 3, 4, 5]:
        sub = [r for r in rows if r["total_goals"] == tg]
        if not sub:
            continue
        n = len(sub)
        fav_cov = sum(1 for r in sub if r["fav_covered"])
        rate = fav_cov / n
        print(f"  total={tg}: n={n:>3}, fav_cover_rate={rate*100:>5.1f}% ({fav_cov}/{n})")
    print()

    # === ANALYSIS 3: hc_drift (positive = odds for fav lengthened = bad for fav) ===
    print("=== FEATURE 3: hc_drift (home_handicap_odds change) ===")
    drift_buckets = [(-1.0, -0.1), (-0.1, 0), (0, 0.05), (0.05, 0.15), (0.15, 1.0)]
    for lo, hi in drift_buckets:
        sub = [r for r in rows if r["hc_drift"] is not None and lo <= r["hc_drift"] < hi]
        if not sub:
            continue
        n = len(sub)
        fav_cov = sum(1 for r in sub if r["fav_covered"])
        rate = fav_cov / n
        print(f"  drift [{lo:>5.2f},{hi:>5.2f}): n={n:>3}, fav_cover_rate={rate*100:>5.1f}% ({fav_cov}/{n})")
    print()

    # === ANALYSIS 4: score_at_75 leading margin ===
    print("=== FEATURE 4: score margin at 75' (fav POV) ===")
    margin_buckets = [(-3, -1), (-1, 0), (0, 1), (1, 2), (2, 4)]
    for lo, hi in margin_buckets:
        sub = []
        for r in rows:
            sh, sa = map(int, r["score_at_75"].split("-"))
            margin = (sh - sa) if r["fav_side"] == "home" else (sa - sh)
            if lo <= margin < hi:
                sub.append(r)
        if not sub:
            continue
        n = len(sub)
        fav_cov = sum(1 for r in sub if r["fav_covered"])
        rate = fav_cov / n
        print(f"  margin [{lo:>2},{hi:>2}): n={n:>3}, fav_cover_rate={rate*100:>5.1f}% ({fav_cov}/{n})")
    print()

    # === BASELINE ===
    n = len(rows)
    baseline = sum(1 for r in rows if r["fav_covered"]) / n
    print(f"=== BASELINE: fav_cover_rate over all {n} matches = {baseline*100:.1f}% ===")

    # Save raw
    with open("/tmp/momentum_results.json", "w") as f:
        json.dump({"n": n, "baseline_fav_cover_rate": baseline, "rows": rows}, f, indent=2, default=str)
    print(f"\nSaved /tmp/momentum_results.json ({n} rows)")


main()
