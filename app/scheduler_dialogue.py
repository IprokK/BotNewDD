"""Планировщик: отправка диалогов по расписанию в Telegram."""
from datetime import datetime, timezone

from sqlalchemy import or_, select

from app.database import async_session_maker
from app.models import (
    ContentAudience,
    DialogueMessage,
    DialogueScheduledDelivery,
    DialogueStartConfig,
    DialogueThread,
    DialogueThreadUnlock,
    DialogueTransitionTrigger,
    Player,
    Team,
    TeamGroup,
)
from app.notify import notify_dialogue_message, notify_dialogue_unlocked
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


async def process_dialogue_starts() -> None:
    """Проверить DialogueStartConfig и разблокировать диалоги по расписанию."""
    now = datetime.now(timezone.utc)

    async with async_session_maker() as db:
        r = await db.execute(
            select(DialogueStartConfig, DialogueThread)
            .join(DialogueThread, DialogueStartConfig.thread_id == DialogueThread.id)
            .where(DialogueStartConfig.start_at.isnot(None))
        )
        for config, thread in r.all():
            sa = config.start_at
            if sa and sa.tzinfo is None:
                sa = sa.replace(tzinfo=timezone.utc)
            if not sa or now < sa:
                continue

            team_ids = []
            if config.target_type == "all":
                r2 = await db.execute(select(Team.id).where(Team.event_id == config.event_id))
                team_ids = [row[0] for row in r2.all()]
            elif config.target_type == "teams":
                team_ids = list(config.target_team_ids or [])
            elif config.target_type == "group" and config.target_group_id:
                r2 = await db.execute(
                    select(TeamGroup.team_ids).where(
                        TeamGroup.id == config.target_group_id,
                        TeamGroup.event_id == config.event_id,
                    )
                )
                row = r2.first()
                team_ids = list(row[0] or []) if row else []

            for tid in team_ids:
                r3 = await db.execute(
                    select(DialogueThreadUnlock).where(
                        DialogueThreadUnlock.thread_id == thread.id,
                        DialogueThreadUnlock.team_id == tid,
                    )
                )
                if r3.scalar_one_or_none():
                    continue

                r4 = await db.execute(select(Player).where(Player.team_id == tid))
                for p in r4.scalars().all():
                    if p.tg_id:
                        await notify_dialogue_unlocked(p.tg_id, thread.title or thread.key)
                db.add(
                    DialogueThreadUnlock(thread_id=thread.id, team_id=tid)
                )
            await db.commit()


async def process_dialogue_transitions() -> None:
    """Обработать триггеры перехода в другой диалог (по достижении сообщения)."""
    now = datetime.now(timezone.utc)
    async with async_session_maker() as db:
        r = await db.execute(
            select(DialogueTransitionTrigger, DialogueThread).join(
                DialogueThread, DialogueTransitionTrigger.target_thread_id == DialogueThread.id
            ).where(DialogueTransitionTrigger.unlock_at <= now)
        )
        for trigger, thread in r.all():
            r2 = await db.execute(
                select(DialogueThreadUnlock).where(
                    DialogueThreadUnlock.thread_id == thread.id,
                    DialogueThreadUnlock.team_id == trigger.team_id,
                )
            )
            if r2.scalar_one_or_none():
                await db.delete(trigger)
                await db.commit()
                continue
            r3 = await db.execute(select(Player).where(Player.team_id == trigger.team_id))
            for p in r3.scalars().all():
                if p.tg_id:
                    await notify_dialogue_unlocked(p.tg_id, thread.title or thread.key)
            db.add(DialogueThreadUnlock(thread_id=thread.id, team_id=trigger.team_id))
            await db.delete(trigger)
            await db.commit()
