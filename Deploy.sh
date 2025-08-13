#!/bin/bash
# Script to deploy Qwen3-8B on AWS EKS using vLLM

#Go to K8 deployment directory
cd ../001-K8 deployment

#K8 deployment
terraform apply --auto-approve

## Configure kubectl for this cluster

aws eks update-kubeconfig --region us-east-1 --name llasta --alias llasta
kubectl config set-context llasta --namespace=llasta
kubectl config use-context llasta

### 4. Check that the cluster is running

kubectl get nodes

# Installer le NVIDIA Device Plugin
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.14.1/nvidia-device-plugin.yml

# Vérifier que les pods NVIDIA démarrent
kubectl get pods -n kube-system -l name=nvidia-device-plugin-ds

# Attendre la détection des GPU (30-60 secondes)
sleep 60

# Vérifier que les GPU sont maintenant visibles dans Kubernetes
kubectl describe nodes | grep -A 5 -B 5 nvidia.com/gpu
# Doit afficher: nvidia.com/gpu: 1 dans Capacity et Allocatable

#vérifier l'EBS SCSI driver
aws eks describe-addon --cluster-name llasta --addon-name aws-ebs-csi-driver --region us-east-1

#IMPORTANT : Ajouter les permissions EBS au rôle des nœuds
aws iam attach-role-policy --role-name eks-node-role --policy-arn arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy

echo("K8 deployment and setup completed. Now, we start the vLLM deployment phase")

#Go to vLLM deployment directory
cd ../002-vLLM deployment

# Créer le PersistentVolume qui référence au volume contenant les poids Qwen3-8B INT4
kubectl apply -f 10-pvc-from-ebs.yaml

#Vérifier que le PVC est bien lié
kubectl get pvc qwen3-weights-src

#Déployer le runtime vLLM
kubectl apply -f 11-deploy-vllm.yaml
