#!/usr/bin/env python3
"""Daily report generator for goal1_paper_sim.

Reads `goal1_paper_trades` for a given VN-day and writes a Markdown report.
Default day = today (VN). Override with --day YYYY-MM-DD.

Output:
  reports/goal1_YYYY-MM-DD.md  — one card per day, persistent.

The report shows:
  - cumulative running totals since the simulator started
  - that day's bets, in chronological order
  - daily P/L, win rate, ROI, vig, avg odds
  - resolved vs pending bets
  - the strategy parameters (so the card is self-describing)

Usage:
  python scripts/goal1_daily_report.py                  # today (VN)
  python scripts/goal1_daily_report.py --day 2026-06-21 # specific day
  python scripts/goal1_daily_report.py --stdout        # print, do not write file
"""
from __future__ import annotations

import os
import sys
import argparse
import statistics
from datetime import datetime, timezone, timedelta

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import psycopg2
import psycopg2.extras

VN_TZ = timezone(timedelta(hours=7))

REPORTS_DIR = os.path.join(_PROJECT_ROOT, "reports")


def _db_connect():
    """Prefer DATABASE_URL (set by .env in app + collector containers), fall
    back to component env vars for local-dev."""
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


def _today_vn() -> str:
    return datetime.now(VN_TZ).date().isoformat()


