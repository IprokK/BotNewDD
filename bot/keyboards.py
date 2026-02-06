"""Клавиатуры бота."""
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton


def main_kb():
    """Главное меню: Информация о квесте, Регистрация."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Информация о квесте")],
            [KeyboardButton(text="✍️ Регистрация")],
        ],
        resize_keyboard=True,
    )
