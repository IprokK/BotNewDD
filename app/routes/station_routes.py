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
from app.models import EventLog, Player, RegistrationForm, Station, StationHost, StationVisit, Team, VisitState
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
    """Station Host UI: выбор станции, сканер QR, оценка. Ведущие могут подменять друг друга."""
    r = await db.execute(
        select(StationHost).where(
            StationHost.event_id == user.event_id,
            StationHost.tg_id == user.tg_id,
        )
    )
    host_record = r.scalar_one_or_none()
    host_name = host_record.name if host_record and host_record.name else f"tg:{user.tg_id}"
    r = await db.execute(
        select(Station).where(Station.event_id == user.event_id).order_by(Station.name)
    )
    stations = r.scalars().all()
    default_station = next((s for s in stations if s.id == user.station_id), stations[0] if stations else None)
    if not stations:
        return templates.TemplateResponse(
            "station/error.html",
            {"request": request, "message": "Нет станций в мероприятии"},
            status_code=404,
        )
    stations_data = [
        {
            "id": s.id,
            "name": s.name,
            "config": s.config or {},
        }
        for s in stations
    ]
    return templates.TemplateResponse(
        "station/index.html",
        {
            "request": request,
            "user": user,
            "host_name": host_name,
            "stations": stations,
            "stations_data": stations_data,
            "default_station": default_station,
        },
    )


class ScanRequest(BaseModel):
    token: str
    station_id: int | None = None


async def _resolve_station_id(user: UserContext, station_id: int | None, db: AsyncSession, event_id: int) -> int | None:
    """Вернуть station_id: из запроса или из сессии. Проверить, что станция принадлежит event."""
    sid = station_id or user.station_id
    if not sid:
        return None
    r = await db.execute(select(Station).where(Station.id == sid, Station.event_id == event_id))
    return sid if r.scalar_one_or_none() else None


@router.post("/station/scan")
async def station_scan(
    req: ScanRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_host),
):
    """Validate QR token and return team info."""
    station_id = await _resolve_station_id(user, req.station_id, db, user.event_id)
    if not station_id:
        return {"ok": False, "error": "Выберите станцию"}
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

    r = await db.execute(
        select(StationVisit).where(
            StationVisit.team_id == team_id,
            StationVisit.station_id == station_id,
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
    station_id: int | None = None


class VisitFinishRequest(BaseModel):
    team_id: int
    station_id: int | None = None
    points_awarded: float = 0
    host_rating: int | None = None
    host_notes: str | None = None


@router.post("/station/visit/start")
async def visit_start(
    req: VisitStartRequest,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_host),
):
    station_id = await _resolve_station_id(user, req.station_id, db, user.event_id)
    if not station_id:
        return {"ok": False, "error": "Выберите станцию"}
    r = await db.execute(
        select(StationVisit).where(
            StationVisit.team_id == req.team_id,
            StationVisit.station_id == station_id,
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
            station_id=station_id,
            state=VisitState.STARTED.value,
            started_at=datetime.now(timezone.utc),
        )
        db.add(visit)
    await db.flush()

    # Update team state
    r = await db.execute(select(Team).where(Team.id == req.team_id))
    team = r.scalar_one()
    team.current_state = "in_visit"
    team.current_station_id = station_id

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
    station_id = await _resolve_station_id(user, req.station_id, db, user.event_id)
    if not station_id:
        return {"ok": False, "error": "Выберите станцию"}
    r = await db.execute(
        select(StationVisit).where(
            StationVisit.team_id == req.team_id,
            StationVisit.station_id == station_id,
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

    # Состав команды на момент завершения визита (для отображения в админке)
    rp = await db.execute(
        select(Player, RegistrationForm)
        .outerjoin(
            RegistrationForm,
            (RegistrationForm.event_id == Player.event_id) & (RegistrationForm.tg_id == Player.tg_id),
        )
        .where(Player.team_id == req.team_id, Player.event_id == user.event_id)
    )
    players_snapshot = []
    for p, form in rp.all():
        name = (form.full_name if form else None) or f"tg:{p.tg_id}"
        players_snapshot.append(name)

    await log_event(
        db,
        user.event_id,
        "visit_finished",
        {
            "visit_id": visit.id,
            "team_id": req.team_id,
            "points": req.points_awarded,
            "players": players_snapshot,
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
    r = await db.execute(select(Station).where(Station.id == station_id))
    station = r.scalar_one_or_none()
    if station:
        r = await db.execute(select(Player).where(Player.team_id == req.team_id))
        for p in r.scalars().all():
            import asyncio
            asyncio.create_task(notify_visit_finished(p.tg_id, station.name, req.points_awarded))

    return {"ok": True, "visit_id": visit.id}