def _fetch_day(conn, day: str):
    """Return rows for one VN day, plus running totals since the simulator started."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Day's trades
    cur.execute("""
        SELECT t.id, t.match_id, t.goal1_minute, t.open_hc, t.open_ou,
               t.over_decimal, t.vig, t.ou_line_at_bet,
               t.ft_home, t.ft_away, t.ft_total, t.will_win, t.pnl,
               t.resolved_at, t.trade_day_vn,
               m.competition, m.home, m.away, m.start_time_utc::timestamptz AS started
        FROM goal1_paper_trades t
        JOIN matches m ON m.id = t.match_id
        WHERE t.trade_day_vn = %s
        ORDER BY m.start_time_utc ASC, t.goal1_minute ASC;
    """, (day,))
    day_rows = cur.fetchall()

    # All-time aggregates (since the table was created)
    cur.execute("""
        SELECT
            COUNT(*) AS total_bets,
            SUM(CASE WHEN resolved_at IS NOT NULL THEN 1 ELSE 0 END) AS resolved,
            SUM(CASE WHEN will_win IS TRUE THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN will_win IS FALSE THEN 1 ELSE 0 END) AS losses,
            SUM(COALESCE(pnl, 0)) AS total_pnl,
            SUM(COALESCE(stake, 0)) AS total_stake
        FROM goal1_paper_trades;
    """)
    all_time = cur.fetchone()

    return day_rows, all_time


def _fmt_pct(x: float) -> str:
    return f"{x:+.1f}%"


def _render_markdown(day: str, day_rows, all_time) -> str:
    n_bets = len(day_rows)
    resolved = [r for r in day_rows if r["resolved_at"] is not None]
    pending = [r for r in day_rows if r["resolved_at"] is None]

    wins = sum(1 for r in resolved if r["will_win"])
    losses = len(resolved) - wins
    win_rate = (100.0 * wins / len(resolved)) if resolved else None

    pnl = sum(float(r["pnl"]) for r in resolved)
    stake = 10.0 * len(resolved)
    roi = (100.0 * pnl / stake) if stake else None

    vig_vals = [float(r["vig"]) for r in resolved if r["vig"] is not None]
    avg_vig = (100.0 * statistics.mean(vig_vals)) if vig_vals else None
    avg_odds = (statistics.mean([float(r["over_decimal"]) for r in resolved]) if resolved else None)

    lines: list[str] = []
    lines.append(f"# Goal-1 paper-trade report — {day} (VN)")
    lines.append("")
    lines.append(f"_Generated: {datetime.now(VN_TZ).isoformat(timespec='seconds')}_")
    lines.append("")
    lines.append("## Strategy")
    lines.append("- Filter on opening odds: `|home_handicap| <= 0.5` AND `opening OU >= 2.25`.")
    lines.append("- At goal 1 (first score change in `match_odds_history`), bet **$10 OVER** (FT > 1).")
    lines.append("- Stake = flat $10. No Kelly yet.")
    lines.append("- Day bucket = match kickoff converted to UTC+7 then truncated to date.")
    lines.append("- Reference backtest: ROI +53.3%, win rate 84.1%, n=671 (combined 2025+2026).")
    lines.append("")

    # Headline KPIs
    lines.append("## Daily totals")
    if n_bets == 0:
        lines.append("_No paper trades today yet — service is running, will populate as matches kick off._")
    else:
        lines.append(f"| Metric | Value |")
        lines.append(f"|---|---:|")
        lines.append(f"| Bets placed today | {n_bets} |")
        lines.append(f"| Resolved | {len(resolved)} |")
        lines.append(f"| Pending | {len(pending)} |")
        lines.append(f"| Wins / Losses | {wins} / {losses} |")
        if win_rate is not None:
            lines.append(f"| Win rate (resolved) | {win_rate:.1f}% |")
        if pnl != 0 or stake:
            lines.append(f"| Daily P/L | ${pnl:+.2f} |")
            lines.append(f"| Daily stake | ${stake:.2f} |")
        if roi is not None:
            lines.append(f"| Daily ROI | {_fmt_pct(roi)} |")
        if avg_vig is not None:
            lines.append(f"| Avg vig | {avg_vig:+.2f}% |")
        if avg_odds is not None:
            lines.append(f"| Avg over odds | {avg_odds:.3f} |")
    lines.append("")

    # Cumulative
    if all_time and all_time["total_bets"]:
        cum_bets = int(all_time["total_bets"])
        cum_resolved = int(all_time["resolved"] or 0)
        cum_wins = int(all_time["wins"] or 0)
        cum_pnl = float(all_time["total_pnl"] or 0)
        cum_stake = float(all_time["total_stake"] or 0)
        cum_roi = (100.0 * cum_pnl / cum_stake) if cum_stake else None
        cum_wr = (100.0 * cum_wins / cum_resolved) if cum_resolved else None

        lines.append("## Cumulative since simulator start")
        lines.append(f"| Metric | Value |")
        lines.append(f"|---|---:|")
        lines.append(f"| Total bets | {cum_bets} |")
        lines.append(f"| Resolved | {cum_resolved} |")
        lines.append(f"| Wins | {cum_wins} |")
        lines.append(f"| Cumulative P/L | ${cum_pnl:+.2f} |")
        lines.append(f"| Cumulative stake | ${cum_stake:.2f} |")
        if cum_roi is not None:
            lines.append(f"| Cumulative ROI | {_fmt_pct(cum_roi)} |")
        if cum_wr is not None:
            lines.append(f"| Win rate | {cum_wr:.1f}% |")
        lines.append("")

    # Per-bet table (resolved first, then pending)
    if day_rows:
        lines.append("## Bets today")
        lines.append("")
        lines.append("| Time (UTC) | Match | HC | OU | G1' | Over odds | Vig | FT | Result | P/L |")
        lines.append("|---|---|---|---|---:|---:|---:|---:|---|---:|")
        for r in day_rows:
            started = r["started"].strftime("%m-%d %H:%M") if r.get("started") else "?"
            match = f"{r['home'][:18]} vs {r['away'][:18]}"
            hc = r["open_hc"] or "-"
            ou = r["open_ou"] or "-"
            g1 = r["goal1_minute"]
            od = float(r["over_decimal"])
            vig = float(r["vig"]) * 100
            if r["resolved_at"]:
                ft = f"{r['ft_home']}-{r['ft_away']} ({r['ft_total']})"
                if r["will_win"]:
                    res = "✅ WIN"
                else:
                    res = "❌ LOSS"
                pnl_str = f"${float(r['pnl']):+.2f}"
            else:
                ft = "pending"
                res = "⏳ pending"
                pnl_str = "—"
            lines.append(f"| {started} | {match} | {hc} | {ou} | {g1} | {od:.3f} | {vig:+.1f}% | {ft} | {res} | {pnl_str} |")
        lines.append("")

    return "\n".join(lines) + "\n"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--day", default=None, help="VN day YYYY-MM-DD (default: today VN)")
    p.add_argument("--stdout", action="store_true", help="print to stdout instead of writing reports/")
    args = p.parse_args()

    day = args.day or _today_vn()

    with _db_connect() as conn:
        day_rows, all_time = _fetch_day(conn, day)
    md = _render_markdown(day, day_rows, all_time)

    if args.stdout:
        sys.stdout.write(md)
        return

    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.join(REPORTS_DIR, f"goal1_{day}.md")
    with open(path, "w") as f:
        f.write(md)
    print(f"wrote {path} ({len(day_rows)} bets, ${sum(float(r['pnl']) for r in day_rows if r['resolved_at'] is not None):+.2f} P/L)",
          flush=True)


if __name__ == "__main__":
    main()