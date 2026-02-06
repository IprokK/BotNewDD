"""Планировщик: отправка диалогов по расписанию в Telegram."""
from datetime import datetime, timezone

from sqlalchemy import or_, select

from app.database import async_session_maker
from app.models import (
    ContentAudience,
    DialogueMessage,
    DialogueScheduledDelivery,
    DialogueThread,
    Player,
    Team,
)
from app.notify import notify_dialogue_message
from config import settings


def _parse_scheduled(sa: str) -> datetime | None:
    if not sa:
        return None
    try:
        t = datetime.fromisoformat(sa.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t
    except Exception:
        return None


async def process_scheduled_dialogues() -> None:
    """Проверить и отправить все просроченные запланированные сообщения."""
    now = datetime.now(timezone.utc)
    webapp = settings.webapp_url.rstrip("/")

    async with async_session_maker() as db:
        r = await db.execute(
            select(DialogueMessage, DialogueThread).join(
                DialogueThread, DialogueMessage.thread_id == DialogueThread.id
            )
        )
        rows = r.all()
        for msg, thread in rows:
            gr = msg.gate_rules or {}
            if gr.get("condition_type") != "scheduled":
                continue
            sa = gr.get("scheduled_at")
            t = _parse_scheduled(sa) if isinstance(sa, str) else None
            if not t or now < t:
                continue

            # Получить команды для рассылки
            r2 = await db.execute(
                select(Team).where(Team.event_id == msg.event_id)
            )
            teams = r2.scalars().all()

            payload = msg.payload or {}
            text = payload.get("text", "")
            character = payload.get("character", "")
            audience = msg.audience or ContentAudience.TEAM.value

            for team in teams:
                # Проверить, уже отправили этой команде
                r3 = await db.execute(
                    select(DialogueScheduledDelivery).where(
                        DialogueScheduledDelivery.message_id == msg.id,
                        DialogueScheduledDelivery.team_id == team.id,
                    )
                )
                if r3.scalar_one_or_none():
                    continue

                # Получить получателей
                if audience == ContentAudience.TEAM.value:
                    r4 = await db.execute(select(Player).where(Player.team_id == team.id))
                    players = r4.scalars().all()
                elif audience == ContentAudience.ROLE_A.value:
                    r4 = await db.execute(
                        select(Player).where(
                            Player.team_id == team.id,
                            or_(Player.role == "ROLE_A", Player.role == "A"),
                        )
                    )
                    players = r4.scalars().all()
                elif audience == ContentAudience.ROLE_B.value:
                    r4 = await db.execute(
                        select(Player).where(
                            Player.team_id == team.id,
                            or_(Player.role == "ROLE_B", Player.role == "B"),
                        )
                    )
                    players = r4.scalars().all()
                else:
                    players = []

                if not players:
                    # Записать доставку даже пустой команде, чтобы не проверять снова
                    pass

                for p in players:
                    if p.tg_id:
                        await notify_dialogue_message(
                            p.tg_id,
                            thread.title or thread.key,
                            character,
                            text,
                            f"{webapp}/dialogues/{thread.key}",
                        )

                # Записать факт доставки
                d = DialogueScheduledDelivery(message_id=msg.id, team_id=team.id)
                db.add(d)

            await db.commit()
