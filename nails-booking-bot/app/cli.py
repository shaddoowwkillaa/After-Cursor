import argparse
import asyncio
from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot
from sqlalchemy import select

from app.db import async_session_factory
from app.models import Business, WorkSchedule


async def add_business(args: argparse.Namespace) -> None:
    try:
        ZoneInfo(args.timezone)
    except ZoneInfoNotFoundError:
        raise SystemExit(f"Неизвестный часовой пояс: {args.timezone}")

    reminders = [int(part.strip()) for part in args.reminders.split(",") if part.strip()]
    if not reminders:
        raise SystemExit("Список напоминаний пуст.")

    bot = Bot(token=args.bot_token)
    try:
        me = await bot.get_me()
    except Exception as exc:
        raise SystemExit(f"Токен невалиден (getMe): {exc}") from exc
    finally:
        await bot.session.close()

    async with async_session_factory() as session:
        exists = await session.scalar(
            select(Business.id).where(Business.bot_token == args.bot_token)
        )
        if exists:
            raise SystemExit("Бизнес с таким токеном уже существует.")

        business = Business(
            name=args.name,
            bot_token=args.bot_token,
            bot_username=me.username,
            owner_telegram_id=args.owner_telegram_id,
            timezone=args.timezone,
            slot_step_minutes=args.slot_step,
            min_notice_minutes=args.min_notice,
            max_booking_days=args.max_days,
            reminder_offsets_minutes=reminders,
            is_active=True,
        )
        session.add(business)
        await session.flush()
        for weekday in range(7):
            working = weekday < 5
            session.add(
                WorkSchedule(
                    business_id=business.id,
                    weekday=weekday,
                    is_working=working,
                    start_time=time(10, 0) if working else None,
                    end_time=time(20, 0) if working else None,
                )
            )
        await session.commit()
        print(
            f"Создан бизнес id={business.id}, бот=@{me.username}, "
            f"владелец={args.owner_telegram_id}, tz={args.timezone}"
        )
        print("Перезапустите приложение, чтобы бот начал polling.")


async def list_businesses() -> None:
    async with async_session_factory() as session:
        rows = (
            await session.scalars(select(Business).order_by(Business.id))
        ).all()
    if not rows:
        print("Бизнесов нет.")
        return
    for b in rows:
        flag = "active" if b.is_active else "off"
        print(
            f"{b.id}\t{b.name}\t@{b.bot_username or '-'}\t"
            f"owner={b.owner_telegram_id}\t{b.timezone}\t{flag}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Онбординг мастеров nails-booking")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add-business", help="Подключить нового мастера")
    add.add_argument("--name", required=True)
    add.add_argument("--bot-token", required=True)
    add.add_argument("--owner-telegram-id", required=True, type=int)
    add.add_argument("--timezone", required=True)
    add.add_argument("--slot-step", type=int, default=15)
    add.add_argument("--min-notice", type=int, default=60)
    add.add_argument("--max-days", type=int, default=30)
    add.add_argument("--reminders", default="1440,120", help="Минуты до визита через запятую")

    sub.add_parser("list-businesses", help="Список подключённых мастеров")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "add-business":
        asyncio.run(add_business(args))
    elif args.command == "list-businesses":
        asyncio.run(list_businesses())


if __name__ == "__main__":
    main()
