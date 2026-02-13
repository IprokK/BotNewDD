"""Создать таблицу dialogue_transition_triggers."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import engine
from sqlalchemy import text


async def main():
    async with engine.connect() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS dialogue_transition_triggers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                source_message_id INTEGER NOT NULL REFERENCES dialogue_messages(id) ON DELETE CASCADE,
                target_thread_id INTEGER NOT NULL REFERENCES dialogue_threads(id) ON DELETE CASCADE,
                unlock_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        await conn.commit()
    print("OK: таблица dialogue_transition_triggers создана")


if __name__ == "__main__":
    asyncio.run(main())
