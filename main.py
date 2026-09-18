import os
import re
import uuid
import hmac
import hashlib
import json
import subprocess
import unicodedata
import zipfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import ipaddress
import socket
from urllib.parse import urlparse

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

FREE_LIMIT = 5
BATCH_MAX_ITEMS = 10
MAX_URL_LENGTH = 2048
MAX_DOWNLOAD_BYTES = 150 * 1024 * 1024
BATCH_MAX_TOTAL_BYTES = 500 * 1024 * 1024
MAX_FILENAME_LENGTH = 80
CLEAN_FILE_TTL_SECONDS = 3600
FREE_TIMEZONE = ZoneInfo("America/Sao_Paulo")
WEBHOOK_MAX_AGE_SECONDS = 300

# Servidor local do bgutil-ytdlp-pot-provider usado pelo yt-dlp no YouTube.
# O serviço é iniciado pelo start.sh no container de produção.
BGUTIL_POT_BASE_URL = os.getenv(
    "BGUTIL_POT_BASE_URL",
    "http://127.0.0.1:4416",
).strip().rstrip("/")

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


def _is_public_ip(hostname: str) -> bool:
    try:
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(
                hostname,
                None,
                type=socket.SOCK_STREAM,
            )
        }
    except (OSError, socket.gaierror):
        return False

    if not addresses:
        return False

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False

        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False

    return True


def is_safe_remote_download_url(value: str) -> bool:
    """Evita que URLs externas sejam usadas como SSRF."""
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
        "metadata.google.internal",
    }

    if hostname in blocked_hosts or hostname.endswith(".local"):
        return False

    try:
        ipaddress.ip_address(hostname)
        return _is_public_ip(hostname)
    except ValueError:
        return _is_public_ip(hostname)


def validate_video_file(file_path: str):
    """Valida estrutura e decodificação básica do vídeo antes de liberar o arquivo."""
    if not os.path.exists(file_path) or os.path.getsize(file_path) < 1024:
        raise RuntimeError("O arquivo processado ficou inválido ou vazio.")

    probe_command = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        file_path,
    ]

    try:
        result = subprocess.run(
            probe_command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        data = json.loads(result.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise RuntimeError("O arquivo final não pôde ser validado como vídeo.") from exc

    streams = data.get("streams") or []
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]

    if not video_streams:
        raise RuntimeError("O arquivo final não contém uma faixa de vídeo válida.")

    video = video_streams[0]

    try:
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
    except (TypeError, ValueError):
        width = height = 0

    if width < 2 or height < 2:
        raise RuntimeError("O arquivo final não contém uma imagem de vídeo válida.")

    duration_value = video.get("duration") or (data.get("format") or {}).get("duration")
    try:
        duration = float(duration_value) if duration_value is not None else 0.0
    except (TypeError, ValueError):
        duration = 0.0

    if duration <= 0:
        raise RuntimeError("O arquivo final não contém uma duração de vídeo válida.")

    # O ffprobe pode enxergar uma faixa de vídeo mesmo quando o arquivo
    # não consegue decodificar nenhum frame. Testamos um frame real antes
    # de considerar a origem válida.
    decode_command = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        file_path,
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-f",
        "null",
        "-",
    ]

    try:
        subprocess.run(
            decode_command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("O arquivo contém uma faixa de vídeo, mas não foi possível decodificar um frame.") from exc

def clean_metadata(input_path: str, output_path: str):
    """
    Gera uma nova versão MP4, remove metadados e valida a decodificação.
    Primeiro tenta remux sem reencodar; se a origem não for compatível,
    usa H.264/AAC como fallback.
    Não é uma garantia de evasão de sistemas de detecção das plataformas.
    """
    remux_command = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map_metadata",
        "-1",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        output_path,
    ]

    transcode_command = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map_metadata",
        "-1",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        output_path,
    ]

    errors = []

    try:
        subprocess.run(
            remux_command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=180,
        )
        try:
            validate_video_file(output_path)
            return
        except Exception as exc:
            errors.append(f"remux: {exc}")
            if os.path.exists(output_path):
                os.remove(output_path)
    except subprocess.CalledProcessError as exc:
        error_text = (exc.stderr or b"").decode("utf-8", errors="ignore")
        errors.append(f"remux: {error_text[-400:]}")
        if os.path.exists(output_path):
            os.remove(output_path)
    except subprocess.TimeoutExpired:
        errors.append("remux: tempo limite excedido.")
        if os.path.exists(output_path):
            os.remove(output_path)

    try:
        subprocess.run(
            transcode_command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=300,
        )
        validate_video_file(output_path)
    except subprocess.CalledProcessError as exc:
        error_text = (exc.stderr or b"").decode("utf-8", errors="ignore")
        errors.append(f"transcode: {error_text[-500:]}")
        if os.path.exists(output_path):
            os.remove(output_path)
        raise RuntimeError(
            "Não foi possível preparar o vídeo em MP4 compatível."
            + (f" Detalhe: {errors[-1]}" if errors else "")
        ) from exc
    except subprocess.TimeoutExpired as exc:
        if os.path.exists(output_path):
            os.remove(output_path)
        raise RuntimeError("O processamento do vídeo excedeu o tempo limite.") from exc
    except Exception as exc:
        if os.path.exists(output_path):
            os.remove(output_path)
        raise RuntimeError(
            "Não foi possível preparar o vídeo em MP4 compatível."
            + (f" Detalhe: {exc}" if str(exc) else "")
        ) from exc

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
    # Instâncias públicas do Cobalt não são uma dependência confiável para
    # produção. Só usamos Cobalt quando uma instância autorizada é fornecida
    # explicitamente via variável de ambiente.
    instances = [
        item.strip().rstrip("/")
        for item in os.getenv("COBALT_API_URLS", "").split(",")
        if item.strip()
    ]

    if not instances:
        return False

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


