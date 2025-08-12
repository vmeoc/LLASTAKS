# Stage\_README.md

## Objective

This guide explains how to deploy a **minimal EKS cluster** on AWS using Terraform, with the **default VPC**, no add-ons, to prepare a basic Kubernetes environment.

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
aws eks update-kubeconfig --region us-east-1 --name llasta-minimal
```

### 4. Check that the cluster is running

```bash
kubectl get nodes
```

---

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
