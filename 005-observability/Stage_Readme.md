## Goal of this stage
Bring visibility on the following layers through metrics, logs, trace:
- Kubernetes: CLuster, nodes, pods: what are the resources consumption and health.
- service layer: what are the key metrics for the various services. For example: Token generation latency for vLLM, number of chunk served for FAISS, number of calls from the orchestrator, etc...
- application layer: visibility on most important metrics to understand the application usage & end to end traceability. Example: number of requests & total latency for the chatbot-rag, end to end traceability to trace when a request enter chatbot-rab, get processed by the orchestrator component, augmented with FAISS, sent to vLLM, the answer is sent back to the user.


## stack used

I will use the cloud service of Grafana with the LGTM stack (Loki, Grafana, Tempo, Mimir):
Grafana = visualization
Loki = logs aggregator
Mimir = Metrics aggregator
Tempo = trace aggregator

## How to implement it
Open a free account on grafana.com and follow the instruction.
To deploy the Grafana Alloy agents

```
cd Terraform
Terraform init
Terraform apply
```

Go to Grafana\Testing & Synthetics\Kubernetes

The Alloy agent scan all pods configuration for the annotation
      annotations:
        k8s.grafana.com/scrape: "true"
        k8s.grafana.com/metrics.portNumber: "8080"
        k8s.grafana.com/metrics.path: "/metrics"

If the pod provide the /metrics route, Alloy will scrape all the data every 15 seconds. 
Log to the grafana.net URL and go to explore/query Less to view all the metrics.

## details of the metrics

### Kubernetes