def get_free_usage(db: Session, client_ip: str, period_start_str: str):
    """Obtém o contador Free da semana atual por IP."""
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
            last_download_date=period_start_str,
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

    if not usage:
        raise RuntimeError("Não foi possível inicializar a cota gratuita.")

    if usage.last_download_date != period_start_str:
        usage.downloads_today = 0
        usage.reserved_today = 0
        usage.last_download_date = period_start_str
        db.commit()
        db.refresh(usage)

    return usage


def get_brazil_date_str(now: datetime | None = None) -> str:
    local_now = now or datetime.now(FREE_TIMEZONE)
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=FREE_TIMEZONE)
    else:
        local_now = local_now.astimezone(FREE_TIMEZONE)
    return local_now.strftime("%Y-%m-%d")


def get_free_period_start(now: datetime | None = None) -> str:
    """Retorna a segunda-feira da semana atual no horário de São Paulo."""
    local_now = now or datetime.now(FREE_TIMEZONE)
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=FREE_TIMEZONE)
    else:
        local_now = local_now.astimezone(FREE_TIMEZONE)
    monday = local_now - timedelta(days=local_now.weekday())
    return monday.strftime("%Y-%m-%d")


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
    period_start_str: str,
):
    """Reserva uma unidade somente quando ainda existe capacidade real."""
    if plan_type == "vip":
        return None

    if plan_type == "free":
        usage = get_free_usage(db, client_ip, period_start_str)
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
                detail="Limite semanal do plano Free atingido (5/5).",
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
            if response.url and is_valid_video_url(response.url):
                return response.url
    except Exception:
        pass

    return url


