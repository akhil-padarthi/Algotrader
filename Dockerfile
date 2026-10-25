# ---- 1. build the React dashboard -------------------------------------------
FROM node:22-slim AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- 2. Python runtime: API + bot ------------------------------------------
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    MODEL_DIR=/data/models \
    FRONTEND_DIST=/app/frontend/dist \
    PORT=8000
WORKDIR /app/backend

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/algotrader ./algotrader
COPY docker/start-all.sh /app/start-all.sh
COPY --from=frontend /app/frontend/dist /app/frontend/dist
RUN chmod +x /app/start-all.sh && useradd --create-home --uid 1000 trader \
    && mkdir -p /data && chown trader:trader /data
USER trader
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/api/health', timeout=4)"

CMD ["/app/start-all.sh"]
