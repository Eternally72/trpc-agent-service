"""Dependency-aware physical deletion with durable external cleanup receipts.

Only control-plane owners can invoke this service. SQL facts are removed in
one transaction; external cleanup is idempotent and can be resumed afterwards.
"""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import Table, and_, delete, func, or_, select, tuple_, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from trpc_service.admin.auth import ManagementActor
from trpc_service.admin.audit import append_management_audit
from trpc_service.admin.deletion_models import DeletionRecord
from trpc_service.storage.orm import Base

RESOURCES = {
    "tenants": ("tenant", "tenant_id", "name"),
    "model-catalog": ("model_catalog_entry", "model_catalog_id", "display_name"),
    "model-credentials": ("model_provider_credential", "model_credential_id", "name"),
    "model-profiles": ("model_profile", "model_profile_id", "name"),
    "principals": ("management_principal", "management_principal_id", "display_name"),
    "agents": ("agent_app", "agent_app_id", "name"),
    "channel-bindings": ("channel_binding", "binding_id", "binding_public_id"),
    "mcp-connections": ("mcp_connection", "connection_id", "name"),
    "documents": ("knowledge_source_document", "document_id", "filename"),
}
HISTORY = {"audit_log", "usage_ledger", "management_audit_log", "management_deletion"}
LABELS = {
    "tenant": "租户",
    "agent_app": "Agent",
    "agent_config_version": "Agent 配置版本",
    "channel_binding": "IM 接入",
    "mcp_connection": "MCP 连接",
    "model_profile": "模型策略",
    "model_catalog_entry": "模型",
    "model_provider_credential": "模型凭据",
    "management_principal": "管理账号",
    "management_role_assignment": "角色授权",
    "management_web_session": "登录会话",
    "management_credential": "管理令牌",
    "management_password_credential": "登录密码",
    "tenant_secret": "加密密钥",
    "knowledge_base": "知识库",
    "knowledge_source_document": "知识文档版本",
    "knowledge_artifact": "源文件记录",
    "knowledge_chunk": "知识分块",
    "agent_session": "会话",
    "session_event": "会话事件",
    "memory_record": "记忆",
    "session_summary": "会话摘要",
    "agent_task": "任务",
    "inbox_message": "入站消息",
    "outbox_message": "回复记录",
    "outbox_attempt": "投递记录",
    "runner_request": "执行请求",
    "runner_attempt": "执行尝试",
    "runner_checkpoint": "执行检查点",
    "tool_call_ledger": "工具调用记录",
    "approval_request": "审批记录",
    "session_execution_fence": "会话执行租约",
    "channel_principal": "IM 用户",
    "channel_identity": "IM 身份",
    "channel_conversation": "IM 会话",
    "conversation_member": "IM 会话成员",
}


def table(name: str) -> Table:
    return Base.metadata.tables[name]


@dataclass
class DeletionPlan:
    kind: str
    resource_id: UUID
    name: str
    tenant_id: UUID | None
    predicates: dict[str, ColumnElement[bool]] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    cleanup: list[dict[str, Any]] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "resource_type":
            self.kind,
            "resource_id":
            str(self.resource_id),
            "name":
            self.name,
            "can_delete":
            not self.blockers,
            "blockers":
            self.blockers,
            "affected": [{
                "label": LABELS.get(key, key),
                "count": count
            } for key, count in self.counts.items() if count],
            "notes": ["删除的历史配置版本不可再回滚。", "外部环境变量、密钥文件及第三方账号不会被删除。", "节点临时工作目录仍按现有保留策略回收。"],
            "retained": ["脱敏管理审计和历史用量", "仍被其他资源使用的共享模型、凭据和文件"],
        }


