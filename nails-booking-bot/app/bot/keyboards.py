from __future__ import annotations

from datetime import date, datetime

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from app.models import Service
from app.services.formatting import WEEKDAYS_RU, format_price, format_time


def client_main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Записаться")],
            [KeyboardButton(text="Мои записи")],
        ],
        resize_keyboard=True,
    )


def master_main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Записи"), KeyboardButton(text="Клиенты")],
            [KeyboardButton(text="Услуги"), KeyboardButton(text="Окошки")],
        ],
        resize_keyboard=True,
    )


def remove_kb() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def contact_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Отправить контакт", request_contact=True)],
            [KeyboardButton(text="Отмена")],
        ],
        resize_keyboard=True,
    )


def cancel_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отмена")]],
        resize_keyboard=True,
    )


class ServiceCB(CallbackData, prefix="svc"):
    id: int


class DateCB(CallbackData, prefix="dt"):
    d: str


class SlotCB(CallbackData, prefix="sl"):
    ts: int


class ConfirmCB(CallbackData, prefix="cf"):
    ok: int


class ApptActCB(CallbackData, prefix="aa"):
    id: int
    act: str


class DayCB(CallbackData, prefix="day"):
    n: int


class SvcActCB(CallbackData, prefix="sa"):
    id: int
    act: str


class ClientCB(CallbackData, prefix="cl"):
    id: int


class BlockCB(CallbackData, prefix="tb"):
    id: int


def services_kb(services: list[Service]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{s.name} · {format_price(s.price_minor)} · {s.duration_minutes} мин",
                callback_data=ServiceCB(id=s.id).pack(),
            )
        ]
        for s in services
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dates_kb(dates: list[date]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for d in dates:
        label = f"{WEEKDAYS_RU[d.weekday()]} {d.strftime('%d.%m')}"
        row.append(InlineKeyboardButton(text=label, callback_data=DateCB(d=d.isoformat()).pack()))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def slots_kb(slots: list[datetime], tz_name: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for slot in slots:
        row.append(
            InlineKeyboardButton(
                text=format_time(slot, tz_name),
                callback_data=SlotCB(ts=int(slot.timestamp())).pack(),
            )
        )
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Подтвердить", callback_data=ConfirmCB(ok=1).pack()),
                InlineKeyboardButton(text="Отмена", callback_data=ConfirmCB(ok=0).pack()),
            ]
        ]
    )


def appointment_actions_kb(appointment_id: int, *, for_master: bool) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text="Отменить", callback_data=ApptActCB(id=appointment_id, act="c").pack()
        ),
        InlineKeyboardButton(
            text="Перенести", callback_data=ApptActCB(id=appointment_id, act="r").pack()
        ),
    ]
    if for_master:
        buttons.append(
            InlineKeyboardButton(
                text="Завершена", callback_data=ApptActCB(id=appointment_id, act="d").pack()
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def master_days_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data="md:today"),
                InlineKeyboardButton(text="Завтра", callback_data="md:tomorrow"),
            ],
            [InlineKeyboardButton(text="Выбрать дату", callback_data="md:pick")],
        ]
    )


def weekdays_kb() -> InlineKeyboardMarkup:
    names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=names[i], callback_data=DayCB(n=i).pack())
                for i in range(7)
            ]
        ]
    )


def schedule_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Часы дня недели", callback_data="sch:hours")],
            [InlineKeyboardButton(text="Выходной / отпуск", callback_data="sch:close")],
            [InlineKeyboardButton(text="Особый день", callback_data="sch:open")],
            [InlineKeyboardButton(text="Заблокировать время", callback_data="sch:block")],
            [InlineKeyboardButton(text="Снять блокировку", callback_data="sch:unblock")],
        ]
    )
