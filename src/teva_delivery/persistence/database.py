"""The database: PostgreSQL in production, a SQLite file for demos and development.

Writers are serialised per row on PostgreSQL (``SELECT ... FOR UPDATE``) and per database on
SQLite (``BEGIN IMMEDIATE``). Schema changes are Alembic migrations, applied on start under
an advisory lock so two starting containers never migrate at once.
"""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, Engine, create_engine, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

MIGRATIONS = Path(__file__).with_name("migrations")
logger = logging.getLogger("teva_delivery.database")
# Fixed advisory lock key for migrations ("tdlv").
MIGRATION_LOCK_ID = 0x74646C76


def _begin_sqlite(connection: Connection) -> None:
    connection.exec_driver_sql("PRAGMA foreign_keys=ON").close()
    if connection.get_execution_options().get("write"):
        # Take the writer lock before reading, so read-modify-write never interleaves.
        connection.exec_driver_sql("BEGIN IMMEDIATE").close()
    else:
        connection.exec_driver_sql("BEGIN").close()


class Database:
    def __init__(self, target: URL | Path) -> None:
        self.postgres = not isinstance(target, Path)
        self.engine: Engine
        if isinstance(target, Path):
            target.parent.mkdir(parents=True, exist_ok=True)
            self.engine = create_engine(
                URL.create("sqlite+pysqlite", database=str(target)),
                connect_args={"timeout": 15, "isolation_level": None, "check_same_thread": False},
            )
            # WAL lets the screens read while the worker writes. It is a property of the file,
            # set before the listener below starts wrapping every connection in a transaction.
            with self.engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA journal_mode=WAL").close()
                connection.commit()
            event.listen(self.engine, "begin", _begin_sqlite)
        else:
            self.engine = create_engine(
                target,
                pool_pre_ping=True,
                pool_recycle=1800,
                pool_size=10,
                max_overflow=10,
                connect_args={"connect_timeout": 10},
            )
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def migrate(self) -> None:
        """Bring the schema to the latest revision. Safe to run on every start."""
        with self.engine.connect().execution_options(write=True) as connection:
            if self.postgres:
                connection.execute(
                    text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_ID}
                )
            try:
                config = Config()
                config.set_main_option("script_location", str(MIGRATIONS))
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
                connection.commit()
            finally:
                if self.postgres:
                    connection.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_ID}
                    )
                    connection.commit()

    @contextmanager
    def session(self, *, write: bool = False) -> Iterator[Session]:
        """A unit of work: committed when the block ends, rolled back when it raises.

        Callbacks in ``session.info["after_commit"]`` run once the commit succeeded, so a
        screen is never told about a change that was rolled back.
        """
        with (
            self.engine.connect().execution_options(write=write) as connection,
            Session(bind=connection, expire_on_commit=False) as session,
        ):
            with session.begin():
                yield session
            for callback in session.info.pop("after_commit", ()):
                try:
                    callback()
                except Exception:
                    logger.exception("An after-commit callback failed")

    def dispose(self) -> None:
        self.engine.dispose()


def after_commit(session: Session, callback: Callable[[], None]) -> None:
    session.info.setdefault("after_commit", []).append(callback)
