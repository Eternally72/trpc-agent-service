"""Explicit purge endpoints; legacy DELETE routes continue to mean disable."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from trpc_service.admin.auth import ManagementActor, require_platform_admin, require_tenant_admin
from trpc_service.admin.deletion import build_plan, delete_sql, lock_deletion_tables
from trpc_service.admin.deletion_cleanup import prepare_cleanup, clean_record, receipt
from trpc_service.admin.deletion_models import DeletionRecord
from trpc_service.storage.database import get_session

router = APIRouter(tags=["resource-deletion"])
PLATFORM = {"tenants", "model-catalog", "model-credentials", "principals"}
TENANT = {"agents", "channel-bindings", "mcp-connections", "model-profiles", "documents"}


class PurgeRequest(BaseModel):
    confirmation: str = Field(min_length=1, max_length=255)


def authorize(kind: str, allowed: set[str], actor: ManagementActor) -> None:
    if kind not in allowed:
        raise HTTPException(404, "资源类型不存在")
    if kind == "model-profiles" and not actor.is_platform_admin:
        raise HTTPException(403, "模型策略只能由平台管理员管理")


async def execute(request: Request, session: AsyncSession, kind: str, resource_id: UUID,
                  tenant_id: UUID | None, actor: ManagementActor,
                  payload: PurgeRequest) -> dict[str, Any]:
    try:
        await lock_deletion_tables(session)
        plan = await build_plan(session, kind, resource_id, tenant_id, actor)
        if payload.confirmation != plan.name:
            raise HTTPException(422, "确认名称不匹配，请输入完整名称")
        await prepare_cleanup(session, plan, request.app.state.container.storage_backends,
                              request.app.state.settings)
        record = await delete_sql(session, plan, actor)
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(409, "资源引用已发生变化，请刷新删除预览后重试") from error
    except OperationalError as error:
        await session.rollback()
        raise HTTPException(409, "资源仍在被使用，请稍后刷新并重试") from error
    await clean_record(session, record, request.app.state.container.storage_backends,
                       request.app.state.settings)
    return receipt(record)


@router.get("/admin/resources/{kind}/{resource_id}/deletion-preview")
async def platform_preview(
    kind: str,
    resource_id: UUID,
    actor: ManagementActor = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    authorize(kind, PLATFORM, actor)
    return (await build_plan(session, kind, resource_id, None, actor)).public()


@router.delete("/admin/resources/{kind}/{resource_id}/purge")
async def platform_purge(
    kind: str,
    resource_id: UUID,
    payload: PurgeRequest,
    request: Request,
    actor: ManagementActor = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    authorize(kind, PLATFORM, actor)
    return await execute(request, session, kind, resource_id, None, actor, payload)


@router.get("/tenants/{tenant_id}/resources/{kind}/{resource_id}/deletion-preview")
async def tenant_preview(
    tenant_id: UUID,
    kind: str,
    resource_id: UUID,
    actor: ManagementActor = Depends(require_tenant_admin),
    session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    authorize(kind, TENANT, actor)
    return (await build_plan(session, kind, resource_id, tenant_id, actor)).public()


@router.delete("/tenants/{tenant_id}/resources/{kind}/{resource_id}/purge")
async def tenant_purge(
    tenant_id: UUID,
    kind: str,
    resource_id: UUID,
    payload: PurgeRequest,
    request: Request,
    actor: ManagementActor = Depends(require_tenant_admin),
    session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    authorize(kind, TENANT, actor)
    return await execute(request, session, kind, resource_id, tenant_id, actor, payload)


async def list_records(session: AsyncSession, tenant_id: UUID | None) -> dict[str, Any]:
    query = select(DeletionRecord)
    if tenant_id is not None:
        query = query.where(DeletionRecord.tenant_id == tenant_id)
    rows = await session.scalars(
        query.order_by((DeletionRecord.status != "completed").desc(),
                       DeletionRecord.created_at.desc()).limit(100))
    return {"items": [receipt(row) for row in rows]}


@router.get("/admin/deletions")
async def platform_records(_: ManagementActor = Depends(require_platform_admin),
                           session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    return await list_records(session, None)


@router.get("/tenants/{tenant_id}/deletions")
async def tenant_records(
    tenant_id: UUID,
    _: ManagementActor = Depends(require_tenant_admin),
    session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    return await list_records(session, tenant_id)


async def retry(request: Request, session: AsyncSession, deletion_id: UUID,
                tenant_id: UUID | None) -> dict[str, Any]:
    query = select(DeletionRecord).where(DeletionRecord.deletion_id == deletion_id)
    if tenant_id is not None:
        query = query.where(DeletionRecord.tenant_id == tenant_id)
    record = await session.scalar(query)
    if record is None:
        raise HTTPException(404, "删除记录不存在")
    await clean_record(session, record, request.app.state.container.storage_backends,
                       request.app.state.settings)
    return receipt(record)


@router.post("/admin/deletions/{deletion_id}/retry")
async def platform_retry(
    deletion_id: UUID,
    request: Request,
    _: ManagementActor = Depends(require_platform_admin),
    session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    return await retry(request, session, deletion_id, None)


@router.post("/tenants/{tenant_id}/deletions/{deletion_id}/retry")
async def tenant_retry(
    tenant_id: UUID,
    deletion_id: UUID,
    request: Request,
    _: ManagementActor = Depends(require_tenant_admin),
    session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    return await retry(request, session, deletion_id, tenant_id)
