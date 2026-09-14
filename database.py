import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker


DATABASE_URL = (
    os.getenv("DATABASE_URL", "sqlite:///./minhoca.db")
    .strip()
)

# Render/Postgres e alguns provedores ainda podem fornecer a URL
# com o prefixo antigo postgres://.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace(
        "postgres://",
        "postgresql://",
        1,
    )


if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
    )
else:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
    )


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)


Base = declarative_base()


def get_db():
    db = SessionLocal()

    try:
        yield db

    finally:
        db.close()


def _get_table_columns(connection, table_name: str):
    """Retorna as colunas existentes em uma tabela SQLite."""

    result = connection.execute(
        text(f"PRAGMA table_info({table_name})")
    )

    return {row[1] for row in result.fetchall()}


def _add_column_if_missing(
    connection,
    table_name: str,
    column_name: str,
    column_definition: str,
):
    """Adiciona uma coluna SQLite somente quando ela ainda não existe."""

    existing_columns = _get_table_columns(
        connection,
        table_name,
    )

    if column_name not in existing_columns:
        connection.execute(
            text(
                f"ALTER TABLE {table_name} "
                f"ADD COLUMN {column_name} {column_definition}"
            )
        )


def init_db():
    """
    Cria as tabelas e aplica a migração leve usada pelo projeto.

    Em PostgreSQL, o schema deve ser criado pelo SQLAlchemy e não usamos
    PRAGMA/ALTER TABLE específico do SQLite.
    """

    Base.metadata.create_all(bind=engine)

    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as connection:
        # USERS
        _add_column_if_missing(
            connection, "users", "vip_until", "DATETIME"
        )
        _add_column_if_missing(
            connection, "users", "plan_type", "VARCHAR DEFAULT 'free'"
        )
        _add_column_if_missing(
            connection, "users", "status", "VARCHAR DEFAULT 'active'"
        )
        _add_column_if_missing(
            connection, "users", "subscription_id", "VARCHAR"
        )
        _add_column_if_missing(
            connection, "users", "started_at", "DATETIME"
        )
        _add_column_if_missing(
            connection, "users", "expires_at", "DATETIME"
        )
        _add_column_if_missing(
            connection, "users", "next_billing_at", "DATETIME"
        )
        _add_column_if_missing(
            connection, "users", "cancelled_at", "DATETIME"
        )
        _add_column_if_missing(
            connection, "users", "created_at", "DATETIME"
        )

        # TRANSACTIONS
        _add_column_if_missing(
            connection, "transactions", "email", "VARCHAR"
        )
        _add_column_if_missing(
            connection, "transactions", "amount", "FLOAT"
        )
        _add_column_if_missing(
            connection, "transactions", "subscription_id", "VARCHAR"
        )
        _add_column_if_missing(
            connection, "transactions", "approved_at", "DATETIME"
        )
        _add_column_if_missing(
            connection, "transactions", "processed_at", "DATETIME"
        )
        _add_column_if_missing(
            connection, "transactions", "created_at", "DATETIME"
        )
