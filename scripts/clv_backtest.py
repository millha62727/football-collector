"""CLV (Closing Line Value) backtest — no LLM, no leakage.

Hypothesis: betting at the OPENING line and "holding" until kickoff gives
a positive expected return when the line moved TOWARDS the bet side.

Signal:
  - For each FT match with valid opening + closing snapshots:
    * open_h_odds, close_h_odds (Malay)
    * open_a_odds, close_a_odds
    * clv_fav = (decimal(close) / decimal(open)) - 1   for whichever side is "favorite"
    * coverage: did the fav actually cover?
  - Aggregate:
    * mean CLV (positive = line moved toward bet side)
    * coverage_rate
    * implied_prob_at_close (1/decimal(close))
    * vig = 1/dec_fav + 1/dec_dog - 1
    * edge_adj = coverage_rate - implied_prob - vig
  - OOS: 2025 vs 2026

Pre-flight (verified 2026-06-18):
  - 8,347 FT matches have pre-kickoff snapshot
  - median 11.7h from opening to kickoff, 25min from closing to kickoff
  - 65.4% of pre-kickoff snaps have both h_odds and a_odds
  - 2025: 3,402 matches / 2026: 4,945 matches

Run inside container (DB env):
  docker exec -w /app football_dashboard python3 scripts/clv_backtest.py
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, "/app")

from app.database import _connect


def malay_to_decimal(o):
    if o is None or o == 0:
        return 1.0
    return 1.0 + o if o > 0 else 1.0 + 1.0 / abs(o)


def main():
    with _connect() as conn:
        cur = conn.cursor()

        # Pull opening + closing snapshots for all FT matches with both odds
        cur.execute("""
            WITH b AS (
              SELECT m.id, m.start_time_utc::timestamptz AS ko,
                     m.home_score, m.away_score,
                     m.home_handicap AS open_hc_line,
                     h.captured_at, h.home_handicap AS snap_hc,
                     h.home_handicap_odds, h.away_handicap_odds,
                     ROW_NUMBER() OVER (PARTITION BY m.id ORDER BY h.captured_at ASC)  AS rn_open,
                     ROW_NUMBER() OVER (PARTITION BY m.id ORDER BY h.captured_at DESC) AS rn_close
              FROM matches m
              JOIN match_odds_history h ON h.match_id = m.id
              WHERE m.status='FT'
                AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
                AND m.home_handicap IS NOT NULL AND m.home_handicap <> ''
                AND h.captured_at < m.start_time_utc::timestamptz - INTERVAL '1 minute'
                AND h.home_handicap_odds IS NOT NULL
                AND h.away_handicap_odds IS NOT NULL
            )
            SELECT
              o.id, EXTRACT(YEAR FROM o.ko)::int AS yr,
              o.snap_hc, o.open_hc_line,
              o.home_handicap_odds AS open_h_odds,
              c.home_handicap_odds AS close_h_odds,
              o.away_handicap_odds AS open_a_odds,
              c.away_handicap_odds AS close_a_odds,
              o.home_score, o.away_score
            FROM b o
            JOIN b c ON c.id = o.id
            WHERE o.rn_open = 1 AND c.rn_close = 1
        """)

        rows = cur.fetchall()
        print(f"[CLV] loaded {len(rows)} matches with open+close snapshots\n")

        # Per-row CLV
        per_year = {}  # yr -> list of dicts
        for (mid, yr, snap_hc, open_hc_line, open_h, close_h, open_a, close_a, fh, fa) in rows:
            try:
                oh, ch = float(open_h), float(close_h)
                oa, ca = float(open_a), float(close_a)
            except (TypeError, ValueError):
                continue

            dec_oh = malay_to_decimal(oh)
            dec_ch = malay_to_decimal(ch)
            dec_oa = malay_to_decimal(oa)
            dec_ca = malay_to_decimal(ca)

            # Convert handicap to fav side. Malay odds < 1.0 positive => fav.
            # We pick "side to bet" = side whose OPENING odds are smaller (fav).
            if oh <= oa:
                bet_odds_open = oh
                bet_odds_close = ch
                dec_bet_open = dec_oh
                dec_bet_close = dec_ch
                other_odds_open = oa
                other_odds_close = ca
                dec_other_open = dec_oa
                dec_other_close = dec_ca
                # Coverage: home covers if home_score + handicap > away_score
                hc = float(snap_hc) if snap_hc is not None else 0.0
                covers = (fh + hc) > fa
            else:
                # Bet away
                bet_odds_open = oa
                bet_odds_close = ca
                dec_bet_open = dec_oa
                dec_bet_close = dec_ca
                other_odds_open = oh
                other_odds_close = ch
                dec_other_open = dec_oh
                dec_other_close = dec_ch
                # Away covers if home_score + handicap <= away_score
                hc = float(snap_hc) if snap_hc is not None else 0.0
                covers = (fh + hc) <= fa

            # CLV: positive means closing price is BETTER for bettor (line moved toward our side)
            # Convention: clv = (decimal_close - decimal_open) / decimal_open
            # If close_decimal > open_decimal → odds got larger → better for bettor → positive CLV
            clv = (dec_bet_close - dec_bet_open) / dec_bet_open

            # Vig at close (sb21 typical 40-60% per cell — see SKILL P12)
            vig_close = 1.0 / dec_bet_close + 1.0 / dec_other_close - 1.0
            impl_close = 1.0 / dec_bet_close

            per_year.setdefault(yr, []).append({
                "covers": 1 if covers else 0,
                "clv": clv,
                "impl_close": impl_close,
                "vig": vig_close,
            })

        # Aggregate per year
        print("=" * 78)
        print(f"{'Year':<6} {'n':>6} {'cov%':>7} {'impl%':>7} {'vig%':>7} {'edge_adj%':>10} {'meanCLV%':>10}")
        print("-" * 78)
        grand = []
        for yr in sorted(per_year):
            data = per_year[yr]
            n = len(data)
            cov = sum(d["covers"] for d in data) / n
            impl = sum(d["impl_close"] for d in data) / n
            vig = sum(d["vig"] for d in data) / n
            edge_adj = (cov - impl - vig) * 100
            mean_clv = sum(d["clv"] for d in data) / n * 100
            print(f"{yr:<6} {n:>6} {cov*100:>6.2f}% {impl*100:>6.2f}% {vig*100:>6.2f}% {edge_adj:>+9.3f}pp {mean_clv:>+9.3f}%")
            for d in data:
                d["yr"] = yr
            grand.extend(data)

        # Aggregate all
        n = len(grand)
        cov = sum(d["covers"] for d in grand) / n
        impl = sum(d["impl_close"] for d in grand) / n
        vig = sum(d["vig"] for d in grand) / n
        edge_adj = (cov - impl - vig) * 100
        mean_clv = sum(d["clv"] for d in grand) / n * 100
        print(f"{'ALL':<6} {n:>6} {cov*100:>6.2f}% {impl*100:>6.2f}% {vig*100:>6.2f}% {edge_adj:>+9.3f}pp {mean_clv:>+9.3f}%")
        print("=" * 78)

        # CLV stratified: did positive CLV (line moved toward us) → more coverage?
        print("\n=== CLV STRATIFICATION (does positive CLV → more coverage?) ===")
        # Aggregate all years, bucket by CLV sign + magnitude
        bands = [(-999, -0.05, "CLV < -5%"), (-0.05, -0.01, "-5% to -1%"),
                 (-0.01, 0.01, "neutral"), (0.01, 0.05, "+1% to +5%"),
                 (0.05, 999, "CLV > +5%")]
        print(f"{'Band':<20} {'n':>6} {'cov%':>8} {'edge_adj%':>11}")
        print("-" * 50)
        for lo, hi, label in bands:
            sub = [d for d in grand if lo <= d["clv"] < hi]
            if not sub:
                continue
            n = len(sub)
            cov = sum(d["covers"] for d in sub) / n
            impl = sum(d["impl_close"] for d in sub) / n
            vig = sum(d["vig"] for d in sub) / n
            edge_adj = (cov - impl - vig) * 100
            print(f"{label:<20} {n:>6} {cov*100:>7.2f}% {edge_adj:>+10.3f}pp")
        print("-" * 50)


if __name__ == "__main__":
    main()
