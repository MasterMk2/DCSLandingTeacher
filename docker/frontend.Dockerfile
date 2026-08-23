# syntax=docker/dockerfile:1
# OPTIONAL standalone frontend image (nginx + API proxy).
#
# The recommended deployment is the single-container setup in
# docker/backend.Dockerfile / docker-compose.yml, where FastAPI serves the
# built SPA directly. Use this file only when you want to host the frontend
# separately (e.g. behind your own reverse proxy tier):
#
#   docker build -f docker/frontend.Dockerfile -t dlt-frontend .
#   docker run -p 8080:80 dlt-frontend   # expects a "backend" host reachable
#                                        # on the same Docker network
#
# Build context must be the repository root.

FROM node:20-alpine AS build
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY <<'NGINX' /etc/nginx/conf.d/default.conf
server {
    listen 80;
    server_name _;

    root /usr/share/nginx/html;
    index index.html;

    # REST + WebSocket traffic goes to the backend service.
    location /api/ws {
        proxy_pass http://backend:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
    }

    location /api {
        proxy_pass http://backend:8000;
        proxy_set_header Host $host;
    }

    # SPA fallback.
    location / {
        try_files $uri $uri/ /index.html;
    }
}
NGINX
COPY --from=build /src/frontend/dist /usr/share/nginx/html
