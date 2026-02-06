"""Auth routes: verify initData, JWT, logout."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from app.auth import create_jwt, get_user_from_session, is_miniapp_allowed, verify_telegram_init_data
from app.database import get_db
from app.models import EventUser, Player, StationHost
from app.services import resolve_user_role
from config import settings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/auth", tags=["auth"])


class VerifyRequest(BaseModel):
    init_data: str
    event_id: int


@router.post("/verify")
async def verify(
    req: VerifyRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Verify Telegram initData and create JWT session."""
    data = verify_telegram_init_data(req.init_data)
    user_data = data.get("user")
    if not user_data:
        return {"error": "No user in initData"}

    tg_id = user_data.get("id")
    if not tg_id:
        return {"error": "Invalid user"}

    # Resolve role for event
    event_user = await resolve_user_role(db, tg_id, req.event_id)
    if not event_user:
        # Check if player
        r = await db.execute(
            select(Player).where(Player.event_id == req.event_id, Player.tg_id == tg_id)
        )
        player = r.scalar_one_or_none()
        if player:
            role = "PLAYER"
            team_id = player.team_id
            player_id = player.id
            station_id = None
        else:
            # Check station host
            r = await db.execute(
                select(StationHost).where(
                    StationHost.event_id == req.event_id, StationHost.tg_id == tg_id
                )
            )
            host = r.scalar_one_or_none()
            if host:
                role = "STATION_HOST"
                team_id = None
                player_id = None
                station_id = host.station_id
            else:
                return {"error": "Not registered for this event", "tg_id": tg_id}
    else:
        role = event_user.role
        team_id = None
        player_id = None
        station_id = event_user.station_id
        if role == "PLAYER":
            r = await db.execute(
                select(Player).where(Player.event_id == req.event_id, Player.tg_id == tg_id)
            )
            p = r.scalar_one_or_none()
            if p:
                team_id = p.team_id
                player_id = p.id

    # Ограничение доступа к mini-app: PLAYER вне whitelist — не выдаём сессию, редирект на /closed
    if role == "PLAYER" and not is_miniapp_allowed(tg_id):
        if request.headers.get("Accept", "").startswith("application/json") or "application/json" in request.headers.get("Content-Type", ""):
            return JSONResponse({"error": "closed", "redirect": "/closed"})
        return RedirectResponse(url="/closed", status_code=303)

    extra = {}
    if team_id:
        extra["team_id"] = team_id
    if player_id:
        extra["player_id"] = player_id
    if station_id:
        extra["station_id"] = station_id

    token = create_jwt(tg_id, req.event_id, role, extra)

    # JSON/API: return token + redirect, set cookie
    if request.headers.get("Accept", "").startswith("application/json") or "application/json" in request.headers.get("Content-Type", ""):
        resp = JSONResponse({"token": token, "redirect": "/player"})
        resp.set_cookie("session", token, httponly=True, samesite="lax", max_age=86400)
        return resp
    response = RedirectResponse(url="/player", status_code=303)
    response.set_cookie("session", token, httponly=True, samesite="lax", max_age=86400)
    return response


@router.get("/me")
async def me(current_user=Depends(get_user_from_session)):
    return {
        "tg_id": current_user.tg_id,
        "event_id": current_user.event_id,
        "role": current_user.role,
        "team_id": current_user.team_id,
        "player_id": current_user.player_id,
        "station_id": current_user.station_id,
    }


@router.post("/logout")
async def logout(request: Request):
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("session")
    return response
