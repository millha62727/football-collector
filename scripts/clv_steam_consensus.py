"""Three line-movement-based edge tests on full match_odds_history.

H1: CLV (Closing Line Value) — for every bet placed at minute m, does the
    closing handicap (last snapshot before FT) cover the bet?
H2: Steam move — within a 5-min window, did the handicap move >= 1 line
    (0.25 steps)? Compare outcome between steam and non-steam matches.
H3: Market consensus vs closing — at minute m, what is the implied fair
    line? Compare to actual closing line. Positive drift = sharp consensus
    shifted during the window.

Self-contained: reads DATABASE_URL from env, defaults to db:5432.
Run on VPS where DB lives: python3 scripts/clv_steam_consensus.py
"""
from __future__ import annotations
import os
import sys
import json
import statistics
from collections import defaultdict

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("psycopg2 not installed; run: pip install psycopg2-binary", file=sys.stderr)
    sys.exit(1)


def malay_to_decimal(o):
    if o is None or o == 0:
        return None
    return 1.0 + o if o > 0 else 1.0 + 1.0 / abs(o)


def malay_to_implied(o):
    """Implied prob (no vig removed). For vig-adjusted use 1/h_fav + 1/h_dog - 1."""
    if o is None or o == 0:
        return None
    return 1.0 / (1.0 + o) if o > 0 else abs(o) / (abs(o) + 1.0)


def vig(h_fav, h_dog):
    if h_fav is None or h_dog is None or h_fav <= 1.0 or h_dog <= 1.0:
        return None
    return 1.0 / h_fav + 1.0 / h_dog - 1.0


def parse_hc(s):
    """Handicap string like '0.5' or '-0.25' or '1/1.5' → float (lower bound)."""
    if not s:
        return None
    try:
        return float(str(s).strip())
    except ValueError:
        return None


def get_conn():
    url = os.environ.get("DATABASE_URL", "postgresql://football:***@db:5432/football")
    return psycopg2.connect(url)