def download_video_source(video_url: str, raw_path: str):
    clean_url = resolve_short_url(video_url)
    download_errors = []

    hostname = (urlparse(clean_url).hostname or "").lower()
    is_youtube = "youtube.com" in hostname or "youtu.be" in hostname
    is_tiktok = "tiktok.com" in hostname
    is_instagram = "instagram.com" in hostname
    is_pinterest = "pinterest.com" in hostname

    def remove_candidate():
        if os.path.exists(raw_path):
            try:
                os.remove(raw_path)
            except OSError:
                pass

        prefix = os.path.basename(raw_path).rsplit(".", 1)[0]
        for filename in os.listdir(DOWNLOAD_DIR):
            if filename.startswith(prefix):
                candidate = os.path.join(DOWNLOAD_DIR, filename)
                if candidate != raw_path:
                    try:
                        os.remove(candidate)
                    except OSError:
                        pass

    def accept_candidate(candidate_path: str):
        if not os.path.exists(candidate_path) or os.path.getsize(candidate_path) < 1024:
            raise RuntimeError("O download retornou um arquivo inválido ou vazio.")

        if os.path.getsize(candidate_path) > MAX_DOWNLOAD_BYTES:
            raise RuntimeError("O vídeo excede o tamanho máximo permitido.")

        # A validação acontece em cada fonte, e não somente no final.
        # Assim, um arquivo quebrado retornado pelo Cobalt não impede que
        # o yt-dlp seja tentado como próximo fallback.
        validate_video_file(candidate_path)
        return candidate_path

    # TikTok continua usando o caminho que já funciona em produção.
    if is_tiktok:
        try:
            if try_tikwm_download(clean_url, raw_path):
                return accept_candidate(raw_path)
        except Exception as exc:
            download_errors.append(f"TikTok/TikWM: {exc}")
            remove_candidate()

    # Para Instagram/Pinterest/YouTube, tentamos primeiro o yt-dlp.
    # O Cobalt fica como fallback. Isso evita aceitar um arquivo Cobalt
    # inválido e encerrar o processamento antes de testar outra origem.
    source_attempts = []

    if is_youtube or is_instagram or is_pinterest:
        source_attempts.append(("yt-dlp", None))
        source_attempts.append(("cobalt", None))
    else:
        source_attempts.append(("cobalt", None))
        source_attempts.append(("yt-dlp", None))

    base_opts = {
        "outtmpl": raw_path,
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "quiet": True,
        "ignoreerrors": False,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 2,
        "remote_components": {"ejs:npm"},
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
    }

    deno_path = "/usr/local/bin/deno"
    if os.path.exists(deno_path):
        base_opts["js_runtimes"] = {"deno": {"path": deno_path}}

    # YouTube recebe primeiro o cliente mweb, que é o cliente recomendado
    # atualmente quando um PO Token Provider está configurado. O provider
    # bgutil é instalado no container e atende localmente em 127.0.0.1:4416.
    # web_embedded permanece como fallback para vídeos que aceitam esse cliente.
    ytdlp_clients = [None]
    if is_youtube:
        ytdlp_clients = [["mweb"], ["web_embedded"]]

    for source_type, _ in source_attempts:
        if source_type == "cobalt":
            remove_candidate()
            try:
                if try_cobalt_fallback(clean_url, raw_path):
                    return accept_candidate(raw_path)
                download_errors.append("Cobalt: não retornou um arquivo de vídeo.")
            except Exception as exc:
                download_errors.append(f"Cobalt: {exc}")
            continue

        for player_clients in ytdlp_clients:
            remove_candidate()

            try:
                ydl_opts = dict(base_opts)

                if player_clients:
                    ydl_opts["extractor_args"] = {
                        "youtube": {
                            "player_client": player_clients,
                        }
                    }

                    # O plugin bgutil-ytdlp-pot-provider usa o servidor HTTP
                    # local para gerar automaticamente o PO Token necessário
                    # pelo cliente mweb. O endereço permanece em loopback e
                    # não é exposto publicamente pelo servidor web.
                    if is_youtube and player_clients == ["mweb"]:
                        ydl_opts["extractor_args"]["youtubepot-bgutilhttp"] = {
                            "base_url": [BGUTIL_POT_BASE_URL],
                        }

                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([clean_url])

                actual_raw_path = raw_path

                if not os.path.exists(actual_raw_path):
                    prefix = os.path.basename(raw_path).rsplit(".", 1)[0]
                    for filename in os.listdir(DOWNLOAD_DIR):
                        if filename.startswith(prefix):
                            actual_raw_path = os.path.join(
                                DOWNLOAD_DIR,
                                filename,
                            )
                            break

                if os.path.exists(actual_raw_path):
                    return accept_candidate(actual_raw_path)

                download_errors.append(
                    f"yt-dlp{'/' + '/'.join(player_clients) if player_clients else ''}: "
                    "nenhum arquivo foi gerado."
                )
            except Exception as exc:
                client_label = (
                    f"/{'/'.join(player_clients)}"
                    if player_clients
                    else ""
                )
                download_errors.append(f"yt-dlp{client_label}: {exc}")

    if download_errors:
        detail = " | ".join(download_errors[-3:])
    else:
        detail = "erro desconhecido"

    print(f"[download] falha em {clean_url}: {detail}")
    raise RuntimeError(
        f"Não foi possível baixar este vídeo. Tentativas: {detail}"
    )

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

    # Em produção, webhook sem segredo configurado deve falhar fechado.
    # Em desenvolvimento, a exceção só pode ser habilitada explicitamente.
    if not secret:
        return os.getenv("ALLOW_UNSIGNED_WEBHOOKS", "0") == "1" and os.getenv(
            "ENVIRONMENT", "development"
        ).lower() != "production"

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

    try:
        timestamp_int = int(timestamp)
    except ValueError:
        return False

    now_epoch = int(datetime.utcnow().timestamp())
    if abs(now_epoch - timestamp_int) > WEBHOOK_MAX_AGE_SECONDS:
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

    return hmac.compare_digest(expected_hash, received_hash)


