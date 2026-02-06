"""Telegram bot: регистрация на квест, информация, опыт участника, уведомления."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from config import settings
from app.database import async_session_maker
from app.models import Event, Player, RegistrationForm
from sqlalchemy import select

from bot.keyboards import main_kb
from bot.registration import router as registration_router, start_registration

if not settings.telegram_bot_token:
    print("Set TELEGRAM_BOT_TOKEN to run the bot")
    sys.exit(1)

bot = Bot(token=settings.telegram_bot_token)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
dp.include_router(registration_router)

CURRENT_EVENT_ID = 1  # TODO: из конфига или БД


# --- Красивое форматирование ---
def quest_info_text(event: Event) -> str:
    cfg = event.config or {}
    name = event.name
    desc = cfg.get("description", "Увлекательный квест для команд из двух человек.")
    date = cfg.get("date", "")
    duration = cfg.get("duration", "~6 часов")
    lines = [
        f"🏆 *{name}*",
        "",
        desc,
        "",
        f"⏱ Длительность: {duration}",
    ]
    if date:
        lines.insert(-1, f"📅 Дата: {date}")
    return "\n".join(lines)


# --- Обработчики ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    name = message.from_user.first_name or "Участник"
    async with async_session_maker() as db:
        r = await db.execute(
            select(Player).where(
                Player.event_id == CURRENT_EVENT_ID,
                Player.tg_id == message.from_user.id,
            )
        )
    await message.answer(
        f"Привет, *{name}*!\n\n"
        "Добро пожаловать в квест-платформу. "
        "Здесь — регистрация и информация о квесте.",
        parse_mode="Markdown",
        reply_markup=main_kb(),
    )


@dp.message(F.text == "📋 Информация о квесте")
async def quest_info(message: Message):
    async with async_session_maker() as db:
        r = await db.execute(select(Event).where(Event.id == CURRENT_EVENT_ID))
        event = r.scalar_one_or_none()
    if not event:
        await message.answer("Квест не найден.")
        return
    await message.answer(quest_info_text(event), parse_mode="Markdown")


@dp.message(F.text == "✍️ Регистрация")
async def register(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    async with async_session_maker() as db:
        r = await db.execute(
            select(Player).where(
                Player.event_id == CURRENT_EVENT_ID,
                Player.tg_id == tg_id,
            )
        )
        existing = r.scalar_one_or_none()
        if existing and existing.team_id:
            await message.answer(
                "✅ Ты уже зарегистрирован и в команде!\n"
                "Организаторы назначат станции и отправят уведомления.",
                reply_markup=main_kb(),
            )
            return
        if existing:
            # Есть Player, но нет команды — уже подал заявку (заполнил анкету)
            r = await db.execute(
                select(RegistrationForm).where(
                    RegistrationForm.event_id == CURRENT_EVENT_ID,
                    RegistrationForm.tg_id == tg_id,
                )
            )
            if r.scalar_one_or_none():
                await message.answer(
                    "✅ Ты уже подал заявку!\n"
                    "Ожидай, пока организаторы добавят тебя в команду. "
                    "Уведомление придёт сюда.",
                    reply_markup=main_kb(),
                )
                return
    await start_registration(message, state)


@dp.message(Command("quest"))
async def cmd_quest(message: Message):
    await quest_info(message)


@dp.message(Command("register", "reg", "registration"))
async def cmd_register(message: Message, state: FSMContext):
    """Команда /register — запуск анкеты регистрации."""
    await register(message, state)  # reuse same logic as button


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
