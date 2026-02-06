"""Station Host UI routes."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from datetime import datetime, timezone

from app.auth import UserContext, get_user_from_session, require_host
from app.database import get_db
from app.models import EventLog, Station, StationHost, StationVisit, Team, VisitState
from app.services import log_event, parse_qr_token, ws_manager
from app.notify import notify_visit_finished

router = APIRouter(tags=["station"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/station", response_class=HTMLResponse)
async def station_ui(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_host),
):
    """Station Host UI: scan QR, team info, start/finish, points."""
    r = await db.execute(
        select(Station).where(Station.id == user.station_id)
    )
    station = r.scalar_one_or_none()
    if not station:
        return templates.TemplateResponse(
            "station/error.html",
            {"request": request, "message": "Станция не найдена"},
            status_code=404,
        )
    return templates.TemplateResponse(
        "station/index.html",
        {"request": request, "user": user, "station": station},
    )


class ScanRequest(BaseModel):
    token: str


@router.post("/station/scan")
async def station_scan(
    req: ScanRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_host),
):
    """Validate QR token and return team info."""
    parsed = parse_qr_token(req.token)
    if not parsed or parsed[0] != user.event_id:
        return {"ok": False, "error": "Неверный QR-код"}

    event_id, team_id = parsed

    r = await db.execute(
        select(Team)
        .options(selectinload(Team.players))
        .where(Team.id == team_id, Team.event_id == event_id)
    )
    team = r.scalar_one_or_none()
    if not team:
        return {"ok": False, "error": "Команда не найдена"}

    if team.qr_token != req.token:
        return {"ok": False, "error": "Недействительный токен"}

    # Check for active visit
    r = await db.execute(
        select(StationVisit).where(
            StationVisit.team_id == team_id,
            StationVisit.station_id == user.station_id,
            StationVisit.state != VisitState.FINISHED.value,
        )
    )
    active_visit = r.scalar_one_or_none()

    return {
        "ok": True,
        "team": {
            "id": team.id,
            "name": team.name,
            "score_total": team.score_total,
            "players": [{"role": p.role, "tg_id": p.tg_id} for p in team.players],
        },
        "visit_id": active_visit.id if active_visit else None,
        "visit_state": active_visit.state if active_visit else None,
    }


class VisitStartRequest(BaseModel):
    team_id: int


class VisitFinishRequest(BaseModel):
    team_id: int
    points_awarded: int = 0
    host_rating: int | None = None
    host_notes: str | None = None


@router.post("/station/visit/start")
async def visit_start(
    req: VisitStartRequest,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_host),
):
    r = await db.execute(
        select(StationVisit).where(
            StationVisit.team_id == req.team_id,
            StationVisit.station_id == user.station_id,
            StationVisit.event_id == user.event_id,
        )
    )
    visit = r.scalar_one_or_none()
    if visit:
        if visit.state == VisitState.STARTED.value:
            return {"ok": True, "visit_id": visit.id}
        visit.state = VisitState.STARTED.value
        visit.started_at = datetime.now(timezone.utc)
    else:
        visit = StationVisit(
            event_id=user.event_id,
            team_id=req.team_id,
            station_id=user.station_id,
            state=VisitState.STARTED.value,
            started_at=datetime.now(timezone.utc),
        )
        db.add(visit)
    await db.flush()

    # Update team state
    r = await db.execute(select(Team).where(Team.id == req.team_id))
    team = r.scalar_one()
    team.current_state = "in_visit"
    team.current_station_id = user.station_id

    await log_event(db, user.event_id, "visit_started", {"visit_id": visit.id, "team_id": req.team_id}, team_id=req.team_id)
    await db.commit()

    await ws_manager.broadcast_team(req.team_id, "visit:started", {"visit_id": visit.id})
    await ws_manager.broadcast_admin(user.event_id, "admin:visit_update", {"visit_id": visit.id})

    return {"ok": True, "visit_id": visit.id}


@router.post("/station/visit/finish")
async def visit_finish(
    req: VisitFinishRequest,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_host),
):
    r = await db.execute(
        select(StationVisit).where(
            StationVisit.team_id == req.team_id,
            StationVisit.station_id == user.station_id,
            StationVisit.event_id == user.event_id,
        )
    )
    visit = r.scalar_one_or_none()
    if not visit:
        return {"ok": False, "error": "Визит не найден"}

    visit.state = VisitState.FINISHED.value
    visit.ended_at = datetime.now(timezone.utc)
    visit.points_awarded = req.points_awarded
    visit.host_rating = req.host_rating
    visit.host_notes = req.host_notes

    r = await db.execute(select(Team).where(Team.id == req.team_id))
    team = r.scalar_one()
    team.current_state = "free_roam"
    team.current_station_id = None
    team.score_total += req.points_awarded

    await log_event(
        db,
        user.event_id,
        "visit_finished",
        {
            "visit_id": visit.id,
            "team_id": req.team_id,
            "points": req.points_awarded,
        },
        team_id=req.team_id,
    )
    await db.commit()

    await ws_manager.broadcast_team(
        req.team_id,
        "visit:finished",
        {"visit_id": visit.id, "points": req.points_awarded},
    )
    await ws_manager.broadcast_admin(user.event_id, "admin:visit_update", {"visit_id": visit.id})

    # Уведомление игроков в Telegram
    r = await db.execute(select(Station).where(Station.id == user.station_id))
    station = r.scalar_one_or_none()
    if station:
        r = await db.execute(select(Player).where(Player.team_id == req.team_id))
        for p in r.scalars().all():
            import asyncio
            asyncio.create_task(notify_visit_finished(p.tg_id, station.name, req.points_awarded))

    return {"ok": True, "visit_id": visit.id}
