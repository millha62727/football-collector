import hashlib
import json
import os
import time
from contextlib import contextmanager
from typing import Any, Optional
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from .models import Match

_LIVE_STATUSES = ("LIVE", "H1", "H2", "INJURY_TIME_H1", "INJURY_TIME_H2")

# --- Connection pool -------------------------------------------------------

_pool: Optional[ThreadedConnectionPool] = None

def _get_db_config() -> dict:
    url = os.getenv("DATABASE_URL", "postgresql://football:football@localhost:5432/football")
    r = urlparse(url)
    return {
        "host": r.hostname or "localhost",
        "port": r.port or 5432,
        "dbname": (r.path or "/football").lstrip("/"),
        "user": r.username or "football",
        "password": r.password or "football",
    }


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        cfg = _get_db_config()
        _pool = ThreadedConnectionPool(1, 10, **cfg)
    return _pool


@contextmanager
def _connect():
    conn = None
    try:
        conn = _get_pool().getconn()
        yield conn
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            _get_pool().putconn(conn)


# --- Init ------------------------------------------------------------------

def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000)
    return salt.hex() + ":" + dk.hex()


def _verify_password(password: str, stored: str) -> bool:
    salt_hex, dk_hex = stored.split(":", 1)
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000)
    return dk.hex() == dk_hex


def init_db() -> None:
    admin_user = os.getenv("ADMIN_USERNAME", "admin")
    admin_pass = os.getenv("ADMIN_PASSWORD", "admin123")

    retries = 15
    while retries > 0:
        try:
            with _connect() as conn:
                cur = conn.cursor()

                cur.execute("""
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

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id            SERIAL PRIMARY KEY,
                        username      TEXT UNIQUE NOT NULL,
                        password_hash TEXT NOT NULL,
                        created_at    TIMESTAMP DEFAULT NOW()
                    )
                """)

                cur.execute("CREATE INDEX IF NOT EXISTS idx_matches_status     ON matches(status)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_matches_start_time ON matches(start_time_utc)")

                # Seed admin
                cur.execute("SELECT id FROM users WHERE username = %s", (admin_user,))
                if cur.fetchone() is None:
                    cur.execute(
                        "INSERT INTO users (username, password_hash) VALUES (%s, %s)",
                        (admin_user, _hash_password(admin_pass)),
                    )
                    print(f"[init_db] Admin user '{admin_user}' created")
                else:
                    print(f"[init_db] Admin user '{admin_user}' already exists")

            return
        except psycopg2.OperationalError:
            retries -= 1
            if retries == 0:
                raise
            print(f"[init_db] PostgreSQL not ready, retrying... ({retries} left)")
            time.sleep(2)


# --- Auth helpers ----------------------------------------------------------

def verify_user(username: str, password: str) -> bool:
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT password_hash FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
    if row is None:
        return False
    return _verify_password(password, row[0])


# --- Write -----------------------------------------------------------------

def upsert_match(match: Match) -> None:
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO matches (
                id, competition, home, away, start_time_utc, status, minute,
                home_score, away_score,
                home_handicap, home_handicap_odds, away_handicap, away_handicap_odds,
                ou_line, over_odds, under_odds,
                odds_1, odds_x, odds_2,
                raw_data, last_seen
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s
            )
            ON CONFLICT (id) DO UPDATE SET
                competition        = EXCLUDED.competition,
                home               = EXCLUDED.home,
                away               = EXCLUDED.away,
                start_time_utc     = EXCLUDED.start_time_utc,
                status             = EXCLUDED.status,
                minute             = EXCLUDED.minute,
                home_score         = EXCLUDED.home_score,
                away_score         = EXCLUDED.away_score,
                home_handicap      = EXCLUDED.home_handicap,
                home_handicap_odds = EXCLUDED.home_handicap_odds,
                away_handicap      = EXCLUDED.away_handicap,
                away_handicap_odds = EXCLUDED.away_handicap_odds,
                ou_line            = EXCLUDED.ou_line,
                over_odds          = EXCLUDED.over_odds,
                under_odds         = EXCLUDED.under_odds,
                odds_1             = EXCLUDED.odds_1,
                odds_x             = EXCLUDED.odds_x,
                odds_2             = EXCLUDED.odds_2,
                raw_data           = EXCLUDED.raw_data,
                last_seen          = EXCLUDED.last_seen
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


# --- Read ------------------------------------------------------------------

def get_all_matches(limit: int = 500) -> list[dict[str, Any]]:
    with _connect() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM matches ORDER BY start_time_utc DESC LIMIT %s", (limit,))
        return [dict(r) for r in cur.fetchall()]


def get_live_matches() -> list[dict[str, Any]]:
    with _connect() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM matches WHERE status = ANY(%s) ORDER BY start_time_utc",
            (list(_LIVE_STATUSES),),
        )
        return [dict(r) for r in cur.fetchall()]


def get_stats() -> dict[str, Any]:
    with _connect() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT status, COUNT(*) AS cnt FROM matches GROUP BY status")
        rows = cur.fetchall()
        cur.execute("SELECT COUNT(*) FROM matches")
        total = cur.fetchone()["count"]

    by_status = {r["status"]: r["cnt"] for r in rows}
    return {
        "total": total,
        "live": sum(by_status.get(s, 0) for s in _LIVE_STATUSES),
        "ht": by_status.get("HT", 0),
        "upcoming": by_status.get("UPCOMING", 0),
        "ft": by_status.get("FT", 0),
        "by_status": by_status,
    }
