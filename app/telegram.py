"""One-way Telegram notifier for OTP delivery.

Reads BOT_TOKEN + CHAT_IDS from environment. No long-polling — the bot is
only used to push messages out (login OTP, idle-lock OTP). Users read the
message in Telegram and paste the code back into the web UI.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable

log = logging.getLogger(__name__)

_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
_CHAT_IDS_RAW = os.getenv("TELEGRAM_CHAT_IDS", "").strip()
_TIMEOUT = 10


def _chat_ids_from_env() -> list[str]:
    if not _CHAT_IDS_RAW:
        return []
    return [c.strip() for c in _CHAT_IDS_RAW.split(",") if c.strip()]


def _chat_ids() -> list[str]:
    """Resolve effective chat_ids: DB settings override env, env is fallback.

    Read failures fall back to env silently so the OTP path keeps working even
    when the DB is briefly unavailable.
    """
    try:
        from .database import get_telegram_settings
        db = (get_telegram_settings().get("chat_ids") or "").strip()
        if db:
            return [c.strip() for c in db.split(",") if c.strip()]
    except Exception:
        pass
    return _chat_ids_from_env()


def is_configured() -> bool:
    """True when both BOT_TOKEN and at least one CHAT_ID are present."""
    return bool(_BOT_TOKEN) and bool(_chat_ids())


def send_message(text: str, chat_ids: Iterable[str] | None = None) -> dict:
    """Push `text` to every chat_id. Returns delivery summary.

    Failure to deliver to one chat does not block the others. The web UI
    treats overall success as: at least one chat_id received the message.
    """
    targets = list(chat_ids) if chat_ids is not None else _chat_ids()
    if not _BOT_TOKEN:
        return {"ok": False, "sent": 0, "total": 0, "error": "TELEGRAM_BOT_TOKEN chưa được cấu hình"}
    if not targets:
        return {"ok": False, "sent": 0, "total": 0, "error": "TELEGRAM_CHAT_IDS chưa được cấu hình"}

    url = f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage"
    sent = 0
    errors: list[str] = []
    for cid in targets:
        try:
            body = urllib.parse.urlencode({
                "chat_id": cid,
                "text": text,
                "parse_mode": "HTML",
            }).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                if resp.status == 200:
                    sent += 1
                else:
                    errors.append(f"{cid}: HTTP {resp.status}")
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                detail = ""
            errors.append(f"{cid}: HTTP {e.code} {detail}")
        except Exception as e:
            errors.append(f"{cid}: {e}")

    if errors:
        log.warning("telegram send: %d/%d ok, errors=%s", sent, len(targets), errors)

    return {
        "ok": sent > 0,
        "sent": sent,
        "total": len(targets),
        "error": "; ".join(errors) if errors and sent == 0 else None,
    }


def send_login_otp(username: str, otp: str, ttl_seconds: int) -> dict:
    """Compose + push a login-OTP message."""
    text = (
        f"🔐 <b>Mã OTP đăng nhập</b>\n\n"
        f"User: <code>{username}</code>\n"
        f"OTP:  <code>{otp}</code>\n\n"
        f"Hết hạn sau {ttl_seconds // 60} phút."
    )
    return send_message(text)


def send_unlock_otp(username: str, otp: str, ttl_seconds: int) -> dict:
    """OTP for unlocking after idle-timeout lock."""
    text = (
        f"🔓 <b>Mã mở khoá (idle)</b>\n\n"
        f"User: <code>{username}</code>\n"
        f"OTP:  <code>{otp}</code>\n\n"
        f"Hết hạn sau {ttl_seconds // 60} phút."
    )
    return send_message(text)


def _html_escape(s) -> str:
    return (
        str(s if s is not None else "")
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def send_goal_alert(match, goals: list[dict], settings: dict) -> dict:
    """Goal-event Telegram alert. Body fields are gated by `settings.include_*`.

    Yêu cầu #7. Sender is the same `send_message` pipeline used by OTPs; chat
    targets come from `_chat_ids()` (DB-first with env fallback).
    """
    lines: list[str] = ["⚽️ <b>Cảnh báo trận đấu</b>"]
    if settings.get("include_match_name", True):
        home = _html_escape(getattr(match, "home", ""))
        away = _html_escape(getattr(match, "away", ""))
        score = f"{getattr(match, 'home_score', 0) or 0} - {getattr(match, 'away_score', 0) or 0}"
        lines.append(f"<b>{home} {score} {away}</b>")
    if settings.get("include_competition", True):
        lines.append(f"🏆 {_html_escape(getattr(match, 'competition', ''))}")

    minute = getattr(match, "minute", None)
    if minute is not None:
        lines.append(f"⏱ Phút {minute}")

    for n in (1, 2, 3, 4):
        if not settings.get(f"include_goal_{n}", n <= 3):
            continue
        g = next((x for x in goals if x.get("goal_number") == n), None)
        if not g:
            continue
        hc_b = _html_escape(g.get("hc_before") or "?")
        hc_a = _html_escape(g.get("hc_after") or "?")
        ou_b = _html_escape(g.get("ou_before") or "?")
        ou_a = _html_escape(g.get("ou_after") or "?")
        gmin = g.get("minute")
        team = g.get("team", "?")
        lines.append(
            f"• Bàn {n} ({team}, phút {gmin}): "
            f"HC {hc_b} → {hc_a} · OU {ou_b} → {ou_a}"
        )

    return send_message("\n".join(lines))
