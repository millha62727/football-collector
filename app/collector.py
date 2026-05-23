"""
Background data-collection task — standalone process.
State is persisted to the collector_state DB table so the web server
can display status and send pause/resume/force commands.
"""
import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone

import aiohttp
from dotenv import load_dotenv

from .database import (
    clear_collector_command,
    db_count_upcoming_within,
    db_ping,
    get_collector_command,
    init_db,
    set_collector_state,
    stale_finish_sweep,
    upsert_match,
    backfill_goal_odds_after,
)
from .parser import parse_match

load_dotenv()

API_BASE                  = os.getenv("API_BASE_URL", "https://be.sb21.net/api/v2")
TIMEOUT_S                 = int(os.getenv("API_TIMEOUT", "10"))
POLL_INTERVAL             = int(os.getenv("POLL_INTERVAL", "30"))
# Two-tier scheduling — Yêu cầu #11: if any UPCOMING kicks off within the next
# UPCOMING_FAST_WINDOW_MIN minutes, switch to UPCOMING_FAST_INTERVAL seconds
# between polls so we capture pre-kickoff handicap/OU drift at 1-minute fidelity.
UPCOMING_FAST_WINDOW_MIN  = int(os.getenv("UPCOMING_FAST_WINDOW_MIN", "30"))
UPCOMING_FAST_INTERVAL    = int(os.getenv("UPCOMING_FAST_INTERVAL", "60"))
STALE_SWEEP_HOURS         = int(os.getenv("STALE_FT_HOURS", "6"))
CMD_POLL_S                = 3   # how often to check for commands during sleep

# DB-init retry policy: postgres may not be ready when collector boots,
# even with `depends_on: condition: service_healthy` (compose file edit pending).
# Cap exponential backoff at 60s so the loop never goes silent for too long.
DB_INIT_BACKOFF_INITIAL_S = 2
DB_INIT_BACKOFF_MAX_S     = 60

_SKIP_KEYWORDS = ("ảo", "virtual", "esports", "soccer battle", "điện tử")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _log(logs: list, level: str, msg: str) -> None:
    entry = {"t": datetime.now(timezone.utc).strftime("%H:%M:%S"), "l": level, "m": msg}
    logs.append(entry)
    if len(logs) > 200:
        del logs[:-200]
    print(f"[{entry['t']}] [{level}] {msg}", flush=True)
    set_collector_state(logs=json.dumps(logs))


async def _fetch(session: aiohttp.ClientSession, url: str) -> dict | None:
    headers = {"accept": "application/json", "content-type": "application/json", "lng": "vi"}
    try:
        async with session.get(
            url, headers=headers,
            timeout=aiohttp.ClientTimeout(total=TIMEOUT_S),
        ) as resp:
            return await resp.json() if resp.status == 200 else None
    except Exception:
        return None


def _extract(data) -> list[tuple[str, dict]]:
    pairs: list[tuple[str, dict]] = []
    if not isinstance(data, list):
        return pairs
    for item in data:
        if isinstance(item, list):
            for comp in item:
                if isinstance(comp, dict) and "2" in comp:
                    name = comp.get("1", "Unknown")
                    for m in comp.get("2", []):
                        pairs.append((name, m))
    return pairs


