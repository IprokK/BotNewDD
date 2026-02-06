"""Admin database browser."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin
from app.database import get_db
from app.models import (
    Event,
    Team,
    Player,
    Station,
    StationHost,
    StationVisit,
    ContentBlock,
    DialogueThread,
    DialogueMessage,
    EventLog,
    EventUser,
    Rating,
    Delivery,
)

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")

# Таблицы для браузера
DB_TABLES = [
    ("events", "События", Event),
    ("teams", "Команды", Team),
    ("players", "Игроки", Player),
    ("stations", "Станции", Station),
    ("station_hosts", "Ведущие", StationHost),
    ("content_blocks", "Контент", ContentBlock),
    ("dialogue_threads", "Диалоги", DialogueThread),
    ("dialogue_messages", "Сообщения диалогов", DialogueMessage),
    ("event_log", "Лог", EventLog),
    ("event_users", "Пользователи событий", EventUser),
    ("station_visits", "Визиты станций", StationVisit),
    ("ratings", "Оценки", Rating),
    ("deliveries", "Доставки", Delivery),
]


@router.get("/db", response_class=HTMLResponse)
async def admin_db(
    request: Request,
    table: str = "",
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
):
    """Интерактивный браузер БД."""
    table_map = {t[0]: t for t in DB_TABLES}
    rows = []
    selected_table = None
    if table and table in table_map:
        selected_table = table
        model = table_map[table][2]
        q = select(model)
        if hasattr(model, "event_id"):
            q = q.where(model.event_id == user.event_id)
        r = await db.execute(q)
        rows = r.scalars().all()
    return templates.TemplateResponse(
        "admin/db.html",
        {
            "request": request,
            "user": user,
            "tables": DB_TABLES,
            "selected_table": selected_table,
            "rows": rows,
        },
    )
