from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app.bot.manager import start_polling, stop_polling
from app.db import async_session_factory


@asynccontextmanager
async def lifespan(app: FastAPI):
    await start_polling()
    yield
    await stop_polling()


app = FastAPI(
    title="Nails Booking Bot",
    description="Backend for Telegram booking bots for nail masters",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    return {
        "status": "ok",
    }


@app.get("/health/db")
async def health_db():
    async with async_session_factory() as session:
        result = await session.execute(text("SELECT 1"))
        value = result.scalar_one()

    return {
        "status": "ok",
        "db": value,
    }
