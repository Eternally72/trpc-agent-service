"""tRPC-Agent-Python MCP Toolset adapter with tenant and network boundaries."""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import re
import socket
from typing import Any, Protocol, cast
from urllib.parse import urlsplit
from uuid import UUID

from mcp.client.session_group import StreamableHttpParameters
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from trpc_agent_sdk.tools.mcp_tool import MCPToolset
from trpc_agent_sdk.context import InvocationContext
from trpc_agent_sdk.tools import BaseTool
from trpc_agent_sdk.types import FunctionDeclaration, Schema

from trpc_service.admin.secret_store import TenantSecretStore
from trpc_service.agent.contracts import (
    AgentExecutionContext,
    AgentToolCall,
    AgentToolKind,
    AgentToolResult,
)
from trpc_service.agent.ports import AgentToolInvoker
from trpc_service.agent.adapters.trpc_tools import CapabilityCallSequence
from trpc_service.agent.governance import ToolApprovalRequired
from trpc_service.mcp.models import MCPConnection


class RemoteMCPTool(Protocol):
    name: str
    description: str

    def declaration(self) -> dict[str, object]:
        ...

    async def invoke(self, arguments: dict[str, object]) -> object:
        ...


class RemoteMCPToolset(Protocol):

    async def tools(self) -> list[RemoteMCPTool]:
        ...

    async def close(self) -> None:
        ...


ToolsetFactory = Callable[[MCPConnection, Mapping[str, str]], RemoteMCPToolset]
HostResolver = Callable[[str], Sequence[str]]

# Third-party catalogs and results are untrusted input. Keep both the persisted
# catalog and the model-visible response bounded even when the remote server is
# authenticated and explicitly configured by a tenant administrator.
_MAX_MCP_TOOLS = 100
_MAX_MCP_SCHEMA_BYTES = 64 * 1024
_MAX_MCP_CATALOG_BYTES = 1024 * 1024
_MAX_MCP_RESULT_BYTES = 256 * 1024


def _public_addresses(host: str) -> tuple[str, ...]:
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return tuple({str(item[4][0]) for item in addresses})


def _exposed_tool_name(connection_id: UUID, remote_name: str) -> str:
    """Build a collision-free model function name within the common 64-byte limit."""

    suffix = re.sub(r"[^a-zA-Z0-9_]", "_", remote_name).strip("_") or "tool"
    return f"mcp_{connection_id.hex[:12]}_{suffix}"[:64]


class _TRPCMCPTool:
    """Present one upstream MCPTool through the small service protocol."""

    def __init__(self, tool: object) -> None:
        self._tool = tool
        self.name = str(getattr(tool, "name"))
        self.description = str(getattr(tool, "description", ""))

    def declaration(self) -> dict[str, object]:
        declaration = self._tool._get_declaration()  # type: ignore[attr-defined]
        parameters = getattr(declaration, "parameters", None)
        if parameters is None:
            return {"type": "object", "properties": {}}
        return cast(dict[str, object], parameters.model_dump(mode="json", exclude_none=True))

    async def invoke(self, arguments: dict[str, object]) -> object:
        # MCPTool's implementation does not consume InvocationContext; the
        # platform already applied its own trusted execution context and policy.
        return await self._tool._run_async_impl(  # type: ignore[attr-defined]
            args=arguments,
            tool_context=None,
        )


class _TRPCMCPToolset:
    """Adapt the upstream MCPToolset lifecycle without reimplementing MCP."""

    def __init__(self, toolset: MCPToolset) -> None:
        self._toolset = toolset

    async def tools(self) -> list[RemoteMCPTool]:
        return [_TRPCMCPTool(tool) for tool in await self._toolset.get_tools()]

    async def close(self) -> None:
        await self._toolset.close()


