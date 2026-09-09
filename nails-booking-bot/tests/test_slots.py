from datetime import date, datetime, time, timedelta, timezone

from app.models import Appointment, AppointmentStatus, Business, DateOverride, Service, TimeBlock, WorkSchedule
from app.services.formatting import format_price
from app.services.slots import compute_free_slots, get_free_slots

NOW = datetime(2026, 6, 1, 6, 0, tzinfo=timezone.utc)
DAY = date(2026, 6, 1)  # понедельник


def _weekly(start=time(10, 0), end=time(20, 0), working=True, weekday=0) -> WorkSchedule:
    return WorkSchedule(
        business_id=1,
        weekday=weekday,
        is_working=working,
        start_time=start,
        end_time=end,
    )


def _slots(**kwargs) -> list[datetime]:
    defaults = dict(
        tz_name="Europe/Minsk",
        slot_step_minutes=30,
        min_notice_minutes=60,
        duration_minutes=90,
        local_date=DAY,
        now_utc=NOW,
        weekly=_weekly(),
        override=None,
        blocks=[],
        appointments=[],
    )
    defaults.update(kwargs)
    return compute_free_slots(**defaults)


def test_format_price():
    assert format_price(2500) == "25.00 BYN"


def test_minsk_first_slot_is_10():
    slots = _slots(duration_minutes=60, slot_step_minutes=60)
    assert slots[0] == datetime(2026, 6, 1, 7, 0, tzinfo=timezone.utc)  # 10:00 Minsk


def test_berlin_same_local_time_different_utc():
    minsk = _slots(tz_name="Europe/Minsk", duration_minutes=60, slot_step_minutes=60)
    berlin = _slots(tz_name="Europe/Berlin", duration_minutes=60, slot_step_minutes=60)
    assert minsk[0] == datetime(2026, 6, 1, 7, 0, tzinfo=timezone.utc)
    assert berlin[0] == datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)


def test_time_block_removes_slot():
    block_start = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)  # 12:00 Minsk
    block_end = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)  # 13:00
    slots = _slots(
        duration_minutes=60,
        slot_step_minutes=60,
        blocks=[(block_start, block_end)],
    )
    assert block_start not in slots
    assert datetime(2026, 6, 1, 7, 0, tzinfo=timezone.utc) in slots


def test_appointment_removes_slot():
    taken = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)  # 11:00 Minsk
    slots = _slots(
        duration_minutes=60,
        slot_step_minutes=60,
        appointments=[(taken, taken + timedelta(hours=1))],
    )
    assert taken not in slots


def test_closed_override_no_slots():
    override = DateOverride(
        business_id=1,
        date=DAY,
        is_closed=True,
        start_time=None,
        end_time=None,
    )
    assert _slots(override=override) == []


def test_custom_hours_override():
    override = DateOverride(
        business_id=1,
        date=DAY,
        is_closed=False,
        start_time=time(11, 0),
        end_time=time(12, 0),
    )
    slots = _slots(duration_minutes=60, slot_step_minutes=15, override=override)
    assert slots == [datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)]


def test_weekend_without_override_empty():
    sunday = date(2026, 6, 7)
    slots = _slots(
        local_date=sunday,
        weekly=_weekly(working=False, start=None, end=None, weekday=6),
        now_utc=datetime(2026, 6, 7, 6, 0, tzinfo=timezone.utc),
    )
    assert slots == []


def test_dst_gap_skips_nonexistent_times():
    """29.03.2026 в Berlin 02:00–03:00 не существует."""
    day = date(2026, 3, 29)
    slots = compute_free_slots(
        tz_name="Europe/Berlin",
        slot_step_minutes=30,
        min_notice_minutes=0,
        duration_minutes=30,
        local_date=day,
        now_utc=datetime(2026, 3, 28, 0, 0, tzinfo=timezone.utc),
        weekly=_weekly(start=time(1, 0), end=time(5, 0), weekday=6),
        override=None,
        blocks=[],
        appointments=[],
    )
    local_hours = [(s.astimezone(timezone.utc), s) for s in slots]
    from zoneinfo import ZoneInfo

    berlin = ZoneInfo("Europe/Berlin")
    locals_ = [s.astimezone(berlin).strftime("%H:%M") for s in slots]
    assert "02:00" not in locals_
    assert "02:30" not in locals_
    assert "01:00" in locals_
    assert "03:00" in locals_
    assert local_hours  # слоты вообще есть


async def test_get_free_slots_ignores_canceled(session):
    business = Business(
        name="Слоты",
        bot_token="slot-token",
        owner_telegram_id=1,
        timezone="Europe/Minsk",
        slot_step_minutes=60,
        min_notice_minutes=0,
        max_booking_days=30,
    )
    session.add(business)
    await session.flush()
    session.add(
        WorkSchedule(
            business_id=business.id,
            weekday=DAY.weekday(),
            is_working=True,
            start_time=time(10, 0),
            end_time=time(12, 0),
        )
    )
    service = Service(
        business_id=business.id,
        name="Маникюр",
        price_minor=2500,
        duration_minutes=60,
        is_active=True,
        position=0,
    )
    from app.models import Client

    client = Client(business_id=business.id, telegram_id=2)
    session.add_all([service, client])
    await session.flush()
    session.add(
        Appointment(
            business_id=business.id,
            client_id=client.id,
            service_id=service.id,
            starts_at=datetime(2026, 6, 1, 7, 0, tzinfo=timezone.utc),
            ends_at=datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc),
            status=AppointmentStatus.canceled,
        )
    )
    session.add(
        TimeBlock(
            business_id=business.id,
            starts_at=datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc),
            ends_at=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
            reason="обед",
        )
    )
    await session.commit()

    slots = await get_free_slots(
        session,
        business,
        service,
        DAY,
        now_utc=datetime(2026, 6, 1, 5, 0, tzinfo=timezone.utc),
    )
    assert datetime(2026, 6, 1, 7, 0, tzinfo=timezone.utc) in slots
    assert datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc) not in slots
