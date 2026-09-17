from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models import Appointment, AppointmentStatus, Business, Client, Service, Staff
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


@pytest.mark.asyncio
async def test_add_day_window(session, business, staff):
    window = await add_day_window(session, business, staff, date(2027, 3, 10), time(10, 30))
    assert window is not None
    assert window.date == date(2027, 3, 10)
    assert window.starts_at.tzinfo == timezone.utc
    assert window.staff_id == staff.id


@pytest.mark.asyncio
async def test_get_free_slots_empty(session, business, staff, service):
    free = await get_free_slots(session, business, staff, service, date(2027, 3, 10))
    assert free == []


@pytest.mark.asyncio
async def test_get_free_slots_with_windows(session, business, staff, service):
    await add_day_window(session, business, staff, date(2027, 3, 10), time(10, 30))
    await add_day_window(session, business, staff, date(2027, 3, 10), time(14, 0))
    free = await get_free_slots(session, business, staff, service, date(2027, 3, 10))
    assert len(free) == 2


@pytest.mark.asyncio
async def test_other_staff_windows_not_visible(session, business, staff, service):
    other = Staff(business_id=business.id, name="Maria", telegram_id=2, is_owner=False)
    session.add(other)
    await session.flush()
    await add_day_window(session, business, other, date(2027, 3, 10), time(10, 30))
    free = await get_free_slots(session, business, staff, service, date(2027, 3, 10))
    assert free == []


@pytest.mark.asyncio
async def test_get_free_slots_with_appointment(session, business, staff, service, client):
    local_date = date(2027, 3, 10)
    await add_day_window(session, business, staff, local_date, time(10, 30))
    await add_day_window(session, business, staff, local_date, time(14, 0))

    tz = ZoneInfo(business.timezone)
    starts_at = datetime.combine(local_date, time(10, 30), tzinfo=tz).astimezone(timezone.utc)
    appt = Appointment(
        business_id=business.id,
        staff_id=staff.id,
        client_id=client.id,
        service_id=service.id,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(minutes=90),
        status=AppointmentStatus.confirmed,
    )
    session.add(appt)
    await session.flush()

    free = await get_free_slots(session, business, staff, service, local_date)
    assert len(free) == 1
    assert free[0].astimezone(tz).time() == time(14, 0)


@pytest.mark.asyncio
async def test_get_bookable_dates(session, business, staff, service):
    today = date(2027, 3, 1)
    await add_day_window(session, business, staff, date(2027, 3, 5), time(10, 0))
    await add_day_window(session, business, staff, date(2027, 3, 10), time(14, 0))
    bookable = await get_bookable_dates(session, business, staff, today)
    assert date(2027, 3, 5) in bookable
    assert date(2027, 3, 10) in bookable
    assert len(bookable) == 2


@pytest.mark.asyncio
async def test_get_day_windows_marks_busy(session, business, staff, service, client):
    local_date = date(2027, 3, 10)
    await add_day_window(session, business, staff, local_date, time(10, 30))
    await add_day_window(session, business, staff, local_date, time(14, 0))

    tz = ZoneInfo(business.timezone)
    starts_at = datetime.combine(local_date, time(10, 30), tzinfo=tz).astimezone(timezone.utc)
    appt = Appointment(
        business_id=business.id,
        staff_id=staff.id,
        client_id=client.id,
        service_id=service.id,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(minutes=90),
        status=AppointmentStatus.confirmed,
    )
    session.add(appt)
    await session.flush()

    windows = await get_day_windows(session, business, staff, local_date, service)
    assert len(windows) == 2
    assert windows[0]["is_free"] is False
    assert windows[0]["reason"] == "busy"
    assert windows[1]["is_free"] is True