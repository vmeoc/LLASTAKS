"""
faiss-wrap: FastAPI service providing embeddings (BAAI/bge-m3), FAISS indexing, and retrieval.

Endpoints:
- GET /health: basic health check
- POST /upsert: add or update chunks in the vector store
  Payload: { items: [{ id, text, metadata? }] }
- POST /search: query top-k results
  Payload: { query: str, top_k: int }
- GET /metrics: Prometheus metrics
- POST /reset: clear all data from FAISS index and metadata store

Persistence:
- FAISS index stored at /data/index.faiss
- Metadata stored at /data/meta.parquet
- HuggingFace cache at /models/hub

Notes for students:
- This is a minimal, production-lean implementation. It handles cold-start loading,
  concurrency with a simple lock, and periodic save after upserts.
- In production, consider sharding, background persistence, and better metadata stores.
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from contextlib import asynccontextmanager
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import PlainTextResponse

import os
import json
import time
import threading

import numpy as np
import pandas as pd

# Ensure HF cache is on mounted volume
os.environ.setdefault("HF_HOME", "/models/hub")

# Late imports to speed cold startup of the process
import faiss  # type: ignore
from sentence_transformers import SentenceTransformer

DATA_DIR = os.getenv("DATA_DIR", "/data")
INDEX_PATH = os.path.join(DATA_DIR, "index.faiss")
META_PATH = os.path.join(DATA_DIR, "meta.parquet")
MODEL_NAME = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
EMBED_DIM = None  # will be set after model loads

# Prometheus metrics

# Compteurs de requêtes par endpoint
REQ_COUNTER = Counter("faiss_wrap_requests_total", "Total requests", ["endpoint", "status"]) 

# Latence des requêtes
REQ_LAT = Histogram("faiss_wrap_request_latency_seconds", "Request latency", ["endpoint"]) 

# Compteurs d'opérations
UPSERT_COUNTER = Counter("faiss_wrap_upserts_total", "Total items upserted")
SEARCH_COUNTER = Counter("faiss_wrap_search_total", "Total searches")
SEARCH_RESULTS_COUNTER = Counter("faiss_wrap_search_results_total", "Total search results returned")

# Gauges pour l'état actuel
INDEX_SIZE_GAUGE = Gauge("faiss_wrap_index_size", "Number of vectors in FAISS index")
METADATA_SIZE_GAUGE = Gauge("faiss_wrap_metadata_size", "Number of metadata entries")
EMBED_DIM_GAUGE = Gauge("faiss_wrap_embedding_dimension", "Embedding dimension")

# Histogramme pour la distribution du nombre de résultats
SEARCH_RESULTS_HISTOGRAM = Histogram(
    "faiss_wrap_search_results_count",
    "Distribution of number of results returned per search",
    buckets=[0, 1, 2, 3, 5, 10, 20, 50]
)

# Initialiser les labels par défaut
REQ_COUNTER.labels(endpoint="health", status="success")
REQ_COUNTER.labels(endpoint="upsert", status="success")
REQ_COUNTER.labels(endpoint="upsert", status="error")
REQ_COUNTER.labels(endpoint="search", status="success")
REQ_COUNTER.labels(endpoint="search", status="error")
REQ_COUNTER.labels(endpoint="reset", status="success")

# Global components
app = FastAPI(title="faiss-wrap", version="1.0.0")
_model: Optional[SentenceTransformer] = None
_index: Optional[faiss.Index] = None
_meta_df: Optional[pd.DataFrame] = None
_lock = threading.Lock()

class UpsertItem(BaseModel):
    id: str
    text: str
    metadata: Optional[Dict[str, Any]] = None

class UpsertRequest(BaseModel):
    items: List[UpsertItem]

class SearchRequest(BaseModel):
    query: str
    top_k: int = 5

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _model, _index, _meta_df, EMBED_DIM
    t0 = time.time()
    print("🧠 Loading embedding model...", MODEL_NAME)
    _model = SentenceTransformer(MODEL_NAME)
    EMBED_DIM = _model.get_sentence_embedding_dimension()

    # Load or create index and metadata
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(INDEX_PATH) and os.path.exists(META_PATH):
        print("📦 Loading existing index and metadata from /data ...")
        _index = faiss.read_index(INDEX_PATH)
        _meta_df = pd.read_parquet(META_PATH)
        # Validate dims
        if _index.d != EMBED_DIM:
            print(f"⚠️ Index dim {_index.d} != model dim {EMBED_DIM}, recreating empty index.")
            _index = faiss.IndexFlatIP(EMBED_DIM)
            _meta_df = pd.DataFrame(columns=["id", "text", "metadata"]) 
    else:
        print("🆕 Creating new empty index and metadata store ...")
        _index = faiss.IndexFlatIP(EMBED_DIM)  # cosine via normalized dot
        _meta_df = pd.DataFrame(columns=["id", "text", "metadata"]) 

    # Normalize flag for cosine similarity using inner product
    faiss.normalize_L2  # touch to ensure symbol import
    
    # Initialiser les gauges Prometheus
    INDEX_SIZE_GAUGE.set(_index.ntotal)
    METADATA_SIZE_GAUGE.set(len(_meta_df))
    EMBED_DIM_GAUGE.set(EMBED_DIM)
    
    print(f"✅ Ready. Dim={EMBED_DIM}, items={len(_meta_df)}. Startup took {time.time()-t0:.1f}s")
    yield
    # Persist on shutdown
    with _lock:
        _persist()
        print("📝 Persisted index and metadata on shutdown")

app.router.lifespan_context = lifespan

@app.get("/health")
def health():
    REQ_COUNTER.labels(endpoint="health", status="success").inc()
    index_size = _index.ntotal if _index is not None else 0
    metadata_size = len(_meta_df) if _meta_df is not None else 0
    return {
        "status": "healthy",
        "model": MODEL_NAME,
        "index_size": int(index_size),
        "metadata_size": int(metadata_size),
        "embedding_dim": EMBED_DIM
    }

@app.get("/metrics")
def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.post("/upsert")
def upsert(req: UpsertRequest):
    global _meta_df
    endpoint = "upsert"
    with REQ_LAT.labels(endpoint).time():
        try:
            if _model is None or _index is None or _meta_df is None:
                REQ_COUNTER.labels(endpoint=endpoint, status="error").inc()
                raise HTTPException(status_code=503, detail="Service not ready")
            if not req.items:
                return {"upserted": 0}
            # Prepare texts and ids
            ids = [it.id for it in req.items]
            texts = [it.text for it in req.items]
            metas = [it.metadata or {} for it in req.items]
            # Compute embeddings
            embs = _embed(texts)
            # Normalize embeddings for cosine similarity using IP index
            faiss.normalize_L2(embs)
            n = len(ids)
            with _lock:
                # Remove duplicates by id: drop old rows and rebuild index if needed
                existing_mask = _meta_df['id'].isin(ids)
                if existing_mask.any():
                    # Rebuild index without existing ids (simple but clear)
                    remaining = _meta_df[~existing_mask]
                    if len(remaining) > 0:
                        re_texts = remaining['text'].tolist()
                        re_embs = _embed(re_texts)
                        faiss.normalize_L2(re_embs)
                        new_index = faiss.IndexFlatIP(embs.shape[1])
                        new_index.add(re_embs)
                        _replace_index(new_index)
                        _meta_df.drop(_meta_df[existing_mask].index, inplace=True)
                    else:
                        # Just reset empty index
                        new_index = faiss.IndexFlatIP(embs.shape[1])
                        _replace_index(new_index)
                        _meta_df.drop(_meta_df.index, inplace=True)
                # Add new vectors
                _index.add(embs)
                # Append metadata
                new_df = pd.DataFrame({"id": ids, "text": texts, "metadata": metas})
                _meta_df = pd.concat([_meta_df, new_df], ignore_index=True)
                # Persist to disk
                _persist()
                
                # Mettre à jour les gauges
                INDEX_SIZE_GAUGE.set(_index.ntotal)
                METADATA_SIZE_GAUGE.set(len(_meta_df))
                
            UPSERT_COUNTER.inc(n)
            REQ_COUNTER.labels(endpoint=endpoint, status="success").inc()
            return {"upserted": n, "total_items": int(len(_meta_df))}
        except Exception as e:
            REQ_COUNTER.labels(endpoint=endpoint, status="error").inc()
            raise

@app.post("/search")
def search(req: SearchRequest):
    endpoint = "search"
    SEARCH_COUNTER.inc()
    with REQ_LAT.labels(endpoint).time():
        try:
            if _model is None or _index is None or _meta_df is None:
                REQ_COUNTER.labels(endpoint=endpoint, status="error").inc()
                raise HTTPException(status_code=503, detail="Service not ready")
            if not req.query.strip():
                REQ_COUNTER.labels(endpoint=endpoint, status="success").inc()
                SEARCH_RESULTS_HISTOGRAM.observe(0)
                return {"results": []}
            q = req.query.strip()
            top_k = max(1, min(int(req.top_k), 50))
            # Embed query
            q_emb = _embed([q])
            faiss.normalize_L2(q_emb)
            if _index.ntotal == 0:
                return {"results": []}
            # Search
            D, I = _index.search(q_emb, top_k)
            results = []
            for i in range(len(I[0])):
                idx = int(I[0][i])
                score = float(D[0][i])
                if idx < 0 or idx >= len(_meta_df):
                    continue
                row = _meta_df.iloc[idx]
                results.append({
                    "id": row["id"],
                    "text": row["text"],
                    "metadata": row.get("metadata", {}),
                    "score": score,
                })
            
            # Enregistrer le nombre de résultats retournés
            num_results = len(results)
            SEARCH_RESULTS_COUNTER.inc(num_results)
            SEARCH_RESULTS_HISTOGRAM.observe(num_results)
            REQ_COUNTER.labels(endpoint=endpoint, status="success").inc()
            
            return {"results": results}
        except Exception as e:
            REQ_COUNTER.labels(endpoint=endpoint, status="error").inc()
            raise

@app.post("/reset")
def reset():
    """Clear all data from FAISS index and metadata store."""
    global _meta_df, _index
    endpoint = "reset"
    REQ_COUNTER.labels(endpoint=endpoint, status="success").inc()
    with REQ_LAT.labels(endpoint).time():
        if _model is None or _index is None or _meta_df is None:
            raise HTTPException(status_code=503, detail="Service not ready")
        
        with _lock:
            # Create new empty index
            _index = faiss.IndexFlatIP(EMBED_DIM)
            # Create new empty metadata DataFrame
            _meta_df = pd.DataFrame(columns=["id", "text", "metadata"])
            # Persist empty state
            _persist()
        
        return {"message": "All data cleared", "total_items": 0}

# ---------------------
# Internal helpers
# ---------------------

def _embed(texts: List[str]) -> np.ndarray:
    assert _model is not None
    vecs = _model.encode(texts, normalize_embeddings=False, convert_to_numpy=True, show_progress_bar=False)
    return vecs.astype("float32")


def _persist():
    """Persist FAISS index and metadata parquet to /data."""
    assert _index is not None and _meta_df is not None
    os.makedirs(DATA_DIR, exist_ok=True)
    faiss.write_index(_index, INDEX_PATH)
    _meta_df.to_parquet(META_PATH, index=False)


def _replace_index(new_index: faiss.Index):
    global _index
    _index = new_index
