#!/bin/sh

set -e

echo "========================================"
echo " Iniciando BGUTIL PO TOKEN PROVIDER"
echo "========================================"

cd /opt/bgutil-ytdlp-pot-provider/server/node_modules

deno run \
    --allow-env \
    --allow-net \
    --allow-ffi=. \
    --allow-read=. \
    ../src/main.ts \
    --host 127.0.0.1 \
    --port 4416 &

POT_PID=$!

echo "PO Token Provider iniciado (PID: $POT_PID)"

cd /app

echo "========================================"
echo " Iniciando Minhoca de Jardim"
echo "========================================"

trap 'kill "$POT_PID" 2>/dev/null || true' EXIT INT TERM

exec uvicorn main:app --host 0.0.0.0 --port 8000