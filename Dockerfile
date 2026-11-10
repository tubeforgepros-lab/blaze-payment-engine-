# Tubeforge Sandbox Bank: production image for Railway
# Stage 1: install production dependencies from the lockfile only
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force

# Stage 2: minimal runtime image
FROM node:22-alpine
ENV NODE_ENV=production
WORKDIR /app
COPY --from=deps --chown=node:node /app/node_modules ./node_modules
COPY --chown=node:node package.json server.js wise.js audit.js app.js index.html ./
# never run as root
USER node
# Railway injects PORT; 8080 is only the local default
ENV PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD wget -qO- "http://127.0.0.1:${PORT}/health" >/dev/null || exit 1
# run node directly as PID 1 so it receives SIGTERM; server.js shuts down gracefully
CMD ["node", "server.js"]
