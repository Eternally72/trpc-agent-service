import httpx
import pytest


@pytest.mark.anyio
async def test_not_found_uses_the_public_error_contract(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get("/api/v1/tenants/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": "tenant not found",
        }
    }


@pytest.mark.anyio
async def test_validation_errors_do_not_echo_request_values(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get("/api/v1/tenants/not-a-uuid")

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["message"] == "request validation failed"
    assert "not-a-uuid" not in response.text
