#!/bin/sh
# Подставляет WS_API_BASE_URL и WS_API_KEY в конфигурацию nginx и запускает сервер.
set -eu
: "${WS_API_BASE_URL:=http://orchestrator-api:8080}"
: "${WS_API_KEY:=}"
export WS_API_BASE_URL WS_API_KEY
envsubst '${WS_API_BASE_URL} ${WS_API_KEY}' \
  < /etc/nginx/templates/nginx.conf.template > /etc/nginx/conf.d/default.conf
exec nginx -g "daemon off;"
