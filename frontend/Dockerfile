# Frontend Dockerfile — Phase 0
# Multi-stage: build with node, serve with a tiny static server.
# - Pinned base images (no `latest`).
# - Non-root user for the runtime stage.
# - No baked-in secrets.

FROM node:20.18.1-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi
COPY tsconfig.json vite.config.ts tailwind.config.js postcss.config.js index.html ./
COPY src ./src
RUN npm run build

FROM node:20.18.1-alpine AS runtime
RUN addgroup -S app && adduser -S -G app app
WORKDIR /app
COPY --from=build /app/dist /app/dist
# `npx serve` is the simplest static server; nginx serves these in production.
RUN npm install --no-audit --no-fund -g serve@14.2.4
USER app
EXPOSE 8080
CMD ["serve", "-s", "dist", "-l", "8080"]
