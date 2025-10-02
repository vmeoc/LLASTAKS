## Goal of this stage
Bring visibility on the following layers through metrics, logs, trace:
- Kubernetes: CLuster, nodes, pods: what are the resources consumption and health.
- service layer: what are the key metrics for the various services. For example: Token generation latency for vLLM, number of chunk served for FAISS, number of calls from the orchestrator, etc...
- application layer: visibility on most important metrics to understand the application usage & end to end traceability. Example: number of requests & total latency for the chatbot-rag, end to end traceability to trace when a request enter chatbot-rab, get processed by the orchestrator component, augmented with FAISS, sent to vLLM, the answer is sent back to the user.


## stack used

I will use the cloud service of Grafana to leverage the LGTM stack (Loki, Grafana, Tempo, Mimir).
Grafana = visualization
Loki = logs aggregator
Mimir = Metrics aggregator
Tempo = trace aggregator

## How to implement it
Open a free account on grafana.com and follow the instruction.


## details of the metrics

### Kubernetes





