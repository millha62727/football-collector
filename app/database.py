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


# Hand-curated tier-1 / "uy tín" league name fragments — case-insensitive
# substring match against `matches.competition`. Vietnamese names match the
# strings emitted by the upstream API (verified against the existing CSV
# fixture filename in app/analyzer/).
PRESTIGIOUS_COMPETITIONS: tuple[str, ...] = (
    "UEFA Champions League", "Champions League",
    "Copa do Brasil", "Cúp vô địch CONCACAF", "CONCACAF",
    "Vòng loại World Cup", "World Cup",
    "CAF",
    "Giải hạng Nhất Bỉ", "Cúp Quốc gia Bỉ",
    "Thụy Sĩ Super League", "Giải VĐQG Áo",
    "Giải hạng Nhất Ba Lan", "Giải hạng Nhất Scotland",
    "Cúp Quốc gia Hy Lạp",
    "Championship Anh", "2. Bundesliga",
    "Segunda División", "Serie B Ý", "Ligue 2",
    "Cúp Nhà vua Tây Ban Nha", "Cúp Quốc gia Ý",
    "Cúp Quốc gia Đức", "Cúp Quốc gia Bồ Đào Nha",
    "Cúp Quốc gia Hà Lan", "Cúp Liên Đoàn Anh",
    "Cúp Liên lục địa", "AFC Champions League", "Cúp FA Anh",
    "Premier League", "La Liga",
    "Serie A Ý", "Bundesliga", "Ligue 1",
    "Serie A Brazil", "Giải VĐQG Argentina",
    "J1 League", "K League 1",
    "MLS", "Saudi Pro League",
    "Süper Lig", "Eredivisie",
    "Primeira Liga", "Giải VĐQG Nữ Nga",
    "Cúp U23 châu Á", "Cúp Quốc gia Pháp",
    "Giải hạng Nhất Đan Mạch", "Giải VĐQG Đan Mạch",
)


def is_prestigious(name: Optional[str]) -> bool:
    """Case-insensitive substring match against the curated tier-1 league list."""
    if not name:
        return False
    lower = name.lower()
    return any(p.lower() in lower for p in PRESTIGIOUS_COMPETITIONS)

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


def _reset_pool() -> None:
    """Tear down the pool so the next _connect() rebuilds against a fresh DB.

    Postgres restarts (container recreated, host reboot) leave every pooled
    connection broken — psycopg2 surfaces them as `OperationalError` /
    `InterfaceError`. Resetting forces a clean reconnect on the next call
    instead of cycling through dead handles forever.
    """
    global _pool
    if _pool is not None:
        try:
            _pool.closeall()
        except Exception:
            pass
        _pool = None


@contextmanager
def _connect():
    conn = None
    try:
        conn = _get_pool().getconn()
        yield conn
        conn.commit()
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        # Connection-level failure: discard the broken handle AND the pool so
        # subsequent callers get a fresh socket instead of more dead ones.
        if conn:
            try:
                conn.close()
            except Exception:
                pass
            conn = None
        _reset_pool()
        raise
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            _get_pool().putconn(conn)


def db_ping() -> bool:
    """Cheap reachability probe used by container healthchecks.

    Returns True only on a successful round-trip. Any failure resets the pool
    so the next attempt rebuilds connections.
    """
    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception:
        return False


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
        "last_heartbeat": kv.get("last_heartbeat") or None,
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

                # Accent-insensitive search support — unaccent('Ngoại') = 'Ngoai'.
                # Idempotent. Requires superuser the FIRST time only; ignored
                # afterwards. If the role lacks privilege we degrade gracefully:
                # search_matches() falls back to plain ILIKE.
                try:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
                except Exception as _ext_exc:  # noqa: BLE001
                    print(f"[init_db] unaccent extension unavailable: {_ext_exc!r}", flush=True)

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

                # Provenance: 'api' (collector) vs 'csv' (bulk import). Drives the
                # dashboard TỔNG card.
                cur.execute("""
                    DO $$ BEGIN
                        ALTER TABLE matches ADD COLUMN source TEXT NOT NULL DEFAULT 'api';
                    EXCEPTION WHEN duplicate_column THEN NULL;
                    END $$
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_matches_source ON matches(source)")

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

                # Per-goal record. Captures HC/OU snapshots taken at the moment the
                # goal lands, used by the Layer 3 advanced search ("HC after goal #k"
                # / "OU before goal #k") and the Telegram alert body.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS match_goals (
                        id           BIGSERIAL PRIMARY KEY,
                        match_id     TEXT NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
                        occurred_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        goal_number  INT NOT NULL,
                        team         TEXT NOT NULL CHECK (team IN ('home','away')),
                        minute       INTEGER,
                        home_score   INTEGER,
                        away_score   INTEGER,
                        hc_before    TEXT,
                        hc_after     TEXT,
                        ou_before    TEXT,
                        ou_after     TEXT,
                        UNIQUE (match_id, goal_number)
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_goals_match ON match_goals(match_id, goal_number)")

                # Singleton row holding Telegram alert configuration. id=1 enforced
                # by CHECK so admin UI cannot accidentally create siblings.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS telegram_settings (
                        id                   INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                        scope                TEXT NOT NULL DEFAULT 'prestigious',
                        goal_threshold       INT  NOT NULL DEFAULT 3,
                        before_minute        INT  NOT NULL DEFAULT 75,
                        include_match_name   BOOLEAN NOT NULL DEFAULT TRUE,
                        include_competition  BOOLEAN NOT NULL DEFAULT TRUE,
                        include_goal_1       BOOLEAN NOT NULL DEFAULT TRUE,
                        include_goal_2       BOOLEAN NOT NULL DEFAULT TRUE,
                        include_goal_3       BOOLEAN NOT NULL DEFAULT TRUE,
                        include_goal_4       BOOLEAN NOT NULL DEFAULT FALSE,
                        chat_ids             TEXT NOT NULL DEFAULT '',
                        bot_token            TEXT NOT NULL DEFAULT '',
                        updated_at           TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                # Migrate older deploys that don't have bot_token yet.
                cur.execute("""
                    DO $$ BEGIN
                        ALTER TABLE telegram_settings ADD COLUMN bot_token TEXT NOT NULL DEFAULT '';
                    EXCEPTION WHEN duplicate_column THEN NULL;
                    END $$
                """)
                cur.execute("INSERT INTO telegram_settings (id) VALUES (1) ON CONFLICT DO NOTHING")

                # De-dup log: at most one Telegram alert per match per "session"
                # (clearable manually if the user wants to re-arm).
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS alert_log (
                        match_id      TEXT PRIMARY KEY REFERENCES matches(id) ON DELETE CASCADE,
                        triggered_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
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


# --- Maintenance / scheduling helpers --------------------------------------

def stale_finish_sweep(stale_after_hours: int = 6) -> int:
    """Force-FT live-flagged matches whose `last_seen` is older than the cutoff.

    The upstream API stops returning a match a few hours after kickoff. Without
    this sweep, finished matches keep their last live status (LIVE/H1/H2) forever
    and inflate the dashboard's LIVE/HT counters.
    """
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            UPDATE matches
               SET status = 'FT'
             WHERE status = ANY(%s)
               AND last_seen IS NOT NULL
               AND last_seen::timestamptz < NOW() - (%s || ' hours')::interval
            """,
            [list(_LIVE_STATUSES), str(int(stale_after_hours))],
        )
        return cur.rowcount


