from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import DBAPIError
from app.services.booking import is_slot_conflict
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
)
from app.services.booking import (
    SLOT_TAKEN_MESSAGE,
    cancel_appointment,
    complete_appointment,
    reschedule_appointment,
)
from app.services.formatting import WEEKDAYS_RU, appointment_card, format_price


router = Router()
router.message.filter(RoleFilter("master"))
router.callback_query.filter(RoleFilter("master"))


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


def _tz(business: Business) -> ZoneInfo:
    return ZoneInfo(business.timezone)


@router.message(CommandStart())
async def start(message: Message, business: Business, state: FSMContext):
    await state.clear()
    await message.answer(
        f"Кабинет мастера «{business.name}».",
        reply_markup=master_main_kb(),
    )


@router.message(F.text == "Отмена")
async def cancel_flow(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=master_main_kb())


# --- записи ---


@router.message(F.text == "Записи")
async def appointments_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Записи за какой день?", reply_markup=master_days_kb())


@router.callback_query(F.data == "md:today")
async def appts_today(callback: CallbackQuery, session: AsyncSession, business: Business):
    day = datetime.now(_tz(business)).date()
    await _send_day_appts(callback.message, session, business, day)
    await callback.answer()


@router.callback_query(F.data == "md:tomorrow")
async def appts_tomorrow(callback: CallbackQuery, session: AsyncSession, business: Business):
    day = datetime.now(_tz(business)).date() + timedelta(days=1)
    await _send_day_appts(callback.message, session, business, day)
    await callback.answer()


@router.callback_query(F.data == "md:pick")
async def appts_pick(callback: CallbackQuery, state: FSMContext):
    await state.set_state(MasterFSM.pick_day)
    await callback.message.answer("Введите дату в формате ДД.ММ.ГГГГ")
    await callback.answer()


@router.message(MasterFSM.pick_day, F.text)
async def appts_picked(message: Message, session: AsyncSession, business: Business, state: FSMContext):
    day = parse_ru_date(message.text or "")
    if day is None:
        await message.answer("Не понял дату. Пример: 10.09.2026")
        return
    await state.clear()
    await _send_day_appts(message, session, business, day)


async def _send_day_appts(message, session, business: Business, day):
    tz = _tz(business)
    start = datetime.combine(day, time.min, tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)
    appts = (
        await session.scalars(
            select(Appointment)
            .where(
                Appointment.business_id == business.id,
                Appointment.starts_at >= start,
                Appointment.starts_at < end,
                Appointment.status != AppointmentStatus.canceled,
            )
            .order_by(Appointment.starts_at)
        )
    ).all()
    if not appts:
        await message.answer(f"На {day.strftime('%d.%m.%Y')} записей нет.")
        return
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


@router.callback_query(ApptActCB.filter(F.act == "c"))
async def master_cancel(
    callback: CallbackQuery,
    callback_data: ApptActCB,
    session: AsyncSession,
    business: Business,
):
    appt = await _business_appt(session, business.id, callback_data.id)
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
    appt = await _business_appt(session, business.id, callback_data.id)
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
    appt = await _business_appt(session, business.id, callback_data.id)
    if appt is None:
        await callback.answer("Не найдено", show_alert=True)
        return
    await state.set_state(MasterFSM.move_date)
    await state.update_data(appointment_id=appt.id, service_id=appt.service_id)
    await ask_dates(callback.message, session, business, state)
    await callback.answer()


