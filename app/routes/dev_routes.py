"""Dev-only routes for testing without Telegram."""
import os

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, RedirectResponse

from app.auth import create_jwt, is_miniapp_allowed
from app.database import get_db
from app.models import EventUser, Player, StationHost
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/dev", tags=["dev"])


def is_dev():
    return os.getenv("ENV", "development") == "development"


@router.get("/login/{tg_id}/{event_id}")
async def dev_login(
    tg_id: int,
    event_id: int,
    role: str = "ADMIN",
    db: AsyncSession = Depends(get_db),
):
    """Dev-only: create session for tg_id. ?role=ADMIN|PLAYER|STATION_HOST"""
    if not is_dev():
        return JSONResponse({"error": "Not available"}, status_code=404)

    if role == "PLAYER" and not is_miniapp_allowed(tg_id):
        return RedirectResponse(url="/closed", status_code=303)

    extra = {}
    if role == "PLAYER":
        r = await db.execute(
            select(Player).where(Player.event_id == event_id, Player.tg_id == tg_id)
        )
        p = r.scalar_one_or_none()
        if p:
            extra["team_id"] = p.team_id
            extra["player_id"] = p.id
    elif role == "STATION_HOST":
        r = await db.execute(
            select(StationHost).where(
                StationHost.event_id == event_id, StationHost.tg_id == tg_id
            )
        )
        h = r.scalar_one_or_none()
        if h:
            extra["station_id"] = h.station_id

    token = create_jwt(tg_id, event_id, role, extra)
    resp = RedirectResponse(url="/player" if role == "PLAYER" else "/admin" if role in ("ADMIN", "SUPERADMIN") else "/station", status_code=303)
    resp.set_cookie("session", token, httponly=True, samesite="lax", max_age=86400)
    return resp
