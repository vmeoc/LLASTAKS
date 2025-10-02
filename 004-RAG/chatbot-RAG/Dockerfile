FROM python:3.11-slim

#LABEL
LABEL org.opencontainers.image.title="Chatbot RAG"
LABEL org.opencontainers.image.description="Chatbot RAG avec Qwen3 et FAISS"
LABEL org.opencontainers.image.source="https://github.com/vmeoc/llasta"
LABEL changelog="Added monitoring with Prometheus"


# Environment
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# System deps (keep minimal)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
  && rm -rf /var/lib/apt/lists/*

# Workdir at app root
WORKDIR /app

# Install Python deps
COPY backend/requirements.txt ./requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy backend entrypoint and frontend assets (follow 003-chatbot pattern)
COPY backend/main.py ./main.py
COPY frontend ./frontend

# Create non-root user and set ownership
RUN useradd -ms /bin/bash appuser && chown -R appuser:appuser /app
USER appuser

# Expose app port
EXPOSE 8080

# Default envs (can be overridden at runtime)
ENV APP_HOST=0.0.0.0 \
    APP_PORT=8080 \
    VLLM_BASE_URL=http://vllm:8000 \
    VLLM_MODEL_NAME=Qwen3-8B \
    FAISS_WRAP_URL=http://faiss-wrap:9000 \
    RAG_TOP_K=5 \
    RAG_MAX_CONTEXT_CHARS=4000

# Start app (main.py runs uvicorn in __main__)
CMD ["python", "main.py"]
