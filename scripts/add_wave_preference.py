"""Добавить колонку wave_preference в registration_forms."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from app.database import engine


async def main():
    async with engine.connect() as conn:
        r = await conn.execute(text("PRAGMA table_info(registration_forms)"))
        cols = [row[1] for row in r.fetchall()]
        if "wave_preference" in cols:
            print("OK: колонка wave_preference уже есть")
            return
        await conn.execute(text("""
            ALTER TABLE registration_forms
            ADD COLUMN wave_preference VARCHAR(100)
        """))
        await conn.commit()
    print("OK: колонка wave_preference добавлена")


if __name__ == "__main__":
    asyncio.run(main())
