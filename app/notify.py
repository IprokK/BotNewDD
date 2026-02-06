"""Отправка уведомлений в Telegram участникам."""
import httpx

from config import settings


async def send_telegram(tg_id: int, text: str, parse_mode: str = "Markdown") -> bool:
    """Отправить сообщение пользователю в Telegram."""
    if not settings.telegram_bot_token:
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                url,
                json={"chat_id": tg_id, "text": text, "parse_mode": parse_mode},
                timeout=10,
            )
            return r.status_code == 200
    except Exception:
        return False


def _esc(s: str) -> str:
    return s.replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")


async def notify_player_assigned(tg_id: int, team_name: str) -> bool:
    text = f"✅ Тебя добавили в команду *{_esc(team_name)}*!\nНажми «Открыть игру» — там твоя команда и прогресс."
    return await send_telegram(tg_id, text)


async def notify_station_assigned(tg_id: int, station_name: str) -> bool:
    text = f"📍 Вам назначена станция *{_esc(station_name)}*!\nНаправляйтесь туда."
    return await send_telegram(tg_id, text)


async def notify_content_delivered(tg_id: int, title: str, preview: str = "") -> bool:
    text = f"📬 *Новый контент:* {_esc(title)}"
    if preview:
        text += f"\n\n{_esc(preview[:300])}"
    return await send_telegram(tg_id, text)


async def notify_visit_finished(tg_id: int, station_name: str, points: int) -> bool:
    text = f"✅ Визит на станцию *{_esc(station_name)}* завершён!\nНачислено очков: {points}"
    return await send_telegram(tg_id, text)


async def notify_dialogue_message(tg_id: int, thread_title: str, character: str, text: str, webapp_url: str) -> bool:
    """Уведомить участника о новом сообщении в диалоге (по расписанию)."""
    sender = f"*{_esc(character)}:* " if character else ""
    msg = f"💬 *{_esc(thread_title)}*\n\n{sender}{_esc(text[:400])}{'…' if len(text) > 400 else ''}\n\n👉 Открыть: {webapp_url}"
    return await send_telegram(tg_id, msg)