def _default_toolset_factory(
    connection: MCPConnection,
    headers: Mapping[str, str],
) -> RemoteMCPToolset:
    timeout = timedelta(seconds=connection.timeout_seconds)
    params = StreamableHttpParameters(
        url=connection.endpoint_url,
        headers=dict(headers) or None,
        timeout=timeout,
        sse_read_timeout=timeout,
    )
    return _TRPCMCPToolset(
        MCPToolset(
            connection_params=params,
            cache_tools=False,
            tools_cache_ttl=None,
        ))


def _risk_level(tool: RemoteMCPTool) -> int:
    """Treat every tenant MCP Tool as an unreviewed external side effect."""

    # MCP annotations are asserted by the remote server itself. They are useful
    # descriptions, but cannot lower platform policy without a future explicit
    # platform review workflow.
    del tool
    return 2


class GovernedMCPTool(BaseTool):  # type: ignore[misc]
    """Expose a discovered schema while routing execution through platform governance."""

    def __init__(
        self,
        *,
        context: AgentExecutionContext,
        invoker: AgentToolInvoker,
        sequence: CapabilityCallSequence,
        connection_id: UUID,
        catalog_entry: Mapping[str, object],
    ) -> None:
        name = catalog_entry.get("name")
        description = catalog_entry.get("description", "")
        if not isinstance(name, str) or not isinstance(description, str):
            raise ValueError("MCP catalog entry identity is invalid")
        super().__init__(name=name, description=description)
        self._context = context
        self._invoker = invoker
        self._sequence = sequence
        self._connection_id = connection_id
        raw_schema = catalog_entry.get("input_schema", {})
        if not isinstance(raw_schema, Mapping):
            raise ValueError("MCP catalog input schema is invalid")
        self._schema = Schema.model_validate(dict(raw_schema))

    def _get_declaration(self) -> FunctionDeclaration:
        return FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters=self._schema,
        )

    async def _run_async_impl(
        self,
        *,
        tool_context: InvocationContext,
        args: dict[str, Any],
    ) -> dict[str, str]:
        del tool_context
        logical_index = self._sequence.next()
        request_id = self._context.request.tenant.request_id
        try:
            result = await self._invoker.invoke(
                self._context,
                AgentToolCall(
                    call_id=f"{request_id}:{logical_index}:{self.name}",
                    name=self.name,
                    kind=AgentToolKind.MCP,
                    logical_call_index=logical_index,
                    resource=str(self._connection_id),
                    arguments=args,
                ),
            )
        except ToolApprovalRequired as error:
            if error.approval is None:
                raise
            return {
                "result": ("该外部操作需要用户确认。请停止继续调用，并请原请求人在当前会话回复："
                           f"确认 {error.approval.short_code}")
            }
        except PermissionError:
            return {"result": "MCP 调用未执行：该连接或工具不在当前 Agent 的授权范围内。"}
        return {"result": result.content or ""}


