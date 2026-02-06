"""Seed database with demo event."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from app.database import async_session_maker, engine
from app.models import Base, Event, EventUser, Station, StationHost


async def seed():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session_maker() as session:
        r = await session.execute(select(Event).where(Event.slug == "demo"))
        if r.scalar_one_or_none():
            print("Demo event already exists")
            return

        event = Event(
            name="Demo Quest",
            slug="demo",
            config={
                "description": "Увлекательный квест для команд из двух человек. Следуйте маршруту, проходите станции и набирайте очки!",
                "date": "Скоро",
                "duration": "~6 часов",
            },
        )
        session.add(event)
        await session.flush()

        s1 = Station(event_id=event.id, name="Станция 1", capacity=2)
        s2 = Station(event_id=event.id, name="Станция 2", capacity=2)
        session.add_all([s1, s2])
        await session.flush()

        # Add ADMIN: set TG_ADMIN_ID=your_telegram_id
        # Add STATION_HOST: set TG_HOST_ID=your_telegram_id (будет привязан к станции 1)
        import os
        from app.models import StationHost
        tg_admin = os.environ.get("TG_ADMIN_ID")
        if tg_admin:
            try:
                eu = EventUser(tg_id=int(tg_admin), event_id=event.id, role="ADMIN")
                session.add(eu)
            except ValueError:
                pass
        tg_host = os.environ.get("TG_HOST_ID")
        if tg_host:
            try:
                sh = StationHost(event_id=event.id, tg_id=int(tg_host), station_id=s1.id)
                session.add(sh)
            except ValueError:
                pass

        await session.commit()
        print("Seeded: event id=", event.id)


if __name__ == "__main__":
    asyncio.run(seed())
