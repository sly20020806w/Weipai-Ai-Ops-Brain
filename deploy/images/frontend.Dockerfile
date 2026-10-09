# syntax=docker/dockerfile:1
FROM node:24-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 AS build
WORKDIR /app
ENV NODE_OPTIONS=--max-old-space-size=1536
RUN npm install --global pnpm@11.25.0
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN --mount=type=cache,target=/pnpm/store pnpm install --frozen-lockfile --store-dir /pnpm/store --network-concurrency=4 --child-concurrency=2
COPY frontend/src ./src
COPY frontend/index.html frontend/tsconfig*.json frontend/vite.config.ts ./
RUN pnpm run build

FROM nginx:stable-alpine@sha256:0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94 AS runtime
COPY --from=build /app/dist /usr/share/nginx/html
COPY deploy/images/nginx.conf.template /etc/nginx/weipai.conf.template
COPY --chmod=0555 deploy/images/frontend-entrypoint.sh /usr/local/bin/weipai-frontend
ENV API_UPSTREAM=api:8000
USER 101:101
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 CMD wget -q -O /dev/null http://127.0.0.1:8080/health || exit 1
ENTRYPOINT ["/usr/local/bin/weipai-frontend"]
