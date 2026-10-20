"""Original durable queue schema, expressed using Alembic operations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table(
        "route_queries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("request_json", JSONB(), nullable=False),
        sa.Column("request_fingerprint", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), unique=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "attempt", sa.Integer(), nullable=False, server_default=sa.literal(0)
        ),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("result_json", JSONB()),
        sa.Column("failure_code", sa.Text()),
        sa.Column("failure_message", sa.Text()),
        sa.Column(
            "failure_retryable", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            sa.column("status").in_(("queued", "running", "completed", "failed")),
            name="route_queries_status_check",
        ),
        sa.CheckConstraint(
            sa.column("attempt") >= 0, name="route_queries_attempt_check"
        ),
        sa.CheckConstraint(
            sa.column("max_attempts") > 0, name="route_queries_max_attempts_check"
        ),
        sa.CheckConstraint(
            (sa.column("status") == "running")
            == sa.and_(
                sa.column("lease_token").is_not(None),
                sa.column("lease_expires_at").is_not(None),
            ),
            name="route_queries_check",
        ),
    )
    op.create_index(
        "route_queries_active_fingerprint_key",
        "route_queries",
        ["request_fingerprint"],
        unique=True,
        postgresql_where=sa.column("status").in_(("queued", "running")),
    )
    op.create_index(
        "route_queries_claim_index",
        "route_queries",
        ["available_at", "created_at"],
        postgresql_where=sa.column("status") == "queued",
    )
    op.create_index(
        "route_queries_expired_lease_index",
        "route_queries",
        ["lease_expires_at"],
        postgresql_where=sa.column("status") == "running",
    )


def downgrade() -> None:
    op.drop_table("route_queries")
