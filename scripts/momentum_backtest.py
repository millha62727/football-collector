"""Backtest margin signal — corrected for MALAY odds.

Malay odds convention (sb21 API):
  positive (0 < x < 1): decimal = 1 + x,       implied = 1 / (1 + x)
  negative (-1 < x < 0): decimal = 1 + 1/|x|,   implied = |x| / (|x| + 1)

EV per 1 unit stake on FAV:
  EV = (actual_cover_rate * (decimal - 1)) - ((1 - actual_cover_rate) * 1)
     = actual_cover_rate * decimal - 1
"""
from __future__ import annotations
import sys, json
sys.path.insert(0, "/app")

from app.database import _connect


def malay_to_decimal(o: float) -> float:
    if o is None or o == 0:
        return 1.0
    if o > 0:
        return 1.0 + o
    else:  # o < 0
        return 1.0 + 1.0 / abs(o)


def malay_to_implied(o: float) -> float:
    """Implied probability (no vig)."""
    if o is None or o == 0:
        return 1.0
    if o > 0:
        return 1.0 / (1.0 + o)
    else:
        return abs(o) / (abs(o) + 1.0)


def main():
    with _connect() as conn:
        cur = conn.cursor()

        cur.execute("""
            WITH matches_with_data AS (
              SELECT m.id, m.home_score AS ft_h, m.away_score AS ft_a
              FROM matches m
              WHERE m.status='FT' AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
                AND EXISTS (
                    SELECT 1 FROM match_odds_history o
                    WHERE o.match_id = m.id AND o.minute <= 75
                      AND o.home_handicap IS NOT NULL AND o.home_score IS NOT NULL
                )
            ),
            opening AS (
              SELECT DISTINCT ON (match_id) match_id, home_handicap AS open_hc
              FROM match_odds_history
              WHERE home_handicap IS NOT NULL
              ORDER BY match_id, minute ASC, captured_at ASC
            ),
            at_75 AS (
              SELECT DISTINCT ON (match_id)
                match_id, home_score AS h_75, away_score AS a_75,
                home_handicap_odds AS hh_75, away_handicap_odds AS ah_75
              FROM match_odds_history
              WHERE minute <= 75 AND home_score IS NOT NULL
                AND home_handicap_odds IS NOT NULL AND away_handicap_odds IS NOT NULL
                AND home_handicap_odds <> 0 AND away_handicap_odds <> 0
              ORDER BY match_id, minute DESC, captured_at DESC
            ),
            joined AS (
              SELECT
                o.open_hc::float AS open_hc_f,
                m.ft_h, m.ft_a,
                a.h_75 - a.a_75 AS margin_75,
                a.hh_75, a.ah_75
              FROM matches_with_data m
              JOIN opening o ON o.match_id = m.id
              JOIN at_75 a ON a.match_id = m.id
              WHERE o.open_hc IS NOT NULL AND o.open_hc <> '0'
                AND (m.ft_h + m.ft_a) >= 1
            ),
            classified AS (
              SELECT
                *,
                CASE
                  WHEN open_hc_f IN (-0.25, -0.5) THEN 'small_neg'
                  WHEN open_hc_f IN (-0.75, -1.0) THEN 'med_neg'
                  WHEN open_hc_f <= -1.25 THEN 'large_neg'
                  WHEN open_hc_f IN (0.25, 0.5) THEN 'small_pos'
                  WHEN open_hc_f IN (0.75, 1.0) THEN 'med_pos'
                  WHEN open_hc_f >= 1.25 THEN 'large_pos'
                END AS hc_bucket,
                CASE
                  WHEN margin_75 >= 3 THEN 'lead_+3'
                  WHEN margin_75 = 2  THEN 'lead_+2'
                  WHEN margin_75 = 1  THEN 'lead_+1'
                  WHEN margin_75 = 0  THEN 'level'
                  WHEN margin_75 = -1 THEN 'lead_-1'
                  WHEN margin_75 = -2 THEN 'lead_-2'
                  ELSE 'lead_-3+'
                END AS margin_bucket,
                CASE
                  WHEN open_hc_f < 0 AND ft_h - ft_a > ABS(open_hc_f) THEN 1
                  WHEN open_hc_f > 0 AND ft_a - ft_h > open_hc_f THEN 1
                  ELSE 0
                END AS fav_covered,
                CASE WHEN open_hc_f < 0 THEN hh_75 ELSE ah_75 END AS fav_malay_75
              FROM joined
            )
            SELECT
              hc_bucket, margin_bucket,
              COUNT(*) AS n,
              SUM(fav_covered) AS cov,
              ROUND(AVG(fav_malay_75)::numeric, 4) AS avg_fav_malay
            FROM classified
            GROUP BY hc_bucket, margin_bucket
            HAVING COUNT(*) >= 30
            ORDER BY hc_bucket, margin_bucket
        """)
        rows = cur.fetchall()

    print(f"{'hc_bucket':<12} {'margin':<10} {'n':>5} {'cov%':>7} "
          f"{'avg_malay':>10} {'decimal':>8} {'implied%':>9} {'edge':>7} {'EV':>7} {'sig':>4}")
    print("=" * 95)

    cells = []
    for hc, mb, n, cov, malay in rows:
        cov_pct = (cov / n) * 100
        dec = malay_to_decimal(float(malay))
        impl_pct = malay_to_implied(float(malay)) * 100
        edge = cov_pct - impl_pct
        ev = (cov_pct / 100.0) * dec - 1.0
        sig = "***" if edge > 5 else ("**" if edge > 2 else ("*" if edge > 0 else ""))
        print(f"{hc:<12} {mb:<10} {n:>5} {cov_pct:>6.2f}% "
              f"{float(malay):>10.3f} {dec:>7.3f} {impl_pct:>8.2f}% {edge:>+6.2f}% {ev:>+6.3f} {sig:>4}")
        cells.append({
            "hc_bucket": hc, "margin_bucket": mb, "n": n, "cov_count": cov,
            "avg_malay_odds": float(malay),
            "avg_decimal_odds": dec,
            "cov_pct": cov_pct,
            "implied_pct": impl_pct,
            "edge_pp": edge,
            "ev_per_unit_stake": ev,
        })

    print()
    print("Notes:")
    print(" - 'edge' = actual_cov% - market implied% (from avg odds at 75')")
    print(" - 'EV' = stake-weighted expected value (1 unit on fav each match)")
    print(" - '***' >5pp edge, '**' >2pp, '*' >0")

    # Aggregate stats
    profitable = [c for c in cells if c["edge_pp"] > 0 and c["n"] >= 100]
    print(f"\nProfitable cells (edge > 0, n>=100): {len(profitable)}")
    for c in profitable:
        print(f"  {c['hc_bucket']:>10} × {c['margin_bucket']:<10} n={c['n']:>4}  edge={c['edge_pp']:+.2f}pp  EV={c['ev_per_unit_stake']:+.3f}")

    with open("/tmp/backtest_results.json", "w") as f:
        json.dump(cells, f, indent=2)
    print(f"\nSaved /tmp/backtest_results.json ({len(cells)} cells)")


main()