def _descendants(plan: DeletionPlan) -> None:
    """Follow real composite foreign keys, never a naming-based cascade."""
    for child in Base.metadata.sorted_tables:
        if child.name in HISTORY or child.name in plan.predicates:
            continue
        conditions = []
        for fk in child.foreign_key_constraints:
            parent = fk.referred_table
            if parent.name not in plan.predicates:
                continue
            local = [element.parent for element in fk.elements]
            remote = [element.column for element in fk.elements]
            conditions.append(
                tuple_(*local).in_(select(*remote).where(plan.predicates[parent.name])))
        if conditions:
            plan.predicates[child.name] = or_(*conditions)


async def _references(session: AsyncSession, plan: DeletionPlan) -> None:
    root = str(plan.resource_id)
    if plan.kind in {"model-catalog", "model-credentials"}:
        profiles = table("model_profile")
        key = "model_catalog_id" if plan.kind == "model-catalog" else "model_credential_id"
        profile_rows = (await session.execute(
            select(profiles.c.name,
                   profiles.c.tenant_id).where(profiles.c[key] == plan.resource_id))).all()
        plan.blockers.extend(f"模型策略「{name}」（租户 {tenant}）仍在引用，请先更换或删除策略。"
                             for name, tenant in profile_rows)
    if plan.kind in {"model-profiles", "mcp-connections"}:
        agents = table("agent_app")
        rows = (await session.execute(select(agents).where(agents.c.tenant_id == plan.tenant_id)
                                      )).mappings()
        for row in rows:
            uses = (str(row["model_profile_id"]) == root
                    if plan.kind == "model-profiles" else root in str(row["tool_permissions"]))
            if uses:
                plan.blockers.append(f"Agent「{row['name']}」仍在引用，请先编辑该 Agent 解除引用。")
        agent_rows = {
            str(row["agent_app_id"]): row
            for row in (
                await session.execute(select(agents).where(
                    agents.c.tenant_id == plan.tenant_id))).mappings()
        }
        versions = table("agent_config_version")
        rows = (await session.execute(
            select(versions).where(
                versions.c.tenant_id == plan.tenant_id,
                versions.c.status.in_(["released", "draft"]),
            ))).mappings()
        historical = []
        for row in rows:
            if root not in str(row["snapshot"]):
                continue
            agent = agent_rows[str(row["agent_app_id"])]
            if row["version"] in {agent["stable_config_version"], agent["canary_config_version"]}:
                plan.blockers.append(f"Agent「{agent['name']}」当前配置 v{row['version']} 仍引用此资源。")
            else:
                historical.append(
                    and_(versions.c.agent_app_id == row["agent_app_id"],
                         versions.c.version == row["version"]))
        if historical:
            plan.predicates[versions.name] = and_(versions.c.tenant_id == plan.tenant_id,
                                                  or_(*historical))
    if plan.kind == "principals":
        roles = table("management_role_assignment")
        owned = await session.scalar(
            select(func.count()).select_from(roles).where(
                roles.c.management_principal_id == plan.resource_id,
                roles.c.role == "tenant_admin"))
        if owned:
            plan.blockers.append("该账号是租户唯一管理员，请通过删除所属租户一并清理账号。")
        is_admin = await session.scalar(
            select(func.count()).select_from(roles).where(
                roles.c.management_principal_id == plan.resource_id,
                roles.c.role == "platform_admin"))
        if is_admin:
            principals = table("management_principal")
            others = await session.scalar(
                select(func.count()).select_from(roles.join(principals)).where(
                    roles.c.role == "platform_admin", principals.c.status == "active",
                    principals.c.management_principal_id != plan.resource_id))
            if not others:
                plan.blockers.append("不能删除最后一个平台管理员。")


