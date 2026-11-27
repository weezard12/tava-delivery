"""Alembic environment. The service passes its open connection in ``config.attributes``."""

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection

from teva_delivery.persistence.models import Base

config = context.config


def run() -> None:
    connection = config.attributes.get("connection")
    if connection is None:
        # Only for `alembic revision --autogenerate` during development.
        url = config.get_main_option("sqlalchemy.url") or "sqlite://"
        with create_engine(url).connect() as fresh:
            _run(fresh)
        return
    _run(connection)


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


run()