def mp_headers(token: str):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def mp_create_checkout_preference(
    email: str,
    plan_type: str,
    external_reference: str,
):
    """Cria um checkout único do Mercado Pago para cartão."""
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
    base_url = get_public_base_url()

    payload = {
        "items": [
            {
                "id": f"minhoca-{plan_type}",
                "title": f"Minhoca de Jardim - Plano {plan['name']}",
                "description": (
                    f"Acesso por {7 if plan_type == 'semanal' else 30} dias"
                ),
                "quantity": 1,
                "currency_id": "BRL",
                "unit_price": plan["amount"],
            }
        ],
        "payer": {"email": email},
        "external_reference": external_reference,
        "notification_url": f"{base_url}/api/webhook",
        "back_urls": {
            "success": base_url,
            "pending": base_url,
            "failure": base_url,
        },
        "auto_return": "approved",
    }

    headers = mp_headers(token)
    headers["X-Idempotency-Key"] = external_reference

    response = requests.post(
        "https://api.mercadopago.com/checkout/preferences",
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
            or "Mercado Pago recusou a criação do checkout."
        )
        raise HTTPException(status_code=502, detail=message)

    if not data.get("id") or not data.get("init_point"):
        raise HTTPException(
            status_code=502,
            detail="Mercado Pago não retornou a URL do checkout.",
        )

    return data


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
        "payer": {"email": email},
    }

    headers = mp_headers(token)
    headers["X-Idempotency-Key"] = external_reference

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


def revoke_user_access_for_transaction(db: Session, transaction: models.Transaction):
    """Revoga acesso somente se a transação afetada ainda for a ativa."""
    if not transaction or not transaction.email:
        return False

    user = get_user(db, normalize_email(transaction.email))
    if not user:
        return False

    if user.active_transaction_id != transaction.id:
        return False

    now = datetime.utcnow()
    user.status = "expired"
    user.expires_at = now
    user.vip_until = now
    user.next_billing_at = None
    user.cancelled_at = now
    db.commit()
    return True


