import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./minhoca.db").strip()

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

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
    inspector = inspect(connection)
    if not inspector.has_table(table_name):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def _add_column_if_missing(
    connection,
    table_name: str,
    column_name: str,
    column_definition: str,
):
    if column_name not in _get_table_columns(connection, table_name):
        connection.execute(
            text(
                f"ALTER TABLE {table_name} "
                f"ADD COLUMN {column_name} {column_definition}"
            )
        )


def init_db():
    """Cria tabelas e aplica migrações de colunas sem apagar dados existentes."""
    Base.metadata.create_all(bind=engine)

    with engine.begin() as connection:
        # USER USAGE
        _add_column_if_missing(
            connection, "user_usage", "reserved_today", "INTEGER DEFAULT 0"
        )

        # PLAN USAGE
        _add_column_if_missing(
            connection, "plan_usage", "reserved_today", "INTEGER DEFAULT 0"
        )

        # USERS
        _add_column_if_missing(connection, "users", "vip_until", "TIMESTAMP")
        _add_column_if_missing(
            connection, "users", "plan_type", "VARCHAR DEFAULT 'free'"
        )
        _add_column_if_missing(
            connection, "users", "status", "VARCHAR DEFAULT 'active'"
        )
        _add_column_if_missing(connection, "users", "subscription_id", "VARCHAR")
        _add_column_if_missing(connection, "users", "payment_type", "VARCHAR")
        _add_column_if_missing(
            connection, "users", "active_transaction_id", "INTEGER"
        )
        _add_column_if_missing(connection, "users", "started_at", "TIMESTAMP")
        _add_column_if_missing(connection, "users", "expires_at", "TIMESTAMP")
        _add_column_if_missing(connection, "users", "next_billing_at", "TIMESTAMP")
        _add_column_if_missing(connection, "users", "cancelled_at", "TIMESTAMP")
        _add_column_if_missing(connection, "users", "created_at", "TIMESTAMP")

        # TRANSACTIONS
        _add_column_if_missing(connection, "transactions", "email", "VARCHAR")
        _add_column_if_missing(
            connection, "transactions", "external_reference", "VARCHAR"
        )
        _add_column_if_missing(connection, "transactions", "plan_type", "VARCHAR")
        _add_column_if_missing(
            connection, "transactions", "payment_type", "VARCHAR"
        )
        _add_column_if_missing(connection, "transactions", "amount", "FLOAT")
        _add_column_if_missing(
            connection, "transactions", "subscription_id", "VARCHAR"
        )
        _add_column_if_missing(
            connection, "transactions", "approved_at", "TIMESTAMP"
        )
        _add_column_if_missing(
            connection, "transactions", "processed_at", "TIMESTAMP"
        )
        _add_column_if_missing(connection, "transactions", "created_at", "TIMESTAMP")
