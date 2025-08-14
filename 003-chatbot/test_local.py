#!/usr/bin/env python3
"""
Script de test local pour le chatbot LLASTA

Ce script permet de tester le backend FastAPI en local avant le déploiement.
Il simule un serveur vLLM pour les tests sans avoir besoin du cluster K8s.

Usage:
    python test_local.py [--mock-vllm]
    
    --mock-vllm : Lance un serveur mock vLLM pour les tests
"""

import asyncio
import json
import time
from typing import Dict, Any
import argparse
import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import httpx

class MockVLLMServer:
    """
    Serveur mock qui simule vLLM pour les tests locaux
    
    Ce serveur simule les endpoints de vLLM:
    - /health : Health check
    - /v1/chat/completions : Endpoint de chat avec streaming
    """
    
    def __init__(self):
        self.app = FastAPI(title="Mock vLLM Server")
        self.setup_routes()
    
    def setup_routes(self):
        @self.app.get("/health")
        async def health():
            return {"status": "healthy", "model": "mock-qwen3-8b"}
        
        @self.app.post("/v1/chat/completions")
        async def chat_completions(request: Dict[str, Any]):
            """Simule une réponse de chat avec streaming"""
            
            # Extraire le dernier message utilisateur
            messages = request.get("messages", [])
            user_message = ""
            if messages:
                user_message = messages[-1].get("content", "")
            
            # Réponse simulée basée sur le message
            mock_response = self.generate_mock_response(user_message)
            
            if request.get("stream", False):
                return StreamingResponse(
                    self.stream_mock_response(mock_response),
                    media_type="text/plain"
                )
            else:
                return {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": mock_response
                        }
                    }],
                    "usage": {
                        "prompt_tokens": len(user_message.split()),
                        "completion_tokens": len(mock_response.split()),
                        "total_tokens": len(user_message.split()) + len(mock_response.split())
                    }
                }
    
    def generate_mock_response(self, user_message: str) -> str:
        """Génère une réponse mock basée sur le message utilisateur"""
        
        user_lower = user_message.lower()
        
        if "bonjour" in user_lower or "hello" in user_lower:
            return "Bonjour ! Je suis le chatbot LLASTA en mode test. Comment puis-je vous aider ?"
        
        elif "comment" in user_lower and "va" in user_lower:
            return "Je vais très bien, merci ! Je suis un modèle de test qui simule Qwen3-8B. Tout fonctionne parfaitement ! 🚀"
        
        elif "test" in user_lower:
            return "✅ Test réussi ! Le système de chat fonctionne correctement. Vous pouvez maintenant déployer en production."
        
        elif "python" in user_lower or "code" in user_lower:
            return """Voici un exemple de code Python simple :

```python
def fibonacci(n):
    if n <= 1:
        return n
    return fibonacci(n-1) + fibonacci(n-2)

print(fibonacci(10))  # Affiche 55
```

Ce code calcule la suite de Fibonacci de manière récursive."""
        
        elif "llasta" in user_lower:
            return """LLASTA (LLM App Stack) est votre projet de déploiement end-to-end d'une application LLM sur AWS ! 

🏗️ **Architecture actuelle :**
- ✅ Cluster EKS avec GPU (g5.xlarge)
- ✅ vLLM avec Qwen3-8B quantifié
- ✅ Backend FastAPI (en cours de test)
- 🔄 Frontend web moderne

Excellent travail sur ce projet d'apprentissage ! 🎉"""
        
        else:
            return f"""Je comprends votre message : "{user_message}"

En tant que modèle de test, je peux vous aider avec diverses tâches comme :
- Répondre à vos questions
- Expliquer des concepts techniques  
- Générer du code Python
- Discuter de votre projet LLASTA

Que souhaitez-vous explorer ?"""
    
    async def stream_mock_response(self, response: str):
        """Simule le streaming d'une réponse token par token"""
        
        words = response.split()
        
        for i, word in enumerate(words):
            # Simuler un délai de génération
            await asyncio.sleep(0.1)
            
            # Envoyer le token
            chunk = {
                "choices": [{
                    "delta": {
                        "content": word + " " if i < len(words) - 1 else word
                    }
                }]
            }
            
            yield f"data: {json.dumps(chunk)}\n\n"
        
        # Signal de fin
        yield "data: [DONE]\n\n"

async def test_backend_health():
    """Teste la santé du backend"""
    print("🔍 Test de santé du backend...")
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get("http://localhost:8080/health")
            
            if response.status_code == 200:
                health = response.json()
                print(f"✅ Backend en bonne santé: {health}")
                return True
            else:
                print(f"❌ Backend en erreur: {response.status_code}")
                return False
                
    except Exception as e:
        print(f"❌ Impossible de se connecter au backend: {e}")
        return False

async def test_chat_endpoint():
    """Teste l'endpoint de chat"""
    print("🔍 Test de l'endpoint de chat...")
    
    test_message = {
        "messages": [
            {"role": "user", "content": "Bonjour, ceci est un test du chatbot LLASTA !"}
        ],
        "stream": True,
        "max_tokens": 100,
        "temperature": 0.7
    }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "http://localhost:8080/api/chat",
                json=test_message
            )
            
            if response.status_code == 200:
                print("✅ Réponse reçue en streaming:")
                
                async for chunk in response.aiter_lines():
                    if chunk.startswith("data: "):
                        data = chunk[6:]
                        if data.strip() == "[DONE]":
                            print("\n✅ Streaming terminé")
                            break
                        
                        try:
                            parsed = json.loads(data)
                            if "content" in parsed:
                                print(parsed["content"], end="", flush=True)
                        except json.JSONDecodeError:
                            continue
                
                return True
            else:
                print(f"❌ Erreur de chat: {response.status_code}")
                return False
                
    except Exception as e:
        print(f"❌ Erreur lors du test de chat: {e}")
        return False

async def run_tests():
    """Lance tous les tests"""
    print("🚀 Démarrage des tests du chatbot LLASTA...\n")
    
    # Test 1: Santé du backend
    health_ok = await test_backend_health()
    print()
    
    if not health_ok:
        print("❌ Les tests s'arrêtent car le backend n'est pas accessible")
        return False
    
    # Test 2: Endpoint de chat
    chat_ok = await test_chat_endpoint()
    print()
    
    # Résumé
    if health_ok and chat_ok:
        print("🎉 Tous les tests sont passés avec succès !")
        print("✅ Votre chatbot LLASTA est prêt pour la containerisation et le déploiement K8s")
        return True
    else:
        print("❌ Certains tests ont échoué")
        return False

def main():
    parser = argparse.ArgumentParser(description="Test local du chatbot LLASTA")
    parser.add_argument("--mock-vllm", action="store_true", 
                       help="Lance un serveur mock vLLM pour les tests")
    
    args = parser.parse_args()
    
    if args.mock_vllm:
        print("🎭 Démarrage du serveur mock vLLM sur le port 8000...")
        mock_server = MockVLLMServer()
        uvicorn.run(mock_server.app, host="localhost", port=8000, log_level="info")
    else:
        print("🧪 Lancement des tests du backend...")
        print("💡 Assurez-vous que le backend FastAPI tourne sur le port 8080")
        print("💡 Pour tester avec un mock vLLM, utilisez: python test_local.py --mock-vllm\n")
        
        # Lancer les tests
        result = asyncio.run(run_tests())
        exit(0 if result else 1)

if __name__ == "__main__":
    main()
