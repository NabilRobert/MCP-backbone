from collections.abc import AsyncIterator
from datetime import datetime
from typing import ClassVar

from sqlalchemy import DateTime
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(settings.database_url)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    # every Mapped[datetime] column is timezone-aware by default -- app
    # code consistently uses datetime.now(UTC), and a naive-vs-aware
    # mismatch is a runtime error (caught by testing the seed script
    # against a real database), not just a style preference.
    type_annotation_map: ClassVar = {datetime: DateTime(timezone=True)}


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
