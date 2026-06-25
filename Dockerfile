# Quant Desk API — production image
# Builds the FastAPI layer over the existing trading engine.
FROM python:3.12-slim

# Headless matplotlib (engine imports pyplot via utils/visualize1.py)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLBACKEND=Agg \
    PORT=8000

WORKDIR /app

# Install deps first for better layer caching.
# Existing engine deps + the API layer (fastapi/uvicorn).
COPY requirements.txt ./requirements.txt
COPY api/requirements-api.txt ./api/requirements-api.txt
RUN pip install --no-cache-dir -r requirements.txt -r api/requirements-api.txt

# App code (data/ is gitignored; yfinance rebuilds the cache on first request)
COPY . .

EXPOSE 8000

# Hosts (Render/Railway) inject $PORT; default to 8000 locally.
CMD ["sh", "-c", "uvicorn api.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