def activate_user_from_payment(
    db: Session,
    payment: dict,
    transaction: models.Transaction,
):
    """Ativa um plano somente após confirmar o pagamento real no Mercado Pago."""
    if not transaction or transaction.plan_type not in PLAN_CONFIG:
        return False

    external_reference = str(payment.get("external_reference") or "").strip()
    if (
        transaction.external_reference
        and external_reference
        and transaction.external_reference != external_reference
    ):
        return False

    email = transaction.email or payment.get("payer", {}).get("email")
    if not email:
        return False

    email = normalize_email(email)
    plan_type = transaction.plan_type
    payment_amount = payment.get("transaction_amount")
    expected_amount = transaction.amount

    if payment_amount is None or expected_amount is None:
        return False

    if abs(float(payment_amount) - float(expected_amount)) > 0.01:
        return False

    if abs(float(payment_amount) - float(PLAN_CONFIG[plan_type]["amount"])) > 0.01:
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
    user.payment_type = transaction.payment_type or "cartao"
    user.active_transaction_id = transaction.id
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
    today_str = get_brazil_date_str()
    free_period_start = get_free_period_start()

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
        period_start_str=(free_period_start if plan_type == "free" else today_str),
    )

    try:
        download_url = process_one_video(
            video_url.strip(),
            quota_usage,
            plan_type,
            requested_name=filename,
        )

        # Só contabiliza depois que o arquivo final foi gerado.
        downloads_count = record_successful_usage(
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
            "downloads_today": downloads_count if plan_type != "free" else None,
            "daily_limit": quota_for_plan(plan_type) if plan_type != "free" else None,
            "downloads_week": downloads_count if plan_type == "free" else None,
            "weekly_limit": FREE_LIMIT if plan_type == "free" else None,
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
                "Aguarde a expiração antes de comprar novamente."
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
        external_reference=external_reference,
        email=email,
        plan_type=plan_type,
        payment_type="pix",
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


@app.post("/api/create-checkout")
def create_checkout(
    payload: SubscriptionRequest,
    db: Session = Depends(get_db),
):
    """Cria checkout único para pagamento com cartão."""
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
    now = datetime.utcnow()

    if subscription_is_active(user, now):
        return {
            "status": "already_active",
            "plan": user.plan_type,
            "expires_at": user.expires_at.isoformat()
            if user.expires_at
            else None,
        }

    external_reference = f"minhoca-card-{plan_type}-{uuid.uuid4().hex}"
    preference = mp_create_checkout_preference(
        email=email,
        plan_type=plan_type,
        external_reference=external_reference,
    )

    transaction = models.Transaction(
        payment_id=f"preference:{preference['id']}",
        external_reference=external_reference,
        email=email,
        plan_type=plan_type,
        payment_type="cartao",
        status="pending",
        amount=PLAN_CONFIG[plan_type]["amount"],
        subscription_id=None,
    )
    db.add(transaction)
    db.commit()

    return {
        "status": "pending",
        "checkout_url": preference["init_point"],
        "preference_id": str(preference["id"]),
        "plan": plan_type,
        "amount": PLAN_CONFIG[plan_type]["amount"],
    }


# Compatibilidade com frontend antigo. Esta rota também é pagamento único.
@app.post("/api/create-subscription")
def create_subscription_legacy(
    payload: SubscriptionRequest,
    db: Session = Depends(get_db),
):
    return create_checkout(payload, db)


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

    if (
        not transaction
        or transaction.email != clean_email
        or transaction.payment_type != "pix"
    ):
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
        activated = activate_user_from_payment(db, payment, transaction)
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

        if payment_status in {"refunded", "charged_back"}:
            revoke_user_access_for_transaction(db, transaction)

    return {
        "status": transaction.status,
        "approved": transaction.status == "approved",
    }


@app.post("/api/webhook")
async def mercado_pago_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    """Recebe notificações do Mercado Pago e confirma pagamentos pela API."""
    data_id = request.query_params.get("data.id")

    try:
        data = await request.json()
    except Exception:
        data = {}

    if not data_id:
        data_id = str(data.get("data", {}).get("id", ""))

    if not verify_webhook_signature(request, data_id):
        raise HTTPException(status_code=401, detail="Webhook não autenticado.")

    event_type = data.get("type") or data.get("action") or ""
    event_id = str(data.get("id") or data_id or "")

    if event_type not in {"payment", "payment.created", "payment.updated"}:
        return {"status": "ok", "event_id": event_id, "ignored": True}

    if not data_id:
        return {"status": "ok", "event_id": event_id}

    try:
        payment = mp_get_payment(str(data_id))
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="Não foi possível consultar o pagamento no Mercado Pago.",
        )

    payment_id = str(payment.get("id") or data_id)
    external_reference = str(payment.get("external_reference") or "").strip()
    payment_status = payment.get("status")

    transaction = (
        db.query(models.Transaction)
        .filter(models.Transaction.payment_id == payment_id)
        .with_for_update()
        .first()
    )

    if not transaction and external_reference:
        transaction = (
            db.query(models.Transaction)
            .filter(models.Transaction.external_reference == external_reference)
            .with_for_update()
            .first()
        )

    if (
        transaction
        and transaction.processed_at
        and transaction.status == payment_status
    ):
        return {"status": "ok", "duplicate": True, "event_id": event_id}

    if not transaction:
        return {"status": "ok", "event_id": event_id, "ignored": True}

    if payment_status == "approved":
        if not activate_user_from_payment(db, payment, transaction):
            raise HTTPException(
                status_code=500,
                detail=(
                    "Pagamento aprovado, mas a transação não passou "
                    "na validação do plano."
                ),
            )

        transaction.payment_id = payment_id
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
        transaction.payment_id = payment_id
        transaction.status = payment_status
        transaction.processed_at = datetime.utcnow()
        db.commit()

        if payment_status in {"refunded", "charged_back"}:
            revoke_user_access_for_transaction(db, transaction)
    else:
        transaction.status = payment_status or transaction.status
        db.commit()

    return {"status": "ok", "event_id": event_id}


