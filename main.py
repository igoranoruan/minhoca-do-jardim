import os
import re
import uuid
import hmac
import hashlib
import subprocess
import unicodedata
import zipfile
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlsplit, urlunsplit, parse_qsl, urlencode

import requests
import yt_dlp
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

load_dotenv()

from database import engine, Base, get_db, init_db
import models


init_db()


app = FastAPI(title="Minhoca de Jardim")

app.mount("/static", StaticFiles(directory="static"), name="static")

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

ADMIN_EMAILS = {
    email.strip().lower()
    for email in os.getenv("ADMIN_EMAILS", "igoranoruan@gmail.com").split(",")
    if email.strip()
}

FREE_LIMIT = 3
BATCH_MAX_ITEMS = 10
MAX_URL_LENGTH = 2048
MAX_DOWNLOAD_BYTES = 150 * 1024 * 1024
BATCH_MAX_TOTAL_BYTES = 500 * 1024 * 1024
MAX_FILENAME_LENGTH = 80
CLEAN_FILE_TTL_SECONDS = 3600

SUPPORTED_HOSTS = {
    "tiktok.com",
    "instagram.com",
    "youtube.com",
    "youtu.be",
    "pinterest.com",
    "pin.it",
}

PLAN_CONFIG = {
    "semanal": {
        "name": "Semanal",
        "amount": 9.90,
        "frequency": 7,
        "frequency_type": "days",
        "limit": 10,
        "label": "10 downloads por dia",
    },
    "mensal": {
        "name": "Mensal",
        "amount": 16.90,
        "frequency": 1,
        "frequency_type": "months",
        "limit": 30,
        "label": "30 downloads por dia",
    },
    "vip": {
        "name": "VIP Batch",
        "amount": 29.90,
        "frequency": 1,
        "frequency_type": "months",
        "limit": None,
        "label": "Downloads ilimitados + lote",
    },
}


class SubscriptionRequest(BaseModel):
    plan_type: str
    email: str


class BatchRequest(BaseModel):
    urls: list[str]
    names: list[str | None] | None = None
    user_email: str | None = None
    filename_prefix: str | None = None


def get_mp_token() -> str:
    token = os.getenv("MP_ACCESS_TOKEN", "").strip().strip('"').strip("'")
    return token


def get_public_base_url() -> str:
    value = (
        os.getenv("PUBLIC_BASE_URL")
        or os.getenv("RENDER_EXTERNAL_URL")
        or ""
    ).strip().rstrip("/")

    if not value:
        raise HTTPException(
            status_code=500,
            detail=(
                "PUBLIC_BASE_URL não configurada. "
                "O Mercado Pago exige uma URL pública HTTPS para o retorno do checkout."
            ),
        )

    parsed = urlparse(value)

    if parsed.scheme != "https" or not parsed.netloc:
        raise HTTPException(
            status_code=500,
            detail=(
                "PUBLIC_BASE_URL inválida. Informe uma URL pública HTTPS, "
                "por exemplo: https://seu-app.onrender.com"
            ),
        )

    return value


def get_webhook_secret() -> str:
    return os.getenv("MP_WEBHOOK_SECRET", "").strip()


def normalize_email(email: str) -> str:
    clean = (email or "").strip().lower()

    if len(clean) > 254 or not re.fullmatch(
        r"[^@\s]+@[^@\s]+\.[^@\s]+",
        clean,
    ):
        raise HTTPException(status_code=400, detail="Informe um e-mail válido.")

    return clean


def is_admin(email: str | None) -> bool:
    return bool(email and email.strip().lower() in ADMIN_EMAILS)


def is_valid_video_url(value: str) -> bool:
    if not value or len(value) > MAX_URL_LENGTH:
        return False

    try:
        parsed = urlparse(value)
    except ValueError:
        return False

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False

    hostname = (parsed.hostname or "").lower().rstrip(".")

    return any(
        hostname == allowed
        or hostname.endswith(f".{allowed}")
        for allowed in SUPPORTED_HOSTS
    )


def is_safe_remote_download_url(value: str) -> bool:
    """Evita que fallbacks externos sejam usados como SSRF."""

    try:
        parsed = urlparse(value)
    except ValueError:
        return False

    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False

    hostname = parsed.hostname.lower().rstrip(".")

    blocked_hosts = {
        "localhost",
        "localhost.localdomain",
        "0.0.0.0",
        "127.0.0.1",
        "::1",
    }

    if hostname in blocked_hosts or hostname.endswith(".local"):
        return False

    return True


def clean_metadata(input_path: str, output_path: str):
    """
    Gera uma nova versão do arquivo e remove os metadados do contêiner.
    Não é uma garantia de evasão de sistemas de detecção das plataformas.
    """
    command = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        "-map_metadata",
        "-1",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        output_path,
    ]

    subprocess.run(
        command,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=180,
    )

    if not os.path.exists(output_path) or os.path.getsize(output_path) < 1024:
        raise RuntimeError("O arquivo processado ficou inválido ou vazio.")


def download_direct_file(url: str, dest_path: str):
    if not is_safe_remote_download_url(url):
        raise RuntimeError("A fonte externa do vídeo não é válida.")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        )
    }

    with requests.get(
        url,
        headers=headers,
        stream=True,
        timeout=(10, 60),
        allow_redirects=True,
    ) as response:
        response.raise_for_status()

        final_url = response.url
        if not is_safe_remote_download_url(final_url):
            raise RuntimeError("O redirecionamento da fonte não é seguro.")

        content_type = (response.headers.get("content-type") or "").lower()
        if "text/html" in content_type:
            raise RuntimeError("A fonte retornou uma página HTML em vez do vídeo.")

        content_length = response.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError("O vídeo excede o tamanho máximo permitido.")
            except ValueError:
                pass

        total = 0

        with open(dest_path, "wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 128):
                if not chunk:
                    continue

                total += len(chunk)

                if total > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError("O vídeo excede o tamanho máximo permitido.")

                file.write(chunk)

    if not os.path.exists(dest_path) or os.path.getsize(dest_path) < 1024:
        raise RuntimeError("O download retornou um arquivo inválido ou vazio.")


def try_tikwm_download(tiktok_url: str, dest_path: str) -> bool:
    try:
        response = requests.post(
            "https://www.tikwm.com/api/",
            data={"url": tiktok_url, "hd": 1},
            timeout=15,
        )

        if response.status_code != 200:
            return False

        data = response.json()
        if data.get("code") != 0:
            return False

        video_url = (
            data.get("data", {}).get("play")
            or data.get("data", {}).get("wmplay")
        )

        if not video_url:
            return False

        if video_url.startswith("//"):
            video_url = "https:" + video_url

        download_direct_file(video_url, dest_path)
        return True

    except Exception:
        return False


def try_cobalt_fallback(video_url: str, dest_path: str) -> bool:
    instances = [
        "https://cobalt-api.kwi.im",
        "https://api.cobalt.tools",
    ]

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
    }

    payload = {
        "url": video_url,
        "videoQuality": "max",
    }

    for instance_url in instances:
        try:
            response = requests.post(
                instance_url,
                json=payload,
                headers=headers,
                timeout=20,
            )

            if response.status_code != 200:
                continue

            data = response.json()
            download_url = data.get("url")

            if not download_url:
                continue

            download_direct_file(download_url, dest_path)
            return True

        except Exception:
            continue

    return False


