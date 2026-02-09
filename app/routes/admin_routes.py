"""Admin dashboard routes."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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
    DialogueThread,
    Event,
    EventLog,
    EventUser,
    Player,
    Rating,
    RegistrationForm,
    ScanCode,
    Station,
    StationHost,
    StationVisit,
    Team,
    TeamChatMessage,
    TeamState,
)
from app.services import generate_qr_token, log_event, ws_manager
from app.notify import notify_player_assigned, notify_station_assigned

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


@router.post("/stations")
async def admin_create_station(
    name: str = Form(...),
    capacity: int = Form(1),
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    station = Station(event_id=user.event_id, name=name, capacity=capacity)
    db.add(station)
    await db.flush()
    return {"ok": True, "station_id": station.id}


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
    return templates.TemplateResponse(
        "admin/qr_items.html",
        {"request": request, "user": user, "scan_codes": scan_codes, "item_defs": ITEM_DEFINITIONS, "obtainable_keys": OBTAINABLE_ITEM_KEYS},
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
    if item_key not in OBTAINABLE_ITEM_KEYS:
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
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_admin),
):
    from fastapi.responses import RedirectResponse
    dt = DialogueThread(event_id=user.event_id, key=key, title=title or key, type=type)
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
    r = await db.execute(
        select(DialogueThread).where(
            DialogueThread.id == thread_id,
            DialogueThread.event_id == user.event_id,
        )
    )
    t = r.scalar_one_or_none()
    if not t:
        return Response(status_code=404)
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
    import json
    stations_json = json.dumps([{"id": s.id, "name": s.name} for s in stations])
    return templates.TemplateResponse(
        "admin/dialogue_graph.html",
        {"request": request, "user": user, "thread": thread, "stations": stations, "stations_json": stations_json},
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
    msgs = sorted(thread.messages, key=lambda m: m.order_index)
    r = await db.execute(select(Station).where(Station.event_id == user.event_id))
    stations = r.scalars().all()
    return templates.TemplateResponse(
        "admin/dialogue_edit.html",
        {"request": request, "user": user, "thread": thread, "messages": msgs, "stations": stations},
    )


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
    reply_options: str = Form(""),  # JSON: [{"text":"...", "next_message_id":N}]
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
    r = await db.execute(
        select(DialogueMessage.order_index)
        .where(DialogueMessage.thread_id == thread_id)
        .order_by(DialogueMessage.order_index.desc())
        .limit(1)
    )
    row = r.first()
    next_order = (row[0] + 1) if row else 0
    payload = {"text": text, "character": character or ""}
    if reply_options.strip():
        try:
            opts = json.loads(reply_options)
            payload["reply_options"] = [{"text": o.get("text", ""), "next_message_id": o.get("next_message_id"), "delay_seconds": int(o.get("delay_seconds") or 0)} for o in opts]
        except Exception:
            pass
    gate_rules = {"condition_type": condition_type}
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
    for m in sorted(thread.messages, key=lambda x: x.order_index):
        opts = (m.payload or {}).get("reply_options") or []
        p = m.payload or {}
        x = p.get("pos_x") if p.get("pos_x") is not None else 50 + (m.order_index % 4) * 220
        y = p.get("pos_y") if p.get("pos_y") is not None else 50 + (m.order_index // 4) * 180
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
        })
    return {"nodes": nodes, "stations": stations, "thread_key": thread.key, "thread_title": thread.title or thread.key}


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
