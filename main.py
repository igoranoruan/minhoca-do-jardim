import os
import uuid
import re
import subprocess
import requests
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
import yt_dlp
import mercadopago
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

from database import engine, Base, get_db, init_db
import models

# Inicializa o banco garantindo as colunas necessárias
init_db()

app = FastAPI(title="Minhoca de Jardim")

app.mount("/static", StaticFiles(directory="static"), name="static")
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Lista de e-mails de administradores com acesso VIP vitalício/grátis
ADMIN_EMAILS = ["igoranoruan@gmail.com"]


class PixPaymentRequest(BaseModel):
    plan_type: str
    email: str


def get_mp_token() -> str:
    """Busca o token do Mercado Pago limpando caracteres especiais e aspas."""
    token = os.getenv("MP_ACCESS_TOKEN", "").strip().strip('"').strip("'")
    if token:
        return token

    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8-sig", errors="ignore") as f:
                for line in f:
                    line_clean = line.strip()
                    if "MP_ACCESS_TOKEN" in line_clean and "=" in line_clean:
                        parts = line_clean.split("=", 1)
                        if len(parts) == 2:
                            return parts[1].strip().strip('"').strip("'")
        except Exception:
            pass
            
    return ""


def clean_metadata(input_path: str, output_path: str):
    """Remove metadados e altera o DNA digital de forma ultrarrápida no FFmpeg."""
    command = [
        "ffmpeg", "-y", "-i", input_path,
        "-map_metadata", "-1",
        "-c:v", "copy",
        "-c:a", "copy",
        output_path
    ]
    subprocess.run(command, check=True)


def download_direct_file(url: str, dest_path: str):
    """Realiza o download direto via HTTP stream."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    }
    res = requests.get(url, headers=headers, stream=True, timeout=45)
    res.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in res.iter_content(chunk_size=8192):
            f.write(chunk)


def try_tikwm_download(tiktok_url: str, dest_path: str) -> bool:
    """Download dedicado para vídeos do TikTok usando a API TikWM (Sem marca d'água)."""
    try:
        api_url = "https://www.tikwm.com/api/"
        payload = {"url": tiktok_url, "hd": 1}
        response = requests.post(api_url, data=payload, timeout=12)
        if response.status_code == 200:
            res_json = response.json()
            if res_json.get("code") == 0:
                video_url = res_json.get("data", {}).get("play") or res_json.get("data", {}).get("wmplay")
                if video_url:
                    if video_url.startswith("//"):
                        video_url = "https:" + video_url
                    download_direct_file(video_url, dest_path)
                    return True
    except Exception:
        pass
    return False


def try_cobalt_fallback(video_url: str, dest_path: str) -> bool:
    """Fallback com a API Cobalt v10."""
    instances = [
        "https://cobalt-api.kwi.im",
        "https://api.cobalt.tools"
    ]
    
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    }
    payload = {
        "url": video_url,
        "videoQuality": "max"
    }

    for instance_url in instances:
        try:
            response = requests.post(instance_url, json=payload, headers=headers, timeout=12)
            if response.status_code == 200:
                data = response.json()
                dl_url = data.get("url")
                if dl_url:
                    download_direct_file(dl_url, dest_path)
                    return True
        except Exception:
            continue

    return False


@app.get("/", response_class=HTMLResponse)
def read_root():
    if os.path.exists("static/index.html"):
        return FileResponse("static/index.html")
    return "<h1>Minhoca de Jardim Backend Rodando!</h1>"