def cleanup_old_files():
    now = datetime.utcnow().timestamp()

    try:
        for filename in os.listdir(DOWNLOAD_DIR):
            path = os.path.join(DOWNLOAD_DIR, filename)

            if not os.path.isfile(path):
                continue

            try:
                age = now - os.path.getmtime(path)
                if age > CLEAN_FILE_TTL_SECONDS:
                    os.remove(path)
            except OSError:
                continue
    except OSError:
        pass


def get_user(db: Session, email: str):
    return (
        db.query(models.User)
        .filter(models.User.email == email)
        .first()
    )


def get_or_create_user(db: Session, email: str):
    user = get_user(db, email)

    if user:
        return user

    user = models.User(
        email=email,
        plan_type="free",
        status="active",
    )
    db.add(user)

    try:
        db.commit()
        db.refresh(user)
        return user
    except IntegrityError:
        db.rollback()
        return get_user(db, email)


def subscription_is_active(user, now: datetime) -> bool:
    if not user:
        return False

    if user.status not in {"active", "cancelled"}:
        return False

    if user.expires_at and user.expires_at > now:
        return True

    # Compatibilidade com clientes antigos.
    if user.vip_until and user.vip_until > now:
        return True

    return False


def current_plan_for_user(user, now: datetime) -> str:
    if not user:
        return "free"

    if user.plan_type in PLAN_CONFIG and subscription_is_active(user, now):
        return user.plan_type

    if user.vip_until and user.vip_until > now:
        return "vip"

    return "free"


def quota_for_plan(plan_type: str):
    if plan_type == "free":
        return FREE_LIMIT
    return PLAN_CONFIG[plan_type]["limit"]


def get_free_usage(db: Session, client_ip: str, today_str: str):
    """Obtém o contador Free do IP e prepara o dia atual."""
    usage = (
        db.query(models.UserUsage)
        .filter(models.UserUsage.ip_address == client_ip)
        .first()
    )

    if not usage:
        usage = models.UserUsage(
            ip_address=client_ip,
            downloads_today=0,
            reserved_today=0,
            last_download_date=today_str,
        )
        db.add(usage)

        try:
            db.commit()
            db.refresh(usage)
        except IntegrityError:
            db.rollback()
            usage = (
                db.query(models.UserUsage)
                .filter(models.UserUsage.ip_address == client_ip)
                .first()
            )

    if usage.last_download_date != today_str:
        usage.downloads_today = 0
        usage.reserved_today = 0
        usage.last_download_date = today_str
        db.commit()
        db.refresh(usage)

    return usage


def get_plan_usage(db: Session, user_id: int, today_str: str):
    usage = (
        db.query(models.PlanUsage)
        .filter(
            models.PlanUsage.user_id == user_id,
            models.PlanUsage.usage_date == today_str,
        )
        .first()
    )

    if not usage:
        usage = models.PlanUsage(
            user_id=user_id,
            usage_date=today_str,
            downloads_today=0,
            reserved_today=0,
        )
        db.add(usage)

        try:
            db.commit()
            db.refresh(usage)
        except IntegrityError:
            db.rollback()
            usage = (
                db.query(models.PlanUsage)
                .filter(
                    models.PlanUsage.user_id == user_id,
                    models.PlanUsage.usage_date == today_str,
                )
                .first()
            )

    return usage


def _reserve_usage_row(
    db: Session,
    usage,
    limit: int,
    reserved_column,
    count_column,
):
    """Reserva uma unidade de cota de forma atômica antes do processamento."""
    result = db.execute(
        update(usage.__class__)
        .where(usage.__class__.id == usage.id)
        .where((count_column + reserved_column) < limit)
        .values(reserved_today=reserved_column + 1)
    )
    db.commit()

    if result.rowcount != 1:
        db.refresh(usage)
        raise HTTPException(
            status_code=429,
            detail=f"Limite diário atingido ({limit}/{limit}).",
        )

    db.refresh(usage)
    return usage


def reserve_quota(
    db: Session,
    plan_type: str,
    client_ip: str,
    user,
    today_str: str,
):
    """Reserva uma unidade somente quando ainda existe capacidade real."""
    if plan_type == "vip":
        return None

    if plan_type == "free":
        usage = get_free_usage(db, client_ip, today_str)
        try:
            return _reserve_usage_row(
                db,
                usage,
                FREE_LIMIT,
                models.UserUsage.reserved_today,
                models.UserUsage.downloads_today,
            )
        except HTTPException:
            raise HTTPException(
                status_code=429,
                detail="Limite diário do plano Free atingido (3/3).",
            )

    if not user:
        raise HTTPException(
            status_code=400,
            detail="Ative seu e-mail antes de usar um plano pago.",
        )

    usage = get_plan_usage(db, user.id, today_str)
    limit = PLAN_CONFIG[plan_type]["limit"]

    if limit is None:
        return usage

    try:
        return _reserve_usage_row(
            db,
            usage,
            limit,
            models.PlanUsage.reserved_today,
            models.PlanUsage.downloads_today,
        )
    except HTTPException:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Limite diário do plano {PLAN_CONFIG[plan_type]['name']} "
                f"atingido ({limit}/{limit})."
            ),
        )


def release_reserved_usage(db: Session, usage, plan_type: str):
    """Libera uma reserva quando o processamento falha."""
    if usage is None or plan_type == "vip":
        return

    model = models.UserUsage if plan_type == "free" else models.PlanUsage
    result = db.execute(
        update(model)
        .where(model.id == usage.id)
        .where(model.reserved_today > 0)
        .values(reserved_today=model.reserved_today - 1)
    )
    db.commit()
    if result.rowcount:
        db.refresh(usage)


def record_successful_usage(db: Session, usage, plan_type: str):
    """Converte a reserva em consumo confirmado após gerar o arquivo."""
    if usage is None or plan_type == "vip":
        return None

    model = models.UserUsage if plan_type == "free" else models.PlanUsage
    result = db.execute(
        update(model)
        .where(model.id == usage.id)
        .where(model.reserved_today > 0)
        .values(
            downloads_today=model.downloads_today + 1,
            reserved_today=model.reserved_today - 1,
        )
    )
    db.commit()

    if result.rowcount != 1:
        raise RuntimeError("Não foi possível confirmar o consumo da cota.")

    db.refresh(usage)
    return usage.downloads_today

