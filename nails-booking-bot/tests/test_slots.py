from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models import Appointment, AppointmentStatus, Business, Client, DayWindow, Service
from app.services.slots import add_day_window, get_bookable_dates, get_day_windows, get_free_slots


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
    )
    session.add(b)
    await session.flush()
    return b


@pytest.fixture
async def service(session, business):
    s = Service(
        business_id=business.id,
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


@pytest.mark.asyncio
async def test_add_day_window(session, business):
    local_date = date(2027, 3, 10)
    local_time = time(10, 30)
    window = await add_day_window(session, business, local_date, local_time)
    assert window is not None
    assert window.date == local_date
    assert window.starts_at.tzinfo == timezone.utc


@pytest.mark.asyncio
async def test_get_free_slots_empty(session, business, service):
    local_date = date(2027, 3, 10)
    free = await get_free_slots(session, business, service, local_date)
    assert free == []


@pytest.mark.asyncio
async def test_get_free_slots_with_windows(session, business, service):
    local_date = date(2027, 3, 10)
    await add_day_window(session, business, local_date, time(10, 30))
    await add_day_window(session, business, local_date, time(14, 0))

    free = await get_free_slots(session, business, service, local_date)
    assert len(free) == 2


@pytest.mark.asyncio
async def test_get_free_slots_with_appointment(session, business, service, client):
    local_date = date(2027, 3, 10)
    await add_day_window(session, business, local_date, time(10, 30))
    await add_day_window(session, business, local_date, time(14, 0))

    tz = ZoneInfo(business.timezone)
    starts_at = datetime.combine(local_date, time(10, 30), tzinfo=tz).astimezone(timezone.utc)
    ends_at = starts_at + timedelta(minutes=90)

    appt = Appointment(
        business_id=business.id,
        client_id=client.id,
        service_id=service.id,
        starts_at=starts_at,
        ends_at=ends_at,
        status=AppointmentStatus.confirmed,
    )
    session.add(appt)
    await session.flush()

    free = await get_free_slots(session, business, service, local_date)
    assert len(free) == 1
    assert free[0].astimezone(tz).time() == time(14, 0)


@pytest.mark.asyncio
async def test_get_bookable_dates(session, business, service):
    today = date(2027, 3, 1)
    await add_day_window(session, business, date(2027, 3, 5), time(10, 0))
    await add_day_window(session, business, date(2027, 3, 10), time(14, 0))

    bookable = await get_bookable_dates(session, business, today)
    assert len(bookable) == 2
    assert date(2027, 3, 5) in bookable
    assert date(2027, 3, 10) in bookable


@pytest.mark.asyncio
async def test_get_day_windows_marks_busy(session, business, service, client):
    local_date = date(2027, 3, 10)
    await add_day_window(session, business, local_date, time(10, 30))
    await add_day_window(session, business, local_date, time(14, 0))

    tz = ZoneInfo(business.timezone)
    starts_at = datetime.combine(local_date, time(10, 30), tzinfo=tz).astimezone(timezone.utc)
    ends_at = starts_at + timedelta(minutes=90)

    appt = Appointment(
        business_id=business.id,
        client_id=client.id,
        service_id=service.id,
        starts_at=starts_at,
        ends_at=ends_at,
        status=AppointmentStatus.confirmed,
    )
    session.add(appt)
    await session.flush()

    windows = await get_day_windows(session, business, local_date, service)
    assert len(windows) == 2
    assert windows[0]["is_free"] is False
    assert windows[1]["is_free"] is True