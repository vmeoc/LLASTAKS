"""
LLASTA Chatbot-RAG Backend - FastAPI Application

This backend acts as the intelligence layer between the frontend and vLLM,
with optional RAG retrieval from the faiss-wrap service.

Architecture:
Frontend (HTML/JS) ↔ Backend (FastAPI) ↔ [RAG: faiss-wrap] ↔ vLLM (OpenAI compatible)

RAG Similarity Filtering:
- FAISS returns top-k chunks (default k=5) based on vector similarity
- We filter results using a similarity score threshold (RAG_MIN_SCORE, default 0.5)
- Only chunks with score >= threshold are sent to the LLM as context
- This prevents irrelevant chunks from polluting the context window
- Adjust RAG_MIN_SCORE via environment variable:
  * 0.3-0.4: Permissive (more chunks, risk of noise)
  * 0.5-0.6: Balanced (recommended)
  * 0.7-0.8: Strict (only highly relevant chunks)

Notes for students:
- We keep the base chatbot behavior. If RAG is unavailable, the app still works.
- We insert retrieved context as a system message before user/assistant turns.
- All external calls use a single shared httpx.AsyncClient for performance.
- Similarity scores are logged for debugging and monitoring.
"""

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse, Response
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import httpx
import json
import os
from pathlib import Path
from contextlib import asynccontextmanager

# Prometheus metrics
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

# ---------------------------
# Prometheus Metrics
# ---------------------------

# Créer un registre personnalisé pour éviter les conflits lors du reload
metrics_registry = CollectorRegistry()

# Counter pour le nombre total de requêtes entrantes
chatbot_requests_total = Counter(
    'chatbot_requests_total',
    'Total number of incoming chat requests',
    ['endpoint', 'status'],  # Labels: endpoint (/api/chat), status (success/error)
    registry=metrics_registry
)

# Counter pour les requêtes vers vLLM
chatbot_vllm_requests_total = Counter(
    'chatbot_vllm_requests_total',
    'Total number of requests sent to vLLM',
    ['status'],  # Labels: status (success/error)
    registry=metrics_registry
)

# Counter pour les requêtes vers FAISS
chatbot_faiss_requests_total = Counter(
    'chatbot_faiss_requests_total',
    'Total number of requests sent to FAISS',
    ['status'],  # Labels: status (success/error)
    registry=metrics_registry
)

# Histogram pour la latence des requêtes
chatbot_request_duration_seconds = Histogram(
    'chatbot_request_duration_seconds',
    'Duration of chat requests in seconds',
    ['endpoint'],
    registry=metrics_registry
)

# Histogramme pour le nombre de chunks RAG envoyés au LLM
chatbot_rag_chunks_sent = Histogram(
    'chatbot_rag_chunks_sent',
    'Number of RAG chunks sent to LLM per request',
    buckets=[0, 1, 2, 3, 5, 10, 20],
    registry=metrics_registry
)

# Counter pour le nombre total de chunks RAG envoyés
chatbot_rag_chunks_total = Counter(
    'chatbot_rag_chunks_total',
    'Total number of RAG chunks sent to LLM',
    registry=metrics_registry
)

# Initialiser les métriques avec des labels par défaut (pour qu'elles apparaissent dans /metrics)
# Cela permet à Grafana de les détecter même sans trafic
chatbot_requests_total.labels(endpoint='/api/chat', status='received')
chatbot_requests_total.labels(endpoint='/api/chat', status='success')
chatbot_requests_total.labels(endpoint='/api/chat', status='error')
chatbot_vllm_requests_total.labels(status='success')
chatbot_vllm_requests_total.labels(status='error')
chatbot_faiss_requests_total.labels(status='success')
chatbot_faiss_requests_total.labels(status='error')

# Initialiser les métriques RAG chunks (observer 0 pour créer les buckets)
chatbot_rag_chunks_sent.observe(0)
chatbot_rag_chunks_total.inc(0)  # inc(0) initialise le counter à 0

