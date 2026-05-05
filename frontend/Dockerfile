# Multi-stage build for the J1 Execution Console.
#
#   Stage 1 (deps)  — install npm dependencies once, cached on
#                     `package*.json` so source edits don't bust it.
#   Stage 2 (build) — produce the static bundle in `dist/`.
#   Stage 3 (run)   — serve `dist/` with nginx and proxy `/api/*` to
#                     the FastAPI service over the Docker network.
#
# The runtime stage is the only one that ships — it's a few MB of
# nginx + the gzipped JS/CSS. Build-arg `VITE_API_BASE_URL` controls
# where the SPA points its REST calls. In the dev compose stack we
# default to `/api` so the browser stays single-origin (no CORS) and
# nginx proxies the calls to `http://api:8000` inside Docker.

# ---- 1. deps -------------------------------------------------------
FROM node:20-alpine AS deps
WORKDIR /app
# Copy only the manifests first so the npm cache layer is reused
# whenever we change source files but not dependencies.
COPY package.json package-lock.json* ./
RUN npm ci

# ---- 2. build ------------------------------------------------------
FROM node:20-alpine AS build
WORKDIR /app

# Vite reads `VITE_*` env vars at build time and hard-bakes them into
# the static bundle. Override at build time:
#   docker compose build --build-arg VITE_API_BASE_URL=https://api.example.com frontend
ARG VITE_API_BASE_URL=/api
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}

COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# ---- 3. run --------------------------------------------------------
FROM nginx:1.27-alpine AS run

# Replace the stock site config with our SPA + API-proxy config.
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

# nginx logs to /var/log/nginx by default; the base image already
# symlinks them to stdout/stderr so `docker logs` works as expected.
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
