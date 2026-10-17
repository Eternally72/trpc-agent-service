"""Opt-in in-Pod RAG lifecycle check with real storage and deterministic embeddings.

Run inside a Gateway Pod with its mounted configuration. No provider calls or IM
messages are made. Both synthetic tenants are purged through the management API.
"""

import asyncio
from collections.abc import Sequence
import json
from pathlib import Path
import sys
from uuid import uuid4

import httpx
from sqlalchemy import func, select

from trpc_service.agent.models import AgentApp
from trpc_service.config import Settings
from trpc_service.storage.database import build_engine, build_session_factory
from trpc_service.storage.embedding import EmbeddingProvider
from trpc_service.storage.errors import StoredObjectNotFound
from trpc_service.storage.factory import build_storage_composition
from trpc_service.storage.knowledge import TenantKnowledgeService
from trpc_service.storage.knowledge_orm import KnowledgeChunkVectorRow, KnowledgeDocumentRow
from trpc_service.tenant.context import TenantContext
from trpc_service.tenant.models import Tenant


class TestEmbedding(EmbeddingProvider):

    @property
    def dimensions(self) -> int:
        return 1024

    async def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [[float("code" in text.lower()), 1.0] + [0.0] * 1022 for text in texts]

    async def embed_query(self, text: str) -> Sequence[float]:
        return (await self.embed_documents([text]))[0]


async def main() -> None:
    settings = Settings()
    engine = build_engine(settings)
    sessions = build_session_factory(engine)
    composition = build_storage_composition(
        settings, embedding_providers={settings.embedding.provider_name: TestEmbedding()})
    service = TenantKnowledgeService(
        sessions,
        storage=composition.router,
        default_backends=settings.storage_profile.model_dump(mode="json"),
        embedding_model="deterministic-kubernetes-acceptance")
    config = {"knowledge_base_names": ["k8s-test"]}
    contexts = []
    artifacts = []
    results = []

    async def upload(context, text):

        async def content():
            yield text.encode()

        artifact = await service.upload(context,
                                        principal_id="k8s-storage-check",
                                        filename="policy.txt",
                                        media_type="text/plain",
                                        content=content())
        artifacts.append((context, artifact.artifact_id))
        return artifact.artifact_id

    try:
        await composition.initialize()
        for suffix in ("a", "b"):
            tenant_id, agent_id = uuid4(), uuid4()
            context = TenantContext(tenant_id=tenant_id,
                                    agent_app_id=agent_id,
                                    config_version=1,
                                    request_id=f"k8s-{suffix}",
                                    trace_id=f"k8s-{suffix}")
            async with sessions.begin() as database:
                database.add(Tenant(tenant_id=tenant_id, name=f"k8s-storage-{tenant_id}"))
                await database.flush()
                database.add(
                    AgentApp(tenant_id=tenant_id,
                             agent_app_id=agent_id,
                             name="storage",
                             knowledge_config=config))
            contexts.append(context)
        a, b = contexts
        first = await upload(a, "Recovery code: alpha-100")
        original = (await service.ingest(a,
                                         config,
                                         knowledge_base_name="k8s-test",
                                         artifact_ids=[first]))[0]
        repeated = (await service.ingest(a,
                                         config,
                                         knowledge_base_name="k8s-test",
                                         artifact_ids=[first]))[0]
        assert repeated.document_id == original.document_id
        other = await upload(b, "Recovery code: beta-200")
        await service.ingest(b, config, knowledge_base_name="k8s-test", artifact_ids=[other])
        for context, expected, forbidden in ((a, "alpha-100", "beta-200"), (b, "beta-200",
                                                                            "alpha-100")):
            hits = await service.search(context, config, "recovery code")
            assert hits and all(
                expected in hit.document.content and forbidden not in hit.document.content
                for hit in hits)
        results.extend(["ingestion-idempotency", "tenant-vector-isolation"])
        replacement = await upload(a, "Recovery code: alpha-300")
        current = await service.update_document(a, config, "k8s-test", original.document_id,
                                                replacement)
        hits = await service.search(a, config, "recovery code")
        assert current.version == 2 and hits
        assert all("alpha-300" in hit.document.content and "alpha-100" not in hit.document.content
                   for hit in hits)
        results.append("document-version-replacement")
        token = Path("/run/secrets/platform/admin_bootstrap_token").read_text().strip()
        headers = {
            "Authorization": "Bearer " + token,
            "X-Support-Reason": "Kubernetes deterministic storage acceptance"
        }
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8000/api/v1",
                                     headers=headers,
                                     timeout=90,
                                     trust_env=False) as client:
            path = (f"/tenants/{a.tenant_id}/knowledge-bases/k8s-test/documents/"
                    f"{current.document_id}")
            for status in ("DISABLED", "READY"):
                response = await client.patch(path,
                                              params={"agent_app_id": str(a.agent_app_id)},
                                              json={"status": status})
                response.raise_for_status()
                assert bool(await service.search(a, config, "recovery code")) == (status == "READY")
        results.append("disable-and-restore")
        await service.delete_document(a, config, "k8s-test", current.document_id)
        assert not await service.search(a, config, "recovery code")
        assert await service.search(b, config, "recovery code")
        results.append("soft-delete-isolation")
    finally:
        primary_error = sys.exception()
        errors = []
        token = Path("/run/secrets/platform/admin_bootstrap_token").read_text().strip()
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8000/api/v1",
                                     trust_env=False,
                                     headers={"Authorization": "Bearer " + token},
                                     timeout=90) as client:
            for context in contexts:
                try:
                    response = await client.patch(f"/tenants/{context.tenant_id}",
                                                  json={"status": "disabled"})
                    response.raise_for_status()
                    response = await client.request(
                        "DELETE",
                        f"/admin/resources/tenants/{context.tenant_id}/purge",
                        json={"confirmation": f"k8s-storage-{context.tenant_id}"})
                    response.raise_for_status()
                    assert response.json()["status"] == "completed"
                    async with sessions() as database:
                        for model in (KnowledgeChunkVectorRow, KnowledgeDocumentRow):
                            count = await database.scalar(
                                select(func.count()).select_from(model).where(
                                    model.tenant_id == context.tenant_id))
                            assert count == 0
                except Exception as error:
                    errors.append(type(error).__name__)
        artifact_store = composition.router.resolve(settings.storage_profile.to_domain()).artifact
        if artifact_store is not None and not errors:
            for context, artifact_id in artifacts:
                try:
                    async for _ in artifact_store.open(context, artifact_id):
                        pass
                except StoredObjectNotFound:
                    continue
                errors.append("purged object still exists")
        await composition.close()
        await engine.dispose()
        if errors:
            message = "storage acceptance cleanup failed: " + ",".join(errors)
            if primary_error is None:
                raise RuntimeError(message)
            primary_error.add_note(message)
    results.append("tenant-purge-and-vector-cleanup")
    print(json.dumps({"checks": results, "status": "passed", "real_embedding": False}))


if __name__ == "__main__":
    asyncio.run(main())
