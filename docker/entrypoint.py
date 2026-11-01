"""Compose startup: build the database URL, optionally migrate, then exec."""

import os
import sys

from sqlalchemy import URL

from ubas_backend.migrate import main as migrate


def main() -> None:
    command = sys.argv[1:]
    run_migrations = command[:1] == ["--migrate"]
    if run_migrations:
        command = command[1:]
    if not command:
        raise SystemExit("Provide a command to run after the container entrypoint.")

    if not os.environ.get("UBAS_DATABASE_URL"):
        os.environ["UBAS_DATABASE_URL"] = URL.create(
            "postgresql+asyncpg",
            username=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            host="postgres",
            port=5432,
            database=os.environ["POSTGRES_DB"],
        ).render_as_string(hide_password=False)

    if run_migrations:
        migrate()
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
