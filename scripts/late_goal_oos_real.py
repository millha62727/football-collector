"""Late goal prediction using ONLY match_odds_history (works for 2025 too).

Strategy: derive 'last_goal_minute' from score changes in odds snapshots,
not from match_goals. This works for both 2025 and 2026 data.
"""
from __future__ import annotations
import sys
sys.path.insert(0, "/app")

from app.database import _connect


def malay_to_decimal(o):
    if o is None or o == 0:
        return 1.0
    return 1.0 + o if o > 0 else 1.0 + 1.0 / abs(o)


def malay_to_implied(o):
    if o is None or o == 0:
        return 1.0
    return 1.0 / (1.0 + o) if o > 0 else abs(o) / (abs(o) + 1.0)


def query_year(cur, year_filter):
    """For each FT match, find the minute of the last goal BEFORE 75' (from odds_history).
    Then compute late_rate by ts_goal bucket."""
    sql = f"""
        WITH matches_with_data AS (
          SELECT m.id, m.home_score AS ft_h, m.away_score AS ft_a,
                 EXTRACT(YEAR FROM m.start_time_utc::timestamptz) AS yr
          FROM matches m
          WHERE m.status='FT' AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
            AND EXISTS (SELECT 1 FROM match_odds_history o WHERE o.match_id = m.id AND o.minute <= 75 AND o.home_score IS NOT NULL)
            AND {year_filter}
        ),
        snaps AS (
          SELECT
            o.match_id,
            o.minute,
            o.home_score AS h, o.away_score AS a,
            o.over_odds, o.under_odds,
            o.captured_at,
            ROW_NUMBER() OVER (PARTITION BY o.match_id ORDER BY o.minute ASC, o.captured_at ASC) AS rn
          FROM match_odds_history o
          JOIN matches_with_data m ON m.id = o.match_id
          WHERE o.minute IS NOT NULL AND o.home_score IS NOT NULL AND o.over_odds IS NOT NULL
        ),
        with_change AS (
          SELECT
            match_id, minute, h, a, over_odds, under_odds,
            LAG(h) OVER w AS prev_h,
            LAG(a) OVER w AS prev_a
          FROM snaps
          WINDOW w AS (PARTITION BY match_id ORDER BY minute ASC, captured_at ASC)
        ),
        goal_events AS (
          -- Find snapshots where score increased (= goal happened)
          SELECT match_id, minute, h, a, over_odds, under_odds,
                 prev_h, prev_a
          FROM with_change
          WHERE prev_h IS NOT NULL AND prev_a IS NOT NULL
            AND (h > prev_h OR a > prev_a)
            AND minute <= 75
        ),
        last_goal_per_match AS (
          SELECT DISTINCT ON (match_id) match_id, minute AS last_goal_min,
            over_odds AS last_over_odds, under_odds AS last_under_odds,
            h AS h_after_last_goal, a AS a_after_last_goal
          FROM goal_events
          ORDER BY match_id, minute DESC
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
        joined AS (
          SELECT
            m.id, m.ft_h, m.ft_a,
            a.h_75, a.a_75,
            COALESCE(lg.last_goal_min, -1) AS last_goal_min,
            a.over_75, a.under_75
          FROM matches_with_data m
          JOIN at_75 a ON a.match_id = m.id
          LEFT JOIN last_goal_per_match lg ON lg.match_id = m.id
        )
        SELECT
          CASE
            WHEN last_goal_min < 0  THEN 'no_goal'
            WHEN last_goal_min >= 70 THEN 'ts_70_75'
            WHEN last_goal_min >= 60 THEN 'ts_60_70'
            WHEN last_goal_min >= 45 THEN 'ts_45_60'
            WHEN last_goal_min >= 30 THEN 'ts_30_45'
            ELSE 'ts_0_30'
          END AS bucket,
          COUNT(*) AS n,
          SUM(CASE WHEN ft_h + ft_a > h_75 + a_75 THEN 1 ELSE 0 END) AS late_n,
          ROUND(AVG(over_75)::numeric, 4) AS avg_over_75,
          ROUND(AVG(under_75)::numeric, 4) AS avg_under_75
        FROM joined
        GROUP BY bucket
        ORDER BY bucket
    """
    cur.execute(sql)
    return cur.fetchall()


