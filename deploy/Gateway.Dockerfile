FROM node:22-bookworm-slim
WORKDIR /app
COPY web/package*.json ./
RUN npm ci
COPY web ./
RUN npm run build
CMD ["node", "server/gateway.mjs"]
