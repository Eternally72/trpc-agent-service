import httpx
import pytest

from trpc_service.config import Settings
from trpc_service.web import create_app


@pytest.mark.anyio
async def test_health_reports_service_identity() -> None:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        auto_create_schema=True,
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    await app.state.engine.dispose()

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "trpc-agent-service",
        "version": "0.1.0",
    }


@pytest.mark.anyio
async def test_readiness_checks_database_connection() -> None:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        auto_create_schema=True,
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/ready")

    await app.state.engine.dispose()

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {
            "database": "ok",
            "worker_nodes": 1,
        },
    }
