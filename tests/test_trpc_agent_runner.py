from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import SecretStr
from trpc_agent_sdk.events import Event
from trpc_agent_sdk.runners import RunConfig
from trpc_agent_sdk.types import Content, GenerateContentResponseUsageMetadata, Part

from trpc_service.agent import (
    AgentExecutionClaim,
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentInputArtifact,
    AgentRuntimeConfig,
    AgentRunResult,
    AgentToolCall,
    AgentToolInvoker,
    AgentToolResult,
    PolicyAction,
    PolicyDecision,
)
from trpc_service.agent.adapters.trpc import TRPCAgentRunner
from trpc_service.agent.factory import (
    ConfiguredTRPCAgentRunner,
    _resolve_secret,
    build_trpc_agent_runner,
)
from trpc_service.channels import ChannelBindingConfig, IncomingMessage, MessageKind
from trpc_service.config import Settings
from trpc_service.storage import (
    KnowledgeDocument,
    KnowledgeHit,
    SessionEvent,
    SessionSnapshot,
)
from trpc_service.tenant import TenantContext


class RecordingSDKRunner:

    def __init__(self) -> None:
        self.user_id = ""
        self.session_id = ""
        self.message = ""
        self.shared_context = ""
        self.parts: list[Part] = []

    async def run_async(
        self,
        *,
        user_id: str,
        session_id: str,
        new_message: Content | list[Content],
        run_config: RunConfig,
    ) -> AsyncIterator[Event]:
        del run_config
        self.user_id = user_id
        self.session_id = session_id
        current = new_message[-1] if isinstance(new_message, list) else new_message
        self.parts = list(current.parts)
        self.message = current.parts[0].text or ""
        if isinstance(new_message, list):
            self.shared_context = new_message[0].parts[0].text or ""
        yield Event(
            author="assistant",
            content=Content(role="model", parts=[Part.from_text(text="Agent 回复")]),
            partial=False,
            usageMetadata=GenerateContentResponseUsageMetadata(
                promptTokenCount=12,
                candidatesTokenCount=4,
                totalTokenCount=16,
            ),
        )


class ErrorSDKRunner:

    async def run_async(
        self,
        *,
        user_id: str,
        session_id: str,
        new_message: Content | list[Content],
        run_config: RunConfig,
    ) -> AsyncIterator[Event]:
        del user_id, session_id, new_message, run_config
        yield Event(author="assistant", errorCode="MODEL_ERROR")


class EmptySDKRunner:

    async def run_async(
        self,
        *,
        user_id: str,
        session_id: str,
        new_message: Content | list[Content],
        run_config: RunConfig,
    ) -> AsyncIterator[Event]:
        del user_id, session_id, new_message, run_config
        if False:
            yield Event(author="assistant")


class UnusedToolInvoker(AgentToolInvoker):

    async def invoke(
        self,
        context: AgentExecutionContext,
        call: AgentToolCall,
    ) -> AgentToolResult:
        raise AssertionError("the text-only Agent must not invoke tools")


def _context() -> AgentExecutionContext:
    tenant_id = uuid4()
    agent_app_id = uuid4()
    tenant = TenantContext(
        tenant_id=tenant_id,
        agent_app_id=agent_app_id,
        config_version=1,
        request_id="request-1",
        trace_id="trace-1",
    )
    return AgentExecutionContext(
        request=AgentExecutionRequest(
            tenant=tenant,
            session_id="session-1",
            incoming=IncomingMessage(
                external_message_id="message-1",
                principal_id="browser-user-1",
                conversation_id="conversation-1",
                kind=MessageKind.TEXT,
                occurred_at=datetime.now(timezone.utc),
                text="用户消息",
            ),
            channel=ChannelBindingConfig(
                binding_id=uuid4(),
                tenant_id=tenant_id,
                agent_app_id=agent_app_id,
                channel_type="wecom",
            ),
        ),
        config=AgentRuntimeConfig(config_version=1, runner_name="trpc_agent"),
        policy=PolicyDecision(action=PolicyAction.ALLOW),
        claim=AgentExecutionClaim(claim_id="claim-1"),
    )


@pytest.mark.anyio
async def test_trpc_agent_runner_maps_context_to_final_text_reply() -> None:
    sdk_runner = RecordingSDKRunner()
    runner = TRPCAgentRunner(sdk_runner)
    context = _context()

    result = await runner.run(context, UnusedToolInvoker())

    assert sdk_runner.user_id.endswith(":browser-user-1")
    assert sdk_runner.session_id.endswith(":session-1")
    assert sdk_runner.message == "用户消息"
    assert len(result.replies) == 1
    assert result.replies[0].kind is MessageKind.TEXT
    assert result.replies[0].text == "Agent 回复"
    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 4
    assert result.usage.total_tokens == 16
    await runner.close()


