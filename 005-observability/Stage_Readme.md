# Observability Stack - LLASTA

## Goal

Bring full visibility across three layers:

1. **Infrastructure (Kubernetes)**: Cluster, nodes, pods resources and health
2. **Services**: Key metrics for each service (vLLM latency, FAISS chunks served, request counts)
3. **Application**: End-to-end traceability from user request through chatbot-rag → FAISS → vLLM → response

---

## Stack: Grafana Cloud (LGTM)

We use **Grafana Cloud** with the LGTM stack:

- **L**oki: Logs aggregation
- **G**rafana: Visualization and dashboards
- **T**empo: Distributed tracing
- **M**imir: Metrics storage (Prometheus-compatible)

**Architecture:**
```
Kubernetes Cluster (EKS)
├─ Grafana Alloy Agents (DaemonSet)
│  ├─ Scrape metrics from pods (/metrics endpoints)
│  ├─ Collect logs from containers
│  └─ Receive traces (OTLP gRPC on port 4317)
└─ Send to Grafana Cloud
   ├─ Mimir (metrics)
   ├─ Loki (logs)
   └─ Tempo (traces)
```

---

## Deployment

### 1. Setup Grafana Cloud Account

1. Create a free account at [grafana.com](https://grafana.com)
2. Create a new stack (you'll get a URL like `https://your-stack.grafana.net`)
3. Generate API keys for Mimir, Loki, and Tempo

### 2. Deploy Grafana Alloy Agents

The Terraform configuration in `005-observability/Terraform/` deploys:
- **Grafana Alloy** agents as a DaemonSet (one per node)
- **RBAC** permissions for Kubernetes metrics collection
- **ConfigMaps** with Alloy configuration for metrics, logs, and traces

**Deploy:**
```bash
cd 005-observability/Terraform
terraform init
terraform apply
```

This creates:
- Namespace: `grafana-k8s-monitoring`
- DaemonSet: `grafana-k8s-monitoring-alloy-logs` (log collection)
- DaemonSet: `grafana-k8s-monitoring-alloy-metrics` (metrics scraping)
- Deployment: `grafana-k8s-monitoring-alloy-receiver` (trace receiver)
- Service: `grafana-k8s-monitoring-alloy-receiver` (OTLP endpoint on port 4317)

### 3. Verify Deployment

```bash
kubectl get pods -n grafana-k8s-monitoring
kubectl get svc -n grafana-k8s-monitoring
```

Expected output:
```
NAME                                              READY   STATUS    RESTARTS   AGE
grafana-k8s-monitoring-alloy-logs-xxxxx           1/1     Running   0          5m
grafana-k8s-monitoring-alloy-metrics-xxxxx        1/1     Running   0          5m
grafana-k8s-monitoring-alloy-receiver-xxxxx       1/1     Running   0          5m

NAME                                        TYPE        CLUSTER-IP      PORT(S)
grafana-k8s-monitoring-alloy-receiver       ClusterIP   10.x.x.x        4317/TCP,4318/TCP,9411/TCP
```

---

## Metrics Collection

### How It Works

Grafana Alloy agents **scrape metrics** from pods that expose a `/metrics` endpoint (Prometheus format).

**Pod annotation for auto-discovery:**
```yaml
metadata:
  annotations:
    k8s.grafana.com/scrape: "true"
    k8s.grafana.com/metrics.portNumber: "8080"
    k8s.grafana.com/metrics.path: "/metrics"
```

Alloy scans all pods every **15 seconds** and scrapes metrics from annotated pods.

### Metrics Collected

#### **1. Kubernetes Infrastructure Metrics**

Automatically collected by Alloy:
- **Cluster**: API server health, etcd latency, scheduler performance
- **Nodes**: CPU, memory, disk, network usage per node
- **Pods**: CPU, memory, restart count, status per pod
- **Containers**: Resource limits, requests, throttling

**Example queries in Grafana:**
```promql
# Node CPU usage
node_cpu_seconds_total

# Pod memory usage
container_memory_usage_bytes{namespace="llasta"}

# Pod restart count
kube_pod_container_status_restarts_total{namespace="llasta"}
```

#### **2. Service-Level Metrics**

Each service exposes custom metrics via `/metrics`:

**vLLM** (exposed on port 8000):
- `vllm_request_duration_seconds`: Generation latency
- `vllm_tokens_generated_total`: Total tokens generated
- `vllm_active_requests`: Current active requests
- `vllm_queue_size`: Requests waiting in queue

**FAISS-wrap** (exposed on port 9000):
- `faiss_wrap_requests_total{endpoint, status}`: Request count by endpoint
- `faiss_wrap_request_latency_seconds{endpoint}`: Latency per endpoint
- `faiss_wrap_upserts_total`: Total items upserted
- `faiss_wrap_search_total`: Total searches performed
- `faiss_wrap_search_results_total`: Total results returned
- `faiss_wrap_index_size`: Number of vectors in index
- `faiss_wrap_metadata_size`: Number of metadata entries
- `faiss_wrap_embedding_dimension`: Embedding dimension (1024 for bge-m3)

**chatbot-rag** (exposed on port 8080):
- `chatbot_requests_total{endpoint, status}`: Request count
- `chatbot_request_duration_seconds{endpoint}`: Request latency
- `chatbot_vllm_requests_total{status}`: Requests to vLLM
- `chatbot_faiss_requests_total{status}`: Requests to FAISS
- `chatbot_rag_chunks_sent`: Histogram of chunks sent to LLM
- `chatbot_rag_chunks_total`: Total chunks sent

#### **3. Application-Level Metrics**

Derived from service metrics:

**RAG Pipeline Performance:**
```promql
# Average chunks per request
rate(chatbot_rag_chunks_total[5m]) / rate(chatbot_requests_total{status="success"}[5m])

# FAISS search latency p95
histogram_quantile(0.95, rate(faiss_wrap_request_latency_seconds_bucket{endpoint="search"}[5m]))

# vLLM generation latency p99
histogram_quantile(0.99, rate(vllm_request_duration_seconds_bucket[5m]))
```

### View Metrics in Grafana

1. Go to **Grafana Cloud** → Your stack URL
2. Click **Explore** (compass icon)
3. Select **Mimir** or **Prometheus** data source
4. Enter a PromQL query (e.g., `chatbot_requests_total`)
5. Click **Run query**

---

## Distributed Tracing

### Architecture

```
User Request
    ↓
chatbot-rag (OpenTelemetry SDK)
    ├─ Span: POST /api/chat
    ├─ Span: rag.retrieve_context
    │   └─ HTTP call to faiss-wrap
    │       ↓
    │   faiss-wrap (OpenTelemetry SDK)
    │       ├─ Span: POST /search
    │       ├─ Span: faiss.embed_query
    │       └─ Span: faiss.index_search
    ├─ Span: rag.build_context
    ├─ Span: vllm.generate (HTTP call to vLLM)
    └─ Span: response.send_to_frontend
        ↓
All spans sent via OTLP gRPC to Alloy Receiver (port 4317)
        ↓
Grafana Tempo (Cloud)
```

### Implementation

**Services instrumented:**
- ✅ **chatbot-rag**: Full OpenTelemetry instrumentation
- ✅ **faiss-wrap**: Full OpenTelemetry instrumentation
- ⏳ **vLLM**: Not yet instrumented (future work)

**Instrumentation details:**

1. **Automatic instrumentation** (via OpenTelemetry):
   - FastAPI endpoints (HTTP method, route, status code)
   - HTTP client calls (httpx to FAISS and vLLM)

2. **Manual spans** (custom business logic):
   - `rag.retrieve_context`: RAG retrieval with FAISS
   - `rag.build_context`: Context assembly
   - `vllm.generate`: LLM generation
   - `vllm.response`: Response parsing
   - `response.send_to_frontend`: Final response
   - `faiss.search`: FAISS search operation
   - `faiss.embed_query`: Query embedding
   - `faiss.index_search`: Index search

**Span attributes tracked:**

| Service | Attribute | Description |
|---------|-----------|-------------|
| chatbot-rag | `rag.query` | User query text |
| chatbot-rag | `rag.top_k` | Number of chunks requested |
| chatbot-rag | `rag.min_score` | Similarity threshold |
| chatbot-rag | `rag.results_filtered_count` | Chunks after filtering |
| chatbot-rag | `rag.chunks_sent` | Chunks sent to LLM |
| chatbot-rag | `rag.score_avg` | Average similarity score |
| chatbot-rag | `rag.context_chars` | Context size in chars |
| chatbot-rag | `llm.model` | LLM model name |
| chatbot-rag | `llm.tokens_prompt` | Prompt tokens |
| chatbot-rag | `llm.tokens_completion` | Completion tokens |
| chatbot-rag | `llm.response_chars` | Response length |
| chatbot-rag | `llm.thinking_chars` | Thinking content length |
| faiss-wrap | `faiss.query` | Search query |
| faiss-wrap | `faiss.top_k` | Number of results requested |
| faiss-wrap | `faiss.index_size` | Total vectors in index |
| faiss-wrap | `faiss.results_count` | Results returned |
| faiss-wrap | `faiss.score_min/max/avg` | Score statistics |
| faiss-wrap | `embedding.model` | Embedding model (bge-m3) |
| faiss-wrap | `embedding.dimension` | Vector dimension (1024) |

### View Traces in Grafana

**1. Access Tempo:**
- Go to **Grafana Cloud** → Your stack URL
- Click **Explore** (compass icon)
- Select **Tempo** data source

**2. Search for traces:**

**By service name:**
- Service Name: `chatbot-rag` or `faiss-wrap`
- Time range: Last 15 minutes
- Click **Run query**

**By attributes (filters):**
- `service.name = chatbot-rag`
- `http.route = /api/chat`
- `rag.chunks_sent > 0`

**Using TraceQL:**
```traceql
{ service.name = "chatbot-rag" && http.route = "/api/chat" }
```

**Find slow requests:**
```traceql
{ service.name = "chatbot-rag" && duration > 5s }
```

**Find requests with many chunks:**
```traceql
{ service.name = "chatbot-rag" && rag.chunks_sent > 3 }
```

**3. Analyze a trace:**

Click on any trace to see the waterfall view:

```
POST /api/chat [200ms] - chatbot-rag
├─ rag.retrieve_context [50ms] - chatbot-rag
│  └─ POST /search [30ms] - HTTP to faiss-wrap
│     └─ faiss.search [28ms] - faiss-wrap
│        ├─ faiss.embed_query [10ms] - faiss-wrap
│        └─ faiss.index_search [15ms] - faiss-wrap
├─ rag.build_context [5ms] - chatbot-rag
├─ vllm.generate [145ms] - chatbot-rag
│  └─ vllm.response [1ms] - chatbot-rag
└─ response.send_to_frontend [1ms] - chatbot-rag
```

**Latency breakdown:**
- Total: 200ms
  - RAG retrieval: 50ms (25%)
    - FAISS search: 30ms (15%)
  - Context building: 5ms (2.5%)
  - vLLM generation: 145ms (72.5%)
  - Response: 1ms (0.5%)

**Click on any span** to see attributes:
- `rag.chunks_sent = 2`
- `rag.score_avg = 0.75`
- `faiss.results_count = 5`
- `llm.tokens_prompt = 250`
- `llm.tokens_completion = 100`

### Deployment Steps for Tracing

**1. Build and push images:**
```bash
# chatbot-rag v5.0
cd 004-RAG/chatbot-RAG
docker build -t vmeoc/chatbot-rag:v5.0 .
docker push vmeoc/chatbot-rag:v5.0

# faiss-wrap v4.0
cd 004-RAG/faiss-wrap
docker build -t vmeoc/faiss-wrap:v4.0 .
docker push vmeoc/faiss-wrap:v4.0
```

**2. Update deployments:**
```bash
# Update image versions in YAMLs
# 004-RAG/k8s/21-deploy-chatbot-rag.yaml → v5.0
# 004-RAG/k8s/20-deploy-faiss-wrap.yaml → v4.0

kubectl apply -f 004-RAG/k8s/20-deploy-faiss-wrap.yaml
kubectl apply -f 004-RAG/k8s/21-deploy-chatbot-rag.yaml

kubectl rollout status deployment/faiss-wrap -n llasta
kubectl rollout status deployment/chatbot-rag -n llasta
```

**3. Verify tracing:**
```bash
# Check OTLP endpoint is configured
kubectl logs -n llasta deployment/chatbot-rag | grep "OTLP endpoint"
kubectl logs -n llasta deployment/faiss-wrap | grep "OTLP endpoint"

# Expected output:
# 📡 OTLP endpoint: http://grafana-k8s-monitoring-alloy-receiver.grafana-k8s-monitoring.svc.cluster.local:4317
```

**4. Send test request:**
```bash
kubectl port-forward -n llasta svc/chatbot-rag 8080:8080

curl -X POST http://localhost:8080/api/chat \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hello"}],"stream":false}'
```

**5. View traces in Grafana Cloud** (see above)

---

## Troubleshooting

### Metrics not appearing

**1. Check pod annotations:**
```bash
kubectl get pod -n llasta <pod-name> -o yaml | grep -A 3 annotations
```

**2. Check Alloy is scraping:**
```bash
kubectl logs -n grafana-k8s-monitoring daemonset/grafana-k8s-monitoring-alloy-metrics | grep llasta
```

**3. Test /metrics endpoint:**
```bash
kubectl port-forward -n llasta svc/chatbot-rag 8080:8080
curl http://localhost:8080/metrics
```

### Traces not appearing

**1. Check OTLP endpoint is reachable:**
```bash
kubectl exec -n llasta deployment/chatbot-rag -- curl -v http://grafana-k8s-monitoring-alloy-receiver.grafana-k8s-monitoring.svc.cluster.local:4317
```

**2. Check Alloy receiver logs:**
```bash
kubectl logs -n grafana-k8s-monitoring deployment/grafana-k8s-monitoring-alloy-receiver --tail=100
```

**3. Verify Tempo data source in Grafana Cloud:**
- Go to **Connections** → **Data sources**
- Find **Tempo** and test connection

---

## Summary

### What's Monitored

| Layer | Component | Metrics | Traces | Logs |
|-------|-----------|---------|--------|------|
| Infrastructure | Kubernetes | ✅ CPU, memory, disk, network | ❌ | ✅ |
| Infrastructure | Nodes | ✅ Resource usage | ❌ | ✅ |
| Infrastructure | Pods | ✅ Resource usage, restarts | ❌ | ✅ |
| Service | vLLM | ✅ Latency, tokens, queue | ⏳ | ✅ |
| Service | FAISS-wrap | ✅ Latency, searches, index size | ✅ | ✅ |
| Service | chatbot-rag | ✅ Latency, requests, chunks | ✅ | ✅ |
| Application | End-to-end | ✅ Derived metrics | ✅ Full trace | ✅ |

### Key Insights Available

1. **Performance bottlenecks**: Which component is slowest?
2. **Resource usage**: Is the cluster under/over-provisioned?
3. **Error rates**: Which services are failing?
4. **RAG effectiveness**: How many chunks are used? What are the scores?
5. **Token consumption**: How many tokens per request?
6. **End-to-end latency**: From user request to response

---

## Next Steps

1. ✅ Deploy Grafana Alloy agents
2. ✅ Instrument services with metrics
3. ✅ Instrument services with tracing
4. ⏳ Create Grafana dashboards
5. ⏳ Set up alerts for critical metrics
6. ⏳ Instrument vLLM with tracing





