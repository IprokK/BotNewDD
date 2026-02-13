"""Player WebApp routes."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import UserContext, get_user_from_session, require_miniapp_access, require_player
from app.database import get_db
from datetime import datetime, timezone

from app.models import (
    ContentBlock,
    Delivery,
    DialogueMessage,
    PhotoItem,
    DialogueReply,
    DialogueStartConfig,
    DialogueThread,
    DialogueThreadUnlock,
    DialogueTransitionTrigger,
    Player,
    Rating,
    RegistrationForm,
    ScanCode,
    Station,
    StationVisit,
    Team,
    TeamChatMessage,
)
from pydantic import BaseModel

router = APIRouter(tags=["player"])
templates = Jinja2Templates(directory="app/templates")


async def _get_dialogue_visibility(db: AsyncSession, event_id: int, team_id: int | None) -> tuple[set[int], set[int]]:
    """(thread_ids_with_config, unlocked_ids). Диалог виден если: нет в with_config ИЛИ есть в unlocked."""
    r = await db.execute(select(DialogueStartConfig.thread_id).where(DialogueStartConfig.event_id == event_id))
    with_config = {row[0] for row in r.all()}
    unlocked = set()
    if team_id:
        r = await db.execute(
            select(DialogueThreadUnlock.thread_id).where(DialogueThreadUnlock.team_id == team_id)
        )
        unlocked = {row[0] for row in r.all()}
    return (with_config, unlocked)


def _thread_visible(thread_id: int, thread_type: str, with_config: set[int], unlocked: set[int]) -> bool:
    """Диалог виден: LEAKED — если нет правила или разблокирован; INTERACTIVE — только если есть правило И разблокирован."""
    if (thread_type or "").upper() == "INTERACTIVE":
        return thread_id in with_config and thread_id in unlocked
    return thread_id not in with_config or thread_id in unlocked


def _normalize_role(role: str | None) -> str:
    """Привести роль к ROLE_A или ROLE_B."""
    if not role:
        return "ROLE_A"
    r = str(role).upper().replace("ROLE_", "")
    return "ROLE_B" if r == "B" else "ROLE_A"


def _thread_has_content_for_role(thread, role: str) -> bool:
    """Есть ли в диалоге хотя бы одно сообщение для этой роли (TEAM или совпадение по роли).
    Также проверяет target_roles в config — если заданы, диалог показывается только этим ролям."""
    from app.models import ContentAudience
    target_roles = (thread.config or {}).get("target_roles")
    if target_roles:
        if role not in target_roles:
            return False
    allowed = {ContentAudience.TEAM.value, role}
    return any((m.audience or ContentAudience.TEAM.value) in allowed for m in (thread.messages or []))


@router.get("/player", response_class=HTMLResponse)
async def player_dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_miniapp_access),
):
    """Player main dashboard: team state, content feed, dialogues."""
    if not user.team_id:
        return templates.TemplateResponse(
            "player/waiting.html",
            {"request": request, "user": user, "message": "Вас ещё не добавили в команду"},
        )

    r = await db.execute(
        select(Team, Station)
        .outerjoin(Station, Team.current_station_id == Station.id)
        .where(Team.id == user.team_id)
    )
    row = r.first()
    team = row[0] if row else None
    station = row[1] if row and row[1] else None

    # Delivered content for this player
    r = await db.execute(
        select(ContentBlock, Delivery)
        .join(Delivery, Delivery.content_block_id == ContentBlock.id)
        .where(
            Delivery.event_id == user.event_id,
            (Delivery.team_id == user.team_id) | (Delivery.player_id == user.player_id),
        )
        .order_by(Delivery.delivered_at.desc())
        .limit(20)
    )
    deliveries = r.all()

    # Dialogue threads available (with preview and avatar)
    r = await db.execute(
        select(DialogueThread)
        .options(selectinload(DialogueThread.messages))
        .where(DialogueThread.event_id == user.event_id)
    )
    all_threads = list(r.scalars().all())
    with_cfg, unlocked = await _get_dialogue_visibility(db, user.event_id, user.team_id)
    r = await db.execute(select(Player).where(Player.id == user.player_id))
    player_for_role = r.scalar_one_or_none()
    role = _normalize_role(player_for_role.role if player_for_role else None)
    threads = [
        t for t in all_threads
        if _thread_visible(t.id, t.type or "", with_cfg, unlocked) and _thread_has_content_for_role(t, role)
    ]
    for t in threads:
        ms_sorted = sorted(t.messages, key=lambda x: (x.order_index if x.order_index is not None else 999999, x.id))
        first = next((m for m in ms_sorted if (m.payload or {}).get("text")), None)
        txt = (first.payload or {}).get("text", "") if first else ""
        t.preview = (txt[:60] + "…") if len(txt) > 60 else (txt or "")
        chars = (t.config or {}).get("characters") or {}
        char_name = (first.payload or {}).get("character") if first else None
        t.avatar_url = (chars.get(char_name) or {}).get("avatar") if char_name else None

    # Визиты, по которым ещё не оценено (для формы рейтинга)
    r = await db.execute(
        select(StationVisit, Station)
        .join(Station, StationVisit.station_id == Station.id)
        .where(
            StationVisit.event_id == user.event_id,
            StationVisit.team_id == user.team_id,
            StationVisit.state == "finished",
        )
    )
    finished_visits = r.all()
    r = await db.execute(
        select(Rating.station_visit_id).where(Rating.player_id == user.player_id)
    )
    rated_visit_ids = {row[0] for row in r.all()}
    pending_ratings = [(v, s) for v, s in finished_visits if v.id not in rated_visit_ids]

    r = await db.execute(
        select(Player).where(Player.id == user.player_id)
    )
    player = r.scalar_one_or_none()
    player_role = (player.role or "A").replace("ROLE_", "") if player else "—"
    inventory = list((player.player_progress or {}).get("inventory") or [])

    # Контент дневника по роли (для предмета «Личный дневник»)
    from app.diary_content import get_diary_entries_for_role
    diary_subtitle, diary_entries = get_diary_entries_for_role(player_role)

    # Фото-предметы в инвентаре
    photo_items_map = {}
    photo_keys = [k for k in inventory if isinstance(k, str) and (k.startswith("photo_") or k in ("photo",))]
    if photo_keys:
        r = await db.execute(
            select(PhotoItem).where(
                PhotoItem.event_id == user.event_id,
                PhotoItem.item_key.in_(photo_keys),
            )
        )
        for pi in r.scalars().all():
            photo_items_map[pi.item_key] = {"image_url": pi.image_url, "signature": pi.back_signature or "", "date": pi.back_date or ""}

    # Все станции и посещённые — для маршрутного листа
    r = await db.execute(
        select(Station).where(Station.event_id == user.event_id).order_by(Station.name)
    )
    all_stations = r.scalars().all()
    visited_station_ids = {v.station_id for v, s in finished_visits}
    current_station_id = station.id if station else None

    # Соседи по команде (напарники) — имена и последнее сообщение
    teammate_info = None  # {name, preview, time, player_id}
    if user.team_id:
        r = await db.execute(
            select(Player).where(
                Player.team_id == user.team_id,
                Player.id != user.player_id,
            )
        )
        teammates = list(r.scalars().all())
        if teammates:
            p = teammates[0]
            rf = await db.execute(
                select(RegistrationForm.full_name).where(
                    RegistrationForm.event_id == user.event_id,
                    RegistrationForm.tg_id == p.tg_id,
                )
            )
            row = rf.first()
            name = row[0] if row else f"Участник {p.id}"
            last_msg = None
            r = await db.execute(
                select(TeamChatMessage)
                .where(
                    TeamChatMessage.team_id == user.team_id,
                    TeamChatMessage.event_id == user.event_id,
                )
                .order_by(TeamChatMessage.created_at.desc())
                .limit(1)
            )
            last_msg = r.scalar_one_or_none()
            preview = ""
            msg_time = None
            if last_msg:
                preview = (last_msg.text[:50] + "…") if len(last_msg.text) > 50 else last_msg.text
                msg_time = last_msg.created_at
            teammate_info = {"name": name, "preview": preview, "time": msg_time, "player_id": p.id}
    else:
        teammates = []

    return templates.TemplateResponse(
        "player/dashboard.html",
        {
            "request": request,
            "user": user,
            "team": team,
            "station": station,
            "deliveries": deliveries,
            "threads": threads,
            "pending_ratings": pending_ratings,
            "player_role": player_role,
            "teammates": teammates,
            "teammate_info": teammate_info,
            "inventory": inventory,
            "all_stations": all_stations,
            "visited_station_ids": visited_station_ids,
            "current_station_id": current_station_id,
            "diary_subtitle": diary_subtitle,
            "diary_entries": diary_entries,
            "photo_items_map": photo_items_map,
        },
    )


@router.get("/player/team-chat", response_class=HTMLResponse)
async def team_chat_messages(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_miniapp_access),
):
    """Сообщения чата команды (напарников) — для HTMX."""
    if not user.team_id:
        return HTMLResponse("<p style='color:var(--muted)'>Нет команды</p>")
    r = await db.execute(
        select(TeamChatMessage, Player)
        .join(Player, TeamChatMessage.sender_player_id == Player.id)
        .where(
            TeamChatMessage.team_id == user.team_id,
            TeamChatMessage.event_id == user.event_id,
        )
        .order_by(TeamChatMessage.created_at.asc())
    )
    messages = r.all()
    # Имена отправителей из RegistrationForm
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
            player_names[p.id] = row[0] if row else f"Участник {p.id}"
    return templates.TemplateResponse(
        "player/partials/team_chat.html",
        {
            "request": request,
            "messages": messages,
            "player_names": player_names,
            "current_player_id": user.player_id,
        },
    )


@router.post("/player/team-chat", response_class=HTMLResponse)
async def team_chat_send(
    request: Request,
    text: str = Form(""),
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_miniapp_access),
):
    """Отправить сообщение в чат команды."""
    if not user.team_id:
        return HTMLResponse("<p>Нет команды</p>", status_code=400)
    text = (text or "").strip()
    if not text or len(text) > 2000:
        return HTMLResponse("<p>Сообщение пустое или слишком длинное</p>", status_code=400)
    msg = TeamChatMessage(
        event_id=user.event_id,
        team_id=user.team_id,
        sender_player_id=user.player_id,
        text=text,
    )
    db.add(msg)
    await db.commit()
    # Вернуть обновлённый список сообщений (тот же partial, что и GET)
    r = await db.execute(
        select(TeamChatMessage, Player)
        .join(Player, TeamChatMessage.sender_player_id == Player.id)
        .where(
            TeamChatMessage.team_id == user.team_id,
            TeamChatMessage.event_id == user.event_id,
        )
        .order_by(TeamChatMessage.created_at.asc())
    )
    messages = r.all()
    player_names = {}
    for m, p in messages:
        if p.id not in player_names:
            rf = await db.execute(
                select(RegistrationForm.full_name).where(
                    RegistrationForm.event_id == user.event_id,
                    RegistrationForm.tg_id == p.tg_id,
                )
            )
            row = rf.first()
            player_names[p.id] = row[0] if row else f"Участник {p.id}"
    return templates.TemplateResponse(
        "player/partials/team_chat.html",
        {
            "request": request,
            "messages": messages,
            "player_names": player_names,
            "current_player_id": user.player_id,
        },
    )


@router.get("/player/dialogues", response_class=HTMLResponse)
async def dialogues_list(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_miniapp_access),
):
    r = await db.execute(
        select(DialogueThread).options(selectinload(DialogueThread.messages)).where(
            DialogueThread.event_id == user.event_id
        )
    )
    all_threads = list(r.scalars().all())
    with_cfg, unlocked = await _get_dialogue_visibility(db, user.event_id, user.team_id)
    rp = await db.execute(select(Player).where(Player.id == user.player_id))
    player = rp.scalar_one_or_none()
    role = _normalize_role(player.role if player else None)
    threads = [
        t for t in all_threads
        if _thread_visible(t.id, t.type or "", with_cfg, unlocked) and _thread_has_content_for_role(t, role)
    ]
    for t in threads:
        ms_sorted = sorted(t.messages, key=lambda x: (x.order_index if x.order_index is not None else 999999, x.id))
        first = next((m for m in ms_sorted if (m.payload or {}).get("text")), None)
        txt = (first.payload or {}).get("text", "") if first else ""
        t.preview = (txt[:60] + "…") if len(txt) > 60 else (txt or None)
        chars = (t.config or {}).get("characters") or {}
        char_name = (first.payload or {}).get("character") if first else None
        t.avatar_url = (chars.get(char_name) or {}).get("avatar") if char_name else None
    team = None
    if user.team_id:
        r = await db.execute(select(Team).where(Team.id == user.team_id))
        team = r.scalar_one_or_none()
    player_role = (player.role or "A").replace("ROLE_", "") if player else "—"
    return templates.TemplateResponse(
        "player/dialogues_list.html",
        {"request": request, "user": user, "threads": threads, "team": team, "player_role": player_role},
    )


def _check_conditions(m, user, player, team_id, replied_ids, visited_station_ids, team_progress=None, thread_key=None) -> bool:
    """Проверка условий показа сообщения."""
    rules = m.gate_rules or {}
    ct = rules.get("condition_type", "immediate")
    # Админ: force_reveal — показать сообщение досрочно
    if team_progress and thread_key:
        overrides = team_progress.get("dialogue_overrides") or {}
        thr_over = overrides.get(thread_key) or {}
        force_ids = thr_over.get("force_reveal_message_ids") or []
        if m.id in force_ids:
            return True
        hold_until = thr_over.get("hold_until")
        if hold_until:
            try:
                t = datetime.fromisoformat(hold_until.replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) < t:
                    return False  # Админ придержал
            except Exception:
                pass
    if ct == "immediate":
        return True
    if ct == "scheduled":
        sa = rules.get("scheduled_at")
        if not sa:
            return True
        try:
            t = datetime.fromisoformat(sa.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) >= t
        except Exception:
            return True
    if ct == "after_station":
        sid = rules.get("station_id")
        return sid in visited_station_ids if sid else True
    if ct == "after_message":
        amid = rules.get("after_message_id")
        return amid in replied_ids if amid else True
    return True


@router.get("/player/dialogues/{key}", response_class=HTMLResponse)
async def dialogue_view(
    request: Request,
    key: str,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_miniapp_access),
):
    from app.models import ContentAudience

    r = await db.execute(
        select(DialogueThread)
        .options(selectinload(DialogueThread.messages))
        .where(DialogueThread.event_id == user.event_id, DialogueThread.key == key)
    )
    thread = r.scalar_one_or_none()
    if not thread:
        return templates.TemplateResponse(
            "player/error.html",
            {"request": request, "message": "Диалог не найден"},
            status_code=404,
        )

    with_cfg, unlocked = await _get_dialogue_visibility(db, user.event_id, user.team_id)
    if not _thread_visible(thread.id, thread.type or "", with_cfg, unlocked):
        return templates.TemplateResponse(
            "player/error.html",
            {"request": request, "message": "Этот диалог ещё недоступен для вашей команды"},
            status_code=403,
        )

    rp = await db.execute(select(Player).where(Player.id == user.player_id))
    player = rp.scalar_one_or_none()
    role = _normalize_role(player.role if player else None)
    if not _thread_has_content_for_role(thread, role):
        return templates.TemplateResponse(
            "player/error.html",
            {"request": request, "message": "Этот диалог не предназначен для вашей роли"},
            status_code=403,
        )

    r = await db.execute(select(Team).where(Team.id == user.team_id)) if user.team_id else None
    team = r.scalar_one_or_none() if r else None

    # Replied message IDs
    r = await db.execute(
        select(DialogueReply.message_id).where(
            DialogueReply.player_id == user.player_id,
            DialogueReply.event_id == user.event_id,
        )
    )
    replied_ids = {row[0] for row in r.all()}
    # Visited station IDs (finished)
    visited_station_ids = set()
    if user.team_id:
        r = await db.execute(
            select(StationVisit.station_id).where(
                StationVisit.team_id == user.team_id,
                StationVisit.state == "finished",
            )
        )
        visited_station_ids = {row[0] for row in r.all()}

    team_progress = (team.team_progress or {}) if team else {}
    check = lambda m: _check_conditions(m, user, player, user.team_id, replied_ids, visited_station_ids, team_progress, thread.key)

    msgs_by_id = {m.id: m for m in thread.messages}
    # Карта входящих: кто указывает на это сообщение через reply_options
    incoming: dict[int, list[int]] = {mid: [] for mid in msgs_by_id}
    for m in thread.messages:
        for o in (m.payload or {}).get("reply_options") or []:
            nid = o.get("next_message_id")
            if nid and nid in msgs_by_id:
                incoming.setdefault(nid, []).append(m.id)
    # Стартовые узлы — без входящих рёбер
    start_candidates = [m for m in thread.messages if not incoming.get(m.id)]
    start_id = min(start_candidates, key=lambda x: x.order_index).id if start_candidates else min(msgs_by_id, key=lambda mid: msgs_by_id[mid].order_index)

    visible: list = []
    pending_reply = None
    queue: list[int] = [start_id]
    visited: set[int] = set()
    replies_by_msg: dict[int, int] = {}  # message_id -> chosen next_message_id
    r_replies = await db.execute(
        select(DialogueReply.message_id, DialogueReply.next_message_id).where(
            DialogueReply.player_id == user.player_id,
            DialogueReply.event_id == user.event_id,
        )
    )
    for row in r_replies.all():
        replies_by_msg[row[0]] = row[1]

    while queue:
        mid = queue.pop(0)
        if mid in visited:
            continue
        visited.add(mid)
        m = msgs_by_id.get(mid)
        if not m or m.audience not in (ContentAudience.TEAM.value, role) or not check(m):
            continue
        visible.append(m)
        opts = [o for o in ((m.payload or {}).get("reply_options") or []) if o.get("next_message_id")]
        if not opts:
            continue
        if len(opts) == 1:
            next_id = opts[0].get("next_message_id")
            if next_id and next_id not in visited:
                queue.insert(0, next_id)
            continue
        if m.id in replied_ids and replies_by_msg.get(m.id):
            next_id = replies_by_msg[m.id]
            if next_id and next_id in msgs_by_id and next_id not in visited:
                queue.insert(0, next_id)
            continue
        pending_reply = m
        break

    default_typing = int((thread.config or {}).get("default_typing_delay", 2))
    cumulative = 0
    enriched = []
    for m in visible:
        rules = m.gate_rules or {}
        delay_prev = rules.get("delay_after_previous_seconds")
        if delay_prev is None or delay_prev == 0:
            delay_prev = default_typing
        show_delay = cumulative
        cumulative += delay_prev
        delete_after = (m.payload or {}).get("delete_after_seconds") or 0
        enriched.append({"msg": m, "show_delay_seconds": show_delay, "delete_after_seconds": delete_after})

    characters = ((thread.config or {}).get("characters") or {})

    # Триггеры перехода: при достижении сообщения с trigger_dialogue — запланировать разблокировку другого диалога
    if user.team_id:
        for m in visible:
            tr = (m.payload or {}).get("trigger_dialogue") or {}
            target_key = tr.get("thread_key") or tr.get("thread_id")
            delay_min = int(tr.get("delay_minutes", 0))
            if not target_key or delay_min < 0:
                continue
            if isinstance(target_key, int) or (isinstance(target_key, str) and str(target_key).isdigit()):
                r_tr = await db.execute(
                    select(DialogueThread).where(
                        DialogueThread.id == int(target_key),
                        DialogueThread.event_id == user.event_id,
                    )
                )
            else:
                r_tr = await db.execute(
                    select(DialogueThread).where(
                        DialogueThread.key == str(target_key),
                        DialogueThread.event_id == user.event_id,
                    )
                )
            target_thread = r_tr.scalar_one_or_none()
            if not target_thread or target_thread.id == thread.id:
                continue
            r_ex = await db.execute(
                select(DialogueTransitionTrigger).where(
                    DialogueTransitionTrigger.team_id == user.team_id,
                    DialogueTransitionTrigger.source_message_id == m.id,
                    DialogueTransitionTrigger.target_thread_id == target_thread.id,
                )
            )
            if r_ex.scalar_one_or_none():
                continue
            from datetime import timedelta
            unlock_at = datetime.now(timezone.utc) + timedelta(minutes=delay_min)
            db.add(DialogueTransitionTrigger(
                event_id=user.event_id,
                team_id=user.team_id,
                source_message_id=m.id,
                target_thread_id=target_thread.id,
                unlock_at=unlock_at,
            ))
            await db.commit()

    # Опции ответа только с валидным next_message_id (конечные узлы графа без связей не показываем как «выбор»)
    pending_reply_opts = []
    if pending_reply:
        opts = (pending_reply.payload or {}).get("reply_options") or []
        pending_reply_opts = [o for o in opts if o.get("next_message_id")]

    player_role = (player.role or "A").replace("ROLE_", "") if player else "—"
    return templates.TemplateResponse(
        "player/dialogue.html",
        {
            "request": request,
            "user": user,
            "thread": thread,
            "messages_enriched": enriched,
            "pending_reply": pending_reply if pending_reply_opts else None,
            "pending_reply_opts": pending_reply_opts,
            "team": team,
            "player_role": player_role,
            "characters": characters,
        },
    )


@router.post("/player/dialogues/{key}/reply")
async def dialogue_reply(
    request: Request,
    key: str,
    message: str = Form(...),
    message_id: int = Form(None),
    next_message_id: int = Form(None),
    delay_seconds: int = Form(0),
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_miniapp_access),
):
    """Handle interactive dialogue reply — save reply, return next message."""
    import html
    r = await db.execute(
        select(DialogueThread)
        .options(selectinload(DialogueThread.messages))
        .where(DialogueThread.event_id == user.event_id, DialogueThread.key == key)
    )
    thread = r.scalar_one_or_none()
    if not thread:
        return HTMLResponse("<p>Диалог не найден</p>", status_code=404)

    next_msg = None
    if message_id and next_message_id:
        r = await db.execute(
            select(DialogueMessage).join(DialogueThread).where(
                DialogueMessage.id == message_id,
                DialogueThread.event_id == user.event_id,
            )
        )
        msg = r.scalar_one_or_none()
        r = await db.execute(
            select(DialogueMessage).where(
                DialogueMessage.id == next_message_id,
                DialogueMessage.thread_id == thread.id,
            )
        )
        next_msg = r.scalar_one_or_none()
        if msg and next_msg:
            reply = DialogueReply(
                event_id=user.event_id,
                player_id=user.player_id,
                message_id=msg.id,
                reply_text=message,
                next_message_id=next_msg.id,
            )
            db.add(reply)
            await db.flush()

    def _render_in_msg(msg, add_opts=True):
        p = msg.payload or {}
        sender = p.get("character", "Ответ")
        text = p.get("text", "")
        chars = (thread.config or {}).get("characters") or {}
        avatar_url = (chars.get(sender) or {}).get("avatar")
        av_inner = f'<img src="{html.escape(avatar_url)}" alt="">' if avatar_url else html.escape((sender[1:2] if sender.startswith("@") else sender[:1]) or "?")
        opts_html = ""
        if add_opts:
            opts = p.get("reply_options") or []
            if opts:
                opts_html = '<div style="margin-top:12px;">'
                for o in opts:
                    nid = o.get("next_message_id")
                    d = int(o.get("delay_seconds") or 0)
                    opts_html += f'<form hx-post="/player/dialogues/{key}/reply" hx-target="#dialogue-messages" hx-swap="beforeend" style="display:inline;"><input type="hidden" name="message" value="{html.escape(o.get("text", ""))}"><input type="hidden" name="message_id" value="{msg.id}"><input type="hidden" name="next_message_id" value="{nid or ""}"><input type="hidden" name="delay_seconds" value="{d}"><button type="submit" class="quick-reply">{html.escape(o.get("text", ""))}</button></form> '
                opts_html += "</div>"
        del_after = p.get("delete_after_seconds") or 0
        da = f' data-delete-after="{del_after}"' if del_after else ""
        img_html = ""
        if p.get("image"):
            img_html = f'<img src="{html.escape(p["image"])}" alt="" style="max-width:100%;border-radius:8px;margin-bottom:8px;display:block;">'
        return f'<div class="msg-row in"{da}><div class="msg-avatar">{av_inner}</div><div class="msg-bubble"><div class="sender">{html.escape(sender)}</div>{img_html}{html.escape(text)}{opts_html}</div></div>'

    if next_msg:
        msgs_by_id_local = {mm.id: mm for mm in thread.messages}
        chain: list = [next_msg]
        curr = next_msg
        while True:
            opts = [o for o in ((curr.payload or {}).get("reply_options") or []) if o.get("next_message_id")]
            if len(opts) != 1:
                break
            nid = opts[0].get("next_message_id")
            if not nid or nid not in msgs_by_id_local:
                break
            curr = msgs_by_id_local[nid]
            chain.append(curr)
        default_typing = int((thread.config or {}).get("default_typing_delay", 2))
        form_delay = max(0, int(delay_seconds or 0)) or default_typing
        cum_delay = 0
        parts = []
        for i, msg in enumerate(chain):
            raw = 0 if i == 0 else int(((chain[i - 1].payload or {}).get("reply_options") or [{}])[0].get("delay_seconds", 0))
            cum_delay = form_delay if i == 0 else cum_delay + (raw if raw else default_typing)
            add_opts = i == len(chain) - 1
            bb = _render_in_msg(msg, add_opts=add_opts)
            if cum_delay > 0:
                bb = f'<div class="msg-delayed" data-delay-seconds="{cum_delay}" data-delete-after="0" style="opacity:0;animation:fadeInMsg 0.3s {cum_delay}s forwards;">{bb}</div>'
            parts.append(bb)
        next_bubbles = "".join(parts)
        if form_delay > 0 or any(((m.payload or {}).get("reply_options") or [{}])[0].get("delay_seconds") for m in chain[:-1]):
            next_bubbles = next_bubbles + '<style>@keyframes fadeInMsg{to{opacity:1;}}</style>'
        out_bubble = f'<div class="msg-row out"><div class="msg-avatar">Я</div><div class="msg-bubble"><div class="sender">Вы</div>{html.escape(message)}</div></div>'
        return HTMLResponse(out_bubble + next_bubbles)

    for m in sorted(thread.messages, key=lambda x: (x.order_index if x.order_index is not None else 999999, x.id)):
        opts = (m.payload or {}).get("reply_options") or []
        for o in opts:
            if o.get("text", "").strip().lower() == message.strip().lower():
                nid = o.get("next_message_id")
                if nid:
                    r = await db.execute(select(DialogueMessage).where(DialogueMessage.id == nid))
                    next_msg = r.scalar_one_or_none()
                    if next_msg:
                        reply = DialogueReply(
                            event_id=user.event_id,
                            player_id=user.player_id,
                            message_id=m.id,
                            reply_text=message,
                            next_message_id=nid,
                        )
                        db.add(reply)
                        await db.flush()
                        delay = max(0, int(o.get("delay_seconds") or delay_seconds or 0))
                        next_bubble = _render_in_msg(next_msg, add_opts=False)
                        if delay > 0:
                            next_bubble = f'<div class="msg-delayed" style="opacity:0;animation:fadeInMsg 0.3s {delay}s forwards;">{next_bubble}</div><style>@keyframes fadeInMsg{{to{{opacity:1;}}}}</style>'
                        out_bubble = f'<div class="msg-row out"><div class="msg-avatar">Я</div><div class="msg-bubble"><div class="sender">Вы</div>{html.escape(message)}</div></div>'
                        return HTMLResponse(out_bubble + next_bubble)
    out_bubble = f'<div class="msg-row out"><div class="msg-avatar">Я</div><div class="msg-bubble"><div class="sender">Вы</div>{html.escape(message)}</div></div>'
    return HTMLResponse(out_bubble)


@router.get("/player/rating", response_class=HTMLResponse)
async def rating_form(
    request: Request,
    visit_id: int,
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_miniapp_access),
):
    """Show rating form after station visit."""
    r = await db.execute(
        select(StationVisit, Station).join(Station, StationVisit.station_id == Station.id).where(
            StationVisit.id == visit_id,
            StationVisit.event_id == user.event_id,
            StationVisit.team_id == user.team_id,
            StationVisit.state == "finished",
        )
    )
    row = r.first()
    if not row:
        return templates.TemplateResponse(
            "player/error.html",
            {"request": request, "message": "Визит не найден"},
            status_code=404,
        )
    visit, station = row
    r = await db.execute(select(Team).where(Team.id == user.team_id))
    team = r.scalar_one_or_none()
    rp = await db.execute(select(Player).where(Player.id == user.player_id))
    player = rp.scalar_one_or_none()
    player_role = (player.role or "A").replace("ROLE_", "") if player else "—"
    return templates.TemplateResponse(
        "player/rating.html",
        {"request": request, "user": user, "visit": visit, "station": station, "team": team, "player_role": player_role},
    )


class RatingSubmitRequest(BaseModel):
    visit_id: int
    station_rating: int
    host_rating: int
    comment: str | None = None


@router.post("/player/rating")
async def rating_submit(
    request: Request,
    visit_id: int = Form(...),
    station_rating: int = Form(...),
    host_rating: int = Form(...),
    comment: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_miniapp_access),
):
    r = await db.execute(
        select(StationVisit).where(
            StationVisit.id == visit_id,
            StationVisit.event_id == user.event_id,
            StationVisit.team_id == user.team_id,
            StationVisit.state == "finished",
        )
    )
    visit = r.scalar_one_or_none()
    if not visit:
        return {"ok": False, "error": "Визит не найден"}

    rating = Rating(
        event_id=user.event_id,
        station_visit_id=visit_id,
        player_id=user.player_id,
        station_rating=station_rating,
        host_rating=host_rating,
        comment=comment,
    )
    db.add(rating)
    await db.flush()
    return RedirectResponse(url="/player", status_code=303)


@router.post("/player/scan")
async def player_scan(
    code: str = Form(...),
    db: AsyncSession = Depends(get_db),
    user: UserContext = Depends(require_miniapp_access),
):
    """При сканировании QR — проверить код и добавить предмет в инвентарь."""
    code = (code or "").strip()
    if not code:
        return {"ok": False, "error": "Код пустой"}

    r = await db.execute(
        select(ScanCode).where(ScanCode.event_id == user.event_id, ScanCode.code == code)
    )
    sc = r.scalar_one_or_none()
    if not sc:
        return {"ok": False, "error": "QR-код не найден"}

    r = await db.execute(select(Player).where(Player.id == user.player_id))
    player = r.scalar_one_or_none()
    if not player:
        return {"ok": False, "error": "Игрок не найден"}

    progress = dict(player.player_progress or {})
    inv = list(progress.get("inventory") or [])
    if sc.item_key not in inv:
        inv.append(sc.item_key)
    progress["inventory"] = inv
    player.player_progress = progress
    await db.commit()

    return {"ok": True, "item_key": sc.item_key, "inventory": inv}
