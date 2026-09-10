"""Configuration-driven construction of the tRPC-Agent SDK runtime."""

import os
from pathlib import Path

from trpc_agent_sdk.agents import LlmAgent
from trpc_agent_sdk.models import OpenAIModel
from trpc_agent_sdk.runners import Runner
from trpc_agent_sdk.sessions import InMemorySessionService, SessionServiceConfig
from trpc_agent_sdk.abc import ToolABC
from trpc_agent_sdk.skills import BaseSkillRepository
from trpc_agent_sdk.tools import FunctionTool
from trpc_agent_sdk.types import GenerateContentConfig

from trpc_service.agent.adapters.trpc import TRPCAgentRunner
from trpc_service.agent.adapters.trpc_tools import CapabilityCallSequence, TRPCToolBridge
from trpc_service.agent.contracts import AgentExecutionContext, AgentRuntimeConfig, AgentRunResult
from trpc_service.agent.ports import AgentRunner, AgentToolInvoker
from trpc_service.config import Settings
from trpc_service.mcp import TenantMCPService
from trpc_service.skill import BuiltinSkillCatalog, KnowledgeOnlySkillToolset


def _resolve_secret(settings: Settings, reference: str) -> str:
    """Resolve supported local SecretRefs without persisting the secret value."""

    scheme, _, target = reference.partition("://")
    if scheme == "env":
        value = (settings.dashscope_api_key.get_secret_value()
                 if target == "DASHSCOPE_API_KEY" else os.environ.get(target, ""))
    elif scheme == "file":
        value = Path(target).read_text(encoding="utf-8").strip()
    else:
        raise RuntimeError(f"SecretRef resolver is not configured for scheme: {scheme}")
    if value.strip() == "":
        raise RuntimeError("resolved model credential is empty")
    return value


def build_trpc_agent_runner(
    settings: Settings,
    runtime_config: AgentRuntimeConfig | None = None,
    *,
    execution_context: AgentExecutionContext | None = None,
    tool_invoker: AgentToolInvoker | None = None,
    capability_sequence: CapabilityCallSequence | None = None,
    mcp_tools: list[ToolABC] | None = None,
    skill_repository: BaseSkillRepository | None = None,
) -> TRPCAgentRunner:
    """Build qwen through Bailian's OpenAI-compatible endpoint."""

    if (execution_context is None) is not (tool_invoker is None):
        raise ValueError("execution context and Tool invoker must be configured together")

    model_config = (settings.llm.model_dump(
        mode="json") if runtime_config is None else dict(runtime_config.model))
    has_images = execution_context is not None and bool(execution_context.input_artifacts)
    model_field = "vision_model_name" if has_images else "model_name"
    model_default = (settings.llm.vision_model_name if has_images else settings.llm.model_name)
    model_name = str(model_config.get(model_field, model_default))
    base_url = str(model_config.get("base_url", settings.llm.base_url))
    api_key_ref = str(model_config.get("api_key_ref", settings.llm.api_key_ref))
    raw_temperature = model_config.get("temperature", settings.llm.temperature)
    raw_max_output_tokens = model_config.get("max_output_tokens", settings.llm.max_output_tokens)
    if not isinstance(raw_temperature, (str, int, float)):
        raise ValueError("model temperature must be numeric")
    if not isinstance(raw_max_output_tokens, (str, int, float)):
        raise ValueError("model max_output_tokens must be numeric")
    temperature = float(raw_temperature)
    max_output_tokens = int(raw_max_output_tokens)
    generation_config = GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )
    # qwen model names are not part of the SDK's OpenAI registry patterns, so
    # construct OpenAIModel directly instead of resolving by model-name string.
    model = OpenAIModel(
        model_name=model_name,
        api_key=_resolve_secret(settings, api_key_ref),
        base_url=base_url,
        generate_content_config=generation_config,
    )
    application = {} if runtime_config is None else runtime_config.application
    instruction = str(application.get("instruction", settings.agent_instruction))
    # The default qwen-vl-max visual model does not support Function Calling.
    # Visual requests are therefore image Q&A only unless a later profile
    # explicitly selects a tool-capable multimodal Runner implementation.
    sdk_tools: list[object] = []
    if execution_context is not None and tool_invoker is not None and not has_images:
        sequence = capability_sequence or CapabilityCallSequence()
        sdk_tools.extend(
            FunctionTool(function)
            for function in TRPCToolBridge(execution_context, tool_invoker, sequence).functions())
        sdk_tools.extend(mcp_tools or ())
        if skill_repository is not None and skill_repository.summaries():
            sdk_tools.append(KnowledgeOnlySkillToolset(skill_repository))
    agent = LlmAgent(
        name="assistant",
        description="Tenant-scoped assistant shared by configured IM channels.",
        instruction=instruction,
        model=model,
        tools=sdk_tools,
        skill_repository=(None if has_images else skill_repository),
        generate_content_config=generation_config,
    )
    sdk_sessions = InMemorySessionService(session_config=SessionServiceConfig(
        max_events=settings.session_cache_max_events,
        num_recent_events=settings.session_cache_max_events,
    ))
    sdk_runner = Runner(
        app_name=settings.service_name,
        agent=agent,
        session_service=sdk_sessions,
        # Platform Session/Memory/Outbox adapters own post-turn persistence.
        enable_post_turn_processing=False,
    )
    return TRPCAgentRunner(
        sdk_runner,
        sdk_sessions,
        app_name=settings.service_name,
    )


class ConfiguredTRPCAgentRunner(AgentRunner):
    """Build a request-scoped SDK Runner from the immutable runtime config."""

    def __init__(
        self,
        settings: Settings,
        *,
        mcp: TenantMCPService | None = None,
        skills: BuiltinSkillCatalog | None = None,
    ) -> None:
        self._settings = settings
        self._mcp = mcp
        self._skills = skills or BuiltinSkillCatalog()

    async def run(
        self,
        context: AgentExecutionContext,
        tools: AgentToolInvoker,
    ) -> AgentRunResult:
        """Resolve the selected SecretRef and execute the configured model."""

        sequence = CapabilityCallSequence()
        mcp_tools = ([] if self._mcp is None else await self._mcp.tools_for(
            context,
            tools,
            sequence,
        ))
        skill_repository = self._skills.repository_for(context.config)
        runner = build_trpc_agent_runner(
            self._settings,
            context.config,
            execution_context=context,
            tool_invoker=tools,
            capability_sequence=sequence,
            mcp_tools=mcp_tools,
            skill_repository=skill_repository,
        )
        try:
            return await runner.run(context, tools)
        finally:
            await runner.close()
