from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from zoneinfo import ZoneInfo

from app.models import Appointment, AppointmentStatus, Business, DayWindow, Service


def _exists_local(local_date: date, local_time: time, tz: ZoneInfo) -> datetime | None:
    """Собирает aware-datetime; пропускает несуществующий момент (разрыв DST)."""
    naive = datetime.combine(local_date, local_time)
    aware = naive.replace(tzinfo=tz)
    back = aware.astimezone(timezone.utc).astimezone(tz)
    if back.replace(tzinfo=None) != naive:
        return None
    return aware


async def get_day_windows(
    session: AsyncSession,
    business: Business,
    staff,
    local_date: date,
    service: Service | None = None,
) -> list[dict]:
    """Все окошки дня с пометками свободно/занято.

    Возвращает список словарей:
    {
        "window": DayWindow,
        "starts_at": datetime (UTC),
        "is_free": bool,
        "reason": None | "past" | "busy",
    }
    """
    tz = ZoneInfo(business.timezone)

    day_start_utc = datetime.combine(local_date, time.min, tzinfo=tz).astimezone(timezone.utc)
    day_end_utc = datetime.combine(
        local_date + timedelta(days=1), time.min, tzinfo=tz
    ).astimezone(timezone.utc)

    windows = (
        await session.scalars(
            select(DayWindow)
            .where(
                DayWindow.business_id == business.id,
                DayWindow.staff_id == staff.id,
                DayWindow.starts_at >= day_start_utc,
                DayWindow.starts_at < day_end_utc,
            )
            .order_by(DayWindow.starts_at)
        )
    ).all()

    appts = (
        await session.scalars(
            select(Appointment).where(
                Appointment.business_id == business.id,
                Appointment.status == AppointmentStatus.confirmed,
                Appointment.starts_at < day_end_utc,
                Appointment.ends_at > day_start_utc,
            )
        )
    ).all()

    duration_minutes = service.duration_minutes if service else 60
    now_utc = datetime.now(timezone.utc)
    min_notice = business.min_notice_minutes

    result = []
    for w in windows:
        slot_start = w.starts_at
        slot_end = slot_start + timedelta(minutes=duration_minutes)

        if slot_start < now_utc + timedelta(minutes=min_notice):
            is_free = False
            reason = "past"
        else:
            is_free = True
            reason = None
            for a in appts:
                if a.starts_at < slot_end and a.ends_at > slot_start:
                    is_free = False
                    reason = "busy"
                    break

        result.append(
            {
                "window": w,
                "starts_at": slot_start,
                "is_free": is_free,
                "reason": reason,
            }
        )

    return result


async def get_free_slots(
    session: AsyncSession,
    business: Business,
    staff,
    service: Service,
    local_date: date,
    now_utc: datetime | None = None,
) -> list[datetime]:
    """Свободные окошки для записи (совместимый API)."""
    windows = await get_day_windows(session, business, staff, local_date, service)
    return [w["starts_at"] for w in windows if w["is_free"]]


async def get_bookable_dates(
    session: AsyncSession,
    business: Business,
    staff,
    today: date,
) -> list[date]:
    """Даты горизонта, где есть хотя бы одно свободное окошко."""
    last = today + timedelta(days=business.max_booking_days - 1)
    windows = (
        await session.scalars(
            select(DayWindow).where(
                DayWindow.business_id == business.id,
                DayWindow.staff_id == staff.id,
                DayWindow.date >= today,
                DayWindow.date <= last,
            )
        )
    ).all()
    bookable: list[date] = []
    for d in sorted({w.date for w in windows}):
        items = await get_day_windows(session, business, staff, d, None)
        if any(item["is_free"] for item in items):
            bookable.append(d)
    return bookable


async def add_day_window(
    session: AsyncSession,
    business: Business,
    local_date: date,
    local_time: time,
) -> DayWindow | None:
    """Добавить окошко. None, если момента не существует (разрыв DST)."""
    tz = ZoneInfo(business.timezone)
    starts_at = _exists_local(local_date, local_time, tz)
    if starts_at is None:
        return None

    window = DayWindow(
        business_id=business.id,
        date=local_date,
        starts_at=starts_at.astimezone(timezone.utc),
    )
    session.add(window)
    await session.commit()
    return window


async def remove_day_window(session: AsyncSession, window_id: int, business_id: int) -> bool:
    """Удалить окошко. True, если удалили."""
    window = await session.scalar(
        select(DayWindow).where(
            DayWindow.id == window_id,
            DayWindow.business_id == business_id,
        )
    )
    if window is None:
        return False
    await session.delete(window)
    await session.commit()
    return True