def resolve_short_url(url: str) -> str:
    try:
        hostname = (urlparse(url).hostname or "").lower()

        if hostname in {
            "pin.it",
            "vt.tiktok.com",
            "vm.tiktok.com",
        }:
            response = requests.head(
                url,
                allow_redirects=True,
                timeout=10,
            )
            if response.url:
                return response.url
    except Exception:
        pass

    return url


def download_video_source(video_url: str, raw_path: str):
    clean_url = resolve_short_url(video_url)
    downloaded = False

    hostname = (urlparse(clean_url).hostname or "").lower()

    if "tiktok.com" in hostname:
        downloaded = try_tikwm_download(clean_url, raw_path)

    if not downloaded:
        downloaded = try_cobalt_fallback(clean_url, raw_path)

    if not downloaded:
        ydl_opts = {
            "outtmpl": raw_path,
            "format": "bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "quiet": True,
            "ignoreerrors": False,
            "no_warnings": True,
            "noplaylist": True,
            "retries": 2,
            "fragment_retries": 2,
            "socket_timeout": 30,
            "concurrent_fragment_downloads": 2,
            "js_runtimes": {"deno": {"path": "/usr/local/bin/deno"}},
            "remote_components": {"ejs:npm"},
            "extractor_args": {
                "youtube": {
                    "player_client": ["web", "android"],
                }
            },
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([clean_url])
            downloaded = True
        except Exception as exc:
            raise RuntimeError(
                f"Não foi possível baixar este vídeo: {exc}"
            ) from exc

    actual_raw_path = raw_path

    if not os.path.exists(raw_path):
        prefix = os.path.basename(raw_path).rsplit(".", 1)[0]

        for filename in os.listdir(DOWNLOAD_DIR):
            if filename.startswith(prefix):
                actual_raw_path = os.path.join(
                    DOWNLOAD_DIR,
                    filename,
                )
                break

    if not downloaded or not os.path.exists(actual_raw_path):
        raise RuntimeError(
            "Não foi possível extrair o vídeo informado."
        )

    if os.path.getsize(actual_raw_path) > MAX_DOWNLOAD_BYTES:
        try:
            os.remove(actual_raw_path)
        except OSError:
            pass
        raise RuntimeError("O vídeo excede o tamanho máximo permitido.")

    return actual_raw_path


def friendly_download_error(video_url: str, error: Exception) -> str:
    message = str(error)
    lower = message.lower()
    hostname = (urlparse(video_url).hostname or "").lower()

    if "instagram.com" in hostname and (
        "429" in lower
        or "too many requests" in lower
        or "rate" in lower
    ):
        return (
            "O Instagram limitou temporariamente a origem deste servidor. "
            "Tente novamente mais tarde ou use outro vídeo. "
            "Isso é uma limitação da plataforma, não do processamento MP4."
        )

    if "youtube.com" in hostname or "youtu.be" in hostname:
        if "sign in to confirm" in lower or "not a bot" in lower:
            return (
                "O YouTube exigiu autenticação para este vídeo no servidor. "
                "Este link não pôde ser processado agora."
            )

    return message or "Não foi possível processar o vídeo."


def sanitize_filename(value: str | None, fallback: str = "video_minhoca") -> str:
    """Converte o nome informado pelo usuário em um nome de arquivo seguro."""

    text = (value or "").strip()
    if not text:
        text = fallback

    text = os.path.splitext(text)[0]
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^A-Za-z0-9 _-]+", "", text)
    text = re.sub(r"\s+", "_", text).strip("._-")
    text = re.sub(r"_+", "_", text)

    if not text:
        text = fallback

    return text[:MAX_FILENAME_LENGTH].rstrip("._-") or fallback


def build_output_filename(temp_id: str, requested_name: str | None = None) -> str:
    """Mantém o UUID no caminho e usa o nome informado de forma segura."""

    safe_name = sanitize_filename(requested_name)
    return f"minhoca_{temp_id}_{safe_name}.mp4"


def file_path_from_download_url(download_url: str) -> str:
    filename = os.path.basename(urlparse(download_url).path)
    if not re.fullmatch(
        r"minhoca_[0-9a-f-]+(?:_[A-Za-z0-9_-]+)?\.mp4",
        filename,
        flags=re.IGNORECASE,
    ):
        raise RuntimeError("Arquivo de download inválido.")
    return os.path.join(DOWNLOAD_DIR, filename)


def create_batch_zip(file_paths: list[tuple[str, str]]) -> str | None:
    """Cria um ZIP com os arquivos processados, sem duplicar nomes."""

    if not file_paths:
        return None

    total_bytes = 0
    for path, _ in file_paths:
        if not os.path.exists(path):
            continue
        total_bytes += os.path.getsize(path)

    if total_bytes > BATCH_MAX_TOTAL_BYTES:
        raise RuntimeError(
            "O lote ultrapassou o tamanho máximo de 500 MB para o download em bloco."
        )

    zip_id = str(uuid.uuid4())
    zip_filename = f"minhoca_lote_{zip_id}.zip"
    zip_path = os.path.join(DOWNLOAD_DIR, zip_filename)
    used_names = set()

    try:
        with zipfile.ZipFile(
            zip_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            for path, desired_name in file_paths:
                if not os.path.exists(path):
                    continue

                base_name = sanitize_filename(desired_name, "video_minhoca")
                archive_name = f"{base_name}.mp4"
                counter = 2

                while archive_name.lower() in used_names:
                    archive_name = f"{base_name}_{counter}.mp4"
                    counter += 1

                used_names.add(archive_name.lower())
                archive.write(path, arcname=archive_name)

        if not os.path.exists(zip_path) or os.path.getsize(zip_path) < 100:
            raise RuntimeError("Não foi possível gerar o arquivo ZIP do lote.")

        return f"/files/{zip_filename}"
    except Exception:
        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
            except OSError:
                pass
        raise


def process_one_video(
    video_url: str,
    quota_usage,
    plan_type: str,
    requested_name: str | None = None,
):
    temp_id = str(uuid.uuid4())
    raw_path = os.path.join(
        DOWNLOAD_DIR,
        f"raw_{temp_id}.mp4",
    )
    clean_filename = build_output_filename(temp_id, requested_name)
    clean_path = os.path.join(
        DOWNLOAD_DIR,
        clean_filename,
    )

    actual_raw_path = raw_path

    try:
        actual_raw_path = download_video_source(
            video_url,
            raw_path,
        )

        clean_metadata(
            actual_raw_path,
            clean_path,
        )

        return f"/files/{clean_filename}"

    except Exception:
        if os.path.exists(clean_path):
            try:
                os.remove(clean_path)
            except OSError:
                pass

        raise

    finally:
        if os.path.exists(actual_raw_path):
            try:
                os.remove(actual_raw_path)
            except OSError:
                pass


def verify_webhook_signature(request: Request, data_id: str) -> bool:
    secret = get_webhook_secret()

    # Em desenvolvimento local, a chave pode ainda não estar configurada.
    # Em produção, o webhook deve ser protegido.
    if not secret:
        if os.getenv("ENVIRONMENT", "development").lower() == "production":
            return False
        return True

    x_signature = request.headers.get("x-signature", "")
    x_request_id = request.headers.get("x-request-id", "")

    if not x_signature or not x_request_id or not data_id:
        return False

    parts = {}
    for item in x_signature.split(","):
        if "=" in item:
            key, value = item.split("=", 1)
            parts[key.strip()] = value.strip()

    timestamp = parts.get("ts")
    received_hash = parts.get("v1")

    if not timestamp or not received_hash:
        return False

    manifest = (
        f"id:{data_id};"
        f"request-id:{x_request_id};"
        f"ts:{timestamp};"
    )

    expected_hash = hmac.new(
        secret.encode("utf-8"),
        manifest.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(
        expected_hash,
        received_hash,
    )


def mp_headers(token: str):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def normalize_subscription_checkout_url(init_point: str | None) -> str | None:
    """
    Normaliza o checkout retornado pelo Mercado Pago.

    Alguns retornos do checkout podem incluir parâmetros auxiliares que não
    fazem parte do init_point documentado para o checkout de assinaturas.
    O parâmetro `activation` não é necessário para o checkout padrão e,
    quando presente, pode gerar uma página inválida no navegador.

    Se o Mercado Pago devolver um init_point normal, ele permanece intacto.
    """
    if not init_point:
        return None

    try:
        parts = urlsplit(init_point)
        query = parse_qsl(parts.query, keep_blank_values=True)
        filtered_query = [
            (key, value)
            for key, value in query
            if key.lower() != "activation"
        ]

        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode(filtered_query),
                parts.fragment,
            )
        )
    except Exception:
        return init_point


