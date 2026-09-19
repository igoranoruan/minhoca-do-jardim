import os
import re
import uuid
import hmac
import hashlib
import json
import subprocess
import unicodedata
import zipfile
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
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

FREE_LIMIT = 5
SAO_PAULO_TZ = ZoneInfo("America/Sao_Paulo")
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


def validate_video_file(file_path: str):
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
    Prepara o MP4 removendo metadados sem reencodar quando possível.

    O remux é a primeira opção porque é muito mais leve em CPU e memória.
    Se o arquivo não puder ser remuxado/validado, usamos uma transcodificação
    H.264/AAC com apenas uma thread para caber no limite de memória do Render.
    """
    remux_command = [
        "ffmpeg",
        "-y",
        "-threads",
        "1",
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

    try:
        subprocess.run(
            remux_command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        validate_video_file(output_path)
        return
    except Exception:
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass

    transcode_command = [
        "ffmpeg",
        "-y",
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-filter_complex_threads",
        "1",
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
        "-profile:v",
        "main",
        "-threads",
        "1",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "44100",
        "-movflags",
        "+faststart",
        output_path,
    ]

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
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
        error_text = (exc.stderr or b"").decode("utf-8", errors="ignore")
        raise RuntimeError(
            "Não foi possível preparar o vídeo em MP4 compatível."
            + (f" Detalhe: {error_text[-600:]}" if error_text else "")
        ) from exc
    except subprocess.TimeoutExpired as exc:
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
        raise RuntimeError("O processamento do vídeo excedeu o tempo limite.") from exc
    except Exception as exc:
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
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


def get_sao_paulo_now() -> datetime:
    return datetime.now(SAO_PAULO_TZ)


def get_free_period_start_str() -> str:
    local_date = get_sao_paulo_now().date()
    monday = local_date - timedelta(days=local_date.weekday())
    return monday.isoformat()


def get_free_usage(db: Session, client_ip: str, period_start_str: str):
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

    try:
        stored_period = date.fromisoformat(usage.last_download_date)
    except (TypeError, ValueError):
        stored_period = None

    current_period = date.fromisoformat(period_start_str)

    if stored_period is None or stored_period < current_period:
        usage.downloads_today = 0
        usage.reserved_today = 0
        usage.last_download_date = period_start_str
        db.commit()
        db.refresh(usage)
    elif stored_period != current_period:
        usage.last_download_date = period_start_str
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
                detail=f"Limite semanal do plano Free atingido ({FREE_LIMIT}/{FREE_LIMIT}).",
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
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                )
            }
            response = requests.get(
                url,
                headers=headers,
                allow_redirects=True,
                stream=True,
                timeout=10,
            )
            try:
                if response.url:
                    return response.url
            finally:
                response.close()
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

        validate_video_file(candidate_path)
        return candidate_path

    if is_tiktok:
        try:
            if try_tikwm_download(clean_url, raw_path):
                return accept_candidate(raw_path)
        except Exception as exc:
            download_errors.append(f"TikTok/TikWM: {exc}")
            remove_candidate()

    source_attempts = []

    if is_youtube or is_instagram or is_pinterest:
        source_attempts.append(("yt-dlp", None))
        source_attempts.append(("cobalt", None))
    else:
        source_attempts.append(("cobalt", None))
        source_attempts.append(("yt-dlp", None))

    base_opts = {
        "outtmpl": raw_path,
        # Prefere um único arquivo MP4 para evitar o merge de vídeo+áudio
        # quando a plataforma já oferece uma mídia pronta. Isso reduz RAM.
        "format": "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
        "merge_output_format": "mp4",
        "ffmpeg_location": "/usr/bin/ffmpeg",
        "quiet": True,
        "ignoreerrors": False,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
    }

    # Clientes otimizados para contornar restrições do YouTube sem travar se o PO Token local falhar
    ytdlp_clients = [["mweb"], ["android"], ["ios"], ["web"]] if is_youtube else [None]

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
                extractor_args = {}

                if player_clients:
                    extractor_args["youtube"] = {
                        "player_client": player_clients,
                    }

                    # O start.sh só inicia o FastAPI depois que o BGUTIL responde.
                    # Portanto, não precisamos fazer um health-check aqui; o teste
                    # anterior consultava / em vez de /ping e podia deixar o provider
                    # fora do extractor_args mesmo estando operacional.
                    if is_youtube and "mweb" in player_clients:
                        extractor_args["youtubepot-bgutilhttp"] = {
                            "base_url": ["http://127.0.0.1:4416"],
                        }

                if extractor_args:
                    ydl_opts["extractor_args"] = extractor_args

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

    raise RuntimeError(
        f"Não foi possível baixar este vídeo: {detail}"
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


def normalize_checkout_url(init_point: str | None) -> str | None:
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


def mp_create_checkout_preference(
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
        "items": [
            {
                "id": f"minhoca-{plan_type}",
                "title": f"Minhoca de Jardim - Plano {plan['name']}",
                "description": plan["label"],
                "quantity": 1,
                "currency_id": "BRL",
                "unit_price": plan["amount"],
            }
        ],
        "payer": {
            "email": email,
        },
        "back_urls": {
            "success": get_public_base_url(),
            "failure": get_public_base_url(),
            "pending": get_public_base_url(),
        },
        "auto_return": "approved",
        "notification_url": f"{get_public_base_url()}/api/webhook",
        "external_reference": external_reference,
        "payment_methods": {
            "installments": 12,
            "excluded_payment_types": [
                {"id": "ticket"},
            ],
        },
    }

    headers = mp_headers(token)
    headers["X-Idempotency-Key"] = str(uuid.uuid4())

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

    checkout_url = normalize_checkout_url(
        data.get("init_point") or data.get("sandbox_init_point")
    )

    if not data.get("id") or not checkout_url:
        raise HTTPException(
            status_code=502,
            detail="Mercado Pago não retornou o checkout.",
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


def mp_create_pix_payment(
    email: str,
    plan_type: str,
    external_reference: str,
):
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
        return now + timedelta(days=30)

    return None


def activate_user_from_one_time_payment(
    db: Session,
    payment: dict,
    transaction: models.Transaction,
):
    if not transaction or transaction.subscription_id:
        return False

    if transaction.status == "approved" and transaction.processed_at:
        return True

    email = normalize_email(transaction.email or "")

    payment_amount = payment.get("transaction_amount")
    expected_amount = transaction.amount

    if payment_amount is None or expected_amount is None:
        return False

    if abs(float(payment_amount) - float(expected_amount)) > 0.01:
        return False

    external_reference = str(payment.get("external_reference") or "").strip()
    if external_reference and transaction.payment_id != external_reference:
        if not str(transaction.payment_id).isdigit():
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
        if transaction.plan_type == "semanal"
        else timedelta(days=30)
    )

    user.plan_type = transaction.plan_type
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


def revoke_access_for_reversed_payment(
    db: Session,
    transaction: models.Transaction,
):
    if not transaction or transaction.status != "approved":
        return False

    user = get_user(db, normalize_email(transaction.email or ""))
    if not user:
        return False

    if user.plan_type != transaction.plan_type:
        return False

    if not transaction.approved_at:
        return False

    duration = (
        timedelta(days=7)
        if transaction.plan_type == "semanal"
        else timedelta(days=30)
    )
    transaction_period_end = transaction.approved_at + duration

    if user.expires_at and user.expires_at > transaction_period_end:
        return False

    now = datetime.utcnow()
    user.status = "expired"
    user.expires_at = now
    user.vip_until = now
    user.next_billing_at = None
    user.cancelled_at = None

    db.commit()
    return True


def activate_user_from_pix_payment(
    db: Session,
    payment: dict,
    transaction: models.Transaction,
):
    return activate_user_from_one_time_payment(
        db,
        payment,
        transaction,
    )


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

    quota_period_str = (
        get_free_period_start_str()
        if plan_type == "free"
        else today_str
    )

    quota_usage = reserve_quota(
        db=db,
        plan_type=plan_type,
        client_ip=client_ip,
        user=user,
        today_str=quota_period_str,
    )

    try:
        download_url = process_one_video(
            video_url.strip(),
            quota_usage,
            plan_type,
            requested_name=filename,
        )

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
            "quota_period": "week" if plan_type == "free" else "day",
            "quota_label": "na semana" if plan_type == "free" else "hoje",
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


@app.post("/api/create-checkout")
def create_checkout(
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

    external_reference = (
        f"minhoca-checkout-{plan_type}-{uuid.uuid4().hex}"
    )

    preference = mp_create_checkout_preference(
        email=email,
        plan_type=plan_type,
        external_reference=external_reference,
    )

    transaction = models.Transaction(
        payment_id=external_reference,
        email=email,
        plan_type=plan_type,
        status="pending",
        amount=PLAN_CONFIG[plan_type]["amount"],
        subscription_id=None,
    )

    db.add(transaction)
    db.commit()

    return {
        "status": "pending",
        "preference_id": str(preference["id"]),
        "checkout_url": normalize_checkout_url(
            preference.get("init_point") or preference.get("sandbox_init_point")
        ),
        "plan": plan_type,
        "amount": PLAN_CONFIG[plan_type]["amount"],
    }


@app.post("/api/create-subscription")
def create_subscription_compatibility(
    payload: SubscriptionRequest,
    db: Session = Depends(get_db),
):
    return create_checkout(payload, db)


@app.post("/api/cancel-subscription")
def cancel_subscription(
    payload: SubscriptionRequest,
    db: Session = Depends(get_db),
):
    email = normalize_email(payload.email)
    user = get_user(db, email)

    if not user or not subscription_is_active(user, datetime.utcnow()):
        raise HTTPException(
            status_code=404,
            detail="Nenhum plano ativo encontrado para este e-mail.",
        )

    raise HTTPException(
        status_code=409,
        detail=(
            "Os planos atuais são pagamentos únicos e não possuem "
            "renovação automática para cancelar."
        ),
    )


@app.post("/api/webhook")
async def mercado_pago_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    data_id = request.query_params.get("data.id")

    try:
        data = await request.json()
    except Exception:
        data = {}

    if not data_id:
        data_id = str(
            data.get("data", {}).get("id", "")
        )

    if not verify_webhook_signature(request, data_id):
        raise HTTPException(
            status_code=401,
            detail="Webhook não autenticado.",
        )

    event_type = data.get("type")
    event_id = str(data.get("id") or data_id or "")

    if event_type != "payment" or not data_id:
        return {
            "status": "ok",
            "event_id": event_id,
            "ignored": True,
        }

    try:
        payment = mp_get_payment(str(data_id))
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="Não foi possível consultar o pagamento no Mercado Pago.",
        )

    payment_id = str(payment.get("id") or data_id)
    payment_status = payment.get("status")
    external_reference = str(
        payment.get("external_reference") or ""
    ).strip()

    transaction = (
        db.query(models.Transaction)
        .filter(models.Transaction.payment_id == payment_id)
        .first()
    )

    if not transaction and external_reference:
        transaction = (
            db.query(models.Transaction)
            .filter(
                models.Transaction.payment_id == external_reference,
                models.Transaction.status == "pending",
            )
            .first()
        )

    if not transaction:
        return {
            "status": "ok",
            "event_id": event_id,
            "ignored": True,
            "reason": "transaction_not_found",
        }

    if payment_status == "approved":
        if transaction.processed_at and transaction.status == "approved":
            return {
                "status": "ok",
                "duplicate": True,
                "event_id": event_id,
            }

        activated = activate_user_from_one_time_payment(
            db,
            payment,
            transaction,
        )

        if activated:
            transaction.payment_id = payment_id
            transaction.status = "approved"
            transaction.amount = payment.get("transaction_amount")
            transaction.approved_at = (
                transaction.approved_at or datetime.utcnow()
            )
            transaction.processed_at = datetime.utcnow()
            db.commit()

    elif payment_status in {
        "rejected",
        "cancelled",
        "refunded",
        "charged_back",
    }:
        if transaction.processed_at and transaction.status == payment_status:
            return {
                "status": "ok",
                "duplicate": True,
                "event_id": event_id,
            }

        was_approved = transaction.status == "approved"
        transaction.status = payment_status
        transaction.processed_at = datetime.utcnow()
        db.commit()

        if was_approved and payment_status in {"refunded", "charged_back"}:
            revoke_access_for_reversed_payment(
                db,
                transaction,
            )

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
            "quota_period": "day",
            "quota_label": "hoje",
            "expires_at": (
                user.expires_at.isoformat()
                if user.expires_at
                else None
            ),
            "subscription_status": user.status,
            "payment_type": "pagamento_unico",
            "cancelled_at": (
                user.cancelled_at.isoformat()
                if user.cancelled_at
                else None
            ),
        }

    free_period_start = get_free_period_start_str()
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
        "downloads_today": usage.downloads_today,
        "daily_limit": FREE_LIMIT,
        "quota_period": "week",
        "quota_label": "na semana",
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

    return {
        "has_subscription": False,
        "has_paid_plan": True,
        "payment_type": "pagamento_unico",
        "plan": plan_type,
        "status": user.status,
        "subscription_id": None,
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
        "next_billing_at": None,
        "cancelled_at": None,
    }