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
from pathlib import Path
from contextlib import asynccontextmanager

# ---------------------------
# Configuration (env vars)
# ---------------------------

# Determine paths relative to this script's location
SCRIPT_DIR = Path(__file__).parent.absolute()
PROJECT_ROOT = SCRIPT_DIR.parent  # chatbot-RAG/
FRONTEND_DIR = PROJECT_ROOT / "frontend"
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


def parse_thinking_content(response_text: str) -> tuple[str, str]:
    """
    Parse LLM response to separate thinking content from final answer.
    Returns (thinking_content, final_content)
    """
    # Log the parsing process for debugging
    print(f"🧠 Parsing response of {len(response_text)} chars")
    
    # Look for </think> tag to separate thinking from final content
    think_end_tag = "</think>"
    
    if think_end_tag in response_text:
        # Find the last occurrence of </think>
        think_end_index = response_text.rfind(think_end_tag)
        if think_end_index != -1:
            # Extract thinking content (everything before and including </think>)
            thinking_content = response_text[:think_end_index + len(think_end_tag)].strip()
            # Extract final content (everything after </think>)
            final_content = response_text[think_end_index + len(think_end_tag):].strip()
            
            # Remove <think> opening tag from thinking content for cleaner display
            if thinking_content.startswith("<think>"):
                thinking_content = thinking_content[7:]  # Remove "<think>"
            if thinking_content.endswith("</think>"):
                thinking_content = thinking_content[:-8]  # Remove "</think>"
            
            print(f"🧠 Found thinking content: {len(thinking_content)} chars")
            print(f"🧠 Final content: {len(final_content)} chars")
            
            return thinking_content.strip(), final_content.strip()
    
    # If no </think> tag found, return empty thinking and full content
    print("🧠 No </think> tag found, returning full content")
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
        print(f"🎯 Max tokens: {effective_max_tokens or 'unlimited'} (think_mode={request.think_mode})")

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
            raw_content = result["choices"][0]["message"]["content"]
            
            # Log response length and first 200 characters for debugging
            print(f"🔍 LLM Response length: {len(raw_content)} chars")
            print(f"🔍 LLM Response (first 200 chars): {raw_content[:200]}")
            print(f"🔍 LLM Response (last 100 chars): {raw_content[-100:] if len(raw_content) > 100 else 'N/A'}")
            
            thinking_content, final_content = parse_thinking_content(raw_content)
            return ChatResponse(
                message=ChatMessage(
                    role="assistant",
                    content=final_content,
                ),
                thinking_content=thinking_content if thinking_content else None,
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
