import os
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Cookie, HTTPException

from .database import verify_user

# --- Key & config -----------------------------------------------------------
_SECRET = os.getenv("SECRET_KEY") or secrets.token_hex(32)
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
