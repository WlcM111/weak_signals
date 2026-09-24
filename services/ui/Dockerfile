# Образ ui: сборка SPA (Vite) → раздача статики nginx с прокси к orchestrator-api.
# Контекст сборки — корень репозитория (compose.yaml: context ".").
FROM node:22-alpine AS build
WORKDIR /app
COPY services/ui/package.json services/ui/package-lock.json* ./
# С lock-файлом (создаётся командой `npm install --package-lock-only`) сборка воспроизводима: npm ci.
RUN if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi
COPY services/ui/tsconfig*.json services/ui/vite.config.ts services/ui/index.html ./
COPY services/ui/public ./public
COPY services/ui/src ./src
RUN npm run build

FROM nginx:1.27-alpine AS runtime
RUN apk add --no-cache gettext wget
COPY --from=build /app/dist /usr/share/nginx/html
COPY services/ui/deploy/nginx.conf.template /etc/nginx/templates/nginx.conf.template
COPY services/ui/deploy/entrypoint.sh /docker-entrypoint-ws.sh
RUN chmod +x /docker-entrypoint-ws.sh
EXPOSE 8501
ENTRYPOINT ["/docker-entrypoint-ws.sh"]
