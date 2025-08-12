# LLASTA – Stage_README (ECR Pull-Through + EBS Snapshot + vLLM, ClusterIP/port-forward)
Région: **us-east-1** · Compte: **142473567252** · Modèle: **Qwen3-8B** · Runtime: **vLLM (OpenAI-compatible)**

Ce document couvre :
- **Initialisation (one-time)** : config ECR **Pull-Through Cache** (PTC) et "priming" des poids sur EBS.
- **Déploiement** : déploiement du **runtime vLLM** avec les poids, accès via **ClusterIP + port-forward**, tests.

> **💡 Note sur les snapshots** : Pour l'apprentissage avec vLLM en lecture seule, les snapshots ne sont **pas nécessaires**. Le PVC persistant suffit ! Les snapshots sont utiles pour la production multi-environnements.

> **Pré-requis**
> - `aws` CLI, `kubectl`, `jq` installés.
> - Cluster **EKS** (auth IAM ok) avec **nœuds GPU** (AMI NVIDIA) + **NVIDIA device plugin**.
> - **AWS EBS CSI driver** installé.
> - Les fichiers fournis dans ce dossier :  
>   `00-namespace.yaml` · `01-storageclasses.yaml` · `02-pvc-source.yaml` · `03-job-prime-weights.yaml` · `11-deploy-vllm.yaml`

---

## 0) Accès au cluster (IAM → kubeconfig)

```bash
aws eks update-kubeconfig --region us-east-1 --name llasta
kubectl get nodes
```

---

## 1) INITIALISATION (one-time)

### 1.0 Vérifier et installer les composants EKS nécessaires

**Vérifier l'EBS CSI Driver** (requis pour les volumes EBS) :
```bash
aws eks describe-addon --cluster-name llasta --addon-name aws-ebs-csi-driver --region us-east-1
```

Si pas installé :
```bash
aws eks create-addon --cluster-name llasta --addon-name aws-ebs-csi-driver --region us-east-1
```

**Vérifier que les pods EBS CSI fonctionnent** :
```bash
kubectl get pods -n kube-system | grep ebs
# Doit afficher des pods ebs-csi-controller et ebs-csi-node en Running
```

**IMPORTANT : Ajouter les permissions EBS au rôle des nœuds** :
```bash
# Cette étape est CRUCIALE pour que l'EBS CSI Driver puisse créer des volumes
aws iam attach-role-policy --role-name eks-node-role --policy-arn arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy
```

**Vérifier que les permissions sont appliquées** :
```bash
aws iam list-attached-role-policies --role-name eks-node-role
# Doit inclure AmazonEBSCSIDriverPolicy dans la liste
```

**Installer le NVIDIA Device Plugin** (requis pour exposer les GPU aux pods) :
```bash
# Installer le NVIDIA Device Plugin
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.14.1/nvidia-device-plugin.yml

# Vérifier que les pods NVIDIA démarrent
kubectl get pods -n kube-system -l name=nvidia-device-plugin-ds

# Attendre la détection des GPU (30-60 secondes)
sleep 60

# Vérifier que les GPU sont maintenant visibles dans Kubernetes
kubectl describe nodes | grep -A 5 -B 5 nvidia.com/gpu
# Doit afficher: nvidia.com/gpu: 1 dans Capacity et Allocatable
```

> **💡 Pourquoi cette étape ?** L'AMI `AL2_x86_64_GPU` contient les drivers NVIDIA, mais le **Device Plugin** est nécessaire pour exposer les ressources GPU à l'API Kubernetes. Sans lui, les pods ne peuvent pas demander de ressources `nvidia.com/gpu`.



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

### 1.2 "Primer" un volume EBS avec les poids Qwen3-8B

> Objectif : télécharger une fois les poids du modèle sur un volume persistant pour réutilisation directe par vLLM.

1) **Créer le namespace** + classes de stockage/snapshot
```bash
kubectl apply -f 00-namespace.yaml
kubectl config set-context llasta --namespace=llasta
kubectl apply -f 01-storageclasses.yaml
```

**Note importante** : Maintenant que les CRDs sont installés, les `StorageClass` ET `VolumeSnapshotClass` devraient être créées sans erreur.

**Vérifier que la StorageClass est créée** :
```bash
kubectl get storageclass
# Doit afficher 'gp3' avec provisioner 'ebs.csi.aws.com'
```

2) **PVC source** (reçoit les poids)
```bash
kubectl apply -f 02-pvc-source.yaml
kubectl get pvc qwen3-weights-src
```

**État attendu** : `STATUS=Pending` avec message `WaitForFirstConsumer`. C'est **normal** ! Le volume EBS sera créé quand un pod utilisera le PVC.

