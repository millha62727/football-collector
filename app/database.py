import json
import os
import sqlite3
from typing import Any

from .models import Match

# ---------------------------------------------------------------------------
# Path
# ---------------------------------------------------------------------------
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("DB_PATH") or os.path.join(_BASE, "data", "football.db")

_LIVE_STATUSES = ("LIVE", "H1", "H2", "INJURY_TIME_H1", "INJURY_TIME_H2")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------
def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                id                  TEXT PRIMARY KEY,
                competition         TEXT,
                home                TEXT,
                away                TEXT,
                start_time_utc      TEXT,
                status              TEXT,
                minute              INTEGER,
                home_score          INTEGER,
                away_score          INTEGER,
                home_handicap       TEXT,
                home_handicap_odds  REAL,
                away_handicap       TEXT,
                away_handicap_odds  REAL,
                ou_line             TEXT,
                over_odds           REAL,
                under_odds          REAL,
                odds_1              REAL,
                odds_x              REAL,
                odds_2              REAL,
                raw_data            TEXT,
                last_seen           TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_status     ON matches(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_start_time ON matches(start_time_utc)")


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------
def upsert_match(match: Match) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO matches VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                match.id, match.competition, match.home, match.away,
                match.start_time_utc.isoformat(), match.status, match.minute,
                match.home_score, match.away_score,
                match.home_handicap, match.home_handicap_odds,
                match.away_handicap, match.away_handicap_odds,
                match.ou_line, match.over_odds, match.under_odds,
                match.odds_1, match.odds_x, match.odds_2,
                json.dumps(match.raw_data) if match.raw_data else None,
                match.last_seen.isoformat(),
            ),
        )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
def get_all_matches(limit: int = 500) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM matches ORDER BY start_time_utc DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_live_matches() -> list[dict[str, Any]]:
    placeholders = ",".join("?" * len(_LIVE_STATUSES))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM matches WHERE status IN ({placeholders}) ORDER BY start_time_utc",
            _LIVE_STATUSES,
        ).fetchall()
    return [dict(r) for r in rows]


def get_stats() -> dict[str, Any]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM matches GROUP BY status"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]

    by_status = {r["status"]: r["cnt"] for r in rows}
    return {
        "total": total,
        "live": sum(by_status.get(s, 0) for s in _LIVE_STATUSES),
        "ht": by_status.get("HT", 0),
        "upcoming": by_status.get("UPCOMING", 0),
        "ft": by_status.get("FT", 0),
        "by_status": by_status,
    }
