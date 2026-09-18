from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String, UniqueConstraint

from database import Base


class UserUsage(Base):
    __tablename__ = "user_usage"

    id = Column(Integer, primary_key=True, index=True)
    ip_address = Column(String, unique=True, index=True)

    # Nomes mantidos por compatibilidade. No plano Free representam
    # o total de gerações da semana atual.
    downloads_today = Column(Integer, default=0, nullable=False)
    reserved_today = Column(Integer, default=0, nullable=False)
    last_download_date = Column(String)


class PlanUsage(Base):
    __tablename__ = "plan_usage"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "usage_date",
            name="uq_plan_usage_user_date",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    usage_date = Column(String, index=True, nullable=False)
    downloads_today = Column(Integer, default=0, nullable=False)
    reserved_today = Column(Integer, default=0, nullable=False)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    vip_until = Column(DateTime, nullable=True)
    plan_type = Column(String, nullable=False, default="free")
    status = Column(String, nullable=False, default="active")

    # Campos legados, mantidos para não quebrar bancos antigos.
    # Novas compras não criam assinatura recorrente.
    subscription_id = Column(String, nullable=True, index=True)
    payment_type = Column(String, nullable=True)  # pix | cartao
    active_transaction_id = Column(Integer, nullable=True, index=True)

    started_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    next_billing_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)

    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)

    # ID real do pagamento. Enquanto o cartão ainda não foi pago,
    # usamos temporariamente preference:<id>.
    payment_id = Column(String, unique=True, index=True)

    # Referência criada pelo nosso servidor para correlacionar
    # preferência/pagamento no webhook.
    external_reference = Column(String, unique=True, index=True, nullable=True)

    email = Column(String, index=True)
    plan_type = Column(String)
    payment_type = Column(String, nullable=True)  # pix | cartao
    status = Column(String, default="pending")
    amount = Column(Float, nullable=True)

    # Compatibilidade com transações antigas recorrentes.
    subscription_id = Column(String, nullable=True, index=True)

    approved_at = Column(DateTime, nullable=True)
    processed_at = Column(DateTime, nullable=True)

    created_at = Column(
        DateTime,
        default=datetime.utcnow,
    )
