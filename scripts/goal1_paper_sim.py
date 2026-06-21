#!/usr/bin/env python3
"""Goal-1 paper-trade simulator — runs continuously, places "bets" in the DB only.

Strategy (verified OOS, see scripts/goal1_backtest_oos.py):
  - Filter on opening odds: |home_handicap| <= 0.5 AND opening OU >= 2.25.
  - Wait for the first goal in the match (derived from score changes in
    match_odds_history).
  - At that moment, take the FIRST snapshot AT or AFTER the goal minute
    where both over_odds AND under_odds are present.
  - Compute OVER decimal odds, vig, then "place" a $10 bet on OVER (FT > 1).
  - Resolve when the match reaches FT.

Persistence:
  Every bet → 1 row in `goal1_paper_trades` (unique on (match_id, goal1_minute)).
  The daily report (goal1_daily_report.py) rolls these up by `trade_day_vn`.

Day bucketing:
  `trade_day_vn` is the match's start time converted to UTC+7 (Vietnam time)
  then truncated to a DATE. A match kicking off at 23:30 UTC on Jun 21 =
  06:30 Jun 22 in VN → counted on Jun 22.
  A match kicking off at 16:00 UTC on Jun 21 = 23:00 Jun 21 in VN → counted
  on Jun 21.

Run modes:
  - default (foreground, verbose):  logs every detection + every loop tick.
  - --once:  one detection pass + one resolve pass + exit (for cron / smoke).
  - --smoke N: like --once but also reads N recent matches from the last 7 days
               to confirm the backtest path matches what we just inserted.

Designed to be safe to run alongside the existing collector: it ONLY reads
matches and match_odds_history, and writes ONLY to goal1_paper_trades.
"""
from __future__ import annotations

import os
import sys
import time
import signal
import argparse
from datetime import datetime, timezone, timedelta

# When invoked as `python scripts/goal1_paper_sim.py`, sys.path[0] is
# scripts/, so `import app` would fail. Push the project root.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import psycopg2
import psycopg2.extras

VN_TZ = timezone(timedelta(hours=7))
POLL_INTERVAL_S = 30

# Strategy parameters (constants — change carefully, this is what we OOS-tested).
HC_ABS_MAX = 0.5            # |opening handicap| <= 0.5
OPEN_OU_MIN = 2.25          # opening OU >= 2.25
STAKE = 10.00               # flat $10 per paper bet

# ----------------------------------------------------------------------
# DB helpers (use _connect from app.database for connection-pool consistency)
# ----------------------------------------------------------------------

def _db_connect():
    """Direct psycopg2 connection.

    Prefers `DATABASE_URL` (matches the convention used by `app/database.py`
    — both the app + collector containers load it from .env). Falls back to
    component env vars for local-dev use.
    """
    url = os.getenv("DATABASE_URL")
    if url:
        return psycopg2.connect(url, connect_timeout=10)
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "football"),
        password=os.getenv("POSTGRES_PASSWORD", "football"),
        dbname=os.getenv("POSTGRES_DB", "football"),
        connect_timeout=10,
    )


