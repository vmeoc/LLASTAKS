# Stage\_README.md

## Objective

This guide explains how to deploy a **EKS cluster with 1 GPU node** on AWS using Terraform, with the **default VPC**, no add-ons, to prepare a basic Kubernetes environment.

---

## **Prerequisites**

* **AWS CLI** configured with permissions for EKS, EC2, IAM, and VPC
* **Terraform** ≥ 1.5 installed
* **kubectl** installed
* An AWS account with access to the **default VPC**

---

## **Setting up AWS Credentials**

Before running Terraform, make sure AWS credentials are configured.

### Option 1: AWS CLI `aws configure`

```bash
aws configure
```

You will be prompted to enter:

* AWS Access Key ID
* AWS Secret Access Key
* Default region (e.g., `us-east-1`)
* Output format (e.g., `json`)

### Option 2: Environment Variables

```bash
export AWS_ACCESS_KEY_ID=your_access_key_id
export AWS_SECRET_ACCESS_KEY=your_secret_access_key
export AWS_DEFAULT_REGION=us-east-1
```

### Option 3: AWS SSO (recommended for organizations)

```bash
aws sso login --profile my-profile
export AWS_PROFILE=my-profile
```

---

## **Included Files**

* `providers.tf`: Terraform providers configuration
* `eks.tf`: AWS resources (EKS cluster + node group)
* `outputs.tf`: Useful output variables (cluster name, endpoint, etc.)

---

## **Important Variables**

* **AWS Region**: in `providers.tf`, default is `us-east-1`
* **Instance Type**: in `eks.tf`, default is `t3.medium`

---

## **Cluster Deployment**

### 1. Initialize Terraform

```bash
terraform init
```

### 2. Apply the deployment

```bash
terraform apply -auto-approve
```

💡 This deployment takes around 5–7 minutes.

### 3. Configure kubectl for this cluster

```bash
aws eks update-kubeconfig --region us-east-1 --name llasta --alias llasta
kubectl config set-context llasta --namespace=llasta
kubectl config use-context llasta
```

### 4. Check that the cluster is running

```bash
kubectl get nodes
```
### 5. setup inside K8

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

> **💡 Pourquoi cette étape ?** L'AMI  contient les drivers NVIDIA, mais le **Device Plugin** est nécessaire pour exposer les ressources GPU à l'API Kubernetes. Sans lui, les pods ne peuvent pas demander de ressources `nvidia.com/gpu`.

---

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

## **Using the Cluster**

* You can now deploy your pods and services in this minimal cluster.
* Ideal for preparation tasks: downloading LLM weights, pushing images to ECR, etc.

---

## **Destroying the Cluster**

When you are done:

```bash
terraform destroy -auto-approve
```

💡 This will delete **all resources** created by this project (EKS cluster, node group, IAM roles).

---

## **Notes**

* This cluster has **no GPU**.
* It uses the AWS **default VPC**.
* For a GPU version (e.g., `g5.xlarge`), change `instance_types` in `aws_eks_node_group` and use the NVIDIA AMI.
