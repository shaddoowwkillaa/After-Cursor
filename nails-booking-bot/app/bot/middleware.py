from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.db import async_session_factory
from app.models import Business, Client


def _extract_user_and_text(event: TelegramObject):
    if isinstance(event, Message):
        return event.from_user, event.text
    if isinstance(event, CallbackQuery):
        return event.from_user, event.data
    if isinstance(event, Update):
        if event.message:
            return event.message.from_user, event.message.text
        if event.callback_query:
            return event.callback_query.from_user, event.callback_query.data
    return None, None


class TenantMiddleware(BaseMiddleware):
    """Кладёт в data бизнес, роль и сессию БД. Всегда фильтрует по токену бота."""

    async def __call__(self, handler, event, data):
        bot = data["bot"]
        async with async_session_factory() as session:
            business = await session.scalar(
                select(Business).where(
                    Business.bot_token == bot.token,
                    Business.is_active.is_(True),
                )
            )
            if business is None:
                return None

            user, text = _extract_user_and_text(event)
            role = "client"
            if user is not None and user.id == business.owner_telegram_id:
                role = "master"
            elif user is not None and text == "/start":
                stmt = (
                    insert(Client)
                    .values(
                        business_id=business.id,
                        telegram_id=user.id,
                        full_name=user.full_name,
                    )
                    .on_conflict_do_update(
                        constraint="uq_clients_business_telegram",
                        set_={"full_name": user.full_name},
                    )
                )
                await session.execute(stmt)
                await session.commit()

            data["session"] = session
            data["business"] = business
            data["role"] = role
            data["user"] = user
            result = await handler(event, data)
            if session.in_transaction():
                await session.commit()
            return result