def analyze(rows, label):
    print(f"\n{'='*90}")
    print(f"YEAR = {label}")
    print('='*90)
    print(f"{'bucket':<10} {'n':>5} {'late%':>7} {'base%':>7} {'edge_raw':>9} "
          f"{'over':>6} {'under':>6} {'imp_o':>7} {'vig%':>6} {'edge_adj':>9}")
    print("-" * 90)
    cells = []
    total_n = sum(r[1] for r in rows)
    total_late = sum(r[2] for r in rows)
    base_pct = total_late / total_n * 100 if total_n else 0
    for bucket, n, late_n, ov, un in rows:
        late_pct = late_n / n * 100
        ov_f = float(ov) if ov else None
        un_f = float(un) if un else None
        if ov_f is None or un_f is None:
            print(f"{bucket:<10} {n:>5} {late_pct:>6.2f}% {base_pct:>6.2f}%")
            continue
        dec_o = malay_to_decimal(ov_f)
        dec_u = malay_to_decimal(un_f)
        impl_o = malay_to_implied(ov_f) * 100
        impl_u = malay_to_implied(un_f) * 100
        vig = (impl_o + impl_u) - 100
        edge_raw = late_pct - base_pct
        edge_adj = edge_raw - max(0, vig)
        ev = (late_pct/100) * dec_o - 1
        print(f"{bucket:<10} {n:>5} {late_pct:>6.2f}% {base_pct:>6.2f}% {edge_raw:>+7.2f}% "
              f"{ov_f:>6.3f} {un_f:>6.3f} {impl_o:>6.2f}% {vig:>5.2f}% {edge_adj:>+7.2f}%  EV={ev:+.3f}")
        cells.append({"bucket": bucket, "n": n, "late_pct": late_pct,
                      "edge_raw": edge_raw, "vig": vig, "edge_adj": edge_adj, "ev": ev,
                      "impl_o": impl_o})
    return cells, base_pct


def main():
    with _connect() as conn:
        cur = conn.cursor()
        rows_25 = query_year(cur, "EXTRACT(YEAR FROM start_time_utc::timestamptz) = 2025")
        rows_26 = query_year(cur, "EXTRACT(YEAR FROM start_time_utc::timestamptz) = 2026")

    cells_25, base_25 = analyze(rows_25, "2025 (train)")
    cells_26, base_26 = analyze(rows_26, "2026 (test)")

    print("\n" + "="*90)
    print("OOS COMPARISON: 2025 → 2026")
    print("="*90)
    print(f"{'bucket':<10} {'n_25':>5} {'n_26':>5} {'late_25':>8} {'late_26':>8} "
          f"{'raw_25':>8} {'raw_26':>8} {'adj_25':>8} {'adj_26':>8} {'OOS_stable':>10}")
    print("-" * 95)
    d25 = {c["bucket"]: c for c in cells_25}
    d26 = {c["bucket"]: c for c in cells_26}
    profitable_25 = set()
    profitable_26 = set()
    for bucket in sorted(set(d25) & set(d26)):
        a, b = d25[bucket], d26[bucket]
        stable = "✓" if (a["edge_adj"] > 0) == (b["edge_adj"] > 0) else "✗"
        if a["edge_adj"] > 0 and b["edge_adj"] > 0:
            profitable_25.add(bucket)
            profitable_26.add(bucket)
        elif a["edge_adj"] > 0:
            profitable_25.add(bucket)
        elif b["edge_adj"] > 0:
            profitable_26.add(bucket)
        print(f"{bucket:<10} {a['n']:>5} {b['n']:>5} {a['late_pct']:>7.2f}% {b['late_pct']:>7.2f}% "
              f"{a['edge_raw']:>+7.2f}% {b['edge_raw']:>+7.2f}% {a['edge_adj']:>+7.2f}% {b['edge_adj']:>+7.2f}% {stable:>10}")

    print(f"\nProfitable in 2025 only: {profitable_25 - profitable_26}")
    print(f"Profitable in 2026 only: {profitable_26 - profitable_25}")
    print(f"Profitable in BOTH: {profitable_25 & profitable_26}")


main()
