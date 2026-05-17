import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from typing import Any, Optional
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from .models import Match

_LIVE_STATUSES = ("LIVE", "H1", "H2", "INJURY_TIME_H1", "INJURY_TIME_H2")

# Competition name patterns to exclude from all listing queries
# Covers e-sports / virtual / simulated football tournaments.
_EXCLUDE_LIKE_PATTERNS = [
    "%E Soccer%",
    "%Esoccer%",
    "%E-Soccer%",
    "%esports%",
    "%e-sports%",
    "%virtual%",
    "%ảo%",
    "%điện tử%",
    "%soccer battle%",
]
# Word-boundary regex for 'pes' so we don't accidentally match real names
# like "Hispanic" or "Naples".
_EXCLUDE_REGEX = r"\mpes\M"

_EXCLUDE_COMP_SQL = (
    "(" + " AND ".join(["competition NOT ILIKE %s"] * len(_EXCLUDE_LIKE_PATTERNS))
    + " AND competition !~* %s)"
)
_EXCLUDE_COMP_PATTERNS = _EXCLUDE_LIKE_PATTERNS + [_EXCLUDE_REGEX]

# Substring keywords (case-insensitive) used to also block these competitions
# from being persisted to the DB in the first place. Matches the SQL filter.
_EXCLUDE_SUBSTRINGS = (
    "e soccer", "esoccer", "e-soccer",
    "esports", "e-sports",
    "virtual", "ảo", "điện tử",
    "soccer battle",
)
_EXCLUDE_PES_RE = re.compile(r"\bpes\b", re.IGNORECASE)


def is_excluded_competition(name: Optional[str]) -> bool:
    """Return True if a competition name matches the e-sports / virtual filter."""
    if not name:
        return False
    lower = name.lower()
    if any(s in lower for s in _EXCLUDE_SUBSTRINGS):
        return True
    return bool(_EXCLUDE_PES_RE.search(name))

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