def mp_create_subscription(
    email: str,
    plan_type: str,
    external_reference: str,
):
    token = get_mp_token()

    if not token:
        raise HTTPException(
            status_code=500,
            detail=(
                "Mercado Pago não configurado no servidor "
                "(MP_ACCESS_TOKEN)."
            ),
        )

    plan = PLAN_CONFIG[plan_type]

    payload = {
        "reason": (
            f"Minhoca de Jardim - "
            f"Plano {plan['name']}"
        ),
        "external_reference": external_reference,
        "payer_email": email,
        "auto_recurring": {
            "frequency": plan["frequency"],
            "frequency_type": plan["frequency_type"],
            "transaction_amount": plan["amount"],
            "currency_id": "BRL",
        },
        "back_url": get_public_base_url(),
        "status": "pending",
    }

    headers = mp_headers(token)
    headers["X-Idempotency-Key"] = str(uuid.uuid4())

    response = requests.post(
        "https://api.mercadopago.com/preapproval",
        headers=headers,
        json=payload,
        timeout=30,
    )

    try:
        data = response.json()
    except ValueError:
        data = {}

    if not response.ok:
        message = (
            data.get("message")
            or data.get("error")
            or "Mercado Pago recusou a criação da assinatura."
        )
        raise HTTPException(
            status_code=502,
            detail=message,
        )

    if not data.get("id") or not data.get("init_point"):
        raise HTTPException(
            status_code=502,
            detail="Mercado Pago não retornou o checkout da assinatura.",
        )

    data["init_point"] = normalize_subscription_checkout_url(
        data.get("init_point")
    )

    return data


def mp_get_subscription(subscription_id: str):
    token = get_mp_token()

    if not token:
        raise RuntimeError("MP_ACCESS_TOKEN não configurado.")

    response = requests.get(
        f"https://api.mercadopago.com/preapproval/{subscription_id}",
        headers=mp_headers(token),
        timeout=20,
    )

    response.raise_for_status()
    return response.json()


def mp_get_payment(payment_id: str):
    token = get_mp_token()

    if not token:
        raise RuntimeError("MP_ACCESS_TOKEN não configurado.")

    response = requests.get(
        f"https://api.mercadopago.com/v1/payments/{payment_id}",
        headers=mp_headers(token),
        timeout=20,
    )

    response.raise_for_status()
    return response.json()


def mp_create_pix_payment(
    email: str,
    plan_type: str,
    external_reference: str,
):
    """Cria um pagamento único via Pix pelo Checkout API."""

    token = get_mp_token()

    if not token:
        raise HTTPException(
            status_code=500,
            detail="Mercado Pago não configurado no servidor (MP_ACCESS_TOKEN).",
        )

    plan = PLAN_CONFIG[plan_type]

    payload = {
        "transaction_amount": plan["amount"],
        "description": f"Minhoca de Jardim - Plano {plan['name']} (Pix)",
        "payment_method_id": "pix",
        "external_reference": external_reference,
        "notification_url": f"{get_public_base_url()}/api/webhook",
        "payer": {
            "email": email,
        },
    }

    headers = mp_headers(token)
    headers["X-Idempotency-Key"] = str(uuid.uuid4())

    response = requests.post(
        "https://api.mercadopago.com/v1/payments",
        headers=headers,
        json=payload,
        timeout=30,
    )

    try:
        data = response.json()
    except ValueError:
        data = {}

    if not response.ok:
        message = (
            data.get("message")
            or data.get("error")
            or "Mercado Pago recusou a criação do pagamento Pix."
        )
        raise HTTPException(status_code=502, detail=message)

    transaction_data = (
        data.get("point_of_interaction", {})
        .get("transaction_data", {})
    )

    if not data.get("id") or not transaction_data.get("qr_code"):
        raise HTTPException(
            status_code=502,
            detail="Mercado Pago não retornou os dados do Pix.",
        )

    return data


def mp_get_authorized_payment(authorized_payment_id: str):
    """
    Consulta a fatura/cobrança recorrente enviada pelo evento
    subscription_authorized_payment.
    """

    token = get_mp_token()

    if not token:
        raise RuntimeError("MP_ACCESS_TOKEN não configurado.")

    response = requests.get(
        f"https://api.mercadopago.com/authorized_payments/{authorized_payment_id}",
        headers=mp_headers(token),
        timeout=20,
    )

    response.raise_for_status()
    return response.json()


def mp_cancel_subscription(subscription_id: str):
    token = get_mp_token()

    if not token:
        raise HTTPException(
            status_code=500,
            detail="Mercado Pago não configurado no servidor.",
        )

    response = requests.put(
        f"https://api.mercadopago.com/preapproval/{subscription_id}",
        headers=mp_headers(token),
        json={"status": "canceled"},
        timeout=20,
    )

    try:
        data = response.json()
    except ValueError:
        data = {}

    if not response.ok:
        raise HTTPException(
            status_code=502,
            detail=(
                data.get("message")
                or "Não foi possível cancelar a assinatura."
            ),
        )

    return data