def backfill_goal_odds_after(window_minutes: int = 30) -> int:
    """Populate `match_goals.hc_after` / `ou_after` from history snapshots.

    Bookmakers suspend odds for ~30s–2 min around each goal, so the snapshot
    written at goal-time is often NULL. This sweep — called from the collector
    loop — finds the first non-NULL snapshot recorded *after* `occurred_at` and
    writes it back.

    Window is intentionally narrow (30 min) so we don't waste cycles on
    long-finished matches. Older NULLs are handled by the one-shot manual
    backfill SQL.

    Returns: number of goal rows updated.
    """
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            WITH targets AS (
                SELECT id, match_id, occurred_at
                  FROM match_goals
                 WHERE (hc_after IS NULL OR ou_after IS NULL)
                   AND occurred_at > NOW() - (%s || ' minutes')::interval
            ),
            picked AS (
                SELECT t.id,
                       (SELECT home_handicap
                          FROM match_odds_history h
                         WHERE h.match_id = t.match_id
                           AND h.captured_at > t.occurred_at
                           AND h.home_handicap IS NOT NULL
                         ORDER BY h.captured_at ASC
                         LIMIT 1) AS hc_after_pick,
                       (SELECT ou_line
                          FROM match_odds_history h
                         WHERE h.match_id = t.match_id
                           AND h.captured_at > t.occurred_at
                           AND h.ou_line IS NOT NULL
                         ORDER BY h.captured_at ASC
                         LIMIT 1) AS ou_after_pick
                  FROM targets t
            )
            UPDATE match_goals g
               SET hc_after = COALESCE(g.hc_after, p.hc_after_pick),
                   ou_after = COALESCE(g.ou_after, p.ou_after_pick)
              FROM picked p
             WHERE g.id = p.id
               AND (
                     (g.hc_after IS NULL AND p.hc_after_pick IS NOT NULL)
                  OR (g.ou_after IS NULL AND p.ou_after_pick IS NOT NULL)
                   )
            """,
            [str(int(window_minutes))],
        )
        return cur.rowcount


def db_count_upcoming_within(minutes: int) -> int:
    """Number of UPCOMING matches whose kickoff falls in the next `minutes`."""
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*) FROM matches
             WHERE status = 'UPCOMING'
               AND start_time_utc::timestamptz BETWEEN NOW() AND NOW() + (%s || ' minutes')::interval
            """,
            (str(int(minutes)),),
        )
        row = cur.fetchone()
    return int(row[0] if row else 0)


# --- Telegram settings (singleton row) --------------------------------------

_TELEGRAM_SETTINGS_COLUMNS = (
    "scope", "goal_threshold", "before_minute",
    "include_match_name", "include_competition",
    "include_goal_1", "include_goal_2", "include_goal_3", "include_goal_4",
    "chat_ids", "bot_token",
)


def get_telegram_settings() -> dict:
    with _connect() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM telegram_settings WHERE id = 1")
        row = cur.fetchone()
    if not row:
        return {
            "scope": "prestigious", "goal_threshold": 3, "before_minute": 75,
            "include_match_name": True, "include_competition": True,
            "include_goal_1": True, "include_goal_2": True,
            "include_goal_3": True, "include_goal_4": False,
            "chat_ids": "", "bot_token": "",
        }
    out = dict(row)
    out.pop("id", None)
    out.pop("updated_at", None)
    return out