def _upsert_kv(key: str, value: str) -> None:
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO collector_state (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """,
            (key, value),
        )


def set_collector_state(**kwargs) -> None:
    if not kwargs:
        return
    with _connect() as conn:
        cur = conn.cursor()
        for key, val in kwargs.items():
            if isinstance(val, bool):
                val = "true" if val else "false"
            elif val is None:
                val = ""
            else:
                val = str(val)
            cur.execute(
                """
                INSERT INTO collector_state (key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                """,
                (key, val),
            )


def get_collector_state() -> dict[str, Any]:
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM collector_state")
        rows = cur.fetchall()
    kv = {r[0]: r[1] for r in rows}
    return {
        "running": kv.get("running") == "true",
        "paused": kv.get("paused") == "true",
        "loop_count": int(kv.get("loop_count") or 0),
        "session_saved": int(kv.get("session_saved") or 0),
        "session_skipped": 0,
        "error_count": int(kv.get("error_count") or 0),
        "last_fetch_at": kv.get("last_fetch_at") or None,
        "last_fetch_ms": int(kv.get("last_fetch_ms") or 0),
        "last_error": kv.get("last_error") or None,
        "api_ok": kv.get("api_ok") == "true",
        "logs": json.loads(kv.get("logs") or "[]"),
    }


def send_collector_command(cmd: str) -> None:
    _upsert_kv(f"cmd_{cmd}", "1")


def clear_collector_command(cmd: str) -> None:
    _upsert_kv(f"cmd_{cmd}", "0")


def get_collector_command(cmd: str) -> bool:
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM collector_state WHERE key = %s", (f"cmd_{cmd}",))
        row = cur.fetchone()
    return row is not None and row[0] == "1"


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

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS match_odds_history (
                        id                  BIGSERIAL PRIMARY KEY,
                        match_id            TEXT NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
                        captured_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
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
                        minute              INTEGER,
                        home_score          INTEGER,
                        away_score          INTEGER,
                        status              TEXT
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_odds_hist_match ON match_odds_history(match_id, captured_at DESC)")
                # Migrate existing table: add columns if absent (safe no-op if already present)
                for col, typedef in [
                    ("minute",     "INTEGER"),
                    ("home_score", "INTEGER"),
                    ("away_score", "INTEGER"),
                    ("status",     "TEXT"),
                ]:
                    cur.execute(f"""
                        DO $$ BEGIN
                            ALTER TABLE match_odds_history ADD COLUMN {col} {typedef};
                        EXCEPTION WHEN duplicate_column THEN NULL;
                        END $$
                    """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS match_events (
                        id           BIGSERIAL PRIMARY KEY,
                        match_id     TEXT NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
                        occurred_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        event_type   TEXT NOT NULL,
                        status       TEXT,
                        minute       INTEGER,
                        home_score   INTEGER,
                        away_score   INTEGER,
                        detail       TEXT
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_events_match ON match_events(match_id, occurred_at)")

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS collector_state (
                        key        TEXT PRIMARY KEY,
                        value      TEXT NOT NULL DEFAULT '',
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS analyzer_sessions (
                        id          BIGSERIAL PRIMARY KEY,
                        owner       TEXT NOT NULL,
                        filename    TEXT NOT NULL,
                        meta        JSONB NOT NULL DEFAULT '{}'::jsonb,
                        csv_blob    TEXT NOT NULL,
                        overrides   JSONB NOT NULL DEFAULT '{}'::jsonb,
                        notes       JSONB NOT NULL DEFAULT '[]'::jsonb,
                        note_text   TEXT NOT NULL DEFAULT '',
                        pred_fh     INTEGER,
                        pred_fa     INTEGER,
                        analysis    JSONB,
                        created_at  TIMESTAMPTZ DEFAULT NOW(),
                        updated_at  TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_analyzer_owner ON analyzer_sessions(owner, updated_at DESC)")

                # Telegram-delivered OTP store. One open OTP per (username, purpose).
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS login_otps (
                        username    TEXT NOT NULL,
                        purpose     TEXT NOT NULL,            -- 'login' or 'unlock'
                        otp_hash    TEXT NOT NULL,
                        expires_at  TIMESTAMPTZ NOT NULL,
                        sent_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        used_at     TIMESTAMPTZ,
                        PRIMARY KEY (username, purpose)
                    )
                """)
                # Seed default state rows (DO NOTHING if already exist)
                cur.execute("""
                    INSERT INTO collector_state (key, value) VALUES
                        ('running',        'false'),
                        ('paused',         'false'),
                        ('loop_count',     '0'),
                        ('session_saved',  '0'),
                        ('error_count',    '0'),
                        ('last_fetch_at',  ''),
                        ('last_fetch_ms',  '0'),
                        ('last_error',     ''),
                        ('api_ok',         'false'),
                        ('logs',           '[]'),
                        ('cmd_pause',      '0'),
                        ('cmd_resume',     '0'),
                        ('cmd_force',      '0')
                    ON CONFLICT (key) DO NOTHING
                """)

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


def user_exists(username: str) -> bool:
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE username = %s", (username,))
        return cur.fetchone() is not None


# --- OTP helpers (Telegram-delivered login + idle-unlock codes) ------------

def store_otp(username: str, purpose: str, otp_plain: str, ttl_seconds: int) -> None:
    """Replace any pending OTP for (username, purpose) with a fresh one."""
    h = _hash_password(otp_plain)
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO login_otps (username, purpose, otp_hash, expires_at, sent_at, used_at)
            VALUES (%s, %s, %s, NOW() + (%s || ' seconds')::interval, NOW(), NULL)
            ON CONFLICT (username, purpose) DO UPDATE
              SET otp_hash   = EXCLUDED.otp_hash,
                  expires_at = EXCLUDED.expires_at,
                  sent_at    = NOW(),
                  used_at    = NULL
            """,
            (username, purpose, h, str(int(ttl_seconds))),
        )


def verify_and_consume_otp(username: str, purpose: str, otp_plain: str) -> bool:
    """Validate OTP and mark it consumed. Returns True only on first successful use."""
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT otp_hash, expires_at, used_at
            FROM login_otps
            WHERE username = %s AND purpose = %s
            """,
            (username, purpose),
        )
        row = cur.fetchone()
        if row is None:
            return False
        otp_hash, expires_at, used_at = row
        if used_at is not None:
            return False
        # expires_at compared in DB; pull "now > expires_at" via a tiny query
        cur.execute("SELECT NOW() > %s", (expires_at,))
        if cur.fetchone()[0]:
            return False
        if not _verify_password(otp_plain, otp_hash):
            return False
        cur.execute(
            "UPDATE login_otps SET used_at = NOW() WHERE username = %s AND purpose = %s AND used_at IS NULL",
            (username, purpose),
        )
        return cur.rowcount == 1