@app.post("/api/download")
def process_video(request: Request, video_url: str, user_email: str = None, db: Session = Depends(get_db)):
    client_ip = request.client.host
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    now = datetime.utcnow()

    is_vip = False
    if user_email:
        if user_email in ADMIN_EMAILS:
            is_vip = True
        else:
            user = db.query(models.User).filter(models.User.email == user_email).first()
            if user and user.vip_until and user.vip_until > now:
                is_vip = True

    if not is_vip:
        usage = db.query(models.UserUsage).filter(models.UserUsage.ip_address == client_ip).first()
        if not usage:
            usage = models.UserUsage(ip_address=client_ip, downloads_today=0, last_download_date=today_str)
            db.add(usage)
            db.commit()

        if usage.last_download_date != today_str:
            usage.downloads_today = 0
            usage.last_download_date = today_str

        if usage.downloads_today >= 3:
            raise HTTPException(
                status_code=429,
                detail="Limite diário do plano Free atingido (3/3). Assine um plano VIP para continuar baixando sem limites!"
            )

    temp_id = str(uuid.uuid4())
    raw_path = os.path.join(DOWNLOAD_DIR, f"raw_{temp_id}.mp4")
    clean_path = os.path.join(DOWNLOAD_DIR, f"minhoca_{temp_id}.mp4")

    clean_url = video_url.strip()

    try:
        if "pin.it" in clean_url or "vt.tiktok.com" in clean_url or "vm.tiktok.com" in clean_url:
            r = requests.head(clean_url, allow_redirects=True, timeout=10)
            clean_url = r.url
    except Exception:
        pass

    downloaded = False

    if "tiktok.com" in clean_url:
        downloaded = try_tikwm_download(clean_url, raw_path)

    if not downloaded:
        downloaded = try_cobalt_fallback(clean_url, raw_path)

    if not downloaded:
        ydl_opts = {
            'outtmpl': raw_path,
            'format': 'bestvideo+bestaudio/best',
            'quiet': True,
            'nocheckcertificate': True,
            'ignoreerrors': False,
            'no_warnings': True,
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([clean_url])
            downloaded = True
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Erro ao baixar vídeo: {str(e)}")

    actual_raw_path = raw_path
    if not os.path.exists(raw_path):
        for file in os.listdir(DOWNLOAD_DIR):
            if file.startswith(f"raw_{temp_id}"):
                actual_raw_path = os.path.join(DOWNLOAD_DIR, file)
                break

    if not os.path.exists(actual_raw_path):
        raise HTTPException(status_code=500, detail="Não foi possível extrair o vídeo informado.")

    try:
        clean_metadata(actual_raw_path, clean_path)

        if not is_vip:
            usage.downloads_today += 1
            db.commit()

        if os.path.exists(actual_raw_path):
            os.remove(actual_raw_path)

        return {"status": "success", "download_url": f"/files/minhoca_{temp_id}.mp4", "is_vip": is_vip}

    except Exception as e:
        if os.path.exists(actual_raw_path):
            os.remove(actual_raw_path)
        raise HTTPException(status_code=500, detail=f"Erro no processamento FFmpeg: {str(e)}")


@app.get("/files/{filename}")
def get_file(filename: str):
    file_path = os.path.join(DOWNLOAD_DIR, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path, filename=filename, media_type="video/mp4")
    raise HTTPException(status_code=404, detail="Arquivo não encontrado.")


@app.post("/api/create-pix")
def create_pix_payment(payload: PixPaymentRequest, db: Session = Depends(get_db)):
    plan_type = payload.plan_type
    email = payload.email

    prices = {"semanal": 9.91, "mensal": 16.92, "vip": 29.93}
    
    if plan_type not in prices:
        raise HTTPException(status_code=400, detail="Plano inválido.")

    token = get_mp_token()
    if not token:
        raise HTTPException(status_code=500, detail="Token do Mercado Pago não configurado no servidor (MP_ACCESS_TOKEN).")

    try:
        sdk = mercadopago.SDK(token)

        payment_data = {
            "transaction_amount": prices[plan_type],
            "description": f"Minhoca de Jardim - Plano {plan_type.upper()}",
            "payment_method_id": "pix",
            "payer": {"email": email}
        }

        payment_response = sdk.payment().create(payment_data)
        payment = payment_response.get("response", {})

        if payment_response.get("status") == 201:
            new_transaction = models.Transaction(
                payment_id=str(payment["id"]),
                email=email,
                plan_type=plan_type,
                status="pending"
            )
            db.add(new_transaction)
            db.commit()

            return {
                "payment_id": payment["id"],
                "qr_code": payment["point_of_interaction"]["transaction_data"]["qr_code"],
                "qr_code_base64": payment["point_of_interaction"]["transaction_data"]["qr_code_base64"]
            }
        
        error_detail = payment.get("message", "Erro ao gerar cobrança no Mercado Pago.")
        raise HTTPException(status_code=400, detail=error_detail)

    except Exception as e:
        db.rollback()
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"Erro de integração com Mercado Pago: {str(e)}")


@app.post("/api/webhook")
async def mercado_pago_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        data = await request.json()
        if data.get("type") == "payment":
            payment_id = str(data["data"]["id"])
            
            token = get_mp_token()
            if not token:
                return {"status": "error", "message": "Token ausente"}
                
            sdk = mercadopago.SDK(token)
            payment_info = sdk.payment().get(payment_id)["response"]
            status = payment_info.get("status")

            transaction = db.query(models.Transaction).filter(models.Transaction.payment_id == payment_id).first()
            if transaction and status == "approved":
                transaction.status = "approved"

                user = db.query(models.User).filter(models.User.email == transaction.email).first()
                if not user:
                    user = models.User(email=transaction.email)
                    db.add(user)

                days_to_add = 7 if transaction.plan_type == "semanal" else 30
                
                now = datetime.utcnow()
                if user.vip_until and user.vip_until > now:
                    user.vip_until += timedelta(days=days_to_add)
                else:
                    user.vip_until = now + timedelta(days=days_to_add)

                db.commit()

        return {"status": "ok"}
    except Exception as e:
        db.rollback()
        return {"status": "error", "message": str(e)}


@app.get("/api/check-email")
def check_email_status(request: Request, email: str, db: Session = Depends(get_db)):
    """Verifica no banco se o e-mail possui VIP ativo ou qual a cota gratuita atual."""
    clean_email = email.strip().lower()
    now = datetime.utcnow()
    client_ip = request.client.host
    today_str = now.strftime("%Y-%m-%d")

    # 1. Checa se é admin
    if clean_email in ADMIN_EMAILS:
        return {"email": clean_email, "is_vip": True, "plan": "admin"}

    # 2. Checa se possui assinatura VIP ativa no banco
    user = db.query(models.User).filter(models.User.email == clean_email).first()
    if user and user.vip_until and user.vip_until > now:
        return {"email": clean_email, "is_vip": True, "vip_until": user.vip_until.isoformat()}

    # 3. Se for plano grátis, retorna a cota de uso por IP
    usage = db.query(models.UserUsage).filter(models.UserUsage.ip_address == client_ip).first()
    downloads_today = usage.downloads_today if (usage and usage.last_download_date == today_str) else 0

    return {"email": clean_email, "is_vip": False, "downloads_today": downloads_today}