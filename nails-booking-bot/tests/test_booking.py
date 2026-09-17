from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import select
from zoneinfo import ZoneInfo

from app.models import (
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
from app.services.booking import (
    cancel_appointment,
    create_appointment,
    reschedule_appointment,
)


@pytest.fixture
async def business(session):
    b = Business(
        name="Test",
        bot_token="test",
        owner_telegram_id=1,
        timezone="Europe/Minsk",
        slot_step_minutes=15,
        min_notice_minutes=60,
        max_booking_days=30,
        reminder_offsets_minutes=[1440, 120],
    )
    session.add(b)
    await session.flush()
    return b


@pytest.fixture
async def staff(session, business):
    s = Staff(
        business_id=business.id,
        name="Anna",
        telegram_id=business.owner_telegram_id,
        is_owner=True,
    )
    session.add(s)
    await session.flush()
    return s


@pytest.fixture
async def service(session, business, staff):
    s = Service(
        business_id=business.id,
        staff_id=staff.id,
        name="Маникюр",
        price_minor=2500,
        duration_minutes=90,
        is_active=True,
    )
    session.add(s)
    await session.flush()
    return s


@pytest.fixture
async def client(session, business):
    c = Client(business_id=business.id, telegram_id=123, full_name="Ирина")
    session.add(c)
    await session.flush()
    return c


def _at(day: date, t: time, tz_name: str) -> datetime:
    return datetime.combine(day, t, tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)


@pytest.mark.asyncio
async def test_create_appointment_builds_notifications(session, business, staff, service, client):
    starts_at = _at(date(2027, 3, 10), time(10, 0), business.timezone)
    appt = await create_appointment(session, business, staff, client, service, starts_at)

    assert appt.id is not None
    assert appt.status == AppointmentStatus.confirmed
    assert appt.staff_id == staff.id
    assert appt.ends_at == starts_at + timedelta(minutes=service.duration_minutes)

    tasks = (
        await session.scalars(
            select(NotificationTask).where(NotificationTask.appointment_id == appt.id)
        )
    ).all()
    assert len(tasks) == 5
    new_booking = [t for t in tasks if t.type == NotificationType.new_booking]
    assert len(new_booking) == 1
    assert new_booking[0].recipient_type == RecipientType.master
    assert new_booking[0].card_text is not None
    assert "Маникюр" in new_booking[0].card_text
    reminders = [t for t in tasks if t.type == NotificationType.reminder]
    assert len(reminders) == 4
    assert all(t.status == NotificationStatus.pending for t in reminders)


@pytest.mark.asyncio
async def test_cancel_appointment_kills_reminders(session, business, staff, service, client):
    starts_at = _at(date(2027, 3, 10), time(10, 0), business.timezone)
    appt = await create_appointment(session, business, staff, client, service, starts_at)

    await cancel_appointment(session, business, appt, business.owner_telegram_id)
    assert appt.status == AppointmentStatus.canceled

    tasks = (
        await session.scalars(
            select(NotificationTask).where(NotificationTask.appointment_id == appt.id)
        )
    ).all()
    reminders = [t for t in tasks if t.type == NotificationType.reminder]
    assert reminders
    assert all(t.status == NotificationStatus.canceled for t in reminders)
    canceled = [t for t in tasks if t.type == NotificationType.canceled]
    assert len(canceled) == 1
    assert canceled[0].telegram_id == business.owner_telegram_id


@pytest.mark.asyncio
async def test_reschedule_recreates_reminders(session, business, staff, service, client):
    starts_at = _at(date(2027, 3, 10), time(10, 0), business.timezone)
    appt = await create_appointment(session, business, staff, client, service, starts_at)
    new_starts = _at(date(2027, 3, 11), time(12, 0), business.timezone)

    appt = await reschedule_appointment(session, business, appt, client, service, new_starts)
    assert appt.starts_at == new_starts
    assert appt.status == AppointmentStatus.confirmed

    tasks = (
        await session.scalars(
            select(NotificationTask).where(NotificationTask.appointment_id == appt.id)
        )
    ).all()
    reminders = [t for t in tasks if t.type == NotificationType.reminder]
    pending = [t for t in reminders if t.status == NotificationStatus.pending]
    canceled = [t for t in reminders if t.status == NotificationStatus.canceled]
    assert len(pending) == 4
    assert len(canceled) == 4
    rescheduled = [t for t in tasks if t.type == NotificationType.rescheduled]
    assert len(rescheduled) == 2