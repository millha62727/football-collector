"""
Football Data Dashboard — web server only.
The data collector runs as a separate process/service (run_collector.py).

Start:  uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
"""
import json
import os
import secrets
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote

# Load .env BEFORE importing any module that reads env vars. Without this the
# web server has no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_IDS when running outside
# docker-compose (which sets `env_file`). The collector already calls this in
# app/collector.py, so the bug only affected uvicorn-launched dev/prod.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    pass

from fastapi import Body, Cookie, Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

# OTP / idle-lock config (read once at import). Both surfaceable to JS as well.
_OTP_TTL = max(60, int(os.getenv("TELEGRAM_OTP_TTL", "300")))
_IDLE_LOCK_SECONDS = max(60, int(os.getenv("IDLE_LOCK_SECONDS", "300")))

from .analyzer.views import router as analyzer_router
from .auth import check_rate_limit, create_token, decode_token, require_auth
from .database import (
    get_all_matches,
    get_collector_state,
    get_live_matches,
    get_match_by_id,
    get_match_events,
    get_odds_history,
    get_stats,
    init_db,
    search_matches,
    send_collector_command,
)

# ---------------------------------------------------------------------------
# Static HTML pages (pure strings — no f-string so CSS {} are safe)
# ---------------------------------------------------------------------------

# Shared idle-lock overlay + toast container. Substituted into every protected
# page that pulls in /static/lock.js. Each page just writes %%LOCK_OVERLAY%%.


# ---------------------------------------------------------------------------
# App lifespan (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Football Dashboard", lifespan=lifespan)


# ---- Templates ------------------------------------------------------------
# All page HTML lives under app/templates/. Jinja2Templates auto-reloads
# on file changes when uvicorn runs with --reload, otherwise content is
# read once per request — fine for this app's traffic.
_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
from fastapi.templating import Jinja2Templates  # noqa: E402  — keep close to use
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

# ---- Static files (analyzer.js etc.) --------------------------------------
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(_STATIC_DIR):
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

# ---- Analyzer page + API --------------------------------------------------
app.include_router(analyzer_router)


# ---- Security headers on every response -----------------------------------
@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# ---- Market-gate enforcement ----------------------------------------------
# Routes the gated user is still allowed to hit: /market itself, the auth
# pages, OTP endpoints (so they can verify and escape), static assets, and
# health checks. Anything else is redirected to /market.
_MARKET_GATE_ALLOW_PREFIXES = (
    "/market",
    "/login",
    "/logout",
    "/static/",
    "/api/health",
    "/api/auth/",
    "/api/lock/",
    "/api/telegram/diagnose",
    "/favicon",
)


@app.middleware("http")
async def _market_gate(request: Request, call_next):
    if request.cookies.get("market_gate") == "pending":
        path = request.url.path
        if not any(path == p.rstrip("/") or path.startswith(p) for p in _MARKET_GATE_ALLOW_PREFIXES):
            return RedirectResponse(url="/market", status_code=302)
    return await call_next(request)


# ---- Redirect 401 → /login ------------------------------------------------
@app.exception_handler(401)
async def _auth_redirect(request: Request, exc):
    return RedirectResponse(url="/login", status_code=302)


# ---- Health endpoint (no auth) -------------------------------------------
@app.get("/api/health")
async def health():
    state = get_collector_state()
    return JSONResponse({"status": "ok", "collector": {k: v for k, v in state.items() if k != "logs"}, "stats": get_stats()})

# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, auth_token: Optional[str] = Cookie(default=None)):
    if auth_token and decode_token(auth_token):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html")