# ---------------------------
# Configuration (env vars)
# ---------------------------

# Determine paths relative to this script's location
# Support both local development and Docker deployment
SCRIPT_DIR = Path(__file__).parent.absolute()

# Détection automatique de l'environnement
if (SCRIPT_DIR / "frontend").exists():
    # Environnement Docker: main.py est à /app/, frontend à /app/frontend/
    PROJECT_ROOT = SCRIPT_DIR  # /app/
    FRONTEND_DIR = SCRIPT_DIR / "frontend"
    print(f"🐳 Docker environment detected")
else:
    # Environnement local: main.py est à backend/, frontend à ../frontend/
    PROJECT_ROOT = SCRIPT_DIR.parent  # chatbot-RAG/
    FRONTEND_DIR = PROJECT_ROOT / "frontend"
    print(f"💻 Local environment detected")

APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("APP_PORT", "8080"))

# vLLM connection
VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "http://localhost:8000")
VLLM_API_KEY = os.getenv("VLLM_API_KEY", "dummy-key")
VLLM_MODEL_NAME = os.getenv("VLLM_MODEL_NAME", "Qwen3-8B")

# faiss-wrap connection (RAG). Example when port-forwarded locally: http://localhost:9000
FAISS_WRAP_URL = os.getenv("FAISS_WRAP_URL", "http://localhost:9000")
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "5"))
RAG_MIN_SCORE = float(os.getenv("RAG_MIN_SCORE", "0.5"))  # Seuil de similarité minimum (0-1)
MAX_CONTEXT_CHARS = int(os.getenv("RAG_MAX_CONTEXT_CHARS", "4000"))  # cap stuffed context

# ---------------------------
# Pydantic models
# ---------------------------
class ChatMessage(BaseModel):
    role: str  # "user", "assistant", "system"
    content: str

class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    stream: bool = False
    max_tokens: Optional[int] = None  # Pas de limite pour les tests
    temperature: Optional[float] = 0.7
    think_mode: bool = True

class ChatResponse(BaseModel):
    message: ChatMessage
    thinking_content: Optional[str]
    usage: Dict[str, Any]

# ---------------------------
# App lifecycle and client
# ---------------------------
http_client: Optional[httpx.AsyncClient] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(timeout=120.0)
    print(f"🚀 Backend-RAG started - vLLM URL: {VLLM_BASE_URL} | faiss-wrap: {FAISS_WRAP_URL}")
    print(f"📁 Script directory: {SCRIPT_DIR}")
    print(f"📁 Project root: {PROJECT_ROOT}")
    print(f"📁 Frontend directory: {FRONTEND_DIR}")
    print(f"📄 Frontend file exists: {(FRONTEND_DIR / 'index.html').exists()}")
    yield
    await http_client.aclose()
    print("🛑 Backend-RAG stopped")

app = FastAPI(
    title="LLASTA Chatbot-RAG Backend",
    description="Backend with optional RAG retrieval via faiss-wrap and generation via vLLM",
    version="1.0.0",
    lifespan=lifespan,
)

# Serve static frontend from sibling folder `frontend/`
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

