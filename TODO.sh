#!/bin/bash
# TODO: Implement Option B — manage the EBS CSI addon via Terraform (not via AWS CLI in Deploy.sh)
# 
# Goal:
#   - Move aws-ebs-csi-driver addon management into Terraform for full idempotency and a single source of truth.
#   - Remove addon creation/update/wait logic from Deploy.sh.
#
# Steps:
# 1) In file: "000-K8 deployment/eks.tf"
#    Add a Terraform resource aws_eks_addon similar to:
#
#    resource "aws_eks_addon" "ebs_csi" {
#      cluster_name             = aws_eks_cluster.this.name
#      addon_name               = "aws-ebs-csi-driver"
#      service_account_role_arn = aws_iam_role.ebs_csi_driver_role.arn
#      resolve_conflicts        = "OVERWRITE"   # or NONE / PRESERVE if preferred
#      # Optional: addon_version = "v1.x.x-eksbuild.x" to pin version
#      depends_on = [
#        aws_iam_openid_connect_provider.eks_oidc,
#        aws_iam_role.ebs_csi_driver_role,
#        aws_iam_role_policy_attachment.ebs_csi_driver_policy,
#      ]
#    }
#
# 2) Remove from Deploy.sh (Phase 4) the CLI-based addon install/update sequence:
#    - aws eks create-addon ...
#    - aws eks update-addon ...
#    - aws eks wait addon-active ...
#    And the subsequent manual verification can be replaced by:
#    - kubectl get pods -n kube-system | grep ebs-csi (optional)
#
# 3) Keep IAM role for EBS CSI (IRSA) in Terraform as defined:
#    - aws_iam_role.ebs_csi_driver_role (trust policy with OIDC + SA ebs-csi-controller-sa)
#    - aws_iam_role_policy_attachment.ebs_csi_driver_policy
#
# 4) Run:
#    - (From 000-K8 deployment/) terraform init
#    - terraform plan  (ensure the addon will be created or updated by Terraform)
#    - terraform apply
#
# 5) Validation after apply:
#    - kubectl get pods -n kube-system | grep ebs-csi
#    - kubectl describe daemonset ebs-csi-node -n kube-system (optional)
#
# Notes:
# - If the addon already exists (created previously by CLI), Terraform may need an import:
#     terraform import aws_eks_addon.ebs_csi llasta:aws-ebs-csi-driver
#   Then run terraform plan/apply again.
# - Using Terraform for the addon eliminates CLI race conditions and "already exists" errors.
