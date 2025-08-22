#!/bin/bash
# LLASTA - Deploy Qwen3-8B on AWS EKS using vLLM
# Robust deployment script with error handling and progress tracking

set -e  # Exit on any error
set -u  # Exit on undefined variables

# Always run from the script directory so relative paths work
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Function to check command success
check_success() {
    if [ $? -eq 0 ]; then
        print_success "$1"
    else
        print_error "Failed: $1"
        exit 1
    fi
}

print_status "Starting LLASTA deployment..."
print_status "Phase 1: Infrastructure deployment with Terraform"

# Navigate to Kubernetes deployment directory
cd "000-K8 deployment" || { print_error "Cannot find K8 deployment directory"; exit 1; }

# Initialize Terraform (needed before state operations)
print_status "Initializing Terraform..."
terraform init -input=false -upgrade=false
check_success "Terraform init"

# Preflight: ensure existing IAM role for EBS CSI is imported into Terraform state
print_status "Preflight: syncing existing IAM role (EBS CSI) to Terraform state if needed..."
if aws iam get-role --role-name AmazonEKS_EBS_CSI_DriverRole >/dev/null 2>&1; then
    if ! terraform state show aws_iam_role.ebs_csi_driver_role >/dev/null 2>&1; then
        print_status "Importing existing role AmazonEKS_EBS_CSI_DriverRole into Terraform state..."
        terraform import aws_iam_role.ebs_csi_driver_role AmazonEKS_EBS_CSI_DriverRole || true
    else
        print_status "IAM role already tracked in Terraform state."
    fi
else
    print_status "IAM role AmazonEKS_EBS_CSI_DriverRole not found; Terraform will create it."
fi

# Deploy infrastructure
print_status "Applying Terraform configuration..."
terraform apply --auto-approve
check_success "Terraform infrastructure deployment"

print_status "Phase 2: Configuring kubectl for EKS cluster"

# Configure kubectl
print_status "Updating kubeconfig..."
aws eks update-kubeconfig --region us-east-1 --name llasta --alias llasta
check_success "Kubeconfig update"

kubectl config set-context llasta --namespace=llasta
check_success "Setting default namespace"

kubectl config use-context llasta
check_success "Switching to llasta context"

# Verify cluster connectivity
print_status "Verifying cluster connectivity..."
kubectl get nodes
check_success "Cluster connectivity verification"

print_status "Phase 3: Installing NVIDIA Device Plugin"

# Install NVIDIA Device Plugin
print_status "Installing NVIDIA Device Plugin..."
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.14.1/nvidia-device-plugin.yml
check_success "NVIDIA Device Plugin installation"

# Wait for NVIDIA pods to start
print_status "Waiting for NVIDIA Device Plugin pods to start..."
sleep 30
kubectl get pods -n kube-system -l name=nvidia-device-plugin-ds
check_success "NVIDIA Device Plugin pods verification"

# Wait for GPU detection
print_status "Waiting for GPU detection (60 seconds)..."
sleep 60

# Verify GPU availability
print_status "Verifying GPU availability in Kubernetes..."
kubectl describe nodes | grep -A 5 -B 5 nvidia.com/gpu || print_warning "GPU not yet visible, may need more time"
print_success "GPU detection phase completed"

# Verify node availability zone
print_status "Verifying node availability zone..."
NODE_AZ=$(kubectl get nodes -o jsonpath='{.items[0].metadata.labels.topology\.kubernetes\.io/zone}')
print_success "Nodes deployed in AZ: $NODE_AZ"
if [ "$NODE_AZ" != "us-east-1d" ]; then
  print_warning "Nodes in $NODE_AZ instead of us-east-1d"
  print_warning "Ensure your EBS volume is also in $NODE_AZ"
fi

print_status "Phase 4: EBS CSI Driver addon (managed by Terraform)"
print_status "Skipping CLI-based addon management. Terraform handles creation/update."

# Optional verification
print_status "Verifying EBS CSI Driver installation..."
kubectl get pods -n kube-system | grep ebs-csi || true
print_success "EBS CSI Driver verification (best-effort)"

print_success "Kubernetes deployment and setup completed!"
print_status "Phase 5: vLLM deployment"

# Navigate to vLLM deployment directory
cd "../002-vLLM deployment" || { print_error "Cannot find vLLM deployment directory"; exit 1; }

# Create namespace
print_status "Creating llasta namespace..."
kubectl apply -f 00-namespace.yaml
check_success "Namespace creation"

# Create PersistentVolume referencing EBS volume with Qwen3-8B weights
print_status "Creating PersistentVolume for Qwen3-8B weights..."
kubectl apply -f 10-pvc-from-ebs.yaml
check_success "PersistentVolume creation"

# Wait for PVC to be bound
print_status "Waiting for PVC to be bound..."
for i in {1..30}; do
    PVC_STATUS=$(kubectl get pvc qwen3-weights-src -n llasta -o jsonpath='{.status.phase}' 2>/dev/null || echo "NotFound")
    if [ "$PVC_STATUS" = "Bound" ]; then
        print_success "PVC successfully bound"
        break
    elif [ "$i" -eq 30 ]; then
        print_error "PVC failed to bind after 5 minutes"
        kubectl describe pvc qwen3-weights-src -n llasta
        exit 1
    else
        print_status "PVC status: $PVC_STATUS, waiting... ($i/30)"
        sleep 10
    fi
done

# Deploy vLLM runtime
print_status "Deploying vLLM runtime..."
kubectl apply -f 11-deploy-vllm.yaml
check_success "vLLM deployment"

# Wait for vLLM pod to be ready
print_status "Waiting for vLLM pod to be ready (this may take 5-10 minutes)..."
kubectl wait --for=condition=ready pod -l app=vllm-qwen3 -n llasta --timeout=600s
check_success "vLLM pod readiness"

print_success "🎉 LLASTA deployment completed successfully!"
print_status "Next steps:"
echo -e "  1. Start port-forward: ${GREEN}kubectl -n llasta port-forward svc/vllm 8000:8000${NC}"
echo -e "  2. Test API: ${GREEN}curl http://127.0.0.1:8000/v1/models${NC}"
echo -e "  3. Chat with Qwen3: ${GREEN}curl -s 'http://127.0.0.1:8000/v1/chat/completions' -H 'Content-Type: application/json' -d '{\"model\": \"Qwen3-8B\", \"messages\": [{\"role\":\"user\",\"content\":\"Hello!\"}]}'${NC}"
