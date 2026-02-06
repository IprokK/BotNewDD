"""Telegram initData verification and JWT session management."""
import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

from jose import JWTError, jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, OAuth2PasswordBearer

from config import settings


@dataclass
class UserContext:
    tg_id: int
    event_id: int
    role: str  # PLAYER, STATION_HOST, ADMIN, SUPERADMIN
    team_id: int | None = None
    player_id: int | None = None
    station_id: int | None = None


def verify_telegram_init_data(init_data: str) -> dict:
    """Verify Telegram WebApp initData via HMAC-SHA256."""
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing initData")

    if not settings.telegram_bot_token:
        # Dev mode: accept without verification
        try:
            parsed = dict(parse_qsl(init_data))
            if "user" in parsed:
                parsed["user"] = json.loads(parsed["user"])
            return parsed
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid initData")

    parsed = dict(parse_qsl(init_data))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Missing hash in initData")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(
        b"WebAppData",
        settings.telegram_bot_token.encode(),
        hashlib.sha256
    ).digest()
    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256
    ).hexdigest()

    if calculated_hash != received_hash:
        raise HTTPException(status_code=401, detail="Invalid initData signature")

    auth_date = int(parsed.get("auth_date", 0))
    if datetime.now(timezone.utc).timestamp() - auth_date > 86400:  # 24h
        raise HTTPException(status_code=401, detail="initData expired")

    if "user" in parsed:
        try:
            parsed["user"] = json.loads(parsed["user"])
        except json.JSONDecodeError:
            pass

    return parsed


def create_jwt(tg_id: int, event_id: int, role: str, extra: dict | None = None) -> str:
    payload = {
        "sub": str(tg_id),
        "event_id": event_id,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expire_hours),
        "iat": datetime.now(timezone.utc),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


# --- Dependency: get UserContext from request ---
# Uses either: Authorization: Bearer <jwt> or cookie "session"
async def get_user_from_session(request: Request) -> UserContext:
    token = None
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]
    if not token:
        token = request.cookies.get("session")

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    payload = decode_jwt(token)
    return UserContext(
        tg_id=int(payload["sub"]),
        event_id=payload["event_id"],
        role=payload["role"],
        team_id=payload.get("team_id"),
        player_id=payload.get("player_id"),
        station_id=payload.get("station_id"),
    )


def require_role(*allowed: str):
    """Dependency factory: user must have one of the allowed roles."""

    async def check(current_user: UserContext = Depends(get_user_from_session)) -> UserContext:
        if current_user.role not in allowed:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return current_user

    return check


require_player = require_role("PLAYER")
require_host = require_role("STATION_HOST")
require_admin = require_role("ADMIN", "SUPERADMIN")


def _allowed_tg_ids() -> set[int]:
    """Telegram IDs, которым разрешён доступ к mini-app. Пусто = всем."""
    s = (getattr(settings, "tg_allowed_ids", None) or "").strip()
    if not s:
        return set()
    return {int(x.strip()) for x in s.split(",") if x.strip().isdigit()}


def is_miniapp_allowed(tg_id: int) -> bool:
    """Проверка: разрешён ли доступ к mini-app для этого tg_id."""
    ids = _allowed_tg_ids()
    return len(ids) == 0 or tg_id in ids


async def require_miniapp_access(
    request: Request,
    current_user: UserContext = Depends(require_player),
):
    """Зависимость: доступ к mini-app только для TG_ALLOWED_IDS (если задано)."""
    if not is_miniapp_allowed(current_user.tg_id):
        from fastapi.templating import Jinja2Templates
        templates = Jinja2Templates(directory="app/templates")
        return templates.TemplateResponse("closed.html", {"request": request})
    return current_user
