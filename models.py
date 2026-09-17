from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String

from database import Base


class UserUsage(Base):
    __tablename__ = "user_usage"

    id = Column(Integer, primary_key=True, index=True)

    # Controle do plano Free por IP.
    ip_address = Column(String, unique=True, index=True)

    downloads_today = Column(Integer, default=0, nullable=False)

    # Reservas em processamento. Evita que requisições simultâneas ultrapassem a cota.
    reserved_today = Column(Integer, default=0, nullable=False)

    last_download_date = Column(String)


class PlanUsage(Base):
    __tablename__ = "plan_usage"

    id = Column(Integer, primary_key=True, index=True)

    # Usuário ao qual pertence o contador.
    user_id = Column(Integer, index=True, nullable=False)

    # Data no formato YYYY-MM-DD.
    usage_date = Column(String, index=True, nullable=False)

    # Quantidade de downloads/processamentos realizados
    # pelo usuário naquele dia.
    downloads_today = Column(Integer, default=0, nullable=False)

    # Reservas em processamento antes da confirmação do arquivo final.
    reserved_today = Column(Integer, default=0, nullable=False)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)

    # Identificação do cliente.
    email = Column(String, unique=True, index=True)

    # Compatibilidade com o sistema antigo.
    vip_until = Column(DateTime, nullable=True)

    # free | semanal | mensal | vip
    plan_type = Column(String, nullable=False, default="free")

    # active | cancelled | expired | pending
    status = Column(String, nullable=False, default="active")

    # ID da assinatura recorrente no Mercado Pago.
    subscription_id = Column(String, nullable=True, index=True)

    # Data em que o período atual começou.
    started_at = Column(DateTime, nullable=True)

    # Data em que o período atual termina.
    expires_at = Column(DateTime, nullable=True)

    # Próxima data prevista para cobrança.
    next_billing_at = Column(DateTime, nullable=True)

    # Data em que o cliente cancelou a renovação.
    cancelled_at = Column(DateTime, nullable=True)

    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)

    # ID do pagamento no Mercado Pago.
    payment_id = Column(String, unique=True, index=True)

    # E-mail associado ao pagamento.
    email = Column(String, index=True)

    # free | semanal | mensal | vip
    plan_type = Column(String)

    # pending | approved | rejected | cancelled | refunded | charged_back
    status = Column(String, default="pending")

    # Valor efetivamente associado à transação.
    amount = Column(Float, nullable=True)

    # ID da assinatura recorrente, quando existir.
    # Para Pix avulso, permanece NULL.
    subscription_id = Column(String, nullable=True, index=True)

    # Momento em que o pagamento foi aprovado.
    approved_at = Column(DateTime, nullable=True)

    # Momento em que o webhook/processamento foi concluído.
    processed_at = Column(DateTime, nullable=True)

    created_at = Column(
        DateTime,
        default=datetime.utcnow,
    )