@pytest.mark.anyio
async def test_trpc_agent_runner_does_not_expose_im_files_as_knowledge_mutations() -> None:
    """An IM file caption remains ordinary model input without RAG write hints."""

    sdk_runner = RecordingSDKRunner()
    runner = TRPCAgentRunner(sdk_runner)
    context = _context()
    context = replace(
        context,
        config=replace(
            context.config,
            knowledge={"knowledge_base_names": ["handbook"]},
        ),
        request=replace(
            context.request,
            incoming=replace(
                context.request.incoming,
                kind=MessageKind.FILE,
                text="把这个文件加入 handbook 知识库",
                artifact_refs=("tenant-artifact-1", ),
            ),
        ),
    )

    result = await runner.run(context, UnusedToolInvoker())

    assert sdk_runner.message == ""
    assert "租户管理员" in (result.replies[0].text or "")
    assert result.state == {}
    await runner.close()


@pytest.mark.anyio
async def test_trpc_agent_runner_rehydrates_shared_session_history() -> None:
    """A stateless Worker makes durable prior turns visible to the model."""

    sdk_runner = RecordingSDKRunner()
    context = _context()
    context = replace(
        context,
        session=SessionSnapshot(
            session_id=context.request.session_id,
            version=2,
            events=(
                SessionEvent(
                    event_id="event-1",
                    event_type="message.received",
                    occurred_at=datetime.now(timezone.utc),
                    payload={"text": "我叫小白"},
                ),
                SessionEvent(
                    event_id="event-2",
                    event_type="agent.replied",
                    occurred_at=datetime.now(timezone.utc),
                    payload={"text": "你好，小白"},
                ),
            ),
        ),
    )

    await TRPCAgentRunner(sdk_runner).run(context, UnusedToolInvoker())

    assert "用户: 我叫小白" in sdk_runner.shared_context
    assert "助手: 你好，小白" in sdk_runner.shared_context


@pytest.mark.anyio
async def test_trpc_agent_runner_injects_citable_tenant_knowledge() -> None:
    sdk_runner = RecordingSDKRunner()
    base_context = _context()
    context = replace(
        base_context,
        config=replace(
            base_context.config,
            knowledge={"knowledge_base_names": ["handbook"]},
        ),
        knowledge=(KnowledgeHit(
            document=KnowledgeDocument(
                document_id="document-1:0",
                knowledge_base_id="base-1",
                content="员工每年享有十二天年假。",
                attributes={
                    "filename": "leave-policy.md",
                    "version": 2,
                    "chunk_index": 0,
                },
            ),
            score=0.91,
        ), ),
    )

    await TRPCAgentRunner(sdk_runner).run(context, UnusedToolInvoker())

    assert "[知识1] 员工每年享有十二天年假。" in sdk_runner.shared_context
    assert "来源：leave-policy.md，第 2 版" in sdk_runner.shared_context
    assert "回答中使用知识时请标注 [知识序号]" in sdk_runner.shared_context
    assert "knowledge_list 或 knowledge_search" in sdk_runner.message


@pytest.mark.anyio
async def test_trpc_agent_runner_does_not_expose_text_attachment_identifiers_to_the_model() -> None:
    sdk_runner = RecordingSDKRunner()
    context = _context()
    incoming = replace(context.request.incoming, artifact_refs=("secret-artifact-id", ))

    await TRPCAgentRunner(sdk_runner).run(
        replace(context, request=replace(context.request, incoming=incoming)),
        UnusedToolInvoker(),
    )

    assert sdk_runner.message == "用户消息"
    assert "secret-artifact-id" not in sdk_runner.message


@pytest.mark.anyio
async def test_trpc_runner_directs_bare_im_files_to_tenant_management() -> None:
    """A bare IM file cannot create hidden RAG mutation state."""

    sdk_runner = RecordingSDKRunner()
    context = _context()
    incoming = replace(
        context.request.incoming,
        kind=MessageKind.FILE,
        text=None,
        artifact_refs=("tenant-artifact-1", ),
    )

    result = await TRPCAgentRunner(sdk_runner).run(
        replace(context, request=replace(context.request, incoming=incoming)),
        UnusedToolInvoker(),
    )

    assert sdk_runner.message == ""
    assert "租户管理员" in (result.replies[0].text or "")
    assert "管理台" in (result.replies[0].text or "")
    assert result.state == {}


