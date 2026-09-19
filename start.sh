#!/bin/sh

set -eu

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

cleanup() {
  kill "$POT_PID" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

echo "Aguardando BGUTIL ficar pronto..."

PROVIDER_READY=0

for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if ! kill -0 "$POT_PID" 2>/dev/null; then
    break
  fi

  if curl -fsS http://127.0.0.1:4416/ping >/dev/null 2>&1; then
    PROVIDER_READY=1
    break
  fi

  sleep 1
done

if [ "$PROVIDER_READY" -ne 1 ]; then
  echo "ERRO: o provider de PO Token não ficou disponível na porta 4416." >&2
  exit 1
fi

echo "BGUTIL PO TOKEN PROVIDER pronto."
echo "PO Token Provider iniciado (PID: $POT_PID)"

echo "========================================"
echo " Iniciando Minhoca de Jardim"
echo "========================================"

cd /app

exec uvicorn main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --proxy-headers \
  --forwarded-allow-ips="*"