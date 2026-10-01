"""Allow configuration removal while preserving independent historical facts.

Revision ID: 20260930_0029
Revises: 20260924_0028
"""
from alembic import op
import sqlalchemy as sa

revision = "20260930_0029"
down_revision = "20260924_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_knowledge_source_document_doc_status"),
                       "knowledge_source_document",
                       type_="check")
    op.create_check_constraint(
        "doc_status", "knowledge_source_document",
        "status IN ('INGESTING', 'READY', 'FAILED', 'SUPERSEDED', 'DELETED', 'DISABLED')")
    op.create_table(
        "management_deletion",
        sa.Column("deletion_id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("resource_type", sa.String(40), nullable=False),
        sa.Column("resource_id", sa.String(255), nullable=False),
        sa.Column("resource_name", sa.String(255), nullable=False),
        sa.Column("actor_subject", sa.String(255), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("cleanup", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("created_at",
                  sa.DateTime(timezone=True),
                  server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at",
                  sa.DateTime(timezone=True),
                  server_default=sa.func.now(),
                  nullable=False),
    )
    op.create_index("ix_management_deletion_tenant_status", "management_deletion",
                    ["tenant_id", "status"])
    for table, name in (("usage_ledger", "fk_usage_ledger_tenant_agent"),
                        ("audit_log", "fk_audit_log_tenant_agent")):
        op.drop_constraint(name, table, type_="foreignkey")
    # Upload attribution is historical. A tenant-owned file may outlive its uploader.
    for fk in sa.inspect(op.get_bind()).get_foreign_keys("knowledge_artifact"):
        if fk["referred_table"] == "agent_app":
            op.drop_constraint(fk["name"], "knowledge_artifact", type_="foreignkey")


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE knowledge_source_document SET status = 'DELETED' WHERE status = 'DISABLED'"))
    op.drop_constraint(op.f("ck_knowledge_source_document_doc_status"),
                       "knowledge_source_document",
                       type_="check")
    op.create_check_constraint(
        "doc_status", "knowledge_source_document",
        "status IN ('INGESTING', 'READY', 'FAILED', 'SUPERSEDED', 'DELETED')")
    # Removed entities cannot be restored. NOT VALID preserves historical rows
    # while restoring checks on subsequent inserts/updates.
    for table, name, owner in (
        ("usage_ledger", "fk_usage_ledger_tenant_agent", "agent_app_id"),
        ("audit_log", "fk_audit_log_tenant_agent", "agent_app_id"),
        ("knowledge_artifact", "fk_knowledge_artifact_uploader", "uploaded_by_agent_id"),
    ):
        op.execute(
            sa.text(f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" '
                    f'FOREIGN KEY (tenant_id, {owner}) '
                    'REFERENCES agent_app (tenant_id, agent_app_id) ON DELETE RESTRICT NOT VALID'))
    op.drop_table("management_deletion")
