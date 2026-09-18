from datetime import datetime, timedelta, timezone

import asyncpg
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Appointment,
    AppointmentStatus,
    Business,
    Client,
    NotificationStatus,
    NotificationTask,
    NotificationType,
    RecipientType,
    Service,
    Staff,
)
from app.services.formatting import appointment_card

SLOT_TAKEN_MESSAGE = "Это время только что заняли, выберите другое."


def is_slot_conflict(exc: BaseException) -> bool:
    """True, если база не дала создать запись, потому что слот занят."""
    if isinstance(exc, IntegrityError):
        return True
    orig = getattr(exc, "orig", None)
    if isinstance(orig, asyncpg.exceptions.DeadlockDetectedError):
        return True
    return "DeadlockDetectedError" in str(exc)


async def slot_taken(
    session: AsyncSession,
    business_id: int,
    starts_at: datetime,
    ends_at: datetime,
    exclude_id: int | None = None,
) -> bool:
    stmt = select(Appointment.id).where(
        Appointment.business_id == business_id,
        Appointment.status == AppointmentStatus.confirmed,
        Appointment.starts_at < ends_at,
        Appointment.ends_at > starts_at,
    )
    if exclude_id is not None:
        stmt = stmt.where(Appointment.id != exclude_id)
    return await session.scalar(stmt) is not None


def _add_reminders(
    session: AsyncSession,
    business: Business,
    appointment: Appointment,
    client: Client,
    master_telegram_id: int,
    now: datetime,
) -> None:
    """Создаёт напоминания клиенту и мастеру записи по каждому офсету салона."""
    offsets = business.reminder_offsets_minutes or []
    recipients = [
        (RecipientType.client, client.telegram_id),
        (RecipientType.master, master_telegram_id),
    ]
    for offset in offsets:
        send_at = appointment.starts_at - timedelta(minutes=int(offset))
        if send_at <= now:
            continue
        for recipient_type, telegram_id in recipients:
            session.add(
                NotificationTask(
                    business_id=business.id,
                    appointment_id=appointment.id,
                    recipient_type=recipient_type,
                    telegram_id=telegram_id,
                    type=NotificationType.reminder,
                    send_at=send_at,
                    status=NotificationStatus.pending,
                )
            )


async def cancel_pending_reminders(session: AsyncSession, appointment_id: int) -> None:
    await session.execute(
        update(NotificationTask)
        .where(
            NotificationTask.appointment_id == appointment_id,
            NotificationTask.status == NotificationStatus.pending,
            NotificationTask.type == NotificationType.reminder,
        )
        .values(status=NotificationStatus.canceled)
    )


async def create_appointment(
    session: AsyncSession,
    business: Business,
    staff,
    client: Client,
    service: Service,
    starts_at: datetime,
) -> Appointment:
    now = datetime.now(timezone.utc)
    ends_at = starts_at + timedelta(minutes=service.duration_minutes)
    appointment = Appointment(
        business_id=business.id,
        staff_id=staff.id,
        client_id=client.id,
        service_id=service.id,
        starts_at=starts_at,
        ends_at=ends_at,
        status=AppointmentStatus.confirmed,
    )
    session.add(appointment)
    await session.flush()

    session.add(
        NotificationTask(
            business_id=business.id,
            appointment_id=appointment.id,
            recipient_type=RecipientType.master,
            telegram_id=staff.telegram_id,
            type=NotificationType.new_booking,
            send_at=now,
            status=NotificationStatus.pending,
            card_text=appointment_card(appointment, business, service),
        )
    )
    _add_reminders(session, business, appointment, client, staff.telegram_id, now)
    await session.commit()
    return appointment


async def cancel_appointment(
    session: AsyncSession,
    business: Business,
    appointment: Appointment,
    notify_telegram_id: int,
) -> None:
    now = datetime.now(timezone.utc)
    appointment.status = AppointmentStatus.canceled
    await cancel_pending_reminders(session, appointment.id)
    recipient_type = (
        RecipientType.client
        if notify_telegram_id == appointment.client.telegram_id
        else RecipientType.master
    )
    session.add(
        NotificationTask(
            business_id=business.id,
            appointment_id=appointment.id,
            recipient_type=recipient_type,
            telegram_id=notify_telegram_id,
            type=NotificationType.canceled,
            send_at=now,
            status=NotificationStatus.pending,
            card_text=appointment_card(appointment, business, appointment.service),
        )
    )
    await session.commit()


async def complete_appointment(session: AsyncSession, appointment: Appointment) -> None:
    appointment.status = AppointmentStatus.completed
    await session.execute(
        update(NotificationTask)
        .where(
            NotificationTask.appointment_id == appointment.id,
            NotificationTask.status == NotificationStatus.pending,
        )
        .values(status=NotificationStatus.canceled)
    )
    await session.commit()


async def reschedule_appointment(
    session: AsyncSession,
    business: Business,
    appointment: Appointment,
    client: Client,
    service: Service,
    new_starts_at: datetime,
) -> Appointment:
    now = datetime.now(timezone.utc)
    appointment.starts_at = new_starts_at
    appointment.ends_at = new_starts_at + timedelta(minutes=service.duration_minutes)
    appointment.status = AppointmentStatus.confirmed
    await session.flush()
    await cancel_pending_reminders(session, appointment.id)
    staff_tid = await _staff_telegram(session, appointment, business)
    _add_reminders(session, business, appointment, client, staff_tid, now)
    for telegram_id, recipient in (
        (staff_tid, RecipientType.master),
        (client.telegram_id, RecipientType.client),
    ):
        session.add(
            NotificationTask(
                business_id=business.id,
                appointment_id=appointment.id,
                recipient_type=recipient,
                telegram_id=telegram_id,
                type=NotificationType.rescheduled,
                send_at=now,
                status=NotificationStatus.pending,
                card_text=appointment_card(appointment, business, service),
            )
        )
    await session.commit()
    return appointment

async def _staff_telegram(session, appointment: Appointment, business: Business) -> int:
    staff = await session.scalar(select(Staff).where(Staff.id == appointment.staff_id))
    return staff.telegram_id if staff is not None else business.owner_telegram_id