# ───────────────────────────────────────────────────────────────────
# H1: CLV — "if I bet at minute m, was the line I got better than the close?"
# ───────────────────────────────────────────────────────────────────
def h1_clv(cur):
    """
    For each snapshot with valid handicap + odds + minute <= 80, compute:
      edge_vs_close = (my_decimal - closing_decimal)   (for fav side)
    Positive = I got better odds than the close = positive CLV (good).
    Then average CLV per minute-bucket, and correlation with FT outcome
    (did fav side cover?).
    """
    cur.execute("""
        WITH ranked AS (
          SELECT match_id, minute, captured_at,
                 home_handicap, home_handicap_odds, away_handicap_odds,
                 home_score, away_score,
                 ROW_NUMBER() OVER (PARTITION BY match_id ORDER BY captured_at DESC) AS rn_close,
                 ROW_NUMBER() OVER (PARTITION BY match_id ORDER BY captured_at ASC) AS rn_open,
                 MAX(captured_at) OVER (PARTITION BY match_id) AS last_cap
          FROM match_odds_history
          WHERE home_handicap IS NOT NULL
            AND home_handicap_odds IS NOT NULL AND away_handicap_odds IS NOT NULL
            AND home_handicap_odds <> 0 AND away_handicap_odds <> 0
            AND minute IS NOT NULL AND minute <= 80
            AND home_score IS NOT NULL AND away_score IS NOT NULL
        ),
        open_close AS (
          SELECT match_id,
                 MAX(CASE WHEN rn_open=1  THEN home_handicap END) AS open_hc,
                 MAX(CASE WHEN rn_open=1  THEN home_handicap_odds END) AS open_h_fav,
                 MAX(CASE WHEN rn_open=1  THEN away_handicap_odds END) AS open_h_dog,
                 MAX(CASE WHEN rn_close=1 THEN home_handicap END) AS close_hc,
                 MAX(CASE WHEN rn_close=1 THEN home_handicap_odds END) AS close_h_fav,
                 MAX(CASE WHEN rn_close=1 THEN away_handicap_odds END) AS close_h_dog
          FROM ranked GROUP BY match_id
        ),
        snapshots AS (
          SELECT r.match_id, r.minute, r.home_handicap, r.home_handicap_odds,
                 r.away_handicap_odds, oc.close_hc, oc.close_h_fav, oc.close_h_dog,
                 r.home_score, r.away_score
          FROM ranked r
          JOIN open_close oc USING (match_id)
          WHERE r.rn_open > 1 AND r.rn_close > 1  -- exclude opening & closing themselves
        )
        SELECT minute, home_handicap, home_handicap_odds, away_handicap_odds,
               close_hc, close_h_fav, close_h_dog,
               home_score, away_score
        FROM snapshots
        WHERE minute IS NOT NULL
        ORDER BY match_id, minute
    """)
    rows = cur.fetchall()
    print(f"\n=== H1: CLV — {len(rows):,} mid-game snapshots across all matches ===")

    # Bucket: did home fav cover the handicap (close_hc applied to FT score)?
    #   ft_home + close_hc > ft_away  → fav covered
    #   ft_home + close_hc < ft_away  → fav didn't cover (push = excluded)
    # Then for each minute bucket: avg(edge_vs_close) where edge_vs_close
    # is the implied edge of betting fav at that minute vs the closing line.
    by_min = defaultdict(list)
    n_covered = 0
    n_total = 0
    for (minute, hc_str, h_fav, h_dog, close_hc_str, close_h_fav, close_h_dog,
         ft_h, ft_a) in rows:
        hc = parse_hc(hc_str)
        close_hc = parse_hc(close_hc_str)
        if hc is None or close_hc is None:
            continue
        # decimal odds of fav at snapshot
        d_snap = malay_to_decimal(h_fav)
        d_close = malay_to_decimal(close_h_fav)
        if d_snap is None or d_close is None:
            continue
        # CLV for fav side: positive = I got better odds than close
        clv = (d_snap - d_close) / d_close  # fractional
        # Did fav cover close_hc on FT score?
        # home_handicap is the fav's handicap. If hc < 0, home is fav (giving).
        #   fav covers if ft_h + hc > ft_a   (or >= for some books, we use >)
        # If hc > 0, home is dog.
        covered = None
        if hc < 0:  # home fav
            if ft_h + hc > ft_a: covered = True
            elif ft_h + hc < ft_a: covered = False
        elif hc > 0:  # home dog
            if ft_h + hc < ft_a: covered = True
            elif ft_h + hc > ft_a: covered = False
        if covered is not None:
            n_total += 1
            if covered: n_covered += 1
        bucket = (minute // 5) * 5  # 5-min buckets
        by_min[bucket].append((clv, covered))

    print(f"Snapshots with valid cover data: {n_total:,}  | fav covered: {n_covered:,} ({100*n_covered/max(1,n_total):.1f}%)")
    print(f"\nCLV vs outcome (avg CLV when fav covered vs didn't):")
    print(f"{'minute':>6}  {'n':>6}  {'avg_clv_fav_covered':>20}  {'avg_clv_fav_lost':>18}  {'edge_pp':>8}")
    grand_n = 0
    grand_edge_sum = 0.0
    for bucket in sorted(by_min):
        clvs = [c for c, _ in by_min[bucket] if c is not None]
        wins = [c for c, cov in by_min[bucket] if cov is True]
        losses = [c for c, cov in by_min[bucket] if cov is False]
        if not clvs:
            continue
        n = len(clvs)
        # Avg edge in pp: if CLV positive when fav wins, that's our signal
        avg_win = statistics.mean(wins) * 100 if wins else 0
        avg_loss = statistics.mean(losses) * 100 if losses else 0
        # Combined: if you bet fav at all snapshots with avg CLV c, expected value per $1
        # EV = P(win) * (D_snap - 1) - P(loss) * 1 + (1 - P_win - P_loss) * 0
        if wins or losses:
            p_win = len(wins) / n
            p_loss = len(losses) / n
            avg_d_snap = statistics.mean([malay_to_decimal(h) for c, cov, h in
                [(c, cov, h_fav) for (c, cov), h_fav in
                 [((c, cov), h_fav) for (c, cov), h_fav in
                  zip(by_min[bucket], [])]] if h]) if False else 0
            # Skip complex EV; just show the CLV split
        print(f"{bucket:>6}  {n:>6}  {avg_win:>20.3f}  {avg_loss:>18.3f}")


# ───────────────────────────────────────────────────────────────────
# H2: Steam move — did line move >= 0.25 in a 5-min window?
# ───────────────────────────────────────────────────────────────────
def h2_steam(cur):
    """
    For each match, find consecutive snapshots within 5 min of each other
    where the home_handicap changed by |Δ| >= 0.25.
    'Steam' if at least one such move detected.
    Compare cover rate: did fav cover close_hc in steam vs non-steam matches?
    """
    cur.execute("""
        WITH ordered AS (
          SELECT match_id, captured_at, minute, home_handicap, home_handicap_odds,
                 away_handicap_odds, home_score, away_score,
                 LAG(home_handicap) OVER (PARTITION BY match_id ORDER BY captured_at) AS prev_hc,
                 LAG(captured_at) OVER (PARTITION BY match_id ORDER BY captured_at) AS prev_cap,
                 LAG(minute) OVER (PARTITION BY match_id ORDER BY captured_at) AS prev_minute,
                 FIRST_VALUE(home_handicap) OVER (PARTITION BY match_id ORDER BY captured_at DESC) AS close_hc
          FROM match_odds_history
          WHERE home_handicap IS NOT NULL
            AND home_handicap_odds IS NOT NULL AND away_handicap_odds IS NOT NULL
            AND home_handicap_odds <> 0 AND away_handicap_odds <> 0
            AND minute IS NOT NULL AND minute <= 80
            AND home_score IS NOT NULL AND away_score IS NOT NULL
        )
        SELECT match_id, minute, home_handicap, home_handicap_odds, away_handicap_odds,
               prev_hc, prev_cap, prev_minute, close_hc, home_score, away_score
        FROM ordered
        WHERE prev_hc IS NOT NULL
        ORDER BY match_id, captured_at
    """)
    rows = cur.fetchall()
    print(f"\n=== H2: Steam moves — {len(rows):,} snapshot transitions ===")

    steam_matches = set()
    non_steam_matches = set()
    all_matches = set()
    steam_by_min_bucket = defaultdict(int)  # bucket -> steam count
    for (mid, minute, hc_str, h_fav, h_dog, prev_hc_str, prev_cap, prev_min,
         close_hc_str, ft_h, ft_a) in rows:
        all_matches.add(mid)
        hc = parse_hc(hc_str)
        prev = parse_hc(prev_hc_str)
        if hc is None or prev is None:
            continue
        delta = abs(hc - prev)
        if delta >= 0.25:  # 1 tick move
            steam_matches.add(mid)
            bkt = (minute // 5) * 5
            steam_by_min_bucket[bkt] += 1
        else:
            non_steam_matches.add(mid)

    print(f"Total matches: {len(all_matches):,}")
    print(f"Steam matches (≥1 line move in 5-min window): {len(steam_matches):,}  ({100*len(steam_matches)/max(1,len(all_matches)):.1f}%)")
    print(f"\nSteam events by minute bucket (where the move happened):")
    for b in sorted(steam_by_min_bucket):
        bar = "█" * min(50, steam_by_min_bucket[b] // 10)
        print(f"  min {b:>3}-{b+4:<3}: {steam_by_min_bucket[b]:>5}  {bar}")

    # Outcome: did fav (per close_hc) cover in steam vs non-steam matches?
    cur.execute("""
        WITH close_per_match AS (
          SELECT DISTINCT ON (match_id) match_id, home_handicap AS close_hc
          FROM match_odds_history
          WHERE minute IS NOT NULL AND home_handicap IS NOT NULL
            AND home_score IS NOT NULL
          ORDER BY match_id, captured_at DESC
        )
        SELECT c.match_id, c.close_hc, m.home_score, m.away_score
        FROM close_per_match c
        JOIN matches m ON m.id = c.match_id
        WHERE m.status='FT' AND m.home_score IS NOT NULL
    """)
    close_rows = cur.fetchall()
    steam_w, steam_l, steam_p = 0, 0, 0
    nsteam_w, nsteam_l, nsteam_p = 0, 0, 0
    for (mid, close_hc_str, ft_h, ft_a) in close_rows:
        ch = parse_hc(close_hc_str)
        if ch is None or ch == 0:
            continue
        if ch < 0:  # home fav
            if ft_h + ch > ft_a: result = 'w'
            elif ft_h + ch < ft_a: result = 'l'
            else: result = 'p'
        else:  # home dog
            if ft_h + ch < ft_a: result = 'w'
            elif ft_h + ch > ft_a: result = 'l'
            else: result = 'p'
        if mid in steam_matches:
            if result == 'w': steam_w += 1
            elif result == 'l': steam_l += 1
            else: steam_p += 1
        else:
            if result == 'w': nsteam_w += 1
            elif result == 'l': nsteam_l += 1
            else: nsteam_p += 1

    def rate(w, l, p):
        n = w + l + p
        return 100*w/n if n else 0
    print(f"\nFav cover rate (vs close_hc on FT score):")
    print(f"  Steam matches:    W {steam_w:>4}  L {steam_l:>4}  P {steam_p:>4}  | cover {rate(steam_w,steam_l,steam_p):.1f}%")
    print(f"  Non-steam matches:W {nsteam_w:>4}  L {nsteam_l:>4}  P {nsteam_p:>4}  | cover {rate(nsteam_w,nsteam_l,nsteam_p):.1f}%")


# ───────────────────────────────────────────────────────────────────
# H3: Market consensus vs closing — does the line drift predict cover?
# ───────────────────────────────────────────────────────────────────
def h3_drift(cur):
    """
    For each match, compare:
      open_hc (first snapshot) → close_hc (last snapshot)
    Delta = close_hc - open_hc (in handicap units; negative = line moved
    toward home fav, i.e. home got stronger favorite).
    Bucket: did fav cover close_hc? Compare by drift bucket.
    """
    cur.execute("""
        WITH ranked AS (
          SELECT match_id, home_handicap, captured_at,
                 ROW_NUMBER() OVER (PARTITION BY match_id ORDER BY captured_at ASC) AS rn_open,
                 ROW_NUMBER() OVER (PARTITION BY match_id ORDER BY captured_at DESC) AS rn_close
          FROM match_odds_history
          WHERE home_handicap IS NOT NULL
            AND minute IS NOT NULL AND minute <= 80
        ),
        oc AS (
          SELECT match_id,
                 MAX(CASE WHEN rn_open=1  THEN home_handicap END) AS open_hc,
                 MAX(CASE WHEN rn_close=1 THEN home_handicap END) AS close_hc
          FROM ranked GROUP BY match_id
        )
        SELECT o.match_id, o.open_hc, o.close_hc, m.home_score, m.away_score
        FROM oc o
        JOIN matches m ON m.id = o.match_id
        WHERE m.status='FT' AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
    """)
    rows = cur.fetchall()
    print(f"\n=== H3: Open→Close line drift — {len(rows):,} matches ===")

    by_drift = defaultdict(lambda: [0, 0, 0])  # drift_bucket -> [W, L, P]
    for (mid, open_hc_s, close_hc_s, ft_h, ft_a) in rows:
        o = parse_hc(open_hc_s)
        c = parse_hc(close_hc_s)
        if o is None or c is None:
            continue
        drift = c - o  # +ve = home got bigger dog / smaller fav
        # Bucket into -1.0, -0.5, -0.25, 0, +0.25, +0.5, +1.0+
        if drift <= -0.75: bkt = "drift_home_fav≤-0.75"
        elif drift <= -0.25: bkt = "drift_home_fav-0.75..-0.25"
        elif drift < 0.25:   bkt = "drift_stable"
        elif drift < 0.75:   bkt = "drift_home_dog+0.25..+0.75"
        else:                bkt = "drift_home_dog≥+0.75"
        # Did fav (per close_hc) cover?
        if c < 0:  # home fav
            if ft_h + c > ft_a: result = 0  # win
            elif ft_h + c < ft_a: result = 1  # loss
            else: result = 2  # push
        else:  # home dog
            if ft_h + c < ft_a: result = 0
            elif ft_h + c > ft_a: result = 1
            else: result = 2
        by_drift[bkt][result] += 1

    print(f"{'drift bucket':<32}  {'W':>5}  {'L':>5}  {'P':>5}  {'cover%':>7}  {'n':>6}")
    for bkt in ["drift_home_fav≤-0.75", "drift_home_fav-0.75..-0.25", "drift_stable",
                "drift_home_dog+0.25..+0.75", "drift_home_dog≥+0.75"]:
        w, l, p = by_drift[bkt]
        n = w + l + p
        cover = 100*w/n if n else 0
        print(f"{bkt:<32}  {w:>5}  {l:>5}  {p:>5}  {cover:>6.1f}%  {n:>6}")


# ───────────────────────────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────────────────────────
def main():
    conn = get_conn()
    cur = conn.cursor()
    print("=" * 60)
    print("Line-movement edge tests")
    print("=" * 60)

    # Quick sanity: match count
    cur.execute("SELECT COUNT(*) FROM matches WHERE status='FT'")
    n_matches = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM match_odds_history")
    n_odds = cur.fetchone()[0]
    print(f"FT matches: {n_matches:,}  |  odds_history rows: {n_odds:,}")

    h1_clv(cur)
    h2_steam(cur)
    h3_drift(cur)

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
