from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.flow import ask_dates, ask_slots
from app.bot.keyboards import (
    ApptActCB,
    ConfirmCB,
    DateCB,
    ServiceCB,
    SlotCB,
    StaffCB,
    appointment_actions_kb,
    client_main_kb,
    confirm_kb,
    contact_kb,
    services_kb,
    staff_kb,
)
from app.models import Appointment, AppointmentStatus, Business, Client, Service, Staff
from app.services.booking import (
    SLOT_TAKEN_MESSAGE,
    cancel_appointment,
    create_appointment,
    is_slot_conflict,
    reschedule_appointment,
)
from app.services.formatting import appointment_card, format_price

router = Router()
router.message.filter(RoleFilter("client"))
router.callback_query.filter(RoleFilter("client"))

BACK_TO_DATES = "back:dates"


class BookFSM(StatesGroup):
    choosing_staff = State()
    choosing_service = State()
    choosing_date = State()
    choosing_slot = State()
    entering_name = State()
    entering_phone = State()
    confirming = State()


class MoveFSM(StatesGroup):
    choosing_date = State()
    choosing_slot = State()


def _back_dates_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Выбрать другую дату", callback_data=BACK_TO_DATES)]
        ]
    )


async def _client(session: AsyncSession, business: Business, telegram_id: int) -> Client | None:
    return await session.scalar(
        select(Client).where(
            Client.business_id == business.id,
            Client.telegram_id == telegram_id,
        )
    )


async def _service(session: AsyncSession, staff_id: int, service_id: int) -> Service | None:
    return await session.scalar(
        select(Service).where(
            Service.staff_id == staff_id,
            Service.id == service_id,
            Service.is_active.is_(True),
        )
    )


async def _staff(session: AsyncSession, business: Business, staff_id: int) -> Staff | None:
    return await session.scalar(
        select(Staff).where(
            Staff.business_id == business.id,
            Staff.id == staff_id,
            Staff.is_active.is_(True),
        )
    )


@router.message(CommandStart())
async def start(message: Message, business: Business, state: FSMContext):
    await state.clear()
    await message.answer(
        f"Здравствуйте! Это запись в салон «{business.name}».\n"
        "Выберите действие:",
        reply_markup=client_main_kb(),
    )


@router.message(F.text == "Отмена")
async def cancel_flow(message: Message, business: Business, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=client_main_kb())


