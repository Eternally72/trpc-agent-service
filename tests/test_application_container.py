from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from trpc_service.config import Settings
from trpc_service.web import create_app
from trpc_service.web.container import ApplicationContainer, build_application_container
from trpc_service.workspace import LocalWorkspaceProvider, WorkspaceProvider


def test_default_application_container_owns_extension_registries() -> None:
    # Unit tests opt out of the developer's local .env so this assertion keeps
    # covering the code-level, all-in-memory fallback configuration.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    container = build_application_container(
        settings=Settings(_env_file=None),
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
    )

    assert isinstance(container, ApplicationContainer)
    assert container.channels.supported_types == ("feishu", "wecom")
    assert container.storage_backends.names == ("inmemory", )
    assert container.agent_pipeline is not None
    assert container.approvals is not None
    assert isinstance(container.workspace, WorkspaceProvider)
    assert isinstance(container.workspace, LocalWorkspaceProvider)


def test_application_factory_keeps_an_explicit_container() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    container = build_application_container(
        settings=Settings(_env_file=None),
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
    )
    app = create_app(
        Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:"),
        container=container,
    )

    assert app.state.container is container


def test_channel_runtime_composes_wecom_transport_without_agent_slots() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    container = build_application_container(
        settings=Settings(
            _env_file=None,
            runtime_role="channel",
            worker_concurrency=0,
        ),
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
    )

    assert container.agent_workers is None
    assert container.delivery_workers is not None
    assert container.wecom_supervisor is not None
    assert container.feishu_supervisor is not None
    assert container.channels.supported_types == ("feishu", "wecom")
