"""Sharp vs recreational split — no LLM, no leakage.

Hypothesis:
  - Sharp money: line moves early (opening to mid-pre-match), then STABILIZES
    at kickoff. Public can't move it further.
  - Public/rec money: late steam chasing the public-fav side, line overshoots,
    then reverses or stays unstable at kickoff.

Stratification:
  - For each FT match with opening + closing snapshot:
    * line_move = closing_odds - opening_odds (for home side)
    * stability = |last_3_snapshots_odds - closing_odds|  (do last 3 differ much?)
  - Stratify by:
    (1) line_move direction (toward_fav, toward_dog, stable)
    (2) snap_hc sign (who is fav)
  - Compare coverage per cell

Pre-flight: same data as CLV (8,347 matches with pre-kickoff snapshots).

OOS: 2025 vs 2026.

Run inside container:
  docker exec -w /app football_dashboard python3 scripts/sharp_rec_split.py
"""
from __future__ import annotations
import sys
sys.path.insert(0, "/app")

from app.database import _connect


def malay_to_decimal(o):
    if o is None or o == 0:
        return 1.0
    return 1.0 + o if o > 0 else 1.0 + 1.0 / abs(o)


def main():
    with _connect() as conn:
        cur = conn.cursor()

        # 1. Pull opening, mid (median pre-kickoff), closing snapshots
        #    + last 3 snapshots for stability check
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
              WHERE m.status = 'FT'
                AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
                AND m.home_handicap IS NOT NULL AND m.home_handicap <> ''
                AND h.captured_at < m.start_time_utc::timestamptz - INTERVAL '1 minute'
                AND h.home_handicap_odds IS NOT NULL
                AND h.away_handicap_odds IS NOT NULL
            )
            SELECT
              o.id, EXTRACT(YEAR FROM o.ko)::int AS yr,
              o.open_hc_line, o.snap_hc,
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
        print(f"[SHARP-REC] loaded {len(rows)} matches\n")

        # 2. Pull last-3 snapshots for stability check
        cur.execute("""
            WITH b AS (
              SELECT m.id, h.captured_at, h.home_handicap_odds,
                     ROW_NUMBER() OVER (PARTITION BY m.id ORDER BY h.captured_at DESC) AS rn
              FROM match_odds_history h
              JOIN matches m ON m.id = h.match_id
              WHERE m.status = 'FT'
                AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
                AND m.home_handicap IS NOT NULL AND m.home_handicap <> ''
                AND h.captured_at < m.start_time_utc::timestamptz - INTERVAL '1 minute'
                AND h.home_handicap_odds IS NOT NULL
                AND h.away_handicap_odds IS NOT NULL
            )
            SELECT id,
              MAX(CASE WHEN rn=1 THEN home_handicap_odds END) AS last1,
              MAX(CASE WHEN rn=2 THEN home_handicap_odds END) AS last2,
              MAX(CASE WHEN rn=3 THEN home_handicap_odds END) AS last3
            FROM b
            WHERE rn <= 3
            GROUP BY id
        """)
        last3_map = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall() if r[1] is not None}

        # 3. Classify each match
        per_year_cells = {}  # yr -> {cell -> [covers]}
        for (mid, yr, open_hc_line, snap_hc, open_h, close_h, open_a, close_a, fh, fa) in rows:
            try:
                oh, ch = float(open_h), float(close_h)
                oa, ca = float(open_a), float(close_a)
                hc_line = float(open_hc_line)
            except (TypeError, ValueError):
                continue

            line_move = ch - oh  # positive = odds rose (line moved away from home fav)
            last3 = last3_map.get(mid)
            if not last3 or last3[0] is None or last3[1] is None or last3[2] is None:
                stability = 999
            else:
                try:
                    l1, l2, l3 = float(last3[0]), float(last3[1]), float(last3[2])
                    stability = max(abs(l1 - ch), abs(l2 - ch), abs(l3 - ch))
                except (TypeError, ValueError):
                    stability = 999

            # Direction relative to fav
            if hc_line < 0:  # home fav
                if line_move < -0.02:
                    direction = "toward_fav"  # home more fav
                elif line_move > 0.02:
                    direction = "toward_dog"
                else:
                    direction = "stable"
            elif hc_line > 0:  # away fav
                if line_move > 0.02:
                    direction = "toward_fav"  # home more dog = away more fav
                elif line_move < -0.02:
                    direction = "toward_dog"
                else:
                    direction = "stable"
            else:
                continue

            # Stability
            if stability < 0.02:
                stab_label = "stable_close"
            else:
                stab_label = "unstable_close"

            cell = f"{direction}_{stab_label}"

            # Coverage
            try:
                hc = float(snap_hc) if snap_hc is not None else hc_line
            except (TypeError, ValueError):
                hc = hc_line
            covers = (fh + hc) > fa

            per_year_cells.setdefault(yr, {}).setdefault(cell, []).append(1 if covers else 0)

        # 4. Print
        print("=" * 80)
        print("SHARP vs REC SPLIT: coverage by (line_direction × closing_stability)")
        print("-" * 80)
        all_cells = set()
        for yr_data in per_year_cells.values():
            all_cells.update(yr_data.keys())
        all_cells = sorted(all_cells)

        header = f"{'cell':<28} | " + " | ".join(f"{yr:>10}" for yr in sorted(per_year_cells))
        print(header)
        print("-" * len(header))
        for cell in all_cells:
            row = f"{cell:<28} | "
            for yr in sorted(per_year_cells):
                data = per_year_cells[yr].get(cell, [])
                if data:
                    cov = sum(data) / len(data) * 100
                    row += f"{cov:>6.1f}% (n={len(data):<4}) | "
                else:
                    row += f"{'    --     ':>15} | "
            print(row)
        print("-" * len(header))

        # Per-year n
        print("\n=== SAMPLE SIZE PER CELL ===")
        for yr in sorted(per_year_cells):
            total = sum(len(v) for v in per_year_cells[yr].values())
            print(f"{yr}: total n = {total}")
            for cell in sorted(per_year_cells[yr]):
                print(f"  {cell}: {len(per_year_cells[yr][cell])}")
        print("=" * 80)


if __name__ == "__main__":
    main()
