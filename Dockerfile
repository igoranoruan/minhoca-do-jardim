FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DENO_INSTALL=/usr/local

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        unzip \
        git \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://deno.land/install.sh | sh \
    && deno --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# O plugin Python e o servidor HTTP precisam usar a mesma versão do bgutil.
RUN git clone \
    --depth 1 \
    --branch 2.0.0 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
    /opt/bgutil-ytdlp-pot-provider \
    && cd /opt/bgutil-ytdlp-pot-provider/server \
    && deno install --allow-scripts=npm:canvas --frozen

COPY . .

RUN mkdir -p downloads \
    && chmod +x /app/start.sh

EXPOSE 8000

CMD ["/app/start.sh"]