def invalidate_otp(username: str, purpose: str) -> None:
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM login_otps WHERE username = %s AND purpose = %s",
            (username, purpose),
        )


# --- Write -----------------------------------------------------------------

_ODDS_FIELDS = (
    "home_handicap", "home_handicap_odds",
    "away_handicap", "away_handicap_odds",
    "ou_line", "over_odds", "under_odds",
    "odds_1", "odds_x", "odds_2",
)


def _odds_changed(match: Match, prev: dict) -> bool:
    for f in _ODDS_FIELDS:
        if getattr(match, f) != prev.get(f):
            return True
    return False


def _record_odds_snapshot(cur, match: Match) -> None:
    cur.execute(
        """
        INSERT INTO match_odds_history (
            match_id,
            home_handicap, home_handicap_odds,
            away_handicap, away_handicap_odds,
            ou_line, over_odds, under_odds,
            odds_1, odds_x, odds_2,
            minute, home_score, away_score, status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            match.id,
            match.home_handicap, match.home_handicap_odds,
            match.away_handicap, match.away_handicap_odds,
            match.ou_line, match.over_odds, match.under_odds,
            match.odds_1, match.odds_x, match.odds_2,
            match.minute, match.home_score, match.away_score, match.status,
        ),
    )


def _record_event(cur, match: Match, event_type: str, detail: str) -> None:
    cur.execute(
        """
        INSERT INTO match_events (
            match_id, event_type, status, minute, home_score, away_score, detail
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            match.id, event_type, match.status, match.minute,
            match.home_score, match.away_score, detail,
        ),
    )


def upsert_match(match: Match) -> None:
    # Drop e-sports / virtual matches at the door — see is_excluded_competition
    if is_excluded_competition(match.competition):
        return
    with _connect() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT status, minute, home_score, away_score, "
            "home_handicap, home_handicap_odds, away_handicap, away_handicap_odds, "
            "ou_line, over_odds, under_odds, odds_1, odds_x, odds_2 "
            "FROM matches WHERE id = %s",
            (match.id,),
        )
        prev = cur.fetchone()

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

        if prev is None:
            # Record initial snapshot so from-match works immediately
            _record_odds_snapshot(cur, match)
            return

        if _odds_changed(match, prev):
            _record_odds_snapshot(cur, match)

        if match.home_score != prev.get("home_score"):
            _record_event(
                cur, match, "GOAL",
                f"Home {prev.get('home_score')}→{match.home_score}",
            )
        if match.away_score != prev.get("away_score"):
            _record_event(
                cur, match, "GOAL",
                f"Away {prev.get('away_score')}→{match.away_score}",
            )
        if match.status != prev.get("status"):
            _record_event(
                cur, match, "STATUS_CHANGE",
                f"{prev.get('status')} → {match.status}",
            )


# --- Read ------------------------------------------------------------------

def get_all_matches(limit: int = 500) -> list[dict[str, Any]]:
    with _connect() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            f"SELECT * FROM matches WHERE {_EXCLUDE_COMP_SQL} "
            f"ORDER BY start_time_utc DESC LIMIT %s",
            _EXCLUDE_COMP_PATTERNS + [limit],
        )
        return [dict(r) for r in cur.fetchall()]


def get_live_matches() -> list[dict[str, Any]]:
    with _connect() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            f"SELECT * FROM matches WHERE status = ANY(%s) AND {_EXCLUDE_COMP_SQL} "
            f"ORDER BY start_time_utc",
            [list(_LIVE_STATUSES)] + _EXCLUDE_COMP_PATTERNS,
        )
        return [dict(r) for r in cur.fetchall()]


def get_stats() -> dict[str, Any]:
    with _connect() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            f"SELECT status, COUNT(*) AS cnt FROM matches WHERE {_EXCLUDE_COMP_SQL} GROUP BY status",
            _EXCLUDE_COMP_PATTERNS,
        )
        rows = cur.fetchall()
        cur.execute(
            f"SELECT COUNT(*) FROM matches WHERE {_EXCLUDE_COMP_SQL}",
            _EXCLUDE_COMP_PATTERNS,
        )
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