def _vn_day_from_utc(utc_iso: str) -> str:
    """Convert a UTC ISO timestamp to a Vietnam-time DATE string (YYYY-MM-DD)."""
    # start_time_utc is stored as TEXT in matches; strip timezone if present.
    s = utc_iso.replace("Z", "+00:00") if utc_iso.endswith("Z") else utc_iso
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Postgres text format fallback: "2026-06-21 17:00:00" (assumed UTC).
        dt = datetime.strptime(s.split("+")[0].strip(), "%Y-%m-%d %H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(VN_TZ).date().isoformat()


def _to_float(s) -> float | None:
    if s is None: return None
    try: return float(s)
    except (ValueError, TypeError): return None


def _malay_to_decimal(o) -> float:
    """Convert Malaysian odds to decimal odds.
    Positive (e.g. 0.85): decimal = 1 + o.
    Negative (e.g. -0.95): decimal = 1 + 1/abs(o).
    """
    if o is None or o == 0:
        return 1.0
    return 1.0 + o if o > 0 else 1.0 + 1.0 / abs(o)


# ----------------------------------------------------------------------
# Detection: find matches that just had their first goal
# ----------------------------------------------------------------------

# We scan FT-matches AT ALL STATES (UPCOMING/H1/H2/HT/FT) where:
#   - opening odds pass the filter (|HC| <= 0.5 AND open_ou >= 2.25)
#   - the first goal minute (derived from score changes) is known
#   - we have not yet placed a paper trade for that goal
#
# The unique key (match_id, goal1_minute) protects against double-placement
# when the loop ticks multiple times before resolution.

_DETECT_SQL = """
WITH first_ou AS (
  SELECT DISTINCT ON (match_id) match_id, ou_line, captured_at
  FROM match_odds_history
  WHERE ou_line IS NOT NULL
  ORDER BY match_id, captured_at ASC
),
score_changes AS (
  SELECT
    mh.match_id, mh.minute, mh.home_score, mh.away_score,
    LAG(mh.home_score) OVER (PARTITION BY mh.match_id ORDER BY mh.minute, mh.captured_at) AS prev_h,
    LAG(mh.away_score) OVER (PARTITION BY mh.match_id ORDER BY mh.minute, mh.captured_at) AS prev_a
  FROM match_odds_history mh
  WHERE mh.home_score IS NOT NULL AND mh.away_score IS NOT NULL
),
goal_events AS (
  SELECT match_id, MIN(minute) AS first_goal_minute
  FROM score_changes
  WHERE prev_h IS NOT NULL AND (home_score > prev_h OR away_score > prev_a)
  GROUP BY match_id
),
odds_at_goal AS (
  SELECT
    ge.match_id, ge.first_goal_minute,
    mh.over_odds, mh.under_odds, mh.minute AS snap_minute, mh.ou_line AS ou_at_bet,
    ROW_NUMBER() OVER (PARTITION BY ge.match_id ORDER BY mh.minute ASC, mh.captured_at ASC) AS rn
  FROM goal_events ge
  JOIN match_odds_history mh
    ON mh.match_id = ge.match_id
   AND mh.over_odds IS NOT NULL
   AND mh.under_odds IS NOT NULL
   AND mh.minute >= ge.first_goal_minute
)
SELECT
  m.id,
  m.home_handicap AS open_hc,
  fo.ou_line      AS open_ou,
  ge.first_goal_minute,
  oag.over_odds,
  oag.under_odds,
  oag.ou_at_bet,
  m.start_time_utc
FROM matches m
JOIN first_ou  fo  ON fo.match_id = m.id
JOIN goal_events ge ON ge.match_id = m.id
JOIN odds_at_goal oag ON oag.match_id = ge.match_id AND oag.rn = 1
LEFT JOIN goal1_paper_trades t
  ON t.match_id = m.id AND t.goal1_minute = ge.first_goal_minute
WHERE m.status IN ('H1','H2','HT','FT')
  AND ABS(m.home_handicap::float) <= %s
  AND fo.ou_line::float >= %s
  AND t.id IS NULL
ORDER BY m.start_time_utc::timestamptz DESC;
"""


def detect_and_place(conn) -> int:
    """Find matches that just hit goal 1 under our filter and place paper bets.
    Returns the number of new rows inserted.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(_DETECT_SQL, (HC_ABS_MAX, OPEN_OU_MIN))
    rows = cur.fetchall()

    if not rows:
        return 0

    placed = 0
    insert_sql = """
    INSERT INTO goal1_paper_trades
        (match_id, goal1_minute, open_hc, open_ou, open_ou_value,
         over_odds, under_odds, over_decimal, vig, ou_line_at_bet,
         trade_day_vn)
    VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s)
    ON CONFLICT (match_id, goal1_minute) DO NOTHING;
    """
    for r in rows:
        over_dec = _malay_to_decimal(r["over_odds"])
        under_dec = _malay_to_decimal(r["under_odds"])
        vig = (1.0 / over_dec) + (1.0 / under_dec) - 1.0
        trade_day = _vn_day_from_utc(r["start_time_utc"])
        try:
            cur.execute(insert_sql, (
                r["id"], r["first_goal_minute"],
                r["open_hc"], r["open_ou"], _to_float(r["open_ou"]),
                r["over_odds"], r["under_odds"],
                over_dec, vig, r["ou_at_bet"],
                trade_day,
            ))
            placed += 1
            print(f"  PLACED  {r['id'][:60]:<60}  G1={r['first_goal_minute']:>3}'  "
                  f"OD={over_dec:.3f}  VIG={vig*100:+.1f}%  day={trade_day}",
                  flush=True)
        except Exception as exc:
            print(f"  ERROR insert {r['id']}: {exc!r}", flush=True)

    conn.commit()
    return placed


# ----------------------------------------------------------------------
# Resolution: stamp ft_home/ft_away/pnl for matches now FT
# ----------------------------------------------------------------------

_RESOLVE_SQL = """
UPDATE goal1_paper_trades t
SET ft_home     = m.home_score,
    ft_away     = m.away_score,
    ft_total    = m.home_score + m.away_score,
    will_win    = (m.home_score + m.away_score) > 1,
    pnl         = CASE
                    WHEN (m.home_score + m.away_score) > 1
                      THEN t.stake * (t.over_decimal - 1)
                    ELSE -t.stake
                  END,
    resolved_at = NOW()
