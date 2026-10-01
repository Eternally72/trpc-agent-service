"""Coordinate destructive management with configuration writes and ingestion."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from trpc_service.admin.deletion_models import DeletionRecord

# Shared transactions permit ordinary changes concurrently. A purge takes the
# exclusive counterpart so validated JSON references cannot change underneath it.
_LOCK_ID = 786234912


@asynccontextmanager
async def configuration_guard(engine: AsyncEngine,
                              *,
                              exclusive: bool = False) -> AsyncIterator[None]:
    if engine.dialect.name != "postgresql":
        yield
        return
    async with engine.begin() as connection:
        function = "pg_try_advisory_xact_lock" if exclusive else "pg_try_advisory_xact_lock_shared"
        acquired = await connection.scalar(text(f"SELECT {function}(:key)"), {"key": _LOCK_ID})
        if not acquired:
            raise HTTPException(409, "配置正在更新或清理，请稍后重试")
        yield


async def require_cleanup_complete(session: AsyncSession, tenant_id: UUID) -> None:
    pending = await session.scalar(
        select(DeletionRecord.deletion_id).where(DeletionRecord.tenant_id == tenant_id,
                                                 DeletionRecord.status != "completed").limit(1))
    if pending is not None:
        raise HTTPException(409, "该租户有未完成的文件清理，请在删除记录中重试清理")
