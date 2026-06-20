"""Binary prediction: 'will there be ANOTHER goal after 75'?'

Target: any_goal_after_75 (1 if ft_total > score_at_75, else 0)

Features computed at 75' (NO leakage):
  - score_at_75: home, away
  - total_goals_at_75
  - margin_at_75 (home - away)
  - time_since_last_goal (from match_goals.occurred_at)
  - open_ou_line
  - over_odds_drift (over_odds_at_75 - over_odds_opening)
  - under_odds_drift
  - score_at_75 × open_ou interaction

Method:
  1. Aggregate over all FT matches with valid 75' data
  2. Bucket each feature, compute 'rate of late goal' per bucket
  3. For 2-feature combos: rate of late goal per (bucket_A, bucket_B) cell
  4. OOS test: train 2025, test 2026 (if profitable cells survive)

Note: only uses deterministic features — no LLM, no leakage.
"""
from __future__ import annotations
import sys, json
sys.path.insert(0, "/app")

from app.database import _connect


def main():
    with _connect() as conn:
        cur = conn.cursor()

        # Single-feature analysis: pull all needed data
        cur.execute("""
            WITH matches_with_data AS (
              SELECT m.id, m.home_score AS ft_h, m.away_score AS ft_a,
                     m.start_time_utc
              FROM matches m
              WHERE m.status='FT' AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
                AND EXISTS (
                    SELECT 1 FROM match_odds_history o
                    WHERE o.match_id = m.id AND o.minute <= 75
                      AND o.home_handicap IS NOT NULL AND o.home_score IS NOT NULL
                )
            ),
            opening AS (
              SELECT DISTINCT ON (match_id)
                match_id, home_handicap AS open_hc, ou_line AS open_ou,
                over_odds AS open_over_odds, under_odds AS open_under_odds
              FROM match_odds_history
              WHERE home_handicap IS NOT NULL AND ou_line IS NOT NULL
              ORDER BY match_id, minute ASC, captured_at ASC
            ),
            at_75 AS (
              SELECT DISTINCT ON (match_id)
                match_id, home_score AS h_75, away_score AS a_75,
                over_odds AS over_75, under_odds AS under_75
              FROM match_odds_history
              WHERE minute <= 75 AND home_score IS NOT NULL
                AND over_odds IS NOT NULL AND under_odds IS NOT NULL
              ORDER BY match_id, minute DESC, captured_at DESC
            ),
            last_goal AS (
              -- ONLY goals with occurred_at <= 75' (no look-ahead)
              SELECT DISTINCT ON (g.match_id)
                g.match_id, g.occurred_at
              FROM match_goals g
              JOIN matches m ON m.id = g.match_id
              WHERE m.start_time_utc IS NOT NULL AND g.occurred_at IS NOT NULL
                AND EXTRACT(EPOCH FROM (g.occurred_at - m.start_time_utc::timestamptz)) / 60.0 BETWEEN 0 AND 75
              ORDER BY g.match_id, g.occurred_at DESC
            )
            SELECT
              m.id,
              m.ft_h, m.ft_a,
              o.open_hc, o.open_ou,
              o.open_over_odds, o.open_under_odds,
              a.h_75, a.a_75,
              a.over_75, a.under_75,
              -- time since last goal in minutes (0 if no goal yet)
              EXTRACT(EPOCH FROM (lg.occurred_at - m.start_time_utc::timestamptz)) / 60.0 AS last_goal_min
            FROM matches_with_data m
            JOIN opening o ON o.match_id = m.id
            JOIN at_75 a ON a.match_id = m.id
            LEFT JOIN last_goal lg ON lg.match_id = m.id
        """)
        rows = cur.fetchall()

    n_total = len(rows)
    late_count = 0
    data = []
    for (mid, ft_h, ft_a, open_hc, open_ou, oo_open, uo_open,
         h_75, a_75, oo_75, uo_75, last_goal_min) in rows:
        score_75 = h_75 + a_75
        ft_total = ft_h + ft_a
        late = 1 if ft_total > score_75 else 0
        late_count += late

        # time since last goal at 75' (or 75 if no goal yet)
        ts_goal = min(75 - last_goal_min, 75) if last_goal_min is not None else 75

        # OU drift
        ou_drift = (oo_75 - oo_open) if oo_open else None
        u_drift = (uo_75 - uo_open) if uo_open else None

        data.append({
            "match_id": mid,
            "score_75": f"{h_75}-{a_75}",
            "total_75": score_75,
            "margin_75": h_75 - a_75,
            "open_ou": float(open_ou) if open_ou else None,
            "ts_goal": ts_goal,
            "ou_drift": ou_drift,
            "u_drift": u_drift,
            "late": late,
        })

    base_rate = late_count / n_total if n_total else 0
    print(f"=== DATASET ===")
    print(f"Total matches: {n_total}")
    print(f"Base rate (any goal after 75'): {late_count}/{n_total} = {base_rate*100:.2f}%")
    print()

    # === FEATURE 1: total goals at 75' ===
    print("=== F1: total_goals_at_75 ===")
    for tg in [0, 1, 2, 3, 4, 5]:
        sub = [d for d in data if d["total_75"] == tg]
        if len(sub) < 30: continue
        n = len(sub)
        rate = sum(d["late"] for d in sub) / n
        edge = (rate - base_rate) * 100
        print(f"  total={tg}: n={n}, late_rate={rate*100:.2f}%, edge vs base={edge:+.2f}pp")
    print()

    # === FEATURE 2: time since last goal ===
    print("=== F2: time_since_last_goal (min, capped at 75) ===")
    for lo, hi in [(0, 5), (5, 10), (10, 20), (20, 40), (40, 75)]:
        sub = [d for d in data if lo <= d["ts_goal"] < hi]
        if len(sub) < 30: continue
        n = len(sub)
        rate = sum(d["late"] for d in sub) / n
        edge = (rate - base_rate) * 100
        print(f"  [{lo:>3}, {hi:>3}): n={n}, late_rate={rate*100:.2f}%, edge={edge:+.2f}pp")
    print()

    # === FEATURE 3: OU drift ===
    print("=== F3: OU_drift (over_odds change from open to 75) ===")
    valid_ou = [d for d in data if d["ou_drift"] is not None]
    if valid_ou:
        # Sort by drift and bucket by quantile
        drifts = sorted(d["ou_drift"] for d in valid_ou)
        q1 = drifts[len(drifts)//4]
        q2 = drifts[len(drifts)//2]
        q3 = drifts[3*len(drifts)//4]
        for lo, hi, label in [
            (drifts[0], q1, "Q1 (most down)"),
            (q1, q2, "Q2-Q1"),
            (q2, q3, "Q3-Q2"),
            (q3, drifts[-1], "Q4 (most up)"),
        ]:
            sub = [d for d in valid_ou if lo <= d["ou_drift"] < hi]
            if len(sub) < 30: continue
            n = len(sub)
            rate = sum(d["late"] for d in sub) / n
            edge = (rate - base_rate) * 100
            print(f"  {label}: drift [{lo:+.3f}, {hi:+.3f}), n={n}, late_rate={rate*100:.2f}%, edge={edge:+.2f}pp")
    print()

    # === FEATURE 4: open_ou line ===
    print("=== F4: open_ou_line ===")
    ou_lines = sorted(set(d["open_ou"] for d in data if d["open_ou"] is not None))
    for line in ou_lines:
        sub = [d for d in data if d["open_ou"] == line]
        if len(sub) < 50: continue
        n = len(sub)
        rate = sum(d["late"] for d in sub) / n
        edge = (rate - base_rate) * 100
        print(f"  ou={line}: n={n}, late_rate={rate*100:.2f}%, edge={edge:+.2f}pp")
    print()

    # === FEATURE 5: margin_75 ===
    print("=== F5: margin_75 (home - away) ===")
    for m_lo, m_hi in [(-3, -1), (-1, 0), (0, 1), (1, 2), (2, 4)]:
        sub = [d for d in data if m_lo <= d["margin_75"] < m_hi]
        if len(sub) < 30: continue
        n = len(sub)
        rate = sum(d["late"] for d in sub) / n
        edge = (rate - base_rate) * 100
        print(f"  margin [{m_lo:>2}, {m_hi:>2}): n={n}, late_rate={rate*100:.2f}%, edge={edge:+.2f}pp")
    print()

    # === Combined: total_75 × ts_goal ===
    print("=== COMBO: total_75 × time_since_last_goal ===")
    print(f"{'total':>6} {'ts_range':<14} {'n':>5} {'late_rate':>10} {'edge':>8}")
    for tg in [0, 1, 2, 3]:
        for lo, hi in [(0, 10), (10, 20), (20, 40), (40, 75)]:
            sub = [d for d in data if d["total_75"] == tg and lo <= d["ts_goal"] < hi]
            if len(sub) < 30: continue
            n = len(sub)
            rate = sum(d["late"] for d in sub) / n
            edge = (rate - base_rate) * 100
            sig = " ***" if abs(edge) > 5 else ""
            print(f"  {tg:>4} [{lo:>3},{hi:>3}) {n:>5} {rate*100:>9.2f}% {edge:>+6.2f}pp{sig}")

    with open("/tmp/late_goal_features.json", "w") as f:
        json.dump({"n": n_total, "base_rate": base_rate, "data": data[:1000]}, f, indent=2, default=str)
    print(f"\nSaved sample to /tmp/late_goal_features.json")


main()