def update_telegram_settings(values: dict) -> dict:
    """Upsert the singleton settings row. Unknown keys are ignored."""
    allowed = {k: values[k] for k in _TELEGRAM_SETTINGS_COLUMNS if k in values}
    if not allowed:
        return get_telegram_settings()
    set_clause = ", ".join(f"{k} = %s" for k in allowed)
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE telegram_settings SET {set_clause}, updated_at = NOW() WHERE id = 1",
            list(allowed.values()),
        )
        if cur.rowcount == 0:
            # First-time insert path (race-safe with ON CONFLICT)
            cols = ", ".join(allowed.keys())
            placeholders = ", ".join(["%s"] * len(allowed))
            cur.execute(
                f"INSERT INTO telegram_settings (id, {cols}) VALUES (1, {placeholders}) "
                f"ON CONFLICT (id) DO UPDATE SET {set_clause}, updated_at = NOW()",
                list(allowed.values()) + list(allowed.values()),
            )
    return get_telegram_settings()


# --- Priority highlight -----------------------------------------------------

def compute_priority(match: dict, last_goal_secs_ago: Optional[float]) -> int:
    """Map a match row to its dashboard highlight tier (1 = top).

    Tier rules — see Yêu cầu #4:
      1 : ≥3 goals before minute 75 AND prestigious league
      2 : ≥3 goals before minute 75 (any league)
      3 : prestigious AND a goal within the last 3 minutes
      4 : prestigious
      5 : everything else
    """
    total = int((match.get("home_score") or 0) + (match.get("away_score") or 0))
    minute = match.get("minute") or 0
    prestigious = is_prestigious(match.get("competition"))
    recent_goal = last_goal_secs_ago is not None and last_goal_secs_ago < 180

    if total >= 3 and minute < 75 and prestigious:
        return 1
    if total >= 3 and minute < 75:
        return 2
    if prestigious and recent_goal:
        return 3
    if prestigious:
        return 4
    return 5


# --- Write -----------------------------------------------------------------

_ODDS_FIELDS = (
    "home_handicap", "home_handicap_odds",
    "away_handicap", "away_handicap_odds",
    "ou_line", "over_odds", "under_odds",
    "odds_1", "odds_x", "odds_2",
)

# Snapshot persistence is gated on these fields only — Yêu cầu #11.
# Noisy 1x2/odds-decimal updates no longer create new history rows; they still
# flow into the `matches` row so the live UI sees them.
_PERSIST_TRIGGER_FIELDS = (
    "home_handicap", "away_handicap", "ou_line",
)


def _odds_changed(match: Match, prev: dict) -> bool:
    for f in _PERSIST_TRIGGER_FIELDS:
        if getattr(match, f) != prev.get(f):
            return True
    return False


def _has_any_odds(match_or_dict) -> bool:
    """True iff at least one of the trigger odds fields is non-NULL.

    Used to (a) skip recording empty snapshots into history and (b) decide
    whether to let an incoming poll overwrite the cached `matches` row.
    """
    if isinstance(match_or_dict, dict):
        get = match_or_dict.get
    else:
        get = lambda k: getattr(match_or_dict, k, None)
    return any(get(f) is not None for f in _PERSIST_TRIGGER_FIELDS)


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

        # Bug guard: upstream API returns the same match in both `today` and
        # `tomorrow` payloads (and sometimes a 2nd live-only entry that lacks
        # odds). The 2nd write would clobber `home_handicap`/`ou_line` to NULL
        # and trigger a noisy NULL snapshot 3ms after a valid one. Preserve
        # last-known-good odds when the incoming poll is empty.
        incoming_has_odds = _has_any_odds(match)
        if not incoming_has_odds and prev is not None and _has_any_odds(prev):
            for f in _ODDS_FIELDS:
                if getattr(match, f) is None and prev.get(f) is not None:
                    setattr(match, f, prev[f])

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
            if _has_any_odds(match):
                _record_odds_snapshot(cur, match)
            return

        if _odds_changed(match, prev) and _has_any_odds(match):
            _record_odds_snapshot(cur, match)

        scored_home = match.home_score != prev.get("home_score")
        scored_away = match.away_score != prev.get("away_score")
        if scored_home:
            _record_event(
                cur, match, "GOAL",
                f"Home {prev.get('home_score')}→{match.home_score}",
            )
        if scored_away:
            _record_event(
                cur, match, "GOAL",
                f"Away {prev.get('away_score')}→{match.away_score}",
            )
        if match.status != prev.get("status"):
            _record_event(
                cur, match, "STATUS_CHANGE",
                f"{prev.get('status')} → {match.status}",
            )

        if scored_home or scored_away:
            _on_goal_scored(cur, match, prev, scored_home, scored_away)


