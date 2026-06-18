"""Verify 2 + 3: Vig adjustment + Out-of-sample test.

Verify 2 (Vig): For each cell, compute overround from home+away odds at 75'.
  overround = 1/h_fav + 1/h_dog - 1  (decimal odds basis)
  edge_adj = edge_raw - overround  (only keep if positive after vig)

Verify 3 (OOS): Train on 2025, test on 2026. Compare edge per cell.

Output: Tables side-by-side for 2025 vs 2026.
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


def compute_cell(cur, year_filter: str):
    """year_filter is SQL fragment like 'EXTRACT(YEAR FROM start_time_utc::timestamptz) = 2025'"""
    sql = f"""
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
            AND {year_filter}
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
            CASE WHEN open_hc_f < 0 THEN hh_75 ELSE ah_75 END AS fav_malay_75,
            CASE WHEN open_hc_f < 0 THEN ah_75 ELSE hh_75 END AS dog_malay_75
          FROM joined
        )
        SELECT
          hc_bucket, margin_bucket,
          COUNT(*) AS n,
          SUM(fav_covered) AS cov,
          ROUND(AVG(fav_malay_75)::numeric, 4) AS avg_fav_malay,
          ROUND(AVG(dog_malay_75)::numeric, 4) AS avg_dog_malay
        FROM classified
        GROUP BY hc_bucket, margin_bucket
        HAVING COUNT(*) >= 20
        ORDER BY hc_bucket, margin_bucket
    """
    cur.execute(sql)
    return cur.fetchall()


def analyze(rows, label):
    print(f"\n{'='*100}")
    print(f"YEAR = {label}")
    print('='*100)
    print(f"{'hc':<12} {'margin':<10} {'n':>5} {'cov%':>7} {'avg_fav_m':>10} "
          f"{'imp%':>7} {'edge':>7} {'vig%':>6} {'edge_adj':>8} {'EV_adj':>7} {'sig':>4}")
    print("-" * 100)
    cells = []
    for hc, mb, n, cov, fav_m, dog_m in rows:
        cov_pct = (cov / n) * 100
        dec_fav = malay_to_decimal(float(fav_m))
        dec_dog = malay_to_decimal(float(dog_m))
        impl_fav = malay_to_implied(float(fav_m)) * 100
        impl_dog = malay_to_implied(float(dog_m)) * 100
        # Vig = overround
        overround = (impl_fav + impl_dog) / 100.0 - 1.0
        vig_pct = overround * 100
        edge = cov_pct - impl_fav
        edge_adj = edge - vig_pct  # conservative: subtract full vig
        ev_adj = (cov_pct / 100.0) * dec_fav - 1.0  # EV doesn't subtract vig directly
        sig = "***" if edge_adj > 5 else ("**" if edge_adj > 2 else ("*" if edge_adj > 0 else ""))
        print(f"{hc:<12} {mb:<10} {n:>5} {cov_pct:>6.2f}% {float(fav_m):>10.3f} "
              f"{impl_fav:>6.2f}% {edge:>+6.2f}% {vig_pct:>5.2f}% {edge_adj:>+7.2f}% "
              f"{ev_adj:>+6.3f} {sig:>4}")
        cells.append({
            "hc_bucket": hc, "margin_bucket": mb, "n": n, "cov_count": cov,
            "cov_pct": cov_pct, "avg_malay_fav": float(fav_m), "avg_malay_dog": float(dog_m),
            "implied_fav_pct": impl_fav, "vig_pct": vig_pct,
            "edge_raw_pp": edge, "edge_adj_pp": edge_adj,
            "ev_per_unit": ev_adj,
        })

    # Profitable after vig
    profitable = [c for c in cells if c["edge_adj_pp"] > 0 and c["n"] >= 50]
    print(f"\n  Profitable AFTER vig (edge_adj > 0, n>=50): {len(profitable)}")
    for c in profitable:
        print(f"    {c['hc_bucket']:>10} × {c['margin_bucket']:<10} n={c['n']:>4}  "
              f"edge_raw={c['edge_raw_pp']:+.2f}  vig={c['vig_pct']:.2f}  edge_adj={c['edge_adj_pp']:+.2f}  "
              f"EV={c['ev_per_unit']:+.3f}")
    return cells


def main():
    with _connect() as conn:
        cur = conn.cursor()
        rows_2025 = compute_cell(cur, "EXTRACT(YEAR FROM start_time_utc::timestamptz) = 2025")
        rows_2026 = compute_cell(cur, "EXTRACT(YEAR FROM start_time_utc::timestamptz) = 2026")

    cells_2025 = analyze(rows_2025, "2025 (train)")
    cells_2026 = analyze(rows_2026, "2026 (test)")

    # Compare overlapping cells
    print("\n" + "=" * 100)
    print("OUT-OF-SAMPLE COMPARISON: 2025 → 2026 (cells with n>=50 in BOTH)")
    print("=" * 100)
    d_25 = {(c["hc_bucket"], c["margin_bucket"]): c for c in cells_2025}
    d_26 = {(c["hc_bucket"], c["margin_bucket"]): c for c in cells_2026}
    common = set(d_25) & set(d_26)
    common = [k for k in common if d_25[k]["n"] >= 50 and d_26[k]["n"] >= 30]
    print(f"\n{'hc':<12} {'margin':<10} {'n25':>5} {'n26':>5} "
          f"{'edge_25':>8} {'edge_26':>8} {'Δedge':>7} {'vig_25':>6} {'vig_26':>6} {'stab':>5}")
    print("-" * 95)
    stable = 0
    both_profitable = 0
    for key in sorted(common):
        a = d_25[key]
        b = d_26[key]
        diff = b["edge_adj_pp"] - a["edge_adj_pp"]
        stable_mark = "✓" if (a["edge_adj_pp"] > 0) == (b["edge_adj_pp"] > 0) else "✗"
        if (a["edge_adj_pp"] > 0) == (b["edge_adj_pp"] > 0):
            stable += 1
        if a["edge_adj_pp"] > 0 and b["edge_adj_pp"] > 0:
            both_profitable += 1
        print(f"{key[0]:<12} {key[1]:<10} {a['n']:>5} {b['n']:>5} "
              f"{a['edge_adj_pp']:>+7.2f}% {b['edge_adj_pp']:>+7.2f}% {diff:>+6.2f}% "
              f"{a['vig_pct']:>5.2f}% {b['vig_pct']:>5.2f}% {stable_mark:>5}")

    print(f"\nStable cells (same sign in both years): {stable}/{len(common)}")
    print(f"Profitable in BOTH years: {both_profitable}/{len(common)}")

    with open("/tmp/oos_results.json", "w") as f:
        json.dump({"cells_2025": cells_2025, "cells_2026": cells_2026,
                   "stable_count": stable, "total_common": len(common),
                   "both_profitable": both_profitable}, f, indent=2)
    print(f"\nSaved /tmp/oos_results.json")


main()
