#!/bin/bash
# Script to deploy Qwen3-8B on AWS EKS using vLLM

#Go to K8 deployment directory
cd K8 deployment

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

# Vérifier l'AZ des nœuds pour s'assurer qu'ils sont dans us-east-1d
NODE_AZ=$(kubectl get nodes -o jsonpath='{.items[0].metadata.labels.topology\.kubernetes\.io/zone}')
echo "✅ Nœuds déployés dans l'AZ: $NODE_AZ"
if [ "$NODE_AZ" != "us-east-1d" ]; then
  echo "⚠️  ATTENTION: Nœuds dans $NODE_AZ au lieu de us-east-1d"
  echo "   Vérifiez que votre volume EBS est aussi dans $NODE_AZ"
fi

#Install EBS CSI driver avec Service Account
echo "Installing EBS CSI Driver addon..."
aws eks create-addon \
    --cluster-name llasta \
    --addon-name aws-ebs-csi-driver \
    --service-account-role-arn arn:aws:iam::142473567252:role/AmazonEKS_EBS_CSI_DriverRole \
    --region us-east-1
fi

# Attendre que l'addon soit actif
echo "Waiting for EBS CSI Driver to be active..."
aws eks wait addon-active --cluster-name llasta --addon-name aws-ebs-csi-driver --region us-east-1

#IMPORTANT : Ajouter les permissions EBS au rôle des nœuds
aws iam attach-role-policy --role-name eks-node-role --policy-arn arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy

# Vérifier l'installation
echo "Verifying EBS CSI Driver installation..."
kubectl get pods -n kube-system | grep ebs-csi

echo"K8 deployment and setup completed. Now, we start the vLLM deployment phase"

#Go to vLLM deployment directory
cd "../002-vLLM deployment"

#création du namespace
kubectl apply -f 00-namespace.yaml

# Créer le PersistentVolume qui référence au volume contenant les poids Qwen3-8B INT4
kubectl apply -f 10-pvc-from-ebs.yaml

#Vérifier que le PVC est bien lié. A Améliorer: attente qu'il passe en bound
kubectl get pvc qwen3-weights-src

#Déployer le runtime vLLM
kubectl apply -f 11-deploy-vllm.yaml
