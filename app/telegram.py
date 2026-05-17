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


def _chat_ids() -> list[str]:
    if not _CHAT_IDS_RAW:
        return []
    return [c.strip() for c in _CHAT_IDS_RAW.split(",") if c.strip()]


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
