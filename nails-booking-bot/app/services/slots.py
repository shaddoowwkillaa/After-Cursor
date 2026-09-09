from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Appointment,
    AppointmentStatus,
    Business,
    DateOverride,
    Service,
    TimeBlock,
    WorkSchedule,
)


def _exists_local(local_date: date, local_time: time, tz: ZoneInfo) -> datetime | None:
    """Собирает aware-datetime; пропускает несуществующий момент (разрыв DST)."""
    naive = datetime.combine(local_date, local_time)
    aware = naive.replace(tzinfo=tz)
    back = aware.astimezone(timezone.utc).astimezone(tz)
    if back.replace(tzinfo=None) != naive:
        return None
    return aware


def _subtract_busy(
    free: list[tuple[datetime, datetime]],
    busy_start: datetime,
    busy_end: datetime,
) -> list[tuple[datetime, datetime]]:
    result: list[tuple[datetime, datetime]] = []
    for f_start, f_end in free:
        if busy_end <= f_start or busy_start >= f_end:
            result.append((f_start, f_end))
            continue
        if f_start < busy_start:
            result.append((f_start, min(busy_start, f_end)))
        if busy_end < f_end:
            result.append((max(busy_end, f_start), f_end))
    return [(a, b) for a, b in result if a < b]


def resolve_local_interval(
    local_date: date,
    weekly: WorkSchedule | None,
    override: DateOverride | None,
) -> tuple[time, time] | None:
    """Локальные часы работы на дату или None, если выходной."""
    if override is not None:
        if override.is_closed:
            return None
        if override.start_time is not None and override.end_time is not None:
            return override.start_time, override.end_time
    if weekly is None or not weekly.is_working:
        return None
    if weekly.start_time is None or weekly.end_time is None:
        return None
    return weekly.start_time, weekly.end_time


def compute_free_slots(
    *,
    tz_name: str,
    slot_step_minutes: int,
    min_notice_minutes: int,
    duration_minutes: int,
    local_date: date,
    now_utc: datetime,
    weekly: WorkSchedule | None,
    override: DateOverride | None,
    blocks: list[tuple[datetime, datetime]],
    appointments: list[tuple[datetime, datetime]],
) -> list[datetime]:
    """Возвращает старты свободных слотов в UTC."""
    tz = ZoneInfo(tz_name)
    interval = resolve_local_interval(local_date, weekly, override)
    if interval is None:
        return []

    start_local, end_local = interval
    start_dt = _exists_local(local_date, start_local, tz)
    end_dt = _exists_local(local_date, end_local, tz)
    if start_dt is None or end_dt is None or start_dt >= end_dt:
        return []

    start_utc = start_dt.astimezone(timezone.utc)
    end_utc = end_dt.astimezone(timezone.utc)
    free = [(start_utc, end_utc)]
    for b0, b1 in list(blocks) + list(appointments):
        free = _subtract_busy(free, b0.astimezone(timezone.utc), b1.astimezone(timezone.utc))

    duration = timedelta(minutes=duration_minutes)
    step = timedelta(minutes=slot_step_minutes)
    min_start = now_utc.astimezone(timezone.utc) + timedelta(minutes=min_notice_minutes)

    slots: list[datetime] = []
    for f0, f1 in free:
        cursor = f0
        while cursor + duration <= f1:
            local_cursor = cursor.astimezone(tz)
            reconstituted = _exists_local(local_cursor.date(), local_cursor.time().replace(microsecond=0), tz)
            if reconstituted is None:
                cursor += step
                continue
            if cursor >= min_start:
                slots.append(cursor)
            cursor += step
    return slots


async def get_weekly_map(session: AsyncSession, business_id: int) -> dict[int, WorkSchedule]:
    rows = (
        await session.scalars(
            select(WorkSchedule).where(WorkSchedule.business_id == business_id)
        )
    ).all()
    return {row.weekday: row for row in rows}


async def get_override(
    session: AsyncSession, business_id: int, local_date: date
) -> DateOverride | None:
    return await session.scalar(
        select(DateOverride).where(
            DateOverride.business_id == business_id,
            DateOverride.date == local_date,
        )
    )


async def get_bookable_dates(
    session: AsyncSession,
    business: Business,
    today: date,
) -> list[date]:
    weekly = await get_weekly_map(session, business.id)
    last = today + timedelta(days=business.max_booking_days - 1)
    overrides = (
        await session.scalars(
            select(DateOverride).where(
                DateOverride.business_id == business.id,
                DateOverride.date >= today,
                DateOverride.date <= last,
            )
        )
    ).all()
    ov_map = {o.date: o for o in overrides}

    dates: list[date] = []
    for i in range(business.max_booking_days):
        d = today + timedelta(days=i)
        if resolve_local_interval(d, weekly.get(d.weekday()), ov_map.get(d)) is not None:
            dates.append(d)
    return dates


async def get_free_slots(
    session: AsyncSession,
    business: Business,
    service: Service,
    local_date: date,
    now_utc: datetime | None = None,
) -> list[datetime]:
    now_utc = now_utc or datetime.now(timezone.utc)
    tz = ZoneInfo(business.timezone)
    weekly = await get_weekly_map(session, business.id)
    override = await get_override(session, business.id, local_date)

    day_start = datetime.combine(local_date, time.min, tzinfo=tz).astimezone(timezone.utc)
    day_end = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=tz).astimezone(
        timezone.utc
    )

    blocks = (
        await session.scalars(
            select(TimeBlock).where(
                TimeBlock.business_id == business.id,
                TimeBlock.starts_at < day_end,
                TimeBlock.ends_at > day_start,
            )
        )
    ).all()
    appts = (
        await session.scalars(
            select(Appointment).where(
                Appointment.business_id == business.id,
                Appointment.status == AppointmentStatus.confirmed,
                Appointment.starts_at < day_end,
                Appointment.ends_at > day_start,
            )
        )
    ).all()

    return compute_free_slots(
        tz_name=business.timezone,
        slot_step_minutes=business.slot_step_minutes,
        min_notice_minutes=business.min_notice_minutes,
        duration_minutes=service.duration_minutes,
        local_date=local_date,
        now_utc=now_utc,
        weekly=weekly.get(local_date.weekday()),
        override=override,
        blocks=[(b.starts_at, b.ends_at) for b in blocks],
        appointments=[(a.starts_at, a.ends_at) for a in appts],
    )
