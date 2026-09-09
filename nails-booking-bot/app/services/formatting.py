from datetime import datetime
from zoneinfo import ZoneInfo

from app.models import Appointment, AppointmentStatus, Business, Service

STATUS_LABELS = {
    AppointmentStatus.confirmed: "подтверждена",
    AppointmentStatus.canceled: "отменена",
    AppointmentStatus.completed: "завершена",
}

WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def format_price(price_minor: int, currency: str = "BYN") -> str:
    return f"{price_minor / 100:.2f} {currency}"


def to_local(dt_utc: datetime, tz_name: str) -> datetime:
    return dt_utc.astimezone(ZoneInfo(tz_name))


def format_dt(dt_utc: datetime, tz_name: str) -> str:
    return to_local(dt_utc, tz_name).strftime("%d.%m.%Y %H:%M")


def format_time(dt_utc: datetime, tz_name: str) -> str:
    return to_local(dt_utc, tz_name).strftime("%H:%M")


def format_date(dt_utc: datetime, tz_name: str) -> str:
    return to_local(dt_utc, tz_name).strftime("%d.%m.%Y")


def appointment_card(appointment: Appointment, business: Business, service: Service) -> str:
    status = STATUS_LABELS.get(appointment.status, appointment.status)
    return (
        f"Запись #{appointment.id}\n"
        f"Услуга: {service.name}\n"
        f"Дата: {format_date(appointment.starts_at, business.timezone)}\n"
        f"Время: {format_time(appointment.starts_at, business.timezone)} "
        f"({business.timezone})\n"
        f"Длительность: {service.duration_minutes} мин\n"
        f"Цена: {format_price(service.price_minor)}\n"
        f"Статус: {status}"
    )
