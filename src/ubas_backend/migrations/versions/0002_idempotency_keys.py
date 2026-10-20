"""Keep every client key, including keys sharing one active search."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    keys = op.create_table(
        "query_idempotency_keys",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("request_fingerprint", sa.Text(), nullable=False),
        sa.Column("query_id", sa.Uuid(), sa.ForeignKey("route_queries.id")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_query_idempotency_keys_query_id", "query_idempotency_keys", ["query_id"]
    )
    queries = sa.table(
        "route_queries",
        sa.column("id"),
        sa.column("idempotency_key"),
        sa.column("request_fingerprint"),
        sa.column("created_at"),
    )
    op.execute(
        keys.insert().from_select(
            ["key", "request_fingerprint", "query_id", "created_at"],
            sa.select(
                queries.c.idempotency_key,
                queries.c.request_fingerprint,
                queries.c.id,
                queries.c.created_at,
            ).where(queries.c.idempotency_key.is_not(None)),
        )
    )
    op.drop_column("route_queries", "idempotency_key")


def downgrade() -> None:
    raise RuntimeError(
        "Downgrading would discard additional idempotency keys. "
        "Restore a backup instead."
    )