async def build_plan(session: AsyncSession, kind: str, resource_id: UUID, tenant_id: UUID | None,
                     actor: ManagementActor) -> DeletionPlan:
    if kind not in RESOURCES:
        raise HTTPException(404, "资源类型不存在")
    name, key, label = RESOURCES[kind]
    root = table(name)
    condition = root.c[key] == resource_id
    if tenant_id is not None:
        condition = and_(condition, root.c.tenant_id == tenant_id)
    row = (await session.execute(select(root).where(condition))).mappings().first()
    if row is None:
        raise HTTPException(404, "资源不存在或已经删除")
    scope = resource_id if kind == "tenants" else tenant_id
    plan = DeletionPlan(kind, resource_id, str(row[label]), scope, {name: condition})
    if kind not in {"documents", "principals"} and row["status"] != "disabled":
        plan.blockers.append("请先停用此资源，再执行彻底删除。")
    if kind == "principals" and str(resource_id) == actor.subject:
        plan.blockers.append("不能删除当前登录账号。")
    await _references(session, plan)
    if kind == "tenants":
        # No upward cascade: only delete identities whose complete role set
        # belongs to this tenant. Shared platform identities are preserved.
        roles = table("management_role_assignment")
        ids = list(await session.scalars(
            select(roles.c.management_principal_id).where(roles.c.tenant_id == scope)))
        exclusive = []
        for principal in ids:
            count = await session.scalar(
                select(func.count()).select_from(roles).where(
                    roles.c.management_principal_id == principal,
                    or_(roles.c.tenant_id.is_(None), roles.c.tenant_id != scope)))
            if not count:
                exclusive.append(principal)
        if exclusive:
            plan.predicates["management_principal"] = table(
                "management_principal").c.management_principal_id.in_(exclusive)
    if kind == "documents":
        # The button removes this filename's complete version family.
        plan.predicates[name] = and_(root.c.tenant_id == scope,
                                     root.c.knowledge_base_id == row["knowledge_base_id"],
                                     root.c.filename == row["filename"])
    _descendants(plan)
    if kind in {"tenants", "agents"}:
        for name in ("memory_record", "session_summary"):
            condition = table(name).c.tenant_id == scope
            if kind == "agents":
                condition = and_(condition, table(name).c.agent_app_id == resource_id)
            plan.predicates[name] = condition
    # Model dependencies are restricted, never silently cascade into tenants.
    if kind in {"model-catalog", "model-credentials", "model-profiles"}:
        plan.predicates = {
            key: condition
            for key, condition in plan.predicates.items()
            if key in {RESOURCES[kind][0], "agent_config_version"}
        }
    if kind == "documents":
        artifacts = table("knowledge_artifact")
        docs = table("knowledge_source_document")
        ids = list(await
                   session.scalars(select(docs.c.artifact_id).where(plan.predicates[docs.name])))
        # An uploaded object can also be retained by conversations and tools.
        payloads: list[str] = []
        for owner, column in (("agent_task", "request_payload"), ("session_event", "payload"),
                              ("agent_session", "state"), ("outbox_message", "payload"),
                              ("approval_request", "artifact_refs"), ("approval_request",
                                                                      "operation_arguments")):
            current = table(owner)
            payloads.extend(
                str(value) for value in await session.scalars(
                    select(current.c[column]).where(current.c.tenant_id == scope)))
        disposable = []
        for artifact_id in set(ids):
            others = await session.scalar(
                select(func.count()).select_from(docs).where(docs.c.tenant_id == scope,
                                                             docs.c.artifact_id == artifact_id,
                                                             ~plan.predicates[docs.name]))
            if not others and not any(artifact_id in payload for payload in payloads):
                disposable.append(artifact_id)
        plan.predicates[artifacts.name] = and_(artifacts.c.tenant_id == scope,
                                               artifacts.c.artifact_id.in_(disposable))
    await _orphan_secrets(session, plan)
    await _check_inflight(session, plan)
    for name, condition in plan.predicates.items():
        plan.counts[name] = int(
            await session.scalar(select(func.count()).select_from(table(name)).where(condition))
            or 0)
    return plan


