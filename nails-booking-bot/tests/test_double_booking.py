import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Appointment, AppointmentStatus, Business, Client, Service, Staff
from app.services.booking import is_slot_conflict


async def _seed(session):
    business = Business(
        name="Тест",
        bot_token="test-token-double-booking",
        owner_telegram_id=100,
        timezone="Europe/Minsk",
    )
    session.add(business)
    await session.flush()

    staff = Staff(
        business_id=business.id,
        name="Анна",
        telegram_id=business.owner_telegram_id,
        is_owner=True,
    )
    session.add(staff)
    await session.flush()

    client = Client(business_id=business.id, telegram_id=200, full_name="Клиент")
    service = Service(
        business_id=business.id,
        staff_id=staff.id,
        name="Маникюр",
        price_minor=2500,
        duration_minutes=90,
        is_active=True,
        position=0,
    )
    session.add_all([client, service])
    await session.commit()
    await session.refresh(business)
    await session.refresh(staff)
    await session.refresh(client)
    await session.refresh(service)
    return business, client, service, staff


def _utc(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, 1, hour, minute, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_concurrent_overlapping_inserts(session_factory, session):
    business, client, service, staff = await _seed(session)
    start = _utc(12)
    end = start + timedelta(minutes=90)

    async def insert_one() -> None:
        async with session_factory() as s:
            s.add(
                Appointment(
                    business_id=business.id,
                    staff_id=staff.id,
                    client_id=client.id,
                    service_id=service.id,
                    starts_at=start,
                    ends_at=end,
                    status=AppointmentStatus.confirmed,
                )
            )
            await s.commit()

    results = await asyncio.gather(insert_one(), insert_one(), return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    oks = [r for r in results if r is None]

    assert len(oks) == 1
    assert len(errors) == 1
    assert is_slot_conflict(errors[0])


@pytest.mark.asyncio
async def test_adjacent_slots_do_not_conflict(session):
    business, client, service, staff = await _seed(session)
    first_start = _utc(12)
    first_end = _utc(13, 30)
    second_start = _utc(13, 30)
    second_end = _utc(15)

    session.add(
        Appointment(
            business_id=business.id,
            staff_id=staff.id,
            client_id=client.id,
            service_id=service.id,
            starts_at=first_start,
            ends_at=first_end,
            status=AppointmentStatus.confirmed,
        )
    )
    session.add(
        Appointment(
            business_id=business.id,
            staff_id=staff.id,
            client_id=client.id,
            service_id=service.id,
            starts_at=second_start,
            ends_at=second_end,
            status=AppointmentStatus.confirmed,
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_canceled_does_not_block_slot(session):
    business, client, service, staff = await _seed(session)
    start = _utc(12)
    end = _utc(13, 30)

    session.add(
        Appointment(
            business_id=business.id,
            staff_id=staff.id,
            client_id=client.id,
            service_id=service.id,
            starts_at=start,
            ends_at=end,
            status=AppointmentStatus.canceled,
        )
    )
    await session.commit()

    session.add(
        Appointment(
            business_id=business.id,
            staff_id=staff.id,
            client_id=client.id,
            service_id=service.id,
            starts_at=start,
            ends_at=end,
            status=AppointmentStatus.confirmed,
        )
    )
    await session.commit()