"""Business logic services."""
import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ContentBlock,
    Delivery,
    DialogueMessage,
    DialogueThread,
    Event,
    EventLog,
    EventUser,
    Player,
    Rating,
    Station,
    StationHost,
    StationVisit,
    Team,
    TeamState,
    VisitState,
)
from app.websocket_hub import ws_manager


def generate_qr_token(event_id: int, team_id: int) -> str:
    """Generate signed token for QR code (event_id:team_id:signature)."""
    payload = f"{event_id}:{team_id}"
    sig = secrets.token_hex(16)
    return f"{payload}:{sig}"


def parse_qr_token(token: str) -> tuple[int, int] | None:
    """Parse and validate QR token, return (event_id, team_id) or None."""
    try:
        parts = token.split(":")
        if len(parts) != 3:
            return None
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return None


async def resolve_user_role(session: AsyncSession, tg_id: int, event_id: int) -> EventUser | None:
    """Get EventUser for tg_id in event."""
    r = await session.execute(
        select(EventUser).where(EventUser.tg_id == tg_id, EventUser.event_id == event_id)
    )
    return r.scalar_one_or_none()


async def log_event(
    session: AsyncSession,
    event_id: int,
    event_type: str,
    data: dict,
    team_id: int | None = None,
    player_id: int | None = None,
) -> None:
    entry = EventLog(
        event_id=event_id,
        team_id=team_id,
        player_id=player_id,
        event_type=event_type,
        data=data,
    )
    session.add(entry)
    await session.flush()
    await ws_manager.broadcast_admin(event_id, "admin:log_entry", {"log": data, "type": event_type})
