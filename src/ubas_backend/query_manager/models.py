"""Persistence models, separate from API contracts and planner domain objects."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    and_,
    column,
    false,
    func,
    literal,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ubas_backend.query_manager.schemas import QueryStatus

ACTIVE_STATUSES = ("queued", "running")


class QueryBase(DeclarativeBase):
    pass


class RouteQueryRow(QueryBase):
    __tablename__ = "route_queries"
    __table_args__ = (
        CheckConstraint(
            column("status").in_((*ACTIVE_STATUSES, "completed", "failed")),
            name="route_queries_status_check",
        ),
        CheckConstraint(column("attempt") >= 0, name="route_queries_attempt_check"),
        CheckConstraint(
            column("max_attempts") > 0, name="route_queries_max_attempts_check"
        ),
        CheckConstraint(
            (column("status") == "running")
            == and_(
                column("lease_token").is_not(None),
                column("lease_expires_at").is_not(None),
            ),
            name="route_queries_check",
        ),
        Index(
            "route_queries_active_fingerprint_key",
            "request_fingerprint",
            unique=True,
            postgresql_where=column("status").in_(ACTIVE_STATUSES),
        ),
        Index(
            "route_queries_claim_index",
            "available_at",
            "created_at",
            postgresql_where=column("status") == "queued",
        ),
        Index(
            "route_queries_expired_lease_index",
            "lease_expires_at",
            postgresql_where=column("status") == "running",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    request_fingerprint: Mapped[str] = mapped_column(Text)
    status: Mapped[QueryStatus] = mapped_column(Text)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempt: Mapped[int] = mapped_column(default=0, server_default=literal(0))
    max_attempts: Mapped[int]
    lease_token: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    failure_code: Mapped[str | None] = mapped_column(Text)
    failure_message: Mapped[str | None] = mapped_column(Text)
    failure_retryable: Mapped[bool] = mapped_column(
        default=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IdempotencyKeyRow(QueryBase):
    __tablename__ = "query_idempotency_keys"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(Text)
    # A reservation is filled before its transaction commits.
    query_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("route_queries.id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
