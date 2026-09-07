# Standalone frontend image
# Builds the frontend with Node.js and serves it with nginx.
#
# Build:
#   docker build -f docker/frontend.Dockerfile -t dlt-frontend .
#
# Run:
#   docker run -p 8080:80 dlt-frontend
#
# The nginx configuration proxies /api requests to the "backend" service
# on port 8000, so both containers must be on the same Docker network.

FROM node:20-alpine AS builder
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


FROM nginx:stable-alpine AS runner

COPY docker/frontend/nginx.conf /etc/nginx/conf.d/default.conf

COPY --from=builder /src/frontend/dist /usr/share/nginx/html
