"""Создать таблицу registration_forms (если её ещё нет)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import engine, Base
from app import models  # noqa: F401 - load all models


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("OK: таблицы созданы/обновлены")


if __name__ == "__main__":
    asyncio.run(main())
