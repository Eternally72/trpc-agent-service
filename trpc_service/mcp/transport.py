"""Pin MCP connections to validated IPs while retaining HTTPS host verification."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import httpx
from mcp.client.session_group import StreamableHttpParameters
from mcp.client.streamable_http import streamable_http_client
from trpc_agent_sdk.tools.mcp_tool._mcp_session_manager import MCPSessionManager


class PinnedMCPTransport(httpx.AsyncHTTPTransport):
    """Resolve once at the policy boundary, never again at socket connection time."""

    def __init__(self, endpoint: str, address: str) -> None:
        super().__init__(retries=0, trust_env=False)
        self._endpoint = httpx.URL(endpoint)
        self._address = address

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if (request.url.scheme != "https" or request.url.host != self._endpoint.host
                or request.url.port != self._endpoint.port):
            raise PermissionError("MCP transport cannot change its authorized origin")
        headers = request.headers.copy()
        headers["Host"] = self._endpoint.netloc.decode("ascii")
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=self._address),
            headers=headers,
            content=request.stream,
            extensions={
                **request.extensions, "sni_hostname": self._endpoint.host
            },
        )
        return await super().handle_async_request(pinned)


class PinnedMCPSessionManager(MCPSessionManager):  # type: ignore[misc]
    """Override only the pinned SDK's transport seam; keep its session lifecycle."""

    def __init__(self, params: StreamableHttpParameters, addresses: tuple[str, ...]) -> None:
        super().__init__(connection_params=params)
        if not addresses:
            raise ValueError("MCP requires a validated destination address")
        self._params = params
        self._address = addresses[0]

    @asynccontextmanager
    async def _create_streamable_http_client(
        self,
        merged_headers: dict[str, str] | None = None,
    ) -> AsyncIterator[Any]:
        timeout = self._params.timeout
        seconds = timeout.total_seconds() if isinstance(timeout, timedelta) else float(timeout)
        async with httpx.AsyncClient(
                transport=PinnedMCPTransport(self._params.url, self._address),
                headers=merged_headers,
                timeout=httpx.Timeout(seconds),
                follow_redirects=False,
                trust_env=False,
        ) as client:
            async with streamable_http_client(
                    self._params.url,
                    http_client=client,
                    terminate_on_close=self._params.terminate_on_close,
            ) as streams:
                yield streams
