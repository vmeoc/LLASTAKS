# Objective

Stand up a **simple, reliable, low‑cost** RAG V1 on EKS with vLLM (Qwen3‑8B INT4) for generation, **FAISS** for retrieval, and a **reranker** for quality.

We want:
- Understand how RAG works from the ingest phase to the retrieval phase, 
- what are his limitations,
- how it works in conjunction with the LLM and how the LLM "intelligence" contribute to the end result

For this:
- setup the wrapped FAISS service & RAG chatbot by following below instructions
- analyse the output of ingest.py for the PDF/Hard to read... & reflect on it
- analyse the output of ingest.py for the PDF/Easy to read... & reflect on it
- Ingest PDF/Easy to read...in FAISS and test the RAG. What happens? Do the same test in large LLMs such as GPT 5. What does it tell us?
- Reset FAISS & Ingest PDF/build your own electric car. Reflect on this.

---






## Logical architecture

* **Chatbot (FastAPI)**
  * Need to work with and without RAG
  * Performs **query cleaning** with Qwen3 (optional)
  * Calls **faiss‑wrap** (internal service) to retrieve **text + metadata** for the top‑k passages.
  * Runs a local **reranker** (cross‑encoder, bge‑reranker‑v2‑m3) → keeps the best m passages.
  * **Stuffing** (concat) of the m passages + user prompt → vLLM (Qwen3‑8B) → answer with **citations**.
* **faiss‑wrap (FastAPI + FAISS)**

  * Computes **embeddings** (bge‑m3) **inside the service**.
  * Stores/queries **vectors** with FAISS.
  * Persists index + metadata on a **PVC (EBS)** at `/data`.
  * Exposes `/upsert`, `/search`, `/metrics`.
* **RAG Manager (script/Job)**

  * Reads PDFs from S3, then **extract → clean → segment** (chunks \~800–1200 tokens).
  * Sends `POST /upsert` to faiss‑wrap.
  * Writes a **manifest.parquet** (ingestion log) to S3.
* **Storage**

  * **PVC `/data`**: FAISS index (+ sqlite/json metadata).
  * **PVC `/models`**: Hugging Face cache (bge‑m3, reranker, tokenizer…), optional if you bake images.

> Reminder: **FAISS does not compute embeddings**; the **embedding model** (bge‑m3) does. vLLM/Qwen3 **only sees text**.

---

## Ingestion flow (ingest.py)

1. **Extract** text from PDFs (remove headers/footers, page numbers).
2. **Clean**: normalize (whitespace, encoding, casing, stray punctuation), **filter** (empty/boilerplate pages), **dedupe** (hash to avoid duplicates).
3. **Segment**: chunks \~1000 tokens (or equivalent character length).
4. **Upsert**: send to `faiss‑wrap` → **embed (bge‑m3)** → `index.add()` + persist.
5. **manifest.parquet** (S3): `doc_id, chunk_id, row_hash, embedding_model, dim, ts, source_uri, token_count, lang, schema_version`.

> For **bank statements**: start simple (standard split). If relevance is mediocre → switch to **tabular slicing** (1 chunk = 1 transaction), which performs much better on that data shape.

---

## Query flow (Chatbot)

1. **Query cleaning** (Qwen3) → rewrites the query in basic terms.
2. `POST /search` (cleaned text, k=20) → faiss‑wrap **embed(query)** + search (IndexFlatIP/L2 for exhaustive V1).
3. Local **rerank** (bge‑reranker‑v2‑m3) over k passages → keep **m=5**.
4. **Stuff**: concat the 5 passages + user prompt → vLLM Qwen3‑8B.
5. Answer **+ citations** (source\_uri/page/score).

**Default settings**

* Chunk: **1 page = 1 chunk** (page‑by‑page, overlap 0); k=20 → rerank → m=5; thresholded refusal for low scores.
* Final context size: 1.5k–2.5k tokens of passages to keep generation budget.
* LLM rules: always cite sources; say “I don’t know” below threshold; avoid hallucinations.

---

## faiss‑wrap – API V1

**Service DNS**: `faiss-wrap.llasta.svc.cluster.local`

**POST /upsert**

```json
[
  {
    "id": "docA#chunk-0001",
    "text": "…passage text…",
    "metadata": {
      "doc_id": "docA",
      "source_uri": "s3://bucket/statement-2024-12.pdf#page=2",
      "page": 2,
      "section": "transactions",
      "lang": "fr"
    }
  }
]
```

* Action: **embed (bge‑m3)** → `index.add()`; persist `id→metadata`.

**POST /search**

```json
{ "query": "total card payments in December", "k": 20 }
```

* Action: **embed(query)** → `index.search()` → returns `[{id, text, metadata, score}]`.

**GET /metrics**

* Exposes latency, QPS, index size, vector count, chunk count (Prometheus).

**Persistence**

* Files: `/data/faiss.index`, `/data/meta.sqlite` (or jsonl). Save every batch/interval.

---

## Model choices

* **Generation**: Qwen3‑8B INT4 w4a16 via vLLM.
* **Embedding**: **bge‑m3** (multilingual FR/EN), CPU OK.
* **Reranker**: **bge‑reranker‑v2‑m3** (cross‑encoder), CPU OK.

---

## K8s – minimum objects

* **faiss‑wrap Deployment** (1 CPU pod) + **PVC `/data`** (EBS) + **PVC `/models`** for BGE model caching; ClusterIP Service.
* **Chatbot Deployment** (1 CPU pod) – mounts `/models` for BGE model caching from ebs to be created; ClusterIP Service.
* **vLLM Deployment** (1 GPU pod) – mounts `/models` for Qwen2 weight models from  already created ebs available through pvc qwen3-weights-src.
* **RAG Manager**: script from your laptop (port‑forward).