@app.post("/api/auth/request-otp")
async def api_request_otp(request: Request, payload: dict = Body(...)):
    """Generate a one-time OTP and push it to all configured Telegram chats."""
    from .database import store_otp, user_exists
    from . import telegram as tg

    ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(ip):
        return JSONResponse(
            {"ok": False, "error": "Quá nhiều lần thử. Vui lòng đợi 1 phút."},
            status_code=429,
        )

    username = (payload.get("username") or "").strip()
    if not username:
        return JSONResponse({"ok": False, "error": "Thiếu tên đăng nhập"}, status_code=400)
    if not user_exists(username):
        # Don't leak which usernames exist; respond identically with a 200,
        # but skip the Telegram send.
        print(f"[WARN] OTP requested for unknown user '{username}' from {ip}", flush=True)
        return JSONResponse({"ok": True, "ttl_seconds": _OTP_TTL})
    diag = tg.diagnose()
    if not diag["has_bot_token"] or not diag["has_chat_ids"]:
        return JSONResponse(
            {"ok": False,
             "error": f"Bot Telegram chưa được cấu hình ({diag}) — đặt TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_IDS trong .env hoặc Cài đặt Telegram"},
            status_code=500,
        )

    otp = f"{secrets.randbelow(1_000_000):06d}"
    store_otp(username, "login", otp, _OTP_TTL)
    result = tg.send_login_otp(username, otp, _OTP_TTL)
    if not result.get("ok"):
        return JSONResponse(
            {"ok": False, "error": f"Không gửi được Telegram: {result.get('error') or ''}"},
            status_code=502,
        )
    print(f"[INFO] OTP sent to {result['sent']}/{result['total']} chats for user '{username}' (login)", flush=True)
    return JSONResponse({"ok": True, "ttl_seconds": _OTP_TTL})


@app.post("/api/auth/verify-otp")
async def api_verify_otp(request: Request, payload: dict = Body(...)):
    from .database import verify_and_consume_otp

    ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(ip):
        return JSONResponse(
            {"ok": False, "error": "Quá nhiều lần thử. Vui lòng đợi 1 phút."},
            status_code=429,
        )

    username = (payload.get("username") or "").strip()
    otp = (payload.get("otp") or "").strip()
    if not username or not otp:
        return JSONResponse({"ok": False, "error": "Thiếu username hoặc OTP"}, status_code=400)

    if not verify_and_consume_otp(username, "login", otp):
        print(f"[WARN] Failed OTP verify for '{username}' from {ip}", flush=True)
        return JSONResponse({"ok": False, "error": "OTP không đúng hoặc đã hết hạn"}, status_code=401)

    print(f"[INFO] User '{username}' logged in via OTP from {ip}", flush=True)
    token = create_token(username)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        "auth_token", token,
        httponly=True, samesite="lax", secure=False,
        max_age=86400,
    )
    return resp


@app.post("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("auth_token")
    return resp


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(require_auth)):
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/match/{match_id}", response_class=HTMLResponse)
async def match_detail_page(request: Request, match_id: str, user: str = Depends(require_auth)):
    return templates.TemplateResponse(
        request,
        "match_detail.html",
        {"match_id_json": json.dumps(match_id)},
    )


# ---------------------------------------------------------------------------
# Data API (all protected)
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def api_status(user: str = Depends(require_auth)):
    state = get_collector_state()
    logs  = state.pop("logs", [])
    return JSONResponse({
        "collector": state,
        "logs": logs[-80:],
        "stats": get_stats(),
        "user": user,
    })


@app.get("/api/matches")
async def api_matches(user: str = Depends(require_auth)):
    return JSONResponse(get_all_matches())


@app.get("/api/live")
async def api_live(user: str = Depends(require_auth)):
    return JSONResponse(get_live_matches())


@app.get("/api/matches/{match_id}")
async def api_match_detail(match_id: str, user: str = Depends(require_auth)):
    m = get_match_by_id(match_id)
    if m is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(m)


@app.get("/api/matches/{match_id}/odds-history")
async def api_match_odds_history(match_id: str, user: str = Depends(require_auth)):
    return JSONResponse(get_odds_history(match_id))


@app.get("/api/matches/{match_id}/events")
async def api_match_events_route(match_id: str, user: str = Depends(require_auth)):
    return JSONResponse(get_match_events(match_id))


# ---------------------------------------------------------------------------
# Collector control API
# ---------------------------------------------------------------------------

@app.post("/api/collector/pause")
async def api_pause(user: str = Depends(require_auth)):
    send_collector_command("pause")
    return {"ok": True, "paused": True}


@app.post("/api/collector/resume")
async def api_resume(user: str = Depends(require_auth)):
    send_collector_command("resume")
    return {"ok": True, "paused": False}


@app.post("/api/collector/force")
async def api_force(user: str = Depends(require_auth)):
    send_collector_command("force")
    return {"ok": True}