class TenantMCPService(AgentToolInvoker):
    """Discover and invoke remote tools without crossing tenant configuration."""

    def __init__(
            self,
            sessions: async_sessionmaker[AsyncSession],
            secrets: TenantSecretStore,
            *,
            toolset_factory: ToolsetFactory = _default_toolset_factory,
            resolve_host: HostResolver = _public_addresses,
            private_allowed_hosts: Sequence[str] = (),
    ) -> None:
        self._sessions = sessions
        self._secrets = secrets
        self._toolset_factory = toolset_factory
        self._resolve_host = resolve_host
        self._private_allowed_hosts = {host.casefold() for host in private_allowed_hosts}

    async def _connection(self, tenant_id: UUID, connection_id: UUID) -> MCPConnection:
        async with self._sessions() as database:
            row = await database.scalar(
                select(MCPConnection).where(
                    MCPConnection.tenant_id == tenant_id,
                    MCPConnection.connection_id == connection_id,
                ))
        if row is None:
            raise LookupError("MCP connection does not exist for this tenant")
        return row

    async def _headers(self, connection: MCPConnection) -> dict[str, str]:
        if connection.auth_type == "none":
            return {}
        if connection.auth_type != "bearer" or connection.secret_ref is None:
            raise RuntimeError("MCP connection credential is not configured")
        value = await self._secrets.resolve(
            connection.secret_ref,
            connection.tenant_id,
            scope="mcp",
        )
        return {"Authorization": f"Bearer {value}"}

    async def _validate_network(self, endpoint_url: str, timeout_seconds: int) -> None:
        parsed = urlsplit(endpoint_url)
        if parsed.scheme != "https" or parsed.hostname is None:
            raise PermissionError("MCP endpoint must use HTTPS")
        # DNS is outside the MCP SDK transport timeout. Bound it independently
        # so a broken tenant endpoint cannot leave an IM reply in "thinking".
        addresses = await asyncio.wait_for(
            asyncio.to_thread(self._resolve_host, parsed.hostname),
            timeout=min(timeout_seconds, 10),
        )
        if not addresses:
            raise ConnectionError("MCP endpoint did not resolve")
        if parsed.hostname.casefold() in self._private_allowed_hosts:
            return
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                    or ip.is_reserved or ip.is_unspecified):
                raise PermissionError("MCP endpoint resolved to a non-public address")

    async def refresh(self, tenant_id: UUID, connection_id: UUID) -> list[dict[str, Any]]:
        """Discover a bounded Tool catalog and persist only schemas, never credentials."""

        connection = await self._connection(tenant_id, connection_id)
        if connection.status != "active":
            raise PermissionError("MCP connection is disabled")
        await self._validate_network(connection.endpoint_url, connection.timeout_seconds)
        toolset = self._toolset_factory(connection, await self._headers(connection))
        try:
            tools = await asyncio.wait_for(
                toolset.tools(),
                timeout=connection.timeout_seconds,
            )
            if len(tools) > _MAX_MCP_TOOLS:
                raise ValueError(f"MCP server exposes more than {_MAX_MCP_TOOLS} tools")
            catalog = []
            exposed_names: set[str] = set()
            catalog_bytes = 0
            for tool in tools:
                input_schema = tool.declaration()
                # Validate at discovery time so an invalid third-party schema
                # never breaks an unrelated IM request during Runner assembly.
                Schema.model_validate(input_schema)
                schema_bytes = len(
                    json.dumps(
                        input_schema,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8"))
                if schema_bytes > _MAX_MCP_SCHEMA_BYTES:
                    raise ValueError("MCP Tool input schema exceeds the size limit")
                exposed_name = _exposed_tool_name(connection.connection_id, tool.name)
                if exposed_name in exposed_names:
                    raise ValueError("MCP server exposes colliding Tool names")
                exposed_names.add(exposed_name)
                catalog_entry = {
                    "name": exposed_name,
                    "remote_name": tool.name,
                    "description": tool.description[:1000],
                    "input_schema": input_schema,
                    "risk_level": _risk_level(tool),
                }
                catalog_bytes += len(
                    json.dumps(
                        catalog_entry,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8"))
                if catalog_bytes > _MAX_MCP_CATALOG_BYTES:
                    raise ValueError("MCP Tool catalog exceeds the size limit")
                catalog.append(catalog_entry)
        finally:
            await toolset.close()
        async with self._sessions.begin() as database:
            persisted = await database.scalar(
                select(MCPConnection).where(
                    MCPConnection.tenant_id == tenant_id,
                    MCPConnection.connection_id == connection_id,
                ).with_for_update())
            if persisted is None:
                raise LookupError("MCP connection does not exist for this tenant")
            persisted.tool_catalog = catalog
            persisted.catalog_refreshed_at = datetime.now(timezone.utc)
            persisted.last_error_code = None
        return catalog

    async def invoke(
        self,
        context: AgentExecutionContext,
        call: AgentToolCall,
    ) -> AgentToolResult:
        """Invoke one catalog-pinned remote Tool after common governance delegates here."""

        if call.kind is not AgentToolKind.MCP or call.resource is None:
            raise PermissionError(f"capability is not a registered MCP Tool: {call.name}")
        try:
            connection_id = UUID(call.resource)
        except ValueError as error:
            raise PermissionError("MCP connection resource is invalid") from error
        connection = await self._connection(context.request.tenant.tenant_id, connection_id)
        if connection.status != "active":
            raise PermissionError("MCP connection is disabled")
        entry = next(
            (item for item in connection.tool_catalog if item.get("name") == call.name),
            None,
        )
        if entry is None or not isinstance(entry.get("remote_name"), str):
            raise PermissionError("MCP Tool is not present in the refreshed catalog")
        await self._validate_network(connection.endpoint_url, connection.timeout_seconds)
        toolset = self._toolset_factory(connection, await self._headers(connection))
        try:
            tools = await asyncio.wait_for(
                toolset.tools(),
                timeout=connection.timeout_seconds,
            )
            remote_name = str(entry["remote_name"])
            tool = next((item for item in tools if item.name == remote_name), None)
            if tool is None:
                raise LookupError("MCP Tool is no longer available; refresh the connection")
            result = await asyncio.wait_for(
                tool.invoke(dict(call.arguments)),
                timeout=connection.timeout_seconds,
            )
        finally:
            await toolset.close()
        content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        if len(content.encode("utf-8")) > _MAX_MCP_RESULT_BYTES:
            raise ValueError("MCP Tool result exceeds the size limit")
        return AgentToolResult(call_id=call.call_id, content=content)

    async def tools_for(
        self,
        context: AgentExecutionContext,
        invoker: AgentToolInvoker,
        sequence: CapabilityCallSequence,
    ) -> list[BaseTool]:
        """Build model-visible wrappers from typed grants and the cached Tool catalog."""

        raw_grants = context.config.tools.get("grants", ())
        if not isinstance(raw_grants, Sequence) or isinstance(raw_grants, (str, bytes)):
            return []
        requested: dict[UUID, dict[str, int]] = {}
        for raw_grant in raw_grants:
            if not isinstance(raw_grant, Mapping) or raw_grant.get("kind") != "mcp":
                continue
            actions = raw_grant.get("actions", ())
            resources = raw_grant.get("resources", ())
            name = raw_grant.get("name")
            risk_level = raw_grant.get("risk_level")
            if (not isinstance(name, str) or not isinstance(actions, Sequence)
                    or isinstance(actions, (str, bytes)) or "execute" not in actions
                    or not isinstance(resources, Sequence) or isinstance(resources, (str, bytes))
                    or isinstance(risk_level, bool) or not isinstance(risk_level, int)):
                continue
            for raw_resource in resources:
                try:
                    connection_id = UUID(str(raw_resource))
                except ValueError:
                    continue
                requested.setdefault(connection_id, {})[name] = risk_level
        if not requested:
            return []
        async with self._sessions() as database:
            rows = (await database.scalars(
                select(MCPConnection).where(
                    MCPConnection.tenant_id == context.request.tenant.tenant_id,
                    MCPConnection.connection_id.in_(requested),
                    MCPConnection.status == "active",
                ))).all()
        tools: list[BaseTool] = []
        for row in rows:
            allowed = requested[row.connection_id]
            for entry in row.tool_catalog:
                entry_name = entry.get("name")
                # Risk is a platform policy, not a remote-server assertion. The
                # minimum also protects databases containing pre-policy catalogs.
                effective_risk = 2
                if not isinstance(entry_name, str) or allowed.get(entry_name) != effective_risk:
                    continue
                safe_entry = {**entry, "risk_level": effective_risk}
                tools.append(
                    GovernedMCPTool(
                        context=context,
                        invoker=invoker,
                        sequence=sequence,
                        connection_id=row.connection_id,
                        catalog_entry=safe_entry,
                    ))
        return tools
