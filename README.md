# Universal Quest/Event Management Platform

Платформа управления квестами и событиями в реальном времени через Telegram + Web Apps.

## Стек

- **Backend**: FastAPI, SQLAlchemy (async), PostgreSQL / SQLite
- **UI**: Jinja2 (SSR), HTMX, минимальный JS для Telegram WebApp и QR
- **Real-time**: WebSocket
- **Telegram**: aiogram (бот для onboarding)

## Быстрый старт

```bash
# Установка
pip install -r requirements.txt

# Настройка
cp .env.example .env
# Заполните TELEGRAM_BOT_TOKEN, SECRET_KEY

# Инициализация БД
python -m scripts.seed

# Запуск API
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# В другом терминале — бот
python -m bot.main
```

## Роли

- **PLAYER** — игрок, видит свою команду и контент
- **STATION_HOST** — ведущий станции, сканирует QR, начисляет очки
- **ADMIN** — организатор, Live Ops Board, контент, команды

## Структура

```
/                   — API info
/login              — Вход (Telegram initData)
/player             — Player WebApp
/station            — Station Host UI
/admin              — Admin Dashboard

/auth/verify        — POST initData → JWT
/ws/{channel}       — WebSocket (event:1, team:5, admin:1)
```

## Документация

- Подробная архитектура — [ARCHITECTURE.md](ARCHITECTURE.md)
- Как зайти в админку, управлять БД — [USAGE.md](USAGE.md)
