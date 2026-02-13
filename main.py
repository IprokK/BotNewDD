"""Quest/Event Management Platform - FastAPI application."""
import asyncio
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from app.auth import decode_jwt, get_user_from_session
from app.database import engine, Base, async_session_maker
from app.routes import admin_routes, admin_db, auth_routes, dev_routes, pages, player_routes, station_routes
from app.scheduler_dialogue import process_dialogue_starts, process_dialogue_transitions, process_scheduled_dialogues
from app.websocket_hub import ws_manager

_scheduler: AsyncIOScheduler | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    global _scheduler
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        process_scheduled_dialogues,
        "interval",
        minutes=1,
        id="dialogue_scheduled",
    )
    _scheduler.add_job(
        process_dialogue_starts,
        "interval",
        minutes=1,
        id="dialogue_starts",
    )
    _scheduler.add_job(
        process_dialogue_transitions,
        "interval",
        minutes=1,
        id="dialogue_transitions",
    )
    _scheduler.start()
    yield
    if _scheduler:
        _scheduler.shutdown(wait=False)


app = FastAPI(
    title="Quest Platform",
    lifespan=lifespan,
)

# Mount static files if needed
# app.mount("/static", StaticFiles(directory="static"), name="static")

# Routes
app.include_router(pages.router)
app.include_router(auth_routes.router)
app.include_router(dev_routes.router)
app.include_router(player_routes.router)
app.include_router(station_routes.router)
app.include_router(admin_routes.router)
app.include_router(admin_db.router)


@app.get("/")
async def root():
    return {"message": "Quest Platform API", "docs": "/docs"}


@app.websocket("/ws/{channel}")
async def websocket_endpoint(websocket: WebSocket, channel: str):
    """WebSocket for real-time updates. channel format: event:1, team:5, admin:1, station:3"""
    await websocket.accept()
    # Optional: verify token from query param for private channels
    token = websocket.query_params.get("token")
    extra = []
    if token:
        try:
            payload = decode_jwt(token)
            event_id = payload.get("event_id")
            team_id = payload.get("team_id")
            station_id = payload.get("station_id")
            role = payload.get("role")
            if role in ("ADMIN", "SUPERADMIN"):
                extra.append(f"admin:{event_id}")
            if team_id:
                extra.append(f"team:{team_id}")
            if station_id:
                extra.append(f"station:{station_id}")
        except Exception:
            pass

    await ws_manager.connect(websocket, channel, extra_channels=extra if extra else None)
    try:
        while True:
            data = await websocket.receive_text()
            # Echo or handle client messages if needed
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, channel, extra_channels=extra if extra else None)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
