from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import SlotCB, dates_kb
from app.models import Business, Service, Staff
from app.services.slots import get_bookable_dates, get_day_windows


async def ask_dates(
    message,
    session: AsyncSession,
    business: Business,
    staff: Staff,
    state: FSMContext,
) -> bool:
    today = datetime.now(ZoneInfo(business.timezone)).date()
    dates = await get_bookable_dates(session, business, staff, today)
    if not dates:
        await message.answer("Пока нет доступных дат для записи.")
        await state.clear()
        return False
    await message.answer("Выберите дату:", reply_markup=dates_kb(dates))
    return True


async def ask_slots(
    message,
    session: AsyncSession,
    business: Business,
    staff: Staff,
    service: Service,
    local_date: date,
    state: FSMContext,
) -> bool:
    windows = await get_day_windows(session, business, staff, local_date, service)
    visible = [w for w in windows if w["reason"] != "past"]
    if not visible:
        await message.answer("На эту дату свободных окошек нет. Выберите другую дату.")
        return False
    tz = ZoneInfo(business.timezone)
    builder = InlineKeyboardBuilder()
    for w in visible:
        label = w["starts_at"].astimezone(tz).strftime("%H:%M")
        if w["is_free"]:
            builder.button(text=label, callback_data=SlotCB(ts=int(w["starts_at"].timestamp())).pack())
        else:
            builder.button(text=f"{label} 🔒", callback_data="slot:locked")
    builder.adjust(4)
    await message.answer(
        f"Свободное время на {local_date.strftime('%d.%m.%Y')}:",
        reply_markup=builder.as_markup(),
    )
    return True


def parse_price_minor(text: str) -> int | None:
    raw = text.strip().replace(" ", "").replace(",", ".")
    try:
        value = round(float(raw) * 100)
    except ValueError:
        return None
    if value < 0:
        return None
    return int(value)


def parse_hhmm(text: str) -> time | None:
    parts = text.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return time(hour, minute)
    return None


def parse_time_range(text: str) -> tuple[time, time] | None:
    cleaned = text.strip().replace("–", "-").replace("—", "-")
    parts = [p.strip() for p in cleaned.split("-")]
    if len(parts) != 2:
        return None
    start, end = parse_hhmm(parts[0]), parse_hhmm(parts[1])
    if start is None or end is None or start >= end:
        return None
    return start, end


def parse_ru_date(text: str) -> date | None:
    raw = text.strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse_date_range(text: str) -> tuple[date, date] | None:
    cleaned = text.strip().replace("–", "-").replace("—", "-")
    if "-" in cleaned and cleaned.count(".") >= 2:
        parts = [p.strip() for p in cleaned.split("-")]
        if len(parts) == 2:
            a, b = parse_ru_date(parts[0]), parse_ru_date(parts[1])
            if a and b and a <= b:
                return a, b
    one = parse_ru_date(cleaned)
    if one:
        return one, one
    return None