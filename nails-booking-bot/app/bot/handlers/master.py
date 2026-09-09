from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import CommandStart
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
from app.bot.flow import ask_dates, ask_slots, parse_date_range, parse_price_minor, parse_ru_date, parse_time_range
from app.bot.keyboards import (
    ApptActCB,
    BlockCB,
    ClientCB,
    DateCB,
    DayCB,
    SlotCB,
    SvcActCB,
    appointment_actions_kb,
    master_days_kb,
    master_main_kb,
    schedule_menu_kb,
    weekdays_kb,
)
from app.models import (
    Appointment,
    AppointmentStatus,
    Business,
    Client,
    DateOverride,
    Service,
    TimeBlock,
    WorkSchedule,
)
from app.services.booking import (
    SLOT_TAKEN_MESSAGE,
    cancel_appointment,
    complete_appointment,
    reschedule_appointment,
)
from app.services.formatting import WEEKDAYS_RU, appointment_card, format_price
from app.services.slots import get_weekly_map

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


@router.message(F.text == "Расписание")
async def schedule_home(message: Message, session: AsyncSession, business: Business, state: FSMContext):
    await state.clear()
    weekly = await get_weekly_map(session, business.id)
    lines = ["Неделя:"]
    for wd in range(7):
        row = weekly.get(wd)
        if row and row.is_working and row.start_time and row.end_time:
            hours = f"{row.start_time.strftime('%H:%M')}–{row.end_time.strftime('%H:%M')}"
        else:
            hours = "выходной"
        lines.append(f"{WEEKDAYS_RU[wd]}: {hours}")
    await message.answer("\n".join(lines), reply_markup=schedule_menu_kb())


