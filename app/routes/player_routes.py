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
    DialogueThread,
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

    # Dialogue threads available
    r = await db.execute(
        select(DialogueThread).where(DialogueThread.event_id == user.event_id)
    )
    threads = r.scalars().all()

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

    # Соседи по команде (напарники) — для ссылки «Написать в Telegram»
    teammates = []
    if user.team_id:
        r = await db.execute(
            select(Player).where(
                Player.team_id == user.team_id,
                Player.id != user.player_id,
            )
        )
        teammates = list(r.scalars().all())

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
            "inventory": inventory,
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
    threads = list(r.scalars().all())
    for t in threads:
        first = next((m for m in sorted(t.messages, key=lambda x: x.order_index) if (m.payload or {}).get("text")), None)
        txt = (first.payload or {}).get("text", "") if first else ""
        t.preview = (txt[:60] + "…") if len(txt) > 60 else (txt or None)
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


def _check_conditions(m, user, player, team_id, replied_ids, visited_station_ids) -> bool:
    """Проверка условий показа сообщения."""
    rules = m.gate_rules or {}
    ct = rules.get("condition_type", "immediate")
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

    msgs_by_id = {m.id: m for m in thread.messages}
    visible = []
    pending_reply = None  # Message waiting for player reply
    i = 0
    messages_sorted = sorted(thread.messages, key=lambda x: x.order_index)
    while i < len(messages_sorted):
        m = messages_sorted[i]
        if m.audience not in (ContentAudience.TEAM.value, role):
            i += 1
            continue
        if not _check_conditions(m, user, player, user.team_id, replied_ids, visited_station_ids):
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
                        if _check_conditions(next_m, user, player, user.team_id, replied_ids, visited_station_ids):
                            visible.append(next_m)
                            # Continue from next message
                            idx = messages_sorted.index(next_m) if next_m in messages_sorted else i + 1
                            i = idx + 1
                            continue
            else:
                pending_reply = m
                break
        i += 1

    player_role = (player.role or "A").replace("ROLE_", "") if player else "—"
    return templates.TemplateResponse(
        "player/dialogue.html",
        {
            "request": request,
            "user": user,
            "thread": thread,
            "messages": visible,
            "pending_reply": pending_reply,
            "team": team,
            "player_role": player_role,
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

    if next_msg:
        sender = (next_msg.payload or {}).get("character", "Ответ")
        text = (next_msg.payload or {}).get("text", "")
        opts = (next_msg.payload or {}).get("reply_options") or []
        opts_html = ""
        if opts:
            opts_html = '<div class="quick-replies" style="margin-top:12px;">'
            for o in opts:
                nid = o.get("next_message_id")
                d = int(o.get("delay_seconds") or 0)
                opts_html += f'<form hx-post="/player/dialogues/{key}/reply" hx-target="#dialogue-messages" hx-swap="beforeend" style="display:inline;"><input type="hidden" name="message" value="{html.escape(o.get("text", ""))}"><input type="hidden" name="message_id" value="{next_msg.id}"><input type="hidden" name="next_message_id" value="{nid or ""}"><input type="hidden" name="delay_seconds" value="{d}"><button type="submit" class="quick-reply">{html.escape(o.get("text", ""))}</button></form> '
            opts_html += "</div>"
        delay = max(0, int(delay_seconds or 0))
        next_bubble = f'<div class="msg-bubble in"><div class="sender">{html.escape(sender)}</div>{html.escape(text)}{opts_html}</div>'
        if delay > 0:
            next_bubble = f'<div class="msg-delayed" style="opacity:0;animation:fadeInMsg 0.3s {delay}s forwards;">{next_bubble}</div><style>@keyframes fadeInMsg{{to{{opacity:1;}}}}</style>'
        return HTMLResponse(
            f'<div class="msg-bubble out" style="margin-left:auto;"><div class="sender">Вы</div>{html.escape(message)}</div>'
            + next_bubble
        )

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
                        sender = (next_msg.payload or {}).get("character", "Ответ")
                        text = (next_msg.payload or {}).get("text", "")
                        delay = max(0, int(o.get("delay_seconds") or delay_seconds or 0))
                        next_bubble = f'<div class="msg-bubble in"><div class="sender">{html.escape(sender)}</div>{html.escape(text)}</div>'
                        if delay > 0:
                            next_bubble = f'<div class="msg-delayed" style="opacity:0;animation:fadeInMsg 0.3s {delay}s forwards;">{next_bubble}</div><style>@keyframes fadeInMsg{{to{{opacity:1;}}}}</style>'
                        return HTMLResponse(
                            f'<div class="msg-bubble out" style="margin-left:auto;"><div class="sender">Вы</div>{html.escape(message)}</div>'
                            + next_bubble
                        )
    return HTMLResponse(
        f'<div class="msg-bubble out" style="margin-left:auto;"><div class="sender">Вы</div>{html.escape(message)}</div>'
        '<div class="msg-bubble in"><div class="sender">—</div>Нет ответа на это сообщение</div>'
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
