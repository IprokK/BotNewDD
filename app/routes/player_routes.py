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
    DialogueReply,
    DialogueStartConfig,
    DialogueThread,
    DialogueThreadUnlock,
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


def _thread_visible(thread_id: int, with_config: set[int], unlocked: set[int]) -> bool:
    return thread_id not in with_config or thread_id in unlocked


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
    threads = [t for t in all_threads if _thread_visible(t.id, with_cfg, unlocked)]
    for t in threads:
        ms_sorted = sorted(t.messages, key=lambda x: x.order_index)
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
    threads = [t for t in all_threads if _thread_visible(t.id, with_cfg, unlocked)]
    for t in threads:
        ms_sorted = sorted(t.messages, key=lambda x: x.order_index)
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
    rp = await db.execute(select(Player).where(Player.id == user.player_id))
    player = rp.scalar_one_or_none()
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
    if not _thread_visible(thread.id, with_cfg, unlocked):
        return templates.TemplateResponse(
            "player/error.html",
            {"request": request, "message": "Этот диалог ещё недоступен для вашей команды"},
            status_code=403,
        )

    rp = await db.execute(select(Player).where(Player.id == user.player_id))
    player = rp.scalar_one_or_none()
    role = (player.role if player else None) or "ROLE_A"
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
    visible = []
    pending_reply = None
    i = 0
    messages_sorted = sorted(thread.messages, key=lambda x: x.order_index)
    while i < len(messages_sorted):
        m = messages_sorted[i]
        if m.audience not in (ContentAudience.TEAM.value, role):
            i += 1
            continue
        if not check(m):
            i += 1
            continue
        visible.append(m)
        opts = (m.payload or {}).get("reply_options") or []
        if opts:
            if m.id in replied_ids:
                r = await db.execute(
                    select(DialogueReply).where(
                        DialogueReply.message_id == m.id,
                        DialogueReply.player_id == user.player_id,
                    ).order_by(DialogueReply.replied_at.desc()).limit(1)
                )
                rep = r.scalar_one_or_none()
                if rep and rep.next_message_id and rep.next_message_id in msgs_by_id:
                    next_m = msgs_by_id[rep.next_message_id]
                    if next_m not in visible and next_m.audience in (ContentAudience.TEAM.value, role):
                        if check(next_m):
                            visible.append(next_m)
                            idx = messages_sorted.index(next_m) if next_m in messages_sorted else i + 1
                            i = idx + 1
                            continue
            else:
                pending_reply = m
                break
        i += 1

    # Вычисляем show_delay и delete_after для каждого сообщения
    cumulative = 0
    enriched = []
    for m in visible:
        rules = m.gate_rules or {}
        delay_prev = rules.get("delay_after_previous_seconds") or 0
        show_delay = cumulative
        cumulative += delay_prev
        delete_after = (m.payload or {}).get("delete_after_seconds") or 0
        enriched.append({"msg": m, "show_delay_seconds": show_delay, "delete_after_seconds": delete_after})

    characters = ((thread.config or {}).get("characters") or {})

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
        return f'<div class="msg-row in"{da}><div class="msg-avatar">{av_inner}</div><div class="msg-bubble"><div class="sender">{html.escape(sender)}</div>{html.escape(text)}{opts_html}</div></div>'

    if next_msg:
        delay = max(0, int(delay_seconds or 0))
        next_bubble = _render_in_msg(next_msg)
        if delay > 0:
            next_bubble = f'<div class="msg-delayed" data-delay-seconds="{delay}" style="opacity:0;animation:fadeInMsg 0.3s {delay}s forwards;">{next_bubble}</div><style>@keyframes fadeInMsg{{to{{opacity:1;}}}}</style>'
        out_bubble = f'<div class="msg-row out"><div class="msg-avatar">Я</div><div class="msg-bubble"><div class="sender">Вы</div>{html.escape(message)}</div></div>'
        return HTMLResponse(out_bubble + next_bubble)

    for m in sorted(thread.messages, key=lambda x: x.order_index):
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
    return HTMLResponse(
        out_bubble + '<div class="msg-row in"><div class="msg-avatar">—</div><div class="msg-bubble"><div class="sender">—</div>Нет ответа на это сообщение</div></div>'
    )


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
