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






### 1.1 Configurer ECR Pull-Through Cache (PTC) pour `vllm/vllm-openai`

1) Secret Docker Hub (évite rate-limits) :
```bash
aws secretsmanager create-secret \
  --name "ecr-pullthroughcache/dockerhub2" \
  --description "Docker Hub credentials for ECR Pull-Through Cache" \
  --secret-string '{"username":"<DOCKERHUB_USER>","accessToken":"<DOCKERHUB_TOKEN>"}' \
  --region us-east-1
```

2) Règle PTC :
```bash
aws ecr create-pull-through-cache-rule \
  --ecr-repository-prefix dockerhub \
  --upstream-registry-url registry-1.docker.io \
  --credential-arn "arn:aws:secretsmanager:us-east-1:142473567252:secret:ecr-pullthroughcache/dockerhub2" \
  --region us-east-1

3) Premier pull pour amorcer le cache :
```bash
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 142473567252.dkr.ecr.us-east-1.amazonaws.com
docker pull 142473567252.dkr.ecr.us-east-1.amazonaws.com/dockerhub/vllm/vllm-openai:v0.10.0
```

4) Vérifier que l'image est dans ECR :
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


# Tests
curl -s "http://127.0.0.1:8000/v1/chat/completions"   -H "Content-Type: application/json" -H "Authorization: Bearer sk-fake"   -d '{"model":"Qwen/Qwen3-8B","messages":[{"role":"user","content":"Bonjour Qwen3 !"}]}'

# Clean-up
kubectl -n llasta delete deploy vllm-qwen3 svc vllm-svc pvc qwen3-weights
```