@router.callback_query(MasterFSM.move_date, DateCB.filter())
async def master_move_date(
    callback: CallbackQuery,
    callback_data: DateCB,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    data = await state.get_data()
    service = await session.scalar(
        select(Service).where(Service.business_id == business.id, Service.id == data["service_id"])
    )
    local_date = datetime.strptime(callback_data.d, "%Y-%m-%d").date()
    await state.update_data(local_date=callback_data.d)
    await state.set_state(MasterFSM.move_slot)
    ok = await ask_slots(callback.message, session, business, service, local_date, state)
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
    data = await state.get_data()
    appt = await _business_appt(session, business.id, data["appointment_id"])
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
        await ask_slots(callback.message, session, business, appt.service, local_date, state)
        await callback.answer()
        return
    await state.clear()
    await callback.message.answer("Запись перенесена.\n" + appointment_card(appt, business, appt.service))
    await callback.answer()


async def _business_appt(session, business_id, appointment_id) -> Appointment | None:
    return await session.scalar(
        select(Appointment).where(
            Appointment.business_id == business_id,
            Appointment.id == appointment_id,
        )
    )


# --- клиенты ---


@router.message(F.text == "Клиенты")
async def list_clients(message: Message, session: AsyncSession, business: Business, state: FSMContext):
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
async def list_services(message: Message, session: AsyncSession, business: Business, state: FSMContext):
    await state.clear()
    await _send_services(message, session, business)


async def _send_services(message, session, business):
    services = (
        await session.scalars(
            select(Service).where(Service.business_id == business.id).order_by(Service.position, Service.id)
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
async def svc_duration(message: Message, session: AsyncSession, business: Business, state: FSMContext):
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
            select(Service).where(Service.business_id == business.id, Service.id == data["edit_id"])
        )
        if service:
            service.name = data["svc_name"]
            service.price_minor = data["price_minor"]
            service.duration_minutes = duration
            await session.commit()
            await message.answer("Услуга обновлена.", reply_markup=master_main_kb())
    else:
        max_pos = await session.scalar(
            select(Service.position)
            .where(Service.business_id == business.id)
            .order_by(Service.position.desc())
            .limit(1)
        )
        session.add(
            Service(
                business_id=business.id,
                name=data["svc_name"],
                price_minor=data["price_minor"],
                duration_minutes=duration,
                is_active=True,
                position=(max_pos or 0) + 1,
            )
        )
        await session.commit()
        await message.answer("Услуга добавлена.", reply_markup=master_main_kb())
    await state.clear()


@router.callback_query(SvcActCB.filter(F.act == "i"))
async def svc_item(
    callback: CallbackQuery,
    callback_data: SvcActCB,
    session: AsyncSession,
    business: Business,
):
    service = await session.scalar(
        select(Service).where(Service.business_id == business.id, Service.id == callback_data.id)
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
    service = await session.scalar(
        select(Service).where(Service.business_id == business.id, Service.id == callback_data.id)
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
    service = await session.scalar(
        select(Service).where(Service.business_id == business.id, Service.id == callback_data.id)
    )
    if service is None:
        await callback.answer("Не найдена", show_alert=True)
        return
    await state.update_data(edit_id=service.id)
    await state.set_state(MasterFSM.svc_name)
    await callback.message.answer("Новое название:")
    await callback.answer()


# --- расписание ---
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


async def _windows_summary(session: AsyncSession, business: Business) -> str:
    """Вид 'как в сторис': даты со временами, занятые помечены."""
    tz = _tz(business)
    today = datetime.now(tz).date()
    last = today + timedelta(days=business.max_booking_days - 1)
    windows = (
        await session.scalars(
            select(DayWindow)
            .where(
                DayWindow.business_id == business.id,
                DayWindow.date >= today,
                DayWindow.date <= last,
            )
            .order_by(DayWindow.starts_at)
        )
    ).all()
    if not windows:
        return "Окошек пока нет. Нажми «Добавить день или времена»."
    day_start = datetime.combine(today, time.min, tzinfo=tz).astimezone(timezone.utc)
    day_end = datetime.combine(last + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)
    appts = (
        await session.scalars(
            select(Appointment).where(
                Appointment.business_id == business.id,
                Appointment.status == AppointmentStatus.confirmed,
                Appointment.starts_at >= day_start,
                Appointment.starts_at < day_end,
            )
        )
    ).all()
    booked = {a.starts_at for a in appts}
    lines: list[str] = ["Окошки на ближайшие дни:"]
    current: date | None = None
    for w in windows:
        if w.date != current:
            current = w.date
            lines.append(f"{WEEKDAYS_RU[current.weekday()]} {current.strftime('%d.%m')}:")
        mark = " 🔒" if w.starts_at in booked else ""
        lines.append(f"   {w.starts_at.astimezone(tz).strftime('%H:%M')}{mark}")
    return "\n".join(lines)


@router.message(F.text == "Окошки")
async def windows_home(message: Message, session: AsyncSession, business: Business, state: FSMContext):
    await state.clear()
    await message.answer(await _windows_summary(session, business), reply_markup=_windows_menu_kb())


@router.callback_query(F.data == "winadd")
async def win_add_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(MasterFSM.win_date)
    await callback.message.answer(
        "Дата дня окошек (ДД.ММ.ГГГГ). Если день уже есть, времена добавятся к нему."
    )
    await callback.answer()


@router.message(MasterFSM.win_date, F.text)
async def win_date_save(message: Message, session: AsyncSession, business: Business, state: FSMContext):
    day = parse_ru_date(message.text or "")
    if day is None:
        await message.answer("Не понял дату. Пример: 10.10.2026")
        return
    tz = _tz(business)
    today = datetime.now(tz).date()
    last = today + timedelta(days=business.max_booking_days - 1)
    if day < today or day > last:
        await message.answer(
            f"Окошки можно выкладывать с {today.strftime('%d.%m')} по {last.strftime('%d.%m')}."
        )
        return
    await state.update_data(win_date=day.isoformat())
    await state.set_state(MasterFSM.win_times)
    await message.answer("Времена через пробел или запятую, например: 10:30 14:00 17:30")


@router.message(MasterFSM.win_times, F.text)
async def win_times_save(message: Message, session: AsyncSession, business: Business, state: FSMContext):
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
                DayWindow.starts_at == aware_utc,
            )
        )
        if exists:
            skipped_dup += 1
            continue
        session.add(DayWindow(business_id=business.id, date=day, starts_at=aware_utc))
        added += 1
    await session.commit()
    await state.clear()
    msg = f"Добавлено окошек: {added}."
    if skipped_dup:
        msg += f" Уже были: {skipped_dup}."
    if skipped_dst:
        msg += f" Пропущено несуществующее время: {skipped_dst}."
    await message.answer(msg + "\n\n" + await _windows_summary(session, business), reply_markup=_windows_menu_kb())


@router.callback_query(F.data == "windel")
async def win_del_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(MasterFSM.win_del_date)
    await callback.message.answer("Дата, с которой убрать окошки (ДД.ММ.ГГГГ):")
    await callback.answer()


@router.message(MasterFSM.win_del_date, F.text)
async def win_del_date_save(message: Message, session: AsyncSession, business: Business, state: FSMContext):
    day = parse_ru_date(message.text or "")
    if day is None:
        await message.answer("Не понял дату. Пример: 10.10.2026")
        return
    windows = (
        await session.scalars(
            select(DayWindow)
            .where(DayWindow.business_id == business.id, DayWindow.date == day)
            .order_by(DayWindow.starts_at)
        )
    ).all()
    if not windows:
        await state.clear()
        await message.answer("На эту дату окошек нет.")
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
        [InlineKeyboardButton(text="🗑 Очистить день целиком", callback_data=WindowCB(id=0, d=day.isoformat()).pack())]
    )
    await state.clear()
    await message.answer(
        f"Окошки на {day.strftime('%d.%m.%Y')}. Нажми, чтобы убрать:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(WindowCB.filter())
async def win_delete(
    callback: CallbackQuery,
    callback_data: WindowCB,
    session: AsyncSession,
    business: Business,
):
    day = datetime.strptime(callback_data.d, "%Y-%m-%d").date()
    if callback_data.id == 0:
        rows = (
            await session.scalars(
                select(DayWindow).where(
                    DayWindow.business_id == business.id,
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
            )
        )
        if w is None:
            await callback.answer("Уже убрано", show_alert=True)
            return
        await session.delete(w)
        await session.commit()
        await callback.message.answer("Окошко убрано.")
    await callback.message.answer(await _windows_summary(session, business), reply_markup=_windows_menu_kb())
    await callback.answer()