# ---------------------------
# Helpers: RAG retrieval and message prep
# ---------------------------
async def retrieve_context(query: str, top_k: int = RAG_TOP_K) -> List[Dict[str, Any]]:
    """
    Call faiss-wrap /search to retrieve top-k chunks for the query.
    Returns a list of {text, metadata, score} dicts. On failure, returns [].
    """
    global http_client
    print(f"🔍 RAG: Searching for '{query[:50]}{'...' if len(query) > 50 else ''}' (top_k={top_k})")
    
    try:
        url = f"{FAISS_WRAP_URL}/search"
        payload = {"query": query, "top_k": top_k}
        
        resp = await http_client.post(url, json=payload)
        
        if resp.status_code != 200:
            error_text = await _safe_text(resp)
            print(f"⚠️ FAISS error {resp.status_code}: {error_text[:100]}")
            chatbot_faiss_requests_total.labels(status='error').inc()
            return []
            
        data = resp.json()
        results = data.get("results", []) if isinstance(data, dict) else []
        
        # Filtrer par score de similarité (ne garder que les chunks pertinents)
        filtered_results = [r for r in results if r.get("score", 0) >= RAG_MIN_SCORE]
        
        # Incrémenter le compteur de succès FAISS
        chatbot_faiss_requests_total.labels(status='success').inc()
        
        if filtered_results:
            print(f"✅ FAISS: Retrieved {len(filtered_results)}/{len(results)} chunks (score >= {RAG_MIN_SCORE})")
            # Log des scores pour debug
            scores = [r.get("score", 0) for r in filtered_results]
            print(f"📊 Scores: min={min(scores):.3f}, max={max(scores):.3f}, avg={sum(scores)/len(scores):.3f}")
        else:
            print(f"📦 FAISS: No results above threshold (min_score={RAG_MIN_SCORE})")
        
        return filtered_results
        
    except httpx.ConnectError:
        print(f"⚠️ FAISS unavailable at {FAISS_WRAP_URL} - continuing without RAG")
        chatbot_faiss_requests_total.labels(status='error').inc()
        return []
    except Exception as e:
        print(f"⚠️ FAISS error: {type(e).__name__}: {str(e)[:100]}")
        chatbot_faiss_requests_total.labels(status='error').inc()
        return []

async def _safe_text(response: httpx.Response) -> str:
    try:
        return response.text
    except Exception:
        return "<no text>"

def build_context_block(results: List[Dict[str, Any]], limit_chars: int = MAX_CONTEXT_CHARS) -> tuple[str, int]:
    """
    Build a compact context block from search results.
    We include simple source hints for transparency.
    Returns (context_block, num_chunks_included)
    """
    if not results:
        return "", 0
    
    lines: List[str] = ["You have access to the following context passages. Cite them when relevant.\n"]
    running = 0
    included_chunks = 0
    
    for i, r in enumerate(results, start=1):
        text = (r.get("text") or "").strip()
        meta = r.get("metadata") or {}
        src = meta.get("source") or meta.get("file") or meta.get("s3_key") or "unknown"
        page = meta.get("page")
        
        header = f"[{i}] Source: {src}{' p.' + str(page) if page is not None else ''}"
        snippet = f"{header}\n{text}\n"
        
        if running + len(snippet) > limit_chars:
            break
            
        lines.append(snippet)
        running += len(snippet)
        included_chunks += 1
    
    context_block = "\n".join(lines).strip()
    print(f"📝 RAG: Built context with {included_chunks}/{len(results)} chunks ({len(context_block)} chars)")
    
    return context_block, included_chunks

def inject_context_into_messages(messages: List[Dict[str, str]], context_block: str) -> List[Dict[str, str]]:
    """
    Insert context as a system message at the beginning of the conversation.
    We avoid modifying user content to preserve intent. If context is empty, return as-is.
    """
    if not context_block:
        return messages
    
    system_msg = {
        "role": "system",
        "content": (
            "You are a helpful assistant that uses retrieved context to answer. "
            "If the context is not sufficient, say so and proceed cautiously.\n\n" + context_block
        ),
    }
    
    # Prepend or insert after an existing system message
    if messages and messages[0].get("role") == "system":
        final_messages = [messages[0], system_msg] + messages[1:]
    else:
        final_messages = [system_msg] + messages
    
    return final_messages


def parse_thinking_content(response_text: str) -> tuple[str, str]:
    """
    Parse LLM response to separate thinking content from final answer.
    Returns (thinking_content, final_content)
    """
    think_end_tag = "</think>"
    
    if think_end_tag in response_text:
        think_end_index = response_text.rfind(think_end_tag)
        if think_end_index != -1:
            thinking_content = response_text[:think_end_index + len(think_end_tag)].strip()
            final_content = response_text[think_end_index + len(think_end_tag):].strip()
            
            # Remove <think> tags for cleaner display
            if thinking_content.startswith("<think>"):
                thinking_content = thinking_content[7:]
            if thinking_content.endswith("</think>"):
                thinking_content = thinking_content[:-8]
            
            return thinking_content.strip(), final_content.strip()
    
    return "", response_text.strip()

