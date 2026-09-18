from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select, func
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.flow import ask_dates, ask_slots, parse_price_minor, parse_ru_date
from app.bot.keyboards import (
    ApptActCB,
    ClientCB,
    DateCB,
    SlotCB,
    SvcActCB,
    appointment_actions_kb,
    dates_kb,
    master_days_kb,
    master_main_kb,
)
from app.models import (
    Appointment,
    AppointmentStatus,
    Business,
    Client,
    DayWindow,
    Service,
    Staff,
)
from app.services.booking import (
    SLOT_TAKEN_MESSAGE,
    cancel_appointment,
    complete_appointment,
    is_slot_conflict,
    reschedule_appointment,
)
from app.services.formatting import WEEKDAYS_RU, appointment_card, format_price
from app.services.slots import get_day_windows

router = Router()
router.message.filter(RoleFilter("master"))
router.callback_query.filter(RoleFilter("master"))


async def _current_staff(session, business, telegram_id: int) -> Staff | None:
    """Staff по telegram_id; None, если человека нет в команде или он отключён."""
    staff = await session.scalar(
        select(Staff).where(
            Staff.business_id == business.id,
            Staff.telegram_id == telegram_id,
        )
    )
    if staff is not None:
        return staff if staff.is_active else None
    return await session.scalar(
        select(Staff).where(
            Staff.business_id == business.id,
            Staff.is_owner.is_(True),
            Staff.is_active.is_(True),
        )
    )

async def _is_owner(session, business, telegram_id: int) -> bool:
    staff = await _current_staff(session, business, telegram_id)
    return staff is not None and staff.is_owner


async def _main_kb(session, business, telegram_id: int):
    return master_main_kb(await _is_owner(session, business, telegram_id))


class MasterFSM(StatesGroup):
    pick_day = State()
    svc_name = State()
    svc_price = State()
    svc_duration = State()
    svc_edit = State()
    hours_value = State()
    close_dates = State()
    open_date = State()
    open_hours = State()
    block_date = State()
    block_hours = State()
    move_date = State()
    move_slot = State()
    win_date = State()
    win_times = State()
    win_del_date = State()
    set_name = State()
    set_rem = State()
    staff_name = State()
    staff_tid = State()


def _tz(business: Business) -> ZoneInfo:
    return ZoneInfo(business.timezone)


@router.message(CommandStart())
async def start(message: Message, business: Business, session: AsyncSession, state: FSMContext):
    await state.clear()
    await message.answer(
        f"Кабинет мастера «{business.name}».",
        reply_markup=await _main_kb(session, business, message.from_user.id),
    )


@router.message(F.text == "Отмена")
async def cancel_flow(message: Message, session: AsyncSession, business: Business, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=await _main_kb(session, business, message.from_user.id))


# --- записи ---


@router.message(F.text == "Записи")
async def appointments_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Записи за какой день?", reply_markup=master_days_kb())


@router.callback_query(F.data == "md:today")
async def appts_today(
    callback: CallbackQuery,
    session: AsyncSession,
    business: Business,
):
    staff = await _current_staff(session, business, callback.from_user.id)
    if staff is None:
        await callback.answer("Нет доступа", show_alert=True)
        return
    day = datetime.now(_tz(business)).date()
    await _send_day_appts(callback.message, session, business, staff, day)
    await callback.answer()


@router.callback_query(F.data == "md:tomorrow")
async def appts_tomorrow(
    callback: CallbackQuery,
    session: AsyncSession,
    business: Business,
):
    staff = await _current_staff(session, business, callback.from_user.id)
    if staff is None:
        await callback.answer("Нет доступа", show_alert=True)
        return
    day = datetime.now(_tz(business)).date() + timedelta(days=1)
    await _send_day_appts(callback.message, session, business, staff, day)
    await callback.answer()


