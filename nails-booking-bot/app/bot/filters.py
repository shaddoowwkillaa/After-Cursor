from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Business, Staff


class RoleFilter(BaseFilter):
    def __init__(self, role: str):
        self.role = role

    async def __call__(
        self,
        event: Message | CallbackQuery,
        business: Business | None = None,
        session: AsyncSession | None = None,
    ) -> bool:
        if business is None or session is None or event.from_user is None:
            return False
        user_id = event.from_user.id
        staff = await session.scalar(
            select(Staff).where(
                Staff.business_id == business.id,
                Staff.telegram_id == user_id,
                Staff.is_active.is_(True),
            )
        )
        if self.role == "master":
            return staff is not None
        return staff is None