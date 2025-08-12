# LLASTA – Stage_README (ECR Pull-Through + EBS Snapshot + vLLM, ClusterIP/port-forward)
Région: **us-east-1** · Compte: **142473567252** · Modèle: **Qwen3-8B** · Runtime: **vLLM (OpenAI-compatible)**

Ce document couvre :
- **Initialisation (one-time)** : config ECR **Pull-Through Cache** (PTC), “priming” des poids sur EBS et création du **VolumeSnapshot**.
- **Cycle quotidien** : restauration du **PVC** depuis snapshot, déploiement du **runtime vLLM**, accès via **ClusterIP + port-forward**, tests, clean-up.

> **Pré-requis**
> - `aws` CLI, `kubectl`, `jq` installés.
> - Cluster **EKS** (auth IAM ok) avec **nœuds GPU** (AMI NVIDIA) + **NVIDIA device plugin**.
> - **AWS EBS CSI driver** et **Snapshot Controller/CRDs** installés.
> - Les fichiers fournis dans ce dossier :  
>   `00-namespace.yaml` · `01-storageclasses.yaml` · `02-pvc-source.yaml` · `03-job-prime-weights.yaml` · `04-snapshot.yaml` · `10-pvc-from-snapshot.yaml` · `11-deploy-vllm.yaml`

---

## 0) Accès au cluster (IAM → kubeconfig)

```bash
aws eks update-kubeconfig --region us-east-1 --name <CLUSTER>
kubectl get nodes
```

---

## 1) INITIALISATION (one-time)

### 1.1 Configurer ECR Pull-Through Cache (PTC) pour `vllm/vllm-openai`

1) Secret Docker Hub (évite rate-limits) :
```bash
aws secretsmanager create-secret   --name ecr-pullthroughcache/dockerhub   --secret-string '{"username":"<DOCKERHUB_USER>","accessToken":"<DOCKERHUB_TOKEN>"}'   --region us-east-1
```

2) Règle PTC :
```bash
aws ecr create-pull-through-cache-rule   --ecr-repository-prefix dockerhub   --upstream-registry-url registry-1.docker.io   --credential-arn arn:aws:secretsmanager:us-east-1:142473567252:secret:ecr-pullthroughcache/dockerhub   --region us-east-1
```

3) **Référence d’image** à utiliser côté K8s :  
`142473567252.dkr.ecr.us-east-1.amazonaws.com/dockerhub/vllm/vllm-openai:<tag>`  
(Premier pull depuis l’amont; suivants depuis ECR local.)

> **Note IAM nœuds** : attache au rôle des nœuds la policy `AmazonEC2ContainerRegistryReadOnly` pour autoriser les pulls depuis ECR privé.

---

### 1.2 “Primer” un volume EBS avec les poids Qwen3-8B et créer un **snapshot**

> Objectif : éviter le re-download quotidien; on restaure un volume **depuis snapshot** en qq secondes.

1) **Créer le namespace** + classes de stockage/snapshot
```bash
kubectl apply -f 00-namespace.yaml
kubectl config set-context llasta --namespace=llasta
# pas nécessaire pour déploiement initial: kubectl apply -f 01-storageclasses.yaml
```

2) **PVC source** (reçoit les poids)
```bash
kubectl apply -f 02-pvc-source.yaml
kubectl -n llasta get pvc qwen3-weights-src
```
Attendre `STATUS=Bound`.

3) **Job de priming** (télécharge `Qwen/Qwen3-8B` → PVC)
```bash
kubectl apply -f 03-job-prime-weights.yaml
kubectl -n llasta wait --for=condition=complete job/prime-qwen3-8b --timeout=2h
kubectl -n llasta get pods -l job-name=prime-qwen3-8b
```

4) **Créer le snapshot** depuis le PVC source
```bash
kubectl apply -f 04-snapshot.yaml
kubectl -n llasta get volumesnapshot qwen3-weights-snap -o jsonpath='{.status.readyToUse}'; echo
```
Attendre `true`.

5) **Nettoyage (optionnel, snapshot conservé)**
```bash
kubectl -n llasta delete job prime-qwen3-8b
kubectl -n llasta delete pvc qwen3-weights-src
```

---

## 2) DÉPLOIEMENT QUOTIDIEN (récurrent)

### 2.1 Restaurer un PVC **depuis** le snapshot (qq secondes)
```bash
kubectl apply -f 10-pvc-from-snapshot.yaml
kubectl -n llasta get pvc qwen3-weights
```
Attendre `STATUS=Bound`.

### 2.2 Déployer le runtime **vLLM** (image via ECR PTC)
```bash
kubectl apply -f 11-deploy-vllm.yaml
kubectl -n llasta rollout status deploy/vllm-qwen3
kubectl -n llasta get pods -l app=vllm-qwen3 -w
```