@router.callback_query(F.data == "md:pick")
async def appts_pick(
    callback: CallbackQuery,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    tz = _tz(business)
    today = datetime.now(tz).date()
    dates = [today + timedelta(days=i) for i in range(business.max_booking_days)]
    await state.set_state(MasterFSM.pick_day)
    await callback.message.answer("Записи за какой день смотреть?", reply_markup=dates_kb(dates))
    await callback.answer()


@router.callback_query(MasterFSM.pick_day, DateCB.filter())
async def appts_picked(
    callback: CallbackQuery,
    callback_data: DateCB,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    staff = await _current_staff(session, business, callback.from_user.id)
    if staff is None:
        await state.clear()
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    day = datetime.strptime(callback_data.d, "%Y-%m-%d").date()
    await _send_day_appts(callback.message, session, business, staff, day)
    await callback.answer()


async def _send_day_appts(message, session, business: Business, staff: Staff, day):
    tz = _tz(business)
    start = datetime.combine(day, time.min, tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)
    appts = (
        await session.scalars(
            select(Appointment)
            .where(
                Appointment.business_id == business.id,
                Appointment.staff_id == staff.id,
                Appointment.starts_at >= start,
                Appointment.starts_at < end,
                Appointment.status != AppointmentStatus.canceled,
            )
            .order_by(Appointment.starts_at)
        )
    ).all()
    windows = (
        await session.scalars(
            select(DayWindow)
            .where(
                DayWindow.business_id == business.id,
                DayWindow.staff_id == staff.id,
                DayWindow.date == day,
            )
            .order_by(DayWindow.starts_at)
        )
    ).all()
    booked = {a.starts_at for a in appts}

    if not appts and not windows:
        await message.answer(f"На {day.strftime('%d.%m.%Y')} записей и окошек нет.")
        return

    if appts:
        await message.answer(f"Записи на {day.strftime('%d.%m.%Y')}:")
        for appt in appts:
            client_name = appt.client.full_name or str(appt.client.telegram_id)
            phone = appt.client.phone or "—"
            text = (
                appointment_card(appt, business, appt.service)
                + f"\nКлиент: {client_name}\nТелефон: {phone}"
            )
            kb = (
                appointment_actions_kb(appt.id, for_master=True)
                if appt.status == AppointmentStatus.confirmed
                else None
            )
            await message.answer(text, reply_markup=kb)
    else:
        await message.answer(f"На {day.strftime('%d.%m.%Y')} записей нет.")

    if windows:
        parts = []
        for w in windows:
            label = w.starts_at.astimezone(tz).strftime("%H:%M")
            parts.append(f"{label} 🔒" if w.starts_at in booked else label)
        await message.answer("Окошки дня: " + " · ".join(parts))


@router.callback_query(ApptActCB.filter(F.act == "c"))
async def master_cancel(
    callback: CallbackQuery,
    callback_data: ApptActCB,
    session: AsyncSession,
    business: Business,
):
    staff = await _current_staff(session, business, callback.from_user.id)
    appt = await _staff_appt(session, business.id, staff.id if staff else 0, callback_data.id)
    if appt is None:
        await callback.answer("Не найдено", show_alert=True)
        return
    await cancel_appointment(session, business, appt, appt.client.telegram_id)
    await callback.message.answer("Запись отменена, клиент получит уведомление.")
    await callback.answer()


@router.callback_query(ApptActCB.filter(F.act == "d"))
async def master_done(
    callback: CallbackQuery,
    callback_data: ApptActCB,
    session: AsyncSession,
    business: Business,
):
    staff = await _current_staff(session, business, callback.from_user.id)
    appt = await _staff_appt(session, business.id, staff.id if staff else 0, callback_data.id)
    if appt is None:
        await callback.answer("Не найдено", show_alert=True)
        return
    await complete_appointment(session, appt)
    await callback.message.answer("Отмечено как завершённая.")
    await callback.answer()


@router.callback_query(ApptActCB.filter(F.act == "r"))
async def master_move_start(
    callback: CallbackQuery,
    callback_data: ApptActCB,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    staff = await _current_staff(session, business, callback.from_user.id)
    appt = await _staff_appt(session, business.id, staff.id if staff else 0, callback_data.id)
    if appt is None:
        await callback.answer("Не найдено", show_alert=True)
        return
    await state.set_state(MasterFSM.move_date)
    await state.update_data(appointment_id=appt.id, service_id=appt.service_id)
    await ask_dates(callback.message, session, business, staff, state)
    await callback.answer()


@router.callback_query(MasterFSM.move_date, DateCB.filter())
async def master_move_date(
    callback: CallbackQuery,
    callback_data: DateCB,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    staff = await _current_staff(session, business, callback.from_user.id)
    if staff is None:
        await state.clear()
        await callback.answer("Нет доступа", show_alert=True)
        return
    data = await state.get_data()
    service = await session.scalar(
        select(Service).where(
            Service.business_id == business.id,
            Service.staff_id == staff.id,
            Service.id == data["service_id"],
        )
    )
    if service is None:
        await state.clear()
        await callback.answer("Услуга недоступна", show_alert=True)
        return
    local_date = datetime.strptime(callback_data.d, "%Y-%m-%d").date()
    await state.update_data(local_date=callback_data.d)
    await state.set_state(MasterFSM.move_slot)
    ok = await ask_slots(callback.message, session, business, staff, service, local_date, state)
    if not ok:
        await state.set_state(MasterFSM.move_date)
    else:
        await callback.message.answer(
            "Дата не подошла?",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Выбрать другую дату", callback_data="back:dates")]
                ]
            ),
        )
    await callback.answer()


@router.callback_query(MasterFSM.move_slot, SlotCB.filter())
async def master_move_slot(
    callback: CallbackQuery,
    callback_data: SlotCB,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    staff = await _current_staff(session, business, callback.from_user.id)
    if staff is None:
        await state.clear()
        await callback.answer("Нет доступа", show_alert=True)
        return
    data = await state.get_data()
    appt = await _staff_appt(session, business.id, staff.id, data["appointment_id"])
    if appt is None:
        await state.clear()
        await callback.answer("Не найдено", show_alert=True)
        return
    starts_at = datetime.fromtimestamp(callback_data.ts, tz=timezone.utc)
    try:
        appt = await reschedule_appointment(
            session, business, appt, appt.client, appt.service, starts_at
        )
    except DBAPIError as exc:
        await session.rollback()
        if not is_slot_conflict(exc):
            raise
        await callback.message.answer(SLOT_TAKEN_MESSAGE)
        local_date = datetime.strptime(data["local_date"], "%Y-%m-%d").date()
        await ask_slots(callback.message, session, business, staff, appt.service, local_date, state)
        await callback.answer()
        return
    await state.clear()
    await callback.message.answer("Запись перенесена.\n" + appointment_card(appt, business, appt.service))
    await callback.answer()


@router.callback_query(MasterFSM.move_slot, F.data == "back:dates")
async def master_move_back_to_dates(
    callback: CallbackQuery,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    staff = await _current_staff(session, business, callback.from_user.id)
    if staff is None:
        await state.clear()
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(MasterFSM.move_date)
    await ask_dates(callback.message, session, business, staff, state)
    await callback.answer()


@router.callback_query(MasterFSM.move_slot, F.data == "slot:locked")
async def slot_locked_move_master(callback: CallbackQuery):
    await callback.answer("Это время уже занято.", show_alert=True)


async def _staff_appt(session, business_id, staff_id, appointment_id) -> Appointment | None:
    return await session.scalar(
        select(Appointment).where(
            Appointment.business_id == business_id,
            Appointment.staff_id == staff_id,
            Appointment.id == appointment_id,
        )
    )


# --- клиенты ---


@router.message(F.text == "Клиенты")
async def list_clients(
    message: Message,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    await state.clear()
    clients = (
        await session.scalars(
            select(Client).where(Client.business_id == business.id).order_by(Client.id.desc())
        )
    ).all()
    if not clients:
        await message.answer("Клиентов пока нет.")
        return
    rows = [
        [
            InlineKeyboardButton(
                text=c.full_name or str(c.telegram_id),
                callback_data=ClientCB(id=c.id).pack(),
            )
        ]
        for c in clients[:50]
    ]
    await message.answer("Клиенты:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(ClientCB.filter())
async def client_card(
    callback: CallbackQuery,
    callback_data: ClientCB,
    session: AsyncSession,
    business: Business,
):
    client = await session.scalar(
        select(Client).where(Client.business_id == business.id, Client.id == callback_data.id)
    )
    if client is None:
        await callback.answer("Не найден", show_alert=True)
        return
    appts = (
        await session.scalars(
            select(Appointment)
            .where(Appointment.business_id == business.id, Appointment.client_id == client.id)
            .order_by(Appointment.starts_at.desc())
            .limit(10)
        )
    ).all()
    lines = [
        f"Клиент: {client.full_name or '—'}",
        f"Телефон: {client.phone or '—'}",
        f"Telegram ID: {client.telegram_id}",
        "",
        "История:",
    ]
    if not appts:
        lines.append("записей нет")
    for appt in appts:
        lines.append(
            f"#{appt.id} {appt.service.name} {appointment_card(appt, business, appt.service).splitlines()[2]}"
        )
    await callback.message.answer("\n".join(lines))
    await callback.answer()


# --- услуги ---


@router.message(F.text == "Услуги")
async def list_services(
    message: Message,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    await state.clear()
    staff = await _current_staff(session, business, message.from_user.id)
    if staff is None:
        await message.answer("Нет доступа.")
        return
    await _send_services(message, session, business, staff)


async def _send_services(message, session, business, staff: Staff):
    services = (
        await session.scalars(
            select(Service)
            .where(Service.business_id == business.id, Service.staff_id == staff.id)
            .order_by(Service.position, Service.id)
        )
    ).all()
    rows = [
        [InlineKeyboardButton(text="Добавить услугу", callback_data="svcadd")],
    ]
    for s in services:
        flag = "🟢" if s.is_active else "🔴"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{flag} {s.name} · {format_price(s.price_minor)}",
                    callback_data=SvcActCB(id=s.id, act="i").pack(),
                )
            ]
        )
    await message.answer("Услуги:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "svcadd")
async def svc_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(MasterFSM.svc_name)
    await callback.message.answer("Название услуги:")
    await callback.answer()


@router.message(MasterFSM.svc_name, F.text)
async def svc_name(message: Message, state: FSMContext):
    await state.update_data(svc_name=message.text.strip()[:200])
    await state.set_state(MasterFSM.svc_price)
    await message.answer("Цена в BYN, например 25 или 25.00")


@router.message(MasterFSM.svc_price, F.text)
async def svc_price(message: Message, state: FSMContext):
    price = parse_price_minor(message.text or "")
    if price is None:
        await message.answer("Не понял цену. Пример: 25.00")
        return
    await state.update_data(price_minor=price)
    await state.set_state(MasterFSM.svc_duration)
    await message.answer("Длительность в минутах, например 90")


@router.message(MasterFSM.svc_duration, F.text)
async def svc_duration(
    message: Message,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    staff = await _current_staff(session, business, message.from_user.id)
    if staff is None:
        await message.answer("Нет доступа.")
        await state.clear()
        return
    try:
        duration = int((message.text or "").strip())
    except ValueError:
        await message.answer("Введите число минут.")
        return
    if duration <= 0:
        await message.answer("Длительность должна быть больше нуля.")
        return
    data = await state.get_data()
    if data.get("edit_id"):
        service = await session.scalar(
            select(Service).where(
                Service.business_id == business.id,
                Service.staff_id == staff.id,
                Service.id == data["edit_id"],
            )
        )
        if service:
            service.name = data["svc_name"]
            service.price_minor = data["price_minor"]
            service.duration_minutes = duration
            await session.commit()
            await message.answer("Услуга обновлена.", reply_markup=await _main_kb(session, business, message.from_user.id))
    else:
        max_pos = await session.scalar(
            select(Service.position)
            .where(Service.business_id == business.id, Service.staff_id == staff.id)
            .order_by(Service.position.desc())
            .limit(1)
        )
        session.add(
            Service(
                business_id=business.id,
                staff_id=staff.id,
                name=data["svc_name"],
                price_minor=data["price_minor"],
                duration_minutes=duration,
                is_active=True,
                position=(max_pos or 0) + 1,
            )
        )
        await session.commit()
        await message.answer("Услуга добавлена.", reply_markup=await _main_kb(session, business, message.from_user.id))
    await state.clear()


@router.callback_query(SvcActCB.filter(F.act == "i"))
async def svc_item(
    callback: CallbackQuery,
    callback_data: SvcActCB,
    session: AsyncSession,
    business: Business,
):
    staff = await _current_staff(session, business, callback.from_user.id)
    service = await session.scalar(
        select(Service).where(
            Service.business_id == business.id,
            Service.staff_id == staff.id if staff else False,
            Service.id == callback_data.id,
        )
    )
    if service is None:
        await callback.answer("Не найдена", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Изменить", callback_data=SvcActCB(id=service.id, act="e").pack()),
                InlineKeyboardButton(
                    text="Выключить" if service.is_active else "Включить",
                    callback_data=SvcActCB(id=service.id, act="t").pack(),
                ),
            ]
        ]
    )
    await callback.message.answer(
        f"{service.name}\n{format_price(service.price_minor)}, {service.duration_minutes} мин",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(SvcActCB.filter(F.act == "t"))
async def svc_toggle(
    callback: CallbackQuery,
    callback_data: SvcActCB,
    session: AsyncSession,
    business: Business,
):
    staff = await _current_staff(session, business, callback.from_user.id)
    service = await session.scalar(
        select(Service).where(
            Service.business_id == business.id,
            Service.staff_id == staff.id if staff else False,
            Service.id == callback_data.id,
        )
    )
    if service is None:
        await callback.answer("Не найдена", show_alert=True)
        return
    service.is_active = not service.is_active
    await session.commit()
    await callback.message.answer("Услуга " + ("включена." if service.is_active else "выключена."))
    await callback.answer()


@router.callback_query(SvcActCB.filter(F.act == "e"))
async def svc_edit(
    callback: CallbackQuery,
    callback_data: SvcActCB,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    staff = await _current_staff(session, business, callback.from_user.id)
    service = await session.scalar(
        select(Service).where(
            Service.business_id == business.id,
            Service.staff_id == staff.id if staff else False,
            Service.id == callback_data.id,
        )
    )
    if service is None:
        await callback.answer("Не найдена", show_alert=True)
        return
    await state.update_data(edit_id=service.id)
    await state.set_state(MasterFSM.svc_name)
    await callback.message.answer("Новое название:")
    await callback.answer()


# --- окошки ---


class WindowCB(CallbackData, prefix="win"):
    id: int
    d: str


def _windows_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить день или времена", callback_data="winadd")],
            [InlineKeyboardButton(text="🗑 Убрать окошко", callback_data="windel")],
        ]
    )


def _local_to_utc(day: date, t: time, tz: ZoneInfo) -> datetime | None:
    """Локальный момент в UTC; None, если момента не существует (разрыв DST)."""
    naive = datetime.combine(day, t)
    aware = naive.replace(tzinfo=tz)
    back = aware.astimezone(timezone.utc).astimezone(tz)
    if back.replace(tzinfo=None) != naive:
        return None
    return aware.astimezone(timezone.utc)


def _parse_times(text: str) -> list[time] | None:
    """Разбирает '10:30 14:00 17:30' или '10:30, 14:00'. None, если мусор."""
    parts = (text or "").replace(",", " ").split()
    if not parts:
        return None
    result: list[time] = []
    for p in parts:
        bits = p.split(":")
        if len(bits) != 2:
            return None
        try:
            hh, mm = int(bits[0]), int(bits[1])
        except ValueError:
            return None
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        result.append(time(hh, mm))
    return result


async def _windows_summary(session: AsyncSession, business: Business, staff: Staff) -> str:
    """Вид 'как в сторис': даты со временами, занятые помечены."""
    tz = _tz(business)
    today = datetime.now(tz).date()
    last = today + timedelta(days=business.max_booking_days - 1)
    windows = (
        await session.scalars(
            select(DayWindow)
            .where(
                DayWindow.business_id == business.id,
                DayWindow.staff_id == staff.id,
                DayWindow.date >= today,
                DayWindow.date <= last,
            )
            .order_by(DayWindow.starts_at)
        )
    ).all()
    if not windows:
        return "Окошек пока нет. Нажми «Добавить день или времена»."
    busy_ids: set[int] = set()
    for d in sorted({w.date for w in windows}):
        day_items = await get_day_windows(session, business, staff, d, None)
        for item in day_items:
            if item["reason"] == "busy":
                busy_ids.add(item["window"].id)
    lines: list[str] = ["Окошки на ближайшие дни:"]
    current: date | None = None
    for w in windows:
        if w.date != current:
            current = w.date
            lines.append(f"{WEEKDAYS_RU[current.weekday()]} {current.strftime('%d.%m')}:")
        mark = " 🔒" if w.id in busy_ids else ""
        lines.append(f"   {w.starts_at.astimezone(tz).strftime('%H:%M')}{mark}")
    return "\n".join(lines)


@router.message(F.text == "Окошки")
async def windows_home(
    message: Message,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    await state.clear()
    staff = await _current_staff(session, business, message.from_user.id)
    if staff is None:
        await message.answer("Нет доступа.")
        return
    await message.answer(await _windows_summary(session, business, staff), reply_markup=_windows_menu_kb())


@router.callback_query(F.data == "winadd")
async def win_add_start(
    callback: CallbackQuery,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    tz = _tz(business)
    today = datetime.now(tz).date()
    dates = [today + timedelta(days=i) for i in range(business.max_booking_days)]
    await state.set_state(MasterFSM.win_date)
    await callback.message.answer("Выбери день для окошек:", reply_markup=dates_kb(dates))
    await callback.answer()


@router.callback_query(MasterFSM.win_date, DateCB.filter())
async def win_date_save(
    callback: CallbackQuery,
    callback_data: DateCB,
    state: FSMContext,
):
    await state.update_data(win_date=callback_data.d)
    await state.set_state(MasterFSM.win_times)
    await callback.message.answer("Времена через пробел или запятую, например: 10:30 14:00 17:30")
    await callback.answer()


@router.message(MasterFSM.win_times, F.text)
async def win_times_save(
    message: Message,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    staff = await _current_staff(session, business, message.from_user.id)
    if staff is None:
        await message.answer("Нет доступа.")
        await state.clear()
        return
    times = _parse_times(message.text or "")
    if times is None:
        await message.answer("Формат: 10:30 14:00 17:30")
        return
    data = await state.get_data()
    day = datetime.strptime(data["win_date"], "%Y-%m-%d").date()
    tz = _tz(business)
    added = skipped_dup = skipped_dst = 0
    for t in times:
        aware_utc = _local_to_utc(day, t, tz)
        if aware_utc is None:
            skipped_dst += 1
            continue
        exists = await session.scalar(
            select(DayWindow.id).where(
                DayWindow.business_id == business.id,
                DayWindow.staff_id == staff.id,
                DayWindow.starts_at == aware_utc,
            )
        )
        if exists:
            skipped_dup += 1
            continue
        session.add(
            DayWindow(
                business_id=business.id,
                staff_id=staff.id,
                date=day,
                starts_at=aware_utc,
            )
        )
        added += 1
    await session.commit()
    await state.clear()
    msg = f"Добавлено окошек: {added}."
    if skipped_dup:
        msg += f" Уже были: {skipped_dup}."
    if skipped_dst:
        msg += f" Пропущено несуществующее время: {skipped_dst}."
    await message.answer(
        msg + "\n\n" + await _windows_summary(session, business, staff),
        reply_markup=_windows_menu_kb(),
    )


@router.callback_query(F.data == "windel")
async def win_del_start(
    callback: CallbackQuery,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    staff = await _current_staff(session, business, callback.from_user.id)
    if staff is None:
        await callback.answer("Нет доступа", show_alert=True)
        return
    today = datetime.now(_tz(business)).date()
    dates = (
        await session.scalars(
            select(DayWindow.date)
            .where(
                DayWindow.business_id == business.id,
                DayWindow.staff_id == staff.id,
                DayWindow.date >= today,
            )
            .order_by(DayWindow.date)
            .distinct()
        )
    ).all()
    if not dates:
        await callback.answer("Окошек пока нет", show_alert=True)
        return
    await state.set_state(MasterFSM.win_del_date)
    await callback.message.answer("С какого дня убрать окошки?", reply_markup=dates_kb(list(dates)))
    await callback.answer()


@router.callback_query(MasterFSM.win_del_date, DateCB.filter())
async def win_del_date_save(
    callback: CallbackQuery,
    callback_data: DateCB,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    staff = await _current_staff(session, business, callback.from_user.id)
    if staff is None:
        await state.clear()
        await callback.answer("Нет доступа", show_alert=True)
        return
    day = datetime.strptime(callback_data.d, "%Y-%m-%d").date()
    windows = (
        await session.scalars(
            select(DayWindow)
            .where(
                DayWindow.business_id == business.id,
                DayWindow.staff_id == staff.id,
                DayWindow.date == day,
            )
            .order_by(DayWindow.starts_at)
        )
    ).all()
    if not windows:
        await state.clear()
        await callback.answer("На эту дату окошек нет", show_alert=True)
        return
    tz = _tz(business)
    rows = [
        [
            InlineKeyboardButton(
                text=f"✖ {w.starts_at.astimezone(tz).strftime('%H:%M')}",
                callback_data=WindowCB(id=w.id, d=day.isoformat()).pack(),
            )
        ]
        for w in windows
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="🗑 Очистить день целиком",
                callback_data=WindowCB(id=0, d=day.isoformat()).pack(),
            )
        ]
    )
    await state.clear()
    await callback.message.answer(
        f"Окошки на {day.strftime('%d.%m.%Y')}. Нажми, чтобы убрать:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(WindowCB.filter())
async def win_delete(
    callback: CallbackQuery,
    callback_data: WindowCB,
    session: AsyncSession,
    business: Business,
):
    staff = await _current_staff(session, business, callback.from_user.id)
    if staff is None:
        await callback.answer("Нет доступа", show_alert=True)
        return
    day = datetime.strptime(callback_data.d, "%Y-%m-%d").date()
    if callback_data.id == 0:
        rows = (
            await session.scalars(
                select(DayWindow).where(
                    DayWindow.business_id == business.id,
                    DayWindow.staff_id == staff.id,
                    DayWindow.date == day,
                )
            )
        ).all()
        for w in rows:
            await session.delete(w)
        await session.commit()
        await callback.message.answer(f"День {day.strftime('%d.%m')} очищен.")
    else:
        w = await session.scalar(
            select(DayWindow).where(
                DayWindow.id == callback_data.id,
                DayWindow.business_id == business.id,
                DayWindow.staff_id == staff.id,
            )
        )
        if w is None:
            await callback.answer("Уже убрано", show_alert=True)
            return
        await session.delete(w)
        await session.commit()
        await callback.message.answer("Окошко убрано.")
    await callback.message.answer(
        await _windows_summary(session, business, staff), reply_markup=_windows_menu_kb()
    )
    await callback.answer()


@router.callback_query(F.data == "slot:locked")
async def slot_locked_master(callback: CallbackQuery):
    await callback.answer("Это время уже занято.", show_alert=True)


# --- настройки ---


def _offsets_human(offsets: list[int]) -> str:
    if not offsets:
        return "выключены"
    parts = []
    for m in offsets:
        if m % 1440 == 0:
            parts.append(f"за {m // 1440} дн")
        elif m % 60 == 0:
            parts.append(f"за {m // 60} ч")
        else:
            parts.append(f"за {m} мин")
    return ", ".join(parts)


def _reminders_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="За сутки и за 2 часа", callback_data="set:rem:1440,120")],
            [InlineKeyboardButton(text="За сутки, 2 часа и 30 минут", callback_data="set:rem:1440,120,30")],
            [InlineKeyboardButton(text="Только за 2 часа", callback_data="set:rem:120")],
            [InlineKeyboardButton(text="Выключить напоминания", callback_data="set:rem:")],
            [InlineKeyboardButton(text="Свои значения", callback_data="set:rem:custom")],
        ]
    )


@router.message(F.text == "⚙️ Настройки")
async def settings_home(
    message: Message,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    await state.clear()
    offsets = business.reminder_offsets_minutes or []
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Название кабинета", callback_data="set:name")],
            [InlineKeyboardButton(text="🔔 Напоминания", callback_data="set:rem")],
        ]
    )
    await message.answer(
        f"Настройки кабинета:\nИмя: {business.name}\nНапоминания: {_offsets_human(list(offsets))}",
        reply_markup=kb,
    )


@router.callback_query(F.data == "set:name")
async def set_name_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(MasterFSM.set_name)
    await callback.message.answer("Новое название кабинета (до 100 символов):")
    await callback.answer()


@router.message(MasterFSM.set_name, F.text)
async def set_name_save(
    message: Message,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Слишком коротко. Введите ещё раз.")
        return
    business.name = name[:100]
    await session.commit()
    await state.clear()
    await message.answer(
        f"Готово. Теперь кабинет называется «{business.name}».",
        reply_markup=await _main_kb(session, business, message.from_user.id),
    )


@router.callback_query(F.data == "set:rem")
async def set_rem_start(callback: CallbackQuery):
    await callback.message.answer("Когда напоминать о визите?", reply_markup=_reminders_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("set:rem:"))
async def set_rem_save(
    callback: CallbackQuery,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    payload = callback.data[len("set:rem:"):]
    if payload == "custom":
        await state.set_state(MasterFSM.set_rem)
        await callback.message.answer("Минуты до визита через запятую, например: 1440,120,30")
        await callback.answer()
        return
    offsets = [int(x) for x in payload.split(",") if x]
    business.reminder_offsets_minutes = offsets
    await session.commit()
    await state.clear()
    await callback.message.answer(
        f"Напоминания обновлены: {_offsets_human(offsets)}. Новые записи получат этот набор.",
        reply_markup=await _main_kb(session, business, message.from_user.id),
    )
    await callback.answer()


@router.message(MasterFSM.set_rem, F.text)
async def set_rem_custom_save(
    message: Message,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    parts = (message.text or "").replace(" ", "").split(",")
    offsets: list[int] = []
    for p in parts:
        if not p.isdigit():
            await message.answer("Формат: минуты через запятую, например 1440,120,30")
            return
        v = int(p)
        if v <= 0 or v > 43200:
            await message.answer("Каждое значение — от 1 до 43200 минут.")
            return
        offsets.append(v)
    if not offsets:
        await message.answer("Нужно хотя бы одно значение или кнопка «Выключить».")
        return
    offsets = sorted(set(offsets), reverse=True)
    business.reminder_offsets_minutes = offsets
    await session.commit()
    await state.clear()
    await message.answer(
        f"Напоминания обновлены: {_offsets_human(offsets)}. Новые записи получат этот набор.",
        reply_markup=await _main_kb(session, business, message.from_user.id),
    )

# --- команда салона (владелец) ---


class StaffManageCB(CallbackData, prefix="mng"):
    id: int
    act: str


@router.message(F.text == "👥 Мастера")
async def staff_home(message: Message, session: AsyncSession, business: Business, state: FSMContext):
    await state.clear()
    if not await _is_owner(session, business, message.from_user.id):
        await message.answer("Раздел доступен только владельцу салона.")
        return
    staff_list = (
        await session.scalars(
            select(Staff).where(Staff.business_id == business.id).order_by(Staff.id)
        )
    ).all()
    rows = []
    for s in staff_list:
        flag = "🟢" if s.is_active else "🔴"
        suffix = " · владелец" if s.is_owner else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{flag} {s.name}{suffix}",
                    callback_data=StaffManageCB(id=s.id, act="i").pack(),
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="➕ Добавить мастера", callback_data="mngadd")])
    await message.answer("Команда салона:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "mngadd")
async def staff_add_start(
    callback: CallbackQuery,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    if not await _is_owner(session, business, callback.from_user.id):
        await callback.answer("Только владелец", show_alert=True)
        return
    await state.set_state(MasterFSM.staff_name)
    await callback.message.answer("Имя нового мастера (как его увидят клиенты):")
    await callback.answer()


@router.message(MasterFSM.staff_name, F.text)
async def staff_name_save(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Слишком коротко. Введите ещё раз.")
        return
    await state.update_data(staff_name=name[:100])
    await state.set_state(MasterFSM.staff_tid)
    await message.answer(
        "Пришли Telegram ID мастера числом.\n"
        "Мастер узнаёт свой ID так: пишет боту @userinfobot и получает число в ответ."
    )


@router.message(MasterFSM.staff_tid, F.text)
async def staff_tid_save(message: Message, session: AsyncSession, business: Business, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw.lstrip("-").isdigit():
        await message.answer("ID — это число. Попробуй ещё раз.")
        return
    tid = int(raw)
    data = await state.get_data()
    exists = await session.scalar(
        select(Staff.id).where(Staff.business_id == business.id, Staff.telegram_id == tid)
    )
    if exists is not None:
        await state.clear()
        await message.answer(
            "Этот Telegram уже есть в команде.",
            reply_markup=await _main_kb(session, business, message.from_user.id),
        )
        return
    session.add(
        Staff(
            business_id=business.id,
            name=data["staff_name"],
            telegram_id=tid,
            is_owner=False,
            is_active=True,
        )
    )
    await session.commit()
    await state.clear()
    link = f"https://t.me/{business.bot_username}" if business.bot_username else "бот салона в Telegram"
    await message.answer(
        f"Мастер {data['staff_name']} добавлен в команду.\n\n"
        "Пришли ей эту инструкцию:\n"
        f"1) Открой {link} и нажми /start.\n"
        "2) Бот узнает тебя по аккаунту и откроет твой личный кабинет.\n"
        "3) В «Услуги» добавь свои услуги и цены, в «Окошки» выложи свободные времена.\n"
        "4) Клиенты будут выбирать тебя в списке мастеров и записываться только на твои окошки.",
        reply_markup=await _main_kb(session, business, message.from_user.id),
    )


@router.callback_query(StaffManageCB.filter())
async def staff_manage(
    callback: CallbackQuery,
    callback_data: StaffManageCB,
    session: AsyncSession,
    business: Business,
):
    if not await _is_owner(session, business, callback.from_user.id):
        await callback.answer("Только владелец", show_alert=True)
        return
    s = await session.scalar(
        select(Staff).where(Staff.business_id == business.id, Staff.id == callback_data.id)
    )
    if s is None:
        await callback.answer("Не найдено", show_alert=True)
        return
    if callback_data.act == "i":
        kb_rows = []
        if not s.is_owner:
            kb_rows.append(
                [
                    InlineKeyboardButton(
                        text="Отключить" if s.is_active else "Включить",
                        callback_data=StaffManageCB(id=s.id, act="t").pack(),
                    )
                ]
            )
        await callback.message.answer(
            f"{s.name}\nСтатус: {'активен' if s.is_active else 'отключён'}"
            + (" · владелец салона" if s.is_owner else ""),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None,
        )
        await callback.answer()
        return
    if callback_data.act == "t":
        if s.is_owner:
            await callback.answer("Владельца нельзя отключить", show_alert=True)
            return
        s.is_active = not s.is_active
        await session.commit()
        await callback.message.answer(f"{s.name}: " + ("включена." if s.is_active else "отключена."))
        await callback.answer()


@router.message(F.text == "📊 Сводка")
async def salon_summary(message: Message, session: AsyncSession, business: Business, state: FSMContext):
    await state.clear()
    if not await _is_owner(session, business, message.from_user.id):
        await message.answer("Раздел доступен только владельцу салона.")
        return
    tz = _tz(business)
    today = datetime.now(tz).date()
    week_start = today - timedelta(days=6)
    week_start_utc = datetime.combine(week_start, time.min, tzinfo=tz).astimezone(timezone.utc)
    today_utc = datetime.combine(today, time.min, tzinfo=tz).astimezone(timezone.utc)
    staff_list = (
        await session.scalars(
            select(Staff).where(Staff.business_id == business.id).order_by(Staff.id)
        )
    ).all()
    lines = [f"Сводка салона за 7 дней (с {week_start.strftime('%d.%m')}):"]
    for s in staff_list:
        week_cnt = await session.scalar(
            select(func.count(Appointment.id)).where(
                Appointment.business_id == business.id,
                Appointment.staff_id == s.id,
                Appointment.status != AppointmentStatus.canceled,
                Appointment.starts_at >= week_start_utc,
            )
        )
        today_cnt = await session.scalar(
            select(func.count(Appointment.id)).where(
                Appointment.business_id == business.id,
                Appointment.staff_id == s.id,
                Appointment.status != AppointmentStatus.canceled,
                Appointment.starts_at >= today_utc,
            )
        )
        lines.append(f"{s.name}: сегодня {today_cnt or 0}, за 7 дней {week_cnt or 0}")
    await message.answer(
        "\n".join(lines),
        reply_markup=await _main_kb(session, business, message.from_user.id),
    )