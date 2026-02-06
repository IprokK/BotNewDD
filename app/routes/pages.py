"""Page routes: login, landing, redirects."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.database import get_db
from app.models import Event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@router.get("/closed", response_class=HTMLResponse)
async def closed_page(request: Request):
    """Страница «Доступ закрыт» для пользователей вне whitelist."""
    return templates.TemplateResponse("closed.html", {"request": request})


@router.get("/player/partial/team-state", response_class=HTMLResponse)
async def player_team_state_partial(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """HTMX partial for team state - requires auth, will 401 if not logged in."""
    from app.auth import get_user_from_session
    try:
        user = await get_user_from_session(request)
    except Exception:
        return HTMLResponse("<p>Не авторизован</p>", status_code=401)
    if not user.team_id:
        return "<p>Ожидание назначения в команду...</p>"

    from app.models import Team, Station
    r = await db.execute(
        select(Team, Station)
        .outerjoin(Station, Team.current_station_id == Station.id)
        .where(Team.id == user.team_id)
    )
    row = r.first()
    team = row[0] if row else None
    station = row[1] if row and row[1] else None

    return templates.TemplateResponse(
        "player/partials/team_state.html",
        {"request": request, "team": team, "station": station},
    )