**EBS RWO note for BGE model caching**

* EBS is **RWO** (one node in R/W). To share `/models`:

  * Co‑locate pods on **the same node** (affinity not needed since there's only 1 node) and mount the same PVC

---

## Observability

* **faiss‑wrap**: `/metrics` for Prometheus (search/upsert latency, QPS, vector/index size, errors).
* **Chatbot**: custom metrics (end‑to‑end latency, context length, k/m, avg score, hit‑rate \<threshold, % “I don’t know”).

### CloudWatch Logs/Insights quick checklist (end‑to‑end chain user → cleaned → top‑k (+metadata) → prompt Qwen3 → answer Qwen3, all correlated by `request_id`)

* **Structured JSON logs** in chatbot & faiss‑wrap, with a common **`request_id`**. Log these events:

  * `user_query.received`, `user_query.cleaned`
  * `rag.search.request`, `rag.search.results` (**id/page/source\_uri/score** + truncated *preview*)
  * `rerank.done`
  * `llm.request` (hash of final prompt, params), `llm.response` (preview, citations/chunk\_ids, tokens\_in/out, latency)
* **Propagate `X-Request-ID`**: FastAPI middleware; forward the header to faiss‑wrap & vLLM; echo it back in HTTP responses.
* **CloudWatch Logs**: deploy *aws‑for‑fluent‑bit* (DaemonSet) to ship `stdout/stderr` → create Log Groups `/llasta/chatbot` and `/llasta/faiss-wrap` (7–30 d retention); enable JSON parsing (or parse in Insights).
* **Insights – useful queries**:

  * Reconstruct a request by `request_id`:

    ```sql
    fields @timestamp, service, event, duration_ms, k, m, results
    | filter request_id = "<RID>"
    | sort @timestamp asc
    ```
  * Key latencies:

    ```sql
    fields @timestamp, service, event, duration_ms
    | filter event in ["rag.search.results","rerank.done","llm.response"]
    | stats avg(duration_ms), pct(duration_ms,95), count() by service, event
    ```
* **Test**: send a query and verify the full sequence in `/llasta/chatbot`:
  `user_query.received` → `user_query.cleaned` → `rag.search.request` → `rag.search.results` → `rerank.done` → `llm.request` → `llm.response`.

---

## Step-by-step setup (Stage RAG)

Follow these steps to deploy the RAG components in EKS.

1) **Prerequisites**

* Ensure EBS volumes exist in the same AZ as your node group and update `004-RAG/.env` with:
  * `VOLUME_FAISS_ID`
  * `VOLUME_MODELS_ID`
  * `AZ`

2) **Create namespace (if not already present)**

```bash
kubectl get ns llasta || kubectl create namespace llasta
```

3) **Apply storage (PV/PVC)**

```bash
kubectl apply -f 004-RAG/k8s/10-pv-pvc-models.yaml
kubectl apply -f 004-RAG/k8s/11-pv-pvc-faiss.yaml
kubectl -n llasta get pvc
```

3b) **Warm models cache**

Pre-download BGE models into the models PVC so pods start faster.

```bash
kubectl apply -f 004-RAG/k8s/15-job-warm-bge-models.yaml
kubectl -n llasta logs job/warm-bge-models -f
```
Expected logs should list files under `/models/hub` (confirming the PVC is populated).

4) **Deploy services**

Images used:

* faiss-wrap: `vmeoc/faiss-wrap:v1`
* chatbot-rag: `vmeoc/chatbot-rag:v1`

```bash
kubectl apply -f 004-RAG/k8s/20-deploy-faiss-wrap.yaml
kubectl apply -f 004-RAG/k8s/21-deploy-chatbot-rag.yaml
kubectl -n llasta get pods -w
```

5) **Validate health**
After connecting through port-forward:

```bash
# faiss-wrap
kubectl -n llasta port-forward deploy/faiss-wrap 9000:9000 &
curl http://localhost:9000/health

# chatbot-rag
kubectl -n llasta port-forward deploy/chatbot-rag 8080:8080 &
curl http://localhost:8080/health
```

6) **Ingest real data (RAG Manager)**

Use `004-RAG/ingest/ingest.py` to read PDFs from S3 and `POST /upsert` to `faiss-wrap` (port-forward as above). Verify `/health` items count grows and chatbot answers include citations.

```bash
kubectl -n llasta port-forward svc/faiss-wrap 19000:9000 

uv run ingest.py   --faiss-wrap-url http://localhost:19000   --s3-input s3://llasta-rag/PDF-Financial/   --s3-manifest s3://llasta-rag/manifests/   --batch-size 8   --max-parallel 4   --http-timeout 300   --preview-chars 120
```

7) **Smoke test retrieval**

After connecting through port-forward:
```bash
curl -X POST http://localhost:9000/upsert \
  -H "Content-Type: application/json" \
  -d '{"items":[{"id":"doc1#p1","text":"Q1 revenue was $10M.","metadata":{"source":"finance.pdf","page":1}}]}'

curl -X POST http://localhost:9000/search \
  -H "Content-Type: application/json" \
  -d '{"query":"What was Q1 revenue?","top_k":3}'
```

7) **Open the chatbot**

Open http://localhost:8080 and ask about Q1 revenue. The backend will inject retrieved context when available.


8) **Reset FAISS**

After connecting through port-forward:
```bash
curl -X POST http://localhost:9000/reset
```
---

## Initial parameters (recommended)

* Chunk: **1 page = 1 chunk** (overlap 0); k=20; m=5; refusal threshold.
* FAISS index: **exhaustive V1** with `IndexFlatIP` (if vectors are normalized) or `IndexFlatL2`.
* Final Qwen3 context: 1.5k–2.5k tokens of passages.
