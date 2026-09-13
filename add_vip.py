from datetime import datetime, timedelta
from database import SessionLocal
import models

def set_user_vip(email: str, days: int = 365):
    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.email == email).first()
        if not user:
            user = models.User(email=email)
            db.add(user)
        
        user.vip_until = datetime.utcnow() + timedelta(days=days)
        db.commit()
        print(f"Sucesso! O e-mail {email} agora tem acesso VIP até {user.vip_until}")
    except Exception as e:
        print(f"Erro ao conceder VIP: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    # Substitua pelo seu e-mail
    set_user_vip("igoranoruan@gmail.com", days=365)