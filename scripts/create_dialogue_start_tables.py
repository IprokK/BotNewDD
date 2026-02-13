"""Создать таблицы team_groups, dialogue_start_configs, dialogue_thread_unlocks."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import engine
from sqlalchemy import text


async def main():
    async with engine.connect() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS team_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                name VARCHAR(100) NOT NULL,
                team_ids TEXT DEFAULT '[]'
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS dialogue_start_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                thread_id INTEGER NOT NULL REFERENCES dialogue_threads(id) ON DELETE CASCADE,
                start_at TIMESTAMP,
                target_type VARCHAR(20) DEFAULT 'all',
                target_team_ids TEXT DEFAULT '[]',
                target_group_id INTEGER REFERENCES team_groups(id) ON DELETE SET NULL,
                order_index INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS dialogue_thread_unlocks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id INTEGER NOT NULL REFERENCES dialogue_threads(id) ON DELETE CASCADE,
                team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                unlocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        await conn.commit()
    print("OK: таблицы созданы")


if __name__ == "__main__":
    asyncio.run(main())