# ---------------------------------------------------------------------------
# New pages and API routes
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Market decoy + return-gate
# ---------------------------------------------------------------------------
# When the user lands on /market we set an HttpOnly cookie `market_gate=pending`.
# A middleware (_market_gate, registered above) bounces every request for the
# real app routes back to /market while the cookie is set. The cookie is
# cleared only when /api/lock/verify-otp succeeds, so the only way out is a
# valid Telegram OTP — even repeated Back-button presses just land back here.
_MARKET_GATE_COOKIE = "market_gate"
_MARKET_GATE_MAX_AGE = 60 * 60 * 24  # 24h


@app.get("/market", response_class=HTMLResponse)
async def market_page(request: Request):
    resp = templates.TemplateResponse(request, "market.html")
    resp.set_cookie(
        _MARKET_GATE_COOKIE,
        "pending",
        max_age=_MARKET_GATE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return resp


@app.get("/data", response_class=HTMLResponse)
async def data_browser_page(request: Request, user: str = Depends(require_auth)):
    return templates.TemplateResponse(request, "data.html")


@app.post("/api/lock/request-otp")
async def api_lock_request_otp(user: str = Depends(require_auth)):
    """Send an unlock-OTP to the configured Telegram chats for the locked user."""
    from .database import store_otp
    from . import telegram as tg

    diag = tg.diagnose()
    if not diag["has_bot_token"] or not diag["has_chat_ids"]:
        return JSONResponse(
            {"ok": False, "error": f"Bot Telegram chưa được cấu hình ({diag})"},
            status_code=500,
        )
    otp = f"{secrets.randbelow(1_000_000):06d}"
    store_otp(user, "unlock", otp, _OTP_TTL)
    result = tg.send_unlock_otp(user, otp, _OTP_TTL)
    if not result.get("ok"):
        return JSONResponse(
            {"ok": False, "error": f"Không gửi được Telegram: {result.get('error') or ''}"},
            status_code=502,
        )
    print(f"[INFO] Unlock OTP sent to {result['sent']}/{result['total']} chats for '{user}'", flush=True)
    return {"ok": True, "ttl_seconds": _OTP_TTL}


@app.post("/api/lock/verify-otp")
async def api_lock_verify_otp(payload: dict = Body(...), user: str = Depends(require_auth)):
    from .database import verify_and_consume_otp
    otp = (payload.get("otp") or "").strip()
    if not otp:
        return JSONResponse({"ok": False, "error": "Thiếu OTP"}, status_code=400)
    if verify_and_consume_otp(user, "unlock", otp):
        resp = JSONResponse({"ok": True})
        # Also clear the market gate (if set) — same OTP serves both idle-lock
        # and market-return flows. The cookie was set with HttpOnly so JS can't
        # tamper; clearing here is the only escape from /market.
        resp.delete_cookie(_MARKET_GATE_COOKIE, path="/")
        return resp
    return JSONResponse({"ok": False, "error": "OTP không đúng hoặc đã hết hạn"}, status_code=401)


@app.get("/api/lock/config")
async def api_lock_config(user: str = Depends(require_auth)):
    """Frontend reads idle timeout from here so the value lives in .env only."""
    return {"idle_seconds": _IDLE_LOCK_SECONDS, "otp_ttl_seconds": _OTP_TTL}


@app.get("/api/stats/timeline")
async def api_stats_timeline(
    period: str = "day",
    date: str = "",
    user: str = Depends(require_auth),
):
    """Stats for a period (day/month/year) or a specific calendar date.

    `date=YYYY-MM-DD` overrides `period` and serves the date-picker dashboard
    (Yêu cầu #6B).
    """
    from .database import get_timeline_stats
    return JSONResponse(get_timeline_stats(period, target_date=date or None))


# ---------------------------------------------------------------------------
# Telegram Setting API — Yêu cầu #7
# ---------------------------------------------------------------------------

@app.get("/api/telegram/settings")
async def api_telegram_settings_get(user: str = Depends(require_auth)):
    from .database import get_telegram_settings
    return JSONResponse(get_telegram_settings())


@app.post("/api/telegram/settings")
async def api_telegram_settings_save(payload: dict = Body(...), user: str = Depends(require_auth)):
    from .database import update_telegram_settings
    # Sanity-clamp the numeric thresholds before persisting.
    if "goal_threshold" in payload:
        try:
            payload["goal_threshold"] = max(1, min(20, int(payload["goal_threshold"])))
        except (TypeError, ValueError):
            payload.pop("goal_threshold", None)
    if "before_minute" in payload:
        try:
            payload["before_minute"] = max(1, min(120, int(payload["before_minute"])))
        except (TypeError, ValueError):
            payload.pop("before_minute", None)
    if "scope" in payload and payload["scope"] not in ("prestigious", "all"):
        payload.pop("scope", None)
    out = update_telegram_settings(payload)
    # `lock_required` cues the client to call window.showLock() — Yêu cầu #12.
    return {"ok": True, "settings": out, "lock_required": True}


@app.post("/api/telegram/test")
async def api_telegram_test(user: str = Depends(require_auth)):
    from . import telegram as tg
    diag = tg.diagnose()
    if not diag["has_bot_token"]:
        return JSONResponse({
            "ok": False,
            "error": "Bot token chưa cấu hình (đặt TELEGRAM_BOT_TOKEN trong .env hoặc nhập trong Cài đặt Telegram)",
            "diag": diag,
        }, status_code=400)
    if not diag["has_chat_ids"]:
        return JSONResponse({
            "ok": False,
            "error": "Chưa có chat_id (đặt TELEGRAM_CHAT_IDS trong .env hoặc nhập trong Cài đặt Telegram)",
            "diag": diag,
        }, status_code=400)
    result = tg.send_message("✅ <b>Test thành công</b>\nFootball Collector — Telegram Setting hoạt động.")
    if not result.get("ok"):
        return JSONResponse({
            "ok": False,
            "error": result.get("error") or "Không gửi được",
            "diag": diag,
        }, status_code=502)
    return {"ok": True, "sent": result.get("sent"), "total": result.get("total"), "diag": diag}


@app.get("/api/telegram/diagnose")
async def api_telegram_diagnose(user: str = Depends(require_auth)):
    """Surface exactly what's wired up so the UI can show a precise reason."""
    from . import telegram as tg
    return JSONResponse(tg.diagnose())


# ---------------------------------------------------------------------------
# Layer 3 advanced search — Yêu cầu #8
# ---------------------------------------------------------------------------

@app.post("/api/data/search-advanced")
async def api_search_advanced(payload: dict = Body(...), user: str = Depends(require_auth)):
    from .database import advanced_search
    open_hc = payload.get("open_hc")
    open_ou = payload.get("open_ou")
    ou_before_goal = payload.get("ou_before_goal") or {}
    hc_after_goal = payload.get("hc_after_goal") or {}
    limit = int(payload.get("limit") or 200)

    import time as _t
    t0 = _t.perf_counter()
    rows = advanced_search(
        open_hc=open_hc,
        open_ou=open_ou,
        ou_before_goal=ou_before_goal,
        hc_after_goal=hc_after_goal,
        limit=limit,
    )
    elapsed_ms = int((_t.perf_counter() - t0) * 1000)
    return JSONResponse({"results": rows, "count": len(rows), "elapsed_ms": elapsed_ms})


@app.get("/api/data/matches")
async def api_data_matches(q: str = "", date_from: str = "", date_to: str = "", status: str = "", limit: int = 300, user: str = Depends(require_auth)):
    return JSONResponse(search_matches(q=q, date_from=date_from, date_to=date_to, status=status, limit=limit))


@app.get("/api/data/matches/{match_id:path}/csv")
async def api_data_csv(match_id: str, user: str = Depends(require_auth)):
    from .database import get_odds_history_for_analyzer
    from .analyzer.views import _db_rows_to_csv_rows, _rows_to_csv_text
    import re
    m = get_match_by_id(match_id)
    if not m:
        return JSONResponse({"error": "not found"}, status_code=404)
    db_rows = get_odds_history_for_analyzer(match_id)
    rows = _db_rows_to_csv_rows(db_rows)
    text = _rows_to_csv_text(rows)
    safe = re.sub(r'[^\w\s-]', '', f"{m.get('home','')} vs {m.get('away','')}")
    fname = quote(safe.replace(' ', '_') + ".csv", safe="")
    return Response(
        content=text.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fname}"}
    )