@router.message(F.text == "Записаться")
async def start_booking(
    message: Message,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    staff_list = (
        await session.scalars(
            select(Staff)
            .where(Staff.business_id == business.id, Staff.is_active.is_(True))
            .order_by(Staff.id)
        )
    ).all()
    if not staff_list:
        await message.answer("Пока нет доступных мастеров.")
        return
    if len(staff_list) == 1:
        # Если мастер один — пропускаем выбор, сразу к услугам
        await state.update_data(staff_id=staff_list[0].id)
        await _show_services_for_staff(message, session, business, staff_list[0], state)
        return
    await state.set_state(BookFSM.choosing_staff)
    await message.answer("Выберите мастера:", reply_markup=staff_kb(staff_list))


@router.callback_query(BookFSM.choosing_staff, StaffCB.filter())
async def book_staff(
    callback: CallbackQuery,
    callback_data: StaffCB,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    staff = await _staff(session, business, callback_data.id)
    if staff is None:
        await callback.answer("Мастер недоступен", show_alert=True)
        return
    await state.update_data(staff_id=staff.id)
    await _show_services_for_staff(callback.message, session, business, staff, state)
    await callback.answer()


async def _show_services_for_staff(
    message, session: AsyncSession, business: Business, staff: Staff, state: FSMContext
):
    services = (
        await session.scalars(
            select(Service)
            .where(
                Service.business_id == business.id,
                Service.staff_id == staff.id,
                Service.is_active.is_(True),
            )
            .order_by(Service.position, Service.id)
        )
    ).all()
    if not services:
        await message.answer(f"У мастера {staff.name} пока нет доступных услуг.")
        await state.clear()
        return
    await state.set_state(BookFSM.choosing_service)
    await message.answer(
        f"Мастер: {staff.name}\nВыберите услугу:",
        reply_markup=services_kb(services),
    )


@router.callback_query(BookFSM.choosing_service, ServiceCB.filter())
async def book_service(
    callback: CallbackQuery,
    callback_data: ServiceCB,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    data = await state.get_data()
    service = await _service(session, data["staff_id"], callback_data.id)
    if service is None:
        await callback.answer("Услуга недоступна", show_alert=True)
        return
    staff = await _staff(session, business, data["staff_id"])
    await state.update_data(service_id=service.id)
    await state.set_state(BookFSM.choosing_date)
    await callback.message.answer(f"Услуга: {service.name} ({format_price(service.price_minor)})")
    await ask_dates(callback.message, session, business, staff, state)
    await callback.answer()


@router.callback_query(BookFSM.choosing_date, DateCB.filter())
async def book_date(
    callback: CallbackQuery,
    callback_data: DateCB,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    data = await state.get_data()
    service = await _service(session, data["staff_id"], data["service_id"])
    staff = await _staff(session, business, data["staff_id"])
    local_date = datetime.strptime(callback_data.d, "%Y-%m-%d").date()
    await state.update_data(local_date=callback_data.d)
    await state.set_state(BookFSM.choosing_slot)
    ok = await ask_slots(callback.message, session, business, staff, service, local_date, state)
    if not ok:
        await state.set_state(BookFSM.choosing_date)
    else:
        await callback.message.answer("Дата не подошла?", reply_markup=_back_dates_kb())
    await callback.answer()


@router.callback_query(BookFSM.choosing_slot, F.data == BACK_TO_DATES)
async def book_back_to_dates(
    callback: CallbackQuery,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    data = await state.get_data()
    staff = await _staff(session, business, data["staff_id"])
    await state.set_state(BookFSM.choosing_date)
    await ask_dates(callback.message, session, business, staff, state)
    await callback.answer()


@router.callback_query(BookFSM.choosing_slot, SlotCB.filter())
async def book_slot(callback: CallbackQuery, callback_data: SlotCB, state: FSMContext):
    starts_at = datetime.fromtimestamp(callback_data.ts, tz=timezone.utc)
    await state.update_data(starts_at=starts_at.isoformat())
    await state.set_state(BookFSM.entering_name)
    await callback.message.answer("Как к вам обращаться? Напишите имя.")
    await callback.answer()


@router.callback_query(BookFSM.choosing_slot, F.data == "slot:locked")
async def slot_locked_client(callback: CallbackQuery):
    await callback.answer("Это время уже занято.", show_alert=True)


@router.message(BookFSM.entering_name, F.text)
async def book_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Имя слишком короткое. Введите ещё раз.")
        return
    await state.update_data(full_name=name[:100])
    await state.set_state(BookFSM.entering_phone)
    await message.answer(
        "Отправьте номер телефона кнопкой ниже или напишите его текстом.",
        reply_markup=contact_kb(),
    )


@router.message(BookFSM.entering_phone, F.contact)
async def book_phone_contact(message: Message, state: FSMContext, session, business: Business):
    await _save_phone_and_confirm(message, state, session, business, message.contact.phone_number)


@router.message(BookFSM.entering_phone, F.text)
async def book_phone_text(message: Message, state: FSMContext, session, business: Business):
    if message.text == "Отмена":
        await cancel_flow(message, business, state)
        return
    phone = (message.text or "").strip()
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 7:
        await message.answer("Не похоже на телефон. Попробуйте ещё раз или нажмите кнопку.")
        return
    await _save_phone_and_confirm(message, state, session, business, phone[:32])


async def _save_phone_and_confirm(message, state, session, business, phone: str):
    data = await state.get_data()
    service = await _service(session, data["staff_id"], data["service_id"])
    starts_at = datetime.fromisoformat(data["starts_at"])
    await state.update_data(phone=phone)
    await state.set_state(BookFSM.confirming)
    await message.answer(
        "Проверьте запись:\n"
        f"{appointment_card(_preview(starts_at, service), business, service)}\n"
        f"Имя: {data['full_name']}\n"
        f"Телефон: {phone}",
        reply_markup=confirm_kb(),
    )


def _preview(starts_at: datetime, service: Service) -> Appointment:
    return Appointment(
        id=0,
        business_id=service.business_id,
        client_id=0,
        service_id=service.id,
        starts_at=starts_at,
        ends_at=starts_at,
        status=AppointmentStatus.confirmed,
    )


@router.callback_query(BookFSM.confirming, ConfirmCB.filter())
async def book_confirm(
    callback: CallbackQuery,
    callback_data: ConfirmCB,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
    user,
):
    if not callback_data.ok:
        await state.clear()
        await callback.message.answer("Запись не создана.", reply_markup=client_main_kb())
        await callback.answer()
        return

    data = await state.get_data()
    client = await _client(session, business, user.id)
    if client is None:
        client = Client(business_id=business.id, telegram_id=user.id)
        session.add(client)
        await session.flush()
    client.full_name = data["full_name"]
    client.phone = data["phone"]
    service = await _service(session, data["staff_id"], data["service_id"])
    staff = await _staff(session, business, data["staff_id"])
    starts_at = datetime.fromisoformat(data["starts_at"])

    try:
        appointment = await create_appointment(session, business, staff, client, service, starts_at)
    except DBAPIError as exc:
        await session.rollback()
        if not is_slot_conflict(exc):
            raise
        await state.set_state(BookFSM.choosing_slot)
        local_date = datetime.strptime(data["local_date"], "%Y-%m-%d").date()
        await callback.message.answer(SLOT_TAKEN_MESSAGE)
        await ask_slots(callback.message, session, business, staff, service, local_date, state)
        await callback.answer()
        return

    await state.clear()
    await callback.message.answer(
        "Запись подтверждена!\n" + appointment_card(appointment, business, service),
        reply_markup=client_main_kb(),
    )
    await callback.answer()


@router.message(F.text == "Мои записи")
async def my_appointments(message: Message, session: AsyncSession, business: Business, user):
    client = await _client(session, business, user.id)
    if client is None:
        await message.answer("У вас пока нет записей.")
        return
    now = datetime.now(timezone.utc)
    appts = (
        await session.scalars(
            select(Appointment)
            .where(
                Appointment.business_id == business.id,
                Appointment.client_id == client.id,
                Appointment.status == AppointmentStatus.confirmed,
                Appointment.starts_at > now,
            )
            .order_by(Appointment.starts_at)
        )
    ).all()
    if not appts:
        await message.answer("Ближайших записей нет.")
        return
    for appt in appts:
        await message.answer(
            appointment_card(appt, business, appt.service),
            reply_markup=appointment_actions_kb(appt.id, for_master=False),
        )


@router.callback_query(ApptActCB.filter(F.act == "c"))
async def client_cancel(
    callback: CallbackQuery,
    callback_data: ApptActCB,
    session: AsyncSession,
    business: Business,
    user,
):
    appt = await _owned_future(session, business, user.id, callback_data.id)
    if appt is None:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    # Пока уведомление летит owner-у; на Шаге 6 заменим на staff записи
    await cancel_appointment(session, business, appt, business.owner_telegram_id)
    await callback.message.answer("Запись отменена.", reply_markup=client_main_kb())
    await callback.answer()


@router.callback_query(ApptActCB.filter(F.act == "r"))
async def client_reschedule_start(
    callback: CallbackQuery,
    callback_data: ApptActCB,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
    user,
):
    appt = await _owned_future(session, business, user.id, callback_data.id)
    if appt is None:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    staff = await _staff(session, business, appt.staff_id)
    if staff is None:
        await callback.answer("Мастер недоступен", show_alert=True)
        return
    await state.set_state(MoveFSM.choosing_date)
    await state.update_data(
        appointment_id=appt.id,
        service_id=appt.service_id,
        staff_id=staff.id,
    )
    await ask_dates(callback.message, session, business, staff, state)
    await callback.answer()


@router.callback_query(MoveFSM.choosing_date, DateCB.filter())
async def move_date(
    callback: CallbackQuery,
    callback_data: DateCB,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    data = await state.get_data()
    service = await _service(session, data["staff_id"], data["service_id"])
    staff = await _staff(session, business, data["staff_id"])
    local_date = datetime.strptime(callback_data.d, "%Y-%m-%d").date()
    await state.update_data(local_date=callback_data.d)
    await state.set_state(MoveFSM.choosing_slot)
    ok = await ask_slots(callback.message, session, business, staff, service, local_date, state)
    if not ok:
        await state.set_state(MoveFSM.choosing_date)
    else:
        await callback.message.answer("Дата не подошла?", reply_markup=_back_dates_kb())
    await callback.answer()


@router.callback_query(MoveFSM.choosing_slot, F.data == BACK_TO_DATES)
async def move_back_to_dates(
    callback: CallbackQuery,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    data = await state.get_data()
    staff = await _staff(session, business, data["staff_id"])
    await state.set_state(MoveFSM.choosing_date)
    await ask_dates(callback.message, session, business, staff, state)
    await callback.answer()


@router.callback_query(MoveFSM.choosing_slot, F.data == "slot:locked")
async def slot_locked_move(callback: CallbackQuery):
    await callback.answer("Это время уже занято.", show_alert=True)


@router.callback_query(MoveFSM.choosing_slot, SlotCB.filter())
async def move_slot(
    callback: CallbackQuery,
    callback_data: SlotCB,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
    user,
):
    data = await state.get_data()
    appt = await _owned_future(session, business, user.id, data["appointment_id"])
    if appt is None:
        await state.clear()
        await callback.answer("Запись не найдена", show_alert=True)
        return
    starts_at = datetime.fromtimestamp(callback_data.ts, tz=timezone.utc)
    client = await _client(session, business, user.id)
    staff = await _staff(session, business, appt.staff_id)
    try:
        appt = await reschedule_appointment(session, business, appt, client, appt.service, starts_at)
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
    await callback.message.answer(
        "Запись перенесена.\n" + appointment_card(appt, business, appt.service),
        reply_markup=client_main_kb(),
    )
    await callback.answer()


async def _owned_future(session, business, telegram_id, appointment_id) -> Appointment | None:
    client = await _client(session, business, telegram_id)
    if client is None:
        return None
    now = datetime.now(timezone.utc)
    return await session.scalar(
        select(Appointment).where(
            Appointment.business_id == business.id,
            Appointment.client_id == client.id,
            Appointment.id == appointment_id,
            Appointment.status == AppointmentStatus.confirmed,
            Appointment.starts_at > now,
        )
    )