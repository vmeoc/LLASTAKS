# faiss-wrap Dockerfile
# Lightweight CPU-only image
FROM python:3.11-slim

#LABEL
LABEL org.opencontainers.image.title="FAISS wrap"
LABEL org.opencontainers.image.description="FAISS wrapped in FASTAPI for chatbot RAG in LLASTAKS project"
LABEL org.opencontainers.image.source="https://github.com/vmeoc/llasta"
LABEL changelog="Added new chunk metrics for Prometheus"

# Install system deps (git for sentence-transformers model downloads; curl for health/debug)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy backend
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend

# Create data and model cache directories
RUN mkdir -p /data /models/hub
ENV DATA_DIR=/data \
    HF_HOME=/models/hub \
    EMBED_MODEL=BAAI/bge-m3

EXPOSE 9000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "9000"]
