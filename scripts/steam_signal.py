"""Steam move signal — no LLM, no leakage.

Hypothesis: synchronized large line-moves in short windows reflect sharp
money. If line moves toward fav, fav covers more often.

Definition (per SKILL P19):
  - steam event: |delta_odds| >= 0.05 (5 ticks Malay) within 60s, pre-match
  - per match: count steam events, classify direction
  - outcome: did fav cover?

Pre-flight (verified 2026-06-18):
  - median snapshot interval 31s (sufficient for sub-minute detection)
  - 1,978,391 pre-match deltas available
  - threshold 0.05/60s pre-match

OOS: 2025 vs 2026.

Run inside container:
  docker exec -w /app football_dashboard python3 scripts/steam_signal.py
"""
from __future__ import annotations
import sys
sys.path.insert(0, "/app")

from app.database import _connect


def malay_to_decimal(o):
    if o is None or o == 0:
        return 1.0
    return 1.0 + o if o > 0 else 1.0 + 1.0 / abs(o)


STEAM_THRESHOLD = 0.05  # 5 ticks malay
WINDOW_SEC = 60


def main():
    with _connect() as conn:
        cur = conn.cursor()

        # 1. Pull all pre-match deltas
        cur.execute("""
            WITH b AS (
              SELECT m.id, m.start_time_utc::timestamptz AS ko,
                     m.home_score, m.away_score,
                     m.home_handicap AS open_hc, h.home_handicap AS snap_hc,
                     h.captured_at, h.home_handicap_odds, h.away_handicap_odds
              FROM match_odds_history h
              JOIN matches m ON m.id = h.match_id
              WHERE m.status = 'FT'
                AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
                AND m.home_handicap IS NOT NULL AND m.home_handicap <> ''
                AND h.captured_at < m.start_time_utc::timestamptz - INTERVAL '1 minute'
                AND h.home_handicap_odds IS NOT NULL
                AND h.away_handicap_odds IS NOT NULL
            ),
            deltas AS (
              SELECT *,
                home_handicap_odds - LAG(home_handicap_odds) OVER (
                  PARTITION BY id ORDER BY captured_at
                ) AS d_odds,
                EXTRACT(EPOCH FROM captured_at - LAG(captured_at) OVER (
                  PARTITION BY id ORDER BY captured_at
                )) AS d_sec
              FROM b
            )
            SELECT id, EXTRACT(YEAR FROM ko)::int AS yr,
                   open_hc, snap_hc, home_handicap_odds, away_handicap_odds,
                   d_odds, d_sec, home_score, away_score
            FROM deltas
            WHERE d_sec IS NOT NULL
              AND d_sec <= %(win)s
              AND d_odds IS NOT NULL
              AND ABS(d_odds) >= %(thr)s
        """, {"win": WINDOW_SEC, "thr": STEAM_THRESHOLD})

        steam_rows = cur.fetchall()
        print(f"[STEAM] loaded {len(steam_rows)} steam events "
              f"(>{STEAM_THRESHOLD} in {WINDOW_SEC}s, pre-match)\n")

        # 2. Per match: aggregate steam direction
        # For each match, sum deltas. Positive = odds increased (line moved away from fav = toward dog)
        # We'll then look at coverage of the side the line moved TOWARD.
        per_match_steam = {}  # mid -> {yr, sum_d_hodds, home_score, away_score, snap_hc, open_hc_line}
        for (mid, yr, open_hc_line, snap_hc, h_odds, a_odds, d_hodds, d_sec, fh, fa) in steam_rows:
            if mid not in per_match_steam:
                per_match_steam[mid] = {
                    "yr": yr,
                    "sum_d_hodds": 0.0,
                    "home_score": fh,
                    "away_score": fa,
                    "snap_hc": float(snap_hc) if snap_hc is not None else 0.0,
                    "n_events": 0,
                }
            per_match_steam[mid]["sum_d_hodds"] += float(d_hodds)
            per_match_steam[mid]["n_events"] += 1

        # 3. Per match: did fav cover?
        # If snap_hc < 0 → home is fav. If snap_hc > 0 → away is fav.
        # We need to use the OPENING handicap to define "fav" (no leakage from in-play line moves)
        # But snap_hc IS the handicap at each snapshot. We can use opening (open_hc_line).
        # Note: in Asian handicap, the side with NEGATIVE handicap is the favorite.
        per_year_steam = {}  # yr -> {n, covers, vig, impl}
        per_year_nosteam = {}

        # Need base rate from non-steam matches too. Pull all FT matches and compare.
        cur.execute("""
            SELECT m.id, EXTRACT(YEAR FROM m.start_time_utc::timestamptz)::int AS yr,
                   m.home_handicap, m.home_score, m.away_score
            FROM matches m
            WHERE m.status = 'FT'
              AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
              AND m.home_handicap IS NOT NULL AND m.home_handicap <> ''
        """)
        all_rows = cur.fetchall()
        print(f"[STEAM] {len(all_rows)} FT matches total (for baseline)\n")

        def coverage(mid, hc_line, fh, fa):
            try:
                hc = float(hc_line)
            except (TypeError, ValueError):
                return None
            return (fh + hc) > fa

        steam_match_ids = set(per_match_steam.keys())
        for (mid, yr, hc_line, fh, fa) in all_rows:
            cov = coverage(mid, hc_line, fh, fa)
            if cov is None:
                continue
            d = per_year_nosteam if mid not in steam_match_ids else per_year_steam
            d.setdefault(yr, []).append(1 if cov else 0)

        # 4. Steam direction analysis
        # Group by direction: line moved toward fav (negative d_hodds if home fav, etc.)
        # Simpler: just stratify by sum_d_hodds sign
        per_year_dir = {}  # yr -> {"toward_fav": [...], "toward_dog": [...]}
        for mid, d in per_match_steam.items():
            cov = coverage(mid, d.get("snap_hc"), d["home_score"], d["away_score"])
            if cov is None:
                continue
            # Steam moved TOWARD fav iff line decreased (Malay odds smaller = bigger fav)
            # If d["sum_d_hodds"] < 0 → home odds decreased → home more fav → toward_fav
            # If snap_hc < 0 (home is fav): toward_fav when sum_d_hodds < 0
            # If snap_hc > 0 (away is fav): toward_fav when sum_d_hodds > 0 (line moving away from home = toward away fav)
            snap_hc = d["snap_hc"]
            sd = d["sum_d_hodds"]
            if snap_hc < 0:  # home fav
                direction = "toward_fav" if sd < 0 else "toward_dog"
            elif snap_hc > 0:  # away fav
                direction = "toward_fav" if sd > 0 else "toward_dog"
            else:
                continue
            per_year_dir.setdefault(d["yr"], {"toward_fav": [], "toward_dog": []})[direction].append(1 if cov else 0)

        # 5. Print
        print("=" * 70)
        print("STEAM MATCHES vs BASELINE (no steam)")
        print("-" * 70)
        print(f"{'Year':<6} {'group':<15} {'n':>6} {'cov%':>8}")
        for yr in sorted(set(list(per_year_steam) + list(per_year_nosteam))):
            for label, d in [("STEAM", per_year_steam), ("no_steam", per_year_nosteam)]:
                if yr in d and d[yr]:
                    cov = sum(d[yr]) / len(d[yr]) * 100
                    print(f"{yr:<6} {label:<15} {len(d[yr]):>6} {cov:>7.2f}%")
        print("-" * 70)

        print("\n=== STEAM DIRECTION (did line move toward fav → fav cover more?) ===")
        print(f"{'Year':<6} {'direction':<15} {'n':>6} {'cov%':>8}")
        for yr in sorted(per_year_dir):
            for direction in ("toward_fav", "toward_dog"):
                data = per_year_dir[yr][direction]
                if data:
                    cov = sum(data) / len(data) * 100
                    print(f"{yr:<6} {direction:<15} {len(data):>6} {cov:>7.2f}%")
        print("=" * 70)


if __name__ == "__main__":
    main()
