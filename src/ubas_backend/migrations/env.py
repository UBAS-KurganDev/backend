"""Migrations run on the connection supplied by ubas-migrate."""

from alembic import context
from sqlalchemy.engine import Connection

from ubas_backend.query_manager.models import QueryBase

connection = context.config.attributes.get("connection")
if not isinstance(connection, Connection):
    raise RuntimeError("Run migrations with ubas-migrate")

context.configure(connection=connection, target_metadata=QueryBase.metadata)
with context.begin_transaction():
    context.run_migrations()
