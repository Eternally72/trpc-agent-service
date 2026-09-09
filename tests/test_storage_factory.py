from collections.abc import Sequence

from pydantic import SecretStr

from trpc_service.config import Settings
from trpc_service.storage import EmbeddingProvider
from trpc_service.storage.factory import build_storage_composition


class FixedEmbedding(EmbeddingProvider):
    """Minimal provider used to compose the vector capability without I/O."""

    @property
    def dimensions(self) -> int:
        return 3

    async def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [[0.0, 0.0, 1.0] for _ in texts]

    async def embed_query(self, text: str) -> Sequence[float]:
        del text
        return [0.0, 0.0, 1.0]


def test_storage_factory_composes_inmemory_profile_from_settings() -> None:
    # The factory fallback is tested independently from the developer's .env.
    settings = Settings(_env_file=None)

    composition = build_storage_composition(settings)
    resolved = composition.router.resolve(settings.storage_profile.to_domain())

    assert composition.registry.names == ("inmemory", )
    assert resolved.session is not None
    assert resolved.memory is not None
    assert resolved.knowledge is not None


def test_storage_factory_reuses_one_pool_for_facts_and_vectors() -> None:
    """A single PostgreSQL instance must not create duplicate local pools."""

    shared_url = "postgresql+asyncpg://trpc@127.0.0.1:55432/trpc_agent"
    settings = Settings(
        _env_file=None,
        storage_backends={
            "facts": {
                "kind": "postgresql",
                "url": shared_url,
            },
            "vectors": {
                "kind": "pgvector",
                "url": shared_url,
                "embedding_provider": "test",
            },
        },
        storage_profile={
            "session": "facts",
            "memory": "facts",
            "summary": "facts",
            "knowledge": "vectors",
            "audit": "facts",
        },
    )

    composition = build_storage_composition(
        settings,
        embedding_providers={"test": FixedEmbedding()},
    )
    resolved = composition.router.resolve(settings.storage_profile.to_domain())

    assert len(composition.engines) == 1
    assert resolved.session is not None
    assert resolved.knowledge is not None


def test_storage_factory_composes_default_bailian_embedding_from_settings() -> None:
    shared_url = "postgresql+asyncpg://trpc@127.0.0.1:55432/trpc_agent"
    settings = Settings(
        _env_file=None,
        dashscope_api_key=SecretStr("test-only-key"),
        storage_backends={
            "facts": {
                "kind": "postgresql",
                "url": shared_url,
            },
            "vectors": {
                "kind": "pgvector",
                "url": shared_url,
                "embedding_provider": "bailian",
            },
        },
        storage_profile={
            "session": "facts",
            "knowledge": "vectors"
        },
    )

    composition = build_storage_composition(settings)

    assert composition.registry.names == ("facts", "vectors")
