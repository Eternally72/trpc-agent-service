"""Durable, secret-free cleanup receipts for physically deleted resources."""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, String, Index
from sqlalchemy.orm import Mapped, mapped_column

from trpc_service.storage.orm import Base, TimestampMixin


class DeletionRecord(TimestampMixin, Base):
    """Keep retry instructions after the source entity has been removed."""

    __tablename__ = "management_deletion"
    __table_args__ = (Index("ix_management_deletion_tenant_status", "tenant_id", "status"), )

    deletion_id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID | None] = mapped_column()
    resource_type: Mapped[str] = mapped_column(String(40))
    resource_id: Mapped[str] = mapped_column(String(255))
    resource_name: Mapped[str] = mapped_column(String(255))
    actor_subject: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    cleanup: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error_code: Mapped[str | None] = mapped_column(String(100))
