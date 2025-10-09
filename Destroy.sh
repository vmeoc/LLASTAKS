#!/bin/bash

################################################################################
# LLASTA Cluster Destroy Script
# 
# Purpose: Cleanly and efficiently destroy the EKS cluster and all resources
# 
# Usage: ./Destroy.sh [--skip-cleanup] [--force]
#   --skip-cleanup : Skip Kubernetes resource cleanup (faster but may leave orphaned resources)
#   --force        : Skip confirmation prompts
#
# Author: LLASTA Project
# Version: 1.0
################################################################################

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Parse arguments
SKIP_CLEANUP=false
FORCE=false

for arg in "$@"; do
    case $arg in
        --skip-cleanup)
            SKIP_CLEANUP=true
            shift
            ;;
        --force)
            FORCE=true
            shift
            ;;
        *)
            echo -e "${RED}Unknown argument: $arg${NC}"
            echo "Usage: $0 [--skip-cleanup] [--force]"
            exit 1
            ;;
    esac
done

################################################################################
# Helper Functions
################################################################################

print_header() {
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
}

print_step() {
    echo -e "${GREEN}▶${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC}  $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

confirm() {
    if [ "$FORCE" = true ]; then
        return 0
    fi
    
    echo -e "${YELLOW}$1 (y/N)${NC}"
    read -r response
    case "$response" in
        [yY][eE][sS]|[yY]) 
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

################################################################################
# Pre-flight Checks
################################################################################

print_header "Pre-flight Checks"

# Check if kubectl is installed
if ! command -v kubectl &> /dev/null; then
    print_error "kubectl not found. Please install kubectl first."
    exit 1
fi
print_success "kubectl found"

# Check if terraform is installed
if ! command -v terraform &> /dev/null; then
    print_error "terraform not found. Please install terraform first."
    exit 1
fi
print_success "terraform found"

# Check if we're in the right directory
if [ ! -d "000-K8 deployment" ]; then
    print_error "Must be run from LLASTA project root directory"
    exit 1
fi
print_success "Running from project root"

# Check if cluster exists
print_step "Checking if cluster exists..."
if kubectl cluster-info &> /dev/null; then
    CLUSTER_EXISTS=true
    CLUSTER_NAME=$(kubectl config current-context 2>/dev/null || echo "unknown")
    print_success "Cluster found: $CLUSTER_NAME"
else
    CLUSTER_EXISTS=false
    print_warning "No active cluster found (this is OK if already destroyed)"
fi

################################################################################
# Confirmation
################################################################################

print_header "Destroy Confirmation"

echo -e "${RED}⚠  WARNING: This will destroy the following:${NC}"
echo "   • EKS Cluster (llasta)"
echo "   • All Kubernetes resources (pods, services, deployments, etc.)"
echo "   • EC2 instances (CPU and GPU nodes)"
echo "   • IAM roles and policies"
echo "   • VPC resources (subnets, security groups)"
echo ""
echo -e "${GREEN}✓ Will be PRESERVED:${NC}"
echo "   • EBS volumes (PVCs: model weights, FAISS data)"
echo "   • These will be reused on next deployment"
echo ""
echo -e "${YELLOW}   Estimated time: 12-15 minutes${NC}"
echo ""

if ! confirm "Are you sure you want to destroy the cluster?"; then
    print_warning "Destroy cancelled by user"
    exit 0
fi

################################################################################
# Step 1: Clean up Kubernetes resources (optional but recommended)
################################################################################

if [ "$SKIP_CLEANUP" = false ] && [ "$CLUSTER_EXISTS" = true ]; then
    print_header "Step 1: Cleaning up Kubernetes resources"
    
    print_step "This speeds up destroy by removing pods before Terraform"
    print_warning "EBS volumes (PVCs) will be preserved for reuse"
    
    # Delete pods, deployments, services (but NOT PVCs)
    print_step "Deleting pods, deployments, and services in namespace 'llasta'..."
    kubectl delete deployments,statefulsets,daemonsets,services,configmaps,secrets --all -n llasta --timeout=60s 2>/dev/null || true
    print_success "Kubernetes resources deleted (PVCs preserved)"
    
    # Wait a bit for pods to terminate
    print_step "Waiting 20s for pods to terminate..."
    sleep 20
    print_success "Cleanup complete"
    
    print_success "Kubernetes cleanup complete (saved ~1-2 minutes)"
else
    if [ "$SKIP_CLEANUP" = true ]; then
        print_warning "Skipping Kubernetes cleanup (--skip-cleanup flag)"
    else
        print_warning "Skipping Kubernetes cleanup (no active cluster)"
    fi
fi

################################################################################
# Step 2: Terraform Destroy
################################################################################

print_header "Step 2: Terraform Destroy"

cd "000-K8 deployment" || exit 1

print_step "Running terraform destroy..."
print_warning "This will take 10-15 minutes (node groups are slow to destroy)"
echo ""

# Run terraform destroy with increased parallelism
if terraform destroy -auto-approve -parallelism=20; then
    print_success "Terraform destroy completed successfully"
else
    print_error "Terraform destroy failed"
    print_warning "You may need to manually clean up resources in AWS Console"
    exit 1
fi

cd ..

################################################################################
# Step 3: Verify Cleanup
################################################################################

print_header "Step 3: Verification"

print_step "Checking for remaining resources..."

# Check if cluster still exists
if kubectl cluster-info &> /dev/null; then
    print_warning "Cluster still accessible (may be cached kubeconfig)"
    print_step "Removing kubeconfig entry..."
    kubectl config delete-context "$CLUSTER_NAME" 2>/dev/null || true
    kubectl config delete-cluster "$CLUSTER_NAME" 2>/dev/null || true
    print_success "Kubeconfig cleaned"
else
    print_success "Cluster no longer accessible"
fi

################################################################################
# Summary
################################################################################

print_header "Destroy Complete"

echo -e "${GREEN}✓ Cluster destroyed successfully${NC}"
echo ""
echo "Summary:"
echo "  • EKS cluster: Deleted"
echo "  • Node groups: Deleted"
echo "  • IAM roles: Deleted"
echo "  • VPC resources: Deleted"
echo ""
echo -e "${GREEN}Preserved:${NC}"
echo "  • EBS volumes (PVCs): Ready for reuse"
echo ""
echo -e "${BLUE}Next steps:${NC}"
echo "  • Verify in AWS Console that all resources are deleted"
echo "  • Check for any orphaned resources (Load Balancers, EBS volumes)"
echo "  • To redeploy: ./Deploy.sh"
echo ""
print_success "Done! 🎉"
