"""tRPC-Agent-Python implementation of the platform AgentRunner port."""

from collections.abc import AsyncIterator
from collections.abc import Awaitable, Sequence
from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4

from trpc_agent_sdk.events import Event
from trpc_agent_sdk.runners import RunConfig
from trpc_agent_sdk.types import Content, Part

from trpc_service.agent.contracts import (
    AgentExecutionContext,
    AgentReply,
    AgentRunResult,
    AgentUsage,
)
from trpc_service.agent.ports import AgentRunner, AgentToolInvoker
from trpc_service.channels.contracts import MessageKind
from trpc_service.storage.types import SessionEvent


class TRPCRunner(Protocol):
    """Subset of the SDK Runner used by the platform adapter."""

    def run_async(
        self,
        *,
        user_id: str,
        session_id: str,
        new_message: Content | list[Content],
        run_config: RunConfig,
    ) -> AsyncIterator[Event]:
        """Yield SDK events for one user turn."""

        ...


class TRPCAgentRunner(AgentRunner):
    """Translate platform execution values to and from the tRPC-Agent SDK."""

    def __init__(self, runner: TRPCRunner) -> None:
        self._runner = runner

    @staticmethod
    def _shared_context(context: AgentExecutionContext) -> Content | None:
        """Convert durable Session and Memory facts into model-visible context."""

        lines: list[str] = []
        if context.session is not None:
            # Bound replay size independently of the concrete Session backend.
            for event in context.session.events[-40:]:
                text = event.payload.get("text")
                if not isinstance(text, str) or text.strip() == "":
                    continue
                role = "用户" if event.event_type == "message.received" else "助手"
                lines.append(f"{role}: {text}")
        if context.memories:
            lines.append("与当前用户相关的长期记忆：")
            lines.extend(f"- {hit.record.content}" for hit in context.memories[:10])
        if context.knowledge:
            lines.append("当前租户知识库检索结果：")
            for index, hit in enumerate(context.knowledge[:10], start=1):
                filename = hit.document.attributes.get("filename", "未知文件")
                version = hit.document.attributes.get("version", "未知")
                lines.append(f"[知识{index}] {hit.document.content}")
                lines.append(f"来源：{filename}，第 {version} 版")
            lines.append("回答中使用知识时请标注 [知识序号]；知识不足时应明确说明。")
        if not lines:
            return None
        # This is a prior history item, not part of the new user message. The
        # request-scoped SDK Session may be in-memory because shared storage is
        # rehydrated on every Worker invocation.
        return Content(
            role="user",
            parts=[Part.from_text(text="以下是平台共享存储中的既有上下文：\n" + "\n".join(lines))],
        )

    async def close(self) -> None:
        """Release SDK-owned sessions and background resources when available."""

        close = getattr(self._runner, "close", None)
        if close is not None:
            result: Awaitable[object] = close()
            await result

    async def run(
        self,
        context: AgentExecutionContext,
        tools: AgentToolInvoker,
    ) -> AgentRunResult:
        """Execute one text, image, or file turn through the SDK."""

        # The request-scoped factory already wrapped this invoker in SDK
        # callables. Alternate runners still receive the same governed port.
        incoming = context.request.incoming
        if incoming.kind not in {MessageKind.TEXT, MessageKind.IMAGE, MessageKind.FILE}:
            raise ValueError("the tRPC Agent runner supports text, image, and file messages only")
        if incoming.kind is MessageKind.TEXT and incoming.text is None:
            raise ValueError("text message content cannot be empty")
        if incoming.kind is MessageKind.IMAGE and not context.input_artifacts:
            raise ValueError("image message content was not resolved from tenant storage")
        if incoming.kind is MessageKind.FILE and not incoming.artifact_refs:
            raise ValueError("file message does not contain a persisted Artifact")

        if incoming.kind is MessageKind.FILE:
            # Ordinary IM users may send files, but only a tenant administrator
            # can parse and mutate knowledge sources through the management
            # console. Never send a caption-only file turn to the model because
            # the model has not received the file bytes.
            reply_text = "文件已接收，但当前不会解析 IM 文件内容。知识库文件请由租户管理员在管理台上传和维护。"
            occurred_at = datetime.now(timezone.utc)
            return AgentRunResult(
                replies=(AgentReply(kind=MessageKind.TEXT, text=reply_text), ),
                events=(
                    SessionEvent(
                        event_id=str(uuid4()),
                        event_type="message.received",
                        occurred_at=incoming.occurred_at,
                        payload={
                            "kind": incoming.kind.value,
                            "text": incoming.text,
                            "artifact_refs": list(incoming.artifact_refs),
                        },
                    ),
                    SessionEvent(
                        event_id=str(uuid4()),
                        event_type="agent.replied",
                        occurred_at=occurred_at,
                        payload={
                            "kind": MessageKind.TEXT.value,
                            "text": reply_text
                        },
                    ),
                ),
            )

        tenant = context.request.tenant
        scoped_user_id = f"{tenant.tenant_id}:{incoming.principal_id}"
        scoped_session_id = f"{tenant.agent_app_id}:{context.request.session_id}"
        if incoming.kind is MessageKind.IMAGE:
            current_text = incoming.text or "请描述这张图片。"
        else:
            current_text = incoming.text or ""
        raw_base_names = context.config.knowledge.get("knowledge_base_names", ())
        base_names = (tuple(
            name.strip() for name in raw_base_names
            if isinstance(name, str) and name.strip()) if isinstance(raw_base_names, Sequence)
                      and not isinstance(raw_base_names, (str, bytes)) else ())
        if base_names:
            # Resource names come from the immutable Agent configuration. Make
            # them explicit so the model never invents a knowledge-base alias
            # that governance will correctly reject.
            current_text += ("\n\n[平台提示：已授权知识库：" + "、".join(base_names) +
                             "；调用 knowledge_list 或 knowledge_search 时，"
                             "knowledge_base_name 必须从上述名称中选择。]")
        parts = [Part.from_text(text=current_text)]
        parts.extend(
            Part.from_bytes(data=artifact.content, mime_type=artifact.media_type)
            for artifact in context.input_artifacts)
        content = Content(role="user", parts=parts)
        shared_context = self._shared_context(context)
        new_message: Content | list[Content] = (content if shared_context is None else
                                                [shared_context, content])
        final_text = ""
        usage = AgentUsage()
        async for event in self._runner.run_async(
                user_id=scoped_user_id,
                session_id=scoped_session_id,
                new_message=new_message,
                run_config=RunConfig(save_history_enabled=True),
        ):
            if event.is_error():
                raise RuntimeError(f"tRPC Agent execution failed: {event.error_code or 'unknown'}")
            if event.is_final_response():
                final_text = event.get_text()
            if event.usage_metadata is not None:
                metadata = event.usage_metadata
                usage = AgentUsage(
                    input_tokens=metadata.prompt_token_count or 0,
                    output_tokens=metadata.candidates_token_count or 0,
                    total_tokens=metadata.total_token_count or 0,
                )

        if final_text.strip() == "":
            raise RuntimeError("tRPC Agent returned no final text response")

        occurred_at = datetime.now(timezone.utc)
        return AgentRunResult(
            replies=(AgentReply(kind=MessageKind.TEXT, text=final_text), ),
            events=(
                SessionEvent(
                    event_id=str(uuid4()),
                    event_type="message.received",
                    occurred_at=incoming.occurred_at,
                    payload={
                        "kind": incoming.kind.value,
                        "text": incoming.text
                    },
                ),
                SessionEvent(
                    event_id=str(uuid4()),
                    event_type="agent.replied",
                    occurred_at=occurred_at,
                    payload={
                        "kind": MessageKind.TEXT.value,
                        "text": final_text
                    },
                ),
            ),
            usage=usage,
        )
