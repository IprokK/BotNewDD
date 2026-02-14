"""Admin dashboard routes."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import UserContext, require_admin
from app.database import get_db
from app.models import (
    ContentBlock,
    ContentAudience,
    DialogueMessage,
    DialogueReply,
    DialogueStartConfig,
    DialogueThread,
    DialogueThreadUnlock,
    Event,
    EventLog,
    EventUser,
    PhotoItem,
    Player,
    Rating,
    RegistrationForm,
    ScanCode,
    Station,
    StationHost,
    StationVisit,
    Team,
    TeamChatMessage,
    TeamGroup,
    TeamState,
)
from config import settings as app_settings
from app.services import generate_qr_token, log_event, ws_manager
from app.notify import notify_player_assigned, notify_station_assigned, send_wave_message

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")


@router.get("", response_class=HTMLResponse)
async def admin_board(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    """Live Ops Board: teams by state."""
    r = await db.execute(
        select(Team, Station)
        .outerjoin(Station, Team.current_station_id == Station.id)
        .where(Team.event_id == user.event_id)
        .order_by(Team.name)
    )
    rows = r.all()

    by_state = {"free_roam": [], "assigned": [], "in_visit": [], "finished": []}
    for team, station in rows:
        state = team.current_state or "free_roam"
        by_state.setdefault(state, []).append((team, station))

    r = await db.execute(select(Station).where(Station.event_id == user.event_id))
    stations = r.scalars().all()

    return templates.TemplateResponse(
        "admin/board.html",
        {
            "request": request,
            "user": user,
            "by_state": by_state,
            "stations": stations,
        },
    )


@router.get("/teams", response_class=HTMLResponse)
async def admin_teams_partial(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    r = await db.execute(
        select(Team, Station)
        .outerjoin(Station, Team.current_station_id == Station.id)
        .where(Team.event_id == user.event_id)
    )
    rows = r.all()
    by_state = {"free_roam": [], "assigned": [], "in_visit": [], "finished": []}
    for team, station in rows:
        state = team.current_state or "free_roam"
        by_state.setdefault(state, []).append((team, station))
    return templates.TemplateResponse(
        "admin/partials/teams_columns.html",
        {"request": request, "by_state": by_state},
    )


class CreateTeamRequest(BaseModel):
    name: str


class AssignTeamRequest(BaseModel):
    station_id: int | None = None  # null = free roam
    state: str = "free_roam"


@router.post("/teams")
async def admin_create_team(
    request: Request,
    name: str = Form(...),
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    team = Team(
        event_id=user.event_id,
        name=name,
        current_state=TeamState.FREE_ROAM.value,
    )
    db.add(team)
    await db.flush()
    team.qr_token = generate_qr_token(user.event_id, team.id)
    await db.flush()
    await log_event(db, user.event_id, "team_created", {"team_id": team.id, "name": name})
    await ws_manager.broadcast_admin(user.event_id, "admin:team_update", {"team_id": team.id})
    return {"ok": True, "team_id": team.id, "qr_token": team.qr_token}


@router.patch("/teams/{team_id}")
async def admin_assign_team(
    team_id: int,
    req: AssignTeamRequest,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    r = await db.execute(select(Team).where(Team.id == team_id, Team.event_id == user.event_id))
    team = r.scalar_one_or_none()
    if not team:
        return {"ok": False, "error": "Team not found"}

    team.current_station_id = req.station_id
    team.current_state = req.state if req.state in ("free_roam", "assigned", "in_visit", "finished") else (TeamState.ASSIGNED.value if req.station_id else TeamState.FREE_ROAM.value)

    await log_event(
        db,
        user.event_id,
        "team_assigned",
        {"team_id": team_id, "station_id": req.station_id, "state": team.current_state},
        team_id=team_id,
    )
    await ws_manager.broadcast_team(team_id, "team:state", {"state": team.current_state, "station_id": req.station_id})
    await ws_manager.broadcast_admin(user.event_id, "admin:team_update", {"team_id": team_id})
    # Уведомление игроков о назначенной станции
    if req.station_id:
        r = await db.execute(select(Station).where(Station.id == req.station_id))
        station = r.scalar_one_or_none()
        if station:
            r = await db.execute(select(Player).where(Player.team_id == team_id))
            for p in r.scalars().all():
                import asyncio
                asyncio.create_task(notify_station_assigned(p.tg_id, station.name))
    return {"ok": True}


class AddPlayerRequest(BaseModel):
    team_id: int
    tg_id: int
    role: str  # ROLE_A, ROLE_B


@router.post("/teams/{team_id}/players")
async def admin_add_player(
    team_id: int,
    req: AddPlayerRequest,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    r = await db.execute(select(Team).where(Team.id == team_id, Team.event_id == user.event_id))
    team = r.scalar_one_or_none()
    if not team:
        return {"ok": False, "error": "Team not found"}

    r = await db.execute(
        select(Player).where(Player.event_id == user.event_id, Player.tg_id == req.tg_id)
    )
    existing = r.scalar_one_or_none()
    if existing:
        existing.team_id = team_id
        existing.role = req.role
        player = existing
    else:
        player = Player(
            event_id=user.event_id,
            tg_id=req.tg_id,
            team_id=team_id,
            role=req.role,
        )
        db.add(player)
    await db.flush()
    await ws_manager.broadcast_team(team_id, "team:state", {"state": team.current_state})
    await ws_manager.broadcast_admin(user.event_id, "admin:team_update", {"team_id": team_id})
    # Уведомление в Telegram
    import asyncio
    asyncio.create_task(notify_player_assigned(req.tg_id, team.name))
    return {"ok": True, "player_id": player.id}


class SetPlayerTeamRequest(BaseModel):
    team_id: int | None = None
    role: str = "ROLE_A"


@router.patch("/players/{player_id}/team")
async def admin_set_player_team(
    player_id: int,
    req: SetPlayerTeamRequest,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    """Убрать игрока из команды (team_id=null) или переместить в другую команду."""
    r = await db.execute(
        select(Player).where(Player.id == player_id, Player.event_id == user.event_id)
    )
    player = r.scalar_one_or_none()
    if not player:
        return {"ok": False, "error": "Player not found"}
    old_team_id = player.team_id
    if req.team_id is None:
        player.team_id = None
        player.role = None
    else:
        r = await db.execute(select(Team).where(Team.id == req.team_id, Team.event_id == user.event_id))
        team = r.scalar_one_or_none()
        if not team:
            return {"ok": False, "error": "Team not found"}
        player.team_id = req.team_id
        player.role = req.role if req.role in ("ROLE_A", "ROLE_B") else "ROLE_A"
    await db.flush()
    if player.team_id:
        import asyncio
        r = await db.execute(select(Team.name).where(Team.id == player.team_id))
        team_name = r.scalar() or ""
        asyncio.create_task(notify_player_assigned(player.tg_id, team_name))
    await ws_manager.broadcast_admin(user.event_id, "admin:team_update", {"team_id": old_team_id or player.team_id})
    return {"ok": True, "player_id": player.id, "team_id": player.team_id}


@router.post("/teams/assign")
async def admin_assign_player_form(
    tg_id: int = Form(...),
    team_id: int = Form(...),
    role: str = Form("ROLE_A"),
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    """Form: добавить игрока в команду (из страницы registrations)."""
    r = await db.execute(select(Team).where(Team.id == team_id, Team.event_id == user.event_id))
    team = r.scalar_one_or_none()
    if not team:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/admin/registrations?error=team", status_code=303)
    r = await db.execute(
        select(Player).where(Player.event_id == user.event_id, Player.tg_id == tg_id)
    )
    existing = r.scalar_one_or_none()
    if existing:
        existing.team_id = team_id
        existing.role = role
    else:
        player = Player(event_id=user.event_id, tg_id=tg_id, team_id=team_id, role=role)
        db.add(player)
    await db.flush()
    import asyncio
    asyncio.create_task(notify_player_assigned(tg_id, team.name))
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/admin/registrations", status_code=303)


@router.get("/station-hosts", response_class=HTMLResponse)
async def admin_station_hosts(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    """Ведущие станций — список и добавление."""
    r = await db.execute(
        select(StationHost, Station)
        .join(Station, StationHost.station_id == Station.id)
        .where(StationHost.event_id == user.event_id)
        .order_by(Station.name)
    )
    hosts = r.all()
    r = await db.execute(select(Station).where(Station.event_id == user.event_id).order_by(Station.name))
    stations = r.scalars().all()
    return templates.TemplateResponse(
        "admin/station_hosts.html",
        {"request": request, "user": user, "hosts": hosts, "stations": stations},
    )


@router.post("/station-hosts")
async def admin_add_station_host(
    tg_id: int = Form(...),
    station_id: int = Form(...),
    name: str = Form(""),
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    r = await db.execute(select(Station).where(Station.id == station_id, Station.event_id == user.event_id))
    station = r.scalar_one_or_none()
    if not station:
        return RedirectResponse(url="/admin/station-hosts?error=station", status_code=303)
    r = await db.execute(
        select(StationHost).where(
            StationHost.event_id == user.event_id,
            StationHost.tg_id == tg_id,
        )
    )
    existing = r.scalar_one_or_none()
    if existing:
        existing.station_id = station_id
        existing.name = (name or "").strip() or None
    else:
        host = StationHost(
            event_id=user.event_id,
            tg_id=tg_id,
            station_id=station_id,
            name=(name or "").strip() or None,
        )
        db.add(host)
    await db.commit()
    return RedirectResponse(url="/admin/station-hosts", status_code=303)


@router.post("/station-hosts/{host_id}/delete")
async def admin_delete_station_host(
    host_id: int,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    r = await db.execute(
        select(StationHost).where(
            StationHost.id == host_id,
            StationHost.event_id == user.event_id,
        )
    )
    host = r.scalar_one_or_none()
    if host:
        await db.delete(host)
        await db.commit()
    return RedirectResponse(url="/admin/station-hosts", status_code=303)


@router.get("/stations", response_class=HTMLResponse)
async def admin_stations_list(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    """Список станций — управление и информация."""
    from sqlalchemy import func
    r = await db.execute(
        select(Station)
        .where(Station.event_id == user.event_id)
        .order_by(Station.name)
    )
    stations = r.scalars().all()
    r = await db.execute(
        select(Station.id, func.count(StationVisit.id).label("visits_count"))
        .outerjoin(StationVisit, StationVisit.station_id == Station.id)
        .where(Station.event_id == user.event_id)
        .group_by(Station.id)
    )
    visits_by_station = {row[0]: row[1] for row in r.all()}
    r = await db.execute(
        select(Station.id, func.count(StationHost.id).label("hosts_count"))
        .outerjoin(StationHost, StationHost.station_id == Station.id)
        .where(Station.event_id == user.event_id)
        .group_by(Station.id)
    )
    hosts_by_station = {row[0]: row[1] for row in r.all()}
    return templates.TemplateResponse(
        "admin/stations.html",
        {
            "request": request,
            "user": user,
            "stations": stations,
            "visits_by_station": visits_by_station,
            "hosts_by_station": hosts_by_station,
        },
    )


@router.get("/stations/{station_id}", response_class=HTMLResponse)
async def admin_station_detail(
    request: Request,
    station_id: int,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    """Просмотр и редактирование станции."""
    r = await db.execute(
        select(Station).where(
            Station.id == station_id,
            Station.event_id == user.event_id,
        )
    )
    station = r.scalar_one_or_none()
    if not station:
        return RedirectResponse(url="/admin/stations", status_code=303)
    r = await db.execute(
        select(StationHost).where(StationHost.station_id == station_id)
    )
    hosts = r.scalars().all()
    # Визиты: очки по командам и состав (из логов visit_finished или текущий состав)
    r = await db.execute(
        select(StationVisit, Team)
        .join(Team, StationVisit.team_id == Team.id)
        .where(
            StationVisit.station_id == station_id,
            StationVisit.event_id == user.event_id,
            StationVisit.state == "finished",
        )
        .order_by(StationVisit.ended_at.desc().nullslast(), StationVisit.created_at.desc())
    )
    # Состав из логов visit_finished (visit_id -> [names])
    rlog = await db.execute(
        select(EventLog).where(
            EventLog.event_id == user.event_id,
            EventLog.event_type == "visit_finished",
        )
    )
    players_from_log: dict[int, list[str]] = {}
    for log in rlog.scalars().all():
        vid = (log.data or {}).get("visit_id")
        plist = (log.data or {}).get("players")
        if vid is not None and isinstance(plist, list):
            players_from_log[vid] = [str(p) for p in plist]

    visits_data = []
    for visit, team in r.all():
        players = players_from_log.get(visit.id)
        if players is None:
            rp = await db.execute(
                select(Player, RegistrationForm)
                .outerjoin(
                    RegistrationForm,
                    (RegistrationForm.event_id == Player.event_id) & (RegistrationForm.tg_id == Player.tg_id),
                )
                .where(Player.team_id == team.id, Player.event_id == user.event_id)
            )
            players = []
            for player, form in rp.all():
                name = (form.full_name if form else None) or f"tg:{player.tg_id}"
                players.append(name)
        visits_data.append({
            "visit": visit,
            "team": team,
            "points": visit.points_awarded,
            "players": players,
            "ended_at": visit.ended_at,
        })
    return templates.TemplateResponse(
        "admin/station_detail.html",
        {
            "request": request,
            "user": user,
            "station": station,
            "hosts": hosts,
            "visits_data": visits_data,
        },
    )


@router.post("/stations/{station_id}")
async def admin_update_station(
    station_id: int,
    name: str = Form(...),
    capacity: int = Form(2),
    description: str = Form(""),
    address: str = Form(""),
    instructions: str = Form(""),
    points_mode: str = Form("free"),
    points_options: str = Form("0,1,2,3,5"),
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    r = await db.execute(
        select(Station).where(
            Station.id == station_id,
            Station.event_id == user.event_id,
        )
    )
    station = r.scalar_one_or_none()
    if not station:
        return RedirectResponse(url="/admin/stations", status_code=303)
    station.name = name.strip()
    station.capacity = max(1, min(10, capacity))
    station.config = dict(station.config or {})
    station.config["description"] = (description or "").strip()
    station.config["address"] = (address or "").strip()
    station.config["instructions"] = (instructions or "").strip()
    station.config["points_mode"] = points_mode if points_mode in ("free", "select") else "free"
    station.config["points_options"] = points_options.strip() if points_options.strip() else "0,1,2,3,5"
    await db.commit()
    return RedirectResponse(url=f"/admin/stations/{station_id}", status_code=303)


@router.post("/stations/{station_id}/delete")
async def admin_delete_station(
    station_id: int,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    r = await db.execute(
        select(Station).where(
            Station.id == station_id,
            Station.event_id == user.event_id,
        )
    )
    station = r.scalar_one_or_none()
    if station:
        await db.delete(station)
        await db.commit()
    return RedirectResponse(url="/admin/stations", status_code=303)


@router.post("/stations")
async def admin_create_station(
    request: Request,
    name: str = Form(...),
    capacity: int = Form(1),
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    station = Station(event_id=user.event_id, name=name.strip(), capacity=max(1, capacity))
    db.add(station)
    await db.commit()
    if request.headers.get("HX-Request"):
        return {"ok": True, "station_id": station.id}
    return RedirectResponse(url="/admin/stations", status_code=303)


@router.get("/team-roster", response_class=HTMLResponse)
async def admin_team_roster(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    """Состав команд — drag-and-drop для удаления/замены игроков."""
    r = await db.execute(
        select(Team).where(Team.event_id == user.event_id).order_by(Team.name)
    )
    teams = r.scalars().all()
    r = await db.execute(
        select(Player, RegistrationForm)
        .outerjoin(
            RegistrationForm,
            (RegistrationForm.event_id == Player.event_id) & (RegistrationForm.tg_id == Player.tg_id),
        )
        .where(Player.event_id == user.event_id)
    )
    players_with_forms = r.all()
    by_team = {t.id: [] for t in teams}
    by_team[None] = []
    for player, form in players_with_forms:
        name = (form.full_name if form else None) or f"tg:{player.tg_id}"
        item = {"player": player, "name": name}
        if player.team_id:
            by_team.setdefault(player.team_id, []).append(item)
        else:
            by_team[None].append(item)
    return templates.TemplateResponse(
        "admin/team_roster.html",
        {"request": request, "user": user, "teams": teams, "by_team": by_team},
    )


@router.get("/registrations", response_class=HTMLResponse)
async def admin_registrations(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    """Заявки на участие (игроки без команды, с заполненной анкетой)."""
    r = await db.execute(
        select(Player, RegistrationForm)
        .join(
            RegistrationForm,
            (RegistrationForm.event_id == Player.event_id) & (RegistrationForm.tg_id == Player.tg_id),
        )
        .where(
            Player.event_id == user.event_id,
            Player.team_id.is_(None),
        )
    )
    pending_with_forms = r.all()
    r = await db.execute(
        select(RegistrationForm).where(RegistrationForm.event_id == user.event_id).order_by(RegistrationForm.created_at.desc())
    )
    all_forms = r.scalars().all()
    r = await db.execute(select(Team).where(Team.event_id == user.event_id))
    teams = r.scalars().all()
    return templates.TemplateResponse(
        "admin/registrations.html",
        {"request": request, "user": user, "pending_with_forms": pending_with_forms, "all_forms": all_forms, "teams": teams},
    )


@router.get("/registrations/{form_id}", response_class=HTMLResponse)
async def admin_registration_detail(
    request: Request,
    form_id: int,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    """Полный просмотр анкеты зарегистрировавшегося."""
    r = await db.execute(
        select(RegistrationForm, Player)
        .outerjoin(
            Player,
            (Player.event_id == RegistrationForm.event_id) & (Player.tg_id == RegistrationForm.tg_id),
        )
        .where(
            RegistrationForm.id == form_id,
            RegistrationForm.event_id == user.event_id,
        )
    )
    row = r.first()
    if not row:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/admin/registrations", status_code=303)
    form, player = row
    return templates.TemplateResponse(
        "admin/registration_detail.html",
        {"request": request, "user": user, "form": form, "player": player},
    )


@router.get("/registrations/{form_id}/photo")
async def admin_registration_photo(
    form_id: int,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    """Показать фото участника из анкеты (прокси с Telegram)."""
    from config import settings
    import httpx

    r = await db.execute(
        select(RegistrationForm).where(
            RegistrationForm.id == form_id,
            RegistrationForm.event_id == user.event_id,
        )
    )
    form = r.scalar_one_or_none()
    if not form or not form.photo_file_id:
        return Response(status_code=404)
    if not settings.telegram_bot_token:
        return Response(status_code=503, content="Bot token not configured")
    try:
        async with httpx.AsyncClient() as client:
            get_file = await client.get(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/getFile",
                params={"file_id": form.photo_file_id},
                timeout=10,
            )
            get_file.raise_for_status()
            data = get_file.json()
            if not data.get("ok"):
                return Response(status_code=404)
            file_path = data["result"]["file_path"]
            img_resp = await client.get(
                f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file_path}",
                timeout=15,
            )
            img_resp.raise_for_status()
            return Response(
                content=img_resp.content,
                media_type=img_resp.headers.get("content-type", "image/jpeg"),
            )
    except Exception:
        return Response(status_code=502)


@router.post("/registrations/{form_id}/cancel")
async def admin_registration_cancel(
    form_id: int,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    """Отменить анкету — участник сможет заполнить её заново."""
    from fastapi.responses import RedirectResponse

    from app.notify import notify_registration_cancelled

    r = await db.execute(
        select(RegistrationForm).where(
            RegistrationForm.id == form_id,
            RegistrationForm.event_id == user.event_id,
        )
    )
    form = r.scalar_one_or_none()
    if form:
        tg_id = form.tg_id
        await db.delete(form)
        await db.commit()
        await notify_registration_cancelled(tg_id)
    return RedirectResponse(url="/admin/registrations", status_code=303)


@router.get("/send-wave-message", response_class=HTMLResponse)
async def admin_send_wave_message_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    """Страница рассылки сообщения о выборе волны запуска."""
    # Все получатели: игроки + заявки (уникальные tg_id)
    r = await db.execute(
        select(Player.tg_id, Player.team_id, RegistrationForm.full_name)
        .outerjoin(
            RegistrationForm,
            (RegistrationForm.event_id == Player.event_id) & (RegistrationForm.tg_id == Player.tg_id),
        )
        .where(Player.event_id == user.event_id)
    )
    players = r.all()
    r = await db.execute(
        select(RegistrationForm.tg_id, RegistrationForm.full_name).where(
            RegistrationForm.event_id == user.event_id,
        )
    )
    forms_only = {row[0]: row[1] for row in r.all()}
    recipients = []
    seen = set()
    for tg_id, team_id, name in players:
        if tg_id and tg_id not in seen:
            seen.add(tg_id)
            recipients.append({"tg_id": tg_id, "name": name or forms_only.get(tg_id) or f"tg:{tg_id}", "team_id": team_id})
    for tg_id, name in forms_only.items():
        if tg_id not in seen:
            seen.add(tg_id)
            recipients.append({"tg_id": tg_id, "name": name, "team_id": None})
    recipients.sort(key=lambda x: (x["name"] or "").lower())

    # Участники по выбранному времени
    r = await db.execute(
        select(RegistrationForm.tg_id, RegistrationForm.full_name, RegistrationForm.wave_preference).where(
            RegistrationForm.event_id == user.event_id,
        )
    )
    wave_by_tg = {}
    for row in r.all():
        wave_by_tg[row[0]] = {"name": row[1], "wave": row[2]}
    r = await db.execute(
        select(Player.tg_id, Player.player_progress).where(Player.event_id == user.event_id)
    )
    for tg_id, prog in r.all():
        if not tg_id:
            continue
        wave = (prog or {}).get("wave_preference") if prog else None
        if tg_id not in wave_by_tg:
            wave_by_tg[tg_id] = {"name": forms_only.get(tg_id) or f"tg:{tg_id}", "wave": wave}
        elif not wave_by_tg[tg_id]["wave"] and wave:
            wave_by_tg[tg_id]["wave"] = wave
    by_wave = {"13:00": [], "15:00": [], "17:10": [], "В перерывах между парами": [], "no_choice": []}
    for tg_id, data in wave_by_tg.items():
        wave = data["wave"]
        name = data["name"]
        if wave in by_wave:
            by_wave[wave].append({"name": name})
        else:
            by_wave["no_choice"].append({"name": name})

    return templates.TemplateResponse(
        "admin/send_wave_message.html",
        {"request": request, "user": user, "recipients": recipients, "by_wave": by_wave},
    )


@router.post("/send-wave-message")
async def admin_send_wave_message(
    tg_id: int = Form(None),
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    import asyncio
    from fastapi.responses import RedirectResponse

    if tg_id:
        ok = await send_wave_message(tg_id)
        return RedirectResponse(url=f"/admin/send-wave-message?sent={'ok' if ok else 'err'}&tg_id={tg_id}", status_code=303)
    # Отправить всем
    r = await db.execute(
        select(Player.tg_id).where(Player.event_id == user.event_id).distinct()
    )
    player_ids = {row[0] for row in r.all()}
    r = await db.execute(
        select(RegistrationForm.tg_id).where(RegistrationForm.event_id == user.event_id).distinct()
    )
    form_ids = {row[0] for row in r.all()}
    all_ids = player_ids | form_ids
    sent = 0
    for uid in all_ids:
        if uid and await send_wave_message(uid):
            sent += 1
    return RedirectResponse(url=f"/admin/send-wave-message?sent_all={sent}&total={len(all_ids)}", status_code=303)


@router.get("/team-chats", response_class=HTMLResponse)
async def admin_team_chats_list(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    """Список команд с чатами — просмотр переписок напарников."""
    r = await db.execute(
        select(Team)
        .where(Team.event_id == user.event_id)
        .order_by(Team.name)
    )
    teams = r.scalars().all()
    r = await db.execute(
        select(Team.id, func.count(TeamChatMessage.id).label("cnt"))
        .outerjoin(TeamChatMessage, TeamChatMessage.team_id == Team.id)
        .where(Team.event_id == user.event_id)
        .group_by(Team.id)
    )
    msg_counts = {row[0]: row[1] for row in r.all()}
    return templates.TemplateResponse(
        "admin/team_chats.html",
        {"request": request, "user": user, "teams": teams, "msg_counts": msg_counts},
    )


@router.get("/team-chats/{team_id}", response_class=HTMLResponse)
async def admin_team_chat_detail(
    request: Request,
    team_id: int,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    """Просмотр переписки команды (напарников)."""
    r = await db.execute(
        select(Team).where(Team.id == team_id, Team.event_id == user.event_id)
    )
    team = r.scalar_one_or_none()
    if not team:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/admin/team-chats", status_code=303)
    r = await db.execute(
        select(TeamChatMessage, Player)
        .join(Player, TeamChatMessage.sender_player_id == Player.id)
        .where(TeamChatMessage.team_id == team_id)
        .order_by(TeamChatMessage.created_at.asc())
    )
    messages = r.all()
    player_names = {}
    for msg, p in messages:
        if p.id not in player_names:
            rf = await db.execute(
                select(RegistrationForm.full_name).where(
                    RegistrationForm.event_id == user.event_id,
                    RegistrationForm.tg_id == p.tg_id,
                )
            )
            row = rf.first()
            player_names[p.id] = row[0] if row else f"tg:{p.tg_id}"
    return templates.TemplateResponse(
        "admin/team_chat_detail.html",
        {"request": request, "user": user, "team": team, "messages": messages, "player_names": player_names},
    )


@router.get("/quest-control", response_class=HTMLResponse)
async def admin_quest_control_page(
    request: Request,
    team_id: int = 0,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    """Вмешательство в квест: придержать или досрочно показать сообщения для команды/игрока."""
    r = await db.execute(
        select(Team).where(Team.event_id == user.event_id).order_by(Team.name)
    )
    teams = r.scalars().all()
    r = await db.execute(
        select(DialogueThread)
        .options(selectinload(DialogueThread.messages))
        .where(DialogueThread.event_id == user.event_id)
    )
    threads = list(r.scalars().all())
    team = None
    overrides_by_thread = {}
    if team_id:
        r = await db.execute(select(Team).where(Team.id == team_id, Team.event_id == user.event_id))
        team = r.scalar_one_or_none()
        if team:
            overrides_by_thread = (team.team_progress or {}).get("dialogue_overrides") or {}
    return templates.TemplateResponse(
        "admin/quest_control.html",
        {
            "request": request,
            "user": user,
            "teams": teams,
            "threads": threads,
            "team": team,
            "overrides_by_thread": overrides_by_thread,
        },
    )


@router.post("/quest-control")
async def admin_quest_control_save(
    team_id: int = Form(...),
    thread_key: str = Form(...),
    hold_until: str = Form(""),
    force_reveal_ids: str = Form(""),
    clear_hold: bool = Form(False),
    clear_force: bool = Form(False),
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    from fastapi.responses import RedirectResponse
    import json
    r = await db.execute(select(Team).where(Team.id == team_id, Team.event_id == user.event_id))
    team = r.scalar_one_or_none()
    if not team:
        return RedirectResponse(url="/admin/quest-control", status_code=303)
    prog = dict(team.team_progress or {})
    overrides = prog.get("dialogue_overrides") or {}
    thr = overrides.get(thread_key) or {}
    if clear_hold:
        thr.pop("hold_until", None)
    elif hold_until and hold_until.strip():
        val = hold_until.strip()
        if len(val) == 16 and "T" in val and "+" not in val and "Z" not in val:
            val = val + ":00+00:00"
        thr["hold_until"] = val
    if clear_force:
        thr.pop("force_reveal_message_ids", None)
    elif force_reveal_ids.strip():
        try:
            ids = [int(x.strip()) for x in force_reveal_ids.replace(",", " ").split() if x.strip()]
            thr["force_reveal_message_ids"] = ids
        except ValueError:
            pass
    if thr:
        overrides[thread_key] = thr
    else:
        overrides.pop(thread_key, None)
    prog["dialogue_overrides"] = overrides
    team.team_progress = prog
    await db.flush()
    return RedirectResponse(url=f"/admin/quest-control?team_id={team_id}", status_code=303)


# --- Старты диалогов ---
from app.notify import notify_dialogue_unlocked


async def _unlock_and_notify(db, thread, team_ids, event_id):
    """Разблокировать диалог для команд и отправить уведомления."""
    for tid in team_ids:
        r = await db.execute(
            select(DialogueThreadUnlock).where(
                DialogueThreadUnlock.thread_id == thread.id,
                DialogueThreadUnlock.team_id == tid,
            )
        )
        if r.scalar_one_or_none():
            continue
        r2 = await db.execute(select(Player).where(Player.team_id == tid))
        for p in r2.scalars().all():
            if p.tg_id:
                await notify_dialogue_unlocked(p.tg_id, thread.title or thread.key)
        db.add(DialogueThreadUnlock(thread_id=thread.id, team_id=tid))
    await db.commit()


@router.get("/dialogue-starts", response_class=HTMLResponse)
async def admin_dialogue_starts(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    """Панель управления стартами диалогов."""
    r = await db.execute(
        select(DialogueThread).where(DialogueThread.event_id == user.event_id).order_by(DialogueThread.title)
    )
    threads = r.scalars().all()
    r = await db.execute(
        select(TeamGroup).where(TeamGroup.event_id == user.event_id)
    )
    team_groups = r.scalars().all()
    r = await db.execute(
        select(DialogueStartConfig, DialogueThread)
        .join(DialogueThread, DialogueStartConfig.thread_id == DialogueThread.id)
        .where(DialogueStartConfig.event_id == user.event_id)
        .order_by(DialogueStartConfig.order_index, DialogueStartConfig.id)
    )
    configs = r.all()
    r = await db.execute(select(Team).where(Team.event_id == user.event_id).order_by(Team.name))
    teams = r.scalars().all()
    return templates.TemplateResponse(
        "admin/dialogue_starts.html",
        {
            "request": request,
            "user": user,
            "threads": threads,
            "team_groups": team_groups,
            "configs": configs,
            "teams": teams,
        },
    )


@router.post("/team-groups")
async def admin_create_team_group(
    name: str = Form(...),
    team_ids: str = Form(""),  # comma-separated
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    import json
    ids = [int(x.strip()) for x in team_ids.split(",") if x.strip()]
    g = TeamGroup(event_id=user.event_id, name=name.strip(), team_ids=ids)
    db.add(g)
    await db.flush()
    return RedirectResponse(url="/admin/dialogue-starts", status_code=303)


@router.post("/dialogue-start-configs")
async def admin_create_dialogue_start_config(
    thread_id: int = Form(...),
    start_at: str = Form(""),
    target_type: str = Form("all"),
    target_team_ids: str = Form(""),
    target_group_id: int = Form(None),
    order_index: int = Form(0),
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    team_ids = [int(x.strip()) for x in target_team_ids.split(",") if x.strip()] if target_team_ids else []
    start_dt = None
    if start_at and start_at.strip():
        try:
            start_dt = datetime.fromisoformat(start_at.replace("Z", "+00:00"))
            if start_dt.tzinfo is None:
                tz_name = getattr(app_settings, "event_timezone", "Europe/Moscow")
                start_dt = start_dt.replace(tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)
        except Exception:
            pass
    cfg = DialogueStartConfig(
        event_id=user.event_id,
        thread_id=thread_id,
        start_at=start_dt,
        target_type=target_type,
        target_team_ids=team_ids if target_type == "teams" else [],
        target_group_id=target_group_id if target_type == "group" and target_group_id else None,
        order_index=order_index,
    )
    db.add(cfg)
    await db.flush()
    return RedirectResponse(url="/admin/dialogue-starts", status_code=303)


@router.post("/dialogue-starts/trigger/{config_id}")
async def admin_trigger_dialogue_start(
    config_id: int,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    r = await db.execute(
        select(DialogueStartConfig, DialogueThread)
        .join(DialogueThread, DialogueStartConfig.thread_id == DialogueThread.id)
        .where(
            DialogueStartConfig.id == config_id,
            DialogueStartConfig.event_id == user.event_id,
        )
    )
    row = r.first()
    if not row:
        return RedirectResponse(url="/admin/dialogue-starts?err=notfound", status_code=303)
    config, thread = row
    team_ids = []
    if config.target_type == "all":
        r2 = await db.execute(select(Team.id).where(Team.event_id == user.event_id))
        team_ids = [row[0] for row in r2.all()]
    elif config.target_type == "teams":
        team_ids = list(config.target_team_ids or [])
    elif config.target_type == "group" and config.target_group_id:
        r2 = await db.execute(
            select(TeamGroup.team_ids).where(
                TeamGroup.id == config.target_group_id,
                TeamGroup.event_id == user.event_id,
            )
        )
        rw = r2.first()
        team_ids = list(rw[0] or []) if rw else []
    await _unlock_and_notify(db, thread, team_ids, user.event_id)
    return RedirectResponse(url="/admin/dialogue-starts?triggered=ok", status_code=303)


@router.post("/dialogue-starts/delete-config/{config_id}")
async def admin_delete_dialogue_start_config(
    config_id: int,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    r = await db.execute(
        select(DialogueStartConfig).where(
            DialogueStartConfig.id == config_id,
            DialogueStartConfig.event_id == user.event_id,
        )
    )
    cfg = r.scalar_one_or_none()
    if cfg:
        await db.delete(cfg)
        await db.flush()
    return RedirectResponse(url="/admin/dialogue-starts", status_code=303)


@router.get("/qr-items", response_class=HTMLResponse)
async def admin_qr_items(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    """QR-коды для предметов — при сканировании в mini-app игрок получает предмет."""
    from app.item_definitions import ITEM_DEFINITIONS, OBTAINABLE_ITEM_KEYS
    r = await db.execute(select(ScanCode).where(ScanCode.event_id == user.event_id).order_by(ScanCode.created_at.desc()))
    scan_codes = r.scalars().all()
    r = await db.execute(select(PhotoItem.item_key).where(PhotoItem.event_id == user.event_id))
    photo_keys = [row[0] for row in r.all()]
    obtainable_keys = list(OBTAINABLE_ITEM_KEYS) + photo_keys
    item_defs_merged = dict(ITEM_DEFINITIONS)
    for k in photo_keys:
        item_defs_merged[k] = {"name": f"Фото ({k})", "icon": "🖼️", "desc": "Фотография с оборотной стороной"}
    return templates.TemplateResponse(
        "admin/qr_items.html",
        {"request": request, "user": user, "scan_codes": scan_codes, "item_defs": item_defs_merged, "obtainable_keys": obtainable_keys},
    )


@router.post("/qr-items")
async def admin_create_qr_item(
    item_key: str = Form(...),
    name: str = Form(""),
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    import secrets
    from app.item_definitions import OBTAINABLE_ITEM_KEYS
    r = await db.execute(select(PhotoItem.item_key).where(PhotoItem.event_id == user.event_id))
    photo_keys = [row[0] for row in r.all()]
    valid_keys = list(OBTAINABLE_ITEM_KEYS) + photo_keys
    if item_key not in valid_keys:
        return RedirectResponse(url="/admin/qr-items?error=invalid_item", status_code=303)
    code = f"q94_{secrets.token_hex(12)}"
    sc = ScanCode(event_id=user.event_id, code=code, item_key=item_key, name=(name or None))
    db.add(sc)
    await db.commit()
    return RedirectResponse(url="/admin/qr-items", status_code=303)


@router.post("/qr-items/{item_id}/delete")
async def admin_delete_qr_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    r = await db.execute(select(ScanCode).where(ScanCode.id == item_id, ScanCode.event_id == user.event_id))
    sc = r.scalar_one_or_none()
    if sc:
        await db.delete(sc)
        await db.commit()
    return RedirectResponse(url="/admin/qr-items", status_code=303)


@router.get("/photo-items", response_class=HTMLResponse)
async def admin_photo_items(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    r = await db.execute(select(PhotoItem).where(PhotoItem.event_id == user.event_id).order_by(PhotoItem.item_key))
    photo_items = r.scalars().all()
    return templates.TemplateResponse(
        "admin/photo_items.html",
        {"request": request, "user": user, "photo_items": photo_items},
    )


@router.post("/photo-items")
async def admin_create_photo_item(
    item_key: str = Form(...),
    back_signature: str = Form(""),
    back_date: str = Form(""),
    image: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    from fastapi.responses import RedirectResponse
    import uuid
    import os
    if not image.filename or not image.content_type or not image.content_type.startswith("image/"):
        return RedirectResponse(url="/admin/photo-items?error=need_image", status_code=303)
    ext = (image.filename or "").split(".")[-1] or "jpg"
    safe_ext = ext.lower() if ext.lower() in ("jpg", "jpeg", "png", "gif", "webp") else "jpg"
    fname = f"photo/{uuid.uuid4().hex}.{safe_ext}"
    path = f"uploads/{fname}"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = await image.read()
    with open(path, "wb") as f:
        f.write(content)
    pi = PhotoItem(
        event_id=user.event_id,
        item_key=(item_key or "").strip() or f"photo_{uuid.uuid4().hex[:8]}",
        image_url=f"/uploads/{fname}",
        back_signature=(back_signature or "").strip(),
        back_date=(back_date or "").strip(),
    )
    db.add(pi)
    await db.flush()
    return RedirectResponse(url="/admin/photo-items", status_code=303)


@router.post("/photo-items/{item_id}/delete")
async def admin_delete_photo_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    from fastapi.responses import RedirectResponse
    r = await db.execute(select(PhotoItem).where(PhotoItem.id == item_id, PhotoItem.event_id == user.event_id))
    pi = r.scalar_one_or_none()
    if pi:
        await db.delete(pi)
        await db.flush()
    return RedirectResponse(url="/admin/photo-items", status_code=303)


@router.post("/content")
async def admin_create_content(
    key: str = Form(...),
    type: str = Form("text"),
    text: str = Form(""),
    audience: str = Form("TEAM"),
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    from fastapi.responses import RedirectResponse
    block = ContentBlock(
        event_id=user.event_id,
        key=key,
        type=type,
        payload={"text": text},
        audience=audience,
    )
    db.add(block)
    await db.flush()
    return RedirectResponse(url="/admin/content", status_code=303)


@router.get("/content", response_class=HTMLResponse)
async def admin_content(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    r = await db.execute(select(ContentBlock).where(ContentBlock.event_id == user.event_id))
    blocks = r.scalars().all()
    return templates.TemplateResponse(
        "admin/content.html",
        {"request": request, "user": user, "blocks": blocks},
    )


@router.post("/dialogues")
async def admin_create_dialogue(
    key: str = Form(...),
    title: str = Form(""),
    type: str = Form("LEAKED"),
    target_roles: list[str] = Form([]),
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    from fastapi.responses import RedirectResponse
    dt = DialogueThread(event_id=user.event_id, key=key, title=title or key, type=type)
    valid = [r for r in target_roles if r in ("ROLE_A", "ROLE_B")]
    if valid:
        dt.config = dict(dt.config or {}, target_roles=valid)
    db.add(dt)
    await db.flush()
    return RedirectResponse(url=f"/admin/dialogues/{dt.id}", status_code=303)


@router.delete("/dialogues/{thread_id}")
async def admin_delete_dialogue(
    thread_id: int,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    from fastapi.responses import Response
    from sqlalchemy import delete
    from app.models import DialogueTransitionTrigger, DialogueScheduledDelivery

    r = await db.execute(
        select(DialogueThread)
        .options(selectinload(DialogueThread.messages))
        .where(
            DialogueThread.id == thread_id,
            DialogueThread.event_id == user.event_id,
        )
    )
    t = r.scalar_one_or_none()
    if not t:
        return Response(status_code=404)
    # Удаляем связанные записи вручную (избегаем FK-ошибок)
    msg_ids = [m.id for m in t.messages]
    await db.execute(delete(DialogueTransitionTrigger).where(DialogueTransitionTrigger.target_thread_id == thread_id))
    if msg_ids:
        await db.execute(delete(DialogueTransitionTrigger).where(DialogueTransitionTrigger.source_message_id.in_(msg_ids)))
        await db.execute(delete(DialogueScheduledDelivery).where(DialogueScheduledDelivery.message_id.in_(msg_ids)))
        await db.execute(delete(DialogueReply).where(DialogueReply.message_id.in_(msg_ids)))
    await db.execute(delete(DialogueStartConfig).where(DialogueStartConfig.thread_id == thread_id))
    await db.execute(delete(DialogueThreadUnlock).where(DialogueThreadUnlock.thread_id == thread_id))
    await db.delete(t)
    await db.flush()
    return Response(content="", status_code=200)


@router.get("/dialogues/{thread_id}/graph", response_class=HTMLResponse)
async def admin_dialogue_graph_page(
    request: Request,
    thread_id: int,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    """Visual graph editor for dialogue."""
    r = await db.execute(
        select(DialogueThread)
        .options(selectinload(DialogueThread.messages))
        .where(
            DialogueThread.id == thread_id,
            DialogueThread.event_id == user.event_id,
        )
    )
    thread = r.scalar_one_or_none()
    if not thread:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/admin/dialogues", status_code=303)
    r = await db.execute(select(Station).where(Station.event_id == user.event_id))
    stations = r.scalars().all()
    r = await db.execute(select(DialogueThread).where(DialogueThread.event_id == user.event_id, DialogueThread.id != thread_id))
    other_threads = [{"id": t.id, "key": t.key, "title": t.title or t.key} for t in r.scalars().all()]
    import json
    stations_json = json.dumps([{"id": s.id, "name": s.name} for s in stations])
    other_threads_json = json.dumps(other_threads, ensure_ascii=False)
    return templates.TemplateResponse(
        "admin/dialogue_graph.html",
        {"request": request, "user": user, "thread": thread, "stations": stations, "stations_json": stations_json, "other_threads_json": other_threads_json},
    )


@router.get("/dialogues/{thread_id}", response_class=HTMLResponse)
async def admin_dialogue_edit(
    request: Request,
    thread_id: int,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    r = await db.execute(
        select(DialogueThread)
        .options(selectinload(DialogueThread.messages))
        .where(
            DialogueThread.id == thread_id,
            DialogueThread.event_id == user.event_id,
        )
    )
    thread = r.scalar_one_or_none()
    if not thread:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/admin/dialogues", status_code=303)
    msgs = sorted(thread.messages, key=lambda m: (m.order_index if m.order_index is not None else 999999, m.id))
    r = await db.execute(select(Station).where(Station.event_id == user.event_id))
    stations = r.scalars().all()
    import json
    chars = (thread.config or {}).get("characters") or {}
    characters_json = json.dumps(chars, ensure_ascii=False, indent=2) if chars else "{}"
    return templates.TemplateResponse(
        "admin/dialogue_edit.html",
        {"request": request, "user": user, "thread": thread, "messages": msgs, "stations": stations, "characters_json": characters_json},
    )


@router.post("/dialogues/{thread_id}/characters")
async def admin_save_dialogue_characters(
    thread_id: int,
    characters_json: str = Form("{}"),
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    from fastapi.responses import RedirectResponse
    import json
    r = await db.execute(
        select(DialogueThread).where(
            DialogueThread.id == thread_id,
            DialogueThread.event_id == user.event_id,
        )
    )
    thread = r.scalar_one_or_none()
    if not thread:
        return RedirectResponse(url="/admin/dialogues", status_code=303)
    try:
        chars = json.loads(characters_json) if characters_json.strip() else {}
        if not isinstance(chars, dict):
            chars = {}
        cfg = dict(thread.config or {})
        cfg["characters"] = chars
        thread.config = cfg
        await db.flush()
    except Exception:
        pass
    return RedirectResponse(url=f"/admin/dialogues/{thread_id}", status_code=303)


@router.post("/dialogues/{thread_id}/target-roles")
async def admin_save_dialogue_target_roles(
    thread_id: int,
    target_roles: list[str] = Form([]),
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    from fastapi.responses import RedirectResponse
    r = await db.execute(
        select(DialogueThread).where(
            DialogueThread.id == thread_id,
            DialogueThread.event_id == user.event_id,
        )
    )
    thread = r.scalar_one_or_none()
    if not thread:
        return RedirectResponse(url="/admin/dialogues", status_code=303)
    valid = [r for r in target_roles if r in ("ROLE_A", "ROLE_B")]
    cfg = dict(thread.config or {})
    if len(valid) == 1:
        cfg["target_roles"] = valid
    else:
        cfg.pop("target_roles", None)
    thread.config = cfg
    await db.flush()
    return RedirectResponse(url=f"/admin/dialogues/{thread_id}", status_code=303)


@router.post("/dialogues/{thread_id}/messages")
async def admin_add_message(
    thread_id: int,
    audience: str = Form("TEAM"),
    text: str = Form(""),
    character: str = Form(""),
    condition_type: str = Form("immediate"),
    scheduled_at: str = Form(""),
    station_id: str = Form(""),
    after_message_id: str = Form(""),
    reply_options: str = Form(""),  # JSON: [{"text":"...", "next_message_id":N, "delay_seconds":N}]
    delay_after_previous: int = Form(0),
    delete_after_seconds: int = Form(0),
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    from fastapi.responses import RedirectResponse
    import json
    r = await db.execute(
        select(DialogueThread).where(
            DialogueThread.id == thread_id,
            DialogueThread.event_id == user.event_id,
        )
    )
    thread = r.scalar_one_or_none()
    if not thread:
        return RedirectResponse(url="/admin/dialogues", status_code=303)
    if not text and not (image and image.filename):
        return RedirectResponse(url=f"/admin/dialogues/{thread_id}?error=need_text_or_image", status_code=303)
    r = await db.execute(
        select(DialogueMessage.order_index)
        .where(DialogueMessage.thread_id == thread_id)
        .order_by(DialogueMessage.order_index.desc())
        .limit(1)
    )
    row = r.first()
    next_order = (row[0] + 1) if row else 0
    char = (character or "").strip()
    if char and not char.startswith("@"):
        char = "@" + char
    payload = {"text": text or "", "character": char}
    if image and image.filename and image.content_type and image.content_type.startswith("image/"):
        import uuid
        import os
        ext = (image.filename or "").split(".")[-1] or "jpg"
        safe_ext = ext.lower() if ext.lower() in ("jpg", "jpeg", "png", "gif", "webp") else "jpg"
        fname = f"dialogue/{uuid.uuid4().hex}.{safe_ext}"
        path = f"uploads/{fname}"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        content = await image.read()
        with open(path, "wb") as f:
            f.write(content)
        payload["image"] = f"/uploads/{fname}"
    if delete_after_seconds and delete_after_seconds > 0:
        payload["delete_after_seconds"] = delete_after_seconds
    if reply_options.strip():
        try:
            opts = json.loads(reply_options)
            payload["reply_options"] = [{"text": o.get("text", ""), "next_message_id": o.get("next_message_id"), "delay_seconds": int(o.get("delay_seconds") or 0)} for o in opts]
        except Exception:
            pass
    gate_rules = {"condition_type": condition_type}
    if delay_after_previous and delay_after_previous > 0:
        gate_rules["delay_after_previous_seconds"] = delay_after_previous
    if scheduled_at:
        gate_rules["scheduled_at"] = scheduled_at
    if station_id:
        gate_rules["station_id"] = int(station_id)
    if after_message_id:
        gate_rules["after_message_id"] = int(after_message_id)
    msg = DialogueMessage(
        event_id=user.event_id,
        thread_id=thread_id,
        audience=audience,
        payload=payload,
        order_index=next_order,
        gate_rules=gate_rules,
    )
    db.add(msg)
    await db.flush()
    return RedirectResponse(url=f"/admin/dialogues/{thread_id}", status_code=303)


@router.patch("/dialogues/messages/{msg_id}")
async def admin_update_message(
    msg_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    from fastapi.responses import RedirectResponse
    import json
    data = await request.json()
    r = await db.execute(
        select(DialogueMessage).join(DialogueThread).where(
            DialogueMessage.id == msg_id,
            DialogueThread.event_id == user.event_id,
        )
    )
    msg = r.scalar_one_or_none()
    if not msg:
        return {"ok": False}
    if "audience" in data:
        msg.audience = data["audience"]
    if "text" in data:
        msg.payload = msg.payload or {}
        msg.payload["text"] = data["text"]
    if "character" in data:
        msg.payload = msg.payload or {}
        msg.payload["character"] = data["character"]
    if "reply_options" in data:
        msg.payload = msg.payload or {}
        msg.payload["reply_options"] = data["reply_options"]
    if "gate_rules" in data:
        msg.gate_rules = data["gate_rules"]
    await db.flush()
    return {"ok": True}


@router.get("/dialogues/{thread_id}/graph/data")
async def admin_dialogue_graph_data(
    thread_id: int,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    """Return dialogue as graph (nodes + edges) for visual editor."""
    import json
    r = await db.execute(
        select(DialogueThread)
        .options(selectinload(DialogueThread.messages))
        .where(
            DialogueThread.id == thread_id,
            DialogueThread.event_id == user.event_id,
        )
    )
    thread = r.scalar_one_or_none()
    if not thread:
        return {"nodes": [], "stations": []}
    r = await db.execute(select(Station).where(Station.event_id == user.event_id))
    stations = [{"id": s.id, "name": s.name} for s in r.scalars().all()]
    nodes = []
    for m in sorted(thread.messages, key=lambda x: (x.order_index if x.order_index is not None else 999999, x.id)):
        opts = (m.payload or {}).get("reply_options") or []
        p = m.payload or {}
        x = p.get("pos_x") if p.get("pos_x") is not None else 50 + (m.order_index % 4) * 220
        y = p.get("pos_y") if p.get("pos_y") is not None else 50 + (m.order_index // 4) * 180
        tr = (m.payload or {}).get("trigger_dialogue") or {}
        nodes.append({
            "id": m.id,
            "x": x,
            "y": y,
            "text": (m.payload or {}).get("text", ""),
            "character": (m.payload or {}).get("character", ""),
            "audience": m.audience or "TEAM",
            "gate_rules": m.gate_rules or {"condition_type": "immediate"},
            "order_index": m.order_index,
            "reply_options": [{"text": o.get("text", ""), "next_id": o.get("next_message_id"), "delay_seconds": o.get("delay_seconds", 0)} for o in opts],
            "trigger_dialogue": tr,
        })
    r = await db.execute(select(DialogueThread).where(DialogueThread.event_id == user.event_id, DialogueThread.id != thread_id))
    other_threads = [{"id": t.id, "key": t.key, "title": t.title or t.key} for t in r.scalars().all()]
    return {"nodes": nodes, "stations": stations, "thread_key": thread.key, "thread_title": thread.title or thread.key, "other_threads": other_threads}


@router.post("/dialogues/{thread_id}/graph")
async def admin_dialogue_graph_save(
    thread_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    """Save graph: create/update/delete messages to match payload."""
    import json
    r = await db.execute(
        select(DialogueThread)
        .options(selectinload(DialogueThread.messages))
        .where(
            DialogueThread.id == thread_id,
            DialogueThread.event_id == user.event_id,
        )
    )
    thread = r.scalar_one_or_none()
    if not thread:
        return {"ok": False, "error": "Thread not found"}
    data = await request.json()
    nodes_data = data.get("nodes", [])
    id_map = {}  # client_id -> db_id
    msgs_by_id = {m.id: m for m in thread.messages}

    for i, n in enumerate(nodes_data):
        nid = n.get("id")
        payload = {
            "text": n.get("text", ""),
            "character": n.get("character", ""),
        }
        if n.get("x") is not None:
            payload["pos_x"] = int(n.get("x", 0))
        if n.get("y") is not None:
            payload["pos_y"] = int(n.get("y", 0))
        tr = n.get("trigger_dialogue") or {}
        if tr.get("thread_key") or tr.get("thread_id"):
            payload["trigger_dialogue"] = {
                "thread_key": str(tr.get("thread_key") or tr.get("thread_id") or ""),
                "delay_minutes": int(tr.get("delay_minutes", 0)),
            }
        opts = n.get("reply_options") or []
        payload["reply_options"] = [
            {"text": o.get("text", ""), "next_message_id": o.get("next_id"), "delay_seconds": int(o.get("delay_seconds") or 0)}
            for o in opts
        ]
        gr = dict(n.get("gate_rules") or {"condition_type": "immediate"})
        if gr.get("station_id") is not None:
            gr["station_id"] = int(gr["station_id"]) if gr["station_id"] else None
        if gr.get("after_message_id") is not None:
            gr["after_message_id"] = int(gr["after_message_id"]) if gr["after_message_id"] else None

        is_new = nid is None or (isinstance(nid, str) and str(nid).startswith("new")) or (isinstance(nid, int) and nid not in msgs_by_id)
        if is_new:
            msg = DialogueMessage(
                event_id=user.event_id,
                thread_id=thread_id,
                audience=n.get("audience", "TEAM"),
                payload=payload,
                order_index=i,
                gate_rules=gr,
            )
            db.add(msg)
            await db.flush()
            id_map[nid] = msg.id
            msgs_by_id[msg.id] = msg
        else:
            mid = int(nid)
            msg = msgs_by_id.get(mid)
            if msg:
                id_map[mid] = mid
                msg.audience = n.get("audience", "TEAM")
                msg.payload = payload
                msg.gate_rules = gr
                msg.order_index = i

    def resolve_id(x):
        if x is None:
            return None
        if isinstance(x, str) and x in id_map:
            return id_map[x]
        if isinstance(x, (int, float)):
            xi = int(x)
            return id_map.get(xi) or id_map.get(x)
        return None

    for m in msgs_by_id.values():
        payload = dict(m.payload or {})
        opts = list(payload.get("reply_options") or [])
        for o in opts:
            nid = o.get("next_message_id")
            resolved = resolve_id(nid)
            if resolved is not None or (nid is not None and isinstance(nid, str) and nid.startswith("new")):
                o["next_message_id"] = resolved
        payload["reply_options"] = opts
        m.payload = payload
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(m, "payload")

    keep_ids = set()
    for n in nodes_data:
        nid = n.get("id")
        if nid in id_map:
            keep_ids.add(id_map[nid])
        elif isinstance(nid, (int, float)):
            keep_ids.add(int(nid))

    for mid, m in list(msgs_by_id.items()):
        if mid not in keep_ids:
            await db.delete(m)
    await db.flush()
    return {"ok": True}


@router.delete("/dialogues/messages/{msg_id}")
async def admin_delete_message(
    msg_id: int,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    r = await db.execute(
        select(DialogueMessage).join(DialogueThread).where(
            DialogueMessage.id == msg_id,
            DialogueThread.event_id == user.event_id,
        )
    )
    msg = r.scalar_one_or_none()
    if not msg:
        from fastapi.responses import Response
        return Response(status_code=404)
    await db.delete(msg)
    await db.flush()
    return {"ok": True}


@router.post("/dialogues/messages/reorder")
async def admin_reorder_messages(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    from fastapi import Body
    data = await request.json()
    ids = data.get("ids", [])
    for i, mid in enumerate(ids):
        r = await db.execute(
            select(DialogueMessage).join(DialogueThread).where(
                DialogueMessage.id == int(mid),
                DialogueThread.event_id == user.event_id,
            )
        )
        m = r.scalar_one_or_none()
        if m:
            m.order_index = i
    await db.flush()
    return {"ok": True}


@router.get("/dialogues", response_class=HTMLResponse)
async def admin_dialogues(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    r = await db.execute(
        select(DialogueThread).options(selectinload(DialogueThread.messages)).where(
            DialogueThread.event_id == user.event_id
        )
    )
    threads = r.scalars().all()
    return templates.TemplateResponse(
        "admin/dialogues.html",
        {"request": request, "user": user, "threads": threads},
    )


@router.get("/analytics", response_class=HTMLResponse)
async def admin_analytics(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    r = await db.execute(
        select(Station.name, func.avg(Rating.station_rating), func.avg(Rating.host_rating), func.count(Rating.id))
        .join(StationVisit, Rating.station_visit_id == StationVisit.id)
        .join(Station, StationVisit.station_id == Station.id)
        .where(Station.event_id == user.event_id)
        .group_by(Station.id, Station.name)
    )
    station_stats = r.all()

    r = await db.execute(
        select(Team.name, Team.score_total)
        .where(Team.event_id == user.event_id)
        .order_by(Team.score_total.desc())
        .limit(20)
    )
    leaderboard = r.all()

    return templates.TemplateResponse(
        "admin/analytics.html",
        {
            "request": request,
            "user": user,
            "station_stats": station_stats,
            "leaderboard": leaderboard,
        },
    )


@router.get("/log", response_class=HTMLResponse)
async def admin_log(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    r = await db.execute(
        select(EventLog).where(EventLog.event_id == user.event_id).order_by(EventLog.created_at.desc()).limit(100)
    )
    logs = r.scalars().all()
    return templates.TemplateResponse(
        "admin/log.html",
        {"request": request, "user": user, "logs": logs},
    )
