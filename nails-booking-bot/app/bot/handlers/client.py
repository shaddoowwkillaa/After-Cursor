from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.flow import ask_dates, ask_slots
from app.bot.keyboards import (
    ApptActCB,
    ConfirmCB,
    DateCB,
    ServiceCB,
    SlotCB,
    appointment_actions_kb,
    client_main_kb,
    confirm_kb,
    contact_kb,
    services_kb,
)
from app.models import Appointment, AppointmentStatus, Business, Client, Service
from app.services.booking import (
    SLOT_TAKEN_MESSAGE,
    cancel_appointment,
    create_appointment,
    reschedule_appointment,
)
from app.services.formatting import appointment_card, format_price

router = Router()
router.message.filter(RoleFilter("client"))
router.callback_query.filter(RoleFilter("client"))


class BookFSM(StatesGroup):
    choosing_service = State()
    choosing_date = State()
    choosing_slot = State()
    entering_name = State()
    entering_phone = State()
    confirming = State()


class MoveFSM(StatesGroup):
    choosing_date = State()
    choosing_slot = State()


async def _client(session: AsyncSession, business: Business, telegram_id: int) -> Client | None:
    return await session.scalar(
        select(Client).where(
            Client.business_id == business.id,
            Client.telegram_id == telegram_id,
        )
    )


async def _service(session: AsyncSession, business_id: int, service_id: int) -> Service | None:
    return await session.scalar(
        select(Service).where(
            Service.business_id == business_id,
            Service.id == service_id,
            Service.is_active.is_(True),
        )
    )


@router.message(CommandStart())
async def start(message: Message, business: Business, state: FSMContext):
    await state.clear()
    await message.answer(
        f"Здравствуйте! Это запись к мастеру «{business.name}».\n"
        "Выберите действие:",
        reply_markup=client_main_kb(),
    )


@router.message(F.text == "Отмена")
async def cancel_flow(message: Message, business: Business, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=client_main_kb())


@router.message(F.text == "Записаться")
async def start_booking(message: Message, session: AsyncSession, business: Business, state: FSMContext):
    services = (
        await session.scalars(
            select(Service)
            .where(Service.business_id == business.id, Service.is_active.is_(True))
            .order_by(Service.position, Service.id)
        )
    ).all()
    if not services:
        await message.answer("Пока нет доступных услуг.")
        return
    await state.set_state(BookFSM.choosing_service)
    await message.answer("Выберите услугу:", reply_markup=services_kb(services))


@router.callback_query(BookFSM.choosing_service, ServiceCB.filter())
async def book_service(
    callback: CallbackQuery,
    callback_data: ServiceCB,
    session: AsyncSession,
    business: Business,
    state: FSMContext,
):
    service = await _service(session, business.id, callback_data.id)
    if service is None:
        await callback.answer("Услуга недоступна", show_alert=True)
        return
    await state.update_data(service_id=service.id)
    await state.set_state(BookFSM.choosing_date)
    await callback.message.answer(f"Услуга: {service.name} ({format_price(service.price_minor)})")
    await ask_dates(callback.message, session, business, state)
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
    service = await _service(session, business.id, data["service_id"])
    local_date = datetime.strptime(callback_data.d, "%Y-%m-%d").date()
    await state.update_data(local_date=callback_data.d)
    await state.set_state(BookFSM.choosing_slot)
    ok = await ask_slots(callback.message, session, business, service, local_date, state)
    if not ok:
        await state.set_state(BookFSM.choosing_date)
    await callback.answer()


@router.callback_query(BookFSM.choosing_slot, SlotCB.filter())
async def book_slot(callback: CallbackQuery, callback_data: SlotCB, state: FSMContext):
    starts_at = datetime.fromtimestamp(callback_data.ts, tz=timezone.utc)
    await state.update_data(starts_at=starts_at.isoformat())
    await state.set_state(BookFSM.entering_name)
    await callback.message.answer("Как к вам обращаться? Напишите имя.")
    await callback.answer()


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
    service = await _service(session, business.id, data["service_id"])
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
    service = await _service(session, business.id, data["service_id"])
    starts_at = datetime.fromisoformat(data["starts_at"])

    try:
        appointment = await create_appointment(session, business, client, service, starts_at)
    except IntegrityError:
        await session.rollback()
        await state.set_state(BookFSM.choosing_slot)
        local_date = datetime.strptime(data["local_date"], "%Y-%m-%d").date()
        await callback.message.answer(SLOT_TAKEN_MESSAGE)
        await ask_slots(callback.message, session, business, service, local_date, state)
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
    await state.set_state(MoveFSM.choosing_date)
    await state.update_data(appointment_id=appt.id, service_id=appt.service_id)
    await ask_dates(callback.message, session, business, state)
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
    service = await _service(session, business.id, data["service_id"])
    local_date = datetime.strptime(callback_data.d, "%Y-%m-%d").date()
    await state.update_data(local_date=callback_data.d)
    await state.set_state(MoveFSM.choosing_slot)
    ok = await ask_slots(callback.message, session, business, service, local_date, state)
    if not ok:
        await state.set_state(MoveFSM.choosing_date)
    await callback.answer()


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
    try:
        appt = await reschedule_appointment(
            session, business, appt, client, appt.service, starts_at
        )
    except IntegrityError:
        await session.rollback()
        await callback.message.answer(SLOT_TAKEN_MESSAGE)
        local_date = datetime.strptime(data["local_date"], "%Y-%m-%d").date()
        await ask_slots(callback.message, session, business, appt.service, local_date, state)
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