FROM matches m
WHERE m.id = t.match_id
  AND m.status = 'FT'
  AND t.resolved_at IS NULL
RETURNING t.id, t.match_id, t.ft_total, t.will_win, t.pnl;
"""


def resolve_finished(conn) -> int:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(_RESOLVE_SQL)
    rows = cur.fetchall()
    if rows:
        for r in rows:
            tag = "WIN " if r["will_win"] else "LOSS"
            print(f"  {tag}  {r['match_id'][:60]:<60}  FT={r['ft_total']:>2}  P/L=${float(r['pnl']):+.2f}",
                  flush=True)
        conn.commit()
    return len(rows)


# ----------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------

_running = True


def _handle_sigterm(signum, frame):
    global _running
    _running = False


def _loop(poll_interval: int):
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    print(f"goal1_paper_sim: starting (poll={poll_interval}s, |HC|<={HC_ABS_MAX}, OU>={OPEN_OU_MIN}, stake=${STAKE:.0f})",
          flush=True)

    while _running:
        loop_start = time.time()
        try:
            with _db_connect() as conn:
                placed = detect_and_place(conn)
                resolved = resolve_finished(conn)
            if placed or resolved:
                print(f"  tick: +{placed} placed, +{resolved} resolved", flush=True)
        except psycopg2.OperationalError as exc:
            # Transient DB hiccup — back off and retry next tick.
            print(f"  db unreachable: {exc!r}", flush=True)
        except Exception as exc:
            # Anything else: log and keep going so the loop survives.
            import traceback
            print(f"  loop error: {exc!r}\n{traceback.format_exc()}", flush=True)

        elapsed = time.time() - loop_start
        sleep_for = max(0, poll_interval - int(elapsed))
        for _ in range(sleep_for):
            if not _running: break
            time.sleep(1)

    print("goal1_paper_sim: stopped", flush=True)


def _smoke(n: int):
    """Run detect+resolve once over N recent matches to confirm wiring."""
    print(f"goal1_paper_sim: smoke mode (last {n} matches w/ full data)", flush=True)
    with _db_connect() as conn:
        placed = detect_and_place(conn)
        resolved = resolve_finished(conn)
        print(f"  smoke: +{placed} placed, +{resolved} resolved", flush=True)

        # Pull the trades we just inserted for the smoke window.
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT t.match_id, t.goal1_minute, t.open_hc, t.open_ou,
                   t.over_decimal, t.vig, t.ft_total, t.will_win, t.pnl,
                   t.trade_day_vn, m.home, m.away
            FROM goal1_paper_trades t
            JOIN matches m ON m.id = t.match_id
            WHERE m.start_time_utc::timestamptz > NOW() - INTERVAL '14 days'
            ORDER BY m.start_time_utc::timestamptz DESC
            LIMIT %s;
        """, (n,))
        rows = cur.fetchall()
        print(f"\n  recent paper trades (last 14d, max {n}):")
        for r in rows:
            tag = "W" if r["will_win"] else ("L" if r["will_win"] is False else "?")
            print(f"    [{tag}] {r['home'][:20]:<20} vs {r['away'][:20]:<20}  "
                  f"G1={r['goal1_minute']:>3}'  HC={r['open_hc']:<5}  OU={r['open_ou']:<5}  "
                  f"OD={float(r['over_decimal']):.3f}  VIG={float(r['vig'])*100:+.1f}%  "
                  f"FT={r['ft_total']}  P/L=${float(r['pnl']) if r['pnl'] is not None else 0:+.2f}  day={r['trade_day_vn']}",
                  flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="one pass + exit")
    p.add_argument("--smoke", type=int, default=0, metavar="N",
                   help="one pass + print N recent trades from goal1_paper_trades")
    p.add_argument("--poll", type=int, default=POLL_INTERVAL_S,
                   help=f"loop interval in seconds (default {POLL_INTERVAL_S})")
    args = p.parse_args()

    if args.smoke:
        _smoke(args.smoke)
    elif args.once:
        with _db_connect() as conn:
            p = detect_and_place(conn); r = resolve_finished(conn)
            print(f"once: +{p} placed, +{r} resolved", flush=True)
    else:
        _loop(args.poll)


if __name__ == "__main__":
    main()