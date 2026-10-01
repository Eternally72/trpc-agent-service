"""Idempotent cleanup of external projections after an SQL deletion commits."""

import asyncio
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trpc_service.admin.deletion import DeletionPlan, table
from trpc_service.admin.deletion_models import DeletionRecord
from trpc_service.config import Settings
from trpc_service.storage.adapters.inmemory import InMemorySessionStore
from trpc_service.storage.adapters.pgvector import PgVectorKnowledgeStore
from trpc_service.storage.adapters.postgresql_auxiliary import _PostgreSQLAdapter
from trpc_service.storage.adapters.s3 import S3ArtifactStore
from trpc_service.storage.registry import StorageBackendRegistry


async def prepare_cleanup(session: AsyncSession, plan: DeletionPlan,
                          registry: StorageBackendRegistry, settings: Settings) -> None:
    if plan.kind not in {"tenants", "agents", "documents"}:
        return
    artifacts: list[str] = []
    chunks: list[str] = []
    if plan.kind == "documents":
        artifacts = list(await session.scalars(
            select(table("knowledge_artifact").c.artifact_id).where(
                plan.predicates["knowledge_artifact"])))
        chunks = list(await session.scalars(
            select(table("knowledge_chunk").c.chunk_id).where(plan.predicates["knowledge_chunk"])))
    for name in registry.names:
        backend = registry.resolve(name)
        if isinstance(backend.session, InMemorySessionStore):
            plan.cleanup.append({
                "kind": "inmemory",
                "backend": name,
                "artifacts": artifacts,
                "chunks": chunks
            })
            continue
        if plan.kind in {"tenants", "agents"}:
            # Memory and summaries can route to a separate SQL database.
            for port in ("memory", "summary"):
                if isinstance(getattr(backend, port), _PostgreSQLAdapter):
                    plan.cleanup.append({"kind": port, "backend": name})
        if plan.kind != "agents":
            if isinstance(backend.artifact, S3ArtifactStore):
                plan.cleanup.append({"kind": "artifacts", "backend": name, "ids": artifacts})
            if isinstance(backend.knowledge, PgVectorKnowledgeStore):
                plan.cleanup.append({"kind": "vectors", "backend": name, "ids": chunks})
    if plan.kind in {"tenants", "agents"} and settings.resolved_session_cache_url:
        plan.cleanup.append({"kind": "cache"})


async def clean_record(session: AsyncSession, record: DeletionRecord,
                       registry: StorageBackendRegistry, settings: Settings) -> None:
    """Persist every completed step; an interrupted request remains retryable."""
    if record.status == "completed":
        return
    if record.tenant_id is None:
        raise ValueError("cleanup requires a tenant scope")
    tenant = record.tenant_id
    agent = UUID(record.resource_id) if record.resource_type == "agents" else None
    whole_tenant = record.resource_type == "tenants"
    remaining = list(record.cleanup)
    try:
        async with asyncio.timeout(20):
            while remaining:
                step = remaining[0]
                kind = step["kind"]
                if kind == "cache":
                    url = settings.resolved_session_cache_url
                    if not url:
                        raise RuntimeError("configured cache is unavailable")
                    client: Redis = Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
                    try:
                        suffix = "*" if agent is None else str(agent) + ":*"
                        pattern = f"trpc:session-cache:{tenant}:{suffix}"
                        batch = []
                        async for key in client.scan_iter(match=pattern, count=200):
                            batch.append(key)
                            if len(batch) >= 200:
                                await client.delete(*batch)
                                batch.clear()
                        if batch:
                            await client.delete(*batch)
                    finally:
                        await client.aclose()
                else:
                    backend = registry.resolve(step["backend"])
                    if kind == "inmemory" and isinstance(backend.session, InMemorySessionStore):
                        await backend.session.purge(tenant,
                                                    agent,
                                                    artifact_ids=step["artifacts"] if
                                                    record.resource_type == "documents" else None,
                                                    chunk_ids=step["chunks"] if record.resource_type
                                                    == "documents" else None)
                    elif kind == "artifacts" and isinstance(backend.artifact, S3ArtifactStore):
                        await backend.artifact.purge(tenant, None if whole_tenant else step["ids"])
                    elif kind == "vectors" and isinstance(backend.knowledge,
                                                          PgVectorKnowledgeStore):
                        await backend.knowledge.purge(tenant, None if whole_tenant else step["ids"])
                    elif kind in {"memory", "summary"}:
                        store = getattr(backend, kind)
                        if not isinstance(store, _PostgreSQLAdapter):
                            raise RuntimeError("cleanup backend changed")
                        await store.purge(tenant, agent)
                    else:
                        raise RuntimeError("cleanup backend is unavailable")
                remaining.pop(0)
                record.cleanup = list(remaining)
                record.error_code = None
                await session.commit()
        record.status = "completed"
        record.error_code = None
    except Exception as error:
        await session.rollback()
        # Never persist provider messages, URLs or credentials.
        record.cleanup = remaining
        record.status = "failed"
        record.error_code = type(error).__name__
    await session.commit()
    await session.refresh(record)


def receipt(record: DeletionRecord) -> dict[str, Any]:
    return {
        "deletion_id": str(record.deletion_id),
        "resource_type": record.resource_type,
        "resource_id": record.resource_id,
        "name": record.resource_name,
        "status": record.status,
        "remaining_steps": len(record.cleanup),
        "error_code": record.error_code,
        "created_at": record.created_at.isoformat()
    }
