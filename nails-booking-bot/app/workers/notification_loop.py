import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import func, select

from app.db import async_session_factory
from app.models import (
    Appointment,
    AppointmentStatus,
    Business,
    NotificationStatus,
    NotificationTask,
    NotificationType,
    RecipientType,
    Staff,
)
from app.services.formatting import appointment_card, format_dt

log = logging.getLogger(__name__)

POLL_SECONDS = 15
RETRY_MINUTES = 5
MAX_ATTEMPTS = 3


def _is_blocked(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        isinstance(exc, TelegramForbiddenError)
        or "blocked by the user" in text
        or "user is deactivated" in text
        or "chat not found" in text
    )


def _day_bounds(business: Business, local_day):
    tz = ZoneInfo(business.timezone)
    day_start = datetime.combine(local_day, time.min, tzinfo=tz).astimezone(timezone.utc)
    day_end = datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=tz).astimezone(
        timezone.utc
    )
    return day_start, day_end


async def _digest_text(session, business: Business, staff: Staff) -> str:
    """Утро мастера: его записи на сегодня; владельцу плюс строка команды."""
    tz = ZoneInfo(business.timezone)
    local_today = datetime.now(tz).date()
    day_start, day_end = _day_bounds(business, local_today)
    appts = (
        await session.scalars(
            select(Appointment)
            .where(
                Appointment.business_id == business.id,
                Appointment.staff_id == staff.id,
                Appointment.status == AppointmentStatus.confirmed,
                Appointment.starts_at >= day_start,
                Appointment.starts_at < day_end,
            )
            .order_by(Appointment.starts_at)
        )
    ).all()
    if staff.is_owner:
        lines = [f"Доброе утро! Сегодня {local_today.strftime('%d.%m')}."]
        if appts:
            lines.append(f"Твои записи ({len(appts)}):")
            for a in appts:
                t = a.starts_at.astimezone(tz).strftime("%H:%M")
                name = a.client.full_name or "клиент"
                phone = f" {a.client.phone}" if a.client.phone else ""
                lines.append(f"{t} {name}{phone} — {a.service.name}")
        else:
            lines.append("Твоих записей сегодня нет.")
        counts = (
            await session.execute(
                select(Staff.name, func.count(Appointment.id))
                .outerjoin(
                    Appointment,
                    (Appointment.staff_id == Staff.id)
                    & (Appointment.status == AppointmentStatus.confirmed)
                    & (Appointment.starts_at >= day_start)
                    & (Appointment.starts_at < day_end),
                )
                .where(Staff.business_id == business.id, Staff.is_active.is_(True))
                .group_by(Staff.id, Staff.name)
                .order_by(Staff.id)
            )
        ).all()
        team = ", ".join(f"{name} {cnt or 0}" for name, cnt in counts)
        lines.append(f"Команда сегодня: {team}.")
        return "\n".join(lines)
    if not appts:
        return "Доброе утро! Сегодня записей нет."
    lines = [f"Доброе утро! Сегодня {local_today.strftime('%d.%m')}, записей: {len(appts)}."]
    for a in appts:
        t = a.starts_at.astimezone(tz).strftime("%H:%M")
        name = a.client.full_name or "клиент"
        phone = f" {a.client.phone}" if a.client.phone else ""
        lines.append(f"{t} {name}{phone} — {a.service.name}")
    return "\n".join(lines)


async def _text_for_task(session, task: NotificationTask) -> str:
    business = await session.scalar(select(Business).where(Business.id == task.business_id))
    if business is None:
        return "Уведомление о записи."
    if task.type == NotificationType.day_digest:
        staff = await session.scalar(
            select(Staff).where(
                Staff.business_id == business.id,
                Staff.telegram_id == task.telegram_id,
            )
        )
        if staff is None:
            return "Доброе утро! Сегодня записей нет."
        return await _digest_text(session, business, staff)
    appt = None
    if task.appointment_id:
        appt = await session.scalar(
            select(Appointment).where(
                Appointment.id == task.appointment_id,
                Appointment.business_id == task.business_id,
            )
        )
    if appt is None:
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


DIGEST_HOUR = 8
DIGEST_WINDOW_HOURS = 4


async def schedule_daily_digests(now_utc: datetime) -> None:
    """Утром создаёт задачу дайджеста каждому активному мастеру с записями;
    владельцу — всегда, пока в салоне есть записи на сегодня."""
    async with async_session_factory() as session:
        businesses = (
            await session.scalars(select(Business).where(Business.is_active.is_(True)))
        ).all()
        for business in businesses:
            tz = ZoneInfo(business.timezone)
            local_now = now_utc.astimezone(tz)
            digest_local = datetime.combine(local_now.date(), time(DIGEST_HOUR, 0), tzinfo=tz)
            if local_now < digest_local or local_now >= digest_local + timedelta(
                hours=DIGEST_WINDOW_HOURS
            ):
                continue
            day_start, day_end = _day_bounds(business, local_now.date())
            staff_list = (
                await session.scalars(
                    select(Staff)
                    .where(Staff.business_id == business.id, Staff.is_active.is_(True))
                    .order_by(Staff.id)
                )
            ).all()
            if not staff_list:
                continue
            counts = (
                await session.execute(
                    select(Appointment.staff_id, func.count(Appointment.id))
                    .where(
                        Appointment.business_id == business.id,
                        Appointment.status == AppointmentStatus.confirmed,
                        Appointment.starts_at >= day_start,
                        Appointment.starts_at < day_end,
                    )
                    .group_by(Appointment.staff_id)
                )
            ).all()
            counts_by_staff = {sid: cnt for sid, cnt in counts}
            if not any(counts_by_staff.values()):
                continue
            for s in staff_list:
                mine = counts_by_staff.get(s.id, 0)
                if not s.is_owner and mine == 0:
                    continue
                exists = await session.scalar(
                    select(NotificationTask.id).where(
                        NotificationTask.business_id == business.id,
                        NotificationTask.type == NotificationType.day_digest,
                        NotificationTask.telegram_id == s.telegram_id,
                        NotificationTask.send_at >= day_start,
                        NotificationTask.send_at < day_end,
                    )
                )
                if exists is not None:
                    continue
                session.add(
                    NotificationTask(
                        business_id=business.id,
                        appointment_id=None,
                        recipient_type=RecipientType.master,
                        telegram_id=s.telegram_id,
                        type=NotificationType.day_digest,
                        send_at=now_utc,
                        status=NotificationStatus.pending,
                    )
                )
            await session.commit()


async def run_notification_loop(get_bot) -> None:
    while True:
        try:
            await schedule_daily_digests(datetime.now(timezone.utc))
            await process_due_notifications(get_bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("сбой цикла напоминаний")
        await asyncio.sleep(POLL_SECONDS)