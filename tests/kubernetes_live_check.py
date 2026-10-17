"""Run inside a Gateway Pod alongside live_model_check.py, using mounted credentials.

Uses real model quota and a synthetic binding that never sends external IM messages.
Only resources created by this run are removed. Provider failures remain failures;
the operator may record a quota-related skip after inspecting the error code.
"""

import argparse
import asyncio
from pathlib import Path
import sys
from uuid import UUID, uuid4

import httpx

import live_model_check
from trpc_service.config import Settings


async def main() -> None:
    settings = Settings()
    token_file = Path("/run/secrets/platform/admin_bootstrap_token")
    headers = {
        "Authorization": "Bearer " + token_file.read_text().strip(),
        "X-Support-Reason": "Kubernetes real model acceptance"
    }
    tenant_id = None
    catalog_id = credential_id = None
    created_catalog = False
    original_report = live_model_check.report

    def capture_report(**fields: object) -> None:
        nonlocal tenant_id
        if "tenant" in fields:
            tenant_id = str(fields["tenant"])
        original_report(**fields)

    live_model_check.report = capture_report
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000/api/v1",
                                 headers=headers,
                                 trust_env=False,
                                 timeout=180) as client:

        async def api(method: str, path: str, **kwargs):
            response = await client.request(method, path, **kwargs)
            if not response.is_success:
                raise RuntimeError(f"Kubernetes acceptance API HTTP {response.status_code}: {path}")
            return response.json() if response.content else None

        async def purge(kind: str, resource_id: str, name: str) -> None:
            response = await api("DELETE",
                                 f"/admin/resources/{kind}/{resource_id}/purge",
                                 json={"confirmation": name})
            if response["status"] != "completed":
                raise RuntimeError(f"Kubernetes acceptance cleanup incomplete: {kind}")

        try:
            page = await api("GET", "/admin/model-catalog", params={"limit": 100})
            catalog = next((
                item for item in page["items"]
                if item["model_name"] == settings.llm.model_name and item["provider"] == "bailian"),
                           None)
            if catalog is None:
                catalog = await api("POST",
                                    "/admin/model-catalog",
                                    json={
                                        "provider": "bailian",
                                        "model_name": settings.llm.model_name,
                                        "display_name": "Kubernetes acceptance model"
                                    })
                created_catalog = True
            catalog_id = catalog["model_catalog_id"]
            credential_name = "k8s-check-" + uuid4().hex[:10]
            credential = await api("POST",
                                   "/admin/model-credentials",
                                   json={
                                       "provider": "bailian",
                                       "name": credential_name,
                                       "secret_ref": settings.llm.api_key_ref
                                   })
            credential_id = credential["model_credential_id"]
            await live_model_check.run_check(
                argparse.Namespace(database_url=settings.database_url,
                                   database_password_file=settings.database_password_file,
                                   admin_token_file=token_file,
                                   model_catalog_id=UUID(catalog_id),
                                   credential_id=UUID(credential_id),
                                   disable_thinking=True))
        finally:
            primary_error = sys.exception()
            operations = []
            if tenant_id:

                async def purge_tenant():
                    tenant = await api("GET", f"/tenants/{tenant_id}")
                    await api("PATCH", f"/tenants/{tenant_id}", json={"status": "disabled"})
                    await purge("tenants", tenant_id, tenant["name"])

                operations.append(("purge acceptance tenant", purge_tenant))
            if credential_id:

                async def purge_credential():
                    await api("PATCH",
                              f"/admin/model-credentials/{credential_id}",
                              json={"status": "disabled"})
                    await purge("model-credentials", credential_id, credential_name)

                operations.append(("purge acceptance credential", purge_credential))
            if created_catalog:

                async def purge_catalog():
                    await api("PATCH",
                              f"/admin/model-catalog/{catalog_id}",
                              json={"status": "disabled"})
                    await purge("model-catalog", catalog_id, catalog["display_name"])

                operations.append(("purge acceptance model", purge_catalog))
            try:
                await live_model_check.cleanup_steps(operations, primary_error)
            finally:
                live_model_check.report = original_report


if __name__ == "__main__":
    asyncio.run(main())
