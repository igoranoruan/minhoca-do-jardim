from datetime import datetime, timedelta

from database import SessionLocal
import models


def set_user_vip(email: str, days: int = 365):
    """Concede acesso VIP manual para testes/administração."""
    clean_email = (email or "").strip().lower()
    if not clean_email:
        raise ValueError("Informe um e-mail.")

    db = SessionLocal()
    try:
        now = datetime.utcnow()
        user = (
            db.query(models.User)
            .filter(models.User.email == clean_email)
            .first()
        )

        if not user:
            user = models.User(
                email=clean_email,
                plan_type="vip",
                status="active",
            )
            db.add(user)

        user.plan_type = "vip"
        user.status = "active"
        user.started_at = now
        user.expires_at = now + timedelta(days=days)
        user.vip_until = user.expires_at
        user.cancelled_at = None
        user.subscription_id = None
        user.next_billing_at = None

        db.commit()
        db.refresh(user)
        print(f"Sucesso! {clean_email} agora tem acesso VIP até {user.expires_at}.")
    except Exception as exc:
        db.rollback()
        print(f"Erro ao conceder VIP: {exc}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    set_user_vip("igoranoruan@gmail.com", days=365)