def parse_mp_datetime(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
        if parsed.tzinfo:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except (TypeError, ValueError):
        return None


def period_end_for_plan(plan_type: str, now: datetime):
    if plan_type == "semanal":
        return now + timedelta(days=7)

    if plan_type in {"mensal", "vip"}:
        # A recorrência mensal é gerenciada pelo Mercado Pago.
        # Para o acesso local, usamos 30 dias como janela comercial.
        return now + timedelta(days=30)

    return None


def activate_user_from_payment(
    db: Session,
    payment: dict,
    expected_subscription_id: str | None = None,
):
    """
    Libera/renova o acesso somente depois de confirmar o pagamento
    diretamente na API do Mercado Pago.

    Quando a cobrança veio de subscription_authorized_payment,
    expected_subscription_id é usado como uma segunda validação
    para garantir que a cobrança pertence à assinatura armazenada
    para o usuário.
    """

    email = (
        payment.get("payer", {}).get("email")
        or payment.get("metadata", {}).get("email")
    )

    if not email:
        return False

    email = normalize_email(email)
    user = get_user(db, email)

    if not user or not user.subscription_id:
        return False

    if (
        expected_subscription_id
        and str(user.subscription_id)
        != str(expected_subscription_id)
    ):
        return False

    try:
        subscription = mp_get_subscription(
            user.subscription_id
        )
    except Exception:
        return False

    if expected_subscription_id:
        if str(subscription.get("id")) != str(expected_subscription_id):
            return False

    subscription_amount = (
        subscription.get("auto_recurring", {})
        .get("transaction_amount")
    )

    payment_amount = payment.get("transaction_amount")

    if subscription_amount is not None and payment_amount is not None:
        if abs(float(subscription_amount) - float(payment_amount)) > 0.01:
            return False

    plan_type = user.plan_type

    if plan_type not in PLAN_CONFIG:
        return False

    now = datetime.utcnow()
    next_payment = parse_mp_datetime(
        subscription.get("next_payment_date")
    )

    if next_payment and next_payment > now:
        expires_at = next_payment
    else:
        expires_at = period_end_for_plan(
            plan_type,
            now,
        )

    user.status = "active"
    user.started_at = (
        user.started_at
        if user.started_at and user.expires_at and user.expires_at > now
        else now
    )
    user.expires_at = expires_at
    user.next_billing_at = next_payment
    user.cancelled_at = None

    # Mantém compatibilidade com a estrutura antiga.
    user.vip_until = expires_at

    db.commit()
    return True


def activate_user_from_pix_payment(
    db: Session,
    payment: dict,
    transaction: models.Transaction,
):
    """Libera um período pago via Pix, sem criar renovação automática."""

    if not transaction or transaction.subscription_id:
        return False

    plan_type = transaction.plan_type
    if plan_type not in PLAN_CONFIG:
        return False

    # Para Pix, a identidade da conta do Minhoca é o e-mail informado
    # no checkout e salvo na transação. O payer.email retornado pelo
    # Mercado Pago pode vir ausente ou mascarado, então ele não pode
    # bloquear a ativação de um pagamento já aprovado.
    if not transaction.email:
        return False

    expected_email = normalize_email(transaction.email)
    email = expected_email

    payment_amount = payment.get("transaction_amount")
    expected_amount = transaction.amount

    if payment_amount is None or expected_amount is None:
        return False

    if abs(float(payment_amount) - float(expected_amount)) > 0.01:
        return False

    user = get_or_create_user(db, email)
    now = datetime.utcnow()

    current_access = (
        user.expires_at
        if user.expires_at and user.expires_at > now
        else now
    )

    period_end = current_access + (
        timedelta(days=7)
        if plan_type == "semanal"
        else timedelta(days=30)
    )

    user.plan_type = plan_type
    user.status = "active"
    user.started_at = (
        user.started_at
        if user.started_at and user.expires_at and user.expires_at > now
        else now
    )
    user.expires_at = period_end
    user.next_billing_at = None
    user.cancelled_at = None
    user.subscription_id = None
    user.vip_until = period_end

    db.commit()
    return True


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def read_root():
    if os.path.exists("static/index.html"):
        return FileResponse("static/index.html")

    return "<h1>Minhoca de Jardim Backend Rodando!</h1>"


@app.post("/api/download")
def process_video(
    request: Request,
    video_url: str,
    user_email: str | None = None,
    filename: str | None = None,
    db: Session = Depends(get_db),
):
    cleanup_old_files()

    if not is_valid_video_url(video_url.strip()):
        raise HTTPException(
            status_code=400,
            detail="Informe uma URL válida de vídeo.",
        )

    client_ip = request.client.host if request.client else "unknown"
    now = datetime.utcnow()
    today_str = now.strftime("%Y-%m-%d")

    email = None
    user = None

    if user_email:
        email = normalize_email(user_email)
        user = get_user(db, email)

    if is_admin(email):
        plan_type = "vip"
    else:
        plan_type = current_plan_for_user(user, now)

    quota_usage = reserve_quota(
        db=db,
        plan_type=plan_type,
        client_ip=client_ip,
        user=user,
        today_str=today_str,
    )

    try:
        download_url = process_one_video(
            video_url.strip(),
            quota_usage,
            plan_type,
            requested_name=filename,
        )

        # Só contabiliza depois que o arquivo final foi gerado.
        downloads_today = record_successful_usage(
            db,
            quota_usage,
            plan_type,
        )

        display_filename = (
            f"{sanitize_filename(filename)}.mp4"
            if filename
            else "video_minhoca.mp4"
        )

        return {
            "status": "success",
            "download_url": download_url,
            "filename": display_filename,
            "plan": plan_type,
            "downloads_today": downloads_today,
            "daily_limit": quota_for_plan(plan_type),
        }

    except HTTPException:
        if quota_usage is not None:
            release_reserved_usage(db, quota_usage, plan_type)
        raise

    except Exception as exc:
        if quota_usage is not None:
            release_reserved_usage(db, quota_usage, plan_type)
        raise HTTPException(
            status_code=502,
            detail=friendly_download_error(video_url.strip(), exc),
        ) from exc


@app.post("/api/download-batch")
def process_batch(
    payload: BatchRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    cleanup_old_files()

    if not payload.urls:
        raise HTTPException(
            status_code=400,
            detail="Informe pelo menos uma URL.",
        )

    if len(payload.urls) > BATCH_MAX_ITEMS:
        raise HTTPException(
            status_code=400,
            detail=f"O lote aceita no máximo {BATCH_MAX_ITEMS} vídeos.",
        )

    if payload.names is not None and len(payload.names) > BATCH_MAX_ITEMS:
        raise HTTPException(
            status_code=400,
            detail=f"O lote aceita no máximo {BATCH_MAX_ITEMS} nomes.",
        )

    email = (
        normalize_email(payload.user_email)
        if payload.user_email
        else None
    )

    user = get_user(db, email) if email else None
    now = datetime.utcnow()

    if is_admin(email):
        plan_type = "vip"
    else:
        plan_type = current_plan_for_user(user, now)

    if plan_type != "vip":
        raise HTTPException(
            status_code=403,
            detail="O processamento em lote está disponível apenas no VIP Batch.",
        )

    results = []
    successful_files: list[tuple[str, str]] = []
    batch_total_bytes = 0
    names = payload.names or []

    for index, raw_url in enumerate(payload.urls, start=1):
        url = raw_url.strip()
        provided_name = names[index - 1] if index - 1 < len(names) else None
        requested_name = sanitize_filename(
            provided_name or (
                f"{sanitize_filename(payload.filename_prefix, 'video')}_{index:02d}"
                if payload.filename_prefix
                else f"video_{index:02d}"
            ),
            f"video_{index:02d}",
        )

        if not is_valid_video_url(url):
            results.append({
                "url": url,
                "status": "error",
                "detail": "URL inválida.",
            })
            continue

        try:
            download_url = process_one_video(
                url,
                None,
                plan_type,
                requested_name=requested_name,
            )
            file_path = file_path_from_download_url(download_url)
            file_size = os.path.getsize(file_path)

            if batch_total_bytes + file_size > BATCH_MAX_TOTAL_BYTES:
                try:
                    os.remove(file_path)
                except OSError:
                    pass

                results.append({
                    "url": url,
                    "status": "error",
                    "detail": "O lote ultrapassaria o limite total de 500 MB.",
                })
                break

            batch_total_bytes += file_size
            successful_files.append((file_path, requested_name))

            results.append({
                "url": url,
                "status": "success",
                "download_url": download_url,
                "filename": f"{requested_name}.mp4",
            })
        except Exception as exc:
            results.append({
                "url": url,
                "status": "error",
                "detail": friendly_download_error(url, exc),
            })

    processed_count = len(results)
    if processed_count < len(payload.urls):
        for remaining_index in range(processed_count, len(payload.urls)):
            results.append({
                "url": payload.urls[remaining_index].strip(),
                "status": "error",
                "detail": "Processamento interrompido após atingir o limite total de 500 MB do lote.",
            })

    zip_url = None
    zip_error = None

    if successful_files:
        try:
            zip_url = create_batch_zip(successful_files)
        except Exception as exc:
            zip_error = str(exc)

    return {
        "status": "success",
        "plan": plan_type,
        "total": len(payload.urls),
        "successful": len(successful_files),
        "failed": len(payload.urls) - len(successful_files),
        "results": results,
        "zip_url": zip_url,
        "zip_error": zip_error,
    }


@app.get("/files/{filename}")
def get_file(filename: str):
    video_match = re.fullmatch(
        r"minhoca_[0-9a-f-]+(?:_([A-Za-z0-9_-]+))?\.mp4",
        filename,
        flags=re.IGNORECASE,
    )
    zip_match = re.fullmatch(
        r"minhoca_lote_[0-9a-f-]+\.zip",
        filename,
        flags=re.IGNORECASE,
    )

    if video_match:
        media_type = "video/mp4"
        requested_name = video_match.group(1)
        download_filename = (
            f"{requested_name}.mp4"
            if requested_name
            else "video_minhoca.mp4"
        )
    elif zip_match:
        media_type = "application/zip"
        download_filename = "minhoca_lote.zip"
    else:
        raise HTTPException(
            status_code=400,
            detail="Nome de arquivo inválido.",
        )

    file_path = os.path.join(
        DOWNLOAD_DIR,
        filename,
    )

    if not os.path.exists(file_path):
        raise HTTPException(
            status_code=404,
            detail="Arquivo não encontrado ou expirado.",
        )

    return FileResponse(
        file_path,
        filename=download_filename,
        media_type=media_type,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.post("/api/create-pix")
def create_pix_payment(
    payload: SubscriptionRequest,
    db: Session = Depends(get_db),
):
    plan_type = payload.plan_type.strip().lower()
    email = normalize_email(payload.email)

    if plan_type not in PLAN_CONFIG:
        raise HTTPException(status_code=400, detail="Plano inválido.")

    if is_admin(email):
        raise HTTPException(
            status_code=400,
            detail="A conta administradora não precisa de pagamento.",
        )

    user = get_or_create_user(db, email)

    if subscription_is_active(user, datetime.utcnow()):
        raise HTTPException(
            status_code=409,
            detail=(
                "Você já possui um plano ativo. "
                "Aguarde o vencimento ou use a assinatura atual."
            ),
        )

    external_reference = f"minhoca-pix-{plan_type}-{uuid.uuid4().hex}"

    payment = mp_create_pix_payment(
        email=email,
        plan_type=plan_type,
        external_reference=external_reference,
    )

    transaction = models.Transaction(
        payment_id=str(payment["id"]),
        email=email,
        plan_type=plan_type,
        status=payment.get("status", "pending"),
        amount=PLAN_CONFIG[plan_type]["amount"],
        subscription_id=None,
    )

    db.add(transaction)
    db.commit()

    transaction_data = (
        payment.get("point_of_interaction", {})
        .get("transaction_data", {})
    )

    return {
        "status": payment.get("status", "pending"),
        "payment_id": str(payment["id"]),
        "plan": plan_type,
        "amount": PLAN_CONFIG[plan_type]["amount"],
        "qr_code": transaction_data.get("qr_code"),
        "qr_code_base64": transaction_data.get("qr_code_base64"),
        "date_of_expiration": payment.get("date_of_expiration"),
    }


@app.get("/api/pix-status")
def pix_status(
    payment_id: str,
    email: str,
    db: Session = Depends(get_db),
):
    clean_email = normalize_email(email)
    transaction = (
        db.query(models.Transaction)
        .filter(models.Transaction.payment_id == str(payment_id))
        .first()
    )

    if not transaction or transaction.email != clean_email or transaction.subscription_id:
        raise HTTPException(status_code=404, detail="Pagamento Pix não encontrado.")

    if transaction.processed_at:
        return {
            "status": transaction.status,
            "approved": transaction.status == "approved",
        }

    try:
        payment = mp_get_payment(str(payment_id))
    except Exception:
        return {
            "status": transaction.status,
            "approved": False,
        }

    payment_status = payment.get("status") or transaction.status

    if payment_status == "approved":
        activated = activate_user_from_pix_payment(
            db,
            payment,
            transaction,
        )

        if activated:
            transaction.status = "approved"
            transaction.amount = payment.get("transaction_amount")
            transaction.approved_at = datetime.utcnow()
            transaction.processed_at = datetime.utcnow()
            db.commit()

    elif payment_status in {
        "rejected",
        "cancelled",
        "refunded",
        "charged_back",
    }:
        transaction.status = payment_status
        transaction.processed_at = datetime.utcnow()
        db.commit()

    return {
        "status": transaction.status,
        "approved": transaction.status == "approved",
    }


@app.post("/api/create-subscription")
def create_subscription(
    payload: SubscriptionRequest,
    db: Session = Depends(get_db),
):
    plan_type = payload.plan_type.strip().lower()
    email = normalize_email(payload.email)

    if plan_type not in PLAN_CONFIG:
        raise HTTPException(
            status_code=400,
            detail="Plano inválido.",
        )

    if is_admin(email):
        raise HTTPException(
            status_code=400,
            detail="A conta administradora não precisa de assinatura.",
        )

    user = get_or_create_user(db, email)

    now = datetime.utcnow()

    if subscription_is_active(user, now):
        return {
            "status": "already_active",
            "plan": user.plan_type,
            "expires_at": (
                user.expires_at.isoformat()
                if user.expires_at
                else None
            ),
        }

    # Se já existe uma assinatura pendente, reutiliza o checkout.
    if (
        user.subscription_id
        and user.status == "pending"
    ):
        try:
            subscription = mp_get_subscription(
                user.subscription_id
            )

            if subscription.get("status") == "pending":
                return {
                    "status": "pending",
                    "subscription_id": user.subscription_id,
                    "checkout_url": normalize_subscription_checkout_url(subscription.get("init_point")),
                    "plan": user.plan_type,
                }
        except Exception:
            pass

    external_reference = (
        f"minhoca-{plan_type}-{uuid.uuid4().hex}"
    )

    subscription = mp_create_subscription(
        email=email,
        plan_type=plan_type,
        external_reference=external_reference,
    )

    user.plan_type = plan_type
    user.status = "pending"
    user.subscription_id = str(subscription["id"])
    user.next_billing_at = parse_mp_datetime(
        subscription.get("next_payment_date")
    )

    transaction = models.Transaction(
        payment_id=f"subscription:{subscription['id']}",
        email=email,
        plan_type=plan_type,
        status="pending",
        amount=PLAN_CONFIG[plan_type]["amount"],
        subscription_id=str(subscription["id"]),
    )

    db.add(transaction)
    db.commit()

    return {
        "status": "pending",
        "subscription_id": str(subscription["id"]),
        "checkout_url": normalize_subscription_checkout_url(subscription.get("init_point")),
        "plan": plan_type,
        "amount": PLAN_CONFIG[plan_type]["amount"],
    }


@app.post("/api/cancel-subscription")
def cancel_subscription(
    payload: SubscriptionRequest,
    db: Session = Depends(get_db),
):
    email = normalize_email(payload.email)
    user = get_user(db, email)

    if not user or not user.subscription_id:
        raise HTTPException(
            status_code=404,
            detail="Nenhuma assinatura encontrada para este e-mail.",
        )

    if user.status == "cancelled":
        return {
            "status": "already_cancelled",
            "expires_at": (
                user.expires_at.isoformat()
                if user.expires_at
                else None
            ),
        }

    mp_cancel_subscription(user.subscription_id)

    user.status = "cancelled"
    user.cancelled_at = datetime.utcnow()

    # NÃO removemos expires_at.
    # O cliente continua tendo acesso até o final do período pago.
    db.commit()

    return {
        "status": "cancelled",
        "expires_at": (
            user.expires_at.isoformat()
            if user.expires_at
            else None
        ),
    }


@app.post("/api/webhook")
async def mercado_pago_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Recebe e valida Webhooks do Mercado Pago.

    Eventos relevantes para o nosso modelo de assinatura sem
    plano associado:

    - payment
    - subscription_preapproval
    - subscription_authorized_payment

    O Mercado Pago recomenda o tópico de pagamentos junto com
    os eventos de assinatura. A confirmação real do pagamento
    sempre é feita consultando a API do Mercado Pago.
    """

    data_id = request.query_params.get("data.id")

    if not data_id:
        try:
            body_preview = await request.json()
            data_id = str(
                body_preview.get("data", {}).get("id", "")
            )
        except Exception:
            data_id = ""

    if not verify_webhook_signature(request, data_id):
        raise HTTPException(
            status_code=401,
            detail="Webhook não autenticado.",
        )

    try:
        data = await request.json()
    except Exception:
        return {"status": "ok"}

    event_type = data.get("type")
    event_id = str(data.get("id") or data_id or "")

    # ---------------------------------------------------------
    # PAGAMENTO
    # ---------------------------------------------------------
    if event_type == "payment" and data_id:
        try:
            payment = mp_get_payment(str(data_id))
        except Exception:
            # Erro transitório na API do Mercado Pago:
            # respondemos 500 para permitir nova tentativa.
            raise HTTPException(
                status_code=500,
                detail="Não foi possível consultar o pagamento no Mercado Pago.",
            )

        payment_id = str(payment.get("id") or data_id)
        payment_status = payment.get("status")

        transaction = (
            db.query(models.Transaction)
            .filter(models.Transaction.payment_id == payment_id)
            .first()
        )

        if transaction and transaction.processed_at:
            return {
                "status": "ok",
                "duplicate": True,
                "event_id": event_id,
            }

        if payment_status == "approved":
            activated = False

            if transaction and not transaction.subscription_id:
                # Pagamento único via Pix. A transação já contém o plano,
                # e-mail e valor que esperamos receber.
                activated = activate_user_from_pix_payment(
                    db,
                    payment,
                    transaction,
                )

                if activated:
                    transaction.status = "approved"
                    transaction.amount = payment.get(
                        "transaction_amount"
                    )
                    transaction.approved_at = datetime.utcnow()
                    transaction.processed_at = datetime.utcnow()
                    db.commit()

            else:
                # Pagamento associado a uma assinatura recorrente.
                activated = activate_user_from_payment(
                    db,
                    payment,
                )

                if activated:
                    if transaction:
                        transaction.status = "approved"
                        transaction.amount = payment.get(
                            "transaction_amount"
                        )
                        transaction.approved_at = datetime.utcnow()
                        transaction.processed_at = datetime.utcnow()
                        db.commit()
                    else:
                        email = (
                            payment.get("payer", {}).get("email")
                            or ""
                        )

                        if email:
                            email = normalize_email(email)
                            user = get_user(db, email)

                            if user and user.subscription_id:
                                new_transaction = models.Transaction(
                                    payment_id=payment_id,
                                    email=email,
                                    plan_type=user.plan_type,
                                    status="approved",
                                    amount=payment.get(
                                        "transaction_amount"
                                    ),
                                    subscription_id=user.subscription_id,
                                    approved_at=datetime.utcnow(),
                                    processed_at=datetime.utcnow(),
                                )
                                db.add(new_transaction)
                                db.commit()

        elif transaction and payment_status in {
            "rejected",
            "cancelled",
            "refunded",
            "charged_back",
        }:
            transaction.status = payment_status
            transaction.processed_at = datetime.utcnow()
            db.commit()

    # ---------------------------------------------------------
    # ASSINATURA
    # ---------------------------------------------------------
    elif event_type == "subscription_preapproval" and data_id:
        try:
            subscription = mp_get_subscription(str(data_id))
        except Exception:
            raise HTTPException(
                status_code=500,
                detail="Não foi possível consultar a assinatura no Mercado Pago.",
            )

        subscription_id = str(
            subscription.get("id") or data_id
        )
        email = subscription.get("payer_email")

        if email:
            try:
                email = normalize_email(email)
            except HTTPException:
                email = None

        user = None

        if email:
            user = get_user(db, email)

        if not user:
            user = (
                db.query(models.User)
                .filter(
                    models.User.subscription_id
                    == subscription_id
                )
                .first()
            )

        if user:
            status = subscription.get("status")

            user.subscription_id = subscription_id
            user.next_billing_at = parse_mp_datetime(
                subscription.get("next_payment_date")
            )

            if status in {"cancelled", "canceled"}:
                user.status = "cancelled"

                if not user.cancelled_at:
                    user.cancelled_at = datetime.utcnow()

            elif status in {"authorized", "active"}:
                # A assinatura pode estar autorizada antes de o primeiro
                # pagamento ser confirmado. Não liberamos acesso novo
                # aqui; o pagamento aprovado é quem confirma o período.
                if subscription_is_active(user, datetime.utcnow()):
                    user.status = "active"
                else:
                    user.status = "pending"

            elif status == "pending":
                if user.status != "active":
                    user.status = "pending"

            db.commit()

    # ---------------------------------------------------------
    # COBRANÇA RECORRENTE DA ASSINATURA
    # ---------------------------------------------------------
    elif (
        event_type == "subscription_authorized_payment"
        and data_id
    ):
        try:
            invoice = mp_get_authorized_payment(
                str(data_id)
            )
        except Exception:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Não foi possível consultar a cobrança "
                    "recorrente no Mercado Pago."
                ),
            )

        subscription_id = invoice.get("preapproval_id")
        payment_info = invoice.get("payment") or {}
        payment_id = payment_info.get("id")

        # A fatura pode existir antes de possuir um pagamento
        # final associado. Nesse caso, não há nada para ativar.
        if not payment_id:
            return {
                "status": "ok",
                "event_id": event_id,
                "invoice_id": str(invoice.get("id") or data_id),
                "message": "Fatura recebida sem pagamento associado.",
            }

        payment_id = str(payment_id)

        transaction = (
            db.query(models.Transaction)
            .filter(models.Transaction.payment_id == payment_id)
            .first()
        )

        if transaction and transaction.processed_at:
            return {
                "status": "ok",
                "duplicate": True,
                "event_id": event_id,
            }

        try:
            payment = mp_get_payment(payment_id)
        except Exception:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Não foi possível consultar o pagamento "
                    "da cobrança recorrente."
                ),
            )

        payment_status = payment.get("status")

        if payment_status == "approved":
            activated = activate_user_from_payment(
                db,
                payment,
                expected_subscription_id=(
                    str(subscription_id)
                    if subscription_id
                    else None
                ),
            )

            if activated:
                email = (
                    payment.get("payer", {}).get("email")
                    or ""
                )

                if email:
                    email = normalize_email(email)
                    user = get_user(db, email)

                    if user:
                        if transaction:
                            transaction.status = "approved"
                            transaction.email = email
                            transaction.plan_type = user.plan_type
                            transaction.amount = payment.get(
                                "transaction_amount"
                            )
                            transaction.subscription_id = (
                                user.subscription_id
                            )
                            transaction.approved_at = (
                                datetime.utcnow()
                            )
                            transaction.processed_at = (
                                datetime.utcnow()
                            )
                        else:
                            new_transaction = models.Transaction(
                                payment_id=payment_id,
                                email=email,
                                plan_type=user.plan_type,
                                status="approved",
                                amount=payment.get(
                                    "transaction_amount"
                                ),
                                subscription_id=user.subscription_id,
                                approved_at=datetime.utcnow(),
                                processed_at=datetime.utcnow(),
                            )
                            db.add(new_transaction)

                        db.commit()

        elif transaction and payment_status in {
            "rejected",
            "cancelled",
            "refunded",
            "charged_back",
        }:
            transaction.status = payment_status
            transaction.processed_at = datetime.utcnow()
            db.commit()

    return {
        "status": "ok",
        "event_id": event_id,
    }


@app.get("/api/check-email")
def check_email_status(
    request: Request,
    email: str,
    db: Session = Depends(get_db),
):
    clean_email = normalize_email(email)
    now = datetime.utcnow()
    client_ip = request.client.host if request.client else "unknown"
    today_str = now.strftime("%Y-%m-%d")

    if is_admin(clean_email):
        return {
            "email": clean_email,
            "is_vip": True,
            "plan": "admin",
            "plan_name": "VIP Batch",
            "downloads_today": None,
            "daily_limit": None,
            "expires_at": None,
            "subscription_status": "active",
        }

    user = get_user(db, clean_email)
    plan_type = current_plan_for_user(user, now)

    if plan_type in PLAN_CONFIG:
        usage = get_plan_usage(
            db,
            user.id,
            today_str,
        )

        return {
            "email": clean_email,
            "is_vip": plan_type == "vip",
            "plan": plan_type,
            "plan_name": PLAN_CONFIG[plan_type]["name"],
            "downloads_today": usage.downloads_today,
            "daily_limit": PLAN_CONFIG[plan_type]["limit"],
            "expires_at": (
                user.expires_at.isoformat()
                if user.expires_at
                else None
            ),
            "subscription_status": user.status,
            "payment_type": "cartao" if user.subscription_id else "pix",
            "cancelled_at": (
                user.cancelled_at.isoformat()
                if user.cancelled_at
                else None
            ),
        }

    usage = get_free_usage(
        db,
        client_ip,
        today_str,
    )

    return {
        "email": clean_email,
        "is_vip": False,
        "plan": "free",
        "plan_name": "Gratuito",
        "downloads_today": usage.downloads_today,
        "daily_limit": FREE_LIMIT,
        "expires_at": None,
        "subscription_status": "active",
    }


@app.get("/api/subscription")
def get_subscription(
    email: str,
    db: Session = Depends(get_db),
):
    clean_email = normalize_email(email)
    user = get_user(db, clean_email)
    now = datetime.utcnow()

    if not user:
        return {
            "has_subscription": False,
            "has_paid_plan": False,
            "plan": "free",
        }

    plan_type = current_plan_for_user(user, now)

    if plan_type not in PLAN_CONFIG:
        return {
            "has_subscription": False,
            "has_paid_plan": False,
            "plan": "free",
        }

    is_card = bool(user.subscription_id)

    return {
        "has_subscription": is_card,
        "has_paid_plan": True,
        "payment_type": "cartao" if is_card else "pix",
        "plan": plan_type,
        "status": user.status,
        "subscription_id": user.subscription_id,
        "started_at": (
            user.started_at.isoformat()
            if user.started_at
            else None
        ),
        "expires_at": (
            user.expires_at.isoformat()
            if user.expires_at
            else None
        ),
        "next_billing_at": (
            user.next_billing_at.isoformat()
            if user.next_billing_at
            else None
        ),
        "cancelled_at": (
            user.cancelled_at.isoformat()
            if user.cancelled_at
            else None
        ),
    }
