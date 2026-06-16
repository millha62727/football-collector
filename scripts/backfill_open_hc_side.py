#!/usr/bin/env python3
"""Backfill `match_patterns.open_hc_side` từ `raw_features->>'opening_hc_side'`.

Idempotent — chỉ update rows có open_hc_side IS NULL.

Usage (chạy trong container football_dashboard):
    python3 scripts/backfill_open_hc_side.py --dry-run   # xem sẽ update bao nhiêu rows
    python3 scripts/backfill_open_hc_side.py --apply     # apply
    python3 scripts/backfill_open_hc_side.py --verify    # verify sau khi apply
"""
from __future__ import annotations

import argparse
import sys
from typing import Any

# Allow running as standalone script from /app inside container
sys.path.insert(0, "/app")

from app.database import _connect  # type: ignore


def _normalize_side(raw: Any) -> str | None:
    """Normalize LLM-emitted side string to canonical form.

    Accepts: 'home' | 'away' | 'level' | None | '' | 'H' | 'A'
    Returns: 'home' | 'away' | 'level' | None
    """
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in ("home", "h"):
        return "home"
    if s in ("away", "a"):
        return "away"
    if s in ("level", "draw", "even", "0"):
        return "level"
    if s == "":
        return None
    # Unknown value — keep None to surface in logs rather than corrupt
    return None


def dry_run() -> None:
    """Show stats: how many rows would be backfilled, sample values."""
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE open_hc_side IS NULL) AS pending,
                COUNT(*) FILTER (WHERE open_hc_side IS NOT NULL) AS done,
                COUNT(*) AS total,
                COUNT(*) FILTER (
                    WHERE open_hc_side IS NULL
                      AND raw_features->>'opening_hc_side' IS NOT NULL
                ) AS will_update
            FROM match_patterns
        """)
        row = cur.fetchone()
        if row is None:
            print("[DRY-RUN] No rows in match_patterns — nothing to do")
            return
        pending, done, total, will_update = row  # type: ignore[misc]

        print(f"[DRY-RUN] match_patterns total: {total}")
        print(f"[DRY-RUN]   done (open_hc_side IS NOT NULL): {done}")
        print(f"[DRY-RUN]   pending (open_hc_side IS NULL):    {pending}")
        print(f"[DRY-RUN]   will_update (have raw_features side): {will_update}")

        # Sample of what will be updated
        cur.execute("""
            SELECT match_id, raw_features->>'opening_hc_side' AS raw_side
            FROM match_patterns
            WHERE open_hc_side IS NULL
              AND raw_features->>'opening_hc_side' IS NOT NULL
            LIMIT 10
        """)
        samples = cur.fetchall()
        if samples:
            print("\n[DRY-RUN] Sample (first 10):")
            for match_id, raw_side in samples:
                norm = _normalize_side(raw_side)
                print(f"  match_id={match_id}  raw='{raw_side}'  → '{norm}'")


def apply() -> None:
    """Apply backfill. Idempotent — only updates rows where open_hc_side IS NULL."""
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE match_patterns
               SET open_hc_side = CASE LOWER(TRIM(raw_features->>'opening_hc_side'))
                                      WHEN 'home' THEN 'home'
                                      WHEN 'h'    THEN 'home'
                                      WHEN 'away' THEN 'away'
                                      WHEN 'a'    THEN 'away'
                                      WHEN 'level'  THEN 'level'
                                      WHEN 'draw'   THEN 'level'
                                      WHEN 'even'   THEN 'level'
                                      WHEN '0'      THEN 'level'
                                      ELSE NULL
                                  END
             WHERE open_hc_side IS NULL
               AND raw_features->>'opening_hc_side' IS NOT NULL
        """)
        updated = cur.rowcount
        conn.commit()
        print(f"[APPLY] Updated {updated} rows")


def verify() -> None:
    """Verify backfill state. Show distribution + any NULL/odd values."""
    with _connect() as conn:
        cur = conn.cursor()

        cur.execute("""
            SELECT open_hc_side, COUNT(*)
            FROM match_patterns
            GROUP BY open_hc_side
            ORDER BY 2 DESC
        """)
        rows = cur.fetchall()
        print("[VERIFY] Distribution:")
        total = 0
        for side, count in rows:
            print(f"  {side!r:>10}: {count}")
            total += count
        print(f"  {'TOTAL':>10}: {total}")

        # Odd values (not in canonical set)
        cur.execute("""
            SELECT match_id, raw_features->>'opening_hc_side' AS raw_side
            FROM match_patterns
            WHERE open_hc_side IS NULL
              AND raw_features->>'opening_hc_side' IS NOT NULL
            LIMIT 5
        """)
        odd = cur.fetchall()
        if odd:
            print("\n[VERIFY] ⚠️  Non-canonical side values (first 5):")
            for match_id, raw_side in odd:
                print(f"  match_id={match_id}  raw='{raw_side}'")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="Show what would be updated")
    g.add_argument("--apply", action="store_true", help="Apply the backfill")
    g.add_argument("--verify", action="store_true", help="Verify state after apply")
    args = parser.parse_args()

    if args.dry_run:
        dry_run()
    elif args.apply:
        apply()
    elif args.verify:
        verify()
    return 0


if __name__ == "__main__":
    sys.exit(main())