async def _check_inflight(session: AsyncSession, plan: DeletionPlan) -> None:
    statuses = {
        "agent_task": {"QUEUED", "RUNNING", "RETRYABLE_FAILED"},
        "inbox_message": {"RUNNING", "PROCESSING"},
        "runner_request": {"RUNNING", "PROCESSING"},
        "outbox_message": {"PENDING", "PROCESSING", "RETRYABLE_FAILED", "UNKNOWN"},
        "tool_call_ledger": {"PREPARED", "UNKNOWN"},
        "approval_request": {"PENDING", "APPROVED", "EXECUTING", "UNKNOWN"},
        "knowledge_source_document": {"INGESTING"},
    }
    for name, pending in statuses.items():
        condition = plan.predicates.get(name)
        if plan.kind in {"documents", "mcp-connections", "model-profiles"} and name == "agent_task":
            condition = table(name).c.tenant_id == plan.tenant_id
        if condition is None:
            continue
        count = await session.scalar(
            select(func.count()).select_from(table(name)).where(
                condition,
                func.upper(table(name).c.status).in_(pending)))
        if count:
            plan.blockers.append(f"还有 {count} 条未完成或结果待确认的{LABELS[name]}，请先完成处理。")


async def lock_deletion_tables(session: AsyncSession) -> None:
    """Exclude racing runtime inserts/claims during the short SQL deletion."""
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text("SET LOCAL lock_timeout = '3s'"))
        names = sorted(name for name in Base.metadata.tables
                       if name not in HISTORY and name != "knowledge_chunk_vector")
        # Names are exclusively project-owned metadata, never request input.
        await session.execute(
            text("LOCK TABLE " + ", ".join(f'"{name}"'
                                           for name in names) + " IN SHARE ROW EXCLUSIVE MODE"))


async def delete_sql(session: AsyncSession, plan: DeletionPlan,
                     actor: ManagementActor) -> DeletionRecord:
    if plan.blockers:
        raise HTTPException(409, "；".join(plan.blockers))
    # History is deliberately independent; no retained configuration tombstone.
    for current in reversed(Base.metadata.sorted_tables):
        if current.name in plan.predicates:
            await session.execute(delete(current).where(plan.predicates[current.name]))
    record = DeletionRecord(tenant_id=plan.tenant_id,
                            resource_type=plan.kind,
                            resource_id=str(plan.resource_id),
                            resource_name=plan.name,
                            actor_subject=actor.subject,
                            cleanup=plan.cleanup,
                            status="pending" if plan.cleanup else "completed")
    session.add(record)
    append_management_audit(session,
                            actor,
                            action="resource.purge",
                            resource_type=plan.kind,
                            resource_id=str(plan.resource_id),
                            tenant_id=plan.tenant_id,
                            details_redacted={"counts": plan.counts})
    await session.commit()
    await session.refresh(record)
    return record


async def _orphan_secrets(session: AsyncSession, plan: DeletionPlan) -> None:
    if plan.tenant_id is None or plan.kind == "tenants":
        return
    removed_refs: list[str] = []
    retained_refs: list[str] = []
    for name, column in (("channel_binding", "secret_ref_map"), ("mcp_connection", "secret_ref"),
                         ("model_profile", "secret_ref"), ("agent_app", "application_config"),
                         ("agent_app", "model_config"), ("agent_app", "tool_permissions"),
                         ("agent_app", "backend_config"), ("agent_config_version", "snapshot")):
        current = table(name)
        predicate = plan.predicates.get(name)
        if predicate is not None:
            removed_refs.extend(
                str(value)
                for value in await session.scalars(select(current.c[column]).where(predicate)))
        condition = current.c.tenant_id == plan.tenant_id
        if predicate is not None:
            condition = and_(condition, ~predicate)
        retained_refs.extend(
            str(value)
            for value in await session.scalars(select(current.c[column]).where(condition)))
    secrets = table("tenant_secret")
    ids = await session.scalars(
        select(secrets.c.tenant_secret_id).where(secrets.c.tenant_id == plan.tenant_id))
    disposable = [
        key for key in ids
        if any(str(key) in ref
               for ref in removed_refs) and not any(str(key) in ref for ref in retained_refs)
    ]
    if disposable:
        plan.predicates[secrets.name] = and_(secrets.c.tenant_id == plan.tenant_id,
                                             secrets.c.tenant_secret_id.in_(disposable))
