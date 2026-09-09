from aiogram.filters import Filter


class RoleFilter(Filter):
    def __init__(self, role: str) -> None:
        self.role = role

    async def __call__(self, event, role: str) -> bool:
        return role == self.role