# ---------------------------
# Routes
# ---------------------------
@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    try:
        frontend_file = FRONTEND_DIR / "index.html"
        with open(frontend_file, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(
            content=f"<h1>Frontend not found</h1><p>Expected: {FRONTEND_DIR / 'index.html'}</p>",
            status_code=404,
        )

@app.get("/health")
async def health_check():
    vllm_status = "unknown"
    rag_status = "unknown"
    try:
        response = await http_client.get(f"{VLLM_BASE_URL}/health")
        vllm_status = "healthy" if response.status_code == 200 else "unhealthy"
    except Exception as e:
        vllm_status = f"error: {str(e)}"
    try:
        resp = await http_client.get(f"{FAISS_WRAP_URL}/health")
        rag_status = "healthy" if resp.status_code == 200 else "unhealthy"
    except Exception as e:
        rag_status = f"error: {str(e)}"
    return {"status": "healthy", "vllm": vllm_status, "faiss_wrap": rag_status}

@app.get("/metrics")
async def metrics():
    """
    Expose Prometheus metrics endpoint.
    This endpoint will be scraped by Grafana Alloy.
    """
    return Response(content=generate_latest(metrics_registry), media_type=CONTENT_TYPE_LATEST)

@app.post("/api/chat")
async def chat_endpoint(request: ChatRequest):
    # Incrémenter le compteur de requêtes entrantes
    chatbot_requests_total.labels(endpoint='/api/chat', status='received').inc()
    
    # Mesurer la durée de la requête
    with chatbot_request_duration_seconds.labels(endpoint='/api/chat').time():
        try:
            # Convert Pydantic objects to dicts for vLLM
            base_messages = [{"role": m.role, "content": m.content} for m in request.messages]

            # Retrieve optional context using the latest user message
            last_user_msg = next((m["content"] for m in reversed(base_messages) if m["role"] == "user"), None)
            
            retrieved = []
            if last_user_msg:
                retrieved = await retrieve_context(last_user_msg, top_k=RAG_TOP_K)
                
            context_block, num_chunks_sent = build_context_block(retrieved)
            messages_for_vllm = inject_context_into_messages(base_messages, context_block)
            
            # Enregistrer le nombre de chunks réellement envoyés au LLM
            chatbot_rag_chunks_sent.observe(num_chunks_sent)
            chatbot_rag_chunks_total.inc(num_chunks_sent)

            # Apply think/no_think to the last user message (align with 003-chatbot behavior)
            # We do this AFTER injecting the optional context system message so the index is correct.
            last_user_idx = None
            for i in range(len(messages_for_vllm) - 1, -1, -1):
                if messages_for_vllm[i].get("role") == "user":
                    last_user_idx = i
                    break
            if last_user_idx is not None:
                content = messages_for_vllm[last_user_idx].get("content", "")
                if isinstance(content, str) and "/no_think" not in content:
                    if request.think_mode:
                        messages_for_vllm[last_user_idx]["content"] = content.rstrip()
                    else:
                        messages_for_vllm[last_user_idx]["content"] = content.rstrip() + " /no_think"

            # Pas de limite de tokens pour les tests
            effective_max_tokens = request.max_tokens  # None = pas de limite

            vllm_request = {
                "model": VLLM_MODEL_NAME,
                "messages": messages_for_vllm,
                "stream": request.stream,
                "max_tokens": effective_max_tokens,  # None = pas de limite
                "temperature": request.temperature,
                # Qwen-friendly defaults
                "top_p": 0.8,
                "top_k": 20,
                "presence_penalty": 0.0,
            }

            print(f"📤 vLLM: {len(messages_for_vllm)} msgs | RAG={'ON' if context_block else 'OFF'}")

            if request.stream:
                # Incrémenter le compteur de succès pour les requêtes entrantes
                chatbot_requests_total.labels(endpoint='/api/chat', status='success').inc()
                return StreamingResponse(
                    stream_chat_response(vllm_request),
                    media_type="text/plain",
                )
            else:
                try:
                    response = await http_client.post(
                        f"{VLLM_BASE_URL}/v1/chat/completions",
                        json=vllm_request,
                        headers={"Authorization": f"Bearer {VLLM_API_KEY}"},
                    )
                    response.raise_for_status()
                    
                    # Incrémenter le compteur de succès vLLM
                    chatbot_vllm_requests_total.labels(status='success').inc()
                    
                    result = response.json()
                    raw_content = result["choices"][0]["message"]["content"]
                    
                    # Log de la réponse brute de vLLM
                    print(f"🔍 LLM Response length: {len(raw_content)} chars")
                    print(f"🔍 LLM Response (first 200 chars): {raw_content[:200]}")
                    if len(raw_content) > 100:
                        print(f"🔍 LLM Response (last 100 chars): {raw_content[-100:]}")
                    
                    thinking_content, final_content = parse_thinking_content(raw_content)
                    
                    # Log du parsing
                    print(f"🧠 Parsing: thinking={len(thinking_content)} chars, final={len(final_content)} chars")
                    
                    # Incrémenter le compteur de succès pour les requêtes entrantes
                    chatbot_requests_total.labels(endpoint='/api/chat', status='success').inc()
                    
                    return ChatResponse(
                        message=ChatMessage(
                            role="assistant",
                            content=final_content,
                        ),
                        thinking_content=thinking_content if thinking_content else None,
                        usage=result.get("usage", {}),
                    )
                except httpx.HTTPStatusError as vllm_error:
                    # Incrémenter le compteur d'erreurs vLLM
                    chatbot_vllm_requests_total.labels(status='error').inc()
                    raise vllm_error

        except httpx.HTTPStatusError as e:
            print(f"❌ vLLM error {e.response.status_code}: {e.response.text[:100]}")
            chatbot_requests_total.labels(endpoint='/api/chat', status='error').inc()
            raise HTTPException(status_code=e.response.status_code, detail=f"vLLM error: {e.response.text}")
        except Exception as e:
            print(f"❌ Error: {type(e).__name__}: {str(e)[:100]}")
            chatbot_requests_total.labels(endpoint='/api/chat', status='error').inc()
            raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")

async def stream_chat_response(vllm_request: Dict[str, Any]):
    try:
        async with http_client.stream(
            "POST",
            f"{VLLM_BASE_URL}/v1/chat/completions",
            json=vllm_request,
            headers={"Authorization": f"Bearer {VLLM_API_KEY}"},
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_lines():
                if not chunk:
                    continue
                if chunk.startswith("data: "):
                    data = chunk[6:]
                    if data.strip() == "[DONE]":
                        yield "data: [DONE]\n\n"
                        break
                    try:
                        json_data = json.loads(data)
                        if "choices" in json_data and json_data["choices"]:
                            delta = json_data["choices"][0].get("delta", {})
                            if "content" in delta:
                                yield f"data: {json.dumps({'content': delta['content']})}\n\n"
                    except json.JSONDecodeError:
                        continue
    except httpx.HTTPStatusError as e:
        yield f"data: {json.dumps({'error': f'vLLM error: {e.response.status_code}'})}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'error': f'Server error: {str(e)}'})}\n\n"

if __name__ == "__main__":
    import uvicorn
    print("🚀 Starting LLASTA Chatbot-RAG Backend...")
    print(f"📡 vLLM URL: {VLLM_BASE_URL}")
    print(f"🧠 faiss-wrap URL: {FAISS_WRAP_URL}")
    print(f"🌐 http://{APP_HOST}:{APP_PORT}")
    uvicorn.run("main:app", host=APP_HOST, port=APP_PORT, reload=True, log_level="info")
