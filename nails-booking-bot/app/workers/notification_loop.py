import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import select

from app.db import async_session_factory
from app.models import (
    Appointment,
    Business,
    NotificationStatus,
    NotificationTask,
    NotificationType,
    RecipientType,
)
from app.services.formatting import appointment_card, format_dt

log = logging.getLogger(__name__)

POLL_SECONDS = 15
RETRY_MINUTES = 5
MAX_ATTEMPTS = 3


def _is_blocked(exc: BaseException) -> bool:
    text = str(exc).lower()
    return isinstance(exc, TelegramForbiddenError) or "blocked by the user" in text or "user is deactivated" in text or "chat not found" in text


async def _text_for_task(session, task: NotificationTask) -> str:
    business = await session.scalar(select(Business).where(Business.id == task.business_id))
    appt = None
    if task.appointment_id:
        appt = await session.scalar(
            select(Appointment).where(
                Appointment.id == task.appointment_id,
                Appointment.business_id == task.business_id,
            )
        )
    if appt is None or business is None:
        return "Уведомление о записи."

    card = task.card_text or appointment_card(appt, business, appt.service)
    if task.type == NotificationType.new_booking:
        return "Новая запись.\n" + card
    if task.type == NotificationType.canceled:
        return "Запись отменена.\n" + card
    if task.type == NotificationType.rescheduled:
        return "Запись перенесена.\n" + card
    if task.type == NotificationType.reminder:
        when = format_dt(appt.starts_at, business.timezone)
        who = (
            "Напоминание о записи"
            if task.recipient_type == RecipientType.client
            else "Напоминание мастеру"
        )
        return f"{who}: {when}\n" + card
    return card


async def process_due_notifications(get_bot) -> int:
    """Берёт pending-задачи FOR UPDATE SKIP LOCKED и отправляет их."""
    now = datetime.now(timezone.utc)
    processed = 0
    async with async_session_factory() as session:
        async with session.begin():
            tasks = (
                await session.scalars(
                    select(NotificationTask)
                    .where(
                        NotificationTask.status == NotificationStatus.pending,
                        NotificationTask.send_at <= now,
                    )
                    .order_by(NotificationTask.send_at)
                    .limit(50)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for task in tasks:
                bot: Bot | None = get_bot(task.business_id)
                if bot is None:
                    task.attempts += 1
                    task.last_error = "бот не запущен"
                    if task.attempts >= MAX_ATTEMPTS:
                        task.status = NotificationStatus.failed
                    else:
                        task.send_at = now + timedelta(minutes=RETRY_MINUTES)
                    processed += 1
                    continue
                try:
                    text = await _text_for_task(session, task)
                    await bot.send_message(chat_id=task.telegram_id, text=text)
                    task.status = NotificationStatus.sent
                except Exception as exc:
                    log.warning("notification %s failed: %s", task.id, exc)
                    task.last_error = str(exc)[:500]
                    if _is_blocked(exc):
                        task.status = NotificationStatus.failed
                    else:
                        task.attempts += 1
                        if task.attempts >= MAX_ATTEMPTS:
                            task.status = NotificationStatus.failed
                        else:
                            task.send_at = now + timedelta(minutes=RETRY_MINUTES)
                processed += 1
    return processed


async def run_notification_loop(get_bot) -> None:
    """Цикл в процессе приложения. После рестарта задачи снова читаются из БД."""
    while True:
        try:
            await process_due_notifications(get_bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("сбой цикла напоминаний")
        await asyncio.sleep(POLL_SECONDS)