@pytest.mark.anyio
async def test_trpc_agent_runner_sends_tenant_image_bytes_to_multimodal_model() -> None:
    sdk_runner = RecordingSDKRunner()
    context = _context()
    incoming = replace(
        context.request.incoming,
        kind=MessageKind.IMAGE,
        text=None,
        artifact_refs=("image-artifact-id", ),
    )
    context = replace(
        context,
        request=replace(context.request, incoming=incoming),
        input_artifacts=(AgentInputArtifact(
            artifact_id="image-artifact-id",
            media_type="image/png",
            filename="photo.png",
            content=b"\x89PNG\r\n\x1a\nimage",
        ), ),
    )

    await TRPCAgentRunner(sdk_runner).run(context, UnusedToolInvoker())

    assert sdk_runner.parts[0].text == "请描述这张图片。"
    assert sdk_runner.parts[1].inline_data is not None
    assert sdk_runner.parts[1].inline_data.mime_type == "image/png"
    assert sdk_runner.parts[1].inline_data.data == b"\x89PNG\r\n\x1a\nimage"


@pytest.mark.anyio
async def test_trpc_agent_runner_fails_closed_on_invalid_or_empty_model_output() -> None:
    context = _context()
    invalid_request = replace(
        context.request,
        incoming=replace(context.request.incoming, kind=MessageKind.IMAGE, text=None),
    )

    with pytest.raises(ValueError, match="not resolved from tenant storage"):
        await TRPCAgentRunner(RecordingSDKRunner()).run(replace(context, request=invalid_request),
                                                        UnusedToolInvoker())
    with pytest.raises(RuntimeError, match="MODEL_ERROR"):
        await TRPCAgentRunner(ErrorSDKRunner()).run(context, UnusedToolInvoker())
    with pytest.raises(RuntimeError, match="no final text"):
        await TRPCAgentRunner(EmptySDKRunner()).run(context, UnusedToolInvoker())


@pytest.mark.anyio
async def test_agent_factory_builds_bailian_runner_from_runtime_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover both supported SecretRef resolvers and SDK composition."""

    secret_file = tmp_path / "model-key"
    secret_file.write_text("file-secret", encoding="utf-8")
    monkeypatch.setenv("TENANT_MODEL_KEY", "environment-secret")
    settings = Settings(
        _env_file=None,
        dashscope_api_key=SecretStr("dashscope-secret"),
    )
    runtime = AgentRuntimeConfig(
        config_version=1,
        runner_name="trpc_agent",
        application={"instruction": "Runtime instruction"},
        model={
            "model_name": "qwen-max",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key_ref": "env://TENANT_MODEL_KEY",
            "temperature": 0.4,
            "max_output_tokens": 128,
        },
    )

    runner = build_trpc_agent_runner(settings, runtime)

    assert isinstance(runner, TRPCAgentRunner)
    assert _resolve_secret(settings, "env://DASHSCOPE_API_KEY") == "dashscope-secret"
    assert _resolve_secret(settings, f"file://{secret_file}") == "file-secret"
    with pytest.raises(RuntimeError, match="resolver is not configured"):
        _resolve_secret(settings, "vault://models/key")
    with pytest.raises(RuntimeError, match="credential is empty"):
        _resolve_secret(settings, "env://MISSING_MODEL_KEY")
    await runner.close()


def test_agent_factory_rejects_non_numeric_generation_parameters() -> None:
    settings = Settings(_env_file=None, dashscope_api_key=SecretStr("dashscope-secret"))
    invalid_temperature = AgentRuntimeConfig(
        config_version=1,
        runner_name="trpc_agent",
        model={
            "temperature": {},
            "api_key_ref": "env://DASHSCOPE_API_KEY"
        },
    )
    invalid_tokens = AgentRuntimeConfig(
        config_version=1,
        runner_name="trpc_agent",
        model={
            "max_output_tokens": [],
            "api_key_ref": "env://DASHSCOPE_API_KEY"
        },
    )

    with pytest.raises(ValueError, match="temperature must be numeric"):
        build_trpc_agent_runner(settings, invalid_temperature)
    with pytest.raises(ValueError, match="max_output_tokens must be numeric"):
        build_trpc_agent_runner(settings, invalid_tokens)


@pytest.mark.anyio
async def test_configured_runner_closes_request_scoped_sdk_runner(
    monkeypatch: pytest.MonkeyPatch, ) -> None:
    closed = False
    expected = AgentRunResult()

    class StubRequestRunner:

        async def run(
            self,
            context: AgentExecutionContext,
            tools: AgentToolInvoker,
        ) -> AgentRunResult:
            del context, tools
            return expected

        async def close(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(
        "trpc_service.agent.factory.build_trpc_agent_runner",
        lambda settings, runtime_config, **kwargs: StubRequestRunner(),
    )
    runner = ConfiguredTRPCAgentRunner(Settings(_env_file=None))

    result = await runner.run(_context(), UnusedToolInvoker())

    assert result is expected
    assert closed is True