def _last_odds_before(cur, match_id: str) -> dict:
    """Most recent odds_history snapshot with NOT NULL trigger fields.

    Used to capture the `before` snapshot at goal-time. The bookmaker
    suspends odds in a window around each goal, so we need to reach back
    past the suspension to find a usable quote. Returns {} if no usable
    snapshot exists yet.
    """
    cur.execute(
        """
        SELECT home_handicap, ou_line, captured_at
          FROM match_odds_history
         WHERE match_id = %s
           AND (home_handicap IS NOT NULL OR ou_line IS NOT NULL)
         ORDER BY captured_at DESC
         LIMIT 1
        """,
        (match_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else {}


def _on_goal_scored(cur, match: Match, prev: dict, scored_home: bool, scored_away: bool) -> None:
    """Insert match_goals row(s) + fire Telegram alert if conditions met.

    Drives Yêu cầu #7. Score deltas might be >1 in pathological cases (cancelled
    goal that re-scored same poll), so we iterate the delta. Per-goal HC/OU
    "before/after" capture: 'before' = the latest non-NULL history snapshot
    (skips bookmaker suspensions), 'after' = NULL initially — backfilled by
    `backfill_goal_odds_after()` once the bookmaker re-publishes odds.
    """
    team = "home" if scored_home else "away"
    # Determine current total goal_number from any prior rows for this match.
    cur.execute(
        "SELECT COALESCE(MAX(goal_number), 0) AS g FROM match_goals WHERE match_id = %s",
        (match.id,),
    )
    next_num = (cur.fetchone() or {}).get("g", 0) + 1

    if scored_home:
        delta = (match.home_score or 0) - (prev.get("home_score") or 0)
    else:
        delta = (match.away_score or 0) - (prev.get("away_score") or 0)
    if delta < 1:  # cancellations / parser glitches — bail out cleanly
        return

    # Pull the last known-good odds quote BEFORE this goal landed. Falls
    # back to prev/match if history is empty (first-ever poll for the match).
    before_odds = _last_odds_before(cur, match.id)
    hc_before = before_odds.get("home_handicap") or prev.get("home_handicap") or match.home_handicap
    ou_before = before_odds.get("ou_line") or prev.get("ou_line") or match.ou_line

    for _ in range(delta):
        cur.execute(
            """
            INSERT INTO match_goals (
                match_id, goal_number, team, minute,
                home_score, away_score,
                hc_before, hc_after, ou_before, ou_after
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (match_id, goal_number) DO NOTHING
            """,
            (
                match.id, next_num, team, match.minute,
                match.home_score, match.away_score,
                hc_before, None, ou_before, None,
            ),
        )
        next_num += 1

    # --- Alert evaluation ---------------------------------------------------
    total_goals = (match.home_score or 0) + (match.away_score or 0)
    minute = match.minute or 0
    settings = get_telegram_settings()
    if total_goals < int(settings.get("goal_threshold", 3)):
        return
    if minute >= int(settings.get("before_minute", 75)):
        return
    if settings.get("scope") == "prestigious" and not is_prestigious(match.competition):
        return

    # De-dup: at most one alert per match (UNIQUE PK on alert_log.match_id).
    cur.execute(
        "INSERT INTO alert_log (match_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (match.id,),
    )
    if cur.rowcount != 1:
        return  # already alerted

    # Fetch the goal sequence for the message body.
    cur.execute(
        """
        SELECT goal_number, team, minute, hc_before, hc_after, ou_before, ou_after
          FROM match_goals
         WHERE match_id = %s
         ORDER BY goal_number ASC
        """,
        (match.id,),
    )
    goals = [dict(r) for r in cur.fetchall()]
    try:
        from . import telegram as _tg  # late import to avoid cycle on init
        _tg.send_goal_alert(match, goals, settings)
    except Exception:
        # Alert delivery must never break ingestion — swallow + log via stderr.
        import traceback, sys
        print("[telegram] alert send failed:", file=sys.stderr)
        traceback.print_exc()


# --- Read ------------------------------------------------------------------

def get_all_matches(limit: int = 500) -> list[dict[str, Any]]:
    """Match list enriched with priority_level for the dashboard highlight tiers.

    The LEFT JOIN LATERAL pulls the most recent GOAL event per match so we can
    detect "scored within the last 3 minutes" without a second query.
    """
    with _connect() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            f"""
            SELECT m.*,
                   EXTRACT(EPOCH FROM (NOW() - g.occurred_at)) AS last_goal_secs
              FROM matches m
              LEFT JOIN LATERAL (
                  SELECT occurred_at FROM match_events
                   WHERE match_id = m.id AND event_type = 'GOAL'
                   ORDER BY occurred_at DESC LIMIT 1
              ) g ON TRUE
             WHERE {_EXCLUDE_COMP_SQL}
             ORDER BY m.start_time_utc DESC
             LIMIT %s
            """,
            _EXCLUDE_COMP_PATTERNS + [limit],
        )
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        last_goal = r.pop("last_goal_secs", None)
        r["priority_level"] = compute_priority(r, float(last_goal) if last_goal is not None else None)
    return rows


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
        # CSV-imported subset — drives the dashboard TỔNG card (Yêu cầu #1).
        cur.execute(
            f"SELECT COUNT(*) FROM matches WHERE source = 'csv' AND {_EXCLUDE_COMP_SQL}",
            _EXCLUDE_COMP_PATTERNS,
        )
        csv_total = cur.fetchone()["count"]

    by_status = {r["status"]: r["cnt"] for r in rows}
    return {
        "total": total,
        "csv_total": csv_total,
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


# ---------------------------------------------------------------------------
# Layer 3 mutate ops (Sửa / Xóa) — Yêu cầu Data Browser
# ---------------------------------------------------------------------------

# Whitelist of columns the UI may patch on `match_odds_history`. Anything else
# (id, match_id, captured_at) is server-owned and never accepts user input.
_ODDS_HIST_WRITABLE = (
    "home_handicap", "home_handicap_odds",
    "away_handicap", "away_handicap_odds",
    "ou_line", "over_odds", "under_odds",
    "odds_1", "odds_x", "odds_2",
    "minute", "home_score", "away_score", "status",
)


def update_odds_history_rows(match_id: str, edits: list[dict]) -> int:
    """Bulk-update odds-history rows for a single match.

    Each edit is `{id: int, <field>: value, ...}`. Only whitelisted columns
    are accepted; numeric fields are coerced; empty strings → NULL. The match
    scope is enforced server-side so a leaked id from another match cannot
    overwrite it.
    """
    if not edits:
        return 0

    def _coerce(field: str, raw):
        if raw is None:
            return None
        s = str(raw).strip()
        if s == "" or s == "—":
            return None
        if field in ("home_handicap_odds", "away_handicap_odds",
                     "over_odds", "under_odds", "odds_1", "odds_x", "odds_2"):
            try:
                return float(s)
            except ValueError:
                return None
        if field in ("minute", "home_score", "away_score"):
            try:
                return int(float(s))
            except ValueError:
                return None
        return s  # text columns: handicap strings, ou_line, status

    affected = 0
    with _connect() as conn:
        cur = conn.cursor()
        for e in edits:
            raw_id = e.get("id")
            if raw_id is None:
                continue
            try:
                row_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            sets, params = [], []
            for f in _ODDS_HIST_WRITABLE:
                if f in e:
                    sets.append(f"{f} = %s")
                    params.append(_coerce(f, e[f]))
            if not sets:
                continue
            params.extend([row_id, match_id])
            cur.execute(
                f"UPDATE match_odds_history SET {', '.join(sets)} "
                f"WHERE id = %s AND match_id = %s",
                params,
            )
            affected += cur.rowcount or 0
    return affected


def delete_match(match_id: str) -> bool:
    """Delete a match. FK ON DELETE CASCADE removes odds_history, events,
    goals, alert_log; analyzer_sessions are independent (kept).

    Returns True if a row was actually deleted.
    """
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM matches WHERE id = %s", (match_id,))
        return (cur.rowcount or 0) > 0


def _iso(row: dict, *fields: str) -> dict:
    for f in fields:
        v = row.get(f)
        if hasattr(v, "isoformat"):
            row[f] = v.isoformat()
    return row


def get_odds_history(match_id: str, limit: int = 500) -> list[dict[str, Any]]:
    """Snapshot history feeding the match-detail "Biến động kèo" table.

    `minute` + `status` are surfaced explicitly so the front-end can render
    in-half time (1H:24, HT, 2H:10, FT) instead of raw wall-clock time —
    Yêu cầu #2.
    """
    with _connect() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT id, match_id, captured_at, "
            "home_handicap, home_handicap_odds, "
            "away_handicap, away_handicap_odds, "
            "ou_line, over_odds, under_odds, "
            "odds_1, odds_x, odds_2, "
            "minute, home_score, away_score, status "
            "FROM match_odds_history WHERE match_id = %s "
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


def _has_unaccent() -> bool:
    """Cached probe — does this DB have the unaccent extension installed?

    Determined once per process; the result is cached on the function itself.
    Lets `search_matches` pick the right WHERE clause without paying a catalog
    lookup on every search.
    """
    cached = getattr(_has_unaccent, "_cache", None)
    if cached is not None:
        return cached
    try:
        with _connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'unaccent'")
            cached = cur.fetchone() is not None
    except Exception:  # noqa: BLE001
        cached = False
    _has_unaccent._cache = cached  # type: ignore[attr-defined]
    return cached


def search_matches(q: str = "", date_from: str = "", date_to: str = "", status: str = "", limit: int = 300) -> list[dict]:
    with _connect() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        filters = [_EXCLUDE_COMP_SQL]
        params: list = list(_EXCLUDE_COMP_PATTERNS)
        if q:
            # Accent-insensitive search — Yêu cầu: gõ "ngoai hang anh" phải khớp
            # "Ngoại hạng Anh". Wrap cả column lẫn pattern bằng unaccent() khi
            # extension có sẵn; nếu không, degrade về ILIKE thuần (vẫn hoạt
            # động cho nội dung không dấu).
            #
            # Tokens được AND lại — mỗi token phải match ít nhất một trong
            # (home, away, competition). "ngoai hang anh brighton" khớp một
            # trận Brighton ở EPL.
            tokens = [t for t in q.split() if t]
            use_ua = _has_unaccent()
            for tok in tokens:
                if use_ua:
                    filters.append(
                        "(unaccent(home) ILIKE unaccent(%s) "
                        "OR unaccent(away) ILIKE unaccent(%s) "
                        "OR unaccent(competition) ILIKE unaccent(%s))"
                    )
                else:
                    filters.append("(home ILIKE %s OR away ILIKE %s OR competition ILIKE %s)")
                p = f"%{tok}%"
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
        rows = [dict(r) for r in cur.fetchall()]

        # Attach goal sequence for sidebar transition display.
        # +Bàn k: HC before→after · OU before→after — Yêu cầu Data Browser.
        if rows:
            ids = [r["id"] for r in rows]
            cur.execute(
                """
                SELECT match_id, goal_number, team, minute,
                       hc_before, hc_after, ou_before, ou_after
                  FROM match_goals
                 WHERE match_id = ANY(%s)
                 ORDER BY match_id, goal_number
                """,
                (ids,),
            )
            goals_by: dict[str, list[dict]] = {}
            for g in cur.fetchall():
                goals_by.setdefault(g["match_id"], []).append(dict(g))
            for r in rows:
                r["goals"] = goals_by.get(r["id"], [])
        return rows


def _norm_match_str(v) -> Optional[str]:
    """Normalize HC/OU comparison values: strip, blank→None, numeric→canonical."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        # Reformat numeric strings ("0.50", "-.25") to a stable canonical form
        # so '0.5' and '0.50' compare equal.
        f = float(s)
        return f"{f:g}"
    except ValueError:
        return s


def advanced_search(
    open_hc=None, open_ou=None,
    ou_before_goal: Optional[dict] = None,
    hc_after_goal: Optional[dict] = None,
    limit: int = 200,
) -> list[dict]:
    """Search matches by opening HC/OU + per-goal HC/OU constraints — Yêu cầu #8.

    Implementation: pull candidate matches (with their first odds snapshot for
    "opening" comparison), then filter Python-side against `match_goals` for the
    per-goal constraints. This keeps the SQL simple and avoids fragile lateral
    joins; the candidate set is naturally small because Layer 3 paginates.
    """
    open_hc_n = _norm_match_str(open_hc)
    open_ou_n = _norm_match_str(open_ou)

    with _connect() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Candidates with their first-ever snapshot per match.
        cur.execute(
            f"""
            WITH first_snap AS (
                SELECT DISTINCT ON (match_id)
                       match_id,
                       home_handicap AS open_hc,
                       ou_line       AS open_ou
                  FROM match_odds_history
                 ORDER BY match_id, captured_at ASC
            )
            SELECT m.id, m.competition, m.home, m.away,
                   m.start_time_utc, m.status, m.minute,
                   m.home_score, m.away_score,
                   f.open_hc, f.open_ou
              FROM matches m
              LEFT JOIN first_snap f ON f.match_id = m.id
             WHERE {_EXCLUDE_COMP_SQL}
             ORDER BY m.start_time_utc DESC
             LIMIT 5000
            """,
            _EXCLUDE_COMP_PATTERNS,
        )
        candidates = [dict(r) for r in cur.fetchall()]

        if open_hc_n is not None:
            candidates = [c for c in candidates if _norm_match_str(c.get("open_hc")) == open_hc_n]
        if open_ou_n is not None:
            candidates = [c for c in candidates if _norm_match_str(c.get("open_ou")) == open_ou_n]

        # Per-goal filters need the match_goals rows.
        per_goal_active = any((ou_before_goal or {}).values()) or any((hc_after_goal or {}).values())
        if per_goal_active and candidates:
            ids = [c["id"] for c in candidates]
            cur.execute(
                """
                SELECT match_id, goal_number, team, minute,
                       hc_before, hc_after, ou_before, ou_after
                  FROM match_goals
                 WHERE match_id = ANY(%s)
                """,
                (ids,),
            )
            goals_by_match: dict[str, dict[int, dict]] = {}
            for r in cur.fetchall():
                goals_by_match.setdefault(r["match_id"], {})[r["goal_number"]] = dict(r)

            def _match_ok(c) -> bool:
                gs = goals_by_match.get(c["id"], {})
                for k, want in (ou_before_goal or {}).items():
                    wn = _norm_match_str(want)
                    if wn is None:
                        continue
                    g = gs.get(int(k))
                    if not g or _norm_match_str(g.get("ou_before")) != wn:
                        return False
                for k, want in (hc_after_goal or {}).items():
                    wn = _norm_match_str(want)
                    if wn is None:
                        continue
                    g = gs.get(int(k))
                    if not g or _norm_match_str(g.get("hc_after")) != wn:
                        return False
                return True

            candidates = [c for c in candidates if _match_ok(c)]
        elif per_goal_active and not candidates:
            return []

        candidates = candidates[:limit]

        # Attach the goal sequence to each result so the UI can render details.
        if candidates:
            ids = [c["id"] for c in candidates]
            cur.execute(
                """
                SELECT match_id, goal_number, team, minute,
                       hc_before, hc_after, ou_before, ou_after
                  FROM match_goals
                 WHERE match_id = ANY(%s)
                 ORDER BY match_id, goal_number
                """,
                (ids,),
            )
            goals_by_match2: dict[str, list[dict]] = {}
            for r in cur.fetchall():
                goals_by_match2.setdefault(r["match_id"], []).append(dict(r))
            for c in candidates:
                c["goals"] = goals_by_match2.get(c["id"], [])

    return candidates


# ---------------------------------------------------------------------------
# Pattern base-rates (Layer B) — empirical "công thức" from finished matches
# ---------------------------------------------------------------------------
# These feed the AI layer (app/analyzer/ai_pattern.py) so predictions are
# grounded in real historical frequencies instead of hallucinated numbers.

def _to_float(v: Any) -> Optional[float]:
    """Parse a handicap/OU string to float; None if blank/garbage."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _split_quarter(line: float) -> list[float]:
    """Asian quarter-ball split: -0.75 → [-1.0, -0.5]; whole/half → [line]."""
    if abs(round(line * 2) - line * 2) < 1e-9:
        return [line]
    return [line - 0.25, line + 0.25]


def _cover_score(line: float, margin: float) -> float:
    """Cover outcome for the side `line` applies to (line negative for favorite).

    Returns 1.0 win, 0.5 half-win/push, 0.0 loss — quarter lines averaged.
    `margin` = (that side's score) − (opponent score).
    """
    parts = _split_quarter(line)
    s = 0.0
    for p in parts:
        eff = margin + p
        s += 1.0 if eff > 1e-9 else (0.5 if abs(eff) <= 1e-9 else 0.0)
    return s / len(parts)


def _ou_score(line: float, total: float) -> float:
    """Over outcome: 1.0 over wins, 0.5 push, 0.0 under — quarter lines averaged."""
    parts = _split_quarter(line)
    s = 0.0
    for p in parts:
        s += 1.0 if total > p + 1e-9 else (0.5 if abs(total - p) <= 1e-9 else 0.0)
    return s / len(parts)


def compute_pattern_stats(
    open_hc: Optional[str] = None,
    open_ou: Optional[str] = None,
    prestigious_only: bool = False,
    limit_matches: int = 5000,
) -> dict[str, Any]:
    """Empirical base-rates over finished matches, grounded on opening odds.

    For every FT match we take its FIRST odds snapshot (opening line) and final
    score, then compute: favorite-cover rate, over rate, avg goals, BTTS, 1X2.
    `open_hc`/`open_ou` (canonical strings, e.g. '-0.75' / '2.5') restrict to a
    bucket; omit for the overall picture plus a top-bucket breakdown.

    Returns a dict consumed by the AI layer and the analyzer UI.
    """
    open_hc_n = _norm_match_str(open_hc)
    open_ou_n = _norm_match_str(open_ou)

    with _connect() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            f"""
            WITH first_snap AS (
                SELECT DISTINCT ON (match_id)
                       match_id,
                       home_handicap AS open_hh,
                       away_handicap AS open_ah,
                       ou_line       AS open_ou
                  FROM match_odds_history
                 ORDER BY match_id, captured_at ASC
            )
            SELECT m.id, m.competition, m.home_score, m.away_score,
                   f.open_hh, f.open_ah, f.open_ou
              FROM matches m
              JOIN first_snap f ON f.match_id = m.id
             WHERE m.status = 'FT'
               AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
               AND {_EXCLUDE_COMP_SQL}
             ORDER BY m.start_time_utc DESC
             LIMIT %s
            """,
            _EXCLUDE_COMP_PATTERNS + [limit_matches],
        )
        rows = [dict(r) for r in cur.fetchall()]

    if prestigious_only:
        rows = [r for r in rows if is_prestigious(r.get("competition"))]

    n_total = len(rows)

    def _fav_line_side(r) -> tuple[Optional[float], Optional[str]]:
        hh = _to_float(r.get("open_hh"))
        ah = _to_float(r.get("open_ah"))
        if hh is not None and hh < 0:
            return hh, "home"
        if ah is not None and ah < 0:
            return ah, "away"
        return 0.0, "level"

    def _bucket_hc(r) -> Optional[str]:
        line, _ = _fav_line_side(r)
        return _norm_match_str(line) if line is not None else None

    # Apply the requested bucket filter.
    bucket = rows
    if open_hc_n is not None:
        bucket = [r for r in bucket if _bucket_hc(r) == open_hc_n]
    if open_ou_n is not None:
        bucket = [r for r in bucket if _norm_match_str(r.get("open_ou")) == open_ou_n]

    def _aggregate(sample: list[dict]) -> dict[str, Any]:
        from collections import Counter
        n = len(sample)
        if n == 0:
            return {"n": 0}
        cover_sum = cover_n = 0.0
        over_sum = over_n = 0.0
        goals_sum = btts = fav_win = draw = 0
        scores: Counter = Counter()
        for r in sample:
            hs = int(r.get("home_score") or 0)
            aw = int(r.get("away_score") or 0)
            total = hs + aw
            goals_sum += total
            scores[f"{hs}-{aw}"] += 1
            if hs > 0 and aw > 0:
                btts += 1
            if hs == aw:
                draw += 1
            line, side = _fav_line_side(r)
            if side in ("home", "away") and line is not None:
                margin = (hs - aw) if side == "home" else (aw - hs)
                cover_sum += _cover_score(line, margin)
                cover_n += 1
                if margin > 0:
                    fav_win += 1
            ou = _to_float(r.get("open_ou"))
            if ou is not None and ou > 0:
                over_sum += _ou_score(ou, total)
                over_n += 1
        return {
            "n": n,
            "fav_cover_rate": round(cover_sum / cover_n, 4) if cover_n else None,
            "fav_cover_n": int(cover_n),
            "over_rate": round(over_sum / over_n, 4) if over_n else None,
            "over_n": int(over_n),
            "avg_goals": round(goals_sum / n, 3),
            "btts_rate": round(btts / n, 4),
            "fav_win_rate": round(fav_win / cover_n, 4) if cover_n else None,
            "draw_rate": round(draw / n, 4),
            "score_dist": scores.most_common(8),
        }

    # Top opening-HC buckets (only when no HC filter, for the breakdown view).
    by_open_hc: list[dict[str, Any]] = []
    if open_hc_n is None:
        from collections import defaultdict
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            b = _bucket_hc(r)
            if b is not None:
                groups[b].append(r)
        ranked = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)[:10]
        for hc, sample in ranked:
            agg = _aggregate(sample)
            by_open_hc.append({
                "open_hc": hc,
                "n": agg["n"],
                "fav_cover_rate": agg.get("fav_cover_rate"),
                "over_rate": agg.get("over_rate"),
                "avg_goals": agg.get("avg_goals"),
            })

    return {
        "n_total": n_total,
        "filters": {
            "open_hc": open_hc_n,
            "open_ou": open_ou_n,
            "prestigious_only": prestigious_only,
        },
        "bucket": _aggregate(bucket),
        "overall": _aggregate(rows),
        "by_open_hc": by_open_hc,
    }


