# Frontend image.
# Builds the SPA with Node.js and serves the generated static files with nginx.

FROM node:20-alpine AS builder
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


FROM nginx:stable-alpine AS runner

RUN rm -f /etc/nginx/conf.d/*.conf

COPY docker/frontend/nginx.conf /etc/nginx/conf.d/frontend.conf
COPY --from=builder /src/frontend/dist /usr/share/nginx/html
