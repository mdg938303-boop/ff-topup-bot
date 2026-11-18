import logging
from contextlib import asynccontextmanager
from decimal import Decimal

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import DATABASE_URL
from database.models import Base

logger = logging.getLogger(__name__)

_is_sqlite = DATABASE_URL.startswith("sqlite")

_engine_kwargs: dict = {"echo": False, "pool_pre_ping": True}
if _is_sqlite:
    _engine_kwargs["connect_args"] = {"timeout": 30}
else:
    _engine_kwargs.update(pool_size=5, max_overflow=5, pool_recycle=1800)
    if "-pooler" in DATABASE_URL:
        # Neon's pgbouncer endpoint doesn't support prepared statements.
        _engine_kwargs["connect_args"] = {"statement_cache_size": 0, "prepared_statement_cache_size": 0}

engine = create_async_engine(DATABASE_URL, **_engine_kwargs)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

if _is_sqlite:
    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - local dev only
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def _add_missing_columns(sync_conn) -> None:
    """
    Tiny auto-migration: `create_all` creates missing TABLES but never adds new
    COLUMNS to existing ones. When a new release adds a column, this adds it
    (always nullable, with the Python-side scalar default as DB default) so an
    existing database keeps working after an upgrade — no Alembic needed.
    """
    inspector = inspect(sync_conn)
    dialect = sync_conn.dialect
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            ddl_type = column.type.compile(dialect=dialect)
            default_sql = ""
            default = column.default.arg if (column.default is not None and column.default.is_scalar) else None
            if isinstance(default, bool):
                default_sql = f" DEFAULT {int(default)}" if dialect.name == "sqlite" else f" DEFAULT {'TRUE' if default else 'FALSE'}"
            elif isinstance(default, (int, float, Decimal)):
                default_sql = f" DEFAULT {default}"
            elif isinstance(default, str):
                default_sql = " DEFAULT '" + default.replace("'", "''") + "'"
            logger.warning("Schema upgrade: adding column %s.%s", table.name, column.name)
            sync_conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}{default_sql}'))


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


async def dispose_db() -> None:
    await engine.dispose()


@asynccontextmanager
async def get_session():
    async with SessionLocal() as session:
        yield session