def get_match_by_id(match_id: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM matches WHERE id = %s", (match_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def _iso(row: dict, *fields: str) -> dict:
    for f in fields:
        v = row.get(f)
        if hasattr(v, "isoformat"):
            row[f] = v.isoformat()
    return row


def get_odds_history(match_id: str, limit: int = 500) -> list[dict[str, Any]]:
    with _connect() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM match_odds_history WHERE match_id = %s "
            "ORDER BY captured_at ASC LIMIT %s",
            (match_id, limit),
        )
        return [_iso(dict(r), "captured_at") for r in cur.fetchall()]


def get_odds_history_for_analyzer(match_id: str, limit: int = 2000) -> list[dict[str, Any]]:
    """Like get_odds_history but fetches ALL columns including minute/score/status,
    ordered oldest-first, for use by the analyzer compute pipeline."""
    with _connect() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT home_handicap, away_handicap,
                   ou_line, over_odds, under_odds,
                   odds_1, odds_x, odds_2,
                   minute, home_score, away_score, status, captured_at
            FROM match_odds_history
            WHERE match_id = %s
            ORDER BY captured_at ASC
            LIMIT %s
            """,
            (match_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def get_match_events(match_id: str, limit: int = 200) -> list[dict[str, Any]]:
    with _connect() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM match_events WHERE match_id = %s "
            "ORDER BY occurred_at ASC LIMIT %s",
            (match_id, limit),
        )
        return [_iso(dict(r), "occurred_at") for r in cur.fetchall()]


def search_matches(q: str = "", date_from: str = "", date_to: str = "", status: str = "", limit: int = 300) -> list[dict]:
    with _connect() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        filters = [_EXCLUDE_COMP_SQL]
        params: list = list(_EXCLUDE_COMP_PATTERNS)
        if q:
            filters.append("(home ILIKE %s OR away ILIKE %s OR competition ILIKE %s)")
            p = f"%{q}%"
            params += [p, p, p]
        if date_from:
            filters.append("start_time_utc >= %s")
            params.append(date_from)
        if date_to:
            filters.append("start_time_utc <= %s")
            params.append(date_to + "T23:59:59Z")
        if status:
            filters.append("status = %s")
            params.append(status)
        where = "WHERE " + " AND ".join(filters)
        params.append(limit)
        cur.execute(f"SELECT id,competition,home,away,start_time_utc,status,minute,home_score,away_score FROM matches {where} ORDER BY start_time_utc DESC LIMIT %s", params)
        return [dict(r) for r in cur.fetchall()]


def get_timeline_stats(period: str = "day") -> dict:
    with _connect() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT
                COUNT(*) AS matches,
                COUNT(*) FILTER (WHERE status = ANY(%s)) AS live,
                COUNT(*) FILTER (WHERE status = 'FT') AS finished,
                COUNT(DISTINCT competition) AS competitions,
                COALESCE(SUM(home_score + away_score), 0) AS goals
            FROM matches
            WHERE date_trunc(%s, start_time_utc::timestamptz) = date_trunc(%s, NOW())
              AND """ + _EXCLUDE_COMP_SQL + """
        """, [list(_LIVE_STATUSES), period, period] + _EXCLUDE_COMP_PATTERNS)
        row = cur.fetchone()
        return dict(row) if row else {"matches": 0, "live": 0, "finished": 0, "competitions": 0, "goals": 0}


# --- Bulk CSV import -------------------------------------------------------

def _to_f_or_none(v):
    """Float-or-None for REAL columns (None preserves NULL when value is missing)."""
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f if f != 0.0 else None  # treat 0/0.0 as 'unset' to mirror live collection
    except (TypeError, ValueError):
        return None


def _to_text_or_none(v):
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s != "0" else None