def _skip(name: str) -> bool:
    return any(k in name.lower() for k in _SKIP_KEYWORDS)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run_collector() -> None:
    """Standalone collector — called from run_collector.py."""
    logs: list        = []
    loop_count        = 0
    session_saved     = 0
    error_count       = 0

    # Retry init_db with exponential backoff so a slow-booting Postgres
    # (compose `depends_on: service_healthy` only covers the first start)
    # or a mid-life DB restart doesn't crash the container into a CrashLoop.
    backoff = DB_INIT_BACKOFF_INITIAL_S
    while True:
        try:
            init_db()
            break
        except Exception as exc:
            print(
                f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] [WARN] "
                f"init_db failed ({exc!r}); retrying in {backoff}s",
                flush=True,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, DB_INIT_BACKOFF_MAX_S)

    set_collector_state(
        running=True, paused=False,
        loop_count=0, session_saved=0, error_count=0,
        last_fetch_at="", last_fetch_ms=0, last_error="", api_ok=False,
        logs="[]",
    )
    # Heartbeat for the docker healthcheck — see scripts/healthcheck_collector.py.
    set_collector_state(last_heartbeat=datetime.now(timezone.utc).isoformat())
    # Clear any stale commands from a previous run
    clear_collector_command("pause")
    clear_collector_command("resume")
    clear_collector_command("force")

    _log(logs, "INFO", f"Collector started — API: {API_BASE}, poll every {POLL_INTERVAL}s")

    try:
        while True:
            # ---- Liveness heartbeat -------------------------------------------
            # Refresh on every iteration regardless of pause/error state so the
            # container healthcheck (scripts/healthcheck_collector.py) can tell
            # "stuck process" from "intentionally paused" — paused still ticks.
            set_collector_state(last_heartbeat=datetime.now(timezone.utc).isoformat())

            # ---- Handle pause command ----------------------------------------
            if get_collector_command("pause"):
                clear_collector_command("pause")
                clear_collector_command("resume")
                set_collector_state(paused=True)
                _log(logs, "INFO", "Collector paused — waiting for resume…")
                while True:
                    await asyncio.sleep(2)
                    if get_collector_command("resume"):
                        clear_collector_command("resume")
                        set_collector_state(paused=False)
                        _log(logs, "INFO", "Collector resumed")
                        break

            # ---- Stale-status sweep -------------------------------------------
            # Reclaim matches the upstream API stopped reporting hours ago and
            # mark them FT so they stop inflating the LIVE/HT counters — Yêu cầu #3.
            try:
                swept = stale_finish_sweep(STALE_SWEEP_HOURS)
                if swept:
                    _log(logs, "INFO", f"Stale-status sweep: marked {swept} match(es) FT")
            except Exception as exc:
                _log(logs, "WARN", f"Stale sweep failed: {exc}")

            # ---- Goal-odds backfill --------------------------------------------
            # Bookmakers suspend HC/OU around each goal, so the snapshot taken at
            # goal-time is often NULL. Once the bookmaker re-publishes odds, this
            # sweep populates `hc_after`/`ou_after` for goals from the last 30 min.
            try:
                filled = backfill_goal_odds_after(window_minutes=30)
                if filled:
                    _log(logs, "INFO", f"Goal-odds backfill: filled {filled} row(s)")
            except Exception as exc:
                _log(logs, "WARN", f"Goal-odds backfill failed: {exc}")

            # ---- Fetch ----------------------------------------------------------
            t0 = time.time()
            loop_count += 1
            set_collector_state(loop_count=loop_count)
            _log(logs, "INFO", f"Loop #{loop_count} — fetching data…")

            try:
                today_url = (
                    f"{API_BASE}/getEvent"
                    "?timeRange=today&sportType=1_1&sportId=1&oddsStyle=ma&pinLeague=false"
                )
                tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
                tomorrow_url = (
                    f"{API_BASE}/getEventByDate"
                    f"?date={tomorrow}&sportType=1_1&sportId=1&oddsStyle=ma&pinLeague=false"
                )

                async with aiohttp.ClientSession() as session:
                    today_data, tomorrow_data = await asyncio.gather(
                        _fetch(session, today_url),
                        _fetch(session, tomorrow_url),
                    )

                if today_data is None and tomorrow_data is None:
                    raise ConnectionError("Both API endpoints returned empty responses")

                pairs: list[tuple[str, dict]] = []
                if today_data:
                    pairs.extend(_extract(today_data))
                if tomorrow_data:
                    pairs.extend(_extract(tomorrow_data))

                saved = skipped = parse_errors = 0
                for comp_name, match_json in pairs:
                    if _skip(comp_name):
                        skipped += 1
                        continue
                    try:
                        upsert_match(parse_match(comp_name, match_json))
                        saved += 1
                    except Exception as exc:
                        parse_errors += 1
                        if parse_errors <= 3:
                            _log(logs, "WARN", f"Parse error [{comp_name}]: {exc}")

                elapsed = int((time.time() - t0) * 1000)
                session_saved += saved
                set_collector_state(
                    last_fetch_at=datetime.now(timezone.utc).isoformat(),
                    last_fetch_ms=elapsed,
                    session_saved=session_saved,
                    api_ok=True,
                    last_error="",
                )
                _log(
                    logs, "INFO",
                    f"OK — saved={saved} skipped={skipped} errors={parse_errors} ({elapsed} ms)",
                )

            except Exception as exc:
                error_count += 1
                set_collector_state(error_count=error_count, api_ok=False, last_error=str(exc))
                _log(logs, "ERROR", f"Fetch failed: {exc}")
                # Wait up to 60 s, interruptible by force command
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    remaining = deadline - time.monotonic()
                    await asyncio.sleep(min(CMD_POLL_S, remaining))
                    if get_collector_command("force"):
                        clear_collector_command("force")
                        break
                continue

            # ---- Wait, interruptible by force command -------------------------
            # Two-tier cadence: 60s if any upcoming match within the next 30
            # minutes; otherwise POLL_INTERVAL. Decided at the END of the loop
            # so the next sleep already reflects the freshly persisted matches.
            try:
                upcoming_near = db_count_upcoming_within(UPCOMING_FAST_WINDOW_MIN)
            except Exception:
                upcoming_near = 0
            next_tick = UPCOMING_FAST_INTERVAL if upcoming_near > 0 else POLL_INTERVAL
            if upcoming_near > 0:
                _log(logs, "INFO", f"Fast cadence: {upcoming_near} upcoming within {UPCOMING_FAST_WINDOW_MIN}m → tick {next_tick}s")
            deadline = time.monotonic() + next_tick
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                await asyncio.sleep(min(CMD_POLL_S, remaining))
                if get_collector_command("force"):
                    clear_collector_command("force")
                    _log(logs, "INFO", "Force-fetch triggered")
                    break

    finally:
        set_collector_state(running=False)
        _log(logs, "INFO", "Collector stopped")