def get_timeline_stats(period: str = "day", target_date: Optional[str] = None) -> dict:
    """Aggregated stats for a single date (or period anchored at NOW()).

    When `target_date` is provided it MUST be YYYY-MM-DD; `period` is forced to
    'day' in that case. Otherwise behaves as before — period in {day, month, year}.
    Yêu cầu #6: extra metrics for the calendar/date-picker dashboard.
    """
    if target_date:
        period = "day"
        date_clause = "date_trunc('day', start_time_utc::timestamptz) = %s::date"
        anchor_params: list = [target_date]
    else:
        date_clause = "date_trunc(%s, start_time_utc::timestamptz) = date_trunc(%s, NOW())"
        anchor_params = [period, period]

    with _connect() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(f"""
            SELECT
                COUNT(*) AS matches,
                COUNT(*) FILTER (WHERE status = ANY(%s)) AS live,
                COUNT(*) FILTER (WHERE status = 'FT') AS finished,
                COUNT(DISTINCT competition) AS competitions,
                COALESCE(SUM(home_score + away_score) FILTER (WHERE status = 'FT'), 0) AS goals,
                COUNT(*) FILTER (WHERE status = 'FT' AND (home_score + away_score) >= 3) AS finished_3plus
            FROM matches
            WHERE {date_clause}
              AND {_EXCLUDE_COMP_SQL}
        """, [list(_LIVE_STATUSES)] + anchor_params + _EXCLUDE_COMP_PATTERNS)
        agg = cur.fetchone() or {}

        # Prestigious-league count + handicap-swing count for the period.
        # Done as two cheap follow-ups rather than monster joins.
        cur.execute(f"""
            SELECT competition, id FROM matches
            WHERE {date_clause} AND {_EXCLUDE_COMP_SQL}
        """, anchor_params + _EXCLUDE_COMP_PATTERNS)
        period_rows = cur.fetchall()
        prestigious_count = sum(1 for r in period_rows if is_prestigious(r["competition"]))

        # Handicap swing: any match whose home_handicap range exceeds 0.5 in
        # the period. Numeric cast tolerates Asian-handicap strings like "-0.25".
        if period_rows:
            ids = [r["id"] for r in period_rows]
            cur.execute("""
                SELECT match_id,
                       MAX(NULLIF(home_handicap, '')::numeric) -
                       MIN(NULLIF(home_handicap, '')::numeric) AS span
                FROM match_odds_history
                WHERE match_id = ANY(%s)
                  AND home_handicap ~ '^-?[0-9]+(\\.[0-9]+)?$'
                GROUP BY match_id
            """, (ids,))
            big_odds_swing_count = sum(1 for r in cur.fetchall() if r["span"] and abs(r["span"]) > 0.5)
        else:
            big_odds_swing_count = 0

    matches = int(agg.get("matches") or 0)
    finished = int(agg.get("finished") or 0)
    goals = int(agg.get("goals") or 0)
    finished_3plus = int(agg.get("finished_3plus") or 0)
    return {
        "matches": matches,
        "live": int(agg.get("live") or 0),
        "finished": finished,
        "competitions": int(agg.get("competitions") or 0),
        "goals": goals,
        "avg_goals": round(goals / finished, 2) if finished else 0.0,
        "pct_3plus": round(100.0 * finished_3plus / finished, 1) if finished else 0.0,
        "prestigious_count": prestigious_count,
        "big_odds_swing_count": big_odds_swing_count,
    }


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

        # Insert match row if absent. On conflict (re-upload, or row already
        # written by the live collector), bump `source` to 'csv' so the row is
        # counted under TỔNG even if its values are otherwise left in place.
        cur.execute(
            """
            INSERT INTO matches (
                id, competition, home, away, start_time_utc, status, minute,
                home_score, away_score,
                home_handicap, home_handicap_odds, away_handicap, away_handicap_odds,
                ou_line, over_odds, under_odds, odds_1, odds_x, odds_2,
                raw_data, last_seen, source
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'csv'
            )
            ON CONFLICT (id) DO UPDATE SET source = 'csv'
            RETURNING (xmax = 0) AS inserted
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
        row_ret = cur.fetchone()
        # xmax=0 → fresh insert; otherwise the ON CONFLICT branch promoted source='csv'
        created = bool(row_ret and row_ret.get("inserted"))

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
