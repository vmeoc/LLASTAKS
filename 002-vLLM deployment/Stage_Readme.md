## 2) DÉPLOIEMENT vLLM

### 2.1 Récupérer un volume EBS existant (si cluster recréé)

> **Cas d'usage** : Vous avez supprimé votre cluster K8s mais vos poids Qwen3-8B sont toujours dans un volume EBS grâce à `reclaimPolicy: Retain`.

**Étape 1 : Identifier le volume EBS avec vos poids**
```bash
# Lister tous les volumes EBS avec des tags du projet
aws ec2 describe-volumes \
  --filters "Name=tag:Project,Values=llasta" \
  --query 'Volumes[*].{VolumeId:VolumeId,Size:Size,State:State,Tags:Tags}' \
  --output table

# Ou chercher par nom si vous avez tagué vos volumes
aws ec2 describe-volumes \
  --filters "Name=tag:Name,Values=*qwen3*" \
  --query 'Volumes[*].{VolumeId:VolumeId,Size:Size,State:State,CreateTime:CreateTime}' \
  --output table
```

**Étape 2 : Vérifier que le volume est dans la bonne AZ**
```bash
# Obtenir l'AZ de vos nœuds K8s
NODE_AZ=$(kubectl get nodes -o jsonpath='{.items[0].metadata.labels.topology\.kubernetes\.io/zone}')
echo "Nœuds K8s dans l'AZ: $NODE_AZ"

# Vérifier l'AZ du volume (doit correspondre)
VOLUME_ID="vol-xxxxxxxxxxxxxxxxx"  # Remplacer par votre volume ID
VOLUME_AZ=$(aws ec2 describe-volumes --volume-ids $VOLUME_ID --query 'Volumes[0].AvailabilityZone' --output text)
echo "Volume dans l'AZ: $VOLUME_AZ"

# Si les AZ ne correspondent pas, créer un snapshot et un nouveau volume dans la bonne AZ
if [ "$NODE_AZ" != "$VOLUME_AZ" ]; then
  echo "⚠️  AZ différentes ! Création d'un snapshot et nouveau volume nécessaire..."
  SNAPSHOT_ID=$(aws ec2 create-snapshot --volume-id $VOLUME_ID --description "Qwen3-8B migration" --query 'SnapshotId' --output text)
  aws ec2 wait snapshot-completed --snapshot-ids $SNAPSHOT_ID
  VOLUME_ID=$(aws ec2 create-volume --snapshot-id $SNAPSHOT_ID --volume-type gp3 --availability-zone $NODE_AZ --query 'VolumeId' --output text)
  aws ec2 wait volume-available --volume-ids $VOLUME_ID
  echo "✅ Nouveau volume créé: $VOLUME_ID"
fi
```

**Étape 3 : Créer un PV/PVC pointant vers ce volume**
```bash
# Créer le PersistentVolume qui référence votre volume EBS existant
kubectl apply -f 10-pvc-from-ebs.yaml
```

**Étape 4 : Vérifier que le PVC est bien lié**
```bash
kubectl get pvc qwen3-weights-src
# Statut attendu: Bound
```

### 2.2 Déployer le runtime **vLLM** (image via ECR PTC)
```bash
kubectl apply -f 11-deploy-vllm.yaml
kubectl -n llasta get events   --watch   --field-selector involvedObject.name=$(kubectl -n llasta get pod -l app=vllm-qwen3 -o name | cut -d/ -f2)kubectl -n llasta get pods -l app=vllm-qwen3 -w
kubectl logs <pod_name> -n llasta --tail=20 -f

```
Si erreur pour le téléchargement depuis ECR, voir si la création d'un ECR en AWS CLI résoud le problème.

> `11-deploy-vllm.yaml` utilise un Service **ClusterIP**. L’image attend les flags (entrypoint de l’API vLLM).  

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
    "messages": [{"role":"user","content":"Hey, how are you doing" /no_think}]
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