**Si le PVC reste en erreur** (ex: `storageclass.storage.k8s.io "gp3" not found`), recréez-le :
```bash
kubectl delete -f 02-pvc-source.yaml
kubectl apply -f 02-pvc-source.yaml
```

3) **Job de priming** (télécharge `Qwen/Qwen3-8B` → PVC)
```bash
kubectl create secret generic hf-token --from-literal=token=<HF_Token> -n llasta
```

> **📝 Note** : Le token Hugging Face n'est **pas nécessaire** pour Qwen3-8B car ce modèle est **public** (licence Apache 2.0). Le secret `hf-token` est configuré pour compatibilité avec d'autres modèles privés.

```bash
kubectl apply -f 03-job-prime-weights.yaml
```

**Surveiller le progrès** :
```bash
# Voir l'état du job
kubectl get jobs -w

# Voir les logs en temps réel
kubectl logs -f job/prime-qwen3-8b

# Vérifier que le PVC est maintenant Bound
kubectl get pvc qwen3-weights-src
```

**Attendre la completion** :
```bash
kubectl -n llasta wait --for=condition=complete job/prime-qwen3-8b --timeout=2h
```

**Vérifier le contenu téléchargé** (optionnel) :
```bash
# Créer un pod debug pour explorer le volume
kubectl apply -f debug-pod.yaml

# Se connecter au pod et explorer
kubectl exec -it debug-volume -n llasta -- sh
# Dans le pod : ls -la /models/Qwen3-8B/
# Dans le pod : du -sh /models/Qwen3-8B/

# Nettoyer le pod debug
kubectl delete pod debug-volume -n llasta
```

4) **Protéger et tagger le volume EBS**

Section à supprimer

5) **Vérification finale**
```bash
# Vérifier que le PVC est bien Bound avec les poids
kubectl get pvc qwen3-weights-src -n llasta

# Optionnel : nettoyer le job (garder le PVC pour vLLM)
kubectl delete job prime-qwen3-8b -n llasta
```

> **🎉 Félicitations !** Vos poids Qwen3-8B sont maintenant disponibles sur le volume persistant `qwen3-weights-src`, **protégés contre la suppression** et **tagués pour récupération facile**. Vous pouvez passer directement au déploiement vLLM !

---

## 1.3) TROUBLESHOOTING - Problèmes courants

### PVC reste en `Pending` avec erreur `storageclass not found`
```bash
# Vérifier que la StorageClass existe
kubectl get storageclass gp3

# Si elle n'existe pas, la recréer
kubectl apply -f - <<EOF
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3
provisioner: ebs.csi.aws.com
parameters:
  type: gp3
  encrypted: "true"
  fsType: ext4
reclaimPolicy: Retain
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
EOF

# Puis recréer le PVC
kubectl delete -f 02-pvc-source.yaml
kubectl apply -f 02-pvc-source.yaml
```

### EBS CSI Driver non installé
```bash
# Vérifier l'addon
aws eks describe-addon --cluster-name llasta --addon-name aws-ebs-csi-driver --region us-east-1

# Installer si nécessaire
aws eks create-addon --cluster-name llasta --addon-name aws-ebs-csi-driver --region us-east-1
```

### Erreur de permissions "UnauthorizedOperation: ec2:CreateVolume"
Si vous obtenez cette erreur lors de la création de PVC :
```bash
# Ajouter les permissions EBS CSI Driver au rôle des nœuds
aws iam attach-role-policy --role-name eks-node-role --policy-arn arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy

# Attendre 1-2 minutes pour la propagation des permissions
# Puis vérifier que le PVC passe à "Bound"
kubectl get pvc -n llasta
```

---

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

**Vérifier le contenu téléchargé** (optionnel) :
```bash
# Créer un pod debug pour explorer le volume
kubectl apply -f debug-pod.yaml

# Se connecter au pod et explorer
kubectl exec -it debug-volume -n llasta -- sh
# Dans le pod : ls -la /models/Qwen3-8B/
# Dans le pod : du -sh /models/Qwen3-8B/

# Nettoyer le pod debug
kubectl delete pod debug-volume -n llasta
```

### 2.2 Déployer le runtime **vLLM** (image via ECR PTC)
```bash
kubectl apply -f 11-deploy-vllm.yaml
kubectl -n llasta rollout status deploy/vllm-qwen3
kubectl -n llasta get pods -l app=vllm-qwen3 -w
```
Si erreur pour le téléchargement depuis ECR, voir si la création d'un ECR en AWS CLI résoud le problème.

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
