"""
LLASTA Chatbot-RAG Backend - FastAPI Application

This backend acts as the intelligence layer between the frontend and vLLM,
with optional RAG retrieval from the faiss-wrap service.

Architecture:
Frontend (HTML/JS) ↔ Backend (FastAPI) ↔ [RAG: faiss-wrap] ↔ vLLM (OpenAI compatible)

Notes for students:
- We keep the base chatbot behavior. If RAG is unavailable, the app still works.
- We insert retrieved context as a system message before user/assistant turns.
- All external calls use a single shared httpx.AsyncClient for performance.
"""

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import httpx
import json
import os
from contextlib import asynccontextmanager

# ---------------------------
# Configuration (env vars)
# ---------------------------
APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("APP_PORT", "8080"))

# vLLM connection
VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "http://localhost:8000")
VLLM_API_KEY = os.getenv("VLLM_API_KEY", "dummy-key")
VLLM_MODEL_NAME = os.getenv("VLLM_MODEL_NAME", "Qwen3-8B")

# faiss-wrap connection (RAG). Example when port-forwarded locally: http://localhost:9000
FAISS_WRAP_URL = os.getenv("FAISS_WRAP_URL", "http://localhost:9000")
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "5"))
MAX_CONTEXT_CHARS = int(os.getenv("RAG_MAX_CONTEXT_CHARS", "4000"))  # cap stuffed context

# ---------------------------
# Pydantic models
# ---------------------------
class ChatMessage(BaseModel):
    role: str  # "user", "assistant", "system"
    content: str

class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    stream: bool = False  # keep defaults simple
    max_tokens: Optional[int] = 1000
    temperature: Optional[float] = 0.7

class ChatResponse(BaseModel):
    message: ChatMessage
    usage: Dict[str, Any]

# ---------------------------
# App lifecycle and client
# ---------------------------
http_client: Optional[httpx.AsyncClient] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(timeout=60.0)
    print(f"🚀 Backend-RAG started - vLLM URL: {VLLM_BASE_URL} | faiss-wrap: {FAISS_WRAP_URL}")
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
app.mount("/static", StaticFiles(directory="frontend"), name="static")

# ---------------------------
# Helpers: RAG retrieval and message prep
# ---------------------------
async def retrieve_context(query: str, top_k: int = RAG_TOP_K) -> List[Dict[str, Any]]:
    """
    Call faiss-wrap /search to retrieve top-k chunks for the query.
    Returns a list of {text, metadata, score} dicts. On failure, returns [].
    """
    global http_client
    try:
        url = f"{FAISS_WRAP_URL}/search"
        payload = {"query": query, "top_k": top_k}
        resp = await http_client.post(url, json=payload)
        if resp.status_code != 200:
            print(f"⚠️ faiss-wrap search non-200: {resp.status_code} - {await _safe_text(resp)}")
            return []
        data = resp.json()
        # Expecting {results: [{text, metadata, score}, ...]}
        return data.get("results", []) if isinstance(data, dict) else []
    except Exception as e:
        print(f"⚠️ faiss-wrap search error: {e}")
        return []

async def _safe_text(response: httpx.Response) -> str:
    try:
        return response.text
    except Exception:
        return "<no text>"

def build_context_block(results: List[Dict[str, Any]], limit_chars: int = MAX_CONTEXT_CHARS) -> str:
    """
    Build a compact context block from search results.
    We include simple source hints for transparency.
    """
    if not results:
        return ""
    lines: List[str] = ["You have access to the following context passages. Cite them when relevant.\n"]
    running = 0
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
    return "\n".join(lines).strip()

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
        return [messages[0], system_msg] + messages[1:]
    return [system_msg] + messages

# ---------------------------
# Routes
# ---------------------------
@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    try:
        with open("frontend/index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(
            content="<h1>Frontend not found</h1><p>Create frontend/index.html</p>",
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

@app.post("/api/chat")
async def chat_endpoint(request: ChatRequest):
    try:
        # Convert Pydantic objects to dicts for vLLM
        base_messages = [{"role": m.role, "content": m.content} for m in request.messages]

        # Retrieve optional context using the latest user message
        last_user_msg = next((m["content"] for m in reversed(base_messages) if m["role"] == "user"), None)
        retrieved = []
        if last_user_msg:
            retrieved = await retrieve_context(last_user_msg, top_k=RAG_TOP_K)
        context_block = build_context_block(retrieved)
        messages_for_vllm = inject_context_into_messages(base_messages, context_block)

        # Apply no_think to the last user message (align with 003-chatbot behavior)
        # We do this AFTER injecting the optional context system message so the index is correct.
        last_user_idx = None
        for i in range(len(messages_for_vllm) - 1, -1, -1):
            if messages_for_vllm[i].get("role") == "user":
                last_user_idx = i
                break
        if last_user_idx is not None:
            content = messages_for_vllm[last_user_idx].get("content", "")
            if isinstance(content, str) and "/no_think" not in content:
                messages_for_vllm[last_user_idx]["content"] = content.rstrip() + " /no_think"

        vllm_request = {
            "model": VLLM_MODEL_NAME,
            "messages": messages_for_vllm,
            "stream": request.stream,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            # Qwen-friendly defaults
            "top_p": 0.8,
            "top_k": 20,
            "presence_penalty": 0.0,
        }

        print(
            f"📤 To vLLM: {len(messages_for_vllm)} msgs | RAG={'on' if context_block else 'off'} "
            f"(retrieved={len(retrieved)})"
        )

        if request.stream:
            return StreamingResponse(
                stream_chat_response(vllm_request),
                media_type="text/plain",
            )
        else:
            response = await http_client.post(
                f"{VLLM_BASE_URL}/v1/chat/completions",
                json=vllm_request,
                headers={"Authorization": f"Bearer {VLLM_API_KEY}"},
            )
            response.raise_for_status()
            result = response.json()
            return ChatResponse(
                message=ChatMessage(
                    role="assistant",
                    content=result["choices"][0]["message"]["content"],
                ),
                usage=result.get("usage", {}),
            )

    except httpx.HTTPStatusError as e:
        print(f"❌ vLLM HTTP error: {e.response.status_code} - {e.response.text}")
        raise HTTPException(status_code=e.response.status_code, detail=f"vLLM error: {e.response.text}")
    except Exception as e:
        print(f"❌ Unexpected error: {str(e)}")
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
