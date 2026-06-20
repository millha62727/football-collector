"""Verify late_goal signal with OOS + vig.

Hypothesis: time_since_last_goal predicts late goal (any goal after 75').
Edge from feature alone.

Steps:
  1. Aggregate by ts_goal bucket per year
  2. Compute late_rate per bucket, vs base rate
  3. Compare 2025 vs 2026 (OOS check)
  4. For profitable buckets: compute implied prob from OVER odds at 75'
  5. Subtract vig, report edge_adj
"""
from __future__ import annotations
import sys, json
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


def query_year(cur, year_filter: str):
    sql = f"""
        WITH matches_with_data AS (
          SELECT m.id, m.home_score AS ft_h, m.away_score AS ft_a, m.start_time_utc
          FROM matches m
          WHERE m.status='FT' AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
            AND EXISTS (SELECT 1 FROM match_odds_history o WHERE o.match_id = m.id AND o.minute <= 75 AND o.home_handicap IS NOT NULL AND o.home_score IS NOT NULL)
            AND {year_filter}
        ),
        opening AS (
          SELECT DISTINCT ON (match_id) match_id, ou_line, over_odds AS open_over, under_odds AS open_under
          FROM match_odds_history WHERE ou_line IS NOT NULL AND over_odds IS NOT NULL
          ORDER BY match_id, minute ASC, captured_at ASC
        ),
        at_75 AS (
          SELECT DISTINCT ON (match_id) match_id, home_score AS h_75, away_score AS a_75,
            over_odds AS over_75, under_odds AS under_75
          FROM match_odds_history WHERE minute <= 75 AND home_score IS NOT NULL
            AND over_odds IS NOT NULL AND under_odds IS NOT NULL
          ORDER BY match_id, minute DESC, captured_at DESC
        ),
        lg AS (
          SELECT match_id, MAX(EXTRACT(EPOCH FROM (g.occurred_at - m.start_time_utc::timestamptz)) / 60.0) AS lgm
          FROM match_goals g JOIN matches m ON m.id = g.match_id
          WHERE m.start_time_utc IS NOT NULL AND g.occurred_at IS NOT NULL
            AND EXTRACT(EPOCH FROM (g.occurred_at - m.start_time_utc::timestamptz)) / 60.0 BETWEEN 0 AND 75
          GROUP BY match_id
        ),
        joined AS (
          SELECT
            m.id, m.ft_h, m.ft_a,
            a.h_75, a.a_75,
            a.over_75 AS over_at_75, a.under_75 AS under_at_75,
            o.open_over, o.open_under,
            COALESCE(75 - lg.lgm, 75) AS ts_goal
          FROM matches_with_data m
          JOIN opening o ON o.match_id = m.id
          JOIN at_75 a ON a.match_id = m.id
          LEFT JOIN lg ON lg.match_id = m.id
        )
        SELECT
          CASE
            WHEN ts_goal < 5  THEN 'ts_0_5'
            WHEN ts_goal < 10 THEN 'ts_5_10'
            WHEN ts_goal < 20 THEN 'ts_10_20'
            WHEN ts_goal < 40 THEN 'ts_20_40'
            ELSE 'ts_40_75'
          END AS bucket,
          COUNT(*) AS n,
          SUM(CASE WHEN ft_h + ft_a > h_75 + a_75 THEN 1 ELSE 0 END) AS late_n,
          ROUND(AVG(over_at_75)::numeric, 4) AS avg_over_75,
          ROUND(AVG(open_over)::numeric, 4) AS avg_over_open
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
          f"{'over_odds':>10} {'imp%':>7} {'vig%':>6} {'edge_adj':>9}")
    print("-" * 90)
    cells = []
    base = None
    for bucket, n, late_n, ov_75, ov_open in rows:
        late_pct = late_n / n * 100
        cells.append({"bucket": bucket, "n": n, "late_pct": late_pct,
                      "over_75": float(ov_75) if ov_75 else None,
                      "over_open": float(ov_open) if ov_open else None})
    # Base = total late / total n
    total_n = sum(c["n"] for c in cells)
    total_late = sum(c["late_pct"]/100*c["n"] for c in cells)
    base_pct = total_late / total_n * 100

    for c in cells:
        edge_raw = c["late_pct"] - base_pct
        if c["over_75"] is None or c["over_open"] is None:
            print(f"{c['bucket']:<10} {c['n']:>5} {c['late_pct']:>6.2f}% {base_pct:>6.2f}% {edge_raw:>+7.2f}%")
            continue
        dec_75 = malay_to_decimal(c["over_75"])
        impl_75 = malay_to_implied(c["over_75"]) * 100
        # vig from opening vs 75: opening has vig, 75 might also
        # Approx: vig = impl_open + (1 - impl_75) - 1 if over is moving
        impl_open = malay_to_implied(c["over_open"]) * 100
        vig_approx = max(0, (impl_open + (100 - impl_75) - 100))  # rough
        edge_adj = edge_raw - vig_approx
        print(f"{c['bucket']:<10} {c['n']:>5} {c['late_pct']:>6.2f}% {base_pct:>6.2f}% {edge_raw:>+7.2f}% "
              f"{c['over_75']:>10.3f} {impl_75:>6.2f}% {vig_approx:>5.2f}% {edge_adj:>+7.2f}%")
        c["edge_raw"] = edge_raw
        c["impl_75"] = impl_75
        c["vig_approx"] = vig_approx
        c["edge_adj"] = edge_adj
    return cells, base_pct


def main():
    with _connect() as conn:
        cur = conn.cursor()
        rows_25 = query_year(cur, "EXTRACT(YEAR FROM start_time_utc::timestamptz) = 2025")
        rows_26 = query_year(cur, "EXTRACT(YEAR FROM start_time_utc::timestamptz) = 2026")

    cells_25, base_25 = analyze(rows_25, "2025 (train)")
    cells_26, base_26 = analyze(rows_26, "2026 (test)")

    print("\n" + "="*90)
    print("OOS COMPARISON")
    print("="*90)
    print(f"{'bucket':<10} {'n_25':>5} {'n_26':>5} {'late_25':>8} {'late_26':>8} {'edge_raw_25':>11} {'edge_raw_26':>11} {'edge_adj_25':>11} {'edge_adj_26':>11}")
    print("-" * 95)
    d25 = {c["bucket"]: c for c in cells_25}
    d26 = {c["bucket"]: c for c in cells_26}
    for bucket in sorted(set(d25) & set(d26)):
        a, b = d25[bucket], d26[bucket]
        ea_raw = a.get("edge_raw", 0)
        eb_raw = b.get("edge_raw", 0)
        ea_adj = a.get("edge_adj", 0)
        eb_adj = b.get("edge_adj", 0)
        print(f"{bucket:<10} {a['n']:>5} {b['n']:>5} {a['late_pct']:>7.2f}% {b['late_pct']:>7.2f}% "
              f"{ea_raw:>+10.2f}% {eb_raw:>+10.2f}% {ea_adj:>+10.2f}% {eb_adj:>+10.2f}%")


main()
