import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select

from app.bot.handlers import client as client_handlers
from app.bot.handlers import master as master_handlers
from app.bot.middleware import TenantMiddleware
from app.db import async_session_factory
from app.models import Business

log = logging.getLogger(__name__)

bots_by_business_id: dict[int, Bot] = {}
_dispatcher: Dispatcher | None = None
_polling_task: asyncio.Task | None = None
_notify_task: asyncio.Task | None = None


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(TenantMiddleware())
    dp.include_router(master_handlers.router)
    dp.include_router(client_handlers.router)
    return dp


def get_bot(business_id: int) -> Bot | None:
    return bots_by_business_id.get(business_id)


async def start_polling() -> None:
    global _dispatcher, _polling_task, _notify_task
    from app.workers.notification_loop import run_notification_loop

    _dispatcher = build_dispatcher()
    async with async_session_factory() as session:
        businesses = (
            await session.scalars(select(Business).where(Business.is_active.is_(True)))
        ).all()

    bots: list[Bot] = []
    for business in businesses:
        bot = Bot(token=business.bot_token)
        try:
            me = await bot.get_me()
        except Exception as exc:
            log.error("не удалось запустить бота бизнеса %s: %s", business.id, exc)
            await bot.session.close()
            continue
        log.info("бот @%s для бизнеса %s", me.username, business.id)
        bots_by_business_id[business.id] = bot
        bots.append(bot)

    if bots:
        _polling_task = asyncio.create_task(
            _dispatcher.start_polling(*bots, handle_signals=False)
        )
    _notify_task = asyncio.create_task(run_notification_loop(get_bot))


async def stop_polling() -> None:
    global _polling_task, _notify_task, _dispatcher
    if _dispatcher is not None:
        await _dispatcher.stop_polling()
    if _polling_task is not None:
        try:
            await _polling_task
        except asyncio.CancelledError:
            pass
        _polling_task = None
    if _notify_task is not None:
        _notify_task.cancel()
        try:
            await _notify_task
        except asyncio.CancelledError:
            pass
        _notify_task = None
    for bot in list(bots_by_business_id.values()):
        await bot.session.close()
    bots_by_business_id.clear()
    _dispatcher = None