@router.callback_query(F.data == "sch:hours")
async def sch_hours(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Выберите день недели:", reply_markup=weekdays_kb())
    await callback.answer()


@router.callback_query(DayCB.filter())
async def sch_day(callback: CallbackQuery, callback_data: DayCB, state: FSMContext):
    await state.update_data(weekday=callback_data.n)
    await state.set_state(MasterFSM.hours_value)
    await callback.message.answer(
        "Напишите часы, например 10:00-20:00, или слово «выходной»."
    )
    await callback.answer()


@router.message(MasterFSM.hours_value, F.text)
async def sch_hours_save(message: Message, session: AsyncSession, business: Business, state: FSMContext):
    data = await state.get_data()
    weekday = data["weekday"]
    row = await session.scalar(
        select(WorkSchedule).where(
            WorkSchedule.business_id == business.id,
            WorkSchedule.weekday == weekday,
        )
    )
    text = (message.text or "").strip().lower()
    if text in {"выходной", "выходные"}:
        if row is None:
            session.add(
                WorkSchedule(
                    business_id=business.id,
                    weekday=weekday,
                    is_working=False,
                    start_time=None,
                    end_time=None,
                )
            )
        else:
            row.is_working = False
            row.start_time = None
            row.end_time = None
    else:
        rng = parse_time_range(message.text or "")
        if rng is None:
            await message.answer("Формат: 10:00-20:00 или «выходной».")
            return
        start, end = rng
        if row is None:
            session.add(
                WorkSchedule(
                    business_id=business.id,
                    weekday=weekday,
                    is_working=True,
                    start_time=start,
                    end_time=end,
                )
            )
        else:
            row.is_working = True
            row.start_time = start
            row.end_time = end
    await session.commit()
    await state.clear()
    await message.answer("Расписание обновлено.", reply_markup=master_main_kb())


@router.callback_query(F.data == "sch:close")
async def sch_close(callback: CallbackQuery, state: FSMContext):
    await state.set_state(MasterFSM.close_dates)
    await callback.message.answer(
        "Дата или период отпуска в формате 10.09.2026 или 10.09.2026-20.09.2026"
    )
    await callback.answer()


@router.message(MasterFSM.close_dates, F.text)
async def sch_close_save(message: Message, session: AsyncSession, business: Business, state: FSMContext):
    rng = parse_date_range(message.text or "")
    if rng is None:
        await message.answer("Не понял даты.")
        return
    start, end = rng
    day = start
    while day <= end:
        existing = await session.scalar(
            select(DateOverride).where(
                DateOverride.business_id == business.id,
                DateOverride.date == day,
            )
        )
        if existing:
            existing.is_closed = True
            existing.start_time = None
            existing.end_time = None
            existing.reason = "закрыто"
        else:
            session.add(
                DateOverride(
                    business_id=business.id,
                    date=day,
                    is_closed=True,
                    reason="закрыто",
                )
            )
        day += timedelta(days=1)
    await session.commit()
    await state.clear()
    await message.answer("Даты закрыты.", reply_markup=master_main_kb())


@router.callback_query(F.data == "sch:open")
async def sch_open(callback: CallbackQuery, state: FSMContext):
    await state.set_state(MasterFSM.open_date)
    await callback.message.answer("Дата особого рабочего дня (ДД.ММ.ГГГГ):")
    await callback.answer()


@router.message(MasterFSM.open_date, F.text)
async def sch_open_date(message: Message, state: FSMContext):
    day = parse_ru_date(message.text or "")
    if day is None:
        await message.answer("Не понял дату.")
        return
    await state.update_data(open_date=day.isoformat())
    await state.set_state(MasterFSM.open_hours)
    await message.answer("Часы, например 11:00-16:00")


@router.message(MasterFSM.open_hours, F.text)
async def sch_open_hours(message: Message, session: AsyncSession, business: Business, state: FSMContext):
    rng = parse_time_range(message.text or "")
    if rng is None:
        await message.answer("Формат: 11:00-16:00")
        return
    data = await state.get_data()
    day = datetime.strptime(data["open_date"], "%Y-%m-%d").date()
    start, end = rng
    existing = await session.scalar(
        select(DateOverride).where(
            DateOverride.business_id == business.id,
            DateOverride.date == day,
        )
    )
    if existing:
        existing.is_closed = False
        existing.start_time = start
        existing.end_time = end
        existing.reason = "особый день"
    else:
        session.add(
            DateOverride(
                business_id=business.id,
                date=day,
                is_closed=False,
                start_time=start,
                end_time=end,
                reason="особый день",
            )
        )
    await session.commit()
    await state.clear()
    await message.answer("Особый день сохранён.", reply_markup=master_main_kb())


@router.callback_query(F.data == "sch:block")
async def sch_block(callback: CallbackQuery, state: FSMContext):
    await state.set_state(MasterFSM.block_date)
    await callback.message.answer("Дата блокировки (ДД.ММ.ГГГГ):")
    await callback.answer()


@router.message(MasterFSM.block_date, F.text)
async def sch_block_date(message: Message, state: FSMContext):
    day = parse_ru_date(message.text or "")
    if day is None:
        await message.answer("Не понял дату.")
        return
    await state.update_data(block_date=day.isoformat())
    await state.set_state(MasterFSM.block_hours)
    await message.answer("Интервал, например 13:00-14:30")


@router.message(MasterFSM.block_hours, F.text)
async def sch_block_hours(message: Message, session: AsyncSession, business: Business, state: FSMContext):
    rng = parse_time_range(message.text or "")
    if rng is None:
        await message.answer("Формат: 13:00-14:30")
        return
    data = await state.get_data()
    day = datetime.strptime(data["block_date"], "%Y-%m-%d").date()
    start_t, end_t = rng
    tz = _tz(business)
    starts_at = datetime.combine(day, start_t, tzinfo=tz).astimezone(timezone.utc)
    ends_at = datetime.combine(day, end_t, tzinfo=tz).astimezone(timezone.utc)
    session.add(
        TimeBlock(business_id=business.id, starts_at=starts_at, ends_at=ends_at, reason="блок")
    )
    await session.commit()
    await state.clear()
    await message.answer("Интервал заблокирован.", reply_markup=master_main_kb())


@router.callback_query(F.data == "sch:unblock")
async def sch_unblock_list(callback: CallbackQuery, session: AsyncSession, business: Business):
    now = datetime.now(timezone.utc)
    blocks = (
        await session.scalars(
            select(TimeBlock)
            .where(TimeBlock.business_id == business.id, TimeBlock.ends_at > now)
            .order_by(TimeBlock.starts_at)
        )
    ).all()
    if not blocks:
        await callback.message.answer("Активных блокировок нет.")
        await callback.answer()
        return
    rows = []
    for b in blocks:
        label = f"{b.starts_at.astimezone(_tz(business)).strftime('%d.%m %H:%M')}"
        rows.append(
            [InlineKeyboardButton(text=f"Снять {label}", callback_data=BlockCB(id=b.id).pack())]
        )
    await callback.message.answer(
        "Выберите блокировку:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(BlockCB.filter())
async def sch_unblock_one(
    callback: CallbackQuery,
    callback_data: BlockCB,
    session: AsyncSession,
    business: Business,
):
    block = await session.scalar(
        select(TimeBlock).where(TimeBlock.business_id == business.id, TimeBlock.id == callback_data.id)
    )
    if block is None:
        await callback.answer("Уже снята", show_alert=True)
        return
    await session.delete(block)
    await session.commit()
    await callback.message.answer("Блокировка снята.")
    await callback.answer()