> `11-deploy-vllm.yaml` utilise un Service **ClusterIP**. L’image attend les flags (entrypoint de l’API vLLM).  
> Après le premier test, **pense à pinner un tag** (évite `latest`).

### 2.3 Accéder via **port-forward**
```bash
kubectl -n llasta port-forward svc/vllm-svc 8000:8000
```
API locale : `http://127.0.0.1:8000`

---

## 3) TESTS

### 3.1 `curl` – Chat Completions
```bash
curl -s "http://127.0.0.1:8000/v1/chat/completions"   -H "Content-Type: application/json"   -H "Authorization: Bearer sk-fake"   -d '{
    "model": "Qwen/Qwen3-8B",
    "messages": [{"role":"user","content":"Bonjour Qwen3, résume LLASTA en une phrase."}]
  }' | jq .
```

### 3.2 Python (client OpenAI)
```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="sk-fake")
resp = client.chat.completions.create(
    model="Qwen/Qwen3-8B",
    messages=[{"role":"user","content":"Donne une punchline sur LLASTA."}],
)
print(resp.choices[0].message.content)
```

---

## 4) CLEAN-UP (quotidien, snapshot conservé)
```bash
kubectl -n llasta delete deploy vllm-qwen3 svc vllm-svc pvc qwen3-weights
```

---

## 5) DÉPANNAGE & COHÉRENCE

- **Ordre des fichiers corrigé** : `00-namespace.yaml` est **nouveau** pour garantir que `llasta` existe avant tout objet namespacé.
- **vLLM args corrigés** : l’image `vllm/vllm-openai` a un entrypoint API; on passe `--model /models/Qwen3-8B --host 0.0.0.0 --port 8000 ...` (au lieu de `vllm serve`).
- **ReadinessProbe** ajustée (démarrage long possible) : `initialDelaySeconds: 60`, `failureThreshold: 60`.
- **PVC/Snapshot** : noms/namespace **alignés** (`qwen3-weights-snap` en `llasta`). Restauration: `10-pvc-from-snapshot.yaml`.
- **ECR PTC** : URL d’image **avec préfixe** `dockerhub/`; rôles nœuds avec `AmazonEC2ContainerRegistryReadOnly`.
- **Taints GPU** : le Deployment tolère `nvidia.com/gpu: NoSchedule`. Si tes nœuds ne sont pas taintés, tu peux supprimer la section `tolerations`.
- **Sécurité** : Service **ClusterIP**, accès via `kubectl port-forward`, pas d’exposition publique.

---

## 6) RÉSUMÉ COMMANDES

### Initialisation
```bash
aws eks update-kubeconfig --region us-east-1 --name <CLUSTER>

# ECR Pull-Through Cache
aws secretsmanager create-secret --name ecr-pullthroughcache/dockerhub   --secret-string '{"username":"<DOCKERHUB_USER>","accessToken":"<DOCKERHUB_TOKEN>"}' --region us-east-1
aws ecr create-pull-through-cache-rule --ecr-repository-prefix dockerhub   --upstream-registry-url registry-1.docker.io   --credential-arn arn:aws:secretsmanager:us-east-1:142473567252:secret:ecr-pullthroughcache/dockerhub --region us-east-1

# EBS + Snapshot
kubectl apply -f 00-namespace.yaml
kubectl apply -f 01-storageclasses.yaml
kubectl apply -f 02-pvc-source.yaml
kubectl apply -f 03-job-prime-weights.yaml
kubectl -n llasta wait --for=condition=complete job/prime-qwen3-8b --timeout=2h
kubectl apply -f 04-snapshot.yaml
kubectl -n llasta get volumesnapshot qwen3-weights-snap -o jsonpath='{.status.readyToUse}'; echo
# optionnel
kubectl -n llasta delete job prime-qwen3-8b
kubectl -n llasta delete pvc qwen3-weights-src
```

### Quotidien
```bash
kubectl apply -f 10-pvc-from-snapshot.yaml
kubectl apply -f 11-deploy-vllm.yaml
kubectl -n llasta rollout status deploy/vllm-qwen3
kubectl -n llasta port-forward svc/vllm-svc 8000:8000

# Tests
curl -s "http://127.0.0.1:8000/v1/chat/completions"   -H "Content-Type: application/json" -H "Authorization: Bearer sk-fake"   -d '{"model":"Qwen/Qwen3-8B","messages":[{"role":"user","content":"Bonjour Qwen3 !"}]}'

# Clean-up
kubectl -n llasta delete deploy vllm-qwen3 svc vllm-svc pvc qwen3-weights
```