def bulk_import_csv_match(
    competition: str,
    home: str,
    away: str,
    start_time_utc,  # datetime in UTC
    rows: list,
) -> dict:
    """Insert (or top-up) one match + its odds-history snapshots from a legacy CSV.

    Returns: {match_id, created, rows_inserted, rows_skipped, excluded}
    """
    from datetime import datetime, timezone, timedelta
    from .analyzer.parser import half_to_minute, event_status_to_status, to_f

    # Skip excluded competitions outright (e-sports / virtual)
    if is_excluded_competition(competition):
        return {"match_id": None, "created": False, "rows_inserted": 0,
                "rows_skipped": 0, "excluded": True}

    if not rows:
        return {"match_id": None, "created": False, "rows_inserted": 0,
                "rows_skipped": 0, "excluded": False}

    match_id = f"{competition}_{home}_{away}_{int(start_time_utc.timestamp())}"

    # Build synthesized captured_at for each row, in UTC.
    # CSV rows carry only an HH:MM:SS time recorded in local tz (Asia/Ho_Chi_Minh
    # for the user's Windows machine). Stitch each onto the file date and roll
    # forward a day whenever the clock wraps backwards.
    LOCAL_TZ = timezone(timedelta(hours=7))  # GMT+7
    base_date = start_time_utc.astimezone(LOCAL_TZ).date()
    synth = []
    prev_secs = -1
    day_offset = 0
    for r in rows:
        t = (r.get("Time") or "").strip()
        m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", t)
        if m:
            hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
            secs = hh * 3600 + mm * 60 + ss
            if prev_secs >= 0 and secs + 60 < prev_secs:
                day_offset += 1
            prev_secs = secs
            d = base_date + timedelta(days=day_offset)
            local_dt = datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=LOCAL_TZ)
            synth.append(local_dt.astimezone(timezone.utc))
        else:
            # No time recorded — anchor at start
            synth.append(start_time_utc)

    last = rows[-1]
    last_minute = half_to_minute(last.get("Half", ""))
    last_status = event_status_to_status(last.get("Event Status", ""))
    last_home_score = int(to_f(last.get("Home Score") or 0))
    last_away_score = int(to_f(last.get("Away Score") or 0))

    with _connect() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Insert match row if absent — leaves any existing row untouched
        cur.execute(
            """
            INSERT INTO matches (
                id, competition, home, away, start_time_utc, status, minute,
                home_score, away_score,
                home_handicap, home_handicap_odds, away_handicap, away_handicap_odds,
                ou_line, over_odds, under_odds, odds_1, odds_x, odds_2,
                raw_data, last_seen
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (id) DO NOTHING
            RETURNING id
            """,
            (
                match_id, competition, home, away,
                start_time_utc.isoformat(), last_status, last_minute,
                last_home_score, last_away_score,
                _to_text_or_none(last.get("Home Handicap")),
                _to_f_or_none(last.get("Home Handicap Odds")),
                _to_text_or_none(last.get("Away Handicap")),
                _to_f_or_none(last.get("Away Handicap Odds")),
                _to_text_or_none(last.get("Over/Under Line")),
                _to_f_or_none(last.get("Over Odds")),
                _to_f_or_none(last.get("Under Odds")),
                _to_f_or_none(last.get("1X2 Home")),
                _to_f_or_none(last.get("1X2 Draw")),
                _to_f_or_none(last.get("1X2 Away")),
                None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        created = cur.fetchone() is not None

        # Pre-fetch existing captured_at to dedupe re-uploads
        cur.execute(
            "SELECT captured_at FROM match_odds_history WHERE match_id = %s",
            (match_id,),
        )
        existing = {r["captured_at"] for r in cur.fetchall()}

        inserted = skipped = 0
        for r, cap in zip(rows, synth):
            if cap in existing:
                skipped += 1
                continue
            existing.add(cap)
            cur.execute(
                """
                INSERT INTO match_odds_history (
                    match_id, captured_at,
                    home_handicap, home_handicap_odds,
                    away_handicap, away_handicap_odds,
                    ou_line, over_odds, under_odds,
                    odds_1, odds_x, odds_2,
                    minute, home_score, away_score, status
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    match_id, cap,
                    _to_text_or_none(r.get("Home Handicap")),
                    _to_f_or_none(r.get("Home Handicap Odds")),
                    _to_text_or_none(r.get("Away Handicap")),
                    _to_f_or_none(r.get("Away Handicap Odds")),
                    _to_text_or_none(r.get("Over/Under Line")),
                    _to_f_or_none(r.get("Over Odds")),
                    _to_f_or_none(r.get("Under Odds")),
                    _to_f_or_none(r.get("1X2 Home")),
                    _to_f_or_none(r.get("1X2 Draw")),
                    _to_f_or_none(r.get("1X2 Away")),
                    half_to_minute(r.get("Half", "")),
                    int(to_f(r.get("Home Score") or 0)),
                    int(to_f(r.get("Away Score") or 0)),
                    event_status_to_status(r.get("Event Status", "")),
                ),
            )
            inserted += 1

    return {
        "match_id": match_id,
        "created": created,
        "rows_inserted": inserted,
        "rows_skipped": skipped,
        "excluded": False,
    }