@app.get("/api/check-email")
def check_email_status(
    request: Request,
    email: str,
    db: Session = Depends(get_db),
):
    clean_email = normalize_email(email)
    now = datetime.utcnow()
    client_ip = request.client.host if request.client else "unknown"
    today_str = get_brazil_date_str()
    free_period_start = get_free_period_start()

    if is_admin(clean_email):
        return {
            "email": clean_email,
            "is_vip": True,
            "plan": "admin",
            "plan_name": "VIP Batch",
            "downloads_today": None,
            "daily_limit": None,
            "downloads_week": None,
            "weekly_limit": None,
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
            "payment_type": user.payment_type or ("cartao" if user.subscription_id else "pix"),
            "cancelled_at": (
                user.cancelled_at.isoformat()
                if user.cancelled_at
                else None
            ),
        }

    usage = get_free_usage(
        db,
        client_ip,
        free_period_start,
    )

    return {
        "email": clean_email,
        "is_vip": False,
        "plan": "free",
        "plan_name": "Gratuito",
        "downloads_today": None,
        "daily_limit": None,
        "downloads_week": usage.downloads_today,
        "weekly_limit": FREE_LIMIT,
        "period_label": "esta semana",
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
        return {"has_subscription": False, "has_paid_plan": False, "plan": "free"}

    plan_type = current_plan_for_user(user, now)
    if plan_type not in PLAN_CONFIG:
        return {"has_subscription": False, "has_paid_plan": False, "plan": "free"}

    return {
        "has_subscription": False,
        "has_paid_plan": True,
        "payment_type": user.payment_type or ("cartao" if user.subscription_id else "pix"),
        "plan": plan_type,
        "status": user.status,
        "subscription_id": None,
        "started_at": user.started_at.isoformat() if user.started_at else None,
        "expires_at": user.expires_at.isoformat() if user.expires_at else None,
        "next_billing_at": None,
        "cancelled_at": None,
    }