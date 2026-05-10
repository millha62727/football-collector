"""
Background data-collection task.
Runs inside the FastAPI event loop as an asyncio.Task.
Controlled via AppState: pause/resume + force-fetch.
"""
import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

import aiohttp
from dotenv import load_dotenv

from .database import upsert_match
from .parser import parse_match
from .state import AppState

load_dotenv()

API_BASE = os.getenv("API_BASE_URL", "https://be.sb21.net/api/v2")
TIMEOUT_S = int(os.getenv("API_TIMEOUT", "10"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))

_SKIP_KEYWORDS = ("ảo", "virtual", "esports", "soccer battle", "điện tử")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _fetch(session: aiohttp.ClientSession, url: str) -> dict | None:
    headers = {"accept": "application/json", "content-type": "application/json", "lng": "vi"}
    try:
        async with session.get(
            url, headers=headers,
            timeout=aiohttp.ClientTimeout(total=TIMEOUT_S)
        ) as resp:
            return await resp.json() if resp.status == 200 else None
    except Exception:
        return None


def _extract(data) -> list[tuple[str, dict]]:
    """Walk the nested API structure and yield (competition_name, match_json) pairs."""
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
    low = name.lower()
    return any(k in low for k in _SKIP_KEYWORDS)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run_collector(state: AppState) -> None:
    state.running = True
    state.log("INFO", f"Collector started — API: {API_BASE}, poll every {POLL_INTERVAL}s")

    while state.running:
        # ---- Block here while paused ----
        await state.pause_event.wait()
        if not state.running:
            break

        t0 = time.time()
        state.loop_count += 1
        state.log("INFO", f"Loop #{state.loop_count} — fetching data…")

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
                        state.log("WARN", f"Parse error [{comp_name}]: {exc}")

            elapsed = int((time.time() - t0) * 1000)
            state.last_fetch_at = datetime.now(timezone.utc).isoformat()
            state.last_fetch_ms = elapsed
            state.session_saved += saved
            state.session_skipped += skipped
            state.api_ok = True
            state.last_error = None
            state.log(
                "INFO",
                f"OK — saved={saved} skipped={skipped} errors={parse_errors} ({elapsed} ms)",
            )

        except Exception as exc:
            state.error_count += 1
            state.last_error = str(exc)
            state.api_ok = False
            state.log("ERROR", f"Fetch failed: {exc}")
            # Wait up to 60 s before retry (force-fetch interrupts early)
            try:
                await asyncio.wait_for(state.force_event.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                pass
            finally:
                state.force_event.clear()
            continue

        # ---- Normal wait: POLL_INTERVAL s, interruptible by force-fetch ----
        try:
            await asyncio.wait_for(state.force_event.wait(), timeout=float(POLL_INTERVAL))
        except asyncio.TimeoutError:
            pass
        finally:
            state.force_event.clear()

    state.running = False
    state.log("INFO", "Collector stopped")
