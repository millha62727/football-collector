import logging
import os
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Cookie, HTTPException

from .database import verify_user

log = logging.getLogger(__name__)

# --- Key & config -----------------------------------------------------------
# SECRET_KEY policy:
#   - Production (APP_ENV=production / prod): MUST be set, otherwise abort.
#     A random fallback would invalidate every JWT on every process restart,
#     silently logging users out and breaking the OTP flow under load.
#   - Dev (default): warn loudly + auto-generate so first-time `docker compose
#     up` still works, but make it obvious in the logs.
_APP_ENV = (os.getenv("APP_ENV") or "dev").strip().lower()
_SECRET_FROM_ENV = (os.getenv("SECRET_KEY") or "").strip()

if not _SECRET_FROM_ENV:
    if _APP_ENV in ("prod", "production"):
        raise RuntimeError(
            "SECRET_KEY is not set. Refusing to start in APP_ENV=%s with a "
            "random fallback because every process restart would invalidate "
            "all issued JWTs. Set SECRET_KEY in the .env file (e.g. "
            "`python -c 'import secrets; print(secrets.token_hex(32))'`)."
            % _APP_ENV
        )
    log.warning(
        "SECRET_KEY not set — generating an ephemeral one (APP_ENV=%s). "
        "All issued JWTs will be invalidated on the next process restart. "
        "Set SECRET_KEY in .env to make sessions persist across reloads.",
        _APP_ENV,
    )
    _SECRET = secrets.token_hex(32)
else:
    if len(_SECRET_FROM_ENV) < 32:
        log.warning(
            "SECRET_KEY is shorter than 32 chars (len=%d). HS256 expects at "
            "least 256 bits of entropy. Consider regenerating with "
            "`python -c 'import secrets; print(secrets.token_hex(32))'`.",
            len(_SECRET_FROM_ENV),
        )
    _SECRET = _SECRET_FROM_ENV

_ALGO = "HS256"
_EXPIRE_H = int(os.getenv("TOKEN_EXPIRE_HOURS", "24"))

# --- Rate limiting: max 5 login attempts per IP per 60 s -------------------
_attempts: dict[str, list[float]] = defaultdict(list)


def check_rate_limit(ip: str) -> bool:
    now = time.time()
    _attempts[ip] = [t for t in _attempts[ip] if now - t < 60]
    if len(_attempts[ip]) >= 5:
        return False
    _attempts[ip].append(now)
    return True


# --- Credential verification ------------------------------------------------
verify_credentials = verify_user


# --- JWT helpers ------------------------------------------------------------
def create_token(username: str) -> str:
    payload = {
        "sub": username,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(hours=_EXPIRE_H),
    }
    return jwt.encode(payload, _SECRET, algorithm=_ALGO)


def decode_token(token: str) -> Optional[str]:
    try:
        return jwt.decode(token, _SECRET, algorithms=[_ALGO]).get("sub")
    except jwt.PyJWTError:
        return None


# --- FastAPI dependency -----------------------------------------------------
async def require_auth(auth_token: Optional[str] = Cookie(default=None)) -> str:
    user = decode_token(auth_token) if auth_token else None
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user
