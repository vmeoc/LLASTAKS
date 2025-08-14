"""
LLASTA Chatbot Backend - FastAPI Application

Ce backend sert d'intelligence de gestion entre le frontend et vLLM.
Il permet d'ajouter facilement du RAG, function calling, etc.

Architecture:
Frontend (HTML/JS) ←→ Backend (FastAPI) ←→ vLLM (OpenAI compatible)
"""

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import httpx
import json
import asyncio
import os
from contextlib import asynccontextmanager

# Configuration - Variables d'environnement avec valeurs par défaut pour les tests locaux
VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "http://localhost:8000")  # Pour test local
VLLM_API_KEY = os.getenv("VLLM_API_KEY", "dummy-key")  # vLLM n'en a pas besoin généralement
VLLM_MODEL_NAME = os.getenv("VLLM_MODEL_NAME", "/models/Qwen3-8B")  # Nom du modèle dans vLLM

# Modèles Pydantic pour la validation des données
class ChatMessage(BaseModel):
    """Modèle pour un message de chat"""
    role: str  # "user", "assistant", "system"
    content: str

class ChatRequest(BaseModel):
    """Modèle pour une requête de chat"""
    messages: List[ChatMessage]
    stream: bool = True
    max_tokens: Optional[int] = 1000
    temperature: Optional[float] = 0.7

class ChatResponse(BaseModel):
    """Modèle pour une réponse de chat (mode non-streaming)"""
    message: ChatMessage
    usage: Dict[str, Any]

# Client HTTP global pour réutiliser les connexions
http_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gestionnaire de cycle de vie de l'application"""
    global http_client
    # Startup: Créer le client HTTP
    http_client = httpx.AsyncClient(timeout=60.0)
    print(f"🚀 Backend démarré - vLLM URL: {VLLM_BASE_URL}")
    yield
    # Shutdown: Fermer le client HTTP
    await http_client.aclose()
    print("🛑 Backend arrêté")

# Création de l'application FastAPI
app = FastAPI(
    title="LLASTA Chatbot Backend",
    description="Backend intelligent pour le chatbot LLASTA avec support vLLM",
    version="1.0.0",
    lifespan=lifespan
)

# Servir les fichiers statiques (HTML, CSS, JS)
app.mount("/static", StaticFiles(directory="frontend"), name="static")

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    """
    Sert la page principale du chatbot
    
    Cette fonction lit le fichier HTML et le retourne.
    En production, on pourrait utiliser un CDN ou un serveur web dédié.
    """
    try:
        with open("frontend/index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(
            content="<h1>Frontend non trouvé</h1><p>Veuillez créer le fichier frontend/index.html</p>",
            status_code=404
        )

@app.get("/health")
async def health_check():
    """
    Endpoint de santé pour vérifier que le backend fonctionne
    
    Utile pour les health checks Kubernetes
    """
    try:
        # Test de connexion à vLLM
        response = await http_client.get(f"{VLLM_BASE_URL}/health")
        vllm_status = "healthy" if response.status_code == 200 else "unhealthy"
    except Exception as e:
        vllm_status = f"error: {str(e)}"
    
    return {
        "status": "healthy",
        "vllm_connection": vllm_status,
        "vllm_url": VLLM_BASE_URL
    }

@app.post("/api/chat")
async def chat_endpoint(request: ChatRequest):
    """
    Endpoint principal de chat qui communique avec vLLM
    
    Cette fonction:
    1. Reçoit les messages du frontend
    2. Les formate pour vLLM (format OpenAI)
    3. Envoie la requête à vLLM
    4. Retourne la réponse en streaming ou non
    
    Args:
        request: Requête contenant les messages et paramètres
        
    Returns:
        StreamingResponse ou ChatResponse selon le mode demandé
    """
    try:
        # Préparation de la requête pour vLLM (format OpenAI compatible)
        vllm_request = {
            "model": VLLM_MODEL_NAME,  # Nom du modèle configuré via variable d'environnement
            "messages": [msg.dict() for msg in request.messages],
            "stream": request.stream,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            # Paramètres optimisés pour Qwen3-8B
            "top_p": 0.8,
            "top_k": 20,
            "presence_penalty": 0.0
        }
        
        print(f"📤 Envoi à vLLM: {len(request.messages)} messages")
        
        if request.stream:
            # Mode streaming: retourne les tokens au fur et à mesure
            return StreamingResponse(
                stream_chat_response(vllm_request),
                media_type="text/plain"
            )
        else:
            # Mode non-streaming: retourne la réponse complète
            response = await http_client.post(
                f"{VLLM_BASE_URL}/v1/chat/completions",
                json=vllm_request,
                headers={"Authorization": f"Bearer {VLLM_API_KEY}"}
            )
            response.raise_for_status()
            
            result = response.json()
            return ChatResponse(
                message=ChatMessage(
                    role="assistant",
                    content=result["choices"][0]["message"]["content"]
                ),
                usage=result.get("usage", {})
            )
            
    except httpx.HTTPStatusError as e:
        print(f"❌ Erreur HTTP vLLM: {e.response.status_code} - {e.response.text}")
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"Erreur vLLM: {e.response.text}"
        )
    except Exception as e:
        print(f"❌ Erreur inattendue: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Erreur serveur: {str(e)}")

async def stream_chat_response(vllm_request: Dict[str, Any]):
    """
    Générateur pour le streaming des réponses de chat
    
    Cette fonction:
    1. Envoie la requête en streaming à vLLM
    2. Parse chaque chunk reçu
    3. Formate et yield les tokens pour le frontend
    
    Args:
        vllm_request: Requête formatée pour vLLM
        
    Yields:
        str: Chunks de réponse formatés pour le frontend
    """
    try:
        async with http_client.stream(
            "POST",
            f"{VLLM_BASE_URL}/v1/chat/completions",
            json=vllm_request,
            headers={"Authorization": f"Bearer {VLLM_API_KEY}"}
        ) as response:
            response.raise_for_status()
            
            async for chunk in response.aiter_lines():
                if chunk:
                    # vLLM envoie des lignes au format "data: {json}"
                    if chunk.startswith("data: "):
                        data = chunk[6:]  # Enlever "data: "
                        
                        # Fin du stream
                        if data.strip() == "[DONE]":
                            yield "data: [DONE]\n\n"
                            break
                        
                        try:
                            # Parser le JSON et extraire le contenu
                            json_data = json.loads(data)
                            if "choices" in json_data and json_data["choices"]:
                                delta = json_data["choices"][0].get("delta", {})
                                if "content" in delta:
                                    # Envoyer le token au frontend
                                    yield f"data: {json.dumps({'content': delta['content']})}\n\n"
                        except json.JSONDecodeError:
                            # Ignorer les chunks malformés
                            continue
                            
    except httpx.HTTPStatusError as e:
        error_msg = f"Erreur vLLM: {e.response.status_code}"
        yield f"data: {json.dumps({'error': error_msg})}\n\n"
    except Exception as e:
        error_msg = f"Erreur serveur: {str(e)}"
        yield f"data: {json.dumps({'error': error_msg})}\n\n"

if __name__ == "__main__":
    import uvicorn
    print("🚀 Démarrage du serveur LLASTA Chatbot Backend...")
    print(f"📡 vLLM URL: {VLLM_BASE_URL}")
    print(f"🤖 Modèle: {VLLM_MODEL_NAME}")
    print("🌐 Interface disponible sur: http://localhost:8080")
    
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8080,
        reload=True,  # Rechargement automatique en développement
        log_level="info"
    )
