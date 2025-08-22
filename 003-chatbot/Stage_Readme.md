# Minimal Chatbot Architecture for LLASTA (v1)

## Goals

* Very simple web-based chatbot UI.
* One conversation at a time (no multi-session).
* No database.
* No authentication.
* "Reset" button to clear the conversation.
* Containerized and deployed on Kubernetes.

## Components

### 1. **LLM Backend (vLLM)**

* **Deployment**: GPU-enabled, runs vLLM serving Qwen3:8B.
* **Service**: `ClusterIP`, port `8000`, exposing OpenAI-compatible API (`/v1/*`).
* Internal DNS potentiel: `vllm.llasta.svc.cluster.local:8000`.

### 2. **Chatbot Backend**

* **Framework**: FastAPI.
* **Responsibilities**:

  * Serve static frontend (HTML + JS) at `/`.
  * Expose `/api/chat` endpoint (SSE or JSON) that:

    * Receives user message.
    * Forwards request to vLLM (`/v1/chat/completions`).
    * Streams back tokens to the client.
  * No session persistence (conversation state kept in browser memory).
* **Deployment**: Python 3.11-slim container.
* **Service**: `ClusterIP`.
* \*\*No public access. Will be available through port forward \*\*

### 3. **Frontend**

* Simple `index.html` + JavaScript.
* Maintains conversation state in browser memory.
* Sends POST/stream requests to `/api/chat`.
* "Reset" button clears local conversation state.

## Kubernetes Networking

* **vLLM**: `ClusterIP` (internal only).

* **Backend**: Talks to vLLM via internal DNS.

* **Avoid NodePort** for vLLM.

## Config

* Pass `OPENAI_API_BASE_URL` = `http://vllm.llasta.svc.cluster.local:8000/v1` to backend.
* Pass `OPENAI_API_KEY` if vLLM requires authentication.
* Use ConfigMap for non-secret vars, Secret for API key.

## Deployment Flow

### Initial Setup

1. Build Docker image for Chatbot backend (`fastapi`, `uvicorn`, `httpx` or `openai` client).
2. Push to Docker Hub (or ECR).
3. Apply manifests for:

   * vLLM Deployment + Service.
   * Chatbot Backend Deployment + Service.
   * ConfigMap + Secret.

### Updates

1. Build & push new backend image.
2. Update backend Deployment image.
3. No IPs to update (internal DNS stays the same).

## K8s Manifests Needed

* `chatbot.yaml` (combined Deployment, Service, ConfigMap, Secret, Ingress in one file)


## To use the chatbot

1. For testing in local
uv venv
uv run main.py

2. For k8s deployment
kubectl apply -f k8s/chatbot.yaml
kubectl -n llasta port-forward svc/chatbot 8080:8080
dans le navigateur http://localhost:8080