@app.patch("/api/data/matches/{match_id:path}")
async def api_data_match_update(
    match_id: str,
    payload: dict = Body(...),
    user: str = Depends(require_auth),
):
    """Bulk-update a match's odds-history rows.

    Body: `{"edits": [{"id": <row_id>, "<col>": <value>, ...}, ...]}`. Only
    whitelisted columns are accepted (see _ODDS_HIST_WRITABLE).
    """
    from .database import update_odds_history_rows
    if not get_match_by_id(match_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    edits = payload.get("edits") or []
    if not isinstance(edits, list):
        return JSONResponse({"error": "edits must be a list"}, status_code=400)
    affected = update_odds_history_rows(match_id, edits)
    return JSONResponse({"ok": True, "affected": affected})


@app.delete("/api/data/matches/{match_id:path}")
async def api_data_match_delete(match_id: str, user: str = Depends(require_auth)):
    """Delete a match (cascades odds-history, events, goals, alerts)."""
    from .database import delete_match
    deleted = delete_match(match_id)
    if not deleted:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True, "id": match_id})


@app.post("/api/data/import-csv")
async def api_import_csv(
    files: list[UploadFile] = File(...),
    user: str = Depends(require_auth),
):
    """Bulk-import legacy CSV files (from the old Tkinter tool) into Postgres."""
    from datetime import datetime, timezone, timedelta
    from .analyzer.parser import parse_fname, read_csv_text
    from .database import bulk_import_csv_match

    LOCAL_TZ = timezone(timedelta(hours=7))  # filename's timestamp is GMT+7

    summary = {
        "files": 0,
        "matches_created": 0,
        "matches_updated": 0,
        "rows_inserted": 0,
        "rows_skipped": 0,
        "duplicates": 0,
        "excluded": 0,
        "errors": [],
        "results": [],  # per-file: {filename, status, detail}
    }

    def _record(filename: str, status: str, detail: str = ""):
        summary["results"].append({"filename": filename, "status": status, "detail": detail})

    for f in files:
        summary["files"] += 1
        fname = f.filename or "?"
        try:
            raw = await f.read()
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")

            meta = parse_fname(fname)
            if not meta.get("date") or not meta.get("league"):
                reason = "filename không khớp pattern YYYYMMDD_HHMM_<league>-<home>_vs_<away>.csv"
                summary["errors"].append({"filename": fname, "reason": reason})
                _record(fname, "error", reason)
                continue

            dd, mm, yyyy = meta["date"].split("/")
            hh, mi = meta["time"].split(":")
            local_dt = datetime(int(yyyy), int(mm), int(dd), int(hh), int(mi), tzinfo=LOCAL_TZ)
            start_utc = local_dt.astimezone(timezone.utc)

            rows = read_csv_text(text)
            if not rows:
                summary["errors"].append({"filename": fname, "reason": "CSV rỗng"})
                _record(fname, "error", "CSV rỗng")
                continue

            r = bulk_import_csv_match(
                competition=meta["league"],
                home=meta["home"],
                away=meta["away"],
                start_time_utc=start_utc,
                rows=rows,
            )
            if r["excluded"]:
                summary["excluded"] += 1
                _record(fname, "excluded", "competition bị loại (e-sports/virtual)")
                continue

            inserted = r.get("rows_inserted", 0)
            skipped = r.get("rows_skipped", 0)
            summary["rows_inserted"] += inserted
            summary["rows_skipped"] += skipped

            # Duplicate = match đã tồn tại + không có row mới nào được thêm
            if not r["created"] and inserted == 0:
                summary["duplicates"] += 1
                _record(fname, "duplicate", f"đã có {skipped} dòng, bỏ qua")
                continue

            if r["created"]:
                summary["matches_created"] += 1
                _record(fname, "created", f"+{inserted} dòng")
            else:
                summary["matches_updated"] += 1
                _record(fname, "updated", f"+{inserted} dòng (đã có {skipped})")
        except Exception as e:
            reason = str(e)[:200]
            summary["errors"].append({"filename": fname, "reason": reason})
            _record(fname, "error", reason)

    return JSONResponse(summary)
