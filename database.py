from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base


DATABASE_URL = "sqlite:///./minhoca.db"


engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)


Base = declarative_base()


def get_db():
    db = SessionLocal()

    try:
        yield db

    finally:
        db.close()


def _get_table_columns(connection, table_name: str):
    """
    Retorna o conjunto de colunas existentes em uma tabela SQLite.
    """

    result = connection.execute(
        text(f"PRAGMA table_info({table_name})")
    )

    return {row[1] for row in result.fetchall()}


def _add_column_if_missing(
    connection,
    table_name: str,
    column_name: str,
    column_definition: str
):
    """
    Adiciona uma coluna somente se ela ainda não existir.
    """

    existing_columns = _get_table_columns(
        connection,
        table_name
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
    Cria as tabelas novas e atualiza tabelas existentes
    sem apagar os dados atuais.
    """

    # ---------------------------------------------------------
    # IMPORTANTE:
    # Carrega os modelos antes do create_all().
    #
    # O models.py importa Base deste arquivo, então o import
    # precisa acontecer aqui dentro da função, depois que Base
    # já foi criado.
    # ---------------------------------------------------------
    import models

    # Evita aviso de import não utilizado e deixa explícito
    # que o objetivo é registrar os modelos no SQLAlchemy.
    _ = models

    # Cria as tabelas que ainda não existem.
    Base.metadata.create_all(bind=engine)

    with engine.begin() as connection:

        # =====================================================
        # TABELA USERS
        # =====================================================

        _add_column_if_missing(
            connection,
            "users",
            "vip_until",
            "DATETIME"
        )

        _add_column_if_missing(
            connection,
            "users",
            "plan_type",
            "VARCHAR DEFAULT 'free'"
        )

        _add_column_if_missing(
            connection,
            "users",
            "status",
            "VARCHAR DEFAULT 'active'"
        )

        _add_column_if_missing(
            connection,
            "users",
            "subscription_id",
            "VARCHAR"
        )

        _add_column_if_missing(
            connection,
            "users",
            "started_at",
            "DATETIME"
        )

        _add_column_if_missing(
            connection,
            "users",
            "expires_at",
            "DATETIME"
        )

        _add_column_if_missing(
            connection,
            "users",
            "next_billing_at",
            "DATETIME"
        )

        _add_column_if_missing(
            connection,
            "users",
            "cancelled_at",
            "DATETIME"
        )

        _add_column_if_missing(
            connection,
            "users",
            "created_at",
            "DATETIME"
        )

        # =====================================================
        # TABELA TRANSACTIONS
        # =====================================================

        _add_column_if_missing(
            connection,
            "transactions",
            "email",
            "VARCHAR"
        )

        _add_column_if_missing(
            connection,
            "transactions",
            "amount",
            "FLOAT"
        )

        _add_column_if_missing(
            connection,
            "transactions",
            "subscription_id",
            "VARCHAR"
        )

        _add_column_if_missing(
            connection,
            "transactions",
            "approved_at",
            "DATETIME"
        )

        _add_column_if_missing(
            connection,
            "transactions",
            "processed_at",
            "DATETIME"
        )

        _add_column_if_missing(
            connection,
            "transactions",
            "created_at",
            "DATETIME"